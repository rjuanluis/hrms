from __future__ import annotations

import json

import frappe

RECRUITMENT_WEB_FORM_ROUTE = "empleos/solicitud"
WEB_SOURCE = "Sitio Web"
DEFAULT_JOB_OPENING = "HR-OPN-2026-0001"
PRIVACY_NOTICE_VERSION = "AYP-RH-2026-07-17-v3"
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


def _authoritative_payload(data: str | dict, *, job_opening: str) -> str:
	"""Replace every governed Web Form value before Frappe validates Links."""

	try:
		payload = json.loads(data) if isinstance(data, str) else dict(data)
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError(frappe._("Los datos del formulario de empleos no son válidos.")) from exc
	if not isinstance(payload, dict):
		raise frappe.ValidationError(frappe._("Los datos del formulario de empleos deben ser un objeto."))
	if str(payload.get("name") or "").strip():
		frappe.throw(frappe._("El formulario oficial de empleos solo permite solicitudes nuevas."))
	if payload.get("custom_data_processing_consent") not in (1, True, "1"):
		frappe.throw(frappe._("Debes aceptar el aviso de privacidad para enviar la solicitud."))
	payload.update(
		{
			"source": WEB_SOURCE,
			"status": "Open",
			"job_title": job_opening,
			"custom_data_processing_consent": 1,
			"custom_privacy_notice_version": PRIVACY_NOTICE_VERSION,
			"custom_consent_capture_method": "Web Form",
			"custom_consent_evidence_id": "",
			"custom_consent_recorded_on": None,
			"custom_consent_form_route": RECRUITMENT_WEB_FORM_ROUTE,
		}
	)
	return json.dumps(payload)


# Security-reviewed public boundary: exact form configuration + request-local provenance +
# native Frappe validation; lookalike forms never receive authoritative context.
@frappe.whitelist(  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
	methods=["POST", "PUT"], allow_guest=True
)
def accept(web_form: str, data: str | dict, web_form_request_key: str | None = None):
	"""Delegate to Frappe WebForm.accept with a server-authoritative origin context."""

	form = _authoritative_form(str(web_form))
	previous = authoritative_recruitment_web_form_context()
	if form:
		job_opening = str(frappe.conf.get("ayp_recruitment_job_opening") or DEFAULT_JOB_OPENING).strip()
		data = _authoritative_payload(data, job_opening=job_opening)
		setattr(
			frappe.flags,
			_CONTEXT_FLAG,
			frappe._dict(
				name=form.name,
				route=form.route,
				source=WEB_SOURCE,
				job_opening=job_opening,
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
