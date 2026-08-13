import frappe
from frappe.tests import UnitTestCase

from hrms.recruitment.candidate_profile_governance import candidate_profile_governance_update


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

	def test_ordinary_write_cannot_clear_do_not_contact_or_reactivate_profile(self):
		profile = self._new_profile().insert()

		@candidate_profile_governance_update
		def opt_out():
			profile.do_not_contact = 1
			profile.talent_pool_status = "Sin interés"
			profile.disposition_reason = "Solicitud verificable de no contacto."
			profile.save()

		opt_out()
		profile.do_not_contact = 0
		profile.talent_pool_status = "Activo"
		with self.assertRaises(frappe.PermissionError):
			profile.save()

	def test_audited_governance_context_can_change_privacy_state(self):
		profile = self._new_profile().insert()

		@candidate_profile_governance_update
		def dispose():
			profile.do_not_contact = 1
			profile.talent_pool_status = "Dispuesto"
			profile.disposition_reason = "Disposición autorizada y registrada por endpoint."
			profile.save()

		dispose()
		self.assertEqual(profile.do_not_contact, 1)
		self.assertEqual(profile.talent_pool_status, "Dispuesto")
