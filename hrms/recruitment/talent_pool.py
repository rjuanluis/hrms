from __future__ import annotations

from functools import partial

import frappe
from frappe import _

from hrms.recruitment.matching import (
	DEDUPE_NEW,
	DEDUPE_REVIEW,
	EMAIL_RECRUITMENT_SOURCE,
	candidate_lock_names,
	choose_profile_match,
	has_email_recruitment_provenance,
	names_are_compatible,
	normalize_email,
	normalize_phone,
	requires_name_compatibility,
	should_enroll_in_talent_pool,
)

PROFILE_DOCTYPE = "AYP Candidate Profile"
STATUS_ACTIVE = "Activo"
LOCK_TIMEOUT_SECONDS = 10
GLOBAL_CANDIDATE_LOCK = "ayp-candidate-pool-global"
MAX_PROFILE_REDIRECTS = 20
EMAIL_PROVENANCE_MARKER_FIELD = "custom_ayp_email_provenance"
EMAIL_CV_FILE_FIELD = "custom_ayp_email_file_name"
EMAIL_MESSAGE_KEY_FIELD = "custom_ayp_email_message_id"
EMAIL_CONSENT_EVIDENCE_FIELD = "custom_ayp_email_consent_evidence_sha256"
EMAIL_IMMUTABLE_FIELDS = (
	EMAIL_PROVENANCE_MARKER_FIELD,
	"job_title",
	"resume_attachment",
	"custom_cv_sha256",
	"custom_privacy_notice_version",
	EMAIL_CV_FILE_FIELD,
	EMAIL_MESSAGE_KEY_FIELD,
	"custom_ayp_email_received_on",
	"custom_ayp_email_subject",
	"custom_ayp_email_current_vacancy_consent",
	"custom_ayp_email_consent_notice_version",
	EMAIL_CONSENT_EVIDENCE_FIELD,
)


def _release_candidate_locks(lock_names: tuple[str, ...]) -> None:
	for lock_name in reversed(lock_names):
		frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
	held_locks = getattr(frappe.local, "ayp_candidate_locks", set())
	held_locks.difference_update(lock_names)


def _acquire_candidate_locks(*, email: str, phone: str, cv_sha256: str) -> None:
	lock_names = (GLOBAL_CANDIDATE_LOCK, *candidate_lock_names(email=email, phone=phone, cv_sha256=cv_sha256))
	held_locks = getattr(frappe.local, "ayp_candidate_locks", set())
	frappe.local.ayp_candidate_locks = held_locks
	new_locks = tuple(lock_name for lock_name in lock_names if lock_name not in held_locks)
	acquired = []
	for lock_name in new_locks:
		result = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock_name, LOCK_TIMEOUT_SECONDS))
		if not result or result[0][0] != 1:
			_release_candidate_locks(tuple(acquired))
			frappe.throw(
				_("No pudimos procesar la solicitud en este momento. Intenta nuevamente."),
				frappe.ValidationError,
			)
		acquired.append(lock_name)
	if not acquired:
		return
	held_locks.update(acquired)
	release = partial(_release_candidate_locks, tuple(acquired))
	frappe.db.after_commit.add(release)
	frappe.db.after_rollback.add(release)


def acquire_candidate_identity_lock() -> None:
	"""Serialize manual identity corrections with automatic intake matching."""

	_acquire_candidate_locks(email="", phone="", cv_sha256="")


def resolve_candidate_profile(profile_name: str | None, *, for_update: bool = False):
	"""Resolve a durable merge redirect and return the live profile row.

	Rows in a redirect chain remain immutable aliases for historical audit and
	identifier matching. Callers only receive the terminal profile.
	"""

	current = str(profile_name or "").strip()
	seen = set()
	for _redirect_index in range(MAX_PROFILE_REDIRECTS):
		if not current or current in seen:
			frappe.throw(_("La cadena de perfiles fusionados no es válida."), frappe.ValidationError)
		seen.add(current)
		# `lock_clause` is selected from fixed SQL keywords; the profile name
		# remains a separately bound parameter.
		rows = frappe.db.sql(  # nosemgrep
			"""
			SELECT name, candidate_name, merged_into, do_not_contact
			FROM `tabAYP Candidate Profile`
			WHERE name = %s{lock_clause}
			""".format(lock_clause=" FOR UPDATE" if for_update else ""),
			(current,),
			as_dict=True,
		)
		if not rows:
			return None
		row = rows[0]
		if not row.merged_into:
			return row
		current = row.merged_into
	frappe.throw(_("La cadena de perfiles fusionados excede el límite permitido."), frappe.ValidationError)


