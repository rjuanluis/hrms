from uuid import uuid4

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.tests import UnitTestCase

from hrms.patches.v16_0.create_ayp_candidate_profiles import CANDIDATE_PROFILE_FIELDS
from hrms.recruitment.matching import (
	DEDUPE_REVIEW,
	EMAIL_RECRUITMENT_SOURCE,
	normalize_email,
	normalize_phone,
)

EMAIL_CONSENT_FIELD = {
	"Job Applicant": [
		{
			"fieldname": "custom_data_processing_consent",
			"label": "Consentimiento para tratamiento de datos",
			"fieldtype": "Check",
			"default": "0",
			"insert_after": "upper_range",
		}
	]
}


class TestTalentPoolLifecycle(UnitTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		create_custom_fields(CANDIDATE_PROFILE_FIELDS, update=True)
		create_custom_fields(EMAIL_CONSENT_FIELD, update=True)
		if not frappe.db.exists("Job Applicant Source", EMAIL_RECRUITMENT_SOURCE):
			frappe.get_doc(
				{"doctype": "Job Applicant Source", "source_name": EMAIL_RECRUITMENT_SOURCE}
			).insert(ignore_permissions=True)
		frappe.clear_cache()

	def test_email_applicant_without_consent_is_normalized_without_profile(self):
		token = uuid4().hex
		email = f"Email-Candidate-{token}@Example.com"
		phone = "(809) 555-0123"
		supplied_profile = frappe.get_doc(
			{
				"doctype": "AYP Candidate Profile",
				"candidate_name": "Perfil suministrado",
				"talent_pool_status": "Activo",
			}
		).insert(ignore_permissions=True)
		profile_count = frappe.db.count("AYP Candidate Profile")

		applicant = frappe.get_doc(
			{
				"doctype": "Job Applicant",
				"applicant_name": "Candidata por Email",
				"email_id": email,
				"phone_number": phone,
				"status": "Open",
				"source": EMAIL_RECRUITMENT_SOURCE,
				"custom_data_processing_consent": 0,
				"custom_candidate_profile": supplied_profile.name,
				"custom_ayp_governed": 1,
			}
		).insert(ignore_permissions=True)

		self.assertEqual(applicant.custom_normalized_email, normalize_email(email))
		self.assertEqual(applicant.custom_normalized_phone, normalize_phone(phone))
		self.assertFalse(applicant.custom_candidate_profile)
		self.assertEqual(frappe.db.count("AYP Candidate Profile"), profile_count)

	def test_email_applicant_with_manufactured_future_consent_is_rejected(self):
		token = uuid4().hex
		profile_count = frappe.db.count("AYP Candidate Profile")
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Job Applicant",
					"applicant_name": "Candidata con consentimiento fabricado",
					"email_id": f"email-manufactured-{token}@example.com",
					"status": "Open",
					"source": EMAIL_RECRUITMENT_SOURCE,
					"custom_data_processing_consent": 1,
					"custom_ayp_governed": 1,
				}
			).insert(ignore_permissions=True)
		self.assertEqual(frappe.db.count("AYP Candidate Profile"), profile_count)

	def test_email_source_and_future_consent_cannot_be_mutated_after_insert(self):
		token = uuid4().hex
		profile_count = frappe.db.count("AYP Candidate Profile")
		applicant = frappe.get_doc(
			{
				"doctype": "Job Applicant",
				"applicant_name": "Candidata con procedencia inmutable",
				"email_id": f"email-immutable-{token}@example.com",
				"status": "Open",
				"source": EMAIL_RECRUITMENT_SOURCE,
				"custom_data_processing_consent": 0,
				"custom_ayp_governed": 1,
			}
		).insert(ignore_permissions=True)

		applicant.source = "Referral"
		applicant.custom_data_processing_consent = 1
		with self.assertRaises(frappe.ValidationError):
			applicant.save(ignore_permissions=True)
		self.assertEqual(frappe.db.count("AYP Candidate Profile"), profile_count)

	def test_identity_change_keeps_profile_and_marks_review(self):
		token = uuid4().hex
		applicant = frappe.get_doc(
			{
				"doctype": "Job Applicant",
				"applicant_name": "Candidata Integración",
				"email_id": f"candidate-{token}@example.com",
				"status": "Open",
				"custom_data_processing_consent": 1,
				"custom_ayp_governed": 1,
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
