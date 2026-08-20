from __future__ import annotations

import hashlib

import frappe
from frappe import _
from frappe.utils import now_datetime

from hrms.recruitment.candidate_document_processing import DocumentProcessingError, extract_candidate_document
from hrms.recruitment.matching import EMAIL_RECRUITMENT_SOURCE, has_email_recruitment_provenance
from hrms.security.candidate_cv import (
	CandidateCVScanUnavailableError,
	CandidateCVSecurityError,
	read_stored_candidate_cv_bytes,
	validate_cv_file,
)

PROCESSOR_VERSION = "ayp-cv-extractor-v1"
QUEUE_NAME = "documents"
EMAIL_PROVENANCE_FIELDS = (
	"custom_ayp_email_provenance",
	"custom_ayp_email_file_name",
	"custom_ayp_email_message_id",
	"custom_ayp_email_consent_evidence_sha256",
)
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


def _value(row, fieldname: str):
	getter = getattr(row, "get", None)
	return getter(fieldname) if callable(getter) else getattr(row, fieldname, None)


def _has_email_provenance(row) -> bool:
	return has_email_recruitment_provenance(
		source=_value(row, "source"),
		email_provenance=bool(_value(row, "custom_ayp_email_provenance")),
		email_file_name=_value(row, "custom_ayp_email_file_name"),
		graph_message_key=_value(row, "custom_ayp_email_message_id"),
		consent_evidence_sha256=_value(row, "custom_ayp_email_consent_evidence_sha256"),
	)


def _installed_email_provenance_fields() -> tuple[str, ...]:
	return tuple(
		fieldname for fieldname in EMAIL_PROVENANCE_FIELDS if frappe.db.has_column("Job Applicant", fieldname)
	)


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
		if _has_email_provenance(doc):
			doc.custom_cv_processing_status = "Revisión manual"
			doc.custom_cv_processing_detail = (
				"CV de correo validado sin extracción automática; requiere revisión humana."
			)
			doc.custom_cv_processed_sha256 = sha256
			doc.custom_cv_processed_on = now_datetime()
			doc.custom_cv_processor_version = PROCESSOR_VERSION
			return
		doc.custom_cv_processing_status = "Pendiente"
		doc.custom_cv_processing_detail = "CV recibido de forma segura; extracción pendiente."
		doc.custom_cv_processing_queued_on = now_datetime()
		doc.custom_cv_processor_version = PROCESSOR_VERSION


