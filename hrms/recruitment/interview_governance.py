import re

import frappe
from frappe import _

from hrms.recruitment.candidate_document_service import revalidate_candidate_document
from hrms.recruitment.interview_decision_domain import (
	InterviewDecisionValidationError,
	validate_decision_rationale,
)

FINAL_APPLICATION_STATUSES = frozenset({"Accepted"})
CONCURRENT_CHANGE_MESSAGE = (
	"El expediente cambió mientras se procesaba la acción. Recargue la entrevista y vuelva a intentarlo."
)


def lock_ayp_interview_decision_dependencies(
	interview_name: str,
	job_applicant: str | None = None,
):
	"""Lock and reload one decision evidence set in a global deterministic order.

	Every final-decision and cancellation path must use Interview Feedback ->
	Interview -> Job Applicant. Frappe's native cancellation path locks the
	feedback row before before_cancel, so this order avoids a lock inversion. It forces the
	loser to revalidate after waiting, instead of acting on a stale pre-lock read.
	"""

	feedback_rows = frappe.db.sql(
		"""
		SELECT name, docstatus, interviewer
		FROM `tabInterview Feedback`
		WHERE interview = %s ORDER BY name FOR UPDATE
		""",
		(interview_name,),
		as_dict=True,
	)
	interview_rows = frappe.db.sql(
		"""
		SELECT name, job_applicant, docstatus
		FROM `tabInterview` WHERE name = %s FOR UPDATE
		""",
		(interview_name,),
		as_dict=True,
	)
	if not interview_rows:
		frappe.throw(_("La entrevista ya no existe."), frappe.DoesNotExistError)
	locked_interview = interview_rows[0]
	if job_applicant and locked_interview.job_applicant != job_applicant:
		frappe.throw(_("La entrevista y la solicitud ya no coinciden."), frappe.ValidationError)
	applicant_name = locked_interview.job_applicant
	applicant_rows = frappe.db.sql(
		"""
		SELECT name, status, custom_ayp_final_interview
		FROM `tabJob Applicant` WHERE name = %s FOR UPDATE
		""",
		(applicant_name,),
		as_dict=True,
	)
	if not applicant_rows:
		frappe.throw(_("La solicitud de empleo ya no existe."), frappe.DoesNotExistError)
	return (
		frappe.get_doc("Interview", interview_name, for_update=True),
		frappe.get_doc("Job Applicant", applicant_name, for_update=True),
		feedback_rows,
	)


def lock_ayp_feedback_cancellation_dependencies(interview_name: str):
	"""Continue after Frappe's native target-feedback lock without locking siblings."""

	interview = frappe.get_doc("Interview", interview_name, for_update=True)
	if not interview.job_applicant:
		frappe.throw(_("La entrevista ya no tiene una solicitud asociada."), frappe.ValidationError)
	applicant = frappe.get_doc("Job Applicant", interview.job_applicant, for_update=True)
	return interview, applicant


def lock_ayp_interview_cancellation_dependencies(interview_name: str, job_applicant: str | None = None):
	"""Continue after Frappe's native Interview lock, then lock the applicant."""

	interview = frappe.get_doc("Interview", interview_name, for_update=True)
	if job_applicant and interview.job_applicant != job_applicant:
		frappe.throw(_("La entrevista y la solicitud ya no coinciden."), frappe.ValidationError)
	if not interview.job_applicant:
		frappe.throw(_("La entrevista ya no tiene una solicitud asociada."), frappe.ValidationError)
	applicant = frappe.get_doc("Job Applicant", interview.job_applicant, for_update=True)
	return interview, applicant


def _is_ayp_interview(doc) -> bool:
	return bool(str(doc.get("custom_ayp_questions_snapshot") or "").strip())


def _question_count(snapshot: str) -> int:
	return sum(1 for line in str(snapshot or "").splitlines() if re.match(r"^\s*\d+\.", line))


def validate_interview(doc, method=None):
	if not doc.is_new() and (
		doc.has_value_changed("interview_type") or doc.has_value_changed("custom_ayp_questions_snapshot")
	):
		submitted_feedback = frappe.db.exists("Interview Feedback", {"interview": doc.name, "docstatus": 1})
		if submitted_feedback:
			frappe.throw(
				_("El kit y el snapshot de una entrevista con feedback enviado son inmutables."),
				frappe.ValidationError,
			)
	structured_questions = ""
	if doc.interview_type and (doc.is_new() or doc.has_value_changed("interview_type")):
		structured_questions = (
			frappe.db.get_value("Interview Type", doc.interview_type, "custom_ayp_structured_questions") or ""
		)
	else:
		structured_questions = (
			frappe.db.get_value("Interview", doc.name, "custom_ayp_questions_snapshot") or ""
		)
		if not structured_questions and doc.interview_type:
			interview_type_questions = (
				frappe.db.get_value("Interview Type", doc.interview_type, "custom_ayp_structured_questions")
				or ""
			)
			if interview_type_questions:
				frappe.throw(
					_("La entrevista AyP existente no tiene un snapshot confiable de preguntas."),
					frappe.ValidationError,
				)
	doc.custom_ayp_questions_snapshot = structured_questions

	# Governance is deliberately scoped to the AyP structured kit. Existing
	# HRMS interview types keep their native behavior.
	if not structured_questions:
		return
	try:
		doc.custom_ayp_decision_rationale = validate_decision_rationale(
			doc.status,
			doc.custom_ayp_decision_rationale,
		)
	except InterviewDecisionValidationError as exc:
		frappe.throw(_(str(exc)), frappe.ValidationError)

	if doc.status in {"Cleared", "Rejected"}:
		if not doc.custom_ayp_decided_by or doc.has_value_changed("status"):
			doc.custom_ayp_decided_by = frappe.session.user
			doc.custom_ayp_decided_on = frappe.utils.now_datetime()
	elif not doc.docstatus:
		doc.custom_ayp_decided_by = ""
		doc.custom_ayp_decided_on = None


