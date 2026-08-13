from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

APPLICANT_STATUSES = frozenset({"Open", "Replied", "Shortlisted", "Rejected", "Hold", "Accepted"})
BATCH_TARGET_STATUSES = frozenset({"Open", "Replied", "Shortlisted", "Rejected", "Hold"})
DEDUPE_STATUSES = frozenset({"Nuevo", "Coincidencia", "Revisión requerida", "Manual"})
CV_PROCESSING_STATUSES = frozenset(
	{
		"Sin CV",
		"Pendiente",
		"Procesando",
		"Procesado",
		"Revisión manual",
		"Ilegible",
		"Protegido",
		"No compatible",
		"Error de seguridad",
		"Verificado manualmente",
	}
)
SORT_OPTIONS = frozenset({"received", "score"})
INTERVIEW_QUEUE_OPTIONS = frozenset(
	{"no_interview", "unassigned", "missing_feedback", "disagreement", "overdue", "ready_final_decision"}
)
MAX_BATCH_SIZE = 100
MAX_PAGE_LENGTH = 100
MAX_REASON_LENGTH = 500
MAX_SEARCH_LENGTH = 120
MAX_LINK_LENGTH = 140

ALLOWED_TRANSITIONS = {
	"Open": BATCH_TARGET_STATUSES - {"Open"},
	"Replied": BATCH_TARGET_STATUSES - {"Replied"},
	"Shortlisted": BATCH_TARGET_STATUSES - {"Shortlisted"},
	"Rejected": frozenset({"Open", "Hold"}),
	"Hold": BATCH_TARGET_STATUSES - {"Hold"},
	"Accepted": frozenset(),
}


class CandidateReviewValidationError(ValueError):
	pass


def _mapping(value: Any) -> Mapping[str, Any]:
	if value in (None, ""):
		return {}
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except json.JSONDecodeError as exc:
			raise CandidateReviewValidationError("Los filtros no son JSON válido.") from exc
	if not isinstance(value, Mapping):
		raise CandidateReviewValidationError("Los filtros deben ser un objeto.")
	return value


def _bounded_text(value: Any, *, label: str, maximum: int, required: bool = False) -> str:
	text = str(value or "").strip()
	if required and not text:
		raise CandidateReviewValidationError(f"Debes indicar {label}.")
	if len(text) > maximum:
		raise CandidateReviewValidationError(f"{label.capitalize()} no puede exceder {maximum} caracteres.")
	return text


def _integer(value: Any, *, label: str, default: int, minimum: int, maximum: int) -> int:
	if value in (None, ""):
		return default
	try:
		number = int(value)
	except (TypeError, ValueError) as exc:
		raise CandidateReviewValidationError(f"{label.capitalize()} debe ser un número entero.") from exc
	return max(minimum, min(maximum, number))


@dataclass(frozen=True)
class ReviewFilters:
	search: str = ""
	status: str = ""
	job_title: str = ""
	source: str = ""
	dedupe_status: str = ""
	cv_processing_status: str = ""
	minimum_rating: Any = None
	minimum_score: Any = None
	sort_by: str = "received"
	interview_queue: str = ""
	start: int = 0
	page_length: int = 50

	@classmethod
	def from_input(cls, value: Any = None, *, start: Any = 0, page_length: Any = 50) -> ReviewFilters:
		filters = _mapping(value)
		status = _bounded_text(filters.get("status"), label="el estado", maximum=40)
		if status and status not in APPLICANT_STATUSES:
			raise CandidateReviewValidationError("El estado de aplicación no es válido.")

		dedupe_status = _bounded_text(
			filters.get("dedupe_status"), label="el estado de deduplicación", maximum=40
		)
		if dedupe_status and dedupe_status not in DEDUPE_STATUSES:
			raise CandidateReviewValidationError("El estado de deduplicación no es válido.")
		cv_processing_status = _bounded_text(
			filters.get("cv_processing_status"), label="el estado documental", maximum=40
		)
		if cv_processing_status and cv_processing_status not in CV_PROCESSING_STATUSES:
			raise CandidateReviewValidationError("El estado documental no es válido.")

		minimum_rating = filters.get("minimum_rating")
		if minimum_rating not in (None, ""):
			try:
				minimum_rating = int(minimum_rating)
			except (TypeError, ValueError) as exc:
				raise CandidateReviewValidationError("La calificación mínima debe ser un entero.") from exc
			if minimum_rating < 0 or minimum_rating > 5:
				raise CandidateReviewValidationError("La calificación mínima debe estar entre 0 y 5.")
		else:
			minimum_rating = None

		minimum_score = filters.get("minimum_score")
		if minimum_score not in (None, ""):
			try:
				minimum_score = int(minimum_score)
			except (TypeError, ValueError) as exc:
				raise CandidateReviewValidationError("El score mínimo debe ser un entero.") from exc
			if minimum_score < 0 or minimum_score > 100:
				raise CandidateReviewValidationError("El score mínimo debe estar entre 0 y 100.")
		else:
			minimum_score = None

		sort_by = _bounded_text(filters.get("sort_by"), label="el orden", maximum=20) or "received"
		if sort_by not in SORT_OPTIONS:
			raise CandidateReviewValidationError("La opción de orden no es válida.")
		interview_queue = _bounded_text(
			filters.get("interview_queue"), label="la cola de entrevistas", maximum=30
		)
		if interview_queue and interview_queue not in INTERVIEW_QUEUE_OPTIONS:
			raise CandidateReviewValidationError("La cola de entrevistas no es válida.")

		return cls(
			search=_bounded_text(filters.get("search"), label="la búsqueda", maximum=MAX_SEARCH_LENGTH),
			status=status,
			job_title=_bounded_text(filters.get("job_title"), label="la vacante", maximum=MAX_LINK_LENGTH),
			source=_bounded_text(filters.get("source"), label="la fuente", maximum=MAX_LINK_LENGTH),
			dedupe_status=dedupe_status,
			cv_processing_status=cv_processing_status,
			minimum_rating=minimum_rating,
			minimum_score=minimum_score,
			sort_by=sort_by,
			interview_queue=interview_queue,
			start=_integer(start, label="el inicio", default=0, minimum=0, maximum=1_000_000),
			page_length=_integer(
				page_length,
				label="el tamaño de página",
				default=50,
				minimum=1,
				maximum=MAX_PAGE_LENGTH,
			),
		)


