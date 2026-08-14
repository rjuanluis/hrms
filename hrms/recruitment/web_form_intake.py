from __future__ import annotations

import frappe

RECRUITMENT_WEB_FORM_ROUTE = "empleos/solicitud"
WEB_SOURCE = "Sitio Web"
DEFAULT_JOB_OPENING = "HR-OPN-2026-0001"
_CONTEXT_FLAG = "ayp_authoritative_recruitment_web_form"


def ensure_web_applicant_source() -> str:
	"""Ensure the server-derived Web source has a valid Link target."""

	if not frappe.db.exists("Job Applicant Source", WEB_SOURCE):
		frappe.get_doc(
			{
				"doctype": "Job Applicant Source",
				"source_name": WEB_SOURCE,
				"details": "Solicitud recibida por el Web Form oficial de empleos.",
			}
		).insert(ignore_permissions=True)
	return WEB_SOURCE


def authoritative_recruitment_web_form_context():
	return getattr(frappe.flags, _CONTEXT_FLAG, None)


def _frappe_web_form_accept(*, web_form: str, data: str | dict, web_form_request_key: str | None = None):
	# Import only after Frappe has initialized its site and log paths. Importing
	# WebForm at module load time breaks standalone post-install configurators.
	from frappe.website.doctype.web_form.web_form import accept as native_accept

	return native_accept(
		web_form=web_form,
		data=data,
		web_form_request_key=web_form_request_key,
	)


def _authoritative_form(web_form_name: str):
	matches = frappe.get_all(
		"Web Form",
		filters={"route": RECRUITMENT_WEB_FORM_ROUTE},
		fields=[
			"name",
			"route",
			"doc_type",
			"published",
			"login_required",
			"anonymous",
			"allow_edit",
			"allow_delete",
		],
		limit=2,
	)
	if not any(row.name == web_form_name for row in matches):
		return None
	if len(matches) != 1:
		frappe.throw(frappe._("La ruta oficial de empleos no es única."))
	form = matches[0]
	if (
		form.doc_type != "Job Applicant"
		or not form.published
		or form.login_required
		or not form.anonymous
		or form.allow_edit
		or form.allow_delete
	):
		frappe.throw(frappe._("El formulario oficial de empleos no tiene una configuración segura."))
	return form


# Security-reviewed public boundary: exact form configuration + request-local provenance +
# native Frappe validation; lookalike forms never receive authoritative context.
@frappe.whitelist(  # nosemgrep: tmp.frappe-semgrep-rules.rules.security.guest-whitelisted-method
	methods=["POST", "PUT"], allow_guest=True
)
def accept(web_form: str, data: str | dict, web_form_request_key: str | None = None):
	"""Delegate to Frappe WebForm.accept with a server-authoritative origin context."""

	form = _authoritative_form(str(web_form))
	previous = authoritative_recruitment_web_form_context()
	if form:
		setattr(
			frappe.flags,
			_CONTEXT_FLAG,
			frappe._dict(
				name=form.name,
				route=form.route,
				source=WEB_SOURCE,
				job_opening=str(
					frappe.conf.get("ayp_recruitment_job_opening") or DEFAULT_JOB_OPENING
				).strip(),
			),
		)
	try:
		return _frappe_web_form_accept(
			web_form=web_form,
			data=data,
			web_form_request_key=web_form_request_key,
		)
	finally:
		if previous is None:
			frappe.flags.pop(_CONTEXT_FLAG, None)
		else:
			setattr(frappe.flags, _CONTEXT_FLAG, previous)
