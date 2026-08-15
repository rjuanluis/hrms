from __future__ import annotations

import base64
import hashlib
import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MATCHING_PATH = ROOT / "hrms" / "recruitment" / "matching.py"
MATCHING_SPEC = importlib.util.spec_from_file_location("email_bridge_matching_under_test", MATCHING_PATH)
if MATCHING_SPEC is None or MATCHING_SPEC.loader is None:
	raise ImportError(f"Could not load {MATCHING_PATH}")
MATCHING_MODULE = importlib.util.module_from_spec(MATCHING_SPEC)
MATCHING_SPEC.loader.exec_module(MATCHING_MODULE)
EMAIL_RECRUITMENT_SOURCE = MATCHING_MODULE.EMAIL_RECRUITMENT_SOURCE


class FakeValidationError(Exception):
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
			"custom_ayp_email_message_id",
			"custom_ayp_email_received_on",
			"custom_ayp_email_subject",
			"custom_ayp_email_current_vacancy_consent",
			"custom_ayp_email_consent_notice_version",
		}

	def exists(self, doctype, name):
		if doctype == "Job Applicant Source":
			return name == EMAIL_RECRUITMENT_SOURCE
		if doctype == "File":
			return name.get("content_hash") in self.owner.existing_private_hashes and name.get("is_private") == 1
		return False

	def get_value(self, doctype, filters, fieldname, as_dict=False):
		if doctype == "Job Opening":
			return self.owner.job_opening_status
		if doctype == "Job Applicant":
			message_key = filters["custom_ayp_email_message_id"]
			return self.owner.applicants_by_message.get(message_key)
		raise AssertionError(f"Unexpected get_value call: {(doctype, filters, fieldname, as_dict)}")

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
		if self.owner.fail_applicant_insert:
			raise FakeValidationError("insert failed")
		file_content = self.owner.files_by_url[self.values["resume_attachment"]].content
		self.values["custom_cv_sha256"] = hashlib.sha256(file_content).hexdigest()
		row = FakeRow(self.values)
		row.name = self.name
		self.owner.inserted_applicants.append(row)
		self.owner.applicants_by_message[self.values["custom_ayp_email_message_id"]] = row
		return self


class FakeFrappe(types.ModuleType):
	def __init__(self):
		super().__init__("frappe")
		self.ValidationError = FakeValidationError
		self.PermissionError = PermissionError
		self.session = types.SimpleNamespace(user="operator@example.com")
		self.flags = FakeFlags()
		self.conf = {}
		self.db = FakeDB(self)
		self.job_opening_status = "Open"
		self.files_by_url = {}
		self.deleted_files = []
		self.inserted_applicants = []
		self.applicants_by_message = {}
		self.saved_files = []
		self.existing_private_hashes = set()
		self.fail_applicant_insert = False

	def _(self, text):
		return text

	def get_doc(self, values):
		if values.get("doctype") != "Job Applicant":
			raise AssertionError(f"Unexpected document: {values}")
		return FakeApplicant(values, self)

	def delete_doc(self, doctype, name, **kwargs):
		self.deleted_files.append((doctype, name, kwargs))

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

	def save_file(filename, content, dt, dn, is_private=0):
		if (dt, dn, is_private) != (None, None, 1):
			raise AssertionError("CV must first be stored as a detached private File")
		file_doc = types.SimpleNamespace(
			name=f"FILE-{len(fake_frappe.saved_files) + 1}",
			file_name=filename,
			file_url=f"/private/files/{filename}",
			file_size=len(content),
			is_private=1,
			content=content,
		)
		fake_frappe.saved_files.append(file_doc)
		fake_frappe.files_by_url[file_doc.file_url] = file_doc
		return file_doc

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
	candidate_cv.scan_stored_candidate_cv = lambda file_doc: hashlib.sha256(file_doc.content).hexdigest()

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
	return {
		"message_id": "<message-1@example.com>",
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


class TestEmailBridge(unittest.TestCase):
	def setUp(self):
		self.frappe = FakeFrappe()
		self.bridge = load_email_bridge(self.frappe)

	def test_happy_path_is_private_atomic_and_stores_no_body(self):
		result = self.bridge.ingest_email_payload(email_payload())

		self.assertEqual(result, {"status": "created", "job_applicant": "candidate@example.com"})
		self.assertEqual(len(self.frappe.saved_files), 1)
		self.assertEqual(self.frappe.saved_files[0].is_private, 1)
		applicant = self.frappe.inserted_applicants[0]
		self.assertEqual(applicant.email_id, "candidate@example.com")
		self.assertEqual(applicant.source, EMAIL_RECRUITMENT_SOURCE)
		self.assertEqual(applicant.custom_data_processing_consent, 0)
		self.assertEqual(applicant.custom_ayp_email_current_vacancy_consent, 1)
		self.assertEqual(
			applicant.custom_ayp_email_consent_notice_version,
			self.bridge.EMAIL_CONSENT_NOTICE_VERSION,
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

	def test_exact_duplicate_returns_existing_without_new_file_or_applicant(self):
		first = self.bridge.ingest_email_payload(email_payload())
		second = self.bridge.ingest_email_payload(email_payload())

		self.assertEqual(first["job_applicant"], second["job_applicant"])
		self.assertEqual(second["status"], "already_processed")
		self.assertEqual(len(self.frappe.saved_files), 1)
		self.assertEqual(len(self.frappe.inserted_applicants), 1)

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

	def test_missing_authoritative_vacancy_fails_before_file_storage(self):
		payload = email_payload()
		payload["subject"] = "Solicitud para otra vacante"
		with self.assertRaisesRegex(self.bridge.EmailBridgeError, "no identifica la vacante autorizada"):
			self.bridge.ingest_email_payload(payload)
		self.assertEqual(self.frappe.saved_files, [])

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

	def test_insert_failure_cleans_file_without_commit_or_rollback(self):
		self.frappe.fail_applicant_insert = True
		with self.assertRaisesRegex(FakeValidationError, "insert failed"):
			self.bridge.ingest_email_payload(email_payload())
		self.assertEqual(self.frappe.deleted_files[0][0:2], ("File", "FILE-1"))

	def test_insert_failure_does_not_delete_a_preexisting_shared_blob(self):
		content = b"synthetic cv"
		self.frappe.existing_private_hashes.add(hashlib.md5(content, usedforsecurity=False).hexdigest())
		self.frappe.fail_applicant_insert = True

		with self.assertRaisesRegex(FakeValidationError, "insert failed"):
			self.bridge.ingest_email_payload(email_payload(content))

		self.assertEqual(self.frappe.deleted_files, [])


if __name__ == "__main__":
	unittest.main()
