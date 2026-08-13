from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

MAX_EVIDENCE_LENGTH = 500
MIN_EVIDENCE_LENGTH = 20


class CandidateScoringValidationError(ValueError):
	pass


@dataclass(frozen=True)
class CriterionDefinition:
	key: str
	label: str
	weight: int
	description: str


DEFAULT_CRITERIA = (
	CriterionDefinition(
		"minimum_requirements",
		"Requisitos mínimos",
		30,
		"Cumplimiento comprobable de requisitos indispensables para la vacante.",
	),
	CriterionDefinition(
		"relevant_experience",
		"Experiencia relevante",
		25,
		"Experiencia aplicable a las responsabilidades reales del puesto.",
	),
	CriterionDefinition(
		"functional_skills",
		"Habilidades funcionales",
		20,
		"Dominio demostrado de las habilidades técnicas u operativas necesarias.",
	),
	CriterionDefinition(
		"service_communication",
		"Servicio y comunicación",
		15,
		"Claridad, trato y orientación al cliente o al equipo.",
	),
	CriterionDefinition(
		"availability_conditions",
		"Disponibilidad y condiciones",
		10,
		"Compatibilidad con horario, ubicación y condiciones informadas de la vacante.",
	),
)
CRITERIA_BY_KEY = {criterion.key: criterion for criterion in DEFAULT_CRITERIA}


@dataclass(frozen=True)
class ScoreRow:
	criterion_key: str
	criterion_label: str
	weight: int
	rating: int
	weighted_score: float
	evidence: str


@dataclass(frozen=True)
class Scorecard:
	rows: tuple
	total_score: float
	recommendation: str
	explanation: str

	@classmethod
	def from_input(cls, value: Any) -> "Scorecard":
		if isinstance(value, str):
			try:
				value = json.loads(value)
			except json.JSONDecodeError as exc:
				raise CandidateScoringValidationError("El scorecard no es JSON válido.") from exc
		if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
			raise CandidateScoringValidationError("El scorecard debe contener una lista de criterios.")

		input_by_key = {}
		for raw_row in value:
			if not isinstance(raw_row, Mapping):
				raise CandidateScoringValidationError("Cada criterio debe ser un objeto.")
			key = str(raw_row.get("criterion_key") or "").strip()
			if key in input_by_key:
				raise CandidateScoringValidationError("El scorecard contiene un criterio duplicado.")
			if key not in CRITERIA_BY_KEY:
				raise CandidateScoringValidationError("El scorecard contiene un criterio desconocido.")
			input_by_key[key] = raw_row

		if set(input_by_key) != set(CRITERIA_BY_KEY):
			raise CandidateScoringValidationError("Debes evaluar todos los criterios del scorecard AyP.")

		rows = []
		for criterion in DEFAULT_CRITERIA:
			raw_row = input_by_key[criterion.key]
			try:
				rating = int(raw_row.get("rating"))
			except (TypeError, ValueError) as exc:
				raise CandidateScoringValidationError(
					"La calificación de {0} debe ser un entero.".format(criterion.label)
				) from exc
			if rating < 0 or rating > 5:
				raise CandidateScoringValidationError("Las calificaciones deben estar entre 0 y 5.")
			evidence = str(raw_row.get("evidence") or "").strip()
			if len(evidence) < MIN_EVIDENCE_LENGTH:
				raise CandidateScoringValidationError(
					"La evidencia de {0} debe describir una observación comprobable de al menos {1} caracteres.".format(
						criterion.label, MIN_EVIDENCE_LENGTH
					)
				)
			if len(evidence) > MAX_EVIDENCE_LENGTH:
				raise CandidateScoringValidationError(
					"La evidencia de {0} no puede exceder {1} caracteres.".format(
						criterion.label, MAX_EVIDENCE_LENGTH
					)
				)
			weighted_score = round(criterion.weight * rating / 5, 2)
			rows.append(
				ScoreRow(
					criterion_key=criterion.key,
					criterion_label=criterion.label,
					weight=criterion.weight,
					rating=rating,
					weighted_score=weighted_score,
					evidence=evidence,
				)
			)

		total_score = round(sum(row.weighted_score for row in rows), 2)
		if total_score >= 80:
			recommendation = "Recomendado para shortlist"
		elif total_score >= 60:
			recommendation = "Revisión comparativa"
		else:
			recommendation = "No priorizar"
		explanation = "; ".join(
			"{0}: {1}/5 × {2}% = {3:.1f}".format(
				row.criterion_label,
				row.rating,
				row.weight,
				row.weighted_score,
			)
			for row in rows
		)
		return cls(
			rows=tuple(rows),
			total_score=total_score,
			recommendation=recommendation,
			explanation=explanation,
		)
