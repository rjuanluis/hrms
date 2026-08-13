from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

MAX_DOCUMENT_PAGES = 20
MAX_EXTRACTED_TEXT_CHARS = 100_000
MIN_USEFUL_TEXT_CHARS = 40
COMMAND_TIMEOUT_SECONDS = 45
OCR_LANGUAGES = "spa+eng"


class DocumentProcessingError(RuntimeError):
	def __init__(self, status: str, detail: str):
		super().__init__(detail)
		self.status = status
		self.detail = detail


@dataclass(frozen=True)
class ExtractionResult:
	text: str
	method: str
	page_count: int


def normalize_extracted_text(value: str) -> str:
	text = unicodedata.normalize("NFKC", value or "")
	text = text.replace("\r\n", "\n").replace("\r", "\n")
	text = "".join(
		character for character in text if character in "\n\t" or unicodedata.category(character) != "Cc"
	)
	lines = [" ".join(line.split()) for line in text.split("\n")]
	text = "\n".join(lines)
	text = re.sub(r"\n{3,}", "\n\n", text).strip()
	return text[:MAX_EXTRACTED_TEXT_CHARS]


def _useful(text: str) -> bool:
	return len(re.sub(r"\W", "", text, flags=re.UNICODE)) >= MIN_USEFUL_TEXT_CHARS


