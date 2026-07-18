#!/usr/bin/env python3
"""Apply the standard, official AyP Frappe/ERPNext/HRMS setup idempotently."""

from __future__ import annotations

import os
from pathlib import Path

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.desk.page.setup_wizard.setup_wizard import setup_complete
from frappe.translate import set_default_language

from hrms.recruitment.talent_pool import backfill_candidate_profiles

SITE = os.environ.get("AYP_SITE_NAME", "hr.aroypedal.com")
COMPANY = "ARO Y PEDAL SRL"
TAX_ID = "101-57005-9"
ADMIN_EMAIL = "juanluis@aroypedal.com"
LEAVE_PERIOD_START = "2026-01-01"
LEAVE_PERIOD_END = "2026-12-31"
SITES_DIR = Path(os.environ.get("AYP_SITES_DIR", "/home/frappe/frappe-bench/sites"))
SECRETS_DIR = Path(os.environ.get("AYP_SECRETS_DIR", "/run/ayp-secrets"))
BRANCHES = (
	{
		"name": "Tienda Principal",
		"address_line1": "Ave. 27 de Febrero 112, casi esq. Leopoldo Navarro",
		"city": "Santo Domingo",
		"state": "Distrito Nacional",
		"pincode": "10201",
		"phone": "829-903-6570",
		"email_id": "27defebrero@aroypedal.com",
	},
	{
		"name": "Tienda Kennedy",
		"address_line1": "Plaza Kennedy, Aut. Duarte Km 6 ½, Av. John F. Kennedy",
		"city": "Santo Domingo",
		"state": "Distrito Nacional",
		"pincode": "",
		"phone": "849-858-7657",
		"email_id": "kennedy@aroypedal.com",
	},
)
DEPARTMENTS = (
	"Gerencia",
	"Administración, Finanzas y RRHH",
	"Ventas",
	"Almacén y Logística",
	"Centros de Servicio",
	"Marketing",
)
DEPARTMENT_RENAMES = {
	"Dirección General": "Gerencia",
	"Administración": "Administración, Finanzas y RRHH",
	"Marketing y Ventas": "Marketing",
	"Taller y Servicio Técnico": "Centros de Servicio",
}

RECRUITMENT_PRIVACY_NOTICE_VERSION = "AYP-RH-2026-07-17-v3"
RECRUITMENT_WEB_FORM_ROUTE = "empleos/solicitud"
RECRUITMENT_WEB_FORM_TITLE = "Solicitud de empleo — Aro y Pedal"
RECRUITMENT_SELECT_OPTIONS = {
	"custom_years_sales_experience": "\nMenos de 1 año\n1 a 2 años\n3 a 5 años\nMás de 5 años",
	"custom_retail_experience": "\nSí\nNo",
	"custom_schedule_availability": "\nSí\nNo\nNecesito conversar sobre el horario",
	"custom_start_availability": "\nInmediata\nDentro de 1 semana\nDentro de 2 semanas\nMás de 2 semanas",
	"custom_bicycle_experience": (
		"\nTengo experiencia en ciclismo o bicicletas"
		"\nConozco algunos productos de ciclismo"
		"\nMe interesa aprender"
		"\nNo tengo experiencia, pero tengo disposición para aprender"
	),
}

RECRUITMENT_INTRODUCTION = """
<p><strong>Uso de tus datos:</strong> ARO Y PEDAL SRL utilizará la información que compartas
para evaluar esta candidatura, documentar el proceso de selección y considerar tu perfil para
esta u otras vacantes futuras de Aro y Pedal. Todo candidato real entra al talent pool interno; esto
no autoriza mensajes ilimitados ni uso para marketing. El acceso se limita a RRHH, entrevistadores
autorizados y Gerencia. Puedes
solicitar acceso, corrección, cancelación u oposición escribiendo a
<a href="mailto:recursoshumanos@aroypedal.com">recursoshumanos@aroypedal.com</a>.</p>
<p>El currículum es obligatorio. Solo se aceptan PDF o DOCX, máximo 5 MB; se almacena
de forma privada y pasa por un control antivirus antes de guardarse. No incluyas datos sensibles que
no sean necesarios para evaluar tu experiencia.</p>
"""

