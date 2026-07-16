from __future__ import annotations

import io
import os
import socket
import struct
import zipfile
from pathlib import Path

import frappe
from frappe import _
from frappe.handler import upload_file as frappe_upload_file
from frappe.rate_limiter import rate_limit
from frappe.utils import now_datetime

MAX_CV_BYTES = 5 * 1024 * 1024
MAX_DOCX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_DOCX_ENTRIES = 1000
ALLOWED_EXTENSIONS = {".pdf", ".docx"}
PDF_ACTIVE_MARKERS = (b"/JavaScript", b"/JS", b"/Launch", b"/EmbeddedFile", b"/RichMedia", b"/XFA")


class CandidateCVSecurityError(frappe.ValidationError):
	pass


def _is_candidate_cv_upload() -> bool:
	return (
		frappe.form_dict.get("doctype") == "Job Applicant"
		and frappe.form_dict.get("fieldname") == "resume_attachment"
	)


def _validate_pdf(content: bytes) -> None:
	if not content.startswith(b"%PDF-"):
		raise CandidateCVSecurityError(_("El archivo no es un PDF válido."))
	if any(marker in content for marker in PDF_ACTIVE_MARKERS):
		raise CandidateCVSecurityError(
			_("El PDF contiene contenido activo o archivos incrustados y no puede aceptarse.")
		)


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
			if any(
				name.lower().endswith(("vbaproject.bin", ".exe", ".dll", ".js", ".vbs")) for name in names
			):
				raise CandidateCVSecurityError(_("El DOCX contiene macros o archivos ejecutables."))
	except zipfile.BadZipFile as exc:
		raise CandidateCVSecurityError(_("El archivo no es un DOCX válido.")) from exc


def validate_cv_file(filename: str, content: bytes) -> None:
	if not content:
		raise CandidateCVSecurityError(_("El CV está vacío."))
	if len(content) > MAX_CV_BYTES:
		raise CandidateCVSecurityError(_("El CV no puede superar 5 MB."))

	extension = Path(filename or "").suffix.lower()
	if extension not in ALLOWED_EXTENSIONS:
		raise CandidateCVSecurityError(_("Solo se permiten archivos PDF o DOCX."))
	if extension == ".pdf":
		_validate_pdf(content)
	else:
		_validate_docx(content)


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
		raise CandidateCVSecurityError(
			_("No pudimos validar el CV de forma segura. Intenta nuevamente en unos minutos.")
		) from exc


def _mark_file_clean(file_doc) -> None:
	values = {
		"custom_av_scan_status": "Clean",
		"custom_av_scan_engine": "ClamAV",
		"custom_av_scanned_on": now_datetime(),
	}
	if all(frappe.db.has_column("File", fieldname) for fieldname in values):
		file_doc.db_set(values, update_modified=False)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def upload_file():
	if not _is_candidate_cv_upload():
		return frappe_upload_file()
	return _upload_candidate_cv()


@rate_limit(limit=10, seconds=60 * 60, methods=["POST"], ip_based=True)
def _upload_candidate_cv():
	file_storage = frappe.request.files.get("file")
	if not file_storage:
		raise CandidateCVSecurityError(_("Selecciona un CV para cargar."))

	content = file_storage.stream.read(MAX_CV_BYTES + 1)
	try:
		validate_cv_file(file_storage.filename or "", content)
		_scan_candidate_cv(content)
	finally:
		file_storage.stream.seek(0)

	frappe.form_dict.is_private = 1
	file_doc = frappe_upload_file()
	_mark_file_clean(file_doc)
	return file_doc


def validate_job_applicant_cv(doc, method=None) -> None:
	if frappe.session.user == "Guest" and not doc.custom_data_processing_consent:
		raise CandidateCVSecurityError(_("Debes aceptar el aviso de privacidad para enviar la solicitud."))

	if not doc.resume_attachment:
		return

	file_record = frappe.db.get_value(
		"File",
		{"file_url": doc.resume_attachment},
		[
			"name",
			"file_url",
			"is_private",
			"custom_av_scan_status",
			"attached_to_doctype",
			"attached_to_name",
		],
		as_dict=True,
	)
	invalid_attachment = (
		not file_record
		or not file_record.is_private
		or not file_record.file_url.startswith("/private/files/")
		or file_record.custom_av_scan_status != "Clean"
		or file_record.attached_to_doctype not in (None, "", "Job Applicant")
		or (file_record.attached_to_name and file_record.attached_to_name != doc.name)
	)
	if invalid_attachment:
		raise CandidateCVSecurityError(
			_("El CV debe cargarse como archivo privado y pasar el control antivirus.")
		)


def attach_job_applicant_cv(doc, method=None) -> None:
	if not doc.resume_attachment:
		return

	file_name = frappe.db.get_value("File", {"file_url": doc.resume_attachment}, "name")
	if file_name:
		frappe.db.set_value(
			"File",
			file_name,
			{
				"attached_to_doctype": "Job Applicant",
				"attached_to_name": doc.name,
				"attached_to_field": "resume_attachment",
			},
			update_modified=False,
		)
