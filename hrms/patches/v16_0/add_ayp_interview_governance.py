import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

INTERVIEW_GOVERNANCE_FIELDS = {
	"Job Applicant": [
		{
			"fieldname": "custom_ayp_final_interview",
			"label": "Entrevista final AyP",
			"fieldtype": "Link",
			"options": "Interview",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"insert_after": "custom_ayp_governed",
		},
	],
	"Interview Type": [
		{
			"fieldname": "custom_ayp_structured_questions",
			"label": "Preguntas estructuradas AyP",
			"fieldtype": "Long Text",
			"insert_after": "description",
		},
	],
	"Interview": [
		{
			"fieldname": "custom_ayp_kit_section",
			"label": "Kit de entrevista AyP",
			"fieldtype": "Section Break",
			"insert_after": "interview_details",
			"collapsible": 1,
		},
		{
			"fieldname": "custom_ayp_interview_kit",
			"label": "Guía estructurada",
			"fieldtype": "HTML",
			"insert_after": "custom_ayp_kit_section",
		},
		{
			"fieldname": "custom_ayp_questions_snapshot",
			"label": "Snapshot de preguntas AyP",
			"fieldtype": "Long Text",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"insert_after": "custom_ayp_interview_kit",
		},
		{
			"fieldname": "custom_ayp_decision_section",
			"label": "Decisión humana",
			"fieldtype": "Section Break",
			"insert_after": "interview_summary",
		},
		{
			"fieldname": "custom_ayp_decision_rationale",
			"label": "Justificación de decisión",
			"fieldtype": "Small Text",
			"mandatory_depends_on": "eval:!!doc.custom_ayp_questions_snapshot&&(doc.status=='Cleared'||doc.status=='Rejected')",
			"insert_after": "custom_ayp_decision_section",
		},
		{
			"fieldname": "custom_ayp_decided_by",
			"label": "Decidido por",
			"fieldtype": "Link",
			"options": "User",
			"read_only": 1,
			"no_copy": 1,
			"insert_after": "custom_ayp_decision_rationale",
		},
		{
			"fieldname": "custom_ayp_decided_on",
			"label": "Decidido el",
			"fieldtype": "Datetime",
			"read_only": 1,
			"no_copy": 1,
			"insert_after": "custom_ayp_decided_by",
		},
	],
	"Interview Feedback": [
		{
			"fieldname": "custom_ayp_question_evidence",
			"label": "Evidencia por pregunta AyP",
			"description": "Una línea de evidencia observable por cada pregunta estructurada, en el mismo orden.",
			"fieldtype": "Long Text",
			"insert_after": "feedback",
		},
	],
}


def execute():
	create_custom_fields(INTERVIEW_GOVERNANCE_FIELDS, update=True)
	frappe.db.sql(
		"""
		UPDATE `tabJob Applicant` applicant
		SET custom_ayp_final_interview = (
			SELECT interview.name
			FROM `tabInterview` interview
			WHERE interview.job_applicant = applicant.name
				AND interview.docstatus = 1
				AND (
					(applicant.status = 'Accepted' AND interview.status = 'Cleared')
					OR (applicant.status = 'Rejected' AND interview.status = 'Rejected')
				)
				AND COALESCE(interview.custom_ayp_questions_snapshot, '') != ''
			ORDER BY interview.modified DESC, interview.name DESC
			LIMIT 1
		)
		WHERE applicant.custom_ayp_governed = 1
			AND COALESCE(applicant.custom_ayp_final_interview, '') = ''
		"""
	)
	frappe.clear_cache()
