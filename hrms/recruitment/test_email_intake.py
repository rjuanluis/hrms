from __future__ import annotations

import inspect
import json
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from hrms.patches.v16_0 import add_ayp_recruitment_email_intake as email_intake_patch
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
		communication.reference_doctype = "Job Applicant"
		communication.reference_name = "HR-APP-UNTRUSTED"
		communication.custom_ayp_email_intake_status = ""
		callbacks = []
		rollback_callbacks = []
		with (
			patch.object(email_intake, "_is_recruitment_email", return_value=True),
			patch.object(email_intake, "_has_intake_fields", return_value=True),
			patch.object(email_intake.frappe.db, "after_commit", FakeCallbackManager(callbacks)),
			patch.object(email_intake.frappe.db, "after_rollback", FakeCallbackManager(rollback_callbacks)),
			patch.object(email_intake, "_enqueue_pending_intake", side_effect=RuntimeError("redis down")),
			patch.object(email_intake.frappe, "logger") as logger,
		):
			email_intake.frappe.local.ayp_email_intake_callbacks = set()
			email_intake.enqueue_recruitment_email_intake(communication)
			self.assertEqual(communication.custom_ayp_email_intake_status, email_intake.INTAKE_PENDING)
			self.assertIsNone(communication.reference_doctype)
			self.assertIsNone(communication.reference_name)
			self.assertEqual(len(callbacks), 1)
			callbacks[0]()
		self.assertEqual(communication.custom_ayp_email_intake_status, email_intake.INTAKE_PENDING)
		self.assertEqual(len(rollback_callbacks), 1)
		logger.return_value.error.assert_called_once_with(
			"Recruitment intake enqueue failed; durable Pending will be reconciled."
		)

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

	def test_recruitment_email_account_forces_all_automatic_mail_off(self):
		folder = FakeDocument(append_to="Job Applicant")
		account = FakeDocument(
			email_id="empleos@aroypedal.com",
			enable_auto_reply=1,
			notify_if_unreplied=1,
			send_notification_to="owner@example.com",
			append_to="Job Applicant",
			imap_folder=[folder],
		)
		email_intake.enforce_recruitment_email_account_safety(account)
		self.assertEqual(account.enable_auto_reply, 0)
		self.assertEqual(account.notify_if_unreplied, 0)
		self.assertEqual(account.send_notification_to, "")
		self.assertEqual(account.append_to, "Communication")
		self.assertEqual(folder.append_to, "Communication")
		other = FakeDocument(email_id="ventas@aroypedal.com", enable_auto_reply=1, notify_if_unreplied=1)
		email_intake.enforce_recruitment_email_account_safety(other)
		self.assertEqual(other.enable_auto_reply, 1)
		self.assertEqual(other.notify_if_unreplied, 1)

	def test_spam_and_trash_are_not_recruitment_intakes(self):
		for email_status in ("Spam", "Trash"):
			communication = self._communication()
			communication.sent_or_received = "Received"
			communication.communication_medium = "Email"
			communication.email_account = "RECRUITMENT"
			communication.has_attachment = 1
			communication.email_status = email_status
			with patch.object(email_intake.frappe.db, "get_value") as get_value:
				self.assertFalse(email_intake._is_recruitment_email(communication))
			get_value.assert_not_called()

	def test_candidate_file_query_filters_before_limiting_to_two(self):
		with patch.object(email_intake.frappe.db, "sql", return_value=[]) as sql:
			email_intake._candidate_files("COMM-TEST-1")
		query, parameters = sql.call_args.args[:2]
		self.assertIn("LOWER(file_name)", query)
		self.assertIn("LIMIT 2", query)
		self.assertEqual(parameters, ("COMM-TEST-1", "%.pdf", "%.docx"))

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

	def test_notification_patch_uses_db_update_and_exact_readback(self):
		expected = {
			"enabled": 1,
			"document_type": "Job Applicant",
			"event": "New",
			"condition_type": "Python",
			"condition": email_intake_patch.NOTIFICATION_CONDITION,
		}
		with (
			patch.object(email_intake_patch.frappe.db, "exists", return_value=True),
			patch.object(email_intake_patch.frappe.db, "set_value") as set_value,
			patch.object(email_intake_patch.frappe.db, "get_value", return_value=expected),
			patch.object(email_intake_patch, "clear_notification_cache") as clear_cache,
		):
			email_intake_patch._sync_application_received_notification()
		set_value.assert_called_once_with(
			"Notification",
			email_intake_patch.NOTIFICATION_NAME,
			expected,
			update_modified=False,
		)
		clear_cache.assert_called_once_with()

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

	def test_email_identity_alone_cannot_replace_existing_cv(self):
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
				patch.object(
					email_intake,
					"same_vacancy_application",
					side_effect=email_intake.EmailIntakeDomainError("revisión manual"),
				)
			)
			stack.enter_context(
				patch.object(email_intake.frappe, "get_doc", side_effect=[communication, file_doc])
			)
			detach = stack.enter_context(patch.object(email_intake, "_detach_for_candidate"))
			with self.assertRaisesRegex(email_intake.EmailIntakeDomainError, "revisión manual"):
				email_intake.process_recruitment_email(communication.name)
		self.assertEqual(applicant.resume_attachment, "/private/files/original.pdf")
		self.assertEqual(applicant.source, "Sitio Web")
		detach.assert_not_called()
		applicant.save.assert_not_called()

	def test_antivirus_infrastructure_failure_remains_retryable(self):
		with (
			patch.object(
				email_intake,
				"process_recruitment_email",
				side_effect=email_intake.CandidateCVInfrastructureError("clamav down"),
			),
			patch.object(email_intake.frappe.db, "rollback") as rollback,
			patch.object(email_intake.frappe.db, "set_value") as set_value,
			patch.object(email_intake.frappe.db, "commit") as commit,
		):
			with self.assertRaises(email_intake.CandidateCVInfrastructureError):
				email_intake.process_recruitment_email_safely("COMM-TEST-1")
		rollback.assert_called_once()
		set_value.assert_not_called()
		commit.assert_not_called()

	def test_late_terminal_worker_cannot_degrade_completed_intake(self):
		completed = {
			email_intake.INTAKE_STATUS_FIELD: email_intake.INTAKE_COMPLETED,
			"custom_ayp_email_intake_applicant": "HR-APP-1",
			"custom_ayp_email_intake_completed_on": "2026-08-13 22:00:00",
			"reference_doctype": "Job Applicant",
			"reference_name": "HR-APP-1",
		}
		with (
			patch.object(
				email_intake,
				"process_recruitment_email",
				side_effect=email_intake.CandidateCVSecurityError("bad cv"),
			),
			patch.object(email_intake.frappe.db, "rollback"),
			patch.object(email_intake, "_has_intake_fields", return_value=True),
			patch.object(email_intake.frappe.db, "exists", return_value=True),
			patch.object(email_intake.frappe.db, "sql"),
			patch.object(email_intake.frappe.db, "get_value", return_value=completed),
			patch.object(email_intake.frappe.db, "set_value") as set_value,
			patch.object(email_intake.frappe.db, "commit") as commit,
			patch.object(email_intake.frappe, "log_error"),
		):
			result = email_intake.process_recruitment_email_safely("COMM-TEST-1")
		self.assertEqual(result, {"status": "already_processed", "applicant": "HR-APP-1"})
		set_value.assert_not_called()
		commit.assert_not_called()


if __name__ == "__main__":
	unittest.main()
