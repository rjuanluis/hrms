from __future__ import annotations

import hashlib
import io
import os
import struct
import subprocess
import sys
import time
import unittest
import zipfile
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, IndirectObject, NameObject, TextStringObject

import frappe

from hrms.security.candidate_cv import (
	CandidateCVSecurityError,
	_decode_pdf_name,
	_mark_file_clean,
	_verified_candidate_cv_sha256,
	guard_candidate_cv_upload,
	mark_scanned_candidate_cv_file,
	scan_stored_candidate_cv,
	scan_bytes_with_clamd,
	validate_cv_file,
)


class FakeSocket:
	def __init__(self, response: bytes):
		self.response = response
		self.sent = bytearray()

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc, traceback):
		return False

	def settimeout(self, timeout):
		self.timeout = timeout

	def sendall(self, data: bytes):
		self.sent.extend(data)

	def recv(self, size: int) -> bytes:
		response, self.response = self.response[:size], self.response[size:]
		return response


def make_docx(extra_files: dict[str, bytes] | None = None) -> bytes:
	buffer = io.BytesIO()
	with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
		archive.writestr("[Content_Types].xml", "<Types />")
		archive.writestr("word/document.xml", "<document />")
		for name, content in (extra_files or {}).items():
			archive.writestr(name, content)
	return buffer.getvalue()


def make_pdf(*, active: bool = False) -> bytes:
	buffer = io.BytesIO()
	writer = PdfWriter()
	writer.add_blank_page(width=612, height=792)
	if active:
		writer.root_object[NameObject("/OpenAction")] = DictionaryObject(
			{
				NameObject("/S"): NameObject("/JavaScript"),
				NameObject("/JS"): TextStringObject("app.alert('blocked')"),
			}
		)
	writer.write(buffer)
	return buffer.getvalue()