RECRUITMENT_CLIENT_SCRIPT = """frappe.web_form.after_load = () => {
  const cv_field = frappe.web_form.fields_dict.resume_attachment;
  if (cv_field) {
    cv_field.df.options = Object.assign({}, cv_field.df.options || {}, {
      disable_file_browser: true,
      doctype: 'Job Applicant',
      fieldname: 'resume_attachment',
      is_private: 1,
    });
  }
};

frappe.web_form.validate = () => {
  if (!frappe.web_form.get_value('custom_data_processing_consent')) {
    frappe.msgprint('Debes aceptar el aviso de privacidad para enviar la solicitud.');
    return false;
  }
  return true;
};
"""


def read_secret(name: str) -> str:
	env_name = f"AYP_{name.upper()}"
	value = os.environ.get(env_name, "").strip()
	if not value and (SECRETS_DIR / name).is_file():
		value = (SECRETS_DIR / name).read_text(encoding="utf-8").strip()
	if not value:
		raise RuntimeError(f"Required secret {name} is empty")
	return value


def ensure_company_address() -> str:
	existing = frappe.db.get_value(
		"Dynamic Link",
		{"link_doctype": "Company", "link_name": COMPANY, "parenttype": "Address"},
		"parent",
	)
	if existing:
		return existing

	address = frappe.get_doc(
		{
			"doctype": "Address",
			"address_title": "Aro y Pedal - Santo Domingo",
			"address_type": "Office",
			"address_line1": "Ave. 27 de Febrero 112",
			"city": "Santo Domingo",
			"state": "Distrito Nacional",
			"pincode": "10202",
			"country": "Dominican Republic",
			"email_id": ADMIN_EMAIL,
			"is_your_company_address": 1,
			"links": [{"link_doctype": "Company", "link_name": COMPANY}],
		}
	)
	address.insert(ignore_permissions=True)
	return address.name


def ensure_branches() -> list[str]:
	names = []
	for item in BRANCHES:
		name = item["name"]
		if not frappe.db.exists("Branch", name):
			frappe.get_doc({"doctype": "Branch", "branch": name}).insert(ignore_permissions=True)

		existing_address = frappe.db.get_value(
			"Dynamic Link",
			{"link_doctype": "Branch", "link_name": name, "parenttype": "Address"},
			"parent",
		)
		if not existing_address:
			address = frappe.get_doc(
				{
					"doctype": "Address",
					"address_title": name,
					"address_type": "Office",
					"address_line1": item["address_line1"],
					"city": item["city"],
					"state": item["state"],
					"pincode": item["pincode"],
					"country": "Dominican Republic",
					"phone": item["phone"],
					"email_id": item["email_id"],
					"is_your_company_address": 1,
					"links": [
						{"link_doctype": "Branch", "link_name": name},
						{"link_doctype": "Company", "link_name": COMPANY},
					],
				}
			)
			address.insert(ignore_permissions=True)
		names.append(name)
	return names


def department_name(value: str) -> str | None:
	return frappe.db.get_value("Department", {"department_name": value, "company": COMPANY}, "name")


def ensure_departments() -> list[str]:
	department_meta = frappe.get_meta("Department")
	has_disabled = department_meta.has_field("disabled")

	for old_label, new_label in DEPARTMENT_RENAMES.items():
		old_name = department_name(old_label)
		new_name = department_name(new_label)
		if old_name and not new_name:
			frappe.rename_doc("Department", old_name, new_label, force=True)
			renamed = frappe.get_doc("Department", new_label)
			renamed.department_name = new_label
			if has_disabled:
				renamed.disabled = 0
			renamed.save(ignore_permissions=True)
		elif old_name and new_name and has_disabled:
			frappe.db.set_value("Department", old_name, "disabled", 1, update_modified=False)

	names: list[str] = []
	for label in DEPARTMENTS:
		name = department_name(label)
		if not name:
			doc = frappe.get_doc({"doctype": "Department", "department_name": label, "company": COMPANY})
			doc.insert(ignore_permissions=True)
			name = doc.name
		if has_disabled:
			frappe.db.set_value("Department", name, "disabled", 0, update_modified=False)
		names.append(name)

	if has_disabled:
		desired_names = set(names)
		company_departments = frappe.get_all(
			"Department", filters={"company": COMPANY}, fields=["name", "is_group"]
		)
		for department in company_departments:
			if department.name not in desired_names and not department.is_group:
				frappe.db.set_value("Department", department.name, "disabled", 1, update_modified=False)
	return names


