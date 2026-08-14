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
	_save_profile_from_application,
	initial_talent_pool_status,
	should_activate_talent_pool_profile,
)


class FakeDocument(SimpleNamespace):
	def get(self, fieldname, default=None):
		return getattr(self, fieldname, default)

	def db_set(self, values, update_modified=False):
		for fieldname, value in values.items():
			setattr(self, fieldname, value)


class FakeCallbackManager:
	def __init__(self, callbacks):
		self.callbacks = callbacks

	def add(self, callback):
		self.callbacks.append(callback)


class TestRecruitmentEmailIntake(unittest.TestCase):
	def _communication(self):
		return FakeDocument(
			name="COMM-TEST-1",
			sender="Demo Candidate <demo@example.com>",
			sender_full_name="Demo Candidate",
			reference_doctype="",
			reference_name="",
			custom_ayp_email_intake_status=email_intake.INTAKE_PENDING,
			custom_ayp_email_intake_claim="",
			custom_ayp_email_intake_applicant="",
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
			patch.object(
				email_intake,
				"_candidate_files",
				return_value=[{"name": file_doc.name, "file_name": file_doc.file_name}],
			),
			patch.object(email_intake, "scan_stored_candidate_cv", return_value="a" * 64),
			patch.object(email_intake, "acquire_candidate_identity_lock"),
			patch.object(email_intake.frappe, "get_doc", side_effect=get_doc),
			patch.object(email_intake.frappe.db, "sql", return_value=[]),
			patch.object(email_intake.frappe.db, "exists", return_value=True),
		)

	def test_worker_has_no_internal_commit(self):
		source = inspect.getsource(email_intake.process_recruitment_email)
		self.assertNotIn("frappe.db.commit", source)

	def test_hook_persists_pending_before_enqueue_and_survives_enqueue_failure(self):
		communication = self._communication()
		communication.custom_ayp_email_intake_status = ""
		callbacks = []
		rollback_callbacks = []
		with (
			patch.object(email_intake, "_is_recruitment_email", return_value=True),
			patch.object(email_intake, "_has_intake_fields", return_value=True),
			patch.object(email_intake.frappe.db, "after_commit", FakeCallbackManager(callbacks)),
			patch.object(email_intake.frappe.db, "after_rollback", FakeCallbackManager(rollback_callbacks)),
			patch.object(email_intake, "_enqueue_pending_intake", side_effect=RuntimeError("redis down")),
		):
			email_intake.frappe.local.ayp_email_intake_callbacks = set()
			email_intake.enqueue_recruitment_email_intake(communication)
			self.assertEqual(communication.custom_ayp_email_intake_status, email_intake.INTAKE_PENDING)
			self.assertEqual(len(callbacks), 1)
			with self.assertRaisesRegex(RuntimeError, "redis down"):
				callbacks[0]()
		self.assertEqual(communication.custom_ayp_email_intake_status, email_intake.INTAKE_PENDING)
		self.assertEqual(len(rollback_callbacks), 1)

	def test_pending_enqueue_rechecks_committed_authoritative_state(self):
		with (
			patch.object(email_intake, "_has_intake_fields", return_value=True),
			patch.object(
				email_intake.frappe.db,
				"get_value",
				return_value=email_intake.INTAKE_PENDING,
			),
			patch.object(email_intake.frappe, "enqueue") as enqueue,
		):
			self.assertTrue(email_intake._enqueue_pending_intake("COMM-TEST-1"))
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["job_id"], "ayp-email-intake:COMM-TEST-1")

	def test_recovery_resets_stale_claim_and_registers_after_commit_enqueue(self):
		callbacks = []
		row = SimpleNamespace(name="COMM-STALE-1")
		with (
			patch.object(email_intake, "_has_intake_fields", return_value=True),
			patch.object(email_intake.frappe.db, "sql", return_value=[row]),
			patch.object(email_intake.frappe.db, "set_value") as set_value,
			patch.object(email_intake.frappe.db, "after_commit", FakeCallbackManager(callbacks)),
			patch.object(email_intake, "_enqueue_pending_intake", return_value=True) as enqueue,
		):
			self.assertEqual(email_intake.recover_stale_recruitment_email_intakes(), 1)
			callbacks[0]()
		values = set_value.call_args.args[2]
		self.assertEqual(values[email_intake.INTAKE_STATUS_FIELD], email_intake.INTAKE_PENDING)
		self.assertEqual(values["custom_ayp_email_intake_claim"], "")
		enqueue.assert_called_once_with(row.name)

	def test_recruitment_email_account_forces_auto_reply_off(self):
		account = FakeDocument(email_id="empleos@aroypedal.com", enable_auto_reply=1)
		email_intake.enforce_recruitment_email_account_safety(account)
		self.assertEqual(account.enable_auto_reply, 0)
		other = FakeDocument(email_id="ventas@aroypedal.com", enable_auto_reply=1)
		email_intake.enforce_recruitment_email_account_safety(other)
		self.assertEqual(other.enable_auto_reply, 1)

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
				1,
			)
		)
		self.assertFalse(
			should_activate_talent_pool_profile(
				STATUS_CURRENT_VACANCY_ONLY,
				"Email Recursos Humanos",
				"",
				0,
			)
		)
		self.assertFalse(
			should_activate_talent_pool_profile(
				STATUS_CURRENT_VACANCY_ONLY,
				"Importación interna",
				"ayp-candidates-v1",
				0,
			)
		)
		self.assertEqual(initial_talent_pool_status("Sitio Web"), STATUS_ACTIVE)

	def test_profile_application_save_runs_inside_governance_context(self):
		observed = []
		profile = FakeDocument(
			save=lambda **kwargs: observed.append(
				(email_intake.frappe.flags.get("ayp_candidate_profile_governance_update"), kwargs)
			)
		)
		previous = email_intake.frappe.flags.get("ayp_candidate_profile_governance_update")
		_save_profile_from_application(profile)
		self.assertEqual(observed, [(True, {"ignore_permissions": True})])
		self.assertEqual(email_intake.frappe.flags.get("ayp_candidate_profile_governance_update"), previous)

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
		self.assertEqual(communication.custom_ayp_email_intake_status, email_intake.INTAKE_COMPLETED)
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
		self.assertEqual(communication.custom_ayp_email_intake_status, email_intake.INTAKE_COMPLETED)
		self.assertEqual(applicant.resume_attachment, file_doc.file_url)
		self.assertEqual(applicant.source, "Sitio Web")
		detach.assert_called_once_with(file_doc)
		applicant.save.assert_called_once_with(ignore_permissions=True)
		assert_link.assert_called_once_with(file_doc.name, applicant.name)


if __name__ == "__main__":
	unittest.main()
