from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import NoReturn

import frappe
from frappe import _
from frappe.utils import validate_email_address
from frappe.utils.file_manager import get_content_hash

from hrms.recruitment.ats_vacancy_reference import (
	AUTHORIZED_JOB_OPENING,
	subject_has_only_authorized_vacancy_references,
)
from hrms.recruitment.matching import EMAIL_RECRUITMENT_SOURCE, normalize_email
from hrms.security.candidate_cv import (
	MAX_CV_BYTES,
	CandidateCVScanUnavailableError,
	CandidateCVSecurityError,
	read_stored_candidate_cv_bytes,
	scan_stored_candidate_cv,
	validate_cv_file,
)

DEFAULT_JOB_OPENING = AUTHORIZED_JOB_OPENING
JOB_OPENING_LOCK_SQL = """
	SELECT `name`, `status`
	FROM `tabJob Opening`
	ORDER BY `name`
	FOR UPDATE
"""
TRANSACTION_ISOLATION_SQL = "SELECT @@transaction_isolation AS transaction_isolation"
RECRUITMENT_MAILBOX = "empleos@aroypedal.com"
JOB_OPENING_CONFIG_KEY = "ayp_email_bridge_job_opening"
EMAIL_PROVENANCE_FIELD = "custom_ayp_email_provenance"
CV_FILE_FIELD = "custom_ayp_email_file_name"
MESSAGE_ID_FIELD = "custom_ayp_email_message_id"
RECEIVED_ON_FIELD = "custom_ayp_email_received_on"
SUBJECT_FIELD = "custom_ayp_email_subject"
CURRENT_VACANCY_CONSENT_FIELD = "custom_ayp_email_current_vacancy_consent"
CONSENT_NOTICE_FIELD = "custom_ayp_email_consent_notice_version"
CONSENT_EVIDENCE_FIELD = "custom_ayp_email_consent_evidence_sha256"
EMAIL_CONSENT_NOTICE_VERSION = "AYP-RH-EMAIL-DIRECT-SUBMISSION-2026-08-19-v1"
CONSENT_EVIDENCE_FORMAT = "AYP-EMAIL-DIRECT-SUBMISSION-EVIDENCE-V1"
CONSENT_BASIS = "direct_email_submission_to_recruitment_mailbox"
MAX_DATA_LENGTH = 140
MAX_CANDIDATE_FILENAME_BYTES = 240
MAX_SUBJECT_LENGTH = 4096
MAX_RAW_MESSAGE_ID_LENGTH = 4096
MAX_BASE64_LENGTH = ((MAX_CV_BYTES + 2) // 3) * 4
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class EmailBridgeError(frappe.ValidationError):
	pass


class EmailBridgeAdmissionError(EmailBridgeError):
	def __init__(self, code: str, message: str):
		super().__init__(_(message))
		self.code = code


def _fail(message: str) -> NoReturn:
	raise EmailBridgeError(_(message))


def _block_admission(code: str, message: str) -> NoReturn:
	raise EmailBridgeAdmissionError(code, message)


def _filename_exceeds_storage_bytes(value: str) -> bool:
	try:
		return len(value.encode("utf-8")) > MAX_CANDIDATE_FILENAME_BYTES
	except UnicodeEncodeError:
		return True


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
		payload.get("graph_message_id"),
		label="El identificador inmutable de Graph",
		max_length=MAX_RAW_MESSAGE_ID_LENGTH,
	)
	digest = hashlib.sha256(f"{RECRUITMENT_MAILBOX}\n{value}".encode()).hexdigest()
	return f"message:{digest}"


def _consent_evidence_sha256(payload: dict) -> str:
	graph_id = _clean_data(
		payload.get("graph_message_id"),
		label="El identificador inmutable de Graph",
		max_length=MAX_RAW_MESSAGE_ID_LENGTH,
	)
	received_on = payload.get("received_on")
	if not isinstance(received_on, str) or not received_on.strip() or len(received_on) > 64:
		_fail("La fecha de recepción no es válida.")
	evidence = {
		"basis": CONSENT_BASIS,
		"format": CONSENT_EVIDENCE_FORMAT,
		"graph_message_id": graph_id,
		"mailbox": RECRUITMENT_MAILBOX,
		"notice_version": EMAIL_CONSENT_NOTICE_VERSION,
		"received_on": received_on.strip(),
	}
	canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
	expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
	provided = _clean_data(
		payload.get("consent_evidence_sha256"),
		label="La evidencia del consentimiento",
		max_length=64,
	)
	if not re.fullmatch(r"[0-9a-f]{64}", provided) or not hmac.compare_digest(provided, expected):
		_fail("La evidencia del consentimiento no coincide con el mensaje inmutable.")
	return expected


