from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from deploy import configure_standard as setup  # type: ignore[import-not-found]


class _Document:
	job_title: str
	designation: str
	company: str
	status: str
	publish: int
	job_application_route: str
	use_imap: int
	enable_auto_reply: int
	append_to: str
	imap_folder: list[SimpleNamespace]

	def __init__(self, name=""):
		self.name = name
		self.saved = False
		self.inserted = False
		self.insert_set_name = None

	def update(self, values):
		for fieldname, value in values.items():
			setattr(self, fieldname, value)

	def save(self, **kwargs):
		self.saved = True

	def insert(self, *, set_name=None, **kwargs):
		self.inserted = True
		self.insert_set_name = set_name
		if set_name:
			self.name = set_name
		return self

	def reload(self):
		return self

	def get(self, fieldname):
		return getattr(self, fieldname, None)


class TestNativeRecruitmentSetup(TestCase):
	def test_email_bridge_fields_are_hidden_read_only_no_copy_and_message_id_is_unique(self):
		with patch.object(setup, "create_custom_fields") as create_fields:
			setup.ensure_recruitment_security_fields()

		field_map = create_fields.call_args.args[0]
		fields = {field["fieldname"]: field for field in field_map["Job Applicant"]}
		email_fields = {
			field["fieldname"]: field
			for field in field_map["Job Applicant"]
			if field["fieldname"].startswith("custom_ayp_email_")
		}
		self.assertEqual(
			set(email_fields),
			{
				"custom_ayp_email_message_id",
				"custom_ayp_email_received_on",
				"custom_ayp_email_subject",
				"custom_ayp_email_current_vacancy_consent",
				"custom_ayp_email_consent_notice_version",
				"custom_ayp_email_consent_evidence_sha256",
			},
		)
		for field in email_fields.values():
			self.assertEqual(field["hidden"], 1)
			self.assertEqual(field["read_only"], 1)
			self.assertEqual(field["no_copy"], 1)
		self.assertEqual(email_fields["custom_ayp_email_message_id"]["unique"], 1)
		self.assertEqual(email_fields["custom_ayp_email_subject"]["length"], 140)
		self.assertEqual(email_fields["custom_ayp_email_consent_notice_version"]["length"], 140)
		self.assertEqual(email_fields["custom_ayp_email_consent_evidence_sha256"]["length"], 64)
		self.assertEqual(
			fields["custom_data_processing_consent"]["read_only_depends_on"],
			"eval:doc.source=='Email Recursos Humanos'",
		)

	def test_email_recruitment_source_is_created_idempotently(self):
		source = _Document()
		with (
			patch.object(setup.frappe.db, "exists", side_effect=[False, True]) as exists,
			patch.object(setup.frappe, "get_doc", return_value=source) as get_doc,
		):
			first = setup.ensure_recruitment_email_source()
			second = setup.ensure_recruitment_email_source()

		self.assertEqual(first, setup.EMAIL_RECRUITMENT_SOURCE)
		self.assertEqual(second, setup.EMAIL_RECRUITMENT_SOURCE)
		self.assertTrue(source.inserted)
		get_doc.assert_called_once_with(
			{"doctype": "Job Applicant Source", "source_name": setup.EMAIL_RECRUITMENT_SOURCE}
		)
		self.assertEqual(exists.call_count, 2)

	def test_internal_job_opening_is_created_with_exact_authoritative_name(self):
		designation = _Document()
		opening = _Document("HR-OPN-2026-0001")

		def exists(doctype, name):
			return doctype == "Designation"

		def get_doc(*args):
			if len(args) == 1:
				values = args[0]
				document = designation if values["doctype"] == "Designation" else opening
				document.update(values)
				return document
			raise AssertionError(f"Unexpected get_doc call: {args}")

		with (
			patch.object(setup.frappe.db, "exists", side_effect=exists),
			patch.object(setup.frappe.db, "get_value") as get_opening_name,
			patch.object(setup.frappe, "get_doc", side_effect=get_doc),
		):
			result = setup.ensure_native_recruitment_job_opening()

		get_opening_name.assert_not_called()
		self.assertEqual(result, opening.name)
		self.assertEqual(opening.name, setup.RECRUITMENT_JOB_OPENING)
		self.assertTrue(opening.inserted)
		self.assertIsNone(opening.insert_set_name)
		self.assertEqual(opening.job_title, setup.RECRUITMENT_JOB_TITLE)
		self.assertEqual(opening.designation, setup.RECRUITMENT_JOB_TITLE)
		self.assertEqual(opening.company, setup.COMPANY)
		self.assertEqual(opening.status, "Open")
		self.assertEqual(opening.publish, 0)
		self.assertEqual(opening.job_application_route, setup.RECRUITMENT_WEB_FORM_ROUTE)

	def test_existing_job_opening_preserves_human_lifecycle_state(self):
		opening = _Document("HR-OPN-2026-0001")
		opening.job_title = "Asesor(a) de Venta Online"
		opening.designation = "Asesor/a de Ventas"
		opening.status = "Closed"
		opening.publish = 1

		def exists(doctype, name):
			if (doctype, name) == ("Designation", setup.RECRUITMENT_JOB_TITLE):
				return True
			if (doctype, name) == ("Job Opening", setup.RECRUITMENT_JOB_OPENING):
				return opening.name
			raise AssertionError(f"Unexpected exists call: {(doctype, name)}")

		def get_doc(*args):
			if args == ("Job Opening", setup.RECRUITMENT_JOB_OPENING):
				return opening
			raise AssertionError(f"Unexpected get_doc call: {args}")

		with (
			patch.object(setup.frappe.db, "exists", side_effect=exists) as opening_exists,
			patch.object(setup.frappe.db, "get_value") as get_opening_name,
			patch.object(setup.frappe, "get_doc", side_effect=get_doc) as load_opening,
		):
			setup.ensure_native_recruitment_job_opening()

		opening_exists.assert_any_call("Job Opening", setup.RECRUITMENT_JOB_OPENING)
		load_opening.assert_called_once_with("Job Opening", setup.RECRUITMENT_JOB_OPENING)
		get_opening_name.assert_not_called()
		self.assertEqual(opening.name, setup.RECRUITMENT_JOB_OPENING)
		self.assertEqual(opening.job_title, setup.RECRUITMENT_JOB_TITLE)
		self.assertEqual(opening.designation, setup.RECRUITMENT_JOB_TITLE)
		self.assertEqual(opening.status, "Closed")
		self.assertEqual(opening.publish, 1)
		self.assertEqual(opening.job_application_route, setup.RECRUITMENT_WEB_FORM_ROUTE)
		self.assertTrue(opening.saved)

	def test_legacy_business_key_opening_is_not_used_as_authoritative_vacancy(self):
		legacy_opening = _Document("HR-OPN-2025-0009")
		opening = _Document()

		def exists(doctype, name):
			if (doctype, name) == ("Designation", setup.RECRUITMENT_JOB_TITLE):
				return True
			if (doctype, name) == ("Job Opening", setup.RECRUITMENT_JOB_OPENING):
				return False
			raise AssertionError(f"Unexpected exists call: {(doctype, name)}")

		def get_doc(*args):
			if len(args) == 1 and args[0]["doctype"] == "Job Opening":
				opening.update(args[0])
				return opening
			raise AssertionError(f"Unexpected get_doc call: {args}")

		with (
			patch.object(setup.frappe.db, "exists", side_effect=exists),
			patch.object(setup.frappe.db, "get_value", return_value=legacy_opening.name) as get_opening_name,
			patch.object(setup.frappe, "get_doc", side_effect=get_doc) as load_opening,
		):
			result = setup.ensure_native_recruitment_job_opening()

		get_opening_name.assert_not_called()
		load_opening.assert_called_once()
		self.assertEqual(result, setup.RECRUITMENT_JOB_OPENING)
		self.assertTrue(opening.inserted)
		self.assertEqual(opening.name, setup.RECRUITMENT_JOB_OPENING)
		self.assertEqual(opening.status, "Open")
		self.assertEqual(opening.publish, 0)
		self.assertEqual(opening.job_application_route, setup.RECRUITMENT_WEB_FORM_ROUTE)

	def test_pop_mailbox_appends_natively_without_auto_reply(self):
		account = _Document("Recruitment")
		account.use_imap = 0
		account.enable_auto_reply = 1

		with (
			patch.object(setup.frappe, "get_all", return_value=[account.name]),
			patch.object(setup.frappe, "get_doc", return_value=account),
		):
			result = setup.configure_native_recruitment_mailbox()

		self.assertEqual(result, [account.name])
		self.assertEqual(account.append_to, "Job Applicant")
		self.assertEqual(account.enable_auto_reply, 0)
		self.assertTrue(account.saved)

	def test_imap_mailbox_changes_only_inbox_folder(self):
		account = _Document("Recruitment")
		account.use_imap = 1
		account.enable_auto_reply = 1
		account.append_to = "Issue"
		account.imap_folder = [
			SimpleNamespace(folder_name="INBOX", append_to="Communication"),
			SimpleNamespace(folder_name="Archive", append_to="Communication"),
		]

		with (
			patch.object(setup.frappe, "get_all", return_value=[account.name]),
			patch.object(setup.frappe, "get_doc", return_value=account),
		):
			setup.configure_native_recruitment_mailbox()

		self.assertEqual(account.imap_folder[0].append_to, "Job Applicant")
		self.assertEqual(account.imap_folder[1].append_to, "Communication")
		self.assertEqual(account.append_to, "Job Applicant")
		self.assertEqual(account.enable_auto_reply, 0)

	def test_missing_mailbox_is_safe_during_fresh_install(self):
		with (
			patch.object(setup.frappe, "get_all", return_value=[]),
			patch.object(
				setup.frappe,
				"get_doc",
				side_effect=AssertionError("Missing mailbox must not load a document"),
			) as get_doc,
			patch.object(
				setup.frappe,
				"new_doc",
				side_effect=AssertionError("Missing mailbox must not create a document"),
			) as new_doc,
		):
			self.assertEqual(setup.configure_native_recruitment_mailbox(), [])

		get_doc.assert_not_called()
		new_doc.assert_not_called()

	def test_imap_mailbox_requires_an_inbox_folder(self):
		account = _Document("Recruitment")
		account.use_imap = 1
		account.enable_auto_reply = 1
		account.imap_folder = [SimpleNamespace(folder_name="Archive", append_to="Communication")]

		with (
			patch.object(setup.frappe, "get_all", return_value=[account.name]),
			patch.object(setup.frappe, "get_doc", return_value=account),
			self.assertRaisesRegex(RuntimeError, "no configured IMAP Inbox"),
		):
			setup.configure_native_recruitment_mailbox()
