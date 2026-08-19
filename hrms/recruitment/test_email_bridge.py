from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "deploy" / "ayp_ats_email_bridge.py"
RUNNER_SPEC = importlib.util.spec_from_file_location("ayp_ats_email_runner_under_test", RUNNER_PATH)
if RUNNER_SPEC is None or RUNNER_SPEC.loader is None:
	raise ImportError(f"Could not load {RUNNER_PATH}")
RUNNER_MODULE = importlib.util.module_from_spec(RUNNER_SPEC)
sys.modules[RUNNER_SPEC.name] = RUNNER_MODULE
RUNNER_SPEC.loader.exec_module(RUNNER_MODULE)
MATCHING_PATH = ROOT / "hrms" / "recruitment" / "matching.py"
MATCHING_SPEC = importlib.util.spec_from_file_location("email_bridge_matching_under_test", MATCHING_PATH)
if MATCHING_SPEC is None or MATCHING_SPEC.loader is None:
	raise ImportError(f"Could not load {MATCHING_PATH}")
MATCHING_MODULE = importlib.util.module_from_spec(MATCHING_SPEC)
MATCHING_SPEC.loader.exec_module(MATCHING_MODULE)
EMAIL_RECRUITMENT_SOURCE = MATCHING_MODULE.EMAIL_RECRUITMENT_SOURCE


class FakeValidationError(Exception):
	pass


class FakeDuplicateEntryError(FakeValidationError):
	pass


class FakeCandidateCVSecurityError(FakeValidationError):
	pass


class FakeFlags(dict):
	__getattr__ = dict.get

	def __setattr__(self, key, value):
		self[key] = value


class FakeRow(dict):
	__getattr__ = dict.get


class FakeDB:
	def __init__(self, owner):
		self.owner = owner

	def has_column(self, doctype, fieldname):
		return doctype == "Job Applicant" and fieldname in {
			"custom_ayp_email_provenance",
			"custom_ayp_email_file_name",
			"custom_ayp_email_message_id",
			"custom_ayp_email_received_on",
			"custom_ayp_email_subject",
			"custom_ayp_email_current_vacancy_consent",
			"custom_ayp_email_consent_notice_version",
			"custom_ayp_email_consent_evidence_sha256",
		}

	def exists(self, doctype, name):
		if doctype == "Job Applicant Source":
			return name == EMAIL_RECRUITMENT_SOURCE
		if doctype == "File":
			return (
				name.get("content_hash") in self.owner.existing_private_hashes and name.get("is_private") == 1
			)
		return False

	def get_value(self, doctype, filters, fieldname, as_dict=False):
		if doctype == "Job Opening":
			return self.owner.job_opening_status
		if doctype == "Job Applicant":
			message_key = filters["custom_ayp_email_message_id"]
			return self.owner.applicants_by_message.get(message_key)
		raise AssertionError(f"Unexpected get_value call: {(doctype, filters, fieldname, as_dict)}")

	def sql(self, query, params, as_dict=False):
		if "FOR UPDATE" not in query:
			raise AssertionError(f"Unexpected SQL: {query}")
		self.owner.locking_reads += 1
		if "FROM `tabJob Opening`" in query:
			self.owner.locking_read_tables.append("Job Opening")
			return [FakeRow({"name": name, "status": "Open"}) for name in self.owner.open_job_openings]
		if "FROM `tabJob Applicant`" in query:
			self.owner.locking_read_tables.append("Job Applicant")
			row = self.owner.applicants_by_message.get(params[0])
			return [row] if row else []
		if "FROM `tabFile`" in query:
			self.owner.locking_read_tables.append("File")
			file_doc = self.owner.files_by_name.get(params[0])
			return [FakeRow(vars(file_doc))] if file_doc else []
		raise AssertionError(f"Unexpected SQL: {query}")

	def delete(self, doctype, filters):
		if doctype != "File" or set(filters) != {"name"}:
			raise AssertionError(f"Unexpected direct delete: {(doctype, filters)}")
		self.owner.direct_db_deletes.append((doctype, filters["name"]))
		self.owner.files_by_name.pop(filters["name"], None)

	def commit(self):
		raise AssertionError("ingest_email_payload must not commit")

	def rollback(self, *args, **kwargs):
		raise AssertionError("ingest_email_payload must not roll back the caller transaction")


