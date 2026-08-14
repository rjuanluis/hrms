from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe

from hrms.recruitment import web_form_intake


class TestRecruitmentWebFormIntake(unittest.TestCase):
	def setUp(self):
		frappe.flags.pop("ayp_authoritative_recruitment_web_form", None)

	def test_exact_official_form_sets_request_local_context_only_while_delegating(self):
		form = frappe._dict(
			name="AYP Recruitment Application",
			route="empleos/solicitud",
			doc_type="Job Applicant",
			published=1,
			login_required=0,
			anonymous=1,
			allow_edit=0,
			allow_delete=0,
		)
		observed = []

		def original_accept(**kwargs):
			observed.append(web_form_intake.authoritative_recruitment_web_form_context().copy())
			return frappe._dict(name="HR-APP-1", **kwargs)

		with (
			patch.object(web_form_intake.frappe, "get_all", return_value=[form]),
			patch.object(web_form_intake, "_frappe_web_form_accept", side_effect=original_accept),
		):
			result = web_form_intake.accept(form.name, "{}")
		self.assertEqual(result.name, "HR-APP-1")
		self.assertEqual(observed[0].route, "empleos/solicitud")
		self.assertEqual(observed[0].source, "Sitio Web")
		self.assertIsNone(web_form_intake.authoritative_recruitment_web_form_context())

	def test_lookalike_form_never_receives_authoritative_context(self):
		observed = []

		def original_accept(**kwargs):
			observed.append(web_form_intake.authoritative_recruitment_web_form_context())
			return kwargs

		with (
			patch.object(web_form_intake.frappe, "get_all", return_value=[]),
			patch.object(web_form_intake, "_frappe_web_form_accept", side_effect=original_accept),
		):
			web_form_intake.accept("Lookalike", "{}")
		self.assertEqual(observed, [None])

	def test_official_route_fails_closed_when_form_configuration_is_unsafe(self):
		form = frappe._dict(
			name="AYP Recruitment Application",
			route="empleos/solicitud",
			doc_type="Job Applicant",
			published=1,
			login_required=0,
			anonymous=0,
			allow_edit=0,
			allow_delete=0,
		)
		with (
			patch.object(web_form_intake.frappe, "get_all", return_value=[form]),
			self.assertRaises(frappe.ValidationError),
		):
			web_form_intake.accept(form.name, "{}")


if __name__ == "__main__":
	unittest.main()
