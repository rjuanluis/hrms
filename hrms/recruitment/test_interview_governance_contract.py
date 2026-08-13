from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class TestInterviewGovernanceContract(unittest.TestCase):
	def test_hooks_load_kit_and_server_side_validation(self):
		hooks = (ROOT / "hrms" / "hooks.py").read_text(encoding="utf-8")
		self.assertIn('"Interview": "public/js/ayp_interview.js"', hooks)
		self.assertIn('"validate": "hrms.recruitment.interview_governance.validate_interview"', hooks)
		self.assertIn('"before_submit": "hrms.recruitment.interview_governance.validate_ayp_interview_submission"', hooks)
		self.assertIn("validate_job_applicant_final_transition", hooks)
		self.assertIn('"before_submit": "hrms.recruitment.interview_governance.validate_ayp_interview_feedback"', hooks)
		self.assertIn('"before_cancel": "hrms.recruitment.interview_governance.validate_ayp_interview_cancellation"', hooks)
		self.assertIn(
			'"before_cancel": "hrms.recruitment.interview_governance.validate_ayp_feedback_cancellation"',
			hooks,
		)

	def test_all_decisive_applicant_transitions_revalidate_cv_before_origin_flags(self):
		governance = (ROOT / "hrms" / "recruitment" / "interview_governance.py").read_text(encoding="utf-8")
		start = governance.index("def validate_job_applicant_final_transition")
		end = governance.index("\ndef update_job_applicant_from_downstream", start)
		block = governance[start:end]
		self.assertIn('{"Shortlisted", "Accepted", "Rejected"}', block)
		self.assertIn("revalidate_candidate_document(doc)", block)
		self.assertLess(
			block.index("revalidate_candidate_document(doc)"),
			block.index('frappe.flags.get("ayp_candidate_review_batch")'),
		)
		self.assertLess(
			block.index("revalidate_candidate_document(doc)"),
			block.index('frappe.flags.get("ayp_interview_decision")'),
		)

	def test_interview_kit_escapes_questions_and_decision_is_human(self):
		script = (ROOT / "hrms" / "public" / "js" / "ayp_interview.js").read_text(encoding="utf-8")
		self.assertIn("frappe.utils.escape_html", script)
		self.assertIn("custom_ayp_questions_snapshot", script)
		patch = (ROOT / "hrms" / "patches" / "v16_0" / "create_ayp_interview_kit.py").read_text(encoding="utf-8")
		self.assertIn("Regla de evidencia", patch)
		self.assertNotIn("edad", patch.lower().replace("no inferir edad", ""))

	def test_acceptance_endpoint_requires_submitted_matching_interview_and_audits(self):
		controller = (ROOT / "hrms" / "hr" / "doctype" / "interview" / "interview.py").read_text(encoding="utf-8")
		for required in (
			'@frappe.whitelist(methods=["POST"])\ndef update_job_applicant_status',
			'"interview": self.name',
			"lock_ayp_interview_decision_dependencies",
			"validate_interview_backed_application_decision",
			'"doctype": "AYP Candidate Review Event"',
			'"action": "Interview Decision"',
			"applicant_doc.save()",
		):
			with self.subTest(required=required):
				self.assertIn(required, controller)
		decision_start = controller.index("def update_job_applicant_status")
		decision_end = controller.index("\ndef send_interview_reminder", decision_start)
		decision_block = controller[decision_start:decision_end]
		self.assertNotIn("frappe.db.savepoint", decision_block)
		self.assertNotIn("rollback(save_point=savepoint)", decision_block)
		self.assertIn("except frappe.QueryDeadlockError", decision_block)
		self.assertIn("CONCURRENT_CHANGE_MESSAGE", decision_block)

	def test_decision_and_cancellation_follow_native_compatible_lock_orders(self):
		governance = (ROOT / "hrms" / "recruitment" / "interview_governance.py").read_text(
			encoding="utf-8"
		)
		controller = (ROOT / "hrms" / "hr" / "doctype" / "interview" / "interview.py").read_text(
			encoding="utf-8"
		)
		self.assertIn("def lock_ayp_interview_decision_dependencies", governance)
		decision_start = governance.index("def lock_ayp_interview_decision_dependencies")
		decision_end = governance.index("\ndef lock_ayp_feedback_cancellation_dependencies", decision_start)
		decision = governance[decision_start:decision_end]
		feedback_lock = decision.index("FROM `tabInterview Feedback`")
		interview_lock = decision.index("FROM `tabInterview` WHERE name = %s FOR UPDATE")
		applicant_lock = decision.index("FROM `tabJob Applicant` WHERE name = %s FOR UPDATE")
		self.assertLess(feedback_lock, interview_lock)
		self.assertLess(interview_lock, applicant_lock)
		self.assertNotIn("GET_LOCK", governance)
		self.assertGreaterEqual(governance.count("except frappe.QueryDeadlockError"), 2)
		self.assertGreaterEqual(governance.count("CONCURRENT_CHANGE_MESSAGE"), 3)
		self.assertIn('frappe.get_doc("Interview", interview_name, for_update=True)', decision)
		self.assertIn('frappe.get_doc("Job Applicant", applicant_name, for_update=True)', decision)
		feedback_cancel_start = governance.index("def lock_ayp_feedback_cancellation_dependencies")
		feedback_cancel_end = governance.index("\ndef lock_ayp_interview_cancellation_dependencies", feedback_cancel_start)
		feedback_cancel = governance[feedback_cancel_start:feedback_cancel_end]
		self.assertNotIn("Interview Feedback", feedback_cancel)
		self.assertLess(feedback_cancel.index('frappe.get_doc("Interview"'), feedback_cancel.index('frappe.get_doc("Job Applicant"'))
		interview_cancel_start = governance.index("def lock_ayp_interview_cancellation_dependencies")
		interview_cancel_end = governance.index("\ndef _is_ayp_interview", interview_cancel_start)
		interview_cancel = governance[interview_cancel_start:interview_cancel_end]
		self.assertNotIn("Interview Feedback", interview_cancel)
		self.assertIn("lock_ayp_interview_decision_dependencies", controller)
		self.assertIn("locked_feedback_names", controller)
		self.assertIn("def cancel(self):", controller)
		self.assertIn("return super().cancel()", controller)
		self.assertIn("if is_ayp:", controller)
		feedback_controller = (
			ROOT / "hrms" / "hr" / "doctype" / "interview_feedback" / "interview_feedback.py"
		).read_text(encoding="utf-8")
		self.assertIn("def cancel(self):", feedback_controller)
		self.assertIn("custom_ayp_questions_snapshot", feedback_controller)
		self.assertIn("return super().cancel()", feedback_controller)
		self.assertIn("if is_ayp:", feedback_controller)

	def test_feedback_cancel_scope_is_checked_from_authoritative_locked_interview(self):
		governance = (ROOT / "hrms" / "recruitment" / "interview_governance.py").read_text(
			encoding="utf-8"
		)
		start = governance.index("def validate_ayp_feedback_cancellation")
		end = governance.index("\ndef validate_job_applicant_final_transition", start)
		block = governance[start:end]
		self.assertNotIn('frappe.get_doc("Interview", doc.interview)', block)
		self.assertIn("lock_ayp_feedback_cancellation_dependencies(doc.interview)", block)
		self.assertLess(block.index("lock_ayp_feedback_cancellation_dependencies"), block.index("_is_ayp_interview"))

	def test_candidate_review_uses_server_interview_builder_and_refreshes_queue(self):
		script = (ROOT / "hrms" / "hr" / "page" / "ayp_candidate_review" / "ayp_candidate_review.js").read_text(
			encoding="utf-8"
		)
		self.assertIn("job_applicant.create_interview", script)
		self.assertNotIn('frappe.new_doc("Interview"', script)
		self.assertIn("existing_interview", script)
		self.assertIn("on_page_show", script)
		self.assertIn("load_candidates({ reset: true })", script)
		builder = (ROOT / "hrms" / "hr" / "doctype" / "job_applicant" / "job_applicant.py").read_text(
			encoding="utf-8"
		)
		self.assertIn('"docstatus": ["!=", 2] if interview_type == "AyP - Entrevista estructurada" else 1', builder)
		self.assertIn('@frappe.whitelist(methods=["POST"])', builder)
		self.assertIn('frappe.has_permission("Interview", "create", throw=True)', builder)
		interview_controller = (ROOT / "hrms" / "hr" / "doctype" / "interview" / "interview.py").read_text(
			encoding="utf-8"
		)
		self.assertIn("GET_LOCK", interview_controller)
		self.assertIn('"docstatus": ["!=", 2] if self.interview_type == AYP_INTERVIEW_TYPE else 1', interview_controller)

	def test_feedback_result_is_visible_and_ayp_evidence_is_required(self):
		form_script = (ROOT / "hrms" / "hr" / "doctype" / "interview" / "interview.js").read_text(
			encoding="utf-8"
		)
		controller = (ROOT / "hrms" / "hr" / "doctype" / "interview" / "interview.py").read_text(encoding="utf-8")
		template = (ROOT / "hrms" / "public" / "js" / "templates" / "feedback_history.html").read_text(
			encoding="utf-8"
		)
		patch = (ROOT / "hrms" / "patches" / "v16_0" / "add_ayp_interview_governance.py").read_text(
			encoding="utf-8"
		)
		self.assertIn("interview_feedback.result", controller)
		self.assertIn("feedback_history[i].result", template)
		self.assertIn("custom_ayp_question_evidence", patch)
		self.assertIn("custom_ayp_question_evidence", form_script)
		self.assertIn("custom_ayp_question_evidence", controller)
		self.assertIn("ayp_evidence_", form_script)
		self.assertNotIn('"allow_on_submit": 1', patch)
		self.assertIn('frappe.has_permission("Interview", "read", interview, throw=True)', controller)
		self.assertIn('@frappe.whitelist(methods=["POST"])\ndef create_interview_feedback', controller)

	def test_snapshot_is_restored_from_server_sources_not_client_payload(self):
		governance = (ROOT / "hrms" / "recruitment" / "interview_governance.py").read_text(encoding="utf-8")
		self.assertIn('frappe.db.get_value("Interview", doc.name, "custom_ayp_questions_snapshot")', governance)
		self.assertIn('frappe.db.get_value("Interview Type", doc.interview_type', governance)
		self.assertNotIn("structured_questions = doc.custom_ayp_questions_snapshot or", governance)
		self.assertIn("submitted_feedback = frappe.db.exists(", governance)
		self.assertIn("El kit y el snapshot de una entrevista con feedback enviado son inmutables", governance)

	def test_mutating_candidate_review_endpoints_are_post_only(self):
		api = (ROOT / "hrms" / "hr" / "page" / "ayp_candidate_review" / "ayp_candidate_review.py").read_text(
			encoding="utf-8"
		)
		for endpoint in (
			"apply_batch_action",
			"freeze_filtered_run",
			"process_filtered_run_chunk",
			"save_scorecard",
		):
			with self.subTest(endpoint=endpoint):
				self.assertIn('@frappe.whitelist(methods=["POST"])\ndef {0}'.format(endpoint), api)

	def test_fresh_site_creates_governance_field_before_profile_backfill(self):
		patches = (ROOT / "hrms" / "patches.txt").read_text(encoding="utf-8")
		profile_patch_name = "hrms.patches.v16_0.create_ayp_candidate_profiles"
		review_patch_name = "hrms.patches.v16_0.add_ayp_candidate_review_fields"
		self.assertLess(patches.index(profile_patch_name), patches.index(review_patch_name))
		profile_patch = (ROOT / "hrms" / "patches" / "v16_0" / "create_ayp_candidate_profiles.py").read_text(
			encoding="utf-8"
		)
		self.assertLess(
			profile_patch.index('"fieldname": "custom_ayp_governed"'),
			profile_patch.index("backfill_candidate_profiles()"),
		)

	def test_candidate_profile_privacy_state_requires_audited_endpoint(self):
		profile_controller = (
			ROOT / "hrms" / "hr" / "doctype" / "ayp_candidate_profile" / "ayp_candidate_profile.py"
		).read_text(encoding="utf-8")
		profile_meta = (
			ROOT / "hrms" / "hr" / "doctype" / "ayp_candidate_profile" / "ayp_candidate_profile.json"
		).read_text(encoding="utf-8")
		api = (ROOT / "hrms" / "hr" / "page" / "ayp_candidate_review" / "ayp_candidate_review.py").read_text(
			encoding="utf-8"
		)
		self.assertIn("PRIVACY_GOVERNANCE_FIELDS", profile_controller)
		self.assertIn("_validate_contact_monotonic", profile_controller)
		self.assertIn('@frappe.whitelist(methods=["POST"])\n@candidate_profile_governance_update\ndef update_candidate_profile', api)
		self.assertIn('@frappe.whitelist(methods=["POST"])\n@candidate_profile_governance_update\ndef resolve_candidate_identity', api)
		for fieldname in ("talent_pool_status", "do_not_contact", "disposition_reason"):
			field_block = profile_meta.split('"fieldname": "{0}"'.format(fieldname), 1)[1].split("}", 1)[0]
			self.assertIn('"read_only": 1', field_block)

	def test_review_event_allows_legacy_applicant_without_job_opening(self):
		event = (ROOT / "hrms" / "hr" / "doctype" / "ayp_candidate_review_event" / "ayp_candidate_review_event.json").read_text(
			encoding="utf-8"
		)
		job_opening_block = event.split('"fieldname": "job_opening"', 1)[1].split("}", 1)[0]
		self.assertNotIn('"reqd": 1', job_opening_block)

	def test_downstream_documents_cannot_bypass_ayp_final_decision(self):
		guard_name = "update_job_applicant_from_downstream"
		governance = (ROOT / "hrms" / "recruitment" / "interview_governance.py").read_text(encoding="utf-8")
		job_offer = (ROOT / "hrms" / "hr" / "doctype" / "job_offer" / "job_offer.py").read_text(encoding="utf-8")
		employee = (ROOT / "hrms" / "overrides" / "employee_master.py").read_text(encoding="utf-8")
		self.assertIn("custom_ayp_governed", governance)
		self.assertIn(guard_name, job_offer)
		self.assertIn(guard_name, employee)
		self.assertNotIn('frappe.set_value("Job Applicant"', job_offer)
		self.assertNotIn('frappe.db.set_value("Job Applicant"', employee)

	def test_canonical_candidate_is_governed_and_final_transitions_are_bidirectional(self):
		talent_pool = (ROOT / "hrms" / "recruitment" / "talent_pool.py").read_text(encoding="utf-8")
		governance = (ROOT / "hrms" / "recruitment" / "interview_governance.py").read_text(encoding="utf-8")
		self.assertIn('"custom_ayp_governed"', talent_pool)
		self.assertIn("previous_status", governance)
		self.assertIn("previous_final_interview", governance)
		self.assertIn("FINAL_APPLICATION_STATUSES", governance)
		patch = (ROOT / "hrms" / "patches" / "v16_0" / "add_ayp_interview_governance.py").read_text(
			encoding="utf-8"
		)
		self.assertIn("custom_ayp_final_interview", patch)
		self.assertIn("applicant.status = 'Accepted' AND interview.status = 'Cleared'", patch)
		self.assertIn("applicant.status = 'Rejected' AND interview.status = 'Rejected'", patch)
		controller = (ROOT / "hrms" / "hr" / "doctype" / "interview" / "interview.py").read_text(
			encoding="utf-8"
		)
		self.assertIn("applicant_doc.custom_ayp_final_interview = interview_doc.name", controller)


if __name__ == "__main__":
	unittest.main()