class FakeApplicant:
	def __init__(self, values, owner):
		self.values = values
		self.owner = owner
		self.name = "candidate@example.com"
		self.flags = FakeFlags()

	def insert(self):
		if self.owner.duplicate_winner_on_insert:
			loser_file = self.owner.files_by_url[self.values["resume_attachment"]]
			file_doc = types.SimpleNamespace(**vars(loser_file))
			file_doc.name = "FILE-WINNER"
			file_doc.attached_to_doctype = "Job Applicant"
			file_doc.attached_to_name = "race-winner@example.com"
			file_doc.attached_to_field = "resume_attachment"
			self.owner.files_by_name[file_doc.name] = file_doc
			winner_values = {**self.values, "custom_ayp_email_file_name": file_doc.name}
			winner = FakeRow(
				{
					**winner_values,
					"name": "race-winner@example.com",
					"custom_cv_sha256": hashlib.sha256(file_doc.content).hexdigest(),
				}
			)
			self.owner.applicants_by_message[self.values["custom_ayp_email_message_id"]] = winner
			raise FakeDuplicateEntryError("unique message id")
		if self.owner.fail_applicant_insert:
			raise FakeValidationError("insert failed")
		file_doc = self.owner.files_by_url[self.values["resume_attachment"]]
		if self.flags.ayp_candidate_cv_file_name != file_doc.name:
			raise AssertionError("Applicant must carry the exact scanned File.name")
		file_content = file_doc.content
		file_doc.attached_to_doctype = "Job Applicant"
		file_doc.attached_to_name = self.name
		file_doc.attached_to_field = "resume_attachment"
		self.values["custom_cv_sha256"] = hashlib.sha256(file_content).hexdigest()
		row = FakeRow(self.values)
		row.name = self.name
		self.owner.inserted_applicants.append(row)
		self.owner.applicants_by_message[self.values["custom_ayp_email_message_id"]] = row
		return self


class FakeFile:
	def __init__(self, values, owner):
		self.values = values
		self.owner = owner
		self.flags = FakeFlags()

	def insert(self):
		content = self.values.get("content")
		if not isinstance(content, bytes):
			raise AssertionError("File.content must preserve the original bytes")
		if self.values.get("is_private") != 1:
			raise AssertionError("CV must first be stored as a detached private File")
		if any(self.values.get(fieldname) for fieldname in ("attached_to_doctype", "attached_to_name")):
			raise AssertionError("CV File must remain detached until Applicant.insert")
		self.name = f"FILE-{len(self.owner.saved_files) + 1}"
		self.file_name = self.values["file_name"]
		self.file_url = f"/private/files/{self.file_name}"
		self.file_size = len(content)
		self.is_private = 1
		self.content = content
		self.content_hash = hashlib.md5(content, usedforsecurity=False).hexdigest()
		self.attached_to_doctype = None
		self.attached_to_name = None
		self.attached_to_field = None
		self.custom_av_scan_status = None
		self.custom_av_scan_engine = None
		self.custom_av_scanned_on = None
		self.custom_cv_sha256 = None
		self.owner.saved_files.append(self)
		self.owner.files_by_url[self.file_url] = self
		self.owner.files_by_name[self.name] = self
		return self


