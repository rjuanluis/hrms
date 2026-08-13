import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


DURABILITY_FIELDS = {
	"Job Applicant": [
		{
			"fieldname": "custom_cv_processing_queued_on",
			"label": "Procesamiento de CV encolado el",
			"fieldtype": "Datetime",
			"read_only": 1,
			"hidden": 1,
			"insert_after": "custom_cv_page_count",
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
	],
}


def execute():
	create_custom_fields(DURABILITY_FIELDS, update=True)
	frappe.db.sql(
		"""
		UPDATE `tabJob Applicant`
		SET custom_cv_processing_queued_on = COALESCE(modified, creation)
		WHERE custom_cv_processing_status = 'Pendiente'
			AND custom_cv_processing_queued_on IS NULL
		"""
	)
	frappe.clear_cache()