def _has_identity_control(value: str) -> bool:
	return any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value)


def _sender(payload: dict) -> tuple[str, str]:
	raw_email = payload.get("sender_email")
	if not isinstance(raw_email, str):
		_fail("El correo del remitente no es válido.")
	email = " ".join(raw_email.split())
	if (
		not email
		or len(email) > MAX_DATA_LENGTH
		or _has_identity_control(raw_email)
		or any(character in email for character in (",", ";"))
	):
		_block_admission(
			"blocked_sender_identity",
			"El correo del remitente no cumple la política de admisión.",
		)
	try:
		validate_email_address(email, throw=True)
	except Exception as exc:
		raise EmailBridgeAdmissionError(
			"blocked_sender_identity",
			"El correo del remitente no cumple la política de admisión.",
		) from exc
	raw_name = payload.get("sender_name")
	if not isinstance(raw_name, str):
		_fail("El nombre del remitente no es válido.")
	name = " ".join(raw_name.split())
	if not name:
		_fail("El nombre del remitente es obligatorio.")
	if _has_identity_control(name):
		_block_admission(
			"blocked_sender_identity",
			"El nombre del remitente no cumple la política de admisión.",
		)
	name = name[:MAX_DATA_LENGTH]
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


def _require_compatible_subject_vacancy(subject: str) -> None:
	if not subject_has_only_authorized_vacancy_references(subject):
		_block_admission(
			"blocked_explicit_vacancy_mismatch",
			"El asunto contiene un código de vacante distinto de la vacante autorizada.",
		)


def _attachment(payload: dict) -> tuple[str, bytes, str]:
	attachments = payload.get("attachments")
	if not isinstance(attachments, list) or len(attachments) != 1:
		_fail("El mensaje debe incluir exactamente un CV adjunto.")
	attachment = attachments[0]
	if not isinstance(attachment, dict):
		_fail("El adjunto no es válido.")
	raw_filename = attachment.get("name")
	if isinstance(raw_filename, str) and (
		len(raw_filename) > MAX_DATA_LENGTH
		or _filename_exceeds_storage_bytes(raw_filename)
		or raw_filename in {".", ".."}
		or "/" in raw_filename
		or "\\" in raw_filename
		or CONTROL_CHARACTERS.search(raw_filename)
	):
		_block_admission(
			"blocked_candidate_cv_security",
			"El nombre del CV no supera la validación determinística de seguridad.",
		)
	filename = _clean_data(raw_filename, label="El nombre del archivo")
	encoded = attachment.get("content_base64")
	if not isinstance(encoded, str) or not encoded or len(encoded) > MAX_BASE64_LENGTH:
		_fail("El contenido base64 del CV no es válido o excede 5 MB.")
	try:
		encoded_bytes = encoded.encode("ascii")
		content = base64.b64decode(encoded_bytes, validate=True)
	except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
		raise EmailBridgeError(_("El contenido base64 del CV no es válido.")) from exc
	try:
		validate_cv_file(filename, content)
	except CandidateCVScanUnavailableError:
		raise
	except CandidateCVSecurityError:
		_block_admission(
			"blocked_candidate_cv_security",
			"El CV no supera la validación determinística de formato y seguridad.",
		)
	return filename, content, hashlib.sha256(content).hexdigest()


def _save_detached_private_file(filename: str, content: bytes):
	"""Create a private File without Frappe's lossy pre-write/read cycle.

	``frappe.utils.file_manager.save_file`` writes the bytes before creating the
	File document. ``File.before_insert`` then reads that path as text when a PDF
	contains a decodable binary comment and rewrites different UTF-8 bytes.
	Passing the original bytes through File's virtual ``content`` field lets the
	standard document lifecycle write them exactly once and register rollback
	cleanup normally.
	"""

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": filename,
			"is_private": 1,
			"content": content,
		}
	)
	file_doc.flags.ignore_permissions = True
	file_doc.insert()
	return file_doc