class FakeFrappe(types.ModuleType):
	def __init__(self):
		super().__init__("frappe")
		self.ValidationError = FakeValidationError
		self.DuplicateEntryError = FakeDuplicateEntryError
		self.PermissionError = PermissionError
		self.session = types.SimpleNamespace(user="operator@example.com")
		self.flags = FakeFlags()
		self.conf = {}
		self.db = FakeDB(self)
		self.job_opening_status = "Open"
		self.open_job_openings = ["HR-OPN-2026-0001"]
		self.files_by_url = {}
		self.files_by_name = {}
		self.deleted_files = []
		self.direct_db_deletes = []
		self.inserted_applicants = []
		self.applicants_by_message = {}
		self.saved_files = []
		self.legacy_save_file_calls = 0
		self.existing_private_hashes = set()
		self.preexisting_file_names = set()
		self.fail_applicant_insert = False
		self.duplicate_winner_on_insert = False
		self.locking_reads = 0
		self.locking_read_tables = []

	def _(self, text):
		return text

	def get_doc(self, values, name=None):
		if values == "File" and name:
			return self.files_by_name[name]
		if isinstance(values, dict) and values.get("doctype") == "File":
			return FakeFile(values, self)
		if not isinstance(values, dict) or values.get("doctype") != "Job Applicant":
			raise AssertionError(f"Unexpected document: {values}")
		return FakeApplicant(values, self)

	def delete_doc(self, doctype, name, **kwargs):
		self.deleted_files.append((doctype, name, kwargs))

	def get_all(self, doctype, filters=None, pluck=None):
		if doctype == "Job Opening" and filters == {"status": "Open"} and pluck == "name":
			return list(self.open_job_openings)
		if doctype != "File" or pluck != "name":
			raise AssertionError(f"Unexpected get_all call: {(doctype, filters, pluck)}")
		return sorted(self.preexisting_file_names)

	def sendmail(self, *args, **kwargs):
		raise AssertionError("email bridge must not send email")

	def enqueue(self, *args, **kwargs):
		raise AssertionError("email bridge must not enqueue")


MODULE_PATH = ROOT / "hrms" / "recruitment" / "email_bridge.py"


def load_email_bridge(fake_frappe):
	module_names = (
		"frappe",
		"frappe.utils",
		"frappe.utils.file_manager",
		"hrms.recruitment.matching",
		"hrms.security.candidate_cv",
	)
	original_modules = {name: sys.modules.get(name) for name in module_names}

	frappe_utils = types.ModuleType("frappe.utils")

	def validate_email_address(value, throw=False):
		valid = value.count("@") == 1 and "." in value.rsplit("@", 1)[1] and " " not in value
		if not valid and throw:
			raise FakeValidationError("invalid email")
		return value if valid else ""

	frappe_utils.validate_email_address = validate_email_address
	file_manager = types.ModuleType("frappe.utils.file_manager")

	def save_file(*args, **kwargs):
		fake_frappe.legacy_save_file_calls += 1
		raise AssertionError("email bridge must not use the lossy save_file helper")

	file_manager.save_file = save_file
	file_manager.get_content_hash = lambda content: hashlib.md5(content, usedforsecurity=False).hexdigest()
	candidate_cv = types.ModuleType("hrms.security.candidate_cv")
	candidate_cv.MAX_CV_BYTES = 32
	candidate_cv.CandidateCVSecurityError = FakeCandidateCVSecurityError

	def validate_cv_file(filename, content):
		if not filename.lower().endswith((".pdf", ".doc", ".docx", ".png", ".jpg", ".jpeg")):
			raise FakeCandidateCVSecurityError("invalid extension")
		if not content or len(content) > candidate_cv.MAX_CV_BYTES:
			raise FakeCandidateCVSecurityError("invalid size")

	candidate_cv.validate_cv_file = validate_cv_file

	def scan_stored_candidate_cv(file_doc):
		sha256 = hashlib.sha256(file_doc.content).hexdigest()
		file_doc.custom_av_scan_status = "Clean"
		file_doc.custom_av_scan_engine = "ClamAV"
		file_doc.custom_av_scanned_on = "2026-08-15 17:15:00"
		file_doc.custom_cv_sha256 = sha256
		return sha256

	candidate_cv.scan_stored_candidate_cv = scan_stored_candidate_cv
	candidate_cv.read_stored_candidate_cv_bytes = lambda file_doc: file_doc.content

	try:
		sys.modules["frappe"] = fake_frappe
		sys.modules["frappe.utils"] = frappe_utils
		sys.modules["frappe.utils.file_manager"] = file_manager
		sys.modules["hrms.recruitment.matching"] = MATCHING_MODULE
		sys.modules["hrms.security.candidate_cv"] = candidate_cv
		spec = importlib.util.spec_from_file_location("email_bridge_under_test", MODULE_PATH)
		if spec is None or spec.loader is None:
			raise ImportError(f"Could not load {MODULE_PATH}")
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		return module
	finally:
		for name, original in original_modules.items():
			if original is None:
				sys.modules.pop(name, None)
			else:
				sys.modules[name] = original


