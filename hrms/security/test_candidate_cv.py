from __future__ import annotations

import io
import struct
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from hrms.security.candidate_cv import (
	CandidateCVSecurityError,
	guard_candidate_cv_upload,
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
		validate_cv_file("cv.pdf", b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF")

	def test_rejects_active_pdf(self):
		with self.assertRaises(CandidateCVSecurityError):
			validate_cv_file("cv.pdf", b"%PDF-1.7\n/JavaScript\n%%EOF")
		with self.assertRaises(CandidateCVSecurityError):
			validate_cv_file("cv.pdf", b"%PDF-1.7\n/OPENACTION\n%%EOF")

	def test_accepts_simple_docx(self):
		validate_cv_file("cv.docx", make_docx())

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


if __name__ == "__main__":
	unittest.main()
