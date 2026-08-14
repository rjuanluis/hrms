from __future__ import annotations

from email.utils import parseaddr
from pathlib import Path

EMAIL_CV_EXTENSIONS = frozenset({".pdf", ".docx"})


class EmailIntakeDomainError(ValueError):
	pass


class EmailIntakeReviewRequired(EmailIntakeDomainError):
	"""A clean message that requires an explicit human identity decision."""


def sender_identity(sender: str | None, sender_full_name: str | None = None) -> tuple[str, str]:
	"""Return a normalized sender email and a conservative display name."""

	parsed_name, parsed_email = parseaddr(str(sender or ""))
	email = parsed_email.strip().casefold()
	if not email or "@" not in email:
		raise EmailIntakeDomainError("El remitente no contiene un correo válido.")
	name = " ".join(str(sender_full_name or parsed_name or "").split())
	if not name:
		local_part = email.split("@", 1)[0].replace(".", " ").replace("_", " ").replace("-", " ")
		name = " ".join(part.capitalize() for part in local_part.split())
	if not name:
		raise EmailIntakeDomainError("No se pudo determinar el nombre del candidato.")
	return email, name[:140]


def select_candidate_cv(file_rows: list[dict]) -> dict:
	"""Select exactly one PDF/DOCX and ignore inline/signature images."""

	candidates = [
		row
		for row in file_rows
		if Path(str(row.get("file_name") or "")).suffix.casefold() in EMAIL_CV_EXTENSIONS
	]
	if not candidates:
		raise EmailIntakeDomainError("El correo no contiene un CV PDF o DOCX.")
	if len(candidates) != 1:
		raise EmailIntakeDomainError("El correo contiene más de un posible CV PDF/DOCX.")
	return candidates[0]


def same_vacancy_application(
	rows: list[dict], *, email: str, cv_sha256: str, applicant_name: str
) -> str | None:
	"""Never infer authorship from sender-controlled email/CV signals."""

	matched = {}
	for row in rows:
		signals = {
			signal
			for signal, agrees in {
				"email": email and row.get("custom_normalized_email") == email,
				"cv": cv_sha256 and row.get("custom_cv_sha256") == cv_sha256,
			}.items()
			if agrees
		}
		if signals:
			matched[str(row.get("name"))] = (row, signals)
	if not matched:
		return None
	if len(matched) != 1:
		raise EmailIntakeReviewRequired(
			"Las señales del candidato coinciden con varias solicitudes de la misma vacante."
		)
	raise EmailIntakeReviewRequired(
		"El remitente coincide con otra solicitud; la identidad requiere revisión manual."
	)
