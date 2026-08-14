from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.email.doctype.notification.notification import clear_notification_cache

from hrms.recruitment.email_intake import (
	INTAKE_BLOCKED,
	INTAKE_COMPLETED,
	INTAKE_PENDING,
	INTAKE_PROCESSING,
)

NOTIFICATION_NAME = "AYP Candidate Application Received"
NOTIFICATION_CONDITION = (
	"doc.email_id and doc.get('custom_data_processing_consent') " "and doc.source != 'Email Recursos Humanos'"
)

RECRUITMENT_EMAIL_INTAKE_FIELDS = {
	"Communication": [
		{
			"fieldname": "custom_ayp_email_intake_status",
			"label": "Estado intake de reclutamiento",
			"fieldtype": "Select",
			"options": "\n".join((INTAKE_PENDING, INTAKE_PROCESSING, INTAKE_COMPLETED, INTAKE_BLOCKED)),
			"read_only": 1,
			"in_list_view": 1,
			"insert_after": "reference_name",
		},
		{
			"fieldname": "custom_ayp_email_intake_queued_on",
			"label": "Intake encolado el",
			"fieldtype": "Datetime",
			"read_only": 1,
			"hidden": 1,
			"insert_after": "custom_ayp_email_intake_status",
		},
		{
			"fieldname": "custom_ayp_email_intake_started_on",
			"label": "Intake iniciado el",
			"fieldtype": "Datetime",
			"read_only": 1,
			"hidden": 1,
			"insert_after": "custom_ayp_email_intake_queued_on",
		},
		{
			"fieldname": "custom_ayp_email_intake_claim",
			"label": "Claim intake de reclutamiento",
			"fieldtype": "Data",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"insert_after": "custom_ayp_email_intake_started_on",
		},
		{
			"fieldname": "custom_ayp_email_intake_completed_on",
			"label": "Intake completado el",
			"fieldtype": "Datetime",
			"read_only": 1,
			"insert_after": "custom_ayp_email_intake_claim",
		},
		{
			"fieldname": "custom_ayp_email_intake_error_code",
			"label": "Código seguro de bloqueo del intake",
			"fieldtype": "Data",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"insert_after": "custom_ayp_email_intake_completed_on",
		},
		{
			"fieldname": "custom_ayp_email_intake_applicant",
			"label": "Solicitud creada por intake",
			"fieldtype": "Link",
			"options": "Job Applicant",
			"read_only": 1,
			"insert_after": "custom_ayp_email_intake_error_code",
		},
	],
}


def _sync_application_received_notification() -> None:
	if not frappe.db.exists("Notification", NOTIFICATION_NAME):
		frappe.throw(frappe._("Required standard Notification does not exist: {0}").format(NOTIFICATION_NAME))
	expected = {
		"enabled": 1,
		"document_type": "Job Applicant",
		"event": "New",
		"condition_type": "Python",
		"condition": NOTIFICATION_CONDITION,
	}
	# Standard Notifications cannot be saved outside developer mode. A direct,
	# idempotent DB update is the migration path; invalidate the runtime cache
	# before the exact post-migrate readback.
	frappe.db.set_value("Notification", NOTIFICATION_NAME, expected, update_modified=False)
	clear_notification_cache()

	stored = frappe.db.get_value(
		"Notification",
		NOTIFICATION_NAME,
		list(expected),
		as_dict=True,
	)
	if not stored or any(stored.get(field) != value for field, value in expected.items()):
		raise RuntimeError("La Notification de solicitudes no quedó sincronizada de forma segura.")


def execute():
	create_custom_fields(RECRUITMENT_EMAIL_INTAKE_FIELDS, update=True)
	_sync_application_received_notification()
	frappe.clear_cache(doctype="Communication")