def validate_ayp_interview_feedback(doc, method=None):
	interview = frappe.get_doc("Interview", doc.interview)
	if not _is_ayp_interview(interview):
		return

	evidence_lines = [
		line.strip()
		for line in str(doc.get("custom_ayp_question_evidence") or "").splitlines()
		if line.strip()
	]
	required_count = _question_count(interview.custom_ayp_questions_snapshot)
	if required_count and len(evidence_lines) < required_count:
		frappe.throw(
			_(
				"La entrevista AyP exige al menos una línea de evidencia observable por cada pregunta ({0})."
			).format(required_count),
			frappe.ValidationError,
		)
	if any(len(line) < 10 for line in evidence_lines):
		frappe.throw(
			_("Cada línea de evidencia AyP debe describir una observación concreta."),
			frappe.ValidationError,
		)


def validate_ayp_interview_submission(doc, method=None):
	if not _is_ayp_interview(doc):
		return
	assigned = {row.interviewer for row in doc.interview_details if row.interviewer}
	if not assigned:
		frappe.throw(
			_("La entrevista AyP debe tener al menos una persona entrevistadora asignada."),
			frappe.ValidationError,
		)
	submitted = set(
		frappe.get_all(
			"Interview Feedback",
			filters={"interview": doc.name, "docstatus": 1},
			pluck="interviewer",
		)
	)
	missing = sorted(assigned - submitted)
	if missing:
		frappe.throw(
			_("Falta feedback enviado de: {0}.").format(", ".join(missing)),
			frappe.ValidationError,
		)


def validate_ayp_interview_cancellation(doc, method=None):
	try:
		interview, applicant = lock_ayp_interview_cancellation_dependencies(doc.name, doc.job_applicant)
	except frappe.QueryDeadlockError:
		raise frappe.ValidationError(CONCURRENT_CHANGE_MESSAGE)
	if not _is_ayp_interview(interview):
		return
	if interview.docstatus == 2:
		return
	if applicant.custom_ayp_final_interview == interview.name and applicant.status in {
		"Accepted",
		"Rejected",
	}:
		frappe.throw(
			_(
				"No puedes cancelar la entrevista que respalda una decisión final AyP. Registra otra decisión auditada."
			),
			frappe.ValidationError,
		)


def validate_ayp_feedback_cancellation(doc, method=None):
	try:
		interview, applicant = lock_ayp_feedback_cancellation_dependencies(doc.interview)
	except frappe.QueryDeadlockError:
		raise frappe.ValidationError(CONCURRENT_CHANGE_MESSAGE)
	if not _is_ayp_interview(interview):
		return
	if applicant.custom_ayp_final_interview == interview.name and applicant.status in {
		"Accepted",
		"Rejected",
	}:
		frappe.throw(
			_("No puedes cancelar feedback que sustenta una decisión final AyP."),
			frappe.ValidationError,
		)


def validate_job_applicant_final_transition(doc, method=None):
	if not doc.has_value_changed("status"):
		return
	before = doc.get_doc_before_save()
	previous_status = before.status if before else None
	previous_final_interview = before.get("custom_ayp_final_interview") if before else None
	if not doc.get("custom_ayp_governed"):
		return
	if doc.status in {"Shortlisted", "Accepted", "Rejected"}:
		# This save gate runs before origin flags so direct form/API, legacy AyP
		# rows, Candidate Review, and Interview decisions all require the exact
		# current secure CV.
		revalidate_candidate_document(doc)
	if (
		(previous_status in FINAL_APPLICATION_STATUSES or previous_final_interview)
		and doc.status != previous_status
		and not frappe.flags.get("ayp_interview_decision")
	):
		frappe.throw(
			_("Un estado final AyP solo puede cambiar mediante otra decisión de entrevista auditada."),
			frappe.ValidationError,
		)
	if doc.status not in {"Accepted", "Rejected"}:
		return
	if doc.status == "Rejected" and frappe.flags.get("ayp_candidate_review_batch"):
		return
	if frappe.flags.get("ayp_interview_decision"):
		return
	frappe.throw(
		_("La decisión final de un candidato AyP debe registrarse desde una entrevista enviada y auditada."),
		frappe.ValidationError,
	)


def update_job_applicant_from_downstream(job_applicant: str, status: str, source: str) -> None:
	state = frappe.db.get_value(
		"Job Applicant",
		job_applicant,
		["status", "custom_ayp_governed"],
		as_dict=True,
	)
	if not state or state.status == status:
		return
	if state.custom_ayp_governed:
		frappe.throw(
			_(
				"{0} no puede cambiar directamente la decisión final de un candidato AyP. Finaliza primero la entrevista."
			).format(source),
			frappe.ValidationError,
		)
	frappe.db.set_value("Job Applicant", job_applicant, "status", status)