def make_pdf_with_raw_catalog_entry(entry: bytes) -> bytes:
	objects = (
		b"<< /Type /Catalog /Pages 2 0 R " + entry + b" >>",
		b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
		b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
	)
	content = bytearray(b"%PDF-1.7\n")
	offsets = []
	for object_id, payload in enumerate(objects, start=1):
		offsets.append(len(content))
		content.extend(f"{object_id} 0 obj\n".encode())
		content.extend(payload + b"\nendobj\n")
	xref_offset = len(content)
	content.extend(b"xref\n0 4\n0000000000 65535 f \n")
	for offset in offsets:
		content.extend(f"{offset:010d} 00000 n \n".encode())
	content.extend(b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n")
	content.extend(str(xref_offset).encode() + b"\n%%EOF\n")
	return bytes(content)


_OBJSTM_DECODED = b"4 0 << /S /JavaScript /JS (blocked only inside ObjStm) >>"
_OBJSTM_FLATE = bytes.fromhex(
	"78da33513050b0b151d00f56d0f74a2c4b0c4e2eca2c2801b28315349272f293"
	"b3535314f2f3722a1532f38a33535215fc93b2824b723515ecec00e43911bf"
)


def make_pdf_with_compressed_action_object() -> bytes:
	"""Build deterministic PDF 1.5 with an orphan action only in /ObjStm 5."""

	assert zlib.decompress(_OBJSTM_FLATE) == _OBJSTM_DECODED
	content = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
	offsets = {}

	def add_object(object_id: int, payload: bytes) -> None:
		offsets[object_id] = len(content)
		content.extend(f"{object_id} 0 obj\n".encode("ascii"))
		content.extend(payload + b"\nendobj\n")

	add_object(1, b"<< /Type /Catalog /Pages 2 0 R >>")
	add_object(2, b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>")
	add_object(3, b"<< /Type /Page /Parent 2 0 R /Resources << >> /MediaBox [0 0 612 792] >>")
	add_object(
		5,
		(
			b"<< /Type /ObjStm /N 1 /First 4 /Filter /FlateDecode /Length 63 >>\nstream\n"
			+ _OBJSTM_FLATE
			+ b"\nendstream"
		),
	)
	xref_offset = len(content)
	entries = (
		(0, 0, 65535),
		(1, offsets[1], 0),
		(1, offsets[2], 0),
		(1, offsets[3], 0),
		(2, 5, 0),
		(1, offsets[5], 0),
		(1, xref_offset, 0),
	)
	xref_data = b"".join(struct.pack(">BIH", *entry) for entry in entries)
	add_object(
		6,
		(
			b"<< /Type /XRef /Size 7 /Root 1 0 R /W [1 4 2] /Index [0 7] /Length 49 >>\nstream\n"
			+ xref_data
			+ b"\nendstream"
		),
	)
	content.extend(f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii"))
	return bytes(content)


class TestCandidateCVSecurity(unittest.TestCase):
	def setUp(self):
		frappe.local.form_dict = frappe._dict()
		frappe.local.session = frappe._dict(user="Guest")
		frappe.local.request = SimpleNamespace(path="/api/method/upload_file", method="POST", files={})

	def test_guest_job_applicant_upload_rejects_other_fields(self):
		frappe.local.form_dict.update(doctype="Job Applicant", fieldname="cover_letter")
		with self.assertRaises(CandidateCVSecurityError):
			guard_candidate_cv_upload()

	def test_candidate_cv_upload_reaches_preflight(self):
		frappe.local.form_dict.update(doctype="Job Applicant", fieldname="resume_attachment")
		with patch("hrms.security.candidate_cv._preflight_candidate_cv_upload") as preflight:
			guard_candidate_cv_upload()
		preflight.assert_called_once_with()

	def test_accepts_simple_pdf(self):
		validate_cv_file("cv.pdf", make_pdf())

	def test_rejects_structurally_active_pdf(self):
		with self.assertRaises(CandidateCVSecurityError):
			validate_cv_file("cv.pdf", make_pdf(active=True))

	def test_rejects_active_dictionary_inside_real_compressed_object_stream(self):
		content = make_pdf_with_compressed_action_object()
		self.assertEqual(len(content), 546)
		self.assertEqual(
			hashlib.sha256(content).hexdigest(),
			"0cb69cb71ecf3872d295be5f6911b09f1357a8ceb80f4a4c2343a071efc464e8",
		)
		self.assertNotIn(b"\n4 0 obj\n", content)
		self.assertNotIn(b"/JavaScript", content)
		self.assertNotIn(b"/OpenAction", content)
		reader = PdfReader(io.BytesIO(content), strict=True)
		self.assertEqual(reader.xref_objStm, {4: (5, 0)})
		self.assertNotIn(4, reader.xref.get(0, {}))
		self.assertNotIn((0, 4), reader.resolved_objects)
		objstm = reader.get_object(IndirectObject(5, 0, reader))
		self.assertEqual(objstm["/Type"], "/ObjStm")
		self.assertEqual(objstm.get_data(), _OBJSTM_DECODED)
		action = reader.get_object(IndirectObject(4, 0, reader))
		self.assertEqual(action["/S"], "/JavaScript")
		self.assertEqual(action["/JS"], "blocked only inside ObjStm")
		self.assertIs(reader.resolved_objects[(0, 4)], action)
		with self.assertRaises(CandidateCVSecurityError):
			validate_cv_file("cv.pdf", content)

	def test_rejects_active_pdf(self):
		for entry in (
			b"/OpenAction << /S /JavaScript /JS (blocked) >>",
			b"/AA << /O << /S /JavaScript /JS (blocked) >> >>",
		):
			with self.subTest(entry=entry), self.assertRaises(CandidateCVSecurityError):
				validate_cv_file("cv.pdf", make_pdf_with_raw_catalog_entry(entry))

	def test_rejects_hex_escaped_active_pdf_names(self):
		with self.assertRaises(CandidateCVSecurityError):
			validate_cv_file(
				"cv.pdf",
				make_pdf_with_raw_catalog_entry(b"/Open#41ction << /S /Java#53cript /JS (blocked) >>"),
			)

	def test_rejects_malformed_pdf_name_escape(self):
		for malformed in (b"/Bad#ZZName null", b"/Bad#1 null", b"/Bad# null"):
			with self.subTest(malformed=malformed), self.assertRaises(CandidateCVSecurityError):
				validate_cv_file("cv.pdf", make_pdf_with_raw_catalog_entry(malformed))

	def test_rejects_embedded_file_without_optional_type(self):
		entry = b"/Names << /EmbeddedFiles << /Names [(payload) << /Type /Filespec /F (x.txt) /EF << /F 4 0 R >> >>] >> >>"
		base = make_pdf_with_raw_catalog_entry(entry)
		base = base.replace(
			b"xref\n0 4", b"4 0 obj\n<< /Length 4 >>\nstream\ntest\nendstream\nendobj\nxref\n0 4"
		)
		with self.assertRaises(CandidateCVSecurityError):
			validate_cv_file("cv.pdf", base)

	def test_rejects_submit_form_action(self):
		with self.assertRaises(CandidateCVSecurityError):
			validate_cv_file(
				"cv.pdf",
				make_pdf_with_raw_catalog_entry(
					b"/OpenAction << /S /SubmitForm /F (https://invalid.example/) >>"
				),
			)

	def test_accepts_inert_name_js(self):
		validate_cv_file("cv.pdf", make_pdf_with_raw_catalog_entry(b"/Resources << /JS 3 0 R >>"))

	def test_pdf_subprocess_failure_is_fail_closed(self):
		with patch("hrms.security.candidate_cv.subprocess.run", side_effect=TimeoutError("bounded")):
			with self.assertRaises(CandidateCVSecurityError):
				validate_cv_file("cv.pdf", make_pdf())

	def test_pdf_parser_child_enforces_memory_ceiling(self):
		from hrms.security.pdf_cv_validator import SELF_TEST_MEMORY_LIMIT_ENFORCED

		validator = Path(__file__).with_name("pdf_cv_validator.py")
		started = time.monotonic()
		completed = subprocess.run(
			[sys.executable, str(validator)],
			input=b"",
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			check=False,
			timeout=7,
			env={"PATH": os.environ.get("PATH", ""), "AYP_PDF_VALIDATOR_SELF_TEST": "memory"},
		)
		self.assertEqual(completed.returncode, SELF_TEST_MEMORY_LIMIT_ENFORCED)
		self.assertLess(time.monotonic() - started, 7)

	def test_pdf_parser_self_test_distinguishes_limit_setup_failure(self):
		from hrms.security import pdf_cv_validator

		with (
			patch.dict(os.environ, {"AYP_PDF_VALIDATOR_SELF_TEST": "memory"}),
			patch.object(
				pdf_cv_validator, "_set_limits", side_effect=pdf_cv_validator.PDFSecurityError("no-limit")
			),
			patch.object(pdf_cv_validator, "_load_parser") as load_parser,
		):
			result = pdf_cv_validator.main()
		self.assertEqual(result, pdf_cv_validator.SELF_TEST_LIMIT_SETUP_FAILED)
		load_parser.assert_not_called()

	def test_pdf_parser_self_test_rejects_preloaded_parser(self):
		from hrms.security import pdf_cv_validator

		with (
			patch.dict(os.environ, {"AYP_PDF_VALIDATOR_SELF_TEST": "memory"}),
			patch.dict(sys.modules, {"pypdf.preloaded_probe": object()}),
			patch.object(pdf_cv_validator, "_set_limits"),
			patch.object(pdf_cv_validator, "_load_parser") as load_parser,
		):
			result = pdf_cv_validator.main()
		self.assertEqual(result, pdf_cv_validator.SELF_TEST_PARSER_PRELOADED)
		load_parser.assert_not_called()

	def test_pdf_parser_limit_setup_fails_closed_when_no_memory_limit_is_effective(self):
		from hrms.security import pdf_cv_validator

		with patch.object(pdf_cv_validator, "_set_memory_limit", return_value=False):
			with self.assertRaisesRegex(pdf_cv_validator.PDFSecurityError, "memory-limit-unavailable"):
				pdf_cv_validator._set_limits()

	def test_parent_does_not_forward_parser_self_test_environment(self):
		with patch.dict(os.environ, {"AYP_PDF_VALIDATOR_SELF_TEST": "memory"}):
			validate_cv_file("cv.pdf", make_pdf())

	def test_accepts_pdf_names_that_only_prefix_match_active_names(self):
		self.assertEqual(_decode_pdf_name(b"AAAAAB+Monaco"), b"aaaaab+monaco")
		self.assertEqual(_decode_pdf_name(b"Java#53criptEnabled"), b"javascriptenabled")
		self.assertEqual(_decode_pdf_name(b"Literal#23Hash"), b"literal#hash")

	def test_accepts_simple_docx(self):
		validate_cv_file("cv.docx", make_docx())

	def test_accepts_supported_images_and_legacy_doc_by_signature(self):
		validate_cv_file("cv.jpg", b"\xff\xd8\xff\xe0synthetic")
		validate_cv_file("cv.png", b"\x89PNG\r\n\x1a\nsynthetic")
		validate_cv_file("cv.heic", b"\x00\x00\x00\x18ftypheicsynthetic")
		validate_cv_file("cv.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1synthetic")

	def test_rejects_extension_content_mismatches(self):
		for filename, content in (
			("cv.jpg", b"%PDF-1.7"),
			("cv.png", b"not-png"),
			("cv.heic", b"not-heic"),
			("cv.doc", b"not-ole"),
		):
			with self.subTest(filename=filename), self.assertRaises(CandidateCVSecurityError):
				validate_cv_file(filename, content)

	def test_rejects_docx_macro(self):
		with self.assertRaises(CandidateCVSecurityError):
			validate_cv_file("cv.docx", make_docx({"word/vbaProject.bin": b"macro"}))

	def test_rejects_external_docx_resource(self):
		relationships = b"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
		<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
		Target="https://example.invalid/tracker.png" TargetMode="External" />
		</Relationships>"""
		with self.assertRaises(CandidateCVSecurityError):
			validate_cv_file("cv.docx", make_docx({"word/_rels/document.xml.rels": relationships}))

	def test_allows_external_docx_hyperlink(self):
		relationships = b"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
		<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
		Target="https://www.linkedin.com/" TargetMode="External" />
		</Relationships>"""
		validate_cv_file("cv.docx", make_docx({"word/_rels/document.xml.rels": relationships}))

	def test_rejects_unsupported_extension(self):
		with self.assertRaises(CandidateCVSecurityError):
			validate_cv_file("cv.exe", b"MZ")

	def test_clamd_clean_response_and_protocol(self):
		connection = FakeSocket(b"stream: OK\0")
		with patch("socket.create_connection", return_value=connection):
			response = scan_bytes_with_clamd(b"clean", host="clamav", port=3310)
		self.assertEqual(response, "stream: OK")
		self.assertTrue(connection.sent.startswith(b"zINSTREAM\0"))
		self.assertTrue(connection.sent.endswith(struct.pack("!I", 0)))

	def test_clamd_found_response_is_rejected(self):
		connection = FakeSocket(b"stream: Win.Test.EICAR_HDB-1 FOUND\0")
		with patch("socket.create_connection", return_value=connection):
			with self.assertRaises(CandidateCVSecurityError):
				scan_bytes_with_clamd(b"eicar", host="clamav", port=3310)

	def test_clean_file_persists_preflight_sha256_when_field_exists(self):
		file_doc = SimpleNamespace(
			db_set=lambda values, update_modified=False: setattr(file_doc, "values", values)
		)
		with (
			patch("hrms.security.candidate_cv._file_has_column", return_value=True),
			patch("hrms.security.candidate_cv.now_datetime", return_value="2026-08-12 20:00:00"),
		):
			_mark_file_clean(file_doc, sha256="a" * 64)
		self.assertEqual(file_doc.values["custom_cv_sha256"], "a" * 64)
		self.assertEqual(file_doc.values["custom_av_scan_status"], "Clean")

	def test_inbound_private_cv_is_scanned_from_exact_bytes_and_marked_clean(self):
		content = make_pdf()
		file_doc = SimpleNamespace(
			file_name="cv.pdf",
			file_url="/private/files/cv.pdf",
			file_size=len(content),
			is_private=1,
			db_set=lambda values, update_modified=False: setattr(file_doc, "values", values),
		)
		with (
			patch("hrms.security.candidate_cv.read_stored_candidate_cv_bytes", return_value=content),
			patch("hrms.security.candidate_cv._scan_candidate_cv") as scan,
			patch("hrms.security.candidate_cv._file_has_column", return_value=True),
		):
			sha256 = scan_stored_candidate_cv(file_doc)
		self.assertEqual(sha256, hashlib.sha256(content).hexdigest())
		scan.assert_called_once_with(content)
		self.assertEqual(file_doc.values["custom_av_scan_status"], "Clean")
		self.assertEqual(file_doc.values["custom_cv_sha256"], sha256)

	def test_inbound_public_cv_is_rejected_before_scan(self):
		file_doc = SimpleNamespace(
			file_name="cv.pdf",
			file_url="/files/cv.pdf",
			file_size=100,
			is_private=0,
		)
		with self.assertRaisesRegex(CandidateCVSecurityError, "archivo privado"):
			scan_stored_candidate_cv(file_doc)

	def test_verified_hash_revalidates_pdf_content(self):
		content = make_pdf()
		file_record = SimpleNamespace(
			name="FILE-1",
			file_name="cv.pdf",
			file_size=len(content),
			custom_cv_sha256="",
		)
		with (
			patch("frappe.get_doc", return_value=SimpleNamespace(file_url="/private/files/cv.pdf")),
			patch("hrms.security.candidate_cv.get_file", return_value=("cv.pdf", content)),
			patch("hrms.security.candidate_cv._persist_file_cv_sha256") as persist_sha256,
		):
			sha256 = _verified_candidate_cv_sha256(file_record)
		self.assertEqual(sha256, hashlib.sha256(content).hexdigest())
		persist_sha256.assert_called_once_with(file_record.name, sha256)

	def test_verified_hash_rejects_invalid_pdf_signature(self):
		content = b"not-a-pdf"
		file_record = SimpleNamespace(
			name="FILE-2",
			file_name="cv.pdf",
			file_size=len(content),
			custom_cv_sha256="",
		)
		with (
			patch("frappe.get_doc", return_value=SimpleNamespace(file_url="/private/files/cv.pdf")),
			patch("hrms.security.candidate_cv.get_file", return_value=("cv.pdf", content)),
		):
			with self.assertRaises(CandidateCVSecurityError):
				_verified_candidate_cv_sha256(file_record)

	def test_after_insert_integrity_uses_exact_binary_bytes(self):
		content = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n%%EOF"
		sha256 = hashlib.sha256(content).hexdigest()
		file_doc = SimpleNamespace(
			file_url="/private/files/cv.pdf",
			file_size=len(content),
			is_private=1,
			db_set=lambda values, update_modified=False: setattr(file_doc, "values", values),
		)
		frappe.local.candidate_cv_preflight = {"sha256": sha256, "size": len(content)}
		with (
			patch("hrms.security.candidate_cv._file_has_column", return_value=True),
			patch("hrms.security.candidate_cv.get_file", return_value=("cv.pdf", content)),
			patch("hrms.security.candidate_cv.now_datetime", return_value="2026-08-12 20:00:00"),
		):
			mark_scanned_candidate_cv_file(file_doc)
		self.assertEqual(file_doc.values["custom_cv_sha256"], sha256)
		self.assertEqual(file_doc.values["custom_av_scan_status"], "Clean")


if __name__ == "__main__":
	unittest.main()
