from __future__ import annotations

from email.message import EmailMessage
from io import BytesIO
from unittest.mock import patch

from pypdf import PdfWriter

import frappe
from frappe.email.doctype.email_account.email_account import notify_unreplied
from frappe.email.receive import InboundMail
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from hrms.recruitment import email_intake
from hrms.recruitment.email_intake import (
	APPLICANT_SOURCE,
	INTAKE_PENDING,
	INTAKE_STATUS_FIELD,
	disable_existing_recruitment_mailbox_auto_reply,
)
from hrms.recruitment.talent_pool import STATUS_ACTIVE, STATUS_CURRENT_VACANCY_ONLY
from hrms.security.candidate_cv import PRIVACY_NOTICE_VERSION

WEB_SOURCE = "Sitio Web"


class TestRecruitmentEmailIntakeIntegration(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		for source in (APPLICANT_SOURCE, WEB_SOURCE):
			if not frappe.db.exists("Job Applicant Source", source):
				frappe.get_doc({"doctype": "Job Applicant Source", "source_name": source}).insert()

	def _applicant(
		self, *, email: str, source: str, consent: int, privacy_version: str, as_guest: bool = False
	):
		doc = frappe.get_doc(
			{
				"doctype": "Job Applicant",
				"status": "Open",
				"applicant_name": "_Test Email Intake Candidate",
				"email_id": email,
				"source": source,
				"custom_ayp_governed": 1,
				"custom_data_processing_consent": consent,
				"custom_privacy_notice_version": privacy_version,
			}
		)
		if not as_guest:
			return doc.insert(ignore_permissions=True)
		previous_user = frappe.session.user
		try:
			frappe.set_user("Guest")
			return doc.insert(ignore_permissions=True)
		finally:
			frappe.set_user(previous_user)

	def _outbound_counts(self, applicant_name: str) -> tuple[int, int]:
		queue_count = frappe.db.count(
			"Email Queue",
			{"reference_doctype": "Job Applicant", "reference_name": applicant_name},
		)
		communication_count = frappe.db.count(
			"Communication",
			{
				"reference_doctype": "Job Applicant",
				"reference_name": applicant_name,
				"communication_medium": "Email",
				"sent_or_received": "Sent",
			},
		)
		return queue_count, communication_count

	def test_recruitment_email_account_cannot_enable_automatic_mail(self):
		account = frappe.get_doc(
			{
				"doctype": "Email Account",
				"email_account_name": f"_Test Recruitment {frappe.generate_hash(length=8)}",
				"email_id": "empleos@aroypedal.com",
				"enable_incoming": 0,
				"enable_outgoing": 0,
				"enable_auto_reply": 1,
				"notify_if_unreplied": 1,
				"send_notification_to": "owner@example.com",
				"append_to": "Job Applicant",
				"imap_folder": [{"folder_name": "INBOX", "append_to": "Job Applicant"}],
			}
		).insert(ignore_permissions=True)
		self.assertEqual(account.enable_auto_reply, 0)
		self.assertEqual(account.notify_if_unreplied, 0)
		self.assertEqual(account.enable_outgoing, 0)
		self.assertEqual(account.default_outgoing, 0)
		self.assertEqual(account.send_notification_to, "")
		self.assertEqual(account.append_to, "Communication")
		self.assertTrue(all(folder.append_to == "Communication" for folder in account.imap_folder))
		frappe.db.set_value(
			"Email Account",
			account.name,
			{
				"enable_auto_reply": 1,
				"notify_if_unreplied": 1,
				"enable_outgoing": 1,
				"default_outgoing": 1,
				"send_notification_to": "owner@example.com",
				"append_to": "Job Applicant",
				"enable_incoming": 1,
			},
			update_modified=False,
		)
		for folder in account.imap_folder:
			frappe.db.set_value(
				"IMAP Folder", folder.name, "append_to", "Job Applicant", update_modified=False
			)
		self.assertIn(account.name, disable_existing_recruitment_mailbox_auto_reply())
		self.assertEqual(
			frappe.db.get_value(
				"Email Account",
				account.name,
				(
					"enable_auto_reply",
					"notify_if_unreplied",
					"enable_outgoing",
					"default_outgoing",
					"send_notification_to",
					"append_to",
				),
			),
			(0, 0, 0, 0, "", "Communication"),
		)
		self.assertEqual(
			frappe.get_all(
				"IMAP Folder",
				filters={"parent": account.name, "parentfield": "imap_folder"},
				pluck="append_to",
			),
			["Communication"],
		)
		with patch.object(frappe, "sendmail") as sendmail:
			notify_unreplied()
		sendmail.assert_not_called()

	def test_post_commit_enqueue_failure_is_recovered_from_durable_pending(self):
		account_name = f"_Test Recruitment Recovery {frappe.generate_hash(length=8)}"
		communication_name = None
		try:
			account = frappe.get_doc(
				{
					"doctype": "Email Account",
					"email_account_name": account_name,
					"email_id": "empleos@aroypedal.com",
					"enable_incoming": 0,
					"enable_outgoing": 0,
					"enable_auto_reply": 1,
				}
			).insert(ignore_permissions=True)
			communication = frappe.get_doc(
				{
					"doctype": "Communication",
					"communication_type": "Communication",
					"communication_medium": "Email",
					"sent_or_received": "Received",
					"sender": "_Test Recovery <recovery@example.com>",
					"subject": "_Test durable recruitment intake",
					"content": "_Test",
					"status": "Open",
					"email_account": account.name,
					"has_attachment": 1,
				}
			).insert(ignore_permissions=True)
			communication_name = communication.name
			self.assertEqual(communication.get(INTAKE_STATUS_FIELD), INTAKE_PENDING)

			with patch.object(
				email_intake,
				"_enqueue_pending_intake",
				side_effect=RuntimeError("simulated redis outage"),
			):
				# Execute the real callback: Redis failure must not make Frappe
				# duplicate the raw message in Unhandled Email.
				frappe.db.commit()  # nosemgrep
			self.assertEqual(
				frappe.db.get_value("Communication", communication.name, INTAKE_STATUS_FIELD),
				INTAKE_PENDING,
			)

			frappe.db.set_value(
				"Communication",
				communication.name,
				{
					"custom_ayp_email_intake_queued_on": add_to_date(now_datetime(), minutes=-20),
					"creation": add_to_date(now_datetime(), years=-10),
				},
				update_modified=False,
			)
			# Persist the intentionally aged durable intent before reconciliation.
			frappe.db.commit()  # nosemgrep
			with patch.object(email_intake, "_enqueue_pending_intake", return_value=True) as enqueue:
				self.assertEqual(email_intake.recover_stale_recruitment_email_intakes(communication.name), 1)
				# Execute the reconciler's real post-commit enqueue callback.
				frappe.db.commit()  # nosemgrep
			enqueue.assert_any_call(communication.name)
		finally:
			if communication_name:
				frappe.db.delete("Communication", {"name": communication_name})
			frappe.db.delete("Email Account", {"name": account_name})
			# Clean records committed by this post-commit boundary test.
			frappe.db.commit()  # nosemgrep

	def test_email_account_receive_suppresses_thread_reply_without_attachment(self):
		account_name = f"_Test Recruitment Thread {frappe.generate_hash(length=8)}"
		message_id = f"ayp-thread-{frappe.generate_hash(length=12)}@example.com"
		created = []
		try:
			applicant = self._applicant(
				email=f"thread-{frappe.generate_hash(length=8)}@example.com",
				source=APPLICANT_SOURCE,
				consent=0,
				privacy_version="",
			)
			created.append(("Job Applicant", applicant.name))
			parent = frappe.get_doc(
				{
					"doctype": "Communication",
					"communication_type": "Communication",
					"communication_medium": "Email",
					"sent_or_received": "Sent",
					"subject": "Thread parent",
					"content": "Parent",
					"sender": "hr@example.com",
					"recipients": applicant.email_id,
					"message_id": message_id,
					"reference_doctype": "Job Applicant",
					"reference_name": applicant.name,
				}
			).insert(ignore_permissions=True)
			created.append(("Communication", parent.name))
			account = frappe.get_doc(
				{
					"doctype": "Email Account",
					"email_account_name": account_name,
					"email_id": "empleos@aroypedal.com",
					"enable_incoming": 0,
					"enable_outgoing": 0,
					"default_outgoing": 0,
					"append_to": "Job Applicant",
				}
			).insert(ignore_permissions=True)
			created.append(("Email Account", account.name))
			message = EmailMessage()
			message["From"] = "_Test Candidate <candidate@example.com>"
			message["To"] = "empleos@aroypedal.com"
			message["Subject"] = "Re: Thread parent"
			message["Message-ID"] = f"<child-{frappe.generate_hash(length=12)}@example.com>"
			message["In-Reply-To"] = f"<{message_id}>"
			message.set_content("Follow-up without attachment")
			mail = InboundMail(message.as_bytes(), account)
			queue_before = frappe.db.count("Email Queue")
			with (
				patch.object(account, "get_inbound_mails", return_value=[mail]),
				patch.object(frappe, "sendmail") as sendmail,
				patch.object(email_intake, "_enqueue_pending_intake", return_value=True),
			):
				account.receive()
				sendmail.assert_not_called()
			child = frappe.get_doc("Communication", {"message_id": mail.message_id})
			created.append(("Communication", child.name))
			self.assertFalse(child.reference_doctype)
			self.assertFalse(child.reference_name)
			self.assertEqual(frappe.db.count("Email Queue"), queue_before)
			self.assertEqual(account.enable_outgoing, 0)
			self.assertEqual(account.default_outgoing, 0)
		finally:
			for doctype, name in reversed(created):
				frappe.db.delete(doctype, {"name": name})
			frappe.db.commit()  # nosemgrep

	def test_native_inbound_recruitment_mail_creates_no_outbound_email(self):
		account_name = f"_Test Recruitment Native {frappe.generate_hash(length=8)}"
		communication_name = None
		try:
			account = frappe.get_doc(
				{
					"doctype": "Email Account",
					"email_account_name": account_name,
					"email_id": "empleos@aroypedal.com",
					"enable_incoming": 0,
					"enable_outgoing": 0,
					"enable_auto_reply": 1,
					"notify_if_unreplied": 1,
					"send_notification_to": "owner@example.com",
					"append_to": "Job Applicant",
				}
			).insert(ignore_permissions=True)
			message = EmailMessage()
			message["From"] = "_Test Candidate <candidate@example.com>"
			message["To"] = "empleos@aroypedal.com"
			message["Subject"] = "_Test native silent recruitment intake"
			message["Message-ID"] = f"<{frappe.generate_hash(length=20)}@example.com>"
			message.set_content("_Test application")
			pdf = BytesIO()
			writer = PdfWriter()
			writer.add_blank_page(width=72, height=72)
			writer.write(pdf)
			message.add_attachment(
				pdf.getvalue(),
				maintype="application",
				subtype="pdf",
				filename="cv.pdf",
			)
			queue_before = frappe.db.count("Email Queue")
			sent_before = frappe.db.count("Communication", {"sent_or_received": "Sent"})
			with (
				patch.object(email_intake, "_enqueue_pending_intake", return_value=True),
				patch.object(frappe, "sendmail") as sendmail,
			):
				communication = InboundMail(message.as_bytes(), account).process()
				communication_name = communication.name
				frappe.db.commit()  # nosemgrep
				communication.send_email(is_inbound_mail_communcation=True)
				sendmail.assert_not_called()
			self.assertFalse(communication.reference_doctype)
			self.assertFalse(communication.reference_name)
			self.assertEqual(communication.get(INTAKE_STATUS_FIELD), INTAKE_PENDING)
			self.assertEqual(frappe.db.count("Email Queue"), queue_before)
			self.assertEqual(
				frappe.db.count("Communication", {"sent_or_received": "Sent"}),
				sent_before,
			)
		finally:
			if communication_name:
				frappe.db.delete(
					"File",
					{"attached_to_doctype": "Communication", "attached_to_name": communication_name},
				)
				frappe.db.delete("Communication", {"name": communication_name})
			frappe.db.delete("Email Account", {"name": account_name})
			frappe.db.commit()  # nosemgrep

	def test_email_is_silent_web_is_acknowledged_and_profile_activates(self):
		email = f"email-intake-{frappe.generate_hash(length=12)}@example.com"

		email_application = self._applicant(
			email=email,
			source=APPLICANT_SOURCE,
			consent=1,
			privacy_version=PRIVACY_NOTICE_VERSION,
		)
		self.assertEqual(self._outbound_counts(email_application.name), (0, 0))
		profile_name = email_application.custom_candidate_profile
		self.assertTrue(profile_name)
		self.assertEqual(
			frappe.db.get_value("AYP Candidate Profile", profile_name, "talent_pool_status"),
			STATUS_CURRENT_VACANCY_ONLY,
		)

		try:
			with patch.dict(
				frappe.conf,
				{
					"mail_server": "127.0.0.1",
					"mail_login": "_test-no-reply@example.com",
				},
				clear=False,
			):
				# Fresh installs have no default outgoing Email Account. Exercise
				# the official site-config fallback; sendmail only creates Email Queue.
				frappe.local.outgoing_email_account = {}
				web_application = self._applicant(
					email=email,
					source=WEB_SOURCE,
					consent=1,
					privacy_version=PRIVACY_NOTICE_VERSION,
					as_guest=True,
				)
		finally:
			frappe.local.outgoing_email_account = {}
		self.assertEqual(web_application.custom_candidate_profile, profile_name)
		self.assertEqual(
			frappe.db.get_value("AYP Candidate Profile", profile_name, "talent_pool_status"),
			STATUS_ACTIVE,
		)
		self.assertEqual(self._outbound_counts(web_application.name), (1, 1))

	def test_internal_record_without_consent_does_not_activate_email_profile(self):
		email = f"email-intake-no-consent-{frappe.generate_hash(length=12)}@example.com"
		email_application = self._applicant(
			email=email,
			source=APPLICANT_SOURCE,
			consent=0,
			privacy_version="",
		)
		profile_name = email_application.custom_candidate_profile
		internal_application = self._applicant(
			email=email,
			source=WEB_SOURCE,
			consent=0,
			privacy_version=PRIVACY_NOTICE_VERSION,
		)
		self.assertEqual(internal_application.custom_candidate_profile, profile_name)
		self.assertEqual(
			frappe.db.get_value("AYP Candidate Profile", profile_name, "talent_pool_status"),
			STATUS_CURRENT_VACANCY_ONLY,
		)
		self.assertEqual(self._outbound_counts(internal_application.name), (0, 0))