def _profile_matches(fieldname: str, value: str) -> set[str]:
	if not value:
		return set()
	queries = {
		"normalized_email": """
			SELECT name FROM `tabAYP Candidate Profile`
			WHERE normalized_email = %s LIMIT 3 FOR UPDATE
		""",
		"normalized_phone": """
			SELECT name FROM `tabAYP Candidate Profile`
			WHERE normalized_phone = %s LIMIT 3 FOR UPDATE
		""",
		"latest_cv_sha256": """
			SELECT name FROM `tabAYP Candidate Profile`
			WHERE latest_cv_sha256 = %s LIMIT 3 FOR UPDATE
		""",
	}
	if fieldname not in queries:
		raise ValueError(f"Unsupported candidate profile match field: {fieldname}")
	matches = set()
	for profile_name in frappe.db.sql(queries[fieldname], (value,), pluck=True):
		resolved = resolve_candidate_profile(profile_name, for_update=True)
		if resolved:
			matches.add(resolved.name)
	return matches


def _cv_profile_matches(cv_sha256: str) -> set[str]:
	matches = _profile_matches("latest_cv_sha256", cv_sha256)
	if not cv_sha256 or not all(
		frappe.db.has_column("Job Applicant", fieldname)
		for fieldname in ("custom_cv_sha256", "custom_candidate_profile")
	):
		return matches
	for profile_name in frappe.db.sql(
		"""
		SELECT custom_candidate_profile FROM `tabJob Applicant`
		WHERE custom_cv_sha256 = %s AND COALESCE(custom_candidate_profile, '') != ''
		LIMIT 3 FOR UPDATE
		""",
		(cv_sha256,),
		pluck=True,
	):
		if profile_name:
			resolved = resolve_candidate_profile(profile_name, for_update=True)
			if resolved:
				matches.add(resolved.name)
	return matches


def _persisted_applicant_for_update(applicant_name: str):
	rows = frappe.db.sql(
		"""
		SELECT custom_candidate_profile, custom_normalized_email,
			custom_normalized_phone, custom_cv_sha256, custom_dedupe_status
		FROM `tabJob Applicant` WHERE name = %s FOR UPDATE
		""",
		(applicant_name,),
		as_dict=True,
	)
	return rows[0] if rows else None


def _profile_name_for_update(profile_name: str | None):
	if not profile_name:
		return None
	return resolve_candidate_profile(profile_name, for_update=True)


def _job_applicant_has_field(doc, fieldname: str) -> bool:
	return bool(getattr(doc, "meta", None) and doc.meta.has_field(fieldname))


def _set_if_supported(doc, fieldname: str, value) -> None:
	if _job_applicant_has_field(doc, fieldname):
		doc.set(fieldname, value)


def job_applicant_has_email_provenance(doc) -> bool:
	return has_email_recruitment_provenance(
		source=doc.get("source"),
		email_provenance=bool(doc.get(EMAIL_PROVENANCE_MARKER_FIELD)),
		email_file_name=doc.get(EMAIL_CV_FILE_FIELD),
		graph_message_key=doc.get(EMAIL_MESSAGE_KEY_FIELD),
		consent_evidence_sha256=doc.get(EMAIL_CONSENT_EVIDENCE_FIELD),
	)


def _doc_before_save(doc):
	getter = getattr(doc, "get_doc_before_save", None)
	return getter() if callable(getter) and not doc.is_new() else None