def _run(args: list[str], *, timeout: int = COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
	env = {
		"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
		"LANG": "C.UTF-8",
		"LC_ALL": "C.UTF-8",
		"HOME": "/tmp",
	}
	try:
		return subprocess.run(
			args,
			capture_output=True,
			text=True,
			timeout=timeout,
			check=False,
			env=env,
		)
	except (subprocess.TimeoutExpired, OSError) as exc:
		raise DocumentProcessingError(
			"Revisión manual", "El extractor no respondió de forma segura."
		) from exc


def _pdf_info(path: Path) -> tuple[int, bool]:
	result = _run(["pdfinfo", str(path)])
	combined = f"{result.stdout}\n{result.stderr}"
	if result.returncode != 0:
		if re.search(r"password|encrypted|decrypt", combined, flags=re.IGNORECASE):
			raise DocumentProcessingError("Protegido", "El PDF está protegido con contraseña.")
		raise DocumentProcessingError("Ilegible", "No se pudo leer la estructura del PDF.")
	pages_match = re.search(r"^Pages:\s*(\d+)\s*$", result.stdout, flags=re.MULTILINE | re.IGNORECASE)
	encrypted_match = re.search(r"^Encrypted:\s*(\S+)\s*$", result.stdout, flags=re.MULTILINE | re.IGNORECASE)
	if not pages_match:
		raise DocumentProcessingError("Ilegible", "El PDF no informa un conteo de páginas válido.")
	pages = int(pages_match.group(1))
	if pages < 1:
		raise DocumentProcessingError("Ilegible", "El PDF no contiene páginas.")
	if pages > MAX_DOCUMENT_PAGES:
		raise DocumentProcessingError(
			"Revisión manual",
			f"El PDF tiene {pages} páginas y supera el límite automático de {MAX_DOCUMENT_PAGES}.",
		)
	encrypted = bool(encrypted_match and encrypted_match.group(1).casefold() not in {"no", "false", "0"})
	if encrypted:
		raise DocumentProcessingError("Protegido", "El PDF está protegido con contraseña.")
	return pages, encrypted


def _ocr_image(path: Path) -> str:
	result = _run(["tesseract", str(path), "stdout", "-l", OCR_LANGUAGES, "--psm", "6"])
	if result.returncode != 0:
		raise DocumentProcessingError("Revisión manual", "OCR no pudo interpretar la imagen.")
	return normalize_extracted_text(result.stdout)


def _extract_pdf(content: bytes, directory: Path) -> ExtractionResult:
	path = directory / "candidate.pdf"
	path.write_bytes(content)
	pages, _ = _pdf_info(path)
	text_result = _run(["pdftotext", "-layout", str(path), "-"])
	text = normalize_extracted_text(text_result.stdout if text_result.returncode == 0 else "")
	if _useful(text):
		return ExtractionResult(text=text, method="PDF text", page_count=pages)

	prefix = directory / "page"
	render = _run(
		["pdftoppm", "-png", "-r", "200", "-f", "1", "-l", str(pages), str(path), str(prefix)],
		timeout=max(COMMAND_TIMEOUT_SECONDS, pages * 10),
	)
	if render.returncode != 0:
		raise DocumentProcessingError(
			"Ilegible", "El PDF no tiene texto utilizable y no pudo renderizarse para OCR."
		)
	parts = []
	for image_path in sorted(directory.glob("page-*.png")):
		part = _ocr_image(image_path)
		if part:
			parts.append(part)
	text = normalize_extracted_text("\n\n".join(parts))
	if not _useful(text):
		raise DocumentProcessingError("Ilegible", "OCR no encontró texto suficiente en el PDF.")
	return ExtractionResult(text=text, method="PDF OCR", page_count=pages)


def _extract_docx(content: bytes, directory: Path) -> ExtractionResult:
	from io import BytesIO

	try:
		with zipfile.ZipFile(BytesIO(content)) as archive:
			root = ElementTree.fromstring(archive.read("word/document.xml"))
			text = normalize_extracted_text(
				" ".join(node.text or "" for node in root.iter() if node.tag.endswith("}t"))
			)
			if _useful(text):
				return ExtractionResult(text=text, method="DOCX text", page_count=0)
			parts = []
			for index, name in enumerate(
				sorted(n for n in archive.namelist() if n.lower().startswith("word/media/"))
			):
				if index >= MAX_DOCUMENT_PAGES:
					break
				media_path = directory / f"docx-media-{index}{Path(name).suffix.lower()}"
				media_path.write_bytes(archive.read(name))
				try:
					part = _ocr_image(media_path)
				except DocumentProcessingError:
					continue
				if part:
					parts.append(part)
	except (KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
		raise DocumentProcessingError("Ilegible", "No se pudo interpretar el DOCX.") from exc
	text = normalize_extracted_text("\n\n".join(parts))
	if not _useful(text):
		raise DocumentProcessingError("Ilegible", "El DOCX no contiene texto utilizable.")
	return ExtractionResult(text=text, method="DOCX image OCR", page_count=len(parts))


def _extract_doc(content: bytes, directory: Path) -> ExtractionResult:
	path = directory / "candidate.doc"
	path.write_bytes(content)
	result = _run(["antiword", str(path)])
	combined = f"{result.stdout}\n{result.stderr}"
	if result.returncode != 0:
		status = (
			"Protegido" if re.search(r"encrypt|password|protect", combined, re.IGNORECASE) else "Ilegible"
		)
		raise DocumentProcessingError(status, "No se pudo interpretar el documento Word antiguo.")
	text = normalize_extracted_text(result.stdout)
	if not _useful(text):
		raise DocumentProcessingError("Ilegible", "El documento Word antiguo no contiene texto utilizable.")
	return ExtractionResult(text=text, method="DOC text", page_count=0)


def _extract_image(filename: str, content: bytes, directory: Path) -> ExtractionResult:
	extension = Path(filename).suffix.lower()
	path = directory / f"candidate{extension}"
	path.write_bytes(content)
	if extension in {".heic", ".heif"}:
		converted = directory / "candidate.png"
		result = _run(["heif-convert", str(path), str(converted)])
		if result.returncode != 0 or not converted.exists():
			raise DocumentProcessingError("Ilegible", "No se pudo convertir la imagen HEIC/HEIF.")
		path = converted
	text = _ocr_image(path)
	if not _useful(text):
		raise DocumentProcessingError("Ilegible", "OCR no encontró texto suficiente en la imagen.")
	return ExtractionResult(text=text, method="Image OCR", page_count=1)


def extract_candidate_document(filename: str, content: bytes) -> ExtractionResult:
	extension = Path(filename or "").suffix.lower()
	with tempfile.TemporaryDirectory(prefix="ayp-cv-") as temporary:
		directory = Path(temporary)
		if extension == ".pdf":
			return _extract_pdf(content, directory)
		if extension == ".docx":
			return _extract_docx(content, directory)
		if extension == ".doc":
			return _extract_doc(content, directory)
		if extension in {".jpg", ".jpeg", ".png", ".heic", ".heif"}:
			return _extract_image(filename, content, directory)
		raise DocumentProcessingError("No compatible", "El formato del CV no está permitido.")
