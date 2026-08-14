from __future__ import annotations

import inspect
import json
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from hrms.recruitment import email_intake
from hrms.recruitment.talent_pool import (
	STATUS_ACTIVE,
	STATUS_CURRENT_VACANCY_ONLY,
	initial_talent_pool_status,
	should_activate_talent_pool_profile,
)


class FakeDocument(SimpleNamespace):
	def get(self, fieldname, default=None):
		return getattr(self, fieldname, default)

	def db_set(self, values, update_modified=False):
		for fieldname, value in values.items():
			setattr(self, fieldname, value)


class TestRecruitmentEmailIntake(unittest.TestCase):
	def _communication(self):
		return FakeDocument(
			name="COMM-TEST-1",
			sender="Demo Candidate <demo@example.com>",
			sender_full_name="Demo Candidate",
			reference_doctype="",
			reference_name="",
		)

	def _file(self):
		return FakeDocument(
			name="FILE-NEW",
			file_name="cv.pdf",
			file_url="/private/files/cv.pdf",
			file_size=100,
			is_private=1,
		)

	def _base_patches(self, communication, file_doc):
		def get_doc(doctype, name=None, **kwargs):
			if doctype == "Communication":
				return communication
			if doctype == "File":
				return file_doc
			raise AssertionError((doctype, name, kwargs))

		return (
			patch.object(email_intake, "_is_recruitment_email", return_value=True),
			patch.object(email_intake, "_candidate_files", return_value=[{"name": file_doc.name, "file_name": file_doc.file_name}]),
			patch.object(email_intake, "scan_stored_candidate_cv", return_value="a" * 64),
			patch.object(email_intake, "acquire_candidate_identity_lock"),
			patch.object(email_intake.frappe, "get_doc", side_effect=get_doc),
			patch.object(email_intake.frappe.db, "sql", return_value=[]),
			patch.object(email_intake.frappe.db, "exists", return_value=True),
	)

	def test_worker_has_no_internal_commit(self):
		source = inspect.getsource(email_intake.process_recruitment_email)
		self.assertNotIn("frappe.db.commit", source)

	def test_hook_enqueues_only_after_commit(self):
		source = inspect.getsource(email_intake.enqueue_recruitment_email_intake)
		self.assertIn("frappe.db.after_commit.add", source)
		self.assertIn("frappe.db.after_rollback.add", source)
		self.assertIn("deduplicate=True", source)
		self.assertIn("process_recruitment_email_safely", source)

	def test_email_profile_is_limited_to_current_vacancy(self):
		self.assertEqual(
			initial_talent_pool_status("Email Recursos Humanos"),
			STATUS_CURRENT_VACANCY_ONLY,
		)

	def test_email_application_does_not_infer_contact_consent(self):
		data = email_intake._new_applicant_data(
			applicant_name="Demo Candidate",
			email="demo@example.com",
			job_opening="HR-OPN-2026-0001",
			resume_attachment="/private/files/cv.pdf",
		)
		self.assertEqual(data["custom_data_processing_consent"], 0)
		self.assertEqual(data["custom_privacy_notice_version"], "")
		self.assertEqual(data["source"], "Email Recursos Humanos")

	def test_application_received_notification_excludes_email_intake(self):
		root = Path(__file__).resolve().parents[2]
		standard_path = (
			root
			/ "hrms"
			/ "hr"
			/ "notification"
			/ "ayp_candidate_application_received"
			/ "ayp_candidate_application_received.json"
		)
		temporary_path = Path(__file__).with_name("ayp_candidate_application_received.json")
		notification_path = standard_path if standard_path.exists() else temporary_path
		notification = json.loads(notification_path.read_text())
		condition = notification["condition"]
		self.assertEqual(notification["condition_type"], "Python")
		web_doc = frappe._dict(
			email_id="demo@example.com",
			custom_data_processing_consent=1,
			source="Sitio Web",
		)
		email_doc = frappe._dict(
			email_id="demo@example.com",
			custom_data_processing_consent=1,
			source="Email Recursos Humanos",
		)
		self.assertTrue(frappe.safe_eval(condition, eval_locals={"doc": web_doc}))
		self.assertFalse(frappe.safe_eval(condition, eval_locals={"doc": email_doc}))
		email_doc.custom_data_processing_consent = 0
		self.assertFalse(frappe.safe_eval(condition, eval_locals={"doc": email_doc}))

	def test_valid_later_web_notice_can_activate_profile(self):
		self.assertTrue(
			should_activate_talent_pool_profile(
				STATUS_CURRENT_VACANCY_ONLY,
				"Sitio Web",
				"ayp-candidates-v1",
			)
		)
		self.assertFalse(
			should_activate_talent_pool_profile(
				STATUS_CURRENT_VACANCY_ONLY,
				"Email Recursos Humanos",
				"",
			)
		)
		self.assertEqual(initial_talent_pool_status("Sitio Web"), STATUS_ACTIVE)

	def test_duplicate_message_links_email_and_keeps_private_evidence(self):
		communication = self._communication()
		file_doc = self._file()
		applicant = FakeDocument(
			name="HR-APP-1",
			custom_cv_sha256="a" * 64,
			resume_attachment="/private/files/original.pdf",
		)
		base = self._base_patches(communication, file_doc)
		with ExitStack() as stack:
			for context in base:
				stack.enter_context(context)
			stack.enter_context(
				patch.object(email_intake, "same_vacancy_application", return_value=applicant.name)
			)
			stack.enter_context(
				patch.object(email_intake.frappe, "get_doc", side_effect=[communication, file_doc, applicant])
			)
			link = stack.enter_context(patch.object(email_intake, "_link_communication"))
			result = email_intake.process_recruitment_email(communication.name)
		self.assertEqual(result["status"], "duplicate_message")
		link.assert_called_once_with(communication, applicant.name)


	def test_updated_cv_preserves_original_source_and_replaces_attachment(self):
		communication = self._communication()
		file_doc = self._file()
		applicant = FakeDocument(
			name="HR-APP-1",
			custom_cv_sha256="b" * 64,
			resume_attachment="/private/files/original.pdf",
			source="Sitio Web",
			save=MagicMock(),
		)
		base = self._base_patches(communication, file_doc)
		with ExitStack() as stack:
			for context in base:
				stack.enter_context(context)
			stack.enter_context(
				patch.object(email_intake, "same_vacancy_application", return_value=applicant.name)
			)
			stack.enter_context(
				patch.object(email_intake.frappe, "get_doc", side_effect=[communication, file_doc, applicant])
			)
			detach = stack.enter_context(patch.object(email_intake, "_detach_for_candidate"))
			assert_link = stack.enter_context(patch.object(email_intake, "_assert_candidate_file_link"))
			stack.enter_context(patch.object(email_intake, "_link_communication"))
			result = email_intake.process_recruitment_email(communication.name)
		self.assertEqual(result["status"], "updated")
		self.assertEqual(applicant.resume_attachment, file_doc.file_url)
		self.assertEqual(applicant.source, "Sitio Web")
		detach.assert_called_once_with(file_doc)
		applicant.save.assert_called_once_with(ignore_permissions=True)
		assert_link.assert_called_once_with(file_doc.name, applicant.name)



if __name__ == "__main__":
	unittest.main()
