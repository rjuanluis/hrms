from __future__ import annotations

import unittest

if __package__:
	from hrms.recruitment.candidate_scoring_domain import (
		DEFAULT_CRITERIA,
		CandidateScoringValidationError,
		Scorecard,
	)
else:
	from candidate_scoring_domain import DEFAULT_CRITERIA, CandidateScoringValidationError, Scorecard


def complete_rows(rating=4):
	return [
		{"criterion_key": criterion.key, "rating": rating, "evidence": f"Evidencia para {criterion.label}"}
		for criterion in DEFAULT_CRITERIA
	]


class TestCandidateScoring(unittest.TestCase):
	def test_weights_total_one_hundred(self):
		self.assertEqual(sum(criterion.weight for criterion in DEFAULT_CRITERIA), 100)

	def test_calculates_explainable_weighted_score(self):
		rows = complete_rows(rating=4)
		scorecard = Scorecard.from_input(rows)
		self.assertEqual(scorecard.total_score, 80.0)
		self.assertEqual(scorecard.recommendation, "Recomendado para shortlist")
		self.assertEqual(sum(row.weighted_score for row in scorecard.rows), 80.0)
		self.assertIn("Requisitos mínimos: 4/5 × 30% = 24.0", scorecard.explanation)

	def test_uses_server_weights_instead_of_client_weights(self):
		rows = complete_rows(rating=5)
		rows[0]["weight"] = 1000
		scorecard = Scorecard.from_input(rows)
		self.assertEqual(scorecard.total_score, 100.0)
		self.assertEqual(scorecard.rows[0].weight, 30)

	def test_requires_every_criterion_once(self):
		rows = complete_rows()
		with self.assertRaisesRegex(CandidateScoringValidationError, "todos los criterios"):
			Scorecard.from_input(rows[:-1])
		rows.append(dict(rows[0]))
		with self.assertRaisesRegex(CandidateScoringValidationError, "duplicado"):
			Scorecard.from_input(rows)

	def test_requires_evidence_and_rating_between_zero_and_five(self):
		rows = complete_rows()
		rows[0]["evidence"] = ""
		with self.assertRaisesRegex(CandidateScoringValidationError, "evidencia"):
			Scorecard.from_input(rows)
		rows = complete_rows()
		rows[0]["rating"] = 6
		with self.assertRaisesRegex(CandidateScoringValidationError, "0 y 5"):
			Scorecard.from_input(rows)

	def test_rejects_trivial_evidence_that_cannot_support_a_recommendation(self):
		rows = complete_rows(rating=5)
		rows[0]["evidence"] = "Sí"
		with self.assertRaisesRegex(CandidateScoringValidationError, "al menos 20"):
			Scorecard.from_input(rows)

	def test_recommendation_thresholds_do_not_make_final_decisions(self):
		self.assertEqual(Scorecard.from_input(complete_rows(3)).recommendation, "Revisión comparativa")
		self.assertEqual(Scorecard.from_input(complete_rows(2)).recommendation, "No priorizar")
		self.assertTrue(
			all(
				"Accepted" not in value
				for value in (
					row.recommendation
					for row in [
						Scorecard.from_input(complete_rows(5)),
						Scorecard.from_input(complete_rows(3)),
						Scorecard.from_input(complete_rows(1)),
					]
				)
			)
		)


if __name__ == "__main__":
	unittest.main()
