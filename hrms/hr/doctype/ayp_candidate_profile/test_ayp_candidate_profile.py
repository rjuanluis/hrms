import frappe
from frappe.tests import UnitTestCase


class TestAYPCandidateProfile(UnitTestCase):
	def _new_profile(self):
		profile = frappe.new_doc("AYP Candidate Profile")
		profile.candidate_name = "Persona de prueba"
		profile.talent_pool_status = "Activo"
		return profile

	def test_active_profile_does_not_require_disposition_reason(self):
		profile = self._new_profile()
		profile.validate()

	def test_no_interest_requires_human_reason(self):
		profile = self._new_profile()
		profile.talent_pool_status = "Sin interés"
		with self.assertRaises(frappe.ValidationError):
			profile.validate()

	def test_do_not_contact_requires_human_reason(self):
		profile = self._new_profile()
		profile.do_not_contact = 1
		with self.assertRaises(frappe.ValidationError):
			profile.validate()

	def test_active_profile_cannot_be_deleted(self):
		profile = self._new_profile()
		with self.assertRaises(frappe.ValidationError):
			profile.on_trash()

	def test_disposed_profile_still_requires_privacy_workflow_for_deletion(self):
		profile = self._new_profile()
		profile.talent_pool_status = "Eliminación solicitada"
		profile.disposition_reason = "Solicitud de prueba"
		with self.assertRaises(frappe.ValidationError):
			profile.on_trash()