def _require_repeatable_read() -> None:
	rows = frappe.db.sql(TRANSACTION_ISOLATION_SQL, (), as_dict=True)
	if len(rows) != 1:
		_fail("No se pudo verificar el aislamiento de la transacción del canal de correo.")
	value = str(rows[0].get("transaction_isolation") or "").strip().upper().replace("_", "-")
	if value != "REPEATABLE-READ":
		_fail("La transacción del canal de correo no usa el aislamiento autorizado.")


def _job_opening() -> str:
	configured = frappe.conf.get(JOB_OPENING_CONFIG_KEY)
	job_opening = str(configured or DEFAULT_JOB_OPENING).strip()
	if job_opening != DEFAULT_JOB_OPENING:
		_block_admission(
			"blocked_authorized_vacancy_configuration",
			"La vacante configurada no es la vacante autorizada para el canal de correo.",
		)
	_require_repeatable_read()
	# Lock the complete authoritative set, not only currently-open rows. Under
	# InnoDB's transaction isolation this prevents an opening from changing
	# status or being inserted between authorization and applicant insertion.
	job_opening_rows = frappe.db.sql(JOB_OPENING_LOCK_SQL, (), as_dict=True)
	open_job_openings = sorted(
		{
			str(row.get("name") or "").strip()
			for row in job_opening_rows
			if row.get("status") == "Open" and str(row.get("name") or "").strip()
		}
	)
	if open_job_openings != [job_opening]:
		_block_admission(
			"blocked_single_open_vacancy_required",
			"El canal de correo requiere exactamente una vacante abierta y autorizada.",
		)
	return job_opening


def _require_direct_submission_consent(payload: dict) -> None:
	if payload.get("consent_current_vacancy") is not True:
		_fail("Falta la constancia de envío directo para procesar la solicitud de esta vacante.")
	basis = _clean_data(
		payload.get("consent_basis"),
		label="La base del consentimiento por correo",
	)
	if basis != CONSENT_BASIS:
		_fail("La base del consentimiento por correo no está autorizada.")
	version = _clean_data(
		payload.get("consent_notice_version"),
		label="La versión de la política de consentimiento por envío",
	)
	if version != EMAIL_CONSENT_NOTICE_VERSION:
		_fail("La versión de la política de consentimiento por envío no está autorizada.")


def _require_configuration() -> None:
	missing_fields = [
		fieldname
		for fieldname in (
			EMAIL_PROVENANCE_FIELD,
			CV_FILE_FIELD,
			MESSAGE_ID_FIELD,
			RECEIVED_ON_FIELD,
			SUBJECT_FIELD,
			CURRENT_VACANCY_CONSENT_FIELD,
			CONSENT_NOTICE_FIELD,
			CONSENT_EVIDENCE_FIELD,
		)
		if not frappe.db.has_column("Job Applicant", fieldname)
	]
	if missing_fields:
		_fail("Los campos del puente de correo no están instalados; ejecuta la configuración estándar.")
	if not frappe.db.exists("Job Applicant Source", EMAIL_RECRUITMENT_SOURCE):
		_fail("La fuente de solicitudes por correo no está instalada.")


