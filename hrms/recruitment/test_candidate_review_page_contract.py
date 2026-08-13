from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "hrms" / "hr" / "page" / "ayp_candidate_review"


class TestCandidateReviewPageContract(unittest.TestCase):
	def test_page_exposes_high_volume_review_contract(self):
		script = (PAGE / "ayp_candidate_review.js").read_text(encoding="utf-8")
		for required in (
			'frappe.pages["ayp-candidate-review"]',
			"hrms.hr.page.ayp_candidate_review.ayp_candidate_review.get_candidates",
			"hrms.hr.page.ayp_candidate_review.ayp_candidate_review.apply_batch_action",
			"hrms.hr.page.ayp_candidate_review.ayp_candidate_review.freeze_filtered_run",
			"hrms.hr.page.ayp_candidate_review.ayp_candidate_review.process_filtered_run_chunk",
			"hrms.hr.page.ayp_candidate_review.ayp_candidate_review.get_active_filtered_run",
			"hrms.hr.page.ayp_candidate_review.ayp_candidate_review.get_scorecard",
			"hrms.hr.page.ayp_candidate_review.ayp_candidate_review.save_scorecard",
			'fieldname: "job_title"',
			'fieldname: "status"',
			'fieldname: "source"',
			'fieldname: "dedupe_status"',
			'fieldname: "cv_processing_status"',
			'fieldname: "minimum_rating"',
			'fieldname: "minimum_score"',
			'fieldname: "sort_by"',
			'fieldname: "search"',
			'fieldname: "interview_queue"',
			"total_count",
			"interview_scheduled_on",
			"assigned_interviewers",
			"data-score-applicant",
			"Comparar seleccionados",
			"show_compare_dialog",
			"Promise.all",
			"Criterios y evidencia",
			"scored_by",
			"scored_on",
			"update_candidate_profile",
			"Resolver dedupe",
			"Talent Pool",
			"talent_pool_status",
			"No contactar",
			"resolve_candidate_identity",
			"identity_operation",
			'value: "split"',
			'value: "relink"',
			'value: "merge"',
			"custom_candidate_score",
			"custom_candidate_recommendation",
			"Evidencia comprobable",
			"Verificar CV manualmente",
			"verify_candidate_document_manually",
			"Programar entrevista",
			"AyP - Entrevista estructurada",
			"frappe.utils.escape_html",
			'aria-live="polite"',
			'aria-label="Seleccionar todos los candidatos visibles"',
			"frappe.confirm",
			"max_batch_size = 100",
			"Cada lote admite un máximo de 100 candidatos",
			"Procesar todos los resultados filtrados",
			"load_generation",
			"generation !== this.load_generation",
			"remaining",
			"Lista para decisión final",
			"Decisión final",
			"data-resume-filtered-run",
			"load_candidates({ reset: true })",
		):
			with self.subTest(required=required):
				self.assertIn(required, script)
		self.assertNotIn('value: "Accepted"', script)
		self.assertNotIn("apply_next_filtered_batch", script)

	def test_operator_closure_contract_is_visible(self):
		script = (PAGE / "ayp_candidate_review.js").read_text(encoding="utf-8")
		server = (PAGE / "ayp_candidate_review.py").read_text(encoding="utf-8")
		styles = (PAGE / "ayp_candidate_review.css").read_text(encoding="utf-8")
		for required in (
			"cancel_filtered_run",
			"get_filtered_run_members",
			"preview_candidate_identity",
			"data-cancel-filtered-run",
			"data-review-skipped",
			"Vacante",
			"Estado origen",
			"Estado destino",
			"Motivo",
		):
			with self.subTest(required=required):
				self.assertIn(required, script + server)
		self.assertIn("review-mobile-actions", script)
		self.assertIn("@media (max-width: 767px)", styles)
		self.assertIn("position: sticky", styles)

	def test_page_roles_are_restricted_to_hr(self):
		metadata = json.loads((PAGE / "ayp_candidate_review.json").read_text(encoding="utf-8"))
		self.assertEqual(metadata["page_name"], "ayp-candidate-review")
		self.assertEqual(
			{row["role"] for row in metadata["roles"]},
			{"HR User", "HR Manager", "System Manager"},
		)

	def test_server_count_uses_frappe_v16_function_dict_contract(self):
		server = (PAGE / "ayp_candidate_review.py").read_text(encoding="utf-8")
		self.assertIn('{"COUNT": "name", "as": "total"}', server)
		self.assertNotIn("count(name) as total", server)

	def test_recruitment_sidebar_links_to_candidate_review(self):
		sidebar = json.loads(
			(ROOT / "hrms" / "workspace_sidebar" / "recruitment.json").read_text(encoding="utf-8")
		)
		matches = [item for item in sidebar["items"] if item.get("link_to") == "ayp-candidate-review"]
		self.assertEqual(len(matches), 1)
		self.assertEqual(matches[0]["link_type"], "Page")


if __name__ == "__main__":
	unittest.main()
