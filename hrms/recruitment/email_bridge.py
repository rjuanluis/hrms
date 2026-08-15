from __future__ import annotations

import base64
import binascii
import hashlib
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import NoReturn

import frappe
from frappe import _
from frappe.utils import validate_email_address
from frappe.utils.file_manager import get_content_hash, save_file

from hrms.recruitment.matching import EMAIL_RECRUITMENT_SOURCE, normalize_email
from hrms.security.candidate_cv import (
	MAX_CV_BYTES,
	CandidateCVSecurityError,
	scan_stored_candidate_cv,
	validate_cv_file,
)

DEFAULT_JOB_OPENING = "HR-OPN-2026-0001"
JOB_OPENING_CONFIG_KEY = "ayp_email_bridge_job_opening"
MESSAGE_ID_FIELD = "custom_ayp_email_message_id"
RECEIVED_ON_FIELD = "custom_ayp_email_received_on"
SUBJECT_FIELD = "custom_ayp_email_subject"
CURRENT_VACANCY_CONSENT_FIELD = "custom_ayp_email_current_vacancy_consent"
CONSENT_NOTICE_FIELD = "custom_ayp_email_consent_notice_version"
EMAIL_CONSENT_NOTICE_VERSION = "AYP-RH-EMAIL-CURRENT-VACANCY-2026-08-15-v1"
MAX_DATA_LENGTH = 140
MAX_RAW_MESSAGE_ID_LENGTH = 4096
MAX_BASE64_LENGTH = ((MAX_CV_BYTES + 2) // 3) * 4
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
JOB_OPENING_SUBJECT_PATTERN = re.compile(
	rf"(?<![A-Z0-9-]){re.escape(DEFAULT_JOB_OPENING)}(?![A-Z0-9-])",
	re.IGNORECASE,
)


class EmailBridgeError(frappe.ValidationError):
	pass


def _fail(message: str) -> NoReturn:
	raise EmailBridgeError(_(message))


def _clean_data(value, *, label: str, required: bool = True, max_length: int = MAX_DATA_LENGTH) -> str:
	if value is None and not required:
		return ""
	if not isinstance(value, str):
		_fail(f"{label} no es válido.")
	cleaned = " ".join(value.split())
	if required and not cleaned:
		_fail(f"{label} es obligatorio.")
	if CONTROL_CHARACTERS.search(value) or len(cleaned) > max_length:
		_fail(f"{label} no es válido.")
	return cleaned


def _message_key(payload: dict) -> str:
	value = _clean_data(
		payload.get("message_id"),
		label="El identificador del mensaje",
		max_length=MAX_RAW_MESSAGE_ID_LENGTH,
	)
	digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
	return f"message:{digest}"


def _sender(payload: dict) -> tuple[str, str]:
	email = _clean_data(payload.get("sender_email"), label="El correo del remitente")
	if any(character in email for character in (",", ";", "\r", "\n")):
		_fail("El correo del remitente no es válido.")
	try:
		validate_email_address(email, throw=True)
	except Exception as exc:
		raise EmailBridgeError(_("El correo del remitente no es válido.")) from exc
	name = _clean_data(payload.get("sender_name"), label="El nombre del remitente")
	return normalize_email(email), name


def _received_on(payload: dict) -> datetime:
	value = payload.get("received_on")
	if not isinstance(value, str) or not value.strip() or len(value) > 64:
		_fail("La fecha de recepción no es válida.")
	try:
		parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
	except ValueError as exc:
		raise EmailBridgeError(_("La fecha de recepción no es válida.")) from exc
	if parsed.tzinfo is None or parsed.utcoffset() is None:
		_fail("La fecha de recepción debe incluir zona horaria.")
	return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _attachment(payload: dict) -> tuple[str, bytes, str]:
	attachments = payload.get("attachments")
	if not isinstance(attachments, list) or len(attachments) != 1:
		_fail("El mensaje debe incluir exactamente un CV adjunto.")
	attachment = attachments[0]
	if not isinstance(attachment, dict):
		_fail("El adjunto no es válido.")
	filename = _clean_data(attachment.get("name"), label="El nombre del archivo")
	if "/" in filename or "\\" in filename or filename in {".", ".."}:
		_fail("El nombre del archivo no es válido.")
	encoded = attachment.get("content_base64")
	if not isinstance(encoded, str) or not encoded or len(encoded) > MAX_BASE64_LENGTH:
		_fail("El contenido base64 del CV no es válido o excede 5 MB.")
	try:
		encoded_bytes = encoded.encode("ascii")
		content = base64.b64decode(encoded_bytes, validate=True)
	except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
		raise EmailBridgeError(_("El contenido base64 del CV no es válido.")) from exc
	validate_cv_file(filename, content)
	return filename, content, hashlib.sha256(content).hexdigest()


def _job_opening() -> str:
	configured = frappe.conf.get(JOB_OPENING_CONFIG_KEY)
	job_opening = str(configured or DEFAULT_JOB_OPENING).strip()
	if job_opening != DEFAULT_JOB_OPENING:
		_fail("La vacante configurada no es la vacante autorizada para el canal de correo.")
	if not job_opening or frappe.db.get_value("Job Opening", job_opening, "status") != "Open":
		_fail("La vacante configurada no está abierta.")
	return job_opening


def _require_current_vacancy_consent(payload: dict) -> None:
	subject = _clean_data(payload.get("subject"), label="El asunto", required=False)
	if not JOB_OPENING_SUBJECT_PATTERN.search(subject):
		_fail("El asunto no identifica la vacante autorizada.")
	if payload.get("consent_current_vacancy") is not True:
		_fail("Falta el consentimiento explícito para procesar la solicitud de esta vacante.")
	version = _clean_data(
		payload.get("consent_notice_version"),
		label="La versión del aviso de consentimiento",
	)
	if version != EMAIL_CONSENT_NOTICE_VERSION:
		_fail("La versión del aviso de consentimiento no está autorizada.")


def _require_configuration() -> None:
	missing_fields = [
		fieldname
		for fieldname in (
			MESSAGE_ID_FIELD,
			RECEIVED_ON_FIELD,
			SUBJECT_FIELD,
			CURRENT_VACANCY_CONSENT_FIELD,
			CONSENT_NOTICE_FIELD,
		)
		if not frappe.db.has_column("Job Applicant", fieldname)
	]
	if missing_fields:
		_fail("Los campos del puente de correo no están instalados; ejecuta la configuración estándar.")
	if not frappe.db.exists("Job Applicant Source", EMAIL_RECRUITMENT_SOURCE):
		_fail("La fuente de solicitudes por correo no está instalada.")


def _existing_applicant(message_key: str):
	return frappe.db.get_value(
		"Job Applicant",
		{MESSAGE_ID_FIELD: message_key},
		[
			"name",
			"applicant_name",
			"email_id",
			"job_title",
			"source",
			"custom_data_processing_consent",
			"custom_privacy_notice_version",
			"custom_cv_sha256",
			MESSAGE_ID_FIELD,
			RECEIVED_ON_FIELD,
			SUBJECT_FIELD,
			CURRENT_VACANCY_CONSENT_FIELD,
			CONSENT_NOTICE_FIELD,
		],
		as_dict=True,
	)


def _as_utc_naive(value) -> datetime | None:
	if isinstance(value, datetime):
		parsed = value
	elif isinstance(value, str) and value:
		try:
			parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
		except ValueError:
			return None
	else:
		return None
	if parsed.tzinfo is not None and parsed.utcoffset() is not None:
		parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
	return parsed


def _assert_duplicate_matches(
	existing,
	*,
	message_key: str,
	sender_email: str,
	sender_name: str,
	received_on: datetime,
	subject: str,
	job_opening: str,
	attachment_sha256: str,
) -> None:
	matches = (
		existing.get(MESSAGE_ID_FIELD) == message_key,
		normalize_email(existing.email_id) == sender_email,
		(existing.applicant_name or "").strip() == sender_name,
		existing.job_title == job_opening,
		existing.source == EMAIL_RECRUITMENT_SOURCE,
		int(existing.custom_data_processing_consent or 0) == 0,
		int(existing.get(CURRENT_VACANCY_CONSENT_FIELD) or 0) == 1,
		(existing.get(CONSENT_NOTICE_FIELD) or "") == EMAIL_CONSENT_NOTICE_VERSION,
		(existing.custom_privacy_notice_version or "") == EMAIL_CONSENT_NOTICE_VERSION,
		(existing.custom_cv_sha256 or "").strip().lower() == attachment_sha256,
		_as_utc_naive(existing.get(RECEIVED_ON_FIELD)) == received_on,
		(existing.get(SUBJECT_FIELD) or "") == subject,
	)
	if not all(matches):
		_fail("El identificador del mensaje ya existe con datos diferentes.")


@contextmanager
def _suppress_document_notifications():
	"""Suppress notifications and fail closed if a hook attempts async work."""

	previous_in_import = frappe.flags.in_import
	previous_mute_emails = frappe.flags.mute_emails
	frappe.flags.in_import = True
	frappe.flags.mute_emails = True
	replaced = {}

	def blocked_external_action(*args, **kwargs):
		raise EmailBridgeError(_("El intake por correo no permite envíos ni trabajos asíncronos."))

	for attribute in ("sendmail", "enqueue", "enqueue_doc"):
		if hasattr(frappe, attribute):
			replaced[attribute] = getattr(frappe, attribute)
			setattr(frappe, attribute, blocked_external_action)
	try:
		yield
	finally:
		for attribute, original in replaced.items():
			setattr(frappe, attribute, original)
		frappe.flags.in_import = previous_in_import
		frappe.flags.mute_emails = previous_mute_emails


def _cleanup_uncommitted_file(file_doc) -> None:
	try:
		# save_file creates File with framework-level permission bypass. Match that
		# narrow behavior only for best-effort physical cleanup before caller rollback.
		frappe.delete_doc("File", file_doc.name, ignore_permissions=True, force=True)
	except Exception:
		# The caller owns the transaction and will roll back the File row. Cleanup is
		# best effort because a duplicate blob may be shared with another File record.
		pass


def ingest_email_payload(payload: dict) -> dict:
	"""Create one Job Applicant from a minimized, validated bridge payload.

	This is intentionally not whitelisted. The authenticated bench caller owns the
	transaction; this function never commits, sends, replies, or enqueues work.
	"""

	if not isinstance(payload, dict):
		_fail("El payload del correo no es válido.")
	if frappe.session.user == "Guest":
		raise frappe.PermissionError(_("Se requiere una sesión autenticada."))

	_require_configuration()
	message_key = _message_key(payload)
	_require_current_vacancy_consent(payload)
	sender_email, sender_name = _sender(payload)
	received_on = _received_on(payload)
	subject = _clean_data(payload.get("subject"), label="El asunto", required=False)
	filename, content, attachment_sha256 = _attachment(payload)
	job_opening = _job_opening()

	existing = _existing_applicant(message_key)
	if existing:
		_assert_duplicate_matches(
			existing,
			message_key=message_key,
			sender_email=sender_email,
			sender_name=sender_name,
			received_on=received_on,
			subject=subject,
			job_opening=job_opening,
			attachment_sha256=attachment_sha256,
		)
		return {"status": "already_processed", "job_applicant": existing.name}

	file_doc = None
	blob_preexisted = bool(
		frappe.db.exists(
			"File",
			{"content_hash": get_content_hash(content), "is_private": 1},
		)
	)
	try:
		file_doc = save_file(filename, content, None, None, is_private=1)
		stored_sha256 = scan_stored_candidate_cv(file_doc)
		if stored_sha256 != attachment_sha256:
			raise CandidateCVSecurityError(_("No se pudo verificar la integridad del CV almacenado."))

		applicant = frappe.get_doc(
			{
				"doctype": "Job Applicant",
				"applicant_name": sender_name,
				"email_id": sender_email,
				"status": "Open",
				"job_title": job_opening,
				"source": EMAIL_RECRUITMENT_SOURCE,
				"resume_attachment": file_doc.file_url,
				# This existing field includes future-opportunity consent. Email
				# applicants never receive that status from the current-vacancy phrase.
				"custom_data_processing_consent": 0,
				"custom_privacy_notice_version": EMAIL_CONSENT_NOTICE_VERSION,
				"custom_candidate_profile": None,
				MESSAGE_ID_FIELD: message_key,
				RECEIVED_ON_FIELD: received_on,
				SUBJECT_FIELD: subject,
				CURRENT_VACANCY_CONSENT_FIELD: 1,
				CONSENT_NOTICE_FIELD: EMAIL_CONSENT_NOTICE_VERSION,
			}
		)
		applicant.flags.ignore_notify = True
		with _suppress_document_notifications():
			applicant.insert()
		return {"status": "created", "job_applicant": applicant.name}
	except Exception:
		if file_doc is not None and not blob_preexisted:
			_cleanup_uncommitted_file(file_doc)
		raise