def validate_email_provenance(doc) -> None:
	"""Keep email origin and its current-vacancy evidence immutable server-side."""

	previous = _doc_before_save(doc)
	previous_is_email = bool(previous and job_applicant_has_email_provenance(previous))
	current_is_email = job_applicant_has_email_provenance(doc)
	if previous_is_email and previous is not None:
		if doc.get("source") != EMAIL_RECRUITMENT_SOURCE:
			frappe.throw(
				_("La procedencia de una solicitud recibida por correo es inmutable."), frappe.ValidationError
			)
		for fieldname in EMAIL_IMMUTABLE_FIELDS:
			previous_value = previous.get(fieldname)
			if doc.get(fieldname) != previous_value:
				frappe.throw(
					_("La evidencia de procedencia y consentimiento por correo es inmutable."),
					frappe.ValidationError,
				)
		current_is_email = True
	elif current_is_email and previous:
		frappe.throw(
			_("Una solicitud existente no puede convertirse manualmente en una solicitud por correo."),
			frappe.ValidationError,
		)

	if not current_is_email:
		return
	_set_if_supported(doc, EMAIL_PROVENANCE_MARKER_FIELD, 1)
	if doc.get("custom_data_processing_consent") in (True, 1, "1"):
		frappe.throw(
			_(
				"Las solicitudes por correo requieren un flujo separado y auditable para futuras oportunidades."
			),
			frappe.ValidationError,
		)
	_set_if_supported(doc, "custom_candidate_profile", None)
	_set_if_supported(doc, "custom_dedupe_status", None)


def should_link_job_applicant_profile(doc) -> bool:
	return should_enroll_in_talent_pool(
		source=doc.get("source"),
		has_data_processing_consent=bool(doc.get("custom_data_processing_consent")),
		email_provenance=bool(doc.get(EMAIL_PROVENANCE_MARKER_FIELD)),
		email_file_name=doc.get(EMAIL_CV_FILE_FIELD),
		graph_message_key=doc.get(EMAIL_MESSAGE_KEY_FIELD),
		consent_evidence_sha256=doc.get(EMAIL_CONSENT_EVIDENCE_FIELD),
	)


def validate_email_future_consent(doc) -> None:
	"""Reject manufactured future-opportunity consent for email applicants."""

	consent_value = doc.get("custom_data_processing_consent")
	if job_applicant_has_email_provenance(doc) and consent_value in (True, 1, "1"):
		frappe.throw(
			_(
				"Las solicitudes por correo requieren un flujo separado y auditable para futuras oportunidades."
			),
			frappe.ValidationError,
		)


def _create_candidate_profile(doc, *, email: str, phone: str, cv_sha256: str, dedupe_status: str) -> str:
	profile = frappe.get_doc(
		{
			"doctype": PROFILE_DOCTYPE,
			"candidate_name": (doc.applicant_name or "").strip(),
			"primary_email": (doc.email_id or "").strip(),
			"primary_phone": (doc.phone_number or "").strip(),
			"normalized_email": email,
			"normalized_phone": phone,
			"latest_cv_sha256": cv_sha256,
			"privacy_notice_version": doc.get("custom_privacy_notice_version") or "",
			"talent_pool_status": STATUS_ACTIVE,
			"dedupe_status": dedupe_status,
		}
	)
	profile.insert(ignore_permissions=True)
	return profile.name


