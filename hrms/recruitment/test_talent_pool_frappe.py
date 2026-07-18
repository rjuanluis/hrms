from uuid import uuid4

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.tests import UnitTestCase

from hrms.patches.v16_0.create_ayp_candidate_profiles import CANDIDATE_PROFILE_FIELDS
from hrms.recruitment.matching import DEDUPE_REVIEW


class TestTalentPoolLifecycle(UnitTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		create_custom_fields(CANDIDATE_PROFILE_FIELDS, update=True)
		frappe.clear_cache()

	def test_identity_change_keeps_profile_and_marks_review(self):
		token = uuid4().hex
		applicant = frappe.get_doc(
			{
				"doctype": "Job Applicant",
				"applicant_name": "Candidata Integración",
				"email_id": f"candidate-{token}@example.com",
				"status": "Open",
			}
		).insert(ignore_permissions=True)

		profile_name = applicant.custom_candidate_profile
		self.assertTrue(profile_name)

		applicant.email_id = f"candidate-updated-{token}@example.com"
		applicant.save(ignore_permissions=True)

		self.assertEqual(applicant.custom_candidate_profile, profile_name)
		self.assertEqual(applicant.custom_dedupe_status, DEDUPE_REVIEW)
		self.assertEqual(
			frappe.db.get_value("AYP Candidate Profile", profile_name, "dedupe_status"),
			DEDUPE_REVIEW,
		)
