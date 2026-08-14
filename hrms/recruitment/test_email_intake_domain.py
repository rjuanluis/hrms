from __future__ import annotations

import unittest

from hrms.recruitment.email_intake_domain import (
	EmailIntakeDomainError,
	same_vacancy_application,
	select_candidate_cv,
	sender_identity,
)


class TestEmailIntakeDomain(unittest.TestCase):
	def test_sender_identity_uses_header_name_and_normalizes_email(self):
		self.assertEqual(
			sender_identity("Ana Pérez <ANA.PEREZ@Example.COM>", ""),
			("ana.perez@example.com", "Ana Pérez"),
		)

	def test_sender_identity_falls_back_to_local_part(self):
		self.assertEqual(
			sender_identity("ana_perez@example.com"),
			("ana_perez@example.com", "Ana Perez"),
		)

	def test_sender_identity_rejects_invalid_sender(self):
		with self.assertRaisesRegex(EmailIntakeDomainError, "correo válido"):
			sender_identity("sin-correo")

	def test_select_candidate_cv_ignores_inline_images(self):
		rows = [
			{"name": "logo", "file_name": "logo.png"},
			{"name": "cv", "file_name": "CV Ana.PDF"},
		]
		self.assertEqual(select_candidate_cv(rows)["name"], "cv")

	def test_select_candidate_cv_rejects_zero_or_multiple_documents(self):
		with self.assertRaisesRegex(EmailIntakeDomainError, "no contiene"):
			select_candidate_cv([{"name": "logo", "file_name": "logo.png"}])
		with self.assertRaisesRegex(EmailIntakeDomainError, "más de un"):
			select_candidate_cv(
				[
					{"name": "one", "file_name": "cv.pdf"},
					{"name": "two", "file_name": "carta.docx"},
				]
			)

	def test_same_vacancy_application_reuses_one_exact_row(self):
		rows = [
			{
				"name": "APP-1",
				"applicant_name": "Ana Pérez",
				"custom_normalized_email": "ana@example.com",
				"custom_cv_sha256": "abc",
			}
		]
		self.assertEqual(
			same_vacancy_application(
				rows, email="ana@example.com", cv_sha256="abc", applicant_name="Ana Pérez"
			),
			"APP-1",
		)

	def test_same_vacancy_application_returns_none_without_match(self):
		self.assertIsNone(
			same_vacancy_application([], email="ana@example.com", cv_sha256="abc", applicant_name="Ana Pérez")
		)

	def test_same_vacancy_application_fails_closed_on_conflict(self):
		rows = [
			{
				"name": "APP-EMAIL",
				"applicant_name": "Ana Pérez",
				"custom_normalized_email": "ana@example.com",
				"custom_cv_sha256": "old",
			},
			{
				"name": "APP-CV",
				"applicant_name": "Ana Pérez",
				"custom_normalized_email": "other@example.com",
				"custom_cv_sha256": "abc",
			},
		]
		with self.assertRaisesRegex(EmailIntakeDomainError, "varias solicitudes"):
			same_vacancy_application(
				rows, email="ana@example.com", cv_sha256="abc", applicant_name="Ana Pérez"
			)

	def test_same_vacancy_application_routes_even_compatible_one_signal_to_review(self):
		rows = [
			{
				"name": "APP-1",
				"applicant_name": "Ana Pérez",
				"custom_normalized_email": "familia@example.com",
				"custom_cv_sha256": "old",
			}
		]
		with self.assertRaisesRegex(EmailIntakeDomainError, "revisión manual"):
			same_vacancy_application(
				rows,
				email="familia@example.com",
				cv_sha256="new",
				applicant_name="Ana Pérez",
			)


if __name__ == "__main__":
	unittest.main()
