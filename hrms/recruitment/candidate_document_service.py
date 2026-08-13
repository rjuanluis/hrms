from __future__ import annotations

import hashlib

import frappe
from frappe import _
from frappe.utils import now_datetime

from hrms.recruitment.candidate_document_processing import DocumentProcessingError, extract_candidate_document
from hrms.security.candidate_cv import CandidateCVSecurityError, validate_cv_file

PROCESSOR_VERSION = "ayp-cv-extractor-v1"
QUEUE_NAME = "documents"
READY_FOR_SCORING = frozenset({"Procesado", "Verificado manualmente"})
MANUAL_REVIEWABLE = frozenset({"Revisión manual", "Ilegible"})
TERMINAL_FAILURES = frozenset({"Protegido", "No compatible", "Error de seguridad"})
PROCESSING_STATUSES = (
	"Sin CV",
	"Pendiente",
	"Procesando",
	"Procesado",
	"Revisión manual",
	"Ilegible",
	"Protegido",
	"No compatible",
	"Error de seguridad",
	"Verificado manualmente",
)


def _has_processing_fields() -> bool:
	return frappe.db.has_column("Job Applicant", "custom_cv_processing_status")


def _safe_detail(value: str) -> str:
	text = " ".join(str(value or "").split())
	return text[:500]


def processing_status_for_method(method: str) -> str:
	return "Revisión manual" if "OCR" in str(method or "").upper() else "Procesado"


def _clear_document_projection(doc) -> None:
	doc.custom_cv_processing_method = ""
	doc.custom_cv_processed_sha256 = ""
	doc.custom_cv_extracted_text = ""
	doc.custom_cv_text_sha256 = ""
	doc.custom_cv_page_count = 0
	doc.custom_cv_processed_on = None
	doc.custom_cv_processing_started_on = None
	doc.custom_cv_processing_queued_on = None
	doc.custom_cv_processing_claim = ""
	doc.custom_cv_manual_verified_by = ""
	doc.custom_cv_manual_verified_on = None
	doc.custom_cv_manual_verification_reason = ""
	doc.custom_candidate_score = 0
	doc.custom_candidate_recommendation = ""
	doc.custom_candidate_scorecard = ""
	doc.custom_candidate_scored_on = None


def prepare_candidate_document_state(doc, method=None) -> None:
	"""Bind document-processing state to the exact scanned CV SHA-256."""

	if not getattr(doc, "meta", None) or not doc.meta.has_field("custom_cv_processing_status"):
		return
	sha256 = (doc.get("custom_cv_sha256") or "").strip().lower()
	previous = doc.get_doc_before_save() if not doc.is_new() else None
	previous_sha256 = (previous.get("custom_cv_sha256") or "").strip().lower() if previous else ""
	previous_attachment = (previous.get("resume_attachment") or "") if previous else ""
	attachment_changed = (doc.resume_attachment or "") != previous_attachment or sha256 != previous_sha256

	if not doc.resume_attachment:
		_clear_document_projection(doc)
		doc.custom_cv_processing_status = "Sin CV"
		doc.custom_cv_processing_detail = "La solicitud no incluye un CV adjunto."
		doc.custom_cv_processor_version = PROCESSOR_VERSION
		return
	if not sha256:
		_clear_document_projection(doc)
		doc.custom_cv_processing_status = "Error de seguridad"
		doc.custom_cv_processing_detail = "El CV no tiene una huella de integridad verificada."
		doc.custom_cv_processor_version = PROCESSOR_VERSION
		return
	if attachment_changed or not doc.custom_cv_processing_status:
		_clear_document_projection(doc)
		doc.custom_cv_processing_status = "Pendiente"
		doc.custom_cv_processing_detail = "CV recibido de forma segura; extracción pendiente."
		doc.custom_cv_processing_queued_on = now_datetime()
		doc.custom_cv_processor_version = PROCESSOR_VERSION


def _enqueue_candidate_document_job(applicant_name: str, expected_sha256: str) -> None:
	committed = frappe.db.get_value(
		"Job Applicant",
		applicant_name,
		["custom_cv_processing_status", "custom_cv_sha256", "resume_attachment"],
		as_dict=True,
	)
	if (
		not committed
		or committed.custom_cv_processing_status != "Pendiente"
		or (committed.custom_cv_sha256 or "") != expected_sha256
		or not committed.resume_attachment
	):
		return
	try:
		frappe.enqueue(
			"hrms.recruitment.candidate_document_service.process_candidate_document",
			queue=QUEUE_NAME,
			timeout=600,
			job_id=f"ayp-cv:{applicant_name}:{expected_sha256}",
			deduplicate=True,
			applicant_name=applicant_name,
			expected_sha256=expected_sha256,
		)
	except Exception:
		frappe.log_error(title="Candidate CV enqueue failed", message=frappe.get_traceback())


