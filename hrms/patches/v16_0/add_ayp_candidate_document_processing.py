import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from hrms.recruitment.candidate_document_service import PROCESSING_STATUSES, PROCESSOR_VERSION

CANDIDATE_DOCUMENT_FIELDS = {
	"Job Applicant": [
		{
			"fieldname": "custom_cv_processing_status",
			"label": "Estado documental del CV",
			"fieldtype": "Select",
			"options": "\n".join(PROCESSING_STATUSES),
			"read_only": 1,
			"in_list_view": 1,
			"insert_after": "resume_attachment",
		},
		{
			"fieldname": "custom_cv_processing_method",
			"label": "Método de extracción del CV",
			"fieldtype": "Data",
			"read_only": 1,
			"insert_after": "custom_cv_processing_status",
		},
		{
			"fieldname": "custom_cv_processing_detail",
			"label": "Detalle documental del CV",
			"fieldtype": "Small Text",
			"read_only": 1,
			"insert_after": "custom_cv_processing_method",
		},
		{
			"fieldname": "custom_cv_extracted_text",
			"label": "Texto extraído del CV",
			"fieldtype": "Long Text",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"insert_after": "custom_cv_processing_detail",
		},
		{
			"fieldname": "custom_cv_processed_sha256",
			"label": "SHA-256 del CV procesado",
			"fieldtype": "Data",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"insert_after": "custom_cv_extracted_text",
		},
		{
			"fieldname": "custom_cv_text_sha256",
			"label": "SHA-256 del texto extraído",
			"fieldtype": "Data",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"insert_after": "custom_cv_processed_sha256",
		},
		{
			"fieldname": "custom_cv_page_count",
			"label": "Páginas procesadas del CV",
			"fieldtype": "Int",
			"read_only": 1,
			"hidden": 1,
			"insert_after": "custom_cv_text_sha256",
		},
		{
			"fieldname": "custom_cv_processing_queued_on",
			"label": "Procesamiento de CV encolado el",
			"fieldtype": "Datetime",
			"read_only": 1,
			"hidden": 1,
			"insert_after": "custom_cv_page_count",
		},
		{
			"fieldname": "custom_cv_processing_started_on",
			"label": "Procesamiento de CV iniciado el",
			"fieldtype": "Datetime",
			"read_only": 1,
			"hidden": 1,
			"insert_after": "custom_cv_processing_queued_on",
		},
		{
			"fieldname": "custom_cv_processing_claim",
			"label": "Claim de procesamiento del CV",
			"fieldtype": "Data",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"insert_after": "custom_cv_processing_started_on",
		},
		{
			"fieldname": "custom_cv_processed_on",
			"label": "CV procesado el",
			"fieldtype": "Datetime",
			"read_only": 1,
			"insert_after": "custom_cv_processing_claim",
		},
		{
			"fieldname": "custom_cv_processor_version",
			"label": "Versión del procesador de CV",
			"fieldtype": "Data",
			"read_only": 1,
			"hidden": 1,
			"insert_after": "custom_cv_processed_on",
		},
		{
			"fieldname": "custom_cv_manual_verified_by",
			"label": "CV verificado manualmente por",
			"fieldtype": "Link",
			"options": "User",
			"read_only": 1,
			"insert_after": "custom_cv_processor_version",
		},
		{
			"fieldname": "custom_cv_manual_verified_on",
			"label": "CV verificado manualmente el",
			"fieldtype": "Datetime",
			"read_only": 1,
			"insert_after": "custom_cv_manual_verified_by",
		},
		{
			"fieldname": "custom_cv_manual_verification_reason",
			"label": "Motivo de verificación manual del CV",
			"fieldtype": "Small Text",
			"read_only": 1,
			"insert_after": "custom_cv_manual_verified_on",
		},
	],
}


def execute():
	create_custom_fields(CANDIDATE_DOCUMENT_FIELDS, update=True)
	frappe.db.sql(
		"""
		UPDATE `tabJob Applicant`
		SET custom_cv_processing_status = CASE
				WHEN COALESCE(resume_attachment, '') = '' THEN 'Sin CV'
				WHEN COALESCE(custom_cv_sha256, '') != '' THEN 'Pendiente'
				ELSE 'Error de seguridad'
			END,
			custom_cv_processing_detail = CASE
				WHEN COALESCE(resume_attachment, '') = '' THEN 'La solicitud histórica no incluye un CV adjunto.'
				WHEN COALESCE(custom_cv_sha256, '') != '' THEN 'CV histórico ligado a una huella segura; extracción pendiente.'
				ELSE 'El CV histórico no conserva una huella de integridad verificada; debe cargarse nuevamente.'
			END,
			custom_cv_processing_queued_on = CASE
				WHEN COALESCE(resume_attachment, '') != '' AND COALESCE(custom_cv_sha256, '') != ''
					THEN COALESCE(modified, creation)
				ELSE NULL
			END,
			custom_cv_processor_version = %s
		WHERE COALESCE(custom_cv_processing_status, '') = ''
		""",
		(PROCESSOR_VERSION,),
	)
	frappe.clear_cache()
