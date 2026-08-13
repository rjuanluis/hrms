from __future__ import annotations

from collections.abc import Mapping
from typing import Any

FINAL_INTERVIEW_STATUSES = frozenset({"Cleared", "Rejected"})
INTERVIEW_TO_APPLICATION_STATUS = {"Cleared": "Accepted", "Rejected": "Rejected"}
MIN_DECISION_RATIONALE_LENGTH = 20
MAX_DECISION_RATIONALE_LENGTH = 1000


class InterviewDecisionValidationError(ValueError):
	pass


def _value(document: Any, fieldname: str) -> Any:
	if isinstance(document, Mapping):
		return document.get(fieldname)
	return getattr(document, fieldname, None)


def validate_decision_rationale(interview_status: str, rationale: Any) -> str:
	text = str(rationale or "").strip()
	if interview_status not in FINAL_INTERVIEW_STATUSES:
		return text
	if len(text) < MIN_DECISION_RATIONALE_LENGTH:
		raise InterviewDecisionValidationError(
			f"La decisión final exige una justificación de al menos {MIN_DECISION_RATIONALE_LENGTH} caracteres."
		)
	if len(text) > MAX_DECISION_RATIONALE_LENGTH:
		raise InterviewDecisionValidationError(
			f"La justificación no puede exceder {MAX_DECISION_RATIONALE_LENGTH} caracteres."
		)
	return text


def application_status_for_interview(interview_status: str):
	return INTERVIEW_TO_APPLICATION_STATUS.get(interview_status)


def validate_interview_backed_application_decision(
	applicant: str,
	target_status: str,
	interview: Any,
	*,
	require_ayp: bool = False,
) -> None:
	if target_status not in {"Accepted", "Rejected"}:
		return
	if int(_value(interview, "docstatus") or 0) != 1:
		raise InterviewDecisionValidationError("La entrevista debe estar enviada antes de decidir.")
	if _value(interview, "job_applicant") != applicant:
		raise InterviewDecisionValidationError("La entrevista no corresponde a esta aplicación.")
	expected_interview_status = "Cleared" if target_status == "Accepted" else "Rejected"
	interview_status = str(_value(interview, "status") or "")
	if interview_status != expected_interview_status:
		raise InterviewDecisionValidationError(
			f"La decisión {target_status} requiere una entrevista {expected_interview_status}."
		)
	questions_snapshot = str(_value(interview, "custom_ayp_questions_snapshot") or "").strip()
	if require_ayp and not questions_snapshot:
		raise InterviewDecisionValidationError(
			"La candidatura AyP exige una entrevista estructurada AyP enviada."
		)
	if questions_snapshot:
		validate_decision_rationale(
			interview_status,
			_value(interview, "custom_ayp_decision_rationale"),
		)