def enqueue_candidate_document(doc, method=None) -> None:
	if not _has_processing_fields() or not doc.resume_attachment:
		return
	if doc.get("custom_cv_processing_status") != "Pendiente" or not doc.get("custom_cv_sha256"):
		return
	callback_key = (doc.name, doc.custom_cv_sha256)
	registered = getattr(frappe.local, "candidate_document_enqueue_callbacks", None)
	if registered is None:
		registered = set()
		frappe.local.candidate_document_enqueue_callbacks = registered
	if callback_key in registered:
		return
	registered.add(callback_key)
	frappe.db.after_rollback.add(lambda: registered.discard(callback_key))
	def enqueue_committed_candidate_document():
		try:
			_enqueue_candidate_document_job(*callback_key)
		finally:
			registered.discard(callback_key)

	frappe.db.after_commit.add(enqueue_committed_candidate_document)


def _locked_applicant(applicant_name: str):
	frappe.db.sql("SELECT name FROM `tabJob Applicant` WHERE name = %s FOR UPDATE", (applicant_name,))
	return frappe.get_doc("Job Applicant", applicant_name, for_update=True)


def _load_exact_cv(applicant) -> tuple[str, bytes]:
	file_names = frappe.db.sql(
		"""
		SELECT name
		FROM `tabFile`
		WHERE file_url = %s
			AND attached_to_doctype = 'Job Applicant'
			AND attached_to_name = %s
		ORDER BY creation DESC, name DESC
		LIMIT 1
		FOR UPDATE
		""",
		(applicant.resume_attachment, applicant.name),
		pluck=True,
	)
	file_name = file_names[0] if file_names else None
	file_record = frappe.get_doc("File", file_name, for_update=True) if file_name else None
	if (
		not file_record
		or not file_record.is_private
		or file_record.custom_av_scan_status != "Clean"
		or file_record.custom_cv_sha256 != applicant.custom_cv_sha256
	):
		raise CandidateCVSecurityError("El CV ya no coincide con el archivo privado escaneado.")
	content = file_record.get_content()
	if isinstance(content, str):
		content = content.encode()
	validate_cv_file(file_record.file_name, content)
	if hashlib.sha256(content).hexdigest() != applicant.custom_cv_sha256:
		raise CandidateCVSecurityError("La huella del CV cambió después del escaneo.")
	return file_record.file_name, content


def revalidate_candidate_document(applicant) -> None:
	"""Require the exact private, antivirus-clean CV before human/scoring decisions."""

	sha256 = (applicant.get("custom_cv_sha256") or "").strip().lower()
	processed_sha256 = (applicant.get("custom_cv_processed_sha256") or "").strip().lower()
	if not applicant.get("resume_attachment") or not sha256 or processed_sha256 != sha256:
		frappe.throw(
			_("El CV no conserva una huella procesada ligada al archivo seguro actual."),
			frappe.ValidationError,
		)
	try:
		_load_exact_cv(applicant)
	except CandidateCVSecurityError:
		frappe.throw(
			_("El CV no coincide con un archivo privado y limpio validado por antivirus."),
			frappe.ValidationError,
		)


def _persist_result(
	applicant_name: str,
	expected_sha256: str,
	claim: str,
	*,
	status: str,
	method: str = "",
	detail: str = "",
	text: str = "",
	page_count: int = 0,
) -> bool:
	applicant = _locked_applicant(applicant_name)
	if (
		(applicant.custom_cv_sha256 or "") != expected_sha256
		or applicant.custom_cv_processing_status != "Procesando"
		or (applicant.custom_cv_processing_claim or "") != claim
	):
		frappe.db.rollback()
		return False
	text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
	applicant.db_set(
		{
			"custom_cv_processing_status": status,
			"custom_cv_processing_method": method,
			"custom_cv_processing_detail": _safe_detail(detail),
			"custom_cv_processed_sha256": expected_sha256,
			"custom_cv_extracted_text": text,
			"custom_cv_text_sha256": text_sha256,
			"custom_cv_page_count": page_count,
			"custom_cv_processed_on": now_datetime(),
			"custom_cv_processing_started_on": None,
			"custom_cv_processing_claim": "",
			"custom_cv_processor_version": PROCESSOR_VERSION,
		},
		update_modified=False,
	)
	frappe.db.commit()
	return True


