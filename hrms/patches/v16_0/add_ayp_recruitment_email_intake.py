from __future__ import annotations

import json
from pathlib import Path

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.email.doctype.notification.notification import clear_notification_cache

from hrms.recruitment.email_intake import (
	INTAKE_BLOCKED,
	INTAKE_COMPLETED,
	INTAKE_PENDING,
	INTAKE_PROCESSING,
	disable_existing_recruitment_mailbox_auto_reply,
)
from hrms.recruitment.web_form_intake import ensure_web_applicant_source

NOTIFICATION_NAME = "AYP Candidate Application Received"
NOTIFICATION_CONDITION = (
	"doc.email_id and doc.source == 'Sitio Web' "
	"and doc.get('custom_data_processing_consent') "
	"and doc.get('custom_privacy_notice_version') == 'AYP-RH-2026-07-17-v3' "
	"and doc.get('custom_consent_capture_method') == 'Web Form' "
	"and doc.get('custom_consent_evidence_id') "
	"and doc.get('custom_consent_recorded_on') "
	"and doc.get('custom_consent_form_route') == 'empleos/solicitud'"
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
		{
			"fieldname": "custom_ayp_email_intake_file",
			"label": "Archivo canónico del intake",
			"fieldtype": "Link",
			"options": "File",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"insert_after": "custom_ayp_email_intake_applicant",
		},
		{
			"fieldname": "custom_ayp_email_intake_cv_sha256",
			"label": "SHA-256 del CV del intake",
			"fieldtype": "Data",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"insert_after": "custom_ayp_email_intake_file",
		},
	],
	"Job Applicant": [
		{
			"fieldname": "custom_consent_capture_method",
			"label": "Método de captura del consentimiento",
			"fieldtype": "Data",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"insert_after": "custom_privacy_notice_version",
		},
		{
			"fieldname": "custom_consent_evidence_id",
			"label": "ID de evidencia del consentimiento",
			"fieldtype": "Data",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"unique": 1,
			"insert_after": "custom_consent_capture_method",
		},
		{
			"fieldname": "custom_consent_recorded_on",
			"label": "Fecha de evidencia del consentimiento",
			"fieldtype": "Datetime",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"insert_after": "custom_consent_evidence_id",
		},
		{
			"fieldname": "custom_consent_form_route",
			"label": "Ruta de captura del consentimiento",
			"fieldtype": "Data",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"insert_after": "custom_consent_recorded_on",
		},
		{
			"fieldname": "custom_candidate_cv_file",
			"label": "Archivo canónico del CV",
			"fieldtype": "Link",
			"options": "File",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"insert_after": "custom_consent_capture_method",
		},
	],
}


def _sync_application_received_notification() -> None:
	source_path = (
		Path(__file__).resolve().parents[2]
		/ "hr"
		/ "notification"
		/ "ayp_candidate_application_received"
		/ "ayp_candidate_application_received.json"
	)
	if not source_path.exists():
		source_path = Path(__file__).with_name("ayp_candidate_application_received.json")
	source = json.loads(source_path.read_text(encoding="utf-8"))
	parent_fields = (
		"attach_print",
		"channel",
		"condition",
		"condition_type",
		"docstatus",
		"document_type",
		"enabled",
		"event",
		"is_standard",
		"message",
		"module",
		"send_system_notification",
		"send_to_all_assignees",
		"subject",
	)
	expected = {fieldname: source.get(fieldname) for fieldname in parent_fields}
	if expected["condition"] != NOTIFICATION_CONDITION:
		raise RuntimeError("El JSON estándar y el contrato de Notification divergen.")
	if not frappe.db.exists("Notification", NOTIFICATION_NAME):
		frappe.get_doc({"doctype": "Notification", "name": NOTIFICATION_NAME, **expected}).db_insert()

	# Standard Notifications cannot be saved outside developer mode. Direct,
	# idempotent DB writes are the migration path for this versioned artifact.
	frappe.db.set_value("Notification", NOTIFICATION_NAME, expected, update_modified=False)

	expected_recipients = [
		{
			"receiver_by_document_field": str(row.get("receiver_by_document_field") or ""),
			"receiver_by_role": str(row.get("receiver_by_role") or ""),
			"cc": str(row.get("cc") or ""),
			"bcc": str(row.get("bcc") or ""),
			"condition": str(row.get("condition") or ""),
		}
		for row in source.get("recipients", [])
	]
	recipient_fields = list(expected_recipients[0]) if expected_recipients else ["name"]
	stored_recipients = frappe.get_all(
		"Notification Recipient",
		filters={
			"parent": NOTIFICATION_NAME,
			"parenttype": "Notification",
			"parentfield": "recipients",
		},
		fields=recipient_fields,
		order_by="idx asc",
	)
	normalized_recipients = (
		[
			{fieldname: str(row.get(fieldname) or "") for fieldname in expected_recipients[0]}
			for row in stored_recipients
		]
		if expected_recipients
		else []
	)
	if normalized_recipients != expected_recipients:
		frappe.db.delete(
			"Notification Recipient",
			{
				"parent": NOTIFICATION_NAME,
				"parenttype": "Notification",
				"parentfield": "recipients",
			},
		)
		for idx, recipient in enumerate(expected_recipients, start=1):
			frappe.get_doc(
				{
					"doctype": "Notification Recipient",
					"parent": NOTIFICATION_NAME,
					"parenttype": "Notification",
					"parentfield": "recipients",
					"idx": idx,
					**recipient,
				}
			).db_insert()
	clear_notification_cache()

	stored = frappe.db.get_value(
		"Notification",
		NOTIFICATION_NAME,
		list(expected),
		as_dict=True,
	)
	if not stored or any(stored.get(field) != value for field, value in expected.items()):
		raise RuntimeError("La Notification de solicitudes no quedó sincronizada de forma segura.")
	readback_recipients = frappe.get_all(
		"Notification Recipient",
		filters={
			"parent": NOTIFICATION_NAME,
			"parenttype": "Notification",
			"parentfield": "recipients",
		},
		fields=recipient_fields,
		order_by="idx asc",
	)
	readback_recipients = (
		[
			{fieldname: str(row.get(fieldname) or "") for fieldname in expected_recipients[0]}
			for row in readback_recipients
		]
		if expected_recipients
		else []
	)
	if readback_recipients != expected_recipients:
		raise RuntimeError("Los destinatarios de la Notification no quedaron sincronizados.")


def execute():
	create_custom_fields(RECRUITMENT_EMAIL_INTAKE_FIELDS, update=True)
	ensure_web_applicant_source()
	_sync_application_received_notification()
	disable_existing_recruitment_mailbox_auto_reply()
	frappe.db.add_index(
		"Communication",
		["custom_ayp_email_intake_status", "custom_ayp_email_intake_queued_on"],
		"idx_ayp_email_intake_pending",
	)
	frappe.db.add_index(
		"Communication",
		["custom_ayp_email_intake_status", "custom_ayp_email_intake_started_on"],
		"idx_ayp_email_intake_processing",
	)
	frappe.clear_cache(doctype="Communication")
