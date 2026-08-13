from __future__ import annotations

import unittest

if __package__:
	from hrms.recruitment.interview_decision_domain import (
		InterviewDecisionValidationError,
		application_status_for_interview,
		validate_decision_rationale,
		validate_interview_backed_application_decision,
	)
else:
	from interview_decision_domain import (
		InterviewDecisionValidationError,
		application_status_for_interview,
		validate_decision_rationale,
		validate_interview_backed_application_decision,
	)


class TestInterviewDecision(unittest.TestCase):
	def test_requires_documented_rationale_for_final_interview_result(self):
		with self.assertRaisesRegex(InterviewDecisionValidationError, "justificación"):
			validate_decision_rationale("Cleared", "Muy breve")
		self.assertEqual(
			validate_decision_rationale("Cleared", "Cumplió los criterios y aportó evidencia verificable."),
			"Cumplió los criterios y aportó evidencia verificable.",
		)

	def test_non_final_interview_status_does_not_require_rationale(self):
		self.assertEqual(validate_decision_rationale("Under Review", ""), "")

	def test_maps_interview_result_to_application_status(self):
		self.assertEqual(application_status_for_interview("Cleared"), "Accepted")
		self.assertEqual(application_status_for_interview("Rejected"), "Rejected")
		self.assertIsNone(application_status_for_interview("Under Review"))

	def test_acceptance_requires_matching_submitted_cleared_interview_and_rationale(self):
		valid = {
			"docstatus": 1,
			"status": "Cleared",
			"job_applicant": "HR-APP-0001",
			"custom_ayp_decision_rationale": "La evidencia de entrevista cumple el umbral definido.",
			"custom_ayp_questions_snapshot": "1. Evidencia estructurada",
		}
		validate_interview_backed_application_decision("HR-APP-0001", "Accepted", valid)
		for patch, message in (
			({"docstatus": 0}, "enviada"),
			({"status": "Rejected"}, "Cleared"),
			({"job_applicant": "HR-APP-OTHER"}, "corresponde"),
			({"custom_ayp_decision_rationale": ""}, "justificación"),
		):
			interview = {**valid, **patch}
			with self.subTest(patch=patch):
				with self.assertRaisesRegex(InterviewDecisionValidationError, message):
					validate_interview_backed_application_decision("HR-APP-0001", "Accepted", interview)

	def test_standard_submitted_interview_keeps_legacy_finalization_without_ayp_rationale(self):
		standard = {
			"docstatus": 1,
			"status": "Cleared",
			"job_applicant": "HR-APP-LEGACY",
			"custom_ayp_questions_snapshot": "",
			"custom_ayp_decision_rationale": "",
		}
		validate_interview_backed_application_decision("HR-APP-LEGACY", "Accepted", standard)

	def test_ayp_governed_applicant_cannot_use_standard_interview(self):
		standard = {
			"docstatus": 1,
			"status": "Cleared",
			"job_applicant": "HR-APP-AYP",
			"custom_ayp_questions_snapshot": "",
			"custom_ayp_decision_rationale": "",
		}
		with self.assertRaisesRegex(InterviewDecisionValidationError, "estructurada AyP"):
			validate_interview_backed_application_decision(
				"HR-APP-AYP", "Accepted", standard, require_ayp=True
			)


if __name__ == "__main__":
	unittest.main()