def _enqueue_candidate_document_job(applicant_name: str, expected_sha256: str) -> None:
	fields = [
		"source",
		"custom_cv_processing_status",
		"custom_cv_sha256",
		"resume_attachment",
		*_installed_email_provenance_fields(),
	]
	committed = frappe.db.get_value(
		"Job Applicant",
		applicant_name,
		fields,
		as_dict=True,
	)
	if (
		not committed
		or _has_email_provenance(committed)
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
	if _has_email_provenance(doc):
		return
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
	exact_file_name = _value(applicant, "custom_ayp_email_file_name")
	if exact_file_name:
		file_names = frappe.db.sql(
			"SELECT name FROM `tabFile` WHERE name = %s FOR UPDATE",
			(exact_file_name,),
			pluck=True,
		)
	else:
		file_names = frappe.db.sql(
			"""
			SELECT name
			FROM `tabFile`
			WHERE file_url = %s
				AND attached_to_doctype = 'Job Applicant'
				AND attached_to_name = %s
				AND attached_to_field = 'resume_attachment'
			ORDER BY creation DESC, name DESC
			LIMIT 2
			FOR UPDATE
			""",
			(applicant.resume_attachment, applicant.name),
			pluck=True,
		)
		if len(file_names) != 1:
			raise CandidateCVSecurityError("No existe un único archivo CV autorizado para la solicitud.")
	file_name = file_names[0] if file_names else None
	file_record = frappe.get_doc("File", file_name, for_update=True) if file_name else None
	if (
		not file_record
		or file_record.file_url != applicant.resume_attachment
		or not file_record.is_private
		or not file_record.file_url.startswith("/private/files/")
		or file_record.custom_av_scan_status != "Clean"
		or file_record.custom_av_scan_engine != "ClamAV"
		or not file_record.custom_av_scanned_on
		or file_record.attached_to_doctype != "Job Applicant"
		or file_record.attached_to_name != applicant.name
		or file_record.attached_to_field != "resume_attachment"
		or file_record.custom_cv_sha256 != applicant.custom_cv_sha256
	):
		raise CandidateCVSecurityError("El CV ya no coincide con el archivo privado escaneado.")
	content = read_stored_candidate_cv_bytes(file_record)
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
	except CandidateCVScanUnavailableError:
		raise
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
	# Persist the terminal CAS result before this background job exits so a
	# retry cannot observe the prior in-progress claim.
	frappe.db.commit()  # nosemgrep
	return True


def _release_for_retry(applicant_name: str, expected_sha256: str, claim: str) -> bool:
	"""Release an exact processing claim without manufacturing terminal evidence."""

	applicant = _locked_applicant(applicant_name)
	if (
		(applicant.custom_cv_sha256 or "") != expected_sha256
		or applicant.custom_cv_processing_status != "Procesando"
		or (applicant.custom_cv_processing_claim or "") != claim
	):
		frappe.db.rollback()
		return False
	applicant.db_set(
		{
			"custom_cv_processing_status": "Pendiente",
			"custom_cv_processing_detail": (
				"El validador de seguridad no estuvo disponible; el trabajo será reintentado."
			),
			"custom_cv_processing_queued_on": None,
			"custom_cv_processing_started_on": None,
			"custom_cv_processing_claim": "",
			"custom_cv_processor_version": PROCESSOR_VERSION,
		},
		update_modified=False,
	)
	# The hourly reconciler treats a null queue timestamp as immediately
	# recoverable and enqueues it after that transaction commits.
	frappe.db.commit()  # nosemgrep
	return True


def process_candidate_document(applicant_name: str, expected_sha256: str) -> dict:
	"""Extract one exact, scanned CV with a durable claim safe for retries."""

	if not _has_processing_fields():
		raise RuntimeError("Candidate document fields are unavailable; run migrate first.")
	applicant = _locked_applicant(applicant_name)
	if _has_email_provenance(applicant):
		frappe.db.rollback()
		return {"status": "email-quarantined"}
	if (applicant.custom_cv_sha256 or "") != expected_sha256:
		frappe.db.rollback()
		return {"status": "stale"}
	if (
		applicant.custom_cv_processed_sha256 == expected_sha256
		and applicant.custom_cv_processing_status
		in READY_FOR_SCORING | TERMINAL_FAILURES | {"Ilegible", "Revisión manual"}
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
	# The durable claim must be visible before expensive extraction begins;
	# otherwise a retry could run the same document concurrently.
	frappe.db.commit()  # nosemgrep

	try:
		applicant = frappe.get_doc("Job Applicant", applicant_name)
		if (applicant.custom_cv_sha256 or "") != expected_sha256:
			return {"status": "stale"}
		filename, content = _load_exact_cv(applicant)
		# Release the attachment read transaction before invoking the bounded
		# parser while retaining the already-persisted exact claim.
		frappe.db.commit()  # nosemgrep
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
	except CandidateCVScanUnavailableError:
		released = _release_for_retry(applicant_name, expected_sha256, claim)
		return {"status": "retryable-unavailable" if released else "superseded"}
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
	installed_provenance_fields = set(_installed_email_provenance_fields())
	provenance_select = ", ".join(
		f"`{fieldname}`" if fieldname in installed_provenance_fields else f"NULL AS `{fieldname}`"
		for fieldname in EMAIL_PROVENANCE_FIELDS
	)
	stale_rows = frappe.db.sql(  # nosemgrep
		f"""
		SELECT name, source, custom_cv_sha256, {provenance_select}
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
	recovered = 0
	for row in stale_rows:
		if _has_email_provenance(row):
			continue
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
		recovered += 1
	return recovered


def validate_candidate_ready_for_scoring(doc) -> None:
	if not _has_processing_fields():
		frappe.throw(_("El control documental no está disponible; ejecuta migrate."), frappe.ValidationError)
	if doc.get("custom_cv_processing_status") not in READY_FOR_SCORING:
		frappe.throw(
			_("El CV debe estar Procesado o Verificado manualmente antes de crear un scorecard."),
			frappe.ValidationError,
		)
	revalidate_candidate_document(doc)
