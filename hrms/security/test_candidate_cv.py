from __future__ import annotations

import io
import struct
import unittest
import zipfile
from unittest.mock import patch

from hrms.security.candidate_cv import (
	CandidateCVSecurityError,
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
	def test_accepts_simple_pdf(self):
		validate_cv_file("cv.pdf", b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF")

	def test_rejects_active_pdf(self):
		with self.assertRaises(CandidateCVSecurityError):
			validate_cv_file("cv.pdf", b"%PDF-1.7\n/JavaScript\n%%EOF")

	def test_accepts_simple_docx(self):
		validate_cv_file("cv.docx", make_docx())

	def test_rejects_docx_macro(self):
		with self.assertRaises(CandidateCVSecurityError):
			validate_cv_file("cv.docx", make_docx({"word/vbaProject.bin": b"macro"}))

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