@dataclass(frozen=True)
class BatchReviewRequest:
	applicant_names: tuple
	target_status: str
	reason: str
	job_title: str

	@classmethod
	def from_input(
		cls,
		applicant_names: Any,
		*,
		target_status: Any,
		reason: Any,
		job_title: Any,
	) -> BatchReviewRequest:
		if isinstance(applicant_names, str):
			try:
				applicant_names = json.loads(applicant_names)
			except json.JSONDecodeError as exc:
				raise CandidateReviewValidationError("La selección de candidatos no es JSON válido.") from exc
		if not isinstance(applicant_names, Sequence) or isinstance(applicant_names, str | bytes):
			raise CandidateReviewValidationError("La selección de candidatos no es válida.")

		unique_names = []
		seen = set()
		for raw_name in applicant_names:
			name = _bounded_text(
				raw_name, label="el identificador del candidato", maximum=MAX_LINK_LENGTH, required=True
			)
			if name not in seen:
				seen.add(name)
				unique_names.append(name)
		if not unique_names:
			raise CandidateReviewValidationError("Selecciona al menos un candidato.")
		if len(unique_names) > MAX_BATCH_SIZE:
			raise CandidateReviewValidationError("No puedes procesar más de 100 candidatos por lote.")

		target = _bounded_text(target_status, label="el estado destino", maximum=40, required=True)
		if target not in BATCH_TARGET_STATUSES:
			raise CandidateReviewValidationError(
				f"El estado {target} no está permitido en acciones masivas; Accepted requiere decisión individual."
			)

		return cls(
			applicant_names=tuple(unique_names),
			target_status=target,
			reason=_bounded_text(reason, label="un motivo", maximum=MAX_REASON_LENGTH, required=True),
			job_title=_bounded_text(job_title, label="una vacante", maximum=MAX_LINK_LENGTH, required=True),
		)


@dataclass(frozen=True)
class FilteredReviewRunRequest:
	filters: ReviewFilters
	target_status: str
	reason: str

	@classmethod
	def from_input(cls, filters: Any, *, target_status: Any, reason: Any) -> FilteredReviewRunRequest:
		review_filters = ReviewFilters.from_input(filters, start=0, page_length=MAX_PAGE_LENGTH)
		if not review_filters.job_title or not review_filters.status:
			raise CandidateReviewValidationError(
				"Para procesar todos los resultados debes fijar una vacante y un estado de origen."
			)
		target = _bounded_text(target_status, label="el estado destino", maximum=40, required=True)
		if target not in BATCH_TARGET_STATUSES:
			raise CandidateReviewValidationError(
				f"El estado {target} no está permitido en acciones masivas; Accepted requiere decisión individual."
			)
		if review_filters.status == target:
			raise CandidateReviewValidationError("El estado destino debe ser distinto del filtro de origen.")
		validate_transition(review_filters.status, target)
		return cls(
			filters=review_filters,
			target_status=target,
			reason=_bounded_text(reason, label="un motivo", maximum=MAX_REASON_LENGTH, required=True),
		)


def validate_transition(current_status: Any, target_status: Any) -> None:
	current = str(current_status or "").strip()
	target = str(target_status or "").strip()
	if current not in APPLICANT_STATUSES:
		raise CandidateReviewValidationError("El estado actual de la aplicación no es válido.")
	if target not in BATCH_TARGET_STATUSES:
		raise CandidateReviewValidationError("El estado destino no está permitido en Candidate Review.")
	if current == target:
		raise CandidateReviewValidationError("La aplicación ya tiene el mismo estado.")
	if current == "Accepted":
		raise CandidateReviewValidationError("Accepted es terminal y no puede cambiarse por lote.")
	if target not in ALLOWED_TRANSITIONS[current]:
		raise CandidateReviewValidationError(f"La transición de {current} a {target} no está permitida.")
