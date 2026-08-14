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
	def test_internal_job_opening_uses_native_autonaming(self):
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
			patch.object(setup.frappe.db, "get_value", return_value=None) as get_opening_name,
			patch.object(setup.frappe, "get_doc", side_effect=get_doc),
		):
			result = setup.ensure_native_recruitment_job_opening()

		get_opening_name.assert_called_once_with(
			"Job Opening",
			{"job_title": setup.RECRUITMENT_JOB_TITLE, "company": setup.COMPANY},
			"name",
		)
		self.assertEqual(result, opening.name)
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
		opening.status = "Closed"
		opening.publish = 1

		with (
			patch.object(setup.frappe.db, "exists", return_value=True),
			patch.object(setup.frappe.db, "get_value", return_value=opening.name),
			patch.object(setup.frappe, "get_doc", return_value=opening),
		):
			setup.ensure_native_recruitment_job_opening()

		self.assertEqual(opening.status, "Closed")
		self.assertEqual(opening.publish, 1)
		self.assertEqual(opening.job_application_route, setup.RECRUITMENT_WEB_FORM_ROUTE)
		self.assertTrue(opening.saved)

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
		with patch.object(setup.frappe, "get_all", return_value=[]):
			self.assertEqual(setup.configure_native_recruitment_mailbox(), [])

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