def process_candidate_document(applicant_name: str, expected_sha256: str) -> dict:
	"""Extract one exact, scanned CV with a durable claim safe for retries."""

	if not _has_processing_fields():
		raise RuntimeError("Candidate document fields are unavailable; run migrate first.")
	applicant = _locked_applicant(applicant_name)
	if (applicant.custom_cv_sha256 or "") != expected_sha256:
		frappe.db.rollback()
		return {"status": "stale"}
	if (
		applicant.custom_cv_processed_sha256 == expected_sha256
		and applicant.custom_cv_processing_status in READY_FOR_SCORING | TERMINAL_FAILURES | {"Ilegible", "Revisión manual"}
	):
		frappe.db.rollback()
		return {"status": "already-processed"}
	if applicant.custom_cv_processing_status != "Pendiente":
		frappe.db.rollback()
		return {"status": "superseded"}
	claim = frappe.generate_hash(length=32)
	applicant.db_set(
		{
			"custom_cv_processing_status": "Procesando",
			"custom_cv_processing_detail": "Extracción documental en curso.",
			"custom_cv_processing_started_on": now_datetime(),
			"custom_cv_processing_claim": claim,
		},
		update_modified=False,
	)
	frappe.db.commit()

	try:
		applicant = frappe.get_doc("Job Applicant", applicant_name)
		if (applicant.custom_cv_sha256 or "") != expected_sha256:
			return {"status": "stale"}
		filename, content = _load_exact_cv(applicant)
		frappe.db.commit()
		result = extract_candidate_document(filename, content)
		status = processing_status_for_method(result.method)
		persisted = _persist_result(
			applicant_name,
			expected_sha256,
			claim,
			status=status,
			method=result.method,
			detail=(
				"OCR produjo texto de apoyo; una persona debe compararlo con el original antes de puntuar."
				if status == "Revisión manual"
				else "Texto nativo extraído; requiere verificación humana durante la evaluación."
			),
			text=result.text,
			page_count=result.page_count,
		)
		return {"status": status if persisted else "superseded"}
	except CandidateCVSecurityError:
		persisted = _persist_result(
			applicant_name,
			expected_sha256,
			claim,
			status="Error de seguridad",
			detail="La integridad o el estado antivirus del CV no pudo revalidarse.",
		)
		return {"status": "security-error" if persisted else "superseded"}
	except DocumentProcessingError as exc:
		persisted = _persist_result(
			applicant_name,
			expected_sha256,
			claim,
			status=exc.status,
			detail=exc.detail,
		)
		return {"status": exc.status if persisted else "superseded"}
	except Exception:
		frappe.log_error(title="Candidate CV processing failed", message=frappe.get_traceback())
		persisted = _persist_result(
			applicant_name,
			expected_sha256,
			claim,
			status="Revisión manual",
			detail="La extracción automática no terminó; el CV debe revisarse manualmente.",
		)
		return {"status": "manual-review" if persisted else "superseded"}


def recover_stale_candidate_document_jobs() -> int:
	"""Recover committed pending work and invalidate abandoned processing claims."""

	if not _has_processing_fields():
		return 0
	stale_rows = frappe.db.sql(
		"""
		SELECT name, custom_cv_sha256
		FROM `tabJob Applicant`
		WHERE COALESCE(resume_attachment, '') != ''
			AND COALESCE(custom_cv_sha256, '') != ''
			AND (
				(custom_cv_processing_status = 'Procesando'
					AND custom_cv_processing_started_on < DATE_SUB(NOW(), INTERVAL 15 MINUTE))
				OR
				(custom_cv_processing_status = 'Pendiente'
					AND (custom_cv_processing_queued_on IS NULL
						OR custom_cv_processing_queued_on < DATE_SUB(NOW(), INTERVAL 15 MINUTE)))
			)
		ORDER BY name
		LIMIT 100
		FOR UPDATE
		""",
		as_dict=True,
	)
	for row in stale_rows:
		frappe.db.set_value(
			"Job Applicant",
			row.name,
			{
				"custom_cv_processing_status": "Pendiente",
				"custom_cv_processing_detail": "Trabajo documental recuperado; extracción pendiente.",
				"custom_cv_processing_queued_on": now_datetime(),
				"custom_cv_processing_started_on": None,
				"custom_cv_processing_claim": "",
			},
			update_modified=False,
		)
		frappe.db.after_commit.add(
			lambda name=row.name, sha256=row.custom_cv_sha256: _enqueue_candidate_document_job(name, sha256)
		)
	return len(stale_rows)


def validate_candidate_ready_for_scoring(doc) -> None:
	if not _has_processing_fields():
		frappe.throw(_("El control documental no está disponible; ejecuta migrate."), frappe.ValidationError)
	if doc.get("custom_cv_processing_status") not in READY_FOR_SCORING:
		frappe.throw(
			_("El CV debe estar Procesado o Verificado manualmente antes de crear un scorecard."),
			frappe.ValidationError,
		)
	revalidate_candidate_document(doc)