def _existing_applicant(message_key: str, *, for_update: bool = False):
	fields = [
		"name",
		"applicant_name",
		"email_id",
		"job_title",
		"source",
		"resume_attachment",
		"custom_data_processing_consent",
		"custom_privacy_notice_version",
		"custom_cv_sha256",
		EMAIL_PROVENANCE_FIELD,
		CV_FILE_FIELD,
		MESSAGE_ID_FIELD,
		RECEIVED_ON_FIELD,
		SUBJECT_FIELD,
		CURRENT_VACANCY_CONSENT_FIELD,
		CONSENT_NOTICE_FIELD,
		CONSENT_EVIDENCE_FIELD,
	]
	if for_update:
		columns = ", ".join(f"`{fieldname}`" for fieldname in fields)
		rows = frappe.db.sql(  # nosemgrep
			f"SELECT {columns} FROM `tabJob Applicant` WHERE `{MESSAGE_ID_FIELD}` = %s FOR UPDATE",
			(message_key,),
			as_dict=True,
		)
		if len(rows) > 1:
			_fail("La clave inmutable del correo no es única.")
		return rows[0] if rows else None
	return frappe.db.get_value(
		"Job Applicant",
		{MESSAGE_ID_FIELD: message_key},
		fields,
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
	consent_evidence_sha256: str,
):
	matches = (
		existing.get(EMAIL_PROVENANCE_FIELD) in (True, 1, "1"),
		bool(existing.get(CV_FILE_FIELD)),
		existing.get(MESSAGE_ID_FIELD) == message_key,
		normalize_email(existing.email_id) == sender_email,
		(existing.applicant_name or "").strip() == sender_name,
		existing.job_title == job_opening,
		existing.source == EMAIL_RECRUITMENT_SOURCE,
		int(existing.custom_data_processing_consent or 0) == 0,
		int(existing.get(CURRENT_VACANCY_CONSENT_FIELD) or 0) == 1,
		(existing.get(CONSENT_NOTICE_FIELD) or "") == EMAIL_CONSENT_NOTICE_VERSION,
		(existing.get(CONSENT_EVIDENCE_FIELD) or "") == consent_evidence_sha256,
		(existing.custom_privacy_notice_version or "") == EMAIL_CONSENT_NOTICE_VERSION,
		(existing.custom_cv_sha256 or "").strip().lower() == attachment_sha256,
		_as_utc_naive(existing.get(RECEIVED_ON_FIELD)) == received_on,
		(existing.get(SUBJECT_FIELD) or "") == subject,
	)
	if not all(matches):
		_fail("El identificador del mensaje ya existe con datos diferentes.")

	file_name = existing.get(CV_FILE_FIELD)
	file_fields = (
		"name",
		"file_name",
		"file_url",
		"file_size",
		"content_hash",
		"is_private",
		"attached_to_doctype",
		"attached_to_name",
		"attached_to_field",
		"custom_av_scan_status",
		"custom_av_scan_engine",
		"custom_av_scanned_on",
		"custom_cv_sha256",
	)
	columns = ", ".join(f"`{fieldname}`" for fieldname in file_fields)
	rows = frappe.db.sql(  # nosemgrep
		f"SELECT {columns} FROM `tabFile` WHERE `name` = %s FOR UPDATE",
		(file_name,),
		as_dict=True,
	)
	if len(rows) != 1:
		_fail("El CV procesado ya no tiene un archivo único y verificable.")
	file_record = rows[0]
	file_doc = frappe.get_doc("File", file_name)
	content = read_stored_candidate_cv_bytes(file_doc)
	actual_sha256 = hashlib.sha256(content).hexdigest()
	file_matches = (
		file_record.get("file_url") == existing.get("resume_attachment"),
		bool(file_record.get("file_url")) and file_record.get("file_url").startswith("/private/files/"),
		file_record.get("is_private") in (True, 1, "1"),
		file_record.get("attached_to_doctype") == "Job Applicant",
		file_record.get("attached_to_name") == existing.name,
		file_record.get("attached_to_field") == "resume_attachment",
		file_record.get("custom_av_scan_status") == "Clean",
		file_record.get("custom_av_scan_engine") == "ClamAV",
		bool(file_record.get("custom_av_scanned_on")),
		bool(file_record.get("content_hash")),
		file_record.get("file_size") == len(content),
		file_record.get("custom_cv_sha256") == attachment_sha256,
		actual_sha256 == attachment_sha256,
	)
	if not all(file_matches):
		_fail("El CV procesado ya no coincide con su evidencia privada e inmutable.")
	return file_record


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