def link_job_applicant_profile(doc, method=None) -> None:
	"""Link every application to one canonical talent-pool profile.

	Guest-supplied links are ignored. Conflicting identifiers create a separate
	profile flagged for human review rather than risking an incorrect merge.
	"""

	validate_email_provenance(doc)
	validate_email_future_consent(doc)
	if not _job_applicant_has_field(doc, "custom_candidate_profile"):
		return

	email = normalize_email(doc.email_id)
	phone = normalize_phone(doc.phone_number)
	cv_sha256 = (doc.get("custom_cv_sha256") or "").strip().lower()
	_set_if_supported(doc, "custom_normalized_email", email)
	_set_if_supported(doc, "custom_normalized_phone", phone)
	# The installed AyP site governs every real applicant.  Upstream HRMS's
	# integration suite also creates generic fixtures through these global hooks;
	# do not silently convert those fixtures into AyP workflow records unless a
	# test explicitly opts in by setting the field itself.
	if not frappe.flags.in_test or doc.get("custom_ayp_governed"):
		_set_if_supported(doc, "custom_ayp_governed", 1)
	# A sender who emails a CV has applied to this vacancy, but has not consented
	# to reuse in the broader talent pool. Never trust or retain a supplied link.
	if not should_link_job_applicant_profile(doc):
		doc.set("custom_candidate_profile", None)
		_set_if_supported(doc, "custom_dedupe_status", None)
		return
	_acquire_candidate_locks(email=email, phone=phone, cv_sha256=cv_sha256)

	persisted = None if doc.is_new() else _persisted_applicant_for_update(doc.name)
	persisted_profile = persisted.custom_candidate_profile if persisted else None
	persisted_profile_row = _profile_name_for_update(persisted_profile)
	if persisted and persisted_profile_row:
		identity_changed = any(
			(
				(persisted.custom_normalized_email or "") != email,
				(persisted.custom_normalized_phone or "") != phone,
				(persisted.custom_cv_sha256 or "").strip().lower() != cv_sha256,
			)
		)
		persisted_profile = persisted_profile_row.name
		doc.set("custom_candidate_profile", persisted_profile)
		if identity_changed:
			_set_if_supported(doc, "custom_dedupe_status", DEDUPE_REVIEW)
			frappe.db.set_value(
				PROFILE_DOCTYPE,
				persisted_profile,
				"dedupe_status",
				DEDUPE_REVIEW,
				update_modified=False,
			)
		else:
			_set_if_supported(doc, "custom_dedupe_status", persisted.custom_dedupe_status or DEDUPE_NEW)
		return

	# New or previously unlinked applications never trust a supplied link.
	doc.set("custom_candidate_profile", None)

	matches = {
		"email": _profile_matches("normalized_email", email),
		"phone": _profile_matches("normalized_phone", phone),
		"cv": _cv_profile_matches(cv_sha256),
	}
	profile_name, dedupe_status = choose_profile_match(matches)
	matching_signals = [signal for signal, names in matches.items() if profile_name and profile_name in names]
	if profile_name and requires_name_compatibility(matching_signals):
		profile_row = _profile_name_for_update(profile_name)
		if not profile_row or not names_are_compatible(doc.applicant_name, profile_row.candidate_name):
			profile_name, dedupe_status = None, DEDUPE_REVIEW
	if not profile_name:
		profile_name = _create_candidate_profile(
			doc,
			email=email,
			phone=phone,
			cv_sha256=cv_sha256,
			dedupe_status=dedupe_status,
		)

	doc.set("custom_candidate_profile", profile_name)
	_set_if_supported(doc, "custom_dedupe_status", dedupe_status)


