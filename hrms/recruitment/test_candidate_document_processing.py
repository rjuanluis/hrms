from __future__ import annotations

import io
import inspect
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hrms.recruitment import candidate_document_service
from hrms.recruitment.candidate_document_processing import (
	MAX_DOCUMENT_PAGES,
	DocumentProcessingError,
	extract_candidate_document,
	normalize_extracted_text,
)
from hrms.recruitment.candidate_document_service import prepare_candidate_document_state
from hrms.recruitment.candidate_document_service import processing_status_for_method
from hrms.security.candidate_cv import CandidateCVSecurityError


class FakeMeta:
	def has_field(self, fieldname):
		return True


class FakeApplicant:
	def __init__(self, *, attachment="", sha256="", status="Sin CV", previous=None):
		self.meta = FakeMeta()
		self.resume_attachment = attachment
		self.custom_cv_sha256 = sha256
		self.custom_cv_processing_status = status
		self.custom_cv_processing_method = "old"
		self.custom_cv_processing_detail = "old"
		self.custom_cv_processed_sha256 = "old-sha"
		self.custom_cv_extracted_text = "old text"
		self.custom_cv_text_sha256 = "old-text-sha"
		self.custom_cv_page_count = 2
		self.custom_cv_processed_on = "old-date"
		self.custom_cv_processing_started_on = "old-start"
		self.custom_cv_processing_queued_on = "old-queued"
		self.custom_cv_processing_claim = "old-claim"
		self.custom_cv_manual_verified_by = "reviewer@example.com"
		self.custom_cv_manual_verified_on = "old-manual-date"
		self.custom_cv_manual_verification_reason = "old reason"
		self.custom_candidate_score = 80
		self.custom_candidate_recommendation = "Recomendado"
		self.custom_candidate_scorecard = "SCORE-1"
		self.custom_candidate_scored_on = "old-score-date"
		self.custom_cv_processor_version = "old-version"
		self._previous = previous

	def get(self, key):
		return getattr(self, key, None)

	def is_new(self):
		return self._previous is None

	def get_doc_before_save(self):
		return self._previous


def previous_document(attachment="", sha256=""):
	return SimpleNamespace(get=lambda key: {"resume_attachment": attachment, "custom_cv_sha256": sha256}.get(key))


def make_docx(document_text: str = "", media: dict[str, bytes] | None = None) -> bytes:
	buffer = io.BytesIO()
	with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
		archive.writestr("[Content_Types].xml", "<Types />")
		archive.writestr(
			"word/document.xml",
			("<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
			 f"<w:body><w:p><w:r><w:t>{document_text}</w:t></w:r></w:p></w:body></w:document>"),
		)
		for name, content in (media or {}).items():
			archive.writestr(f"word/media/{name}", content)
	return buffer.getvalue()