def _remove_verified_duplicate_file_row(file_doc, winner_file) -> None:
	"""Remove only the losing DB alias; never invoke File.on_trash on shared bytes."""

	if file_doc.name == winner_file.get("name"):
		return
	if file_doc.file_url != winner_file.get("file_url") or file_doc.content_hash != winner_file.get(
		"content_hash"
	):
		_fail("El archivo perdedor de la carrera no comparte el CV verificado del ganador.")
	frappe.db.delete("File", {"name": file_doc.name})


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
	_require_direct_submission_consent(payload)
	consent_evidence_sha256 = _consent_evidence_sha256(payload)
	sender_email, sender_name = _sender(payload)
	received_on = _received_on(payload)
	raw_subject = payload.get("subject")
	if isinstance(raw_subject, str) and (
		len(raw_subject) > MAX_SUBJECT_LENGTH or CONTROL_CHARACTERS.search(raw_subject)
	):
		_block_admission(
			"blocked_candidate_subject",
			"El asunto no cumple la política de admisión del canal de correo.",
		)
	full_subject = _clean_data(
		raw_subject,
		label="El asunto",
		required=False,
		max_length=MAX_SUBJECT_LENGTH,
	)
	_require_compatible_subject_vacancy(full_subject)
	subject = full_subject[:MAX_DATA_LENGTH]
	filename, content, attachment_sha256 = _attachment(payload)
	job_opening = _job_opening()

	# Lock and reload the authoritative applicant before locking its exact File.
	# Otherwise a concurrent save can change the evidence tuple between the
	# initial lookup and terminal duplicate readback.
	existing = _existing_applicant(message_key, for_update=True)
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
			consent_evidence_sha256=consent_evidence_sha256,
		)
		return {"status": "already_processed", "job_applicant": existing.name}

	file_doc = None
	content_hash = get_content_hash(content)
	preexisting_file_names = set(
		frappe.get_all(
			"File",
			filters={"content_hash": content_hash, "is_private": 1},
			pluck="name",
		)
	)
	try:
		# Re-read under the same transaction lock immediately before any private
		# file is stored. This also detects unexpected in-process authority drift.
		if _job_opening() != job_opening:
			_block_admission(
				"blocked_single_open_vacancy_required",
				"La vacante autorizada cambió durante la admisión del correo.",
			)
		file_doc = _save_detached_private_file(filename, content)
		try:
			stored_sha256 = scan_stored_candidate_cv(file_doc)
		except CandidateCVScanUnavailableError:
			raise
		except CandidateCVSecurityError:
			_block_admission(
				"blocked_candidate_cv_security",
				"El CV almacenado no supera la validación determinística de seguridad.",
			)
		if stored_sha256 != attachment_sha256:
			_block_admission(
				"blocked_candidate_cv_security",
				"La evidencia del CV almacenado no coincide con el contenido validado.",
			)

		applicant = frappe.get_doc(
			{
				"doctype": "Job Applicant",
				"applicant_name": sender_name,
				"email_id": sender_email,
				"status": "Open",
				"job_title": job_opening,
				"source": EMAIL_RECRUITMENT_SOURCE,
				"resume_attachment": file_doc.file_url,
				# This existing field covers future-opportunity consent. A direct
				# submission grants processing only for the current hiring process.
				"custom_data_processing_consent": 0,
				"custom_privacy_notice_version": EMAIL_CONSENT_NOTICE_VERSION,
				"custom_candidate_profile": None,
				EMAIL_PROVENANCE_FIELD: 1,
				CV_FILE_FIELD: file_doc.name,
				MESSAGE_ID_FIELD: message_key,
				RECEIVED_ON_FIELD: received_on,
				SUBJECT_FIELD: subject,
				CURRENT_VACANCY_CONSENT_FIELD: 1,
				CONSENT_NOTICE_FIELD: EMAIL_CONSENT_NOTICE_VERSION,
				CONSENT_EVIDENCE_FIELD: consent_evidence_sha256,
			}
		)
		applicant.flags.ignore_notify = True
		applicant.flags.ayp_candidate_cv_file_name = file_doc.name
		with _suppress_document_notifications():
			applicant.insert()
		return {"status": "created", "job_applicant": applicant.name}
	except frappe.DuplicateEntryError as exc:
		winner = _existing_applicant(message_key, for_update=True)
		if not winner:
			raise EmailBridgeError(
				_("La carrera de idempotencia no produjo un registro verificable.")
			) from exc
		winner_file = _assert_duplicate_matches(
			winner,
			message_key=message_key,
			sender_email=sender_email,
			sender_name=sender_name,
			received_on=received_on,
			subject=subject,
			job_opening=job_opening,
			attachment_sha256=attachment_sha256,
			consent_evidence_sha256=consent_evidence_sha256,
		)
		if file_doc is not None and file_doc.name not in preexisting_file_names:
			_remove_verified_duplicate_file_row(file_doc, winner_file)
		file_doc = None
		return {"status": "already_processed", "job_applicant": winner.name}
	except Exception:
		# The caller owns rollback. Frappe registered after_rollback cleanup for
		# genuinely new bytes; File.on_trash is unsafe when a committed alias shares them.
		raise
