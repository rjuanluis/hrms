from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping

DEDUPE_NEW = "Nuevo"
DEDUPE_MATCHED = "Coincidencia"
DEDUPE_REVIEW = "Revisión requerida"
EMAIL_RECRUITMENT_SOURCE = "Email Recursos Humanos"


def should_enroll_in_talent_pool(*, source: str | None, has_data_processing_consent: bool) -> bool:
	"""Keep every email application out until a dedicated future-consent flow exists."""

	return source != EMAIL_RECRUITMENT_SOURCE


def candidate_lock_names(*, email: str, phone: str, cv_sha256: str) -> tuple[str, ...]:
	signals = {"email": email, "phone": phone, "cv": cv_sha256}
	return tuple(
		sorted(
			f"ayp-candidate-{kind}-{hashlib.sha256(value.encode()).hexdigest()[:32]}"
			for kind, value in signals.items()
			if value
		)
	)


def requires_name_compatibility(matching_signals: list[str]) -> bool:
	return len(matching_signals) == 1


def normalize_email(value: str | None) -> str:
	return (value or "").strip().casefold()


def normalize_phone(value: str | None) -> str:
	raw = (value or "").strip()
	digits = re.sub(r"\D", "", raw)
	if not digits:
		return ""
	if digits.startswith("00"):
		digits = digits[2:]
	if len(digits) == 10:
		return f"+1{digits}"
	if len(digits) == 11 and digits.startswith("1"):
		return f"+{digits}"
	if raw.startswith("+") and 7 <= len(digits) <= 15:
		return f"+{digits}"
	return digits


def normalize_name(value: str | None) -> str:
	text = unicodedata.normalize("NFKD", (value or "").casefold())
	text = "".join(character for character in text if not unicodedata.combining(character))
	return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def names_are_compatible(left: str | None, right: str | None) -> bool:
	left_tokens = set(normalize_name(left).split())
	right_tokens = set(normalize_name(right).split())
	if not left_tokens or not right_tokens:
		return False
	if left_tokens == right_tokens:
		return True
	common = left_tokens & right_tokens
	return len(common) >= 2 and len(common) / min(len(left_tokens), len(right_tokens)) >= 0.75


def choose_profile_match(matches: Mapping[str, set[str]]) -> tuple[str | None, str]:
	"""Return one unambiguous profile or require review without merging records."""

	non_empty = [names for names in matches.values() if names]
	if not non_empty:
		return None, DEDUPE_NEW
	all_names = set().union(*non_empty)
	if len(all_names) == 1 and all(len(names) == 1 for names in non_empty):
		return next(iter(all_names)), DEDUPE_MATCHED
	return None, DEDUPE_REVIEW
