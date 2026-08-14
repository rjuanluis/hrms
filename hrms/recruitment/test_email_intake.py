from __future__ import annotations

import inspect
import json
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

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
			sent_or_received="Received",
			communication_medium="Email",
			email_account="RECRUITMENT",
			has_attachment=1,
			email_status="",
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
			attached_to_doctype="Communication",
			attached_to_name="COMM-TEST-1",
			attached_to_field="",
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
			patch.object(email_intake, "_locked_candidate_file", return_value=file_doc),
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
			patch.object(email_intake, "_is_recruitment_mailbox_message", return_value=True),
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

	def test_recovery_enqueue_failure_does_not_abort_later_rows(self):
		callbacks = []
		rows = [SimpleNamespace(name="COMM-1"), SimpleNamespace(name="COMM-2")]
		with (
			patch.object(email_intake, "_has_intake_fields", return_value=True),
			patch.object(email_intake.frappe.db, "sql", return_value=rows),
			patch.object(email_intake.frappe.db, "set_value"),
			patch.object(email_intake.frappe.db, "after_commit", FakeCallbackManager(callbacks)),
			patch.object(
				email_intake,
				"_enqueue_pending_intake",
				side_effect=[RuntimeError("redis down"), True],
			) as enqueue,
			patch.object(email_intake.frappe, "logger") as logger,
		):
			self.assertEqual(email_intake.recover_stale_recruitment_email_intakes(), 2)
			callbacks[0]()
		self.assertEqual([record.args[0] for record in enqueue.call_args_list], ["COMM-1", "COMM-2"])
		logger.return_value.error.assert_called_once()

	def test_recruitment_email_account_forces_all_automatic_mail_off(self):
		folder = FakeDocument(folder_name="INBOX", append_to="Job Applicant")
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
		self.assertEqual(account.enable_outgoing, 0)
		self.assertEqual(account.default_outgoing, 0)
		self.assertEqual(account.send_notification_to, "")
		self.assertEqual(account.append_to, "Communication")
		self.assertEqual(folder.append_to, "Communication")
		other = FakeDocument(email_id="ventas@aroypedal.com", enable_auto_reply=1, notify_if_unreplied=1)
		email_intake.enforce_recruitment_email_account_safety(other)
		self.assertEqual(other.enable_auto_reply, 1)
		self.assertEqual(other.notify_if_unreplied, 1)
		unsafe = FakeDocument(
			email_id="empleos@aroypedal.com", imap_folder=[FakeDocument(folder_name="Junk")]
		)
		with self.assertRaises(frappe.ValidationError):
			email_intake.enforce_recruitment_email_account_safety(unsafe)

	def test_spam_and_trash_are_not_recruitment_intakes(self):
		for email_status in ("Spam", "Trash"):
			communication = self._communication()
			communication.sent_or_received = "Received"
			communication.communication_medium = "Email"
			communication.email_account = "RECRUITMENT"
			communication.has_attachment = 1
			communication.email_status = email_status
			with patch.object(
				email_intake.frappe.db, "get_value", return_value="empleos@aroypedal.com"
			) as get_value:
				self.assertFalse(email_intake._is_recruitment_email(communication))
			get_value.assert_called_once_with("Email Account", "RECRUITMENT", "email_id")

	def test_mailbox_guard_removes_thread_reference_without_attachment(self):
		communication = self._communication()
		communication.has_attachment = 0
		communication.custom_ayp_email_intake_status = ""
		communication.reference_doctype = "Job Applicant"
		communication.reference_name = "HR-APP-THREAD"
		with patch.object(email_intake.frappe.db, "get_value", return_value="empleos@aroypedal.com"):
			email_intake.enqueue_recruitment_email_intake(communication)
		self.assertIsNone(communication.reference_doctype)
		self.assertIsNone(communication.reference_name)
		self.assertEqual(communication.custom_ayp_email_intake_status, "")

	def test_candidate_file_query_filters_before_limiting_to_two(self):
		with patch.object(email_intake.frappe.db, "sql", return_value=[]) as sql:
			email_intake._candidate_files("COMM-TEST-1")
		query, parameters = sql.call_args.args[:2]
		self.assertIn("LOWER(file_name)", query)
		self.assertIn("LIMIT 2", query)
		self.assertEqual(parameters, ("COMM-TEST-1", "%.pdf", "%.docx"))

	def test_locked_candidate_file_revalidates_attachment_identity(self):
		file_doc = self._file()
		file_doc.attached_to_name = "COMM-OTHER"
		selected = {
			"name": file_doc.name,
			"file_name": file_doc.file_name,
			"file_url": file_doc.file_url,
			"file_size": file_doc.file_size,
		}
		with (
			patch.object(email_intake, "_candidate_files", return_value=[selected]),
			patch.object(email_intake.frappe, "get_doc", return_value=file_doc),
			self.assertRaisesRegex(email_intake.EmailIntakeDomainError, "cambió durante el lock"),
		):
			email_intake._locked_candidate_file("COMM-TEST-1")

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
			custom_privacy_notice_version="AYP-RH-2026-07-17-v3",
			custom_consent_capture_method="Web Form",
			custom_consent_evidence_id="evidence-1",
			custom_consent_recorded_on="2026-08-14 01:00:00",
			custom_consent_form_route="empleos/solicitud",
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

	def test_notification_patch_syncs_full_artifact_and_recipients(self):
		standard_path = (
			Path(__file__).resolve().parents[2]
			/ "hrms"
			/ "hr"
			/ "notification"
			/ "ayp_candidate_application_received"
			/ "ayp_candidate_application_received.json"
		)
		temporary_path = Path(__file__).with_name("ayp_candidate_application_received.json")
		source = json.loads((standard_path if standard_path.exists() else temporary_path).read_text())
		parent_fields = (
			"attach_print",
			"channel",
			"condition",
			"condition_type",
			"docstatus",
			"document_type",
			"enabled",
			"event",
			"is_standard",
			"message",
			"module",
			"send_system_notification",
			"send_to_all_assignees",
			"subject",
		)
		expected = {fieldname: source.get(fieldname) for fieldname in parent_fields}
		recipients = [
			frappe._dict(
				receiver_by_document_field=row.get("receiver_by_document_field") or "",
				receiver_by_role=row.get("receiver_by_role") or "",
				condition=row.get("condition") or "",
			)
			for row in source["recipients"]
		]
		with (
			patch.object(email_intake_patch.frappe.db, "exists", return_value=True),
			patch.object(email_intake_patch.frappe.db, "set_value") as set_value,
			patch.object(email_intake_patch.frappe.db, "get_value", return_value=frappe._dict(expected)),
			patch.object(
				email_intake_patch.frappe,
				"get_all",
				side_effect=[recipients, recipients],
			),
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
		self.assertEqual(expected["channel"], "Email")
		self.assertEqual(source["recipients"], [{"receiver_by_document_field": "email_id"}])

	def test_valid_later_web_notice_can_activate_profile(self):
		self.assertTrue(
			should_activate_talent_pool_profile(
				STATUS_CURRENT_VACANCY_ONLY,
				"Sitio Web",
				"AYP-RH-2026-07-17-v3",
				1,
				"Web Form",
				"evidence-1",
				"2026-08-14 01:00:00",
				"empleos/solicitud",
			)
		)
		self.assertFalse(
			should_activate_talent_pool_profile(
				STATUS_CURRENT_VACANCY_ONLY,
				"Email Recursos Humanos",
				"",
				0,
				"",
				"",
				None,
				"",
			)
		)
		self.assertFalse(
			should_activate_talent_pool_profile(
				STATUS_CURRENT_VACANCY_ONLY,
				"Importación interna",
				"AYP-RH-2026-07-17-v3",
				1,
				"Internal Import",
				"forged-evidence",
				"2026-08-14 01:00:00",
				"empleos/solicitud",
			)
		)
		self.assertEqual(
			initial_talent_pool_status(
				"Sitio Web",
				"AYP-RH-2026-07-17-v3",
				1,
				"Web Form",
				"evidence-1",
				"2026-08-14 01:00:00",
				"empleos/solicitud",
			),
			STATUS_ACTIVE,
		)
		self.assertEqual(initial_talent_pool_status("Sitio Web"), STATUS_CURRENT_VACANCY_ONLY)

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

	def test_sender_controlled_duplicate_never_auto_links(self):
		communication = self._communication()
		communication.reference_doctype = "Job Applicant"
		communication.reference_name = "HR-APP-UNTRUSTED"
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
				patch.object(email_intake.frappe, "get_doc", side_effect=[communication, file_doc])
			)
			link = stack.enter_context(patch.object(email_intake, "_link_communication"))
			with self.assertRaises(email_intake.EmailIntakeReviewRequired):
				email_intake.process_recruitment_email(communication.name)
		link.assert_not_called()
		self.assertIsNone(communication.reference_doctype)
		self.assertIsNone(communication.reference_name)

	def test_human_review_commits_clean_scan_evidence_without_linking(self):
		with (
			patch.object(
				email_intake,
				"process_recruitment_email",
				side_effect=email_intake.EmailIntakeReviewRequired("review"),
			),
			patch.object(email_intake.frappe.db, "rollback") as rollback,
			patch.object(email_intake.frappe.db, "set_value") as set_value,
			patch.object(email_intake.frappe.db, "commit") as commit,
		):
			result = email_intake.process_recruitment_email_safely("COMM-TEST-1")
		self.assertEqual(result["status"], "review_required")
		rollback.assert_not_called()
		self.assertEqual(
			set_value.call_args.args[2][email_intake.INTAKE_STATUS_FIELD], email_intake.INTAKE_BLOCKED
		)
		commit.assert_called_once()

	def test_authorized_reviewer_can_reject_clean_conflict_durably(self):
		communication = self._communication()
		communication.custom_ayp_email_intake_status = email_intake.INTAKE_BLOCKED
		communication.custom_ayp_email_intake_error_code = "EmailIntakeReviewRequired"
		file_doc = self._file()
		file_doc.custom_av_scan_status = "Clean"
		file_doc.custom_cv_sha256 = "a" * 64
		communication.custom_ayp_email_intake_file = file_doc.name
		communication.custom_ayp_email_intake_cv_sha256 = file_doc.custom_cv_sha256
		communication.check_permission = MagicMock()
		communication.add_comment = MagicMock()
		with (
			patch.object(email_intake.frappe, "only_for"),
			patch.object(email_intake.frappe.db, "sql"),
			patch.object(email_intake, "_is_recruitment_mailbox_message", return_value=True),
			patch.object(email_intake.frappe.db, "exists", return_value=True),
			patch.object(email_intake, "_locked_durable_intake_file", return_value=file_doc),
			patch.object(email_intake, "acquire_candidate_identity_lock"),
			patch.object(email_intake.frappe, "get_doc", return_value=communication),
		):
			result = email_intake.resolve_recruitment_email_review(
				communication.name, "reject", reason="No corresponde a la identidad revisada."
			)
		self.assertEqual(result["status"], "rejected")
		self.assertEqual(communication.custom_ayp_email_intake_error_code, "ReviewRejected")
		communication.check_permission.assert_called_once_with("write")
		communication.add_comment.assert_called_once()

	def test_manual_link_is_audited_and_never_claims_completed_cv_tuple(self):
		communication = self._communication()
		communication.custom_ayp_email_intake_status = email_intake.INTAKE_BLOCKED
		communication.custom_ayp_email_intake_error_code = "EmailIntakeReviewRequired"
		communication.custom_ayp_email_intake_file = "FILE-NEW"
		communication.custom_ayp_email_intake_cv_sha256 = "a" * 64
		communication.check_permission = MagicMock()
		communication.add_comment = MagicMock()
		file_doc = self._file()
		file_doc.custom_av_scan_status = "Clean"
		file_doc.custom_cv_sha256 = "a" * 64
		applicant = FakeDocument(
			name="HR-APP-1",
			job_title="HR-OPN-2026-0001",
			custom_candidate_profile="PROFILE-1",
			check_permission=MagicMock(),
		)
		event = FakeDocument(insert=MagicMock(return_value=None))

		def get_doc(doctype, name=None):
			if doctype == "Communication":
				return communication
			if doctype == "Job Applicant":
				return applicant
			if isinstance(doctype, dict) and doctype.get("doctype") == "AYP Candidate Review Event":
				event.payload = doctype
				return event
			raise AssertionError((doctype, name))

		with (
			patch.object(email_intake.frappe, "only_for"),
			patch.object(email_intake.frappe.db, "sql"),
			patch.object(email_intake, "_is_recruitment_mailbox_message", return_value=True),
			patch.object(email_intake.frappe.db, "exists", return_value=True),
			patch.object(email_intake, "_locked_durable_intake_file", return_value=file_doc),
			patch.object(email_intake, "acquire_candidate_identity_lock"),
			patch.object(email_intake.frappe, "get_doc", side_effect=get_doc),
		):
			result = email_intake.resolve_recruitment_email_review(
				communication.name,
				"link_existing",
				applicant.name,
				reason="Coincidencia validada manualmente para esta vacante.",
			)
		self.assertEqual(result["status"], "linked_for_review")
		self.assertEqual(communication.custom_ayp_email_intake_status, email_intake.INTAKE_BLOCKED)
		self.assertEqual(communication.custom_ayp_email_intake_error_code, "ReviewLinked")
		self.assertEqual(file_doc.attached_to_doctype, "Communication")
		self.assertEqual(file_doc.attached_to_name, communication.name)
		applicant.check_permission.assert_called_once_with("write")
		event.insert.assert_called_once_with(ignore_permissions=True)
		self.assertEqual(event.payload["actor"], email_intake.frappe.session.user)

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
			patch.object(email_intake.frappe, "get_doc", return_value=FakeDocument(**completed)),
			patch.object(email_intake, "_verified_completed_applicant", return_value="HR-APP-1"),
			patch.object(email_intake.frappe.db, "set_value") as set_value,
			patch.object(email_intake.frappe.db, "commit") as commit,
			patch.object(email_intake.frappe, "log_error"),
		):
			result = email_intake.process_recruitment_email_safely("COMM-TEST-1")
		self.assertEqual(result, {"status": "already_processed", "applicant": "HR-APP-1"})
		set_value.assert_not_called()
		commit.assert_not_called()

	def test_completed_readback_requires_exact_clean_file_and_sha(self):
		sha = "a" * 64
		communication = FakeDocument(
			name="COMM-COMPLETE",
			reference_doctype="Job Applicant",
			reference_name="HR-APP-1",
			custom_ayp_email_intake_applicant="HR-APP-1",
			custom_ayp_email_intake_file="FILE-1",
			custom_ayp_email_intake_cv_sha256=sha,
		)
		applicant = FakeDocument(
			name="HR-APP-1",
			resume_attachment="/private/files/cv.pdf",
			custom_cv_sha256=sha,
			custom_candidate_cv_file="FILE-1",
		)
		file_record = FakeDocument(
			name="FILE-1",
			file_url="/private/files/cv.pdf",
			attached_to_doctype="Job Applicant",
			attached_to_name="HR-APP-1",
			attached_to_field="resume_attachment",
			is_private=1,
			custom_av_scan_status="Clean",
			custom_cv_sha256=sha,
		)
		with patch.object(email_intake.frappe.db, "get_value", side_effect=[applicant, file_record]):
			self.assertEqual(email_intake._verified_completed_applicant(communication), "HR-APP-1")
		applicant.custom_cv_sha256 = "b" * 64
		with (
			patch.object(email_intake.frappe.db, "get_value", side_effect=[applicant, file_record]),
			self.assertRaises(email_intake.EmailIntakeDomainError),
		):
			email_intake._verified_completed_applicant(communication)
		applicant.custom_cv_sha256 = sha
		file_record.custom_av_scan_status = "Pending"
		with (
			patch.object(email_intake.frappe.db, "get_value", side_effect=[applicant, file_record]),
			self.assertRaises(email_intake.EmailIntakeDomainError),
		):
			email_intake._verified_completed_applicant(communication)


if __name__ == "__main__":
	unittest.main()
