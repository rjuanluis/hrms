from __future__ import annotations

import re
import unicodedata

AUTHORIZED_JOB_OPENING = "HR-OPN-2026-0001"
VACANCY_INTRODUCER_PATTERN = re.compile(
	r"HR-OPN-",
	re.IGNORECASE | re.ASCII,
)


def _is_token_continuation(character: str) -> bool:
	category = unicodedata.category(character)
	return (
		character.isalnum()
		or character == "_"
		or category in {"Pc", "Pd"}
		or category.startswith("M")
		or category == "Cf"
	)


def extract_explicit_vacancy_references(subject: str) -> tuple[str, ...]:
	"""Return every ASCII HR-OPN- reference, including malformed tokens."""

	references: list[str] = []
	for match in VACANCY_INTRODUCER_PATTERN.finditer(subject):
		end = match.end()
		while end < len(subject) and _is_token_continuation(subject[end]):
			end += 1
		reference = subject[match.start() : end].upper()
		if match.start() > 0 and _is_token_continuation(subject[match.start() - 1]):
			reference = f"!{reference}"
		references.append(reference)
	return tuple(references)


def subject_has_only_authorized_vacancy_references(subject: str) -> bool:
	"""Allow no reference or only complete references to the authorized opening."""

	return all(
		reference == AUTHORIZED_JOB_OPENING for reference in extract_explicit_vacancy_references(subject)
	)