def backfill_candidate_profiles() -> int:
	if not frappe.db.exists("DocType", PROFILE_DOCTYPE):
		frappe.throw(_("AYP Candidate Profile no está disponible; ejecuta bench migrate primero."))
	required_fields = (
		EMAIL_PROVENANCE_MARKER_FIELD,
		"custom_candidate_profile",
		"custom_normalized_email",
		"custom_normalized_phone",
		"custom_dedupe_status",
		"custom_cv_sha256",
	)
	if not all(frappe.db.has_column("Job Applicant", fieldname) for fieldname in required_fields):
		frappe.throw(_("Los campos canónicos de candidatos no están disponibles."))

	_acquire_candidate_locks(email="", phone="", cv_sha256="")
	updated = 0
	for applicant_name in frappe.get_all("Job Applicant", pluck="name"):
		applicant = frappe.get_doc("Job Applicant", applicant_name)
		if job_applicant_has_email_provenance(applicant):
			stale_profile = applicant.get("custom_candidate_profile")
			if stale_profile and frappe.db.exists(PROFILE_DOCTYPE, stale_profile):
				linked_count = frappe.db.count("Job Applicant", {"custom_candidate_profile": stale_profile})
				if linked_count != 1:
					frappe.throw(
						_(
							"Un perfil compartido contiene procedencia de correo; requiere remediación manual antes de continuar."
						),
						frappe.ValidationError,
					)
			frappe.db.set_value(
				"Job Applicant",
				applicant.name,
				{
					"source": EMAIL_RECRUITMENT_SOURCE,
					EMAIL_PROVENANCE_MARKER_FIELD: 1,
					"custom_data_processing_consent": 0,
					"custom_candidate_profile": None,
					"custom_dedupe_status": None,
				},
				update_modified=False,
			)
			if stale_profile and frappe.db.exists(PROFILE_DOCTYPE, stale_profile):
				frappe.delete_doc(PROFILE_DOCTYPE, stale_profile, ignore_permissions=True)
			updated += 1
			continue
		if applicant.get("custom_candidate_profile"):
			continue
		if applicant.resume_attachment and frappe.db.has_column("File", "custom_cv_sha256"):
			applicant.custom_cv_sha256 = (
				frappe.db.get_value("File", {"file_url": applicant.resume_attachment}, "custom_cv_sha256")
				or ""
			)
		link_job_applicant_profile(applicant)
		if applicant.resume_attachment and not applicant.custom_cv_sha256:
			applicant.custom_dedupe_status = DEDUPE_REVIEW
		profile_name = applicant.get("custom_candidate_profile")
		if not profile_name and not should_link_job_applicant_profile(applicant):
			frappe.db.set_value(
				"Job Applicant",
				applicant.name,
				{
					"custom_candidate_profile": None,
					"custom_normalized_email": applicant.custom_normalized_email,
					"custom_normalized_phone": applicant.custom_normalized_phone,
					"custom_dedupe_status": None,
					"custom_ayp_governed": 1,
				},
				update_modified=False,
			)
			continue
		if not profile_name:
			frappe.throw(_("No se pudo crear el perfil canónico para {0}.").format(applicant.name))
		frappe.db.set_value(
			"Job Applicant",
			applicant.name,
			{
				"custom_candidate_profile": profile_name,
				"custom_normalized_email": applicant.custom_normalized_email,
				"custom_normalized_phone": applicant.custom_normalized_phone,
				"custom_dedupe_status": applicant.custom_dedupe_status or DEDUPE_NEW,
				"custom_cv_sha256": applicant.custom_cv_sha256 or "",
				"custom_ayp_governed": 1,
			},
			update_modified=False,
		)
		if applicant.custom_dedupe_status == DEDUPE_REVIEW:
			frappe.db.set_value(PROFILE_DOCTYPE, profile_name, "dedupe_status", DEDUPE_REVIEW)
		sync_candidate_profile(applicant)
		updated += 1
	return updated


def sync_candidate_profile(doc, method=None) -> None:
	profile_name = doc.get("custom_candidate_profile")
	if not profile_name or not frappe.db.exists(PROFILE_DOCTYPE, profile_name):
		return
	resolved = resolve_candidate_profile(profile_name, for_update=True)
	if not resolved:
		return
	profile_name = resolved.name
	if doc.get("custom_candidate_profile") != profile_name:
		doc.custom_candidate_profile = profile_name
		frappe.db.set_value(
			"Job Applicant", doc.name, "custom_candidate_profile", profile_name, update_modified=False
		)

	profile = frappe.get_doc(PROFILE_DOCTYPE, profile_name)
	changed = False
	if not profile.primary_email and doc.email_id:
		profile.primary_email = doc.email_id.strip()
		profile.normalized_email = normalize_email(doc.email_id)
		changed = True
	if not profile.primary_phone and doc.phone_number:
		profile.primary_phone = doc.phone_number.strip()
		profile.normalized_phone = normalize_phone(doc.phone_number)
		changed = True
	cv_sha256 = (doc.get("custom_cv_sha256") or "").strip().lower()
	if cv_sha256 and profile.latest_cv_sha256 != cv_sha256:
		profile.latest_cv_sha256 = cv_sha256
		changed = True
	if profile.latest_application != doc.name:
		profile.latest_application = doc.name
		changed = True
	application_count = frappe.db.count("Job Applicant", {"custom_candidate_profile": profile_name})
	if profile.application_count != application_count:
		profile.application_count = application_count
		changed = True
	if changed:
		profile.save(ignore_permissions=True)