class TestCandidateDocumentProcessing(unittest.TestCase):
	def test_exact_cv_lookup_is_bound_to_the_same_applicant(self):
		source = inspect.getsource(candidate_document_service._load_exact_cv)
		self.assertIn("attached_to_doctype = 'Job Applicant'", source)
		self.assertIn("attached_to_name = %s", source)
		self.assertIn("FOR UPDATE", source)
		self.assertIn('frappe.get_doc("File", file_name, for_update=True)', source)

	def test_manual_review_never_accepts_a_missing_cv(self):
		self.assertNotIn("Sin CV", candidate_document_service.MANUAL_REVIEWABLE)
		self.assertEqual(candidate_document_service.MANUAL_REVIEWABLE, {"Revisión manual", "Ilegible"})

	def test_manual_or_scoring_revalidation_requires_exact_processed_hash(self):
		doc = FakeApplicant(attachment="/private/files/cv.pdf", sha256="current", status="Revisión manual")
		doc.custom_cv_processed_sha256 = "stale"
		with patch.object(candidate_document_service.frappe, "throw", side_effect=RuntimeError("blocked")):
			with self.assertRaisesRegex(RuntimeError, "blocked"):
				candidate_document_service.revalidate_candidate_document(doc)

	def test_manual_or_scoring_revalidation_rechecks_private_clean_file(self):
		doc = FakeApplicant(attachment="/private/files/cv.pdf", sha256="current", status="Revisión manual")
		doc.custom_cv_processed_sha256 = "current"
		with patch.object(candidate_document_service, "_load_exact_cv", return_value=("cv.pdf", b"safe")) as load:
			candidate_document_service.revalidate_candidate_document(doc)
		load.assert_called_once_with(doc)
		with (
			patch.object(
				candidate_document_service,
				"_load_exact_cv",
				side_effect=CandidateCVSecurityError("not clean"),
			),
			patch.object(candidate_document_service.frappe, "throw", side_effect=RuntimeError("blocked")),
		):
			with self.assertRaisesRegex(RuntimeError, "blocked"):
				candidate_document_service.revalidate_candidate_document(doc)

	def test_enqueue_deduplicates_inside_post_commit_callback(self):
		hook_source = inspect.getsource(candidate_document_service.enqueue_candidate_document)
		callback_source = inspect.getsource(candidate_document_service._enqueue_candidate_document_job)
		self.assertIn("frappe.db.after_commit.add", hook_source)
		self.assertIn("candidate_document_enqueue_callbacks", hook_source)
		self.assertIn("registered.discard(callback_key)", hook_source)
		self.assertIn("frappe.db.after_rollback.add", hook_source)
		self.assertIn("frappe.db.get_value", callback_source)
		self.assertIn("deduplicate=True", callback_source)
		self.assertNotIn("enqueue_after_commit=True", callback_source)

	def test_late_worker_persistence_requires_processing_claim(self):
		source = inspect.getsource(candidate_document_service._persist_result)
		self.assertIn('custom_cv_processing_status != "Procesando"', source)
		self.assertIn("custom_cv_processing_claim", source)
		self.assertIn("!= claim", source)
		process_source = inspect.getsource(candidate_document_service.process_candidate_document)
		self.assertIn('custom_cv_processing_status != "Pendiente"', process_source)

	def test_recovery_covers_pending_and_invalidates_claim(self):
		source = inspect.getsource(candidate_document_service.recover_stale_candidate_document_jobs)
		self.assertIn("custom_cv_processing_status = 'Pendiente'", source)
		self.assertIn("custom_cv_processing_queued_on", source)
		self.assertIn('"custom_cv_processing_claim": ""', source)
		self.assertIn("_enqueue_candidate_document_job", source)

	def test_locked_applicant_reloads_authoritatively(self):
		root = Path(__file__).resolve().parents[2]
		service = (root / "hrms" / "recruitment" / "candidate_document_service.py").read_text()
		self.assertIn('frappe.get_doc("Job Applicant", applicant_name, for_update=True)', service)

	def test_every_ocr_method_requires_manual_verification_before_scoring(self):
		for method in ("PDF OCR", "DOCX image OCR", "Image OCR"):
			with self.subTest(method=method):
				self.assertEqual(processing_status_for_method(method), "Revisión manual")
		for method in ("PDF text", "DOCX text", "DOC text"):
			with self.subTest(method=method):
				self.assertEqual(processing_status_for_method(method), "Procesado")

	def test_manual_verification_without_attachment_is_invalidated_fail_closed(self):
		doc = FakeApplicant(
			status="Verificado manualmente",
			previous=previous_document(),
		)
		prepare_candidate_document_state(doc)
		self.assertEqual(doc.custom_cv_processing_status, "Sin CV")
		self.assertEqual(doc.custom_candidate_scorecard, "")
		self.assertEqual(doc.custom_cv_processed_sha256, "")

	def test_new_attachment_invalidates_manual_verification_and_score_projection(self):
		doc = FakeApplicant(
			attachment="/private/files/new.pdf",
			sha256="new-sha",
			status="Verificado manualmente",
			previous=previous_document(),
		)
		with patch.object(candidate_document_service, "now_datetime", return_value="2026-08-12 19:30:00"):
			prepare_candidate_document_state(doc)
		self.assertEqual(doc.custom_cv_processing_status, "Pendiente")
		self.assertEqual(doc.custom_candidate_scorecard, "")
		self.assertEqual(doc.custom_candidate_score, 0)
		self.assertEqual(doc.custom_cv_extracted_text, "")

	def test_removed_attachment_invalidates_processed_document_and_score(self):
		doc = FakeApplicant(
			status="Procesado",
			previous=previous_document("/private/files/old.pdf", "old-sha"),
		)
		prepare_candidate_document_state(doc)
		self.assertEqual(doc.custom_cv_processing_status, "Sin CV")
		self.assertEqual(doc.custom_candidate_scorecard, "")
		self.assertEqual(doc.custom_cv_processed_sha256, "")

	def test_normalization_removes_control_characters_and_bounds_output(self):
		text = normalize_extracted_text("  Ana\x00\r\n\r\n  Pérez\tVentas  ")
		self.assertEqual(text, "Ana\n\nPérez Ventas")

	def test_docx_native_text_is_extracted_without_external_process(self):
		result = extract_candidate_document(
			"cv.docx",
			make_docx("Experiencia en ventas, servicio al cliente y gestión de inventario."),
		)
		self.assertEqual(result.method, "DOCX text")
		self.assertIn("servicio al cliente", result.text)
		self.assertEqual(result.page_count, 0)

	def test_image_runs_spanish_and_english_ocr(self):
		png = b"\x89PNG\r\n\x1a\n" + b"synthetic"
		completed = subprocess.CompletedProcess([], 0, stdout="Experiencia profesional en ventas y servicio al cliente.", stderr="")
		with patch("hrms.recruitment.candidate_document_processing.subprocess.run", return_value=completed) as run:
			result = extract_candidate_document("cv.png", png)
		self.assertEqual(result.method, "Image OCR")
		self.assertIn("Experiencia profesional", result.text)
		self.assertIn("spa+eng", run.call_args.args[0])

	def test_scanned_pdf_falls_back_to_ocr(self):
		responses = [
			subprocess.CompletedProcess([], 0, stdout="Pages: 2\nEncrypted: no\n", stderr=""),
			subprocess.CompletedProcess([], 0, stdout="", stderr=""),
			subprocess.CompletedProcess([], 0, stdout="", stderr=""),
			subprocess.CompletedProcess([], 0, stdout="Primera página con experiencia laboral suficiente.", stderr=""),
			subprocess.CompletedProcess([], 0, stdout="Segunda página con estudios y referencias comprobables.", stderr=""),
		]
		def fake_run(args, **kwargs):
			response = responses.pop(0)
			if "pdftoppm" in args[0]:
				prefix = Path(args[-1])
				prefix.with_name(prefix.name + "-1.png").write_bytes(b"png1")
				prefix.with_name(prefix.name + "-2.png").write_bytes(b"png2")
			return response
		with patch("hrms.recruitment.candidate_document_processing.subprocess.run", side_effect=fake_run):
			result = extract_candidate_document("scan.pdf", b"%PDF-1.7\n%%EOF")
		self.assertEqual(result.method, "PDF OCR")
		self.assertEqual(result.page_count, 2)
		self.assertIn("Segunda página", result.text)

	def test_pdf_page_limit_fails_to_manual_review(self):
		info = subprocess.CompletedProcess([], 0, stdout=f"Pages: {MAX_DOCUMENT_PAGES + 1}\nEncrypted: no\n", stderr="")
		with patch("hrms.recruitment.candidate_document_processing.subprocess.run", return_value=info):
			with self.assertRaises(DocumentProcessingError) as raised:
				extract_candidate_document("long.pdf", b"%PDF-1.7\n%%EOF")
		self.assertEqual(raised.exception.status, "Revisión manual")

	def test_encrypted_pdf_is_classified_as_protected(self):
		info = subprocess.CompletedProcess([], 0, stdout="Pages: 1\nEncrypted: yes\n", stderr="")
		with patch("hrms.recruitment.candidate_document_processing.subprocess.run", return_value=info):
			with self.assertRaises(DocumentProcessingError) as raised:
				extract_candidate_document("locked.pdf", b"%PDF-1.7\n%%EOF")
		self.assertEqual(raised.exception.status, "Protegido")

	def test_old_doc_uses_antiword_and_can_fall_back_to_manual_review(self):
		failed = subprocess.CompletedProcess([], 1, stdout="", stderr="encrypted document")
		with patch("hrms.recruitment.candidate_document_processing.subprocess.run", return_value=failed):
			with self.assertRaises(DocumentProcessingError) as raised:
				extract_candidate_document("legacy.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
		self.assertEqual(raised.exception.status, "Protegido")

	def test_command_timeout_is_fail_closed(self):
		with patch(
			"hrms.recruitment.candidate_document_processing.subprocess.run",
			side_effect=subprocess.TimeoutExpired(["tesseract"], 30),
		):
			with self.assertRaises(DocumentProcessingError) as raised:
				extract_candidate_document("cv.jpg", b"\xff\xd8\xff\xe0synthetic")
		self.assertEqual(raised.exception.status, "Revisión manual")


if __name__ == "__main__":
	unittest.main()
