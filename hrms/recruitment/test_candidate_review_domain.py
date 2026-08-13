from __future__ import annotations

import unittest

if __package__:
	from hrms.recruitment.candidate_review_domain import (
		BatchReviewRequest,
		CandidateReviewValidationError,
		ReviewFilters,
		validate_transition,
	)
else:
	from candidate_review_domain import (
		BatchReviewRequest,
		CandidateReviewValidationError,
		ReviewFilters,
		validate_transition,
	)


class TestReviewFilters(unittest.TestCase):
	def test_normalizes_filters_and_caps_page_size(self):
		filters = ReviewFilters.from_input(
			{
				"search": "  ana@example.com  ",
				"status": "Shortlisted",
				"job_title": "JOB-OPEN-0001",
				"dedupe_status": "Revisión requerida",
				"cv_processing_status": "Procesado",
				"minimum_rating": "3",
				"minimum_score": "70",
				"sort_by": "score",
			},
			start="4",
			page_length="999",
		)
		self.assertEqual(filters.search, "ana@example.com")
		self.assertEqual(filters.minimum_rating, 3)
		self.assertEqual(filters.minimum_score, 70)
		self.assertEqual(filters.sort_by, "score")
		self.assertEqual(filters.cv_processing_status, "Procesado")
		self.assertEqual(filters.start, 4)
		self.assertEqual(filters.page_length, 100)

	def test_rejects_unknown_status(self):
		with self.assertRaisesRegex(CandidateReviewValidationError, "estado"):
			ReviewFilters.from_input({"status": "Contratada"})

	def test_rejects_invalid_score_and_sort(self):
		with self.assertRaisesRegex(CandidateReviewValidationError, "score"):
			ReviewFilters.from_input({"minimum_score": 101})
		with self.assertRaisesRegex(CandidateReviewValidationError, "orden"):
			ReviewFilters.from_input({"sort_by": "name"})
		with self.assertRaisesRegex(CandidateReviewValidationError, "documental"):
			ReviewFilters.from_input({"cv_processing_status": "Inventado"})


class TestBatchReviewRequest(unittest.TestCase):
	def test_deduplicates_names_without_changing_order(self):
		request = BatchReviewRequest.from_input(
			["HR-APP-0002", "HR-APP-0001", "HR-APP-0002"],
			target_status="Shortlisted",
			reason="Cumple los criterios de preselección.",
			job_title="JOB-OPEN-0001",
		)
		self.assertEqual(request.applicant_names, ("HR-APP-0002", "HR-APP-0001"))

	def test_requires_reason_and_exact_job_opening_scope(self):
		for kwargs, message in (
			({"reason": "", "job_title": "JOB-OPEN-0001"}, "motivo"),
			({"reason": "Revisión inicial", "job_title": ""}, "vacante"),
		):
			with self.subTest(kwargs=kwargs):
				with self.assertRaisesRegex(CandidateReviewValidationError, message):
					BatchReviewRequest.from_input(
						["HR-APP-0001"],
						target_status="Hold",
						**kwargs,
					)

	def test_rejects_more_than_one_hundred_applicants(self):
		with self.assertRaisesRegex(CandidateReviewValidationError, "100"):
			BatchReviewRequest.from_input(
				[f"HR-APP-{index:04d}" for index in range(101)],
				target_status="Rejected",
				reason="No cumple los criterios mínimos documentados.",
				job_title="JOB-OPEN-0001",
			)

	def test_does_not_allow_batch_acceptance(self):
		with self.assertRaisesRegex(CandidateReviewValidationError, "Accepted"):
			BatchReviewRequest.from_input(
				["HR-APP-0001"],
				target_status="Accepted",
				reason="Decisión final.",
				job_title="JOB-OPEN-0001",
			)


class TestTransitions(unittest.TestCase):
	def test_shortlist_and_reopen_are_allowed(self):
		validate_transition("Open", "Shortlisted")
		validate_transition("Rejected", "Open")

	def test_accepted_is_terminal_for_candidate_review(self):
		with self.assertRaisesRegex(CandidateReviewValidationError, "Accepted"):
			validate_transition("Accepted", "Hold")

	def test_noop_transition_is_rejected(self):
		with self.assertRaisesRegex(CandidateReviewValidationError, "mismo"):
			validate_transition("Hold", "Hold")


if __name__ == "__main__":
	unittest.main()
