from __future__ import annotations

import hashlib
import io
import os
import socket
import struct
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import now_datetime
from frappe.utils.file_manager import get_file

MAX_CV_BYTES = 5 * 1024 * 1024
MAX_DOCX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_DOCX_ENTRIES = 1000
ALLOWED_EXTENSIONS = {".pdf", ".docx"}
PDF_ACTIVE_MARKERS = (
	b"/javascript",
	b"/js",
	b"/launch",
	b"/embeddedfile",
	b"/richmedia",
	b"/xfa",
	b"/openaction",
	b"/aa",
	b"/acroform",
)
PRIVACY_NOTICE_VERSION = "AYP-RH-2026-07-17-v3"


class CandidateCVSecurityError(frappe.ValidationError):
	pass


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


def _read_file_bytes(file_doc) -> bytes:
	"""Read the exact stored bytes without File.get_content() text coercion."""
	_, content = get_file(file_doc.file_url)
	if not isinstance(content, bytes):
		raise CandidateCVSecurityError(_("No se pudo leer el CV como contenido binario seguro."))
	return content


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


def _validate_pdf(content: bytes) -> None:
	if not content.startswith(b"%PDF-"):
		raise CandidateCVSecurityError(_("El archivo no es un PDF válido."))
	if any(marker in content.lower() for marker in PDF_ACTIVE_MARKERS):
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


def _mark_file_clean(file_doc, *, sha256: str = "") -> None:
	values = {
		"custom_av_scan_status": "Clean",
		"custom_av_scan_engine": "ClamAV",
		"custom_av_scanned_on": now_datetime(),
	}
	if sha256 and _file_has_column("custom_cv_sha256"):
		values["custom_cv_sha256"] = sha256
	file_doc.db_set(values, update_modified=False)


def guard_candidate_cv_upload() -> None:
	if not _is_upload_endpoint():
		return

	is_job_applicant_upload = frappe.form_dict.get("doctype") == "Job Applicant"
	if frappe.session.user == "Guest" and is_job_applicant_upload and not _is_candidate_cv_upload():
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
	content = _read_file_bytes(file_doc)
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
	content = _read_file_bytes(file_doc)
	validate_cv_file(file_record.file_name, content)
	actual_sha256 = hashlib.sha256(content).hexdigest()
	if len(content) != file_record.file_size or (
		file_record.custom_cv_sha256 and file_record.custom_cv_sha256 != actual_sha256
	):
		raise CandidateCVSecurityError(_("No se pudo verificar la integridad del CV cargado."))
	if not file_record.custom_cv_sha256:
		_persist_file_cv_sha256(file_record.name, actual_sha256)
	return actual_sha256


def validate_job_applicant_cv(doc, method=None) -> None:
	if frappe.session.user == "Guest" and not doc.get("custom_data_processing_consent"):
		raise CandidateCVSecurityError(_("Debes aceptar el aviso de privacidad para enviar la solicitud."))
	if frappe.session.user == "Guest":
		doc.set("custom_privacy_notice_version", PRIVACY_NOTICE_VERSION)

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
	file_record = frappe.db.get_value(
		"File",
		{"file_url": doc.resume_attachment},
		file_fields,
		as_dict=True,
	)
	invalid_attachment = (
		not file_record
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