def email_payload(content=b"synthetic cv"):
	payload = {
		"graph_message_id": "AAMk-fallback-id",
		"received_on": "2026-08-15T13:14:15-04:00",
		"subject": "Solicitud de empleo HR-OPN-2026-0001",
		"sender_email": "Candidate@Example.com",
		"sender_name": "Candidate Example",
		"consent_current_vacancy": True,
		"consent_notice_version": "AYP-RH-EMAIL-CURRENT-VACANCY-2026-08-15-v1",
		"attachments": [
			{
				"name": "candidate.pdf",
				"content_base64": base64.b64encode(content).decode("ascii"),
			}
		],
	}
	evidence = {
		"canonical_consent": (
			"he leido el aviso de privacidad de aro y pedal y autorizo el tratamiento "
			"de mis datos exclusivamente para esta vacante"
		),
		"format": "AYP-EMAIL-CONSENT-EVIDENCE-V1",
		"graph_message_id": payload["graph_message_id"],
		"mailbox": "empleos@aroypedal.com",
		"notice_version": payload["consent_notice_version"],
		"received_on": payload["received_on"],
	}
	canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
	payload["consent_evidence_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
	return payload


class TestEmailBridge(unittest.TestCase):
	def setUp(self):
		self.frappe = FakeFrappe()
		self.bridge = load_email_bridge(self.frappe)

	def test_happy_path_is_private_atomic_and_stores_no_body(self):
		content = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
		payload = email_payload(content)
		result = self.bridge.ingest_email_payload(payload)

		self.assertEqual(result, {"status": "created", "job_applicant": "candidate@example.com"})
		self.assertEqual(len(self.frappe.saved_files), 1)
		self.assertEqual(self.frappe.saved_files[0].is_private, 1)
		self.assertEqual(self.frappe.saved_files[0].content, content)
		self.assertEqual(self.frappe.legacy_save_file_calls, 0)
		applicant = self.frappe.inserted_applicants[0]
		self.assertEqual(applicant.email_id, "candidate@example.com")
		self.assertEqual(applicant.source, EMAIL_RECRUITMENT_SOURCE)
		self.assertEqual(applicant.custom_data_processing_consent, 0)
		self.assertEqual(applicant.custom_ayp_email_provenance, 1)
		self.assertEqual(applicant.custom_ayp_email_file_name, "FILE-1")
		self.assertEqual(applicant.custom_ayp_email_current_vacancy_consent, 1)
		self.assertEqual(
			applicant.custom_ayp_email_consent_notice_version,
			self.bridge.EMAIL_CONSENT_NOTICE_VERSION,
		)
		self.assertEqual(
			applicant.custom_ayp_email_consent_evidence_sha256,
			email_payload()["consent_evidence_sha256"],
		)
		self.assertEqual(applicant.custom_privacy_notice_version, self.bridge.EMAIL_CONSENT_NOTICE_VERSION)
		self.assertIsNone(applicant.custom_candidate_profile)
		self.assertEqual(applicant.custom_ayp_email_subject, "Solicitud de empleo HR-OPN-2026-0001")
		self.assertEqual(applicant.custom_ayp_email_received_on, datetime(2026, 8, 15, 17, 14, 15))
		self.assertNotIn("body", applicant)
		self.assertNotIn("bodyPreview", applicant)
		self.assertNotIn("cover_letter", applicant)
		self.assertNotIn("notes", applicant)
		self.assertFalse(self.bridge.frappe.flags.in_import)
		self.assertFalse(self.bridge.frappe.flags.mute_emails)

	def test_versioned_runner_payload_is_accepted_without_schema_translation(self):
		message = {
			"id": "GRAPH-IMMUTABLE-CONTRACT-ID",
			"internetMessageId": "<sender-controlled@example.test>",
			"receivedDateTime": "2026-08-15T17:14:15Z",
			"subject": "Solicitud de empleo HR-OPN-2026-0001",
			"sender": {"emailAddress": {"address": "ats-canary@aroypedal.com", "name": "Candidate Example"}},
			"from": {"emailAddress": {"address": "ats-canary@aroypedal.com", "name": "Candidate Example"}},
		}
		attachment = {
			"name": "candidate.pdf",
			"contentBytes": base64.b64encode(b"synthetic cv").decode("ascii"),
		}
		candidate = RUNNER_MODULE._build_candidate(message, attachment)

		result = self.bridge.ingest_email_payload(candidate.payload)

		self.assertEqual(result["status"], "created")
		self.assertEqual(
			self.frappe.inserted_applicants[0].custom_ayp_email_consent_evidence_sha256,
			candidate.payload["consent_evidence_sha256"],
		)

	def test_exact_duplicate_returns_existing_without_new_file_or_applicant(self):
		first = self.bridge.ingest_email_payload(email_payload())
		self.frappe.locking_read_tables.clear()
		second = self.bridge.ingest_email_payload(email_payload())

		self.assertEqual(first["job_applicant"], second["job_applicant"])
		self.assertEqual(second["status"], "already_processed")
		self.assertEqual(len(self.frappe.saved_files), 1)
		self.assertEqual(len(self.frappe.inserted_applicants), 1)
		self.assertEqual(self.frappe.locking_read_tables, ["Job Opening", "Job Applicant", "File"])

	def test_duplicate_fails_closed_when_exact_file_is_missing(self):
		self.bridge.ingest_email_payload(email_payload())
		self.frappe.files_by_name.clear()
		with self.assertRaisesRegex(self.bridge.EmailBridgeError, "archivo único"):
			self.bridge.ingest_email_payload(email_payload())

	def test_duplicate_fails_closed_when_exact_file_is_public(self):
		self.bridge.ingest_email_payload(email_payload())
		self.frappe.files_by_name["FILE-1"].is_private = 0
		with self.assertRaisesRegex(self.bridge.EmailBridgeError, "evidencia privada"):
			self.bridge.ingest_email_payload(email_payload())

	def test_duplicate_fails_closed_when_exact_file_bytes_drift(self):
		self.bridge.ingest_email_payload(email_payload())
		self.frappe.files_by_name["FILE-1"].content = b"tampered"
		with self.assertRaisesRegex(self.bridge.EmailBridgeError, "evidencia privada"):
			self.bridge.ingest_email_payload(email_payload())

	def test_concurrent_unique_race_returns_verified_winner_and_cleans_loser_file(self):
		self.frappe.duplicate_winner_on_insert = True
		result = self.bridge.ingest_email_payload(email_payload())
		self.assertEqual(
			result,
			{"status": "already_processed", "job_applicant": "race-winner@example.com"},
		)
		self.assertEqual(self.frappe.deleted_files, [])
		self.assertEqual(self.frappe.direct_db_deletes, [("File", "FILE-1")])
		self.assertEqual(self.frappe.locking_reads, 5)
		self.assertEqual(
			self.frappe.locking_read_tables,
			["Job Opening", "Job Applicant", "Job Opening", "Job Applicant", "File"],
		)
		self.assertIn("FILE-WINNER", self.frappe.files_by_name)

	def test_duplicate_message_key_with_changed_payload_fails_closed(self):
		self.bridge.ingest_email_payload(email_payload())
		changed = email_payload()
		changed["subject"] = "Different subject HR-OPN-2026-0001"

		with self.assertRaisesRegex(self.bridge.EmailBridgeError, "datos diferentes"):
			self.bridge.ingest_email_payload(changed)
		self.assertEqual(len(self.frappe.inserted_applicants), 1)

	def test_multiple_attachments_fail_closed(self):
		payload = email_payload()
		payload["attachments"].append(dict(payload["attachments"][0]))

		with self.assertRaisesRegex(self.bridge.EmailBridgeError, "exactamente un CV"):
			self.bridge.ingest_email_payload(payload)
		self.assertEqual(self.frappe.saved_files, [])

	def test_missing_or_wrong_current_vacancy_consent_fails_before_file_storage(self):
		missing = email_payload()
		missing["consent_current_vacancy"] = False
		with self.assertRaisesRegex(self.bridge.EmailBridgeError, "consentimiento explícito"):
			self.bridge.ingest_email_payload(missing)

		wrong_version = email_payload()
		wrong_version["consent_notice_version"] = "untrusted-version"
		with self.assertRaisesRegex(self.bridge.EmailBridgeError, "no está autorizada"):
			self.bridge.ingest_email_payload(wrong_version)
		self.assertEqual(self.frappe.saved_files, [])

	def test_consent_evidence_digest_must_match_immutable_graph_message(self):
		payload = email_payload()
		payload["consent_evidence_sha256"] = "0" * 64
		with self.assertRaisesRegex(self.bridge.EmailBridgeError, "evidencia del consentimiento"):
			self.bridge.ingest_email_payload(payload)
		self.assertEqual(self.frappe.saved_files, [])

	def test_subject_without_vacancy_code_uses_only_open_authorized_vacancy(self):
		payload = email_payload()
		payload["subject"] = "Solicitud para otra vacante"
		result = self.bridge.ingest_email_payload(payload)
		self.assertEqual(result["status"], "created")
		self.assertEqual(self.frappe.inserted_applicants[0].job_title, "HR-OPN-2026-0001")

	def test_invalid_base64_and_oversize_fail_before_file_storage(self):
		invalid = email_payload()
		invalid["attachments"][0]["content_base64"] = "%%%not-base64%%%"
		with self.assertRaisesRegex(self.bridge.EmailBridgeError, "base64"):
			self.bridge.ingest_email_payload(invalid)

		oversized = email_payload(b"x" * 33)
		with self.assertRaises((self.bridge.EmailBridgeError, FakeCandidateCVSecurityError)):
			self.bridge.ingest_email_payload(oversized)
		self.assertEqual(self.frappe.saved_files, [])

	def test_non_open_override_fails_closed(self):
		self.bridge.frappe.conf[self.bridge.JOB_OPENING_CONFIG_KEY] = "HR-OPN-CLOSED"
		self.frappe.job_opening_status = "Closed"
		with self.assertRaisesRegex(self.bridge.EmailBridgeError, "no es la vacante autorizada"):
			self.bridge.ingest_email_payload(email_payload())
		self.assertEqual(self.frappe.saved_files, [])

	def test_zero_or_multiple_open_vacancies_fail_before_file_storage(self):
		for open_job_openings in (
			[],
			["HR-OPN-2026-0002"],
			["HR-OPN-2026-0001", "HR-OPN-2026-0002"],
		):
			with self.subTest(open_job_openings=open_job_openings):
				self.frappe.open_job_openings = open_job_openings
				with self.assertRaisesRegex(
					self.bridge.EmailBridgeAdmissionError, "exactamente una vacante abierta"
				) as raised:
					self.bridge.ingest_email_payload(email_payload())
				self.assertEqual(raised.exception.code, "blocked_single_open_vacancy_required")
				self.assertEqual(self.frappe.saved_files, [])

	def test_vacancy_authority_drift_fails_before_file_storage(self):
		original = self.bridge._job_opening
		calls = 0

		def mutate_after_first_authorization():
			nonlocal calls
			calls += 1
			result = original()
			if calls == 1:
				self.frappe.open_job_openings.append("HR-OPN-2026-0002")
			return result

		with (
			patch.object(self.bridge, "_job_opening", side_effect=mutate_after_first_authorization),
			self.assertRaisesRegex(self.bridge.EmailBridgeAdmissionError, "exactamente una vacante abierta"),
		):
			self.bridge.ingest_email_payload(email_payload())
		self.assertEqual(self.frappe.saved_files, [])

	def test_insert_failure_defers_file_cleanup_to_caller_rollback(self):
		self.frappe.fail_applicant_insert = True
		with self.assertRaisesRegex(FakeValidationError, "insert failed"):
			self.bridge.ingest_email_payload(email_payload())
		self.assertEqual(self.frappe.deleted_files, [])
		self.assertEqual(self.frappe.direct_db_deletes, [])

	def test_insert_failure_never_deletes_shared_blob_before_rollback(self):
		content = b"synthetic cv"
		self.frappe.existing_private_hashes.add(hashlib.md5(content, usedforsecurity=False).hexdigest())
		self.frappe.fail_applicant_insert = True

		with self.assertRaisesRegex(FakeValidationError, "insert failed"):
			self.bridge.ingest_email_payload(email_payload(content))

		self.assertEqual(self.frappe.deleted_files, [])
		self.assertEqual(self.frappe.direct_db_deletes, [])


if __name__ == "__main__":
	unittest.main()
