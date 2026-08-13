from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


class FakeValidationError(Exception):
	pass


def _fixed_now_datetime():
	return "2026-08-11 20:00:00"


class FakeFlags(dict):
	__getattr__ = dict.get

	def __setattr__(self, key, value):
		self[key] = value


class FakeDB:
	def __init__(self, owner):
		self.owner = owner
		self.savepoints = []
		self.rollbacks = []
		self.sql_calls = []
		self.set_value_calls = []

	def savepoint(self, name):
		self.savepoints.append(name)

	def rollback(self, save_point=None):
		self.rollbacks.append(save_point)

	def count(self, doctype, filters=None):
		if doctype == "AYP Candidate Scorecard":
			return len(self.owner.scorecards)
		if doctype == "Job Applicant" and filters and filters.get("custom_candidate_profile"):
			return sum(
				getattr(doc, "custom_candidate_profile", None) == filters["custom_candidate_profile"]
				for doc in self.owner.docs.values()
			)
		return 0

	def exists(self, doctype, name):
		return name in self.owner.docs

	def sql(self, query, values=None, **kwargs):
		self.sql_calls.append((query, values))
		if kwargs.get("pluck") and "custom_candidate_profile" in query:
			if not values:
				raise AssertionError("profile SQL requires values")
			profile = values[0]
			return [
				name
				for name, doc in self.owner.docs.items()
				if getattr(doc, "custom_candidate_profile", None) == profile
			]
		return []

	def set_value(self, doctype, name, fieldname, value=None, update_modified=True):
		self.set_value_calls.append((doctype, name, fieldname, value, update_modified))
		doc = self.owner.docs.get(name)
		if not doc:
			return
		values = fieldname if isinstance(fieldname, dict) else {fieldname: value}
		for key, next_value in values.items():
			setattr(doc, key, next_value)


class FakeDoc:
	def __init__(self, name, status, job_title, candidate_profile=""):
		self.name = name
		self.status = status
		self.job_title = job_title
		self.custom_candidate_profile = candidate_profile
		self.saved = 0
		self.comments = []
		self.db_sets = []
		self.applicant_name = name
		self.email_id = f"{name.lower()}@example.com"
		self.phone_number = "8095550101"
		self.custom_cv_sha256 = ""
		self.custom_cv_processing_status = "Verificado manualmente"
		self.custom_privacy_notice_version = "v1"

	def get(self, key, default=None):
		return getattr(self, key, default)

	def save(self):
		self.saved += 1

	def add_comment(self, comment_type=None, text=None):
		self.comments.append((comment_type, text))

	def db_set(self, values):
		self.db_sets.append(values)
		for key, value in values.items():
			setattr(self, key, value)


class FakeProfile:
	def __init__(self, name, talent_pool_status="Activo", dedupe_status="Revisión requerida"):
		self.name = name
		self.talent_pool_status = talent_pool_status
		self.dedupe_status = dedupe_status
		self.do_not_contact = 0
		self.disposition_reason = ""
		self.candidate_name = name
		self.primary_email = ""
		self.primary_phone = ""
		self.normalized_email = ""
		self.normalized_phone = ""
		self.latest_cv_sha256 = ""
		self.privacy_notice_version = ""
		self.latest_application = ""
		self.application_count = 0
		self.saved = 0

	def save(self):
		self.saved += 1

	def db_set(self, values):
		for key, value in values.items():
			setattr(self, key, value)


class FakeEvent:
	def __init__(self, payload, owner):
		self.payload = payload
		self.owner = owner
		self.name = {
			"AYP Candidate Scorecard": "AYP-SCORE-0001",
			"AYP Candidate Profile": "PROFILE-NEW",
			"AYP Candidate Review Run": "AYP-REVIEW-RUN-0001",
		}.get(payload["doctype"], "EVENT-0001")
		for key, value in payload.items():
			setattr(self, key, value)

	def insert(self, ignore_permissions=False):
		if self.payload["doctype"] == "AYP Candidate Scorecard":
			self.owner.scorecards.append((self.payload, ignore_permissions, self.name))
		elif self.payload["doctype"] == "AYP Candidate Profile":
			profile = FakeProfile(self.name, dedupe_status=self.payload.get("dedupe_status", "Manual"))
			profile.do_not_contact = self.payload.get("do_not_contact", 0)
			self.owner.docs[self.name] = profile
			return profile
		elif self.payload["doctype"] == "AYP Candidate Review Run":
			self.owner.docs[self.name] = self
		else:
			self.owner.events.append((self.payload, ignore_permissions))
		return self


