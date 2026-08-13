import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CANDIDATE_REVIEW_FIELDS = {
	"Job Applicant": [
		{
			"fieldname": "custom_ayp_governed",
			"label": "Gobernado por flujo AyP",
			"fieldtype": "Check",
			"default": 0,
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"insert_after": "status",
		},
		{
			"fieldname": "custom_candidate_score",
			"label": "Puntuación AyP",
			"fieldtype": "Percent",
			"read_only": 1,
			"in_list_view": 1,
			"insert_after": "applicant_rating",
		},
		{
			"fieldname": "custom_candidate_recommendation",
			"label": "Recomendación AyP",
			"fieldtype": "Select",
			"options": "\nRecomendado para shortlist\nRevisión comparativa\nNo priorizar",
			"read_only": 1,
			"insert_after": "custom_candidate_score",
		},
		{
			"fieldname": "custom_candidate_scorecard",
			"label": "Último scorecard AyP",
			"fieldtype": "Link",
			"options": "AYP Candidate Scorecard",
			"read_only": 1,
			"insert_after": "custom_candidate_recommendation",
		},
		{
			"fieldname": "custom_candidate_scored_on",
			"label": "Última evaluación AyP",
			"fieldtype": "Datetime",
			"read_only": 1,
			"insert_after": "custom_candidate_scorecard",
		},
	],
}


def execute():
	create_custom_fields(CANDIDATE_REVIEW_FIELDS, update=True)
	frappe.db.sql(
		"""
		UPDATE `tabJob Applicant`
		SET custom_ayp_governed = 1
		WHERE COALESCE(custom_candidate_profile, '') != ''
		"""
	)
	frappe.clear_cache()
