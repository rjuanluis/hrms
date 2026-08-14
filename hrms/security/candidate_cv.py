from __future__ import annotations

import hashlib
import io
import os
import re
import socket
import struct
import subprocess
import sys
import zipfile
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import now_datetime

from hrms.recruitment.web_form_intake import (
	PRIVACY_NOTICE_VERSION,
	RECRUITMENT_WEB_FORM_ROUTE,
	WEB_SOURCE,
	authoritative_recruitment_web_form_context,
)
from hrms.security.pdf_cv_validator import (
	SELF_TEST_LIMIT_SETUP_FAILED,
	SELF_TEST_PARSER_IMPORT_FAILED,
	SELF_TEST_PARSER_PRELOADED,
)

MAX_CV_BYTES = 5 * 1024 * 1024
MAX_DOCX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_DOCX_ENTRIES = 1000
PDF_VALIDATION_TIMEOUT_SECONDS = 7
ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx", ".heic", ".heif", ".jpeg", ".jpg", ".png"}
PDF_NAME_ESCAPE = re.compile(rb"#([0-9a-fA-F]{2})")
CONSENT_WEB_FORM_ROUTE = RECRUITMENT_WEB_FORM_ROUTE
IMMUTABLE_RECRUITMENT_FILE_FIELDS = (
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


class CandidateCVSecurityError(frappe.ValidationError):
	pass


class CandidateCVInfrastructureError(RuntimeError):
	"""Retryable failure of infrastructure required to validate a CV."""


@contextmanager
def candidate_cv_file_identity(file_name: str):
	"""Bind validation and attachment hooks to one exact File row."""

	previous = getattr(frappe.local, "ayp_candidate_cv_file_name", None)
	frappe.local.ayp_candidate_cv_file_name = file_name
	try:
		yield
	finally:
		frappe.local.ayp_candidate_cv_file_name = previous


def _file_has_column(fieldname: str) -> bool:
	return frappe.db.has_column("File", fieldname)


def _persist_file_cv_sha256(file_name: str, sha256: str) -> None:
	frappe.db.set_value(
		"File",
		file_name,
		"custom_cv_sha256",
		sha256,
		update_modified=False,
	)


def read_stored_candidate_cv_bytes(file_doc) -> bytes:
	"""Read exact stored bytes from the validated File path without coercion."""
	try:
		# nosemgrep: frappe-security-file-traversal -- exact server-side File record, never a request path
		with open(file_doc.get_full_path(), "rb") as stored_file:
			return stored_file.read(MAX_CV_BYTES + 1)
	except OSError as exc:
		raise CandidateCVInfrastructureError(
			_("El almacenamiento del CV no está disponible temporalmente.")
		) from exc


def _is_candidate_cv_upload() -> bool:
	return (
		frappe.form_dict.get("doctype") == "Job Applicant"
		and frappe.form_dict.get("fieldname") == "resume_attachment"
	)


def _is_upload_endpoint() -> bool:
	request_path = (getattr(frappe.request, "path", "") or "").rstrip("/")
	return request_path.endswith("/api/method/upload_file") or frappe.form_dict.get("cmd") in (
		"upload_file",
		"frappe.handler.upload_file",
	)


def _decode_pdf_name(raw_name: bytes) -> bytes:
	if re.search(rb"#(?![0-9a-fA-F]{2})", raw_name):
		raise CandidateCVSecurityError(_("El PDF contiene un nombre interno inválido."))
	decoded = PDF_NAME_ESCAPE.sub(lambda match: bytes.fromhex(match.group(1).decode("ascii")), raw_name)
	return decoded.lower()


def _validate_pdf(content: bytes) -> None:
	if not content.startswith(b"%PDF-"):
		raise CandidateCVSecurityError(_("El archivo no es un PDF válido."))
	try:
		completed = subprocess.run(
			[sys.executable, str(Path(__file__).with_name("pdf_cv_validator.py"))],
			input=content,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			check=False,
			timeout=PDF_VALIDATION_TIMEOUT_SECONDS,
			close_fds=True,
			env={"PATH": os.environ.get("PATH", "")},
		)
	except (OSError, subprocess.SubprocessError) as exc:
		raise CandidateCVInfrastructureError(_("El validador PDF no está disponible temporalmente.")) from exc
	if completed.returncode < 0 or completed.returncode in {
		SELF_TEST_LIMIT_SETUP_FAILED,
		SELF_TEST_PARSER_PRELOADED,
		SELF_TEST_PARSER_IMPORT_FAILED,
	}:
		raise CandidateCVInfrastructureError(_("El validador PDF no está disponible temporalmente."))
	if completed.returncode != 0:
		raise CandidateCVSecurityError(_("El PDF está dañado, protegido o contiene contenido activo."))


def _validate_docx(content: bytes) -> None:
	try:
		with zipfile.ZipFile(io.BytesIO(content)) as archive:
			entries = archive.infolist()
			names = {entry.filename for entry in entries}
			if "[Content_Types].xml" not in names or "word/document.xml" not in names:
				raise CandidateCVSecurityError(_("El archivo no es un DOCX válido."))
			if len(entries) > MAX_DOCX_ENTRIES:
				raise CandidateCVSecurityError(_("El DOCX contiene demasiados elementos internos."))
			if sum(entry.file_size for entry in entries) > MAX_DOCX_UNCOMPRESSED_BYTES:
				raise CandidateCVSecurityError(_("El DOCX excede el tamaño interno permitido."))
			if any(entry.flag_bits & 0x1 for entry in entries):
				raise CandidateCVSecurityError(_("No se aceptan documentos DOCX cifrados."))
			if archive.testzip():
				raise CandidateCVSecurityError(
					_("El DOCX está dañado o no supera la validación de integridad.")
				)
			if any(
				name.lower().startswith("word/embeddings/")
				or name.lower().endswith((".bin", ".exe", ".dll", ".js", ".vbs"))
				for name in names
			):
				raise CandidateCVSecurityError(_("El DOCX contiene macros, objetos o archivos ejecutables."))
			for relationship_name in (name for name in names if name.lower().endswith(".rels")):
				try:
					relationships = ElementTree.fromstring(archive.read(relationship_name))
				except ElementTree.ParseError as exc:
					raise CandidateCVSecurityError(_("El DOCX contiene relaciones inválidas.")) from exc
				for relationship in relationships.iter():
					if relationship.attrib.get("TargetMode", "").lower() != "external":
						continue
					relationship_type = relationship.attrib.get("Type", "").lower()
					if not relationship_type.endswith("/hyperlink"):
						raise CandidateCVSecurityError(_("El DOCX contiene recursos externos no permitidos."))
	except zipfile.BadZipFile as exc:
		raise CandidateCVSecurityError(_("El archivo no es un DOCX válido.")) from exc


def _validate_image(extension: str, content: bytes) -> None:
	valid = {
		".jpg": content.startswith(b"\xff\xd8\xff"),
		".jpeg": content.startswith(b"\xff\xd8\xff"),
		".png": content.startswith(b"\x89PNG\r\n\x1a\n"),
		".heic": len(content) >= 12
		and content[4:8] == b"ftyp"
		and content[8:12] in {b"heic", b"heix", b"hevc", b"hevx", b"mif1"},
		".heif": len(content) >= 12
		and content[4:8] == b"ftyp"
		and content[8:12] in {b"heic", b"heix", b"hevc", b"hevx", b"mif1"},
	}[extension]
	if not valid:
		raise CandidateCVSecurityError(_("La imagen no coincide con el formato declarado."))


def _validate_legacy_doc(content: bytes) -> None:
	if not content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
		raise CandidateCVSecurityError(_("El archivo no es un documento Word .doc válido."))


def validate_cv_file(filename: str, content: bytes) -> None:
	if not content:
		raise CandidateCVSecurityError(_("El CV está vacío."))
	if len(content) > MAX_CV_BYTES:
		raise CandidateCVSecurityError(_("El CV no puede superar 5 MB."))

	extension = Path(filename or "").suffix.lower()
	if extension not in ALLOWED_EXTENSIONS:
		raise CandidateCVSecurityError(
			_("Solo se permiten CV en PDF, Word DOC/DOCX o imagen JPG, PNG y HEIC/HEIF.")
		)
	if extension == ".pdf":
		_validate_pdf(content)
	elif extension == ".docx":
		_validate_docx(content)
	elif extension == ".doc":
		_validate_legacy_doc(content)
	else:
		_validate_image(extension, content)


def scan_bytes_with_clamd(content: bytes, *, host: str, port: int, timeout: float = 20.0) -> str:
	with socket.create_connection((host, port), timeout=timeout) as connection:
		connection.settimeout(timeout)
		connection.sendall(b"zINSTREAM\0")
		for offset in range(0, len(content), 64 * 1024):
			chunk = content[offset : offset + 64 * 1024]
			connection.sendall(struct.pack("!I", len(chunk)))
			connection.sendall(chunk)
		connection.sendall(struct.pack("!I", 0))

		response = bytearray()
		while b"\0" not in response and len(response) < 4096:
			chunk = connection.recv(4096)
			if not chunk:
				break
			response.extend(chunk)

	text = bytes(response).rstrip(b"\0").decode("utf-8", errors="replace")
	if text.endswith(" OK"):
		return text
	if text.endswith(" FOUND"):
		raise CandidateCVSecurityError(_("El archivo fue rechazado por el control de seguridad."))
	raise RuntimeError(f"Unexpected ClamAV response: {text or '<empty>'}")


def _scan_candidate_cv(content: bytes) -> None:
	host = os.environ.get("AYP_CLAMAV_HOST") or frappe.conf.get("ayp_clamav_host") or "clamav"
	port = int(os.environ.get("AYP_CLAMAV_PORT") or frappe.conf.get("ayp_clamav_port") or 3310)
	try:
		scan_bytes_with_clamd(content, host=host, port=port)
	except CandidateCVSecurityError:
		raise
	except (OSError, RuntimeError) as exc:
		frappe.log_error(
			title="Candidate CV antivirus unavailable",
			message=f"ClamAV validation failed: {type(exc).__name__}: {exc}",
		)
		raise CandidateCVInfrastructureError(
			_("No pudimos validar el CV de forma segura. Intenta nuevamente en unos minutos.")
		) from exc


def _mark_file_clean(file_doc, *, sha256: str = "") -> None:
	values = {
		"custom_av_scan_status": "Clean",
		"custom_av_scan_engine": "ClamAV",
		"custom_av_scanned_on": now_datetime(),
	}
	if sha256 and _file_has_column("custom_cv_sha256"):
		values["custom_cv_sha256"] = sha256
	file_doc.db_set(values, update_modified=False)


def scan_stored_candidate_cv(file_doc) -> str:
	"""Validate and scan one already-private stored CV, returning its SHA-256.

	Inbound email attachments do not pass through the public upload preflight,
	so the email intake calls this before moving the File to a Job Applicant.
	"""

	if not file_doc.is_private or not str(file_doc.file_url or "").startswith("/private/files/"):
		raise CandidateCVSecurityError(_("El CV recibido debe permanecer como archivo privado."))
	content = read_stored_candidate_cv_bytes(file_doc)
	validate_cv_file(file_doc.file_name, content)
	_scan_candidate_cv(content)
	sha256 = hashlib.sha256(content).hexdigest()
	if file_doc.file_size != len(content):
		raise CandidateCVSecurityError(_("No se pudo verificar la integridad del CV recibido."))
	_mark_file_clean(file_doc, sha256=sha256)
	return sha256


def guard_candidate_cv_upload() -> None:
	if not _is_upload_endpoint():
		return

	if frappe.session.user == "Guest" and not _is_candidate_cv_upload():
		raise CandidateCVSecurityError(_("Los visitantes solo pueden cargar un CV en el campo autorizado."))
	if _is_candidate_cv_upload():
		_preflight_candidate_cv_upload()


@rate_limit(limit=10, seconds=60 * 60, methods=["POST"], ip_based=True)
def _preflight_candidate_cv_upload() -> None:
	file_storage = frappe.request.files.get("file")
	if not file_storage:
		raise CandidateCVSecurityError(_("Selecciona un CV para cargar."))

	content = file_storage.stream.read(MAX_CV_BYTES + 1)
	try:
		validate_cv_file(file_storage.filename or "", content)
		_scan_candidate_cv(content)
		frappe.form_dict.is_private = 1
		frappe.local.candidate_cv_preflight = {
			"sha256": hashlib.sha256(content).hexdigest(),
			"size": len(content),
		}
	finally:
		file_storage.stream.seek(0)


def mark_scanned_candidate_cv_file(file_doc, method=None) -> None:
	preflight = getattr(frappe.local, "candidate_cv_preflight", None)
	if not preflight:
		return

	av_fields = ("custom_av_scan_status", "custom_av_scan_engine", "custom_av_scanned_on")
	if not all(_file_has_column(fieldname) for fieldname in av_fields):
		raise CandidateCVSecurityError(
			_("El control antivirus todavía no está disponible. Intenta nuevamente en unos minutos.")
		)
	content = read_stored_candidate_cv_bytes(file_doc)
	if (
		not file_doc.is_private
		or file_doc.file_size != preflight["size"]
		or hashlib.sha256(content).hexdigest() != preflight["sha256"]
	):
		raise CandidateCVSecurityError(_("No se pudo verificar la integridad del CV cargado."))

	_mark_file_clean(file_doc, sha256=preflight["sha256"])
	frappe.local.candidate_cv_preflight = None


def _verified_candidate_cv_sha256(file_record) -> str:
	file_doc = frappe.get_doc("File", file_record.name)
	content = read_stored_candidate_cv_bytes(file_doc)
	validate_cv_file(file_record.file_name, content)
	actual_sha256 = hashlib.sha256(content).hexdigest()
	if len(content) != file_record.file_size or (
		file_record.custom_cv_sha256 and file_record.custom_cv_sha256 != actual_sha256
	):
		raise CandidateCVSecurityError(_("No se pudo verificar la integridad del CV cargado."))
	if not file_record.custom_cv_sha256:
		_persist_file_cv_sha256(file_record.name, actual_sha256)
	return actual_sha256


def _candidate_file_record(doc, fields):
	exact_name = getattr(frappe.local, "ayp_candidate_cv_file_name", None)
	if not exact_name and frappe.db.has_column("Job Applicant", "custom_candidate_cv_file"):
		exact_name = doc.get("custom_candidate_cv_file")
	if exact_name:
		return frappe.db.get_value("File", exact_name, fields, as_dict=True)
	matches = frappe.get_all("File", filters={"file_url": doc.resume_attachment}, fields=fields, limit=2)
	if len(matches) != 1:
		raise CandidateCVSecurityError(_("No se pudo resolver de forma única el archivo del CV."))
	return matches[0]


def _is_recruitment_candidate_file(file_record) -> bool:
	if file_record.attached_to_doctype == "Communication" and file_record.attached_to_name:
		email_account = frappe.db.get_value("Communication", file_record.attached_to_name, "email_account")
		email_id = frappe.db.get_value("Email Account", email_account, "email_id") if email_account else None
		configured = str(frappe.conf.get("ayp_recruitment_mailbox") or "empleos@aroypedal.com")
		return str(email_id or "").strip().casefold() == configured.strip().casefold()
	if file_record.attached_to_doctype == "Job Applicant" and file_record.attached_to_name:
		return (
			frappe.db.get_value("Job Applicant", file_record.attached_to_name, "source")
			== "Email Recursos Humanos"
		)
	return False


def _file_url_quarantine_records(file_url: str) -> list:
	return frappe.get_all(
		"File",
		filters={"file_url": file_url},
		fields=[
			"name",
			"file_name",
			"file_url",
			"attached_to_doctype",
			"attached_to_name",
			"custom_av_scan_status",
		],
	)


def _file_url_is_quarantined(file_url: str, *, fallback=None) -> bool:
	records = _file_url_quarantine_records(file_url) if file_url else ([fallback] if fallback else [])
	return any(
		record and _is_recruitment_candidate_file(record) and record.get("custom_av_scan_status") != "Clean"
		for record in records
	)


def _persisted_recruitment_file(file_doc):
	"""Return exact persisted authority when this File already belongs to recruitment."""

	file_name = file_doc.get("name") if hasattr(file_doc, "get") else getattr(file_doc, "name", None)
	if not file_name:
		return None
	persisted = frappe.db.get_value(
		"File",
		file_name,
		["name", *IMMUTABLE_RECRUITMENT_FILE_FIELDS],
		as_dict=True,
	)
	return persisted if persisted and _is_recruitment_candidate_file(persisted) else None


def _immutable_file_value(fieldname: str, value):
	if fieldname in {"file_size", "is_private"}:
		return int(value or 0)
	return str(value or "")


def validate_recruitment_cv_file_immutability(file_doc, method=None) -> None:
	"""Reject mutation before Frappe can move or rewrite a protected CV."""

	persisted = _persisted_recruitment_file(file_doc)
	if not persisted:
		return
	changed = [
		fieldname
		for fieldname in IMMUTABLE_RECRUITMENT_FILE_FIELDS
		if _immutable_file_value(fieldname, file_doc.get(fieldname))
		!= _immutable_file_value(fieldname, persisted.get(fieldname))
	]
	if changed:
		raise CandidateCVSecurityError(
			_("El archivo del CV es inmutable. Carga un CV nuevo para volver a ponerlo en cuarentena.")
		)


def prevent_recruitment_cv_file_deletion(file_doc, method=None) -> None:
	"""Require explicit retention governance instead of generic File deletion."""

	if _persisted_recruitment_file(file_doc) or _is_recruitment_candidate_file(file_doc):
		raise CandidateCVSecurityError(
			_("El archivo del CV no puede eliminarse fuera de una operación gobernada de retención.")
		)


def guard_candidate_cv_download() -> None:
	"""Deny direct/API download of recruitment CVs until the exact File is Clean."""

	request_path = str(getattr(frappe.request, "path", "") or "")
	file_url = (
		request_path
		if request_path.startswith("/private/files/")
		else str(frappe.form_dict.get("file_url") or "")
	)
	if not file_url.startswith("/private/files/"):
		return
	# `fid` is client-controlled and aliases can share one physical file_url.
	# Quarantine follows the bytes/path and examines every File row on it.
	if _file_url_is_quarantined(file_url):
		frappe.throw(
			_("This candidate CV is quarantined until its security scan completes."), frappe.PermissionError
		)


def has_candidate_cv_file_permission(doc, ptype=None, user=None, debug=False) -> bool:
	"""Deny mutation and quarantine reads for recruitment CV File rows."""

	if ptype in {"write", "delete", "share"} and _is_recruitment_candidate_file(doc):
		return False
	if ptype in {"read", "select", "print", "email"}:
		if _is_recruitment_candidate_file(doc) and doc.get("custom_av_scan_status") != "Clean":
			return False
		if _file_url_is_quarantined(str(doc.get("file_url") or ""), fallback=doc):
			return False
	return True


def validate_job_applicant_cv(doc, method=None) -> None:
	consent_bundle_fields = (
		"source",
		"custom_data_processing_consent",
		"custom_privacy_notice_version",
		"custom_consent_capture_method",
		"custom_consent_evidence_id",
		"custom_consent_recorded_on",
		"custom_consent_form_route",
	)
	before_save = doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None
	previous_values = before_save if before_save is not None else {}
	is_new = before_save is None
	web_context = authoritative_recruitment_web_form_context()
	is_authoritative_web_insert = bool(
		is_new
		and frappe.session.user == "Guest"
		and web_context
		and web_context.get("route") == RECRUITMENT_WEB_FORM_ROUTE
		and web_context.get("source") == WEB_SOURCE
		and web_context.get("job_opening")
	)
	if frappe.session.user == "Guest" and is_new and not is_authoritative_web_insert:
		raise CandidateCVSecurityError(_("Envía la solicitud mediante el formulario oficial de empleos."))
	if is_authoritative_web_insert:
		if not doc.get("custom_data_processing_consent"):
			raise CandidateCVSecurityError(
				_("Debes aceptar el aviso de privacidad para enviar la solicitud.")
			)
		doc.set("source", WEB_SOURCE)
		doc.set("status", "Open")
		doc.set("job_title", web_context.get("job_opening"))
		doc.set("custom_data_processing_consent", 1)
		doc.set("custom_privacy_notice_version", PRIVACY_NOTICE_VERSION)
		doc.set("custom_consent_capture_method", "Web Form")
		doc.set("custom_consent_evidence_id", frappe.generate_hash(length=32))
		doc.set("custom_consent_recorded_on", now_datetime())
		doc.set("custom_consent_form_route", CONSENT_WEB_FORM_ROUTE)
	elif is_new:
		# Internal imports cannot self-declare authoritative Web consent.
		if doc.get("source") == WEB_SOURCE:
			doc.set("source", "")
		doc.set("custom_data_processing_consent", 0)
		doc.set("custom_privacy_notice_version", "")
		doc.set("custom_consent_capture_method", "")
		doc.set("custom_consent_evidence_id", "")
		doc.set("custom_consent_recorded_on", None)
		doc.set("custom_consent_form_route", "")
	else:

		def normalized(fieldname, values):
			value = values.get(fieldname)
			return int(bool(value)) if fieldname == "custom_data_processing_consent" else str(value or "")

		before_claims_web_consent = any(
			normalized(fieldname, previous_values) for fieldname in consent_bundle_fields[1:]
		) or (normalized("source", previous_values) == WEB_SOURCE)
		current_claims_web_consent = any(
			normalized(fieldname, doc) for fieldname in consent_bundle_fields[1:]
		) or (normalized("source", doc) == WEB_SOURCE)
		bundle_changed = any(
			normalized(fieldname, doc) != normalized(fieldname, previous_values)
			for fieldname in consent_bundle_fields
		)
		if bundle_changed and (before_claims_web_consent or current_claims_web_consent):
			raise CandidateCVSecurityError(
				_("La evidencia de consentimiento Web solo puede cambiar mediante una operación gobernada.")
			)

	if not doc.resume_attachment:
		if frappe.db.has_column("Job Applicant", "custom_cv_sha256"):
			doc.custom_cv_sha256 = ""
		return
	file_security_fields = (
		"custom_av_scan_status",
		"custom_av_scan_engine",
		"custom_av_scanned_on",
		"custom_cv_sha256",
	)
	if not all(
		frappe.db.has_column("File", fieldname) for fieldname in file_security_fields
	) or not frappe.db.has_column("Job Applicant", "custom_cv_sha256"):
		raise CandidateCVSecurityError(
			_("El control antivirus todavía no está disponible. Intenta nuevamente en unos minutos.")
		)

	file_fields = [
		"name",
		"file_name",
		"file_url",
		"file_size",
		"content_hash",
		"is_private",
		"custom_av_scan_status",
		"custom_av_scan_engine",
		"custom_av_scanned_on",
		"attached_to_doctype",
		"attached_to_name",
		"attached_to_field",
		"custom_cv_sha256",
	]
	file_record = _candidate_file_record(doc, file_fields)
	invalid_attachment = (
		not file_record
		or file_record.file_url != doc.resume_attachment
		or not file_record.is_private
		or not file_record.file_url.startswith("/private/files/")
		or not file_record.file_name
		or Path(file_record.file_name).suffix.lower() not in ALLOWED_EXTENSIONS
		or not file_record.content_hash
		or not file_record.file_size
		or file_record.file_size > MAX_CV_BYTES
		or file_record.custom_av_scan_status != "Clean"
		or file_record.custom_av_scan_engine != "ClamAV"
		or not file_record.custom_av_scanned_on
		or file_record.attached_to_doctype not in (None, "", "Job Applicant")
		or (file_record.attached_to_name and file_record.attached_to_name != doc.name)
		or file_record.attached_to_field not in (None, "", "resume_attachment")
	)
	if invalid_attachment:
		raise CandidateCVSecurityError(
			_("El CV debe cargarse como archivo privado y pasar el control antivirus.")
		)
	doc.set("custom_cv_sha256", _verified_candidate_cv_sha256(file_record))
	if frappe.db.has_column("Job Applicant", "custom_candidate_cv_file"):
		doc.set("custom_candidate_cv_file", file_record.name)


def attach_job_applicant_cv(doc, method=None) -> None:
	if not doc.resume_attachment:
		return

	file_record = _candidate_file_record(doc, ["name", "file_url"])
	if not file_record or file_record.file_url != doc.resume_attachment:
		raise CandidateCVSecurityError(_("No se pudo confirmar el archivo exacto del CV."))
	if file_record.name:
		frappe.db.set_value(
			"File",
			file_record.name,
			{
				"attached_to_doctype": "Job Applicant",
				"attached_to_name": doc.name,
				"attached_to_field": "resume_attachment",
			},
			update_modified=False,
		)