class FakeFrappe(types.ModuleType):
	def __init__(self):
		super().__init__("frappe")
		self.ValidationError = FakeValidationError
		self.PermissionError = PermissionError
		self.session = types.SimpleNamespace(user="reviewer@example.com")
		self.flags = FakeFlags()
		self.utils = types.SimpleNamespace(now_datetime=_fixed_now_datetime)
		self.db = FakeDB(self)
		self.docs = {}
		self.rows = []
		self.interview_rows = []
		self.interview_details = []
		self.interview_feedback = []
		self.profile_rows = []
		self.events = []
		self.scorecards = []
		self.scorecard_summaries = []
		self.get_list_calls = []
		self.permission_calls = []

	def _(self, text):
		return text

	def whitelist(self, function=None, **kwargs):
		def decorator(fn):
			fn.allowed_http_methods = kwargs.get("methods")
			return fn

		return decorator(function) if function else decorator

	def only_for(self, roles, message=False):
		self.allowed_roles = roles
		self.only_for_message = message

	def has_permission(self, doctype, permission_type="read", name=None, throw=False):
		self.permission_calls.append((doctype, permission_type, name, throw))
		return True

	def get_list(self, doctype, **kwargs):
		self.get_list_calls.append((doctype, kwargs))
		if doctype == "Job Applicant" and kwargs.get("fields") == [{"COUNT": "name", "as": "total"}]:
			return [{"total": len(self.rows)}]
		if doctype == "AYP Candidate Scorecard":
			return list(self.scorecard_summaries)
		if doctype == "Interview":
			return list(self.interview_rows)
		if doctype == "AYP Candidate Profile":
			return list(self.profile_rows)
		if doctype == "AYP Candidate Review Run":
			return [
				{"name": name}
				for name, doc in self.docs.items()
				if getattr(doc, "doctype", "") == "AYP Candidate Review Run"
				and getattr(doc, "run_status", "") in {"Frozen", "In Progress"}
			]
		if doctype == "Job Applicant" and kwargs.get("pluck") == "name":
			profile = kwargs.get("filters", {}).get("custom_candidate_profile")
			return [
				name
				for name, doc in self.docs.items()
				if getattr(doc, "custom_candidate_profile", None) == profile
			]
		return list(self.rows)

	def get_all(self, doctype, **kwargs):
		if doctype == "AYP Candidate Scorecard":
			return list(self.scorecard_summaries)
		if doctype == "Interview Detail":
			return list(self.interview_details)
		if doctype == "Interview Feedback":
			return list(self.interview_feedback)
		return []

	def get_doc(self, doctype_or_payload, name=None, for_update=False):
		if isinstance(doctype_or_payload, dict):
			return FakeEvent(doctype_or_payload, self)
		return self.docs[name]

	def generate_hash(self, length=12):
		return "batch123456"

	def throw(self, message, exc=FakeValidationError):
		raise exc(message)


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
	sys.path.insert(0, str(ROOT))
MODULE_PATH = ROOT / "hrms" / "hr" / "page" / "ayp_candidate_review" / "ayp_candidate_review.py"


def load_api(fake_frappe):
	module_names = (
		"frappe",
		"frappe.utils",
		"hrms.recruitment.candidate_document_service",
	)
	original_modules = {name: sys.modules.get(name) for name in module_names}
	candidate_document_service = types.ModuleType("hrms.recruitment.candidate_document_service")
	candidate_document_service.MANUAL_REVIEWABLE = frozenset({"Revisión manual", "Ilegible"})
	candidate_document_service.revalidate_candidate_document = lambda doc: None
	candidate_document_service.validate_candidate_ready_for_scoring = lambda doc: None
	try:
		sys.modules["frappe"] = fake_frappe
		sys.modules["frappe.utils"] = fake_frappe.utils
		sys.modules["hrms.recruitment.candidate_document_service"] = candidate_document_service
		spec = importlib.util.spec_from_file_location("candidate_review_api_under_test", MODULE_PATH)
		if spec is None or spec.loader is None:
			raise ImportError(f"No se pudo cargar el módulo de revisión desde {MODULE_PATH}")
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		return module
	finally:
		for name, original in original_modules.items():
			if original is None:
				sys.modules.pop(name, None)
			else:
				sys.modules[name] = original