def ensure_active_leave_period() -> str:
	filters = {
		"company": COMPANY,
		"from_date": LEAVE_PERIOD_START,
		"to_date": LEAVE_PERIOD_END,
	}
	name = frappe.db.get_value("Leave Period", filters, "name")
	leave_period = frappe.get_doc("Leave Period", name) if name else frappe.new_doc("Leave Period")
	leave_period.update({**filters, "is_active": 1})
	leave_period.save(ignore_permissions=True)
	leave_period.reload()
	if not leave_period.is_active:
		raise RuntimeError(f"Leave Period {leave_period.name} did not persist as active")
	return leave_period.name


def ensure_recruitment_security_fields() -> None:
	create_custom_fields(
		{
			"File": [
				{
					"fieldname": "custom_av_scan_status",
					"label": "Antivirus Scan Status",
					"fieldtype": "Select",
					"options": "\nClean\nRejected",
					"read_only": 1,
					"insert_after": "file_size",
				},
				{
					"fieldname": "custom_av_scan_engine",
					"label": "Antivirus Engine",
					"fieldtype": "Data",
					"read_only": 1,
					"insert_after": "custom_av_scan_status",
				},
				{
					"fieldname": "custom_av_scanned_on",
					"label": "Antivirus Scanned On",
					"fieldtype": "Datetime",
					"read_only": 1,
					"insert_after": "custom_av_scan_engine",
				},
				{
					"fieldname": "custom_cv_sha256",
					"label": "Candidate CV SHA-256",
					"fieldtype": "Data",
					"read_only": 1,
					"hidden": 1,
					"insert_after": "custom_av_scanned_on",
				},
			],
			"Job Applicant": [
				{
					"fieldname": "custom_years_sales_experience",
					"label": "Años de experiencia en ventas o servicio al cliente",
					"fieldtype": "Select",
					"options": RECRUITMENT_SELECT_OPTIONS["custom_years_sales_experience"],
					"insert_after": "phone_number",
				},
				{
					"fieldname": "custom_retail_experience",
					"label": "Experiencia en tiendas o retail",
					"fieldtype": "Select",
					"options": RECRUITMENT_SELECT_OPTIONS["custom_retail_experience"],
					"insert_after": "custom_years_sales_experience",
				},
				{
					"fieldname": "custom_schedule_availability",
					"label": "Disponibilidad dentro del horario de tienda",
					"fieldtype": "Select",
					"options": RECRUITMENT_SELECT_OPTIONS["custom_schedule_availability"],
					"insert_after": "custom_retail_experience",
				},
				{
					"fieldname": "custom_start_availability",
					"label": "Disponibilidad para iniciar",
					"fieldtype": "Select",
					"options": RECRUITMENT_SELECT_OPTIONS["custom_start_availability"],
					"insert_after": "custom_schedule_availability",
				},
				{
					"fieldname": "custom_bicycle_experience",
					"label": "Conocimiento o interés en bicicletas",
					"fieldtype": "Select",
					"options": RECRUITMENT_SELECT_OPTIONS["custom_bicycle_experience"],
					"insert_after": "custom_start_availability",
				},
				{
					"fieldname": "custom_data_processing_consent",
					"label": "Consentimiento para tratamiento de datos",
					"fieldtype": "Check",
					"default": "0",
					"insert_after": "upper_range",
				},
				{
					"fieldname": "custom_privacy_notice_version",
					"label": "Versión del aviso de privacidad",
					"fieldtype": "Data",
					"default": RECRUITMENT_PRIVACY_NOTICE_VERSION,
					"read_only": 1,
					"hidden": 1,
					"insert_after": "custom_data_processing_consent",
				},
				{
					"fieldname": "custom_candidate_profile",
					"label": "Perfil canónico del candidato",
					"fieldtype": "Link",
					"options": "AYP Candidate Profile",
					"read_only": 1,
					"insert_after": "custom_privacy_notice_version",
				},
				{
					"fieldname": "custom_dedupe_status",
					"label": "Estado de deduplicación",
					"fieldtype": "Select",
					"options": "\nNuevo\nCoincidencia\nRevisión requerida\nManual",
					"read_only": 1,
					"insert_after": "custom_candidate_profile",
				},
				{
					"fieldname": "custom_cv_sha256",
					"label": "SHA-256 del CV",
					"fieldtype": "Data",
					"read_only": 1,
					"hidden": 1,
					"insert_after": "custom_dedupe_status",
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
			],
		},
		update=True,
	)


def ensure_recruitment_web_form() -> tuple[str, list[str]]:
	name = frappe.db.get_value("Web Form", {"route": RECRUITMENT_WEB_FORM_ROUTE}, "name")
	web_form = frappe.get_doc("Web Form", name) if name else frappe.new_doc("Web Form")
	web_form.update(
		{
			"title": RECRUITMENT_WEB_FORM_TITLE,
			"route": RECRUITMENT_WEB_FORM_ROUTE,
			"doc_type": "Job Applicant",
			"module": "HR",
			"published": 1,
			"login_required": 0,
			"allow_edit": 0,
			"allow_delete": 0,
			"allow_multiple": 0,
			"show_attachments": 0,
			"max_attachment_size": 5,
			"button_label": "Enviar solicitud",
			"hide_navbar": 1,
			"hide_footer": 1,
			"introduction_text": RECRUITMENT_INTRODUCTION,
			"success_title": "Solicitud recibida",
			"success_message": "Gracias. RRHH revisará tu información y te contactará si tu perfil avanza.",
			"success_url": "/jobs",
			"client_script": RECRUITMENT_CLIENT_SCRIPT,
		}
	)
	web_form.set(
		"web_form_fields",
		[
			{
				"fieldname": "job_title",
				"fieldtype": "Data",
				"label": "Vacante (referencia)",
				"reqd": 1,
				"read_only": 1,
				"description": "Vacante a la que aplicas.",
			},
			{"fieldname": "applicant_name", "fieldtype": "Data", "label": "Nombre completo", "reqd": 1},
			{"fieldname": "email_id", "fieldtype": "Data", "label": "Correo electrónico (opcional)", "reqd": 0},
			{"fieldname": "phone_number", "fieldtype": "Data", "label": "Teléfono", "reqd": 1},
			{
				"fieldname": "custom_years_sales_experience",
				"fieldtype": "Select",
				"label": "Años de experiencia en ventas o servicio al cliente",
				"options": RECRUITMENT_SELECT_OPTIONS["custom_years_sales_experience"],
				"reqd": 1,
			},
			{
				"fieldname": "custom_retail_experience",
				"fieldtype": "Select",
				"label": "¿Tienes experiencia en tiendas o retail?",
				"options": RECRUITMENT_SELECT_OPTIONS["custom_retail_experience"],
				"reqd": 1,
			},
			{
				"fieldname": "custom_schedule_availability",
				"fieldtype": "Select",
				"label": "¿Tienes disponibilidad para trabajar en este horario?",
				"options": RECRUITMENT_SELECT_OPTIONS["custom_schedule_availability"],
				"reqd": 1,
				"description": (
					"Lunes a viernes de 9:00 a. m. a 6:00 p. m. y sábados de 10:00 a. m. a 4:00 p. m. "
					"La jornada, los descansos y la rotación se coordinan conforme a la planificación interna y la legislación."
				),
			},
			{
				"fieldname": "custom_start_availability",
				"fieldtype": "Select",
				"label": "Disponibilidad para iniciar",
				"options": RECRUITMENT_SELECT_OPTIONS["custom_start_availability"],
				"reqd": 1,
			},
			{
				"fieldname": "custom_bicycle_experience",
				"fieldtype": "Select",
				"label": "Conocimiento o interés en bicicletas",
				"options": RECRUITMENT_SELECT_OPTIONS["custom_bicycle_experience"],
				"reqd": 1,
			},
			{
				"fieldname": "cover_letter",
				"fieldtype": "Small Text",
				"label": "¿Por qué te interesa esta posición?",
				"reqd": 1,
				"description": "Cuéntanos brevemente sobre tu experiencia y motivación. No incluyas datos sensibles.",
			},
			{
				"fieldname": "resume_attachment",
				"fieldtype": "Attach",
				"label": "Currículum (obligatorio)",
				"reqd": 1,
				"description": "PDF o DOCX, máximo 5 MB. Se almacena de forma privada y pasa por antivirus.",
			},
			{
				"fieldname": "custom_data_processing_consent",
				"fieldtype": "Check",
				"label": "He leído el aviso y autorizo el tratamiento de mis datos para esta vacante y futuras oportunidades de Aro y Pedal",
				"reqd": 1,
			},
			{
				"fieldname": "custom_privacy_notice_version",
				"fieldtype": "Data",
				"label": "Versión del aviso de privacidad",
				"reqd": 0,
				"read_only": 1,
				"hidden": 1,
			},
		],
	)
	web_form.save(ignore_permissions=True)
	web_form.reload()
	rendered_select_options = {
		row.fieldname: row.options or ""
		for row in web_form.web_form_fields
		if row.fieldname in RECRUITMENT_SELECT_OPTIONS
	}
	invalid_selects = {
		fieldname: rendered_select_options.get(fieldname, "")
		for fieldname, expected_options in RECRUITMENT_SELECT_OPTIONS.items()
		if rendered_select_options.get(fieldname) != expected_options
	}
	if invalid_selects:
		raise RuntimeError(
			f"Recruitment Web Form Select options were not persisted: {sorted(invalid_selects)}"
		)

	retired = []
	for other_name in frappe.get_all("Web Form", filters={"doc_type": "Job Applicant"}, pluck="name"):
		if other_name == web_form.name:
			continue
		retired.append(other_name)
		if frappe.db.get_value("Web Form", other_name, "published"):
			frappe.db.set_value("Web Form", other_name, "published", 0, update_modified=False)
	published_forms = frappe.get_all(
		"Web Form",
		filters={"doc_type": "Job Applicant", "published": 1},
		pluck="name",
	)
	if published_forms != [web_form.name]:
		raise RuntimeError(f"Expected exactly one published Job Applicant Web Form; found {published_forms}")
	return web_form.name, retired


def enable_restricted_guest_cv_uploads() -> None:
	frappe.db.set_single_value("System Settings", "allow_guests_to_upload_files", 1)
	frappe.db.set_single_value("System Settings", "allowed_doctypes_for_guest_uploads", "Job Applicant")


def main() -> None:
	os.chdir(SITES_DIR)
	frappe.init(site=SITE)
	frappe.connect()
	frappe.set_user("Administrator")
	try:
		if not frappe.is_setup_complete():
			result = setup_complete(
				{
					"language": "Spanish",
					"lang": "Spanish",
					"country": "Dominican Republic",
					"timezone": "America/Santo_Domingo",
					"currency": "DOP",
					"email": ADMIN_EMAIL,
					"full_name": "Juan Rodríguez",
					"password": read_secret("admin_password"),
					"company_name": COMPANY,
					"company_abbr": "AYP",
					"chart_of_accounts": "Standard",
					"fy_start_date": "2026-01-01",
					"fy_end_date": "2026-12-31",
					"domain": "",
					"bank_account": "",
					"setup_demo": 0,
					"enable_telemetry": 0,
				}
			)
			if result and result.get("status") != "ok":
				raise RuntimeError(f"Unexpected setup result: {result}")

		if not frappe.db.exists("Company", COMPANY):
			raise RuntimeError(f"Setup completed without creating company {COMPANY}")

		frappe.db.set_value("Company", COMPANY, "tax_id", TAX_ID, update_modified=False)
		address = ensure_company_address()
		branches = ensure_branches()
		departments = ensure_departments()
		leave_period = ensure_active_leave_period()
		ensure_recruitment_security_fields()
		candidate_profiles_backfilled = backfill_candidate_profiles()
		recruitment_web_form, retired_recruitment_web_forms = ensure_recruitment_web_form()
		enable_restricted_guest_cv_uploads()
		set_default_language("es")
		frappe.db.set_single_value("System Settings", "language", "es")
		frappe.db.set_value("User", ADMIN_EMAIL, "language", "es", update_modified=False)
		frappe.db.set_value("User", ADMIN_EMAIL, "time_zone", "America/Santo_Domingo", update_modified=False)
		frappe.db.commit()
		print(
			{
				"status": "configured",
				"site": SITE,
				"company": COMPANY,
				"tax_id": TAX_ID,
				"address": address,
				"branches": branches,
				"departments": departments,
				"leave_period": leave_period,
				"recruitment_web_form": recruitment_web_form,
				"retired_recruitment_web_forms": retired_recruitment_web_forms,
				"candidate_profiles_backfilled": candidate_profiles_backfilled,
				"guest_upload_doctypes": ["Job Applicant"],
				"country": "Dominican Republic",
				"currency": "DOP",
				"time_zone": "America/Santo_Domingo",
				"language": "es",
				"demo_data": False,
			}
		)
	finally:
		frappe.destroy()


if __name__ == "__main__":
	main()
