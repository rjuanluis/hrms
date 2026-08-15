import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from hrms.recruitment.talent_pool import backfill_candidate_profiles

CANDIDATE_PROFILE_FIELDS = {
	"File": [
		{
			"fieldname": "custom_cv_sha256",
			"label": "CV SHA-256",
			"fieldtype": "Data",
			"read_only": 1,
			"hidden": 1,
			"insert_after": "content_hash",
		},
	],
	"Job Applicant": [
		{
			"fieldname": "custom_candidate_profile",
			"label": "Perfil canónico del candidato",
			"fieldtype": "Link",
			"options": "AYP Candidate Profile",
			"read_only": 1,
			"insert_after": "resume_attachment",
		},
		{
			"fieldname": "custom_cv_sha256",
			"label": "CV SHA-256",
			"fieldtype": "Data",
			"read_only": 1,
			"hidden": 1,
			"insert_after": "custom_candidate_profile",
		},
		{
			"fieldname": "custom_normalized_email",
			"label": "Correo normalizado",
			"fieldtype": "Data",
			"read_only": 1,
			"hidden": 1,
			"insert_after": "custom_cv_sha256",
		},
		{
			"fieldname": "custom_normalized_phone",
			"label": "Teléfono normalizado",
			"fieldtype": "Data",
			"read_only": 1,
			"hidden": 1,
			"insert_after": "custom_normalized_email",
		},
		{
			"fieldname": "custom_dedupe_status",
			"label": "Estado de deduplicación",
			"fieldtype": "Select",
			"options": "Nuevo\nCoincidencia\nRevisión requerida\nManual",
			"read_only": 1,
			"hidden": 1,
			"insert_after": "custom_normalized_phone",
		},
		{
			"fieldname": "custom_ayp_governed",
			"label": "Gobernado por flujo AyP",
			"fieldtype": "Check",
			"default": "0",
			"read_only": 1,
			"hidden": 1,
			"insert_after": "custom_dedupe_status",
		},
		{
			"fieldname": "custom_ayp_email_provenance",
			"label": "Procedencia inmutable de correo RRHH",
			"fieldtype": "Check",
			"default": "0",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"insert_after": "custom_ayp_governed",
		},
	],
}


def execute():
	if not frappe.db.exists("DocType", "AYP Candidate Profile"):
		frappe.throw(_("AYP Candidate Profile no está disponible después de model sync."))
	create_custom_fields(CANDIDATE_PROFILE_FIELDS, update=True)
	frappe.clear_cache()
	backfill_candidate_profiles()