class TestCandidateReviewAPI(unittest.TestCase):
	def test_locked_candidate_review_state_is_reloaded_authoritatively(self):
		root = Path(__file__).resolve().parents[2]
		source = (
			root / "hrms" / "hr" / "page" / "ayp_candidate_review" / "ayp_candidate_review.py"
		).read_text()
		self.assertGreaterEqual(source.count("revalidate_candidate_document(doc)"), 3)
		for fragment in (
			'frappe.get_doc("Job Applicant", applicant_name, for_update=True)',
			'frappe.get_doc("Job Applicant", applicant, for_update=True)',
			"frappe.get_doc(RUN_DOCTYPE, run, for_update=True)",
			'frappe.get_doc("AYP Candidate Profile", profile_name, for_update=True)',
			'frappe.get_doc("AYP Candidate Profile", source_profile_name, for_update=True)',
			'frappe.get_doc("AYP Candidate Profile", target_profile, for_update=True)',
		):
			self.assertIn(fragment, source)

	def setUp(self):
		self.frappe = FakeFrappe()
		self.api = load_api(self.frappe)
		self.api.acquire_candidate_identity_lock = lambda: None
		self.api.resolve_candidate_profile = lambda name, for_update=False: types.SimpleNamespace(name=name)
		self.api.revalidate_candidate_document = lambda doc: None
		self.api.validate_candidate_ready_for_scoring = lambda doc: None

	def test_get_candidates_returns_bounded_page_and_has_more(self):
		self.frappe.rows = [
			{"name": "A", "applicant_rating": 0.8},
			{"name": "B", "applicant_rating": 0.4},
			{"name": "C", "applicant_rating": 0.2},
		]
		result = self.api.get_candidates(
			filters={"job_title": "JOB-1", "status": "Open", "minimum_rating": 2},
			start=0,
			page_length=2,
		)
		self.assertEqual([row["name"] for row in result["rows"]], ["A", "B"])
		self.assertEqual(result["rows"][0]["rating_out_of_five"], 4.0)
		self.assertTrue(result["has_more"])
		self.assertEqual(result["total_count"], 3)
		_, query = next(
			call
			for call in self.frappe.get_list_calls
			if call[0] == "Job Applicant" and call[1].get("start") == 0
		)
		self.assertEqual(query["page_length"], 3)
		self.assertEqual(query["filters"]["job_title"], "JOB-1")
		self.assertEqual(query["filters"]["status"], "Open")
		self.assertEqual(query["filters"]["applicant_rating"], [">=", 0.4])
		self.assertEqual(self.frappe.allowed_roles, self.api.PAGE_ROLES)

	def test_interview_queue_is_resolved_server_side_before_pagination(self):
		self.frappe.rows = [{"name": "A", "applicant_rating": 0.8}]
		original_sql = self.frappe.db.sql

		def sql(query, values=None, **kwargs):
			if "LEFT JOIN `tabInterview`" in query:
				return ["A"]
			return original_sql(query, values, **kwargs)

		self.frappe.db.sql = sql
		result = self.api.get_candidates(filters={"interview_queue": "missing_feedback"}, page_length=1)
		self.assertEqual(result["rows"][0]["name"], "A")
		page_query = next(
			kwargs
			for doctype, kwargs in self.frappe.get_list_calls
			if doctype == "Job Applicant" and kwargs.get("start") == 0
		)
		self.assertEqual(page_query["filters"]["name"], ["in", ["A"]])

	def test_get_candidates_includes_visible_talent_pool_state(self):
		self.frappe.rows = [
			{
				"name": "A",
				"applicant_rating": 0.8,
				"custom_candidate_profile": "PROFILE-A",
			}
		]
		self.frappe.profile_rows = [
			{
				"name": "PROFILE-A",
				"talent_pool_status": "Prioritario",
				"dedupe_status": "Manual",
				"do_not_contact": 1,
			}
		]
		result = self.api.get_candidates(filters={}, start=0, page_length=50)
		self.assertEqual(result["rows"][0]["talent_pool_status"], "Prioritario")
		self.assertTrue(result["rows"][0]["talent_pool_do_not_contact"])

	def test_score_filter_and_order_are_applied_server_side(self):
		self.api.get_candidates({"minimum_score": 70, "sort_by": "score"})
		kwargs = self.frappe.get_list_calls[-1][1]
		self.assertEqual(kwargs["filters"]["custom_candidate_score"], [">=", 70])
		self.assertEqual(kwargs["order_by"], "custom_candidate_score desc, creation desc, name desc")

	def test_candidate_page_exposes_latest_interview_queue_and_disagreement(self):
		self.frappe.rows = [{"name": "A", "applicant_rating": 0.8}]
		self.frappe.interview_rows = [
			{
				"name": "INT-1",
				"job_applicant": "A",
				"status": "Under Review",
				"scheduled_on": "2026-08-13",
				"docstatus": 0,
				"modified": "2026-08-11 21:00:00",
			}
		]
		self.frappe.interview_details = [
			{"parent": "INT-1", "interviewer": "one@example.com"},
			{"parent": "INT-1", "interviewer": "two@example.com"},
		]
		self.frappe.interview_feedback = [
			{"interview": "INT-1", "interviewer": "one@example.com", "result": "Cleared"},
			{"interview": "INT-1", "interviewer": "other@example.com", "result": "Rejected"},
		]
		row = self.api.get_candidates(page_length=1)["rows"][0]
		self.assertEqual(row["existing_interview"], "INT-1")
		self.assertEqual(row["missing_feedback"], 1)
		self.assertTrue(row["feedback_disagreement"])
		interview_query = next(
			kwargs for doctype, kwargs in self.frappe.get_list_calls if doctype == "Interview"
		)
		self.assertEqual(interview_query["filters"]["interview_type"], self.api.AYP_INTERVIEW_TYPE)

	def test_batch_scope_is_validated_before_any_document_is_saved(self):
		self.frappe.docs = {
			"A": FakeDoc("A", "Open", "JOB-1"),
			"B": FakeDoc("B", "Open", "JOB-2"),
		}
		with self.assertRaisesRegex(FakeValidationError, "vacante"):
			self.api.apply_batch_action(
				applicant_names=["A", "B"],
				target_status="Shortlisted",
				reason="Cumple los criterios.",
				job_title="JOB-1",
			)
		self.assertEqual(self.frappe.docs["A"].saved, 0)
		self.assertEqual(self.frappe.docs["B"].saved, 0)
		self.assertEqual(self.frappe.events, [])

	def test_successful_batch_saves_each_document_and_structured_audit_event(self):
		self.frappe.docs = {
			"A": FakeDoc("A", "Open", "JOB-1", "PROFILE-A"),
			"B": FakeDoc("B", "Hold", "JOB-1", "PROFILE-B"),
		}
		result = self.api.apply_batch_action(
			applicant_names=["A", "B"],
			target_status="Shortlisted",
			reason="Pasa a revisión humana.",
			job_title="JOB-1",
		)
		self.assertEqual(result, {"batch_id": "batch123456", "updated": 2})
		self.assertEqual(
			[self.frappe.docs[name].status for name in ("A", "B")], ["Shortlisted", "Shortlisted"]
		)
		self.assertEqual([self.frappe.docs[name].saved for name in ("A", "B")], [1, 1])
		self.assertEqual(len(self.frappe.events), 2)
		self.assertEqual(self.frappe.events[0][0]["previous_status"], "Open")
		self.assertEqual(self.frappe.events[1][0]["previous_status"], "Hold")
		self.assertTrue(all(event[0]["batch_id"] == "batch123456" for event in self.frappe.events))
		self.assertTrue(all(event[1] for event in self.frappe.events))
		self.assertTrue(all(self.frappe.docs[name].comments for name in ("A", "B")))
		_lock_query, lock_values = next(
			(query, values)
			for query, values in self.frappe.db.sql_calls
			if "tabJob Applicant" in query and "FOR UPDATE" in query
		)
		self.assertEqual(lock_values, ("A", "B"))

	def test_decisive_batch_rejects_unready_document_before_any_save(self):
		applicant = FakeDoc("A", "Open", "JOB-1", "PROFILE-A")
		applicant.custom_cv_processing_status = "Pendiente"
		self.frappe.docs = {"A": applicant}
		with self.assertRaisesRegex(FakeValidationError, "CV procesado"):
			self.api.apply_batch_action(
				applicant_names=["A"],
				target_status="Shortlisted",
				reason="Intento controlado antes de terminar la revisión documental.",
				job_title="JOB-1",
			)
		self.assertEqual(applicant.saved, 0)
		self.assertEqual(self.frappe.events, [])

	def test_non_decisive_batch_allows_document_pending(self):
		applicant = FakeDoc("A", "Open", "JOB-1", "PROFILE-A")
		applicant.custom_cv_processing_status = "Pendiente"
		self.frappe.docs = {"A": applicant}
		result = self.api.apply_batch_action(
			applicant_names=["A"],
			target_status="Hold",
			reason="Se conserva en espera mientras termina la revisión documental.",
			job_title="JOB-1",
		)
		self.assertEqual(result["updated"], 1)
		self.assertEqual(applicant.status, "Hold")

	def test_filtered_run_requires_explicit_vacancy_and_source_status(self):
		with self.assertRaisesRegex(FakeValidationError, "vacante y un estado"):
			self.api.freeze_filtered_run({}, "Shortlisted", "Revisión humana documentada.")

	def test_filtered_run_freezes_server_cohort_and_returns_its_count(self):
		self.frappe.rows = [
			{"name": "A", "status": "Open"},
			{"name": "B", "status": "Open"},
		]
		result = self.api.freeze_filtered_run(
			{"job_title": "JOB-1", "status": "Open"},
			"Shortlisted",
			"Cohorte revisada y confirmada por una persona responsable.",
		)
		self.assertEqual(result["run"], "AYP-REVIEW-RUN-0001")
		self.assertEqual(result["total"], 2)
		run = self.frappe.docs[result["run"]]
		self.assertEqual([row["applicant"] for row in run.members], ["A", "B"])
		query = next(
			kwargs
			for doctype, kwargs in self.frappe.get_list_calls
			if doctype == "Job Applicant" and kwargs.get("fields") == ["name", "status"]
		)
		self.assertEqual(query["page_length"], 0)
		self.assertEqual(query["start"], 0)

	def test_get_scorecard_returns_server_defined_criteria_without_writing(self):
		self.frappe.docs = {"A": FakeDoc("A", "Open", "JOB-1")}
		result = self.api.get_scorecard("A")
		self.assertIsNone(result["latest"])
		self.assertEqual(sum(row["weight"] for row in result["criteria"]), 100)
		self.assertEqual(self.frappe.scorecards, [])

	def test_save_scorecard_uses_server_weights_and_updates_latest_projection(self):
		applicant = FakeDoc("A", "Open", "JOB-1", "PROFILE-A")
		self.frappe.docs = {"A": applicant}
		criteria = [
			{
				"criterion_key": key,
				"rating": 4,
				"weight": 999,
				"evidence": "Evidencia comprobada por revisión humana.",
			}
			for key in (
				"minimum_requirements",
				"relevant_experience",
				"functional_skills",
				"service_communication",
				"availability_conditions",
			)
		]
		result = self.api.save_scorecard("A", criteria)
		self.assertEqual(result["scorecard"], "AYP-SCORE-0001")
		self.assertEqual(result["total_score"], 80.0)
		self.assertEqual(result["recommendation"], "Recomendado para shortlist")
		payload = self.frappe.scorecards[0][0]
		self.assertEqual([row["weight"] for row in payload["criteria"]], [30, 25, 20, 15, 10])
		self.assertEqual(applicant.custom_candidate_score, 80.0)
		self.assertEqual(applicant.custom_candidate_scorecard, "AYP-SCORE-0001")
		self.assertEqual(applicant.status, "Open")
		self.assertIn("FOR UPDATE", self.frappe.db.sql_calls[0][0])
		self.assertEqual(self.frappe.db.sql_calls[0][1], ("A",))

	def test_scorecard_revalidates_exact_current_document_after_lock(self):
		applicant = FakeDoc("A", "Open", "JOB-1", "PROFILE-A")
		self.frappe.docs = {"A": applicant}
		criteria = [
			{
				"criterion_key": key,
				"rating": 4,
				"evidence": "Evidencia comprobada por revisión humana.",
			}
			for key in (
				"minimum_requirements",
				"relevant_experience",
				"functional_skills",
				"service_communication",
				"availability_conditions",
			)
		]
		calls = []

		def reject_stale_document(doc):
			calls.append((doc.name, len(self.frappe.db.sql_calls)))
			raise FakeValidationError("El archivo actual ya no coincide con el CV procesado.")

		self.api.validate_candidate_ready_for_scoring = reject_stale_document
		with self.assertRaisesRegex(FakeValidationError, "ya no coincide"):
			self.api.save_scorecard("A", criteria)
		self.assertEqual(calls, [("A", 1)])
		self.assertIn("FOR UPDATE", self.frappe.db.sql_calls[0][0])
		self.assertEqual(self.frappe.scorecards, [])
		self.assertFalse(hasattr(applicant, "custom_candidate_score"))
		self.assertEqual(len(self.frappe.db.rollbacks), 1)

	def test_scorecard_rejects_unready_document(self):
		applicant = FakeDoc("A", "Open", "JOB-1", "PROFILE-A")
		applicant.custom_cv_processing_status = "Ilegible"
		self.frappe.docs = {"A": applicant}
		with self.assertRaisesRegex(FakeValidationError, "Procesado"):
			self.api.save_scorecard("A", [])
		self.assertEqual(self.frappe.scorecards, [])

	def test_manual_document_verification_is_audited_and_rejects_security_failures(self):
		applicant = FakeDoc("A", "Open", "JOB-1", "PROFILE-A")
		applicant.custom_cv_processing_status = "Ilegible"
		self.frappe.docs = {"A": applicant}
		result = self.api.verify_candidate_document_manually(
			"A", "Se revisó visualmente el documento completo y sus datos principales."
		)
		self.assertEqual(result["cv_processing_status"], "Verificado manualmente")
		self.assertEqual(applicant.custom_cv_manual_verified_by, "reviewer@example.com")
		self.assertEqual(self.frappe.events[-1][0]["action"], "Verificación manual de CV")
		applicant.custom_cv_processing_status = "Error de seguridad"
		with self.assertRaisesRegex(FakeValidationError, "no permite"):
			self.api.verify_candidate_document_manually(
				"A", "Intento de bypass manual que debe quedar bloqueado por seguridad."
			)
		applicant.custom_cv_processing_status = "Sin CV"
		with self.assertRaisesRegex(FakeValidationError, "no permite"):
			self.api.verify_candidate_document_manually(
				"A", "Intento de aprobar manualmente un formulario sin CV adjunto."
			)

	def test_talent_pool_decision_is_human_reasoned_and_audited(self):
		applicant = FakeDoc("A", "Rejected", "JOB-1", "PROFILE-A")
		profile = FakeProfile("PROFILE-A")
		self.frappe.docs = {"A": applicant, "PROFILE-A": profile}
		result = self.api.update_candidate_profile(
			"A",
			"priority",
			"Perfil relevante para futuras vacantes similares verificadas.",
		)
		self.assertEqual(result["talent_pool_status"], "Prioritario")
		self.assertEqual(profile.saved, 1)
		self.assertEqual(self.frappe.events[-1][0]["action"], "Decisión Talent Pool")

	def test_identity_split_creates_new_profile_and_relinks_only_selected_application(self):
		applicant = FakeDoc("A", "Open", "JOB-1", "PROFILE-A")
		linked = FakeDoc("B", "Hold", "JOB-2", "PROFILE-A")
		profile = FakeProfile("PROFILE-A")
		self.frappe.docs = {"A": applicant, "B": linked, "PROFILE-A": profile}
		result = self.api.resolve_candidate_identity(
			"A", "split", None, "La revisión humana confirmó que son identidades diferentes."
		)
		self.assertEqual(result["target_profile"], "PROFILE-NEW")
		self.assertEqual(applicant.custom_candidate_profile, "PROFILE-NEW")
		self.assertEqual(linked.custom_candidate_profile, "PROFILE-A")
		self.assertEqual(self.frappe.events[-1][0]["action"], "Separar identidad")

	def test_identity_merge_relinks_all_source_applications_and_propagates_no_contact(self):
		applicant = FakeDoc("A", "Open", "JOB-1", "PROFILE-A")
		linked = FakeDoc("B", "Hold", "JOB-2", "PROFILE-A")
		source = FakeProfile("PROFILE-A")
		target = FakeProfile("PROFILE-B")
		source.do_not_contact = 1
		self.frappe.docs = {"A": applicant, "B": linked, "PROFILE-A": source, "PROFILE-B": target}
		preview = self.api._identity_preview_payload(applicant, source, target, "merge", ["A", "B"])
		result = self.api.resolve_candidate_identity(
			"A",
			"merge",
			"PROFILE-B",
			"Dos perfiles corresponden a la misma persona verificada.",
			preview["binding"],
		)
		self.assertEqual(result["moved_applications"], 2)
		self.assertTrue(target.do_not_contact)
		self.assertEqual({applicant.custom_candidate_profile, linked.custom_candidate_profile}, {"PROFILE-B"})
		self.assertEqual(source.merged_into, "PROFILE-B")
		self.assertEqual(source.talent_pool_status, "Fusionado")

	def test_identity_preview_hydrates_complete_profiles_before_binding(self):
		applicant = FakeDoc("A", "Open", "JOB-1", "PROFILE-A")
		source = FakeProfile("PROFILE-A", talent_pool_status="Activo")
		target = FakeProfile("PROFILE-B", talent_pool_status="Prioritario")
		target.application_count = 2
		self.frappe.docs = {"A": applicant, "PROFILE-A": source, "PROFILE-B": target}
		preview = self.api.preview_candidate_identity("A", "merge", "PROFILE-B")
		self.assertEqual(preview["result_talent_pool_status"], "Prioritario")
		self.assertEqual(preview["target_application_count"], 2)
		self.assertEqual(
			preview["binding"],
			self.api._identity_preview_payload(applicant, source, target, "merge", ["A"])["binding"],
		)

	def test_identity_merge_requires_authoritative_preview(self):
		applicant = FakeDoc("A", "Open", "JOB-1", "PROFILE-A")
		source = FakeProfile("PROFILE-A")
		target = FakeProfile("PROFILE-B")
		self.frappe.docs = {"A": applicant, "PROFILE-A": source, "PROFILE-B": target}
		with self.assertRaisesRegex(FakeValidationError, "impacto autoritativo"):
			self.api.resolve_candidate_identity(
				"A", "merge", "PROFILE-B", "Dos perfiles corresponden a la misma persona verificada."
			)

	def test_identity_merge_rejects_preview_drift_after_locks(self):
		applicant = FakeDoc("A", "Open", "JOB-1", "PROFILE-A")
		late_link = FakeDoc("B", "Hold", "JOB-2", "PROFILE-A")
		source = FakeProfile("PROFILE-A")
		target = FakeProfile("PROFILE-B")
		preview = self.api._identity_preview_payload(applicant, source, target, "merge", ["A"])
		self.frappe.docs = {
			"A": applicant,
			"B": late_link,
			"PROFILE-A": source,
			"PROFILE-B": target,
		}
		with self.assertRaisesRegex(FakeValidationError, "impacto de la fusión cambió"):
			self.api.resolve_candidate_identity(
				"A",
				"merge",
				"PROFILE-B",
				"Dos perfiles corresponden a la misma persona verificada.",
				preview["binding"],
			)
		self.assertEqual(applicant.custom_candidate_profile, "PROFILE-A")
		self.assertEqual(late_link.custom_candidate_profile, "PROFILE-A")

	def test_positive_talent_pool_action_cannot_clear_do_not_contact(self):
		applicant = FakeDoc("A", "Rejected", "JOB-1", "PROFILE-A")
		profile = FakeProfile("PROFILE-A")
		profile.do_not_contact = 1
		self.frappe.docs = {"A": applicant, "PROFILE-A": profile}
		with self.assertRaises(FakeValidationError):
			self.api.update_candidate_profile(
				"A",
				"retain",
				"Solicitud revisada por una persona con evidencia suficiente.",
			)
		self.assertEqual(profile.saved, 0)
		self.assertEqual(profile.do_not_contact, 1)


if __name__ == "__main__":
	unittest.main()
