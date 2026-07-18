from __future__ import annotations

from functools import partial

import frappe
from frappe import _

from hrms.recruitment.matching import (
	DEDUPE_NEW,
	DEDUPE_REVIEW,
	candidate_lock_names,
	choose_profile_match,
	names_are_compatible,
	normalize_email,
	normalize_phone,
	requires_name_compatibility,
)

PROFILE_DOCTYPE = "AYP Candidate Profile"
STATUS_ACTIVE = "Activo"
LOCK_TIMEOUT_SECONDS = 10


def _release_candidate_locks(lock_names: tuple[str, ...]) -> None:
	for lock_name in reversed(lock_names):
		frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
	held_locks = getattr(frappe.local, "ayp_candidate_locks", set())
	held_locks.difference_update(lock_names)


def _acquire_candidate_locks(*, email: str, phone: str, cv_sha256: str) -> None:
	lock_names = candidate_lock_names(email=email, phone=phone, cv_sha256=cv_sha256)
	if not lock_names:
		return
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


def _profile_matches(fieldname: str, value: str) -> set[str]:
	if not value:
		return set()
	return set(
		frappe.get_all(
			PROFILE_DOCTYPE,
			filters={fieldname: value},
			pluck="name",
			limit_page_length=3,
		)
	)


def _cv_profile_matches(cv_sha256: str) -> set[str]:
	matches = _profile_matches("latest_cv_sha256", cv_sha256)
	if not cv_sha256 or not all(
		frappe.db.has_column("Job Applicant", fieldname)
		for fieldname in ("custom_cv_sha256", "custom_candidate_profile")
	):
		return matches
	for profile_name in frappe.get_all(
		"Job Applicant",
		filters={"custom_cv_sha256": cv_sha256, "custom_candidate_profile": ["!=", ""]},
		pluck="custom_candidate_profile",
		limit_page_length=3,
	):
		if profile_name:
			matches.add(profile_name)
	return matches


def _job_applicant_has_field(doc, fieldname: str) -> bool:
	return bool(getattr(doc, "meta", None) and doc.meta.has_field(fieldname))


def _set_if_supported(doc, fieldname: str, value) -> None:
	if _job_applicant_has_field(doc, fieldname):
		doc.set(fieldname, value)


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

	if not _job_applicant_has_field(doc, "custom_candidate_profile"):
		return

	existing_profile = doc.get("custom_candidate_profile")
	persisted_profile = None
	if not doc.is_new():
		persisted_profile = frappe.db.get_value("Job Applicant", doc.name, "custom_candidate_profile")
	if frappe.session.user == "Guest":
		existing_profile = None
		doc.set("custom_candidate_profile", None)
	elif (
		existing_profile
		and existing_profile == persisted_profile
		and frappe.db.exists(PROFILE_DOCTYPE, existing_profile)
	):
		return
	else:
		doc.set("custom_candidate_profile", None)

	email = normalize_email(doc.email_id)
	phone = normalize_phone(doc.phone_number)
	cv_sha256 = (doc.get("custom_cv_sha256") or "").strip().lower()
	_set_if_supported(doc, "custom_normalized_email", email)
	_set_if_supported(doc, "custom_normalized_phone", phone)
	if not getattr(frappe.flags, "in_candidate_profile_backfill", False):
		_acquire_candidate_locks(email=email, phone=phone, cv_sha256=cv_sha256)

	matches = {
		"email": _profile_matches("normalized_email", email),
		"phone": _profile_matches("normalized_phone", phone),
		"cv": _cv_profile_matches(cv_sha256),
	}
	profile_name, dedupe_status = choose_profile_match(matches)
	matching_signals = [signal for signal, names in matches.items() if profile_name and profile_name in names]
	if profile_name and requires_name_compatibility(matching_signals):
		existing_name = frappe.db.get_value(PROFILE_DOCTYPE, profile_name, "candidate_name")
		if not names_are_compatible(doc.applicant_name, existing_name):
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
		"custom_candidate_profile",
		"custom_normalized_email",
		"custom_normalized_phone",
		"custom_dedupe_status",
		"custom_cv_sha256",
	)
	if not all(frappe.db.has_column("Job Applicant", fieldname) for fieldname in required_fields):
		frappe.throw(_("Los campos canónicos de candidatos no están disponibles."))

	updated = 0
	previous_backfill_flag = getattr(frappe.flags, "in_candidate_profile_backfill", False)
	frappe.flags.in_candidate_profile_backfill = True
	try:
		for applicant_name in frappe.get_all("Job Applicant", pluck="name"):
			applicant = frappe.get_doc("Job Applicant", applicant_name)
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
				},
				update_modified=False,
			)
			if applicant.custom_dedupe_status == DEDUPE_REVIEW:
				frappe.db.set_value(PROFILE_DOCTYPE, profile_name, "dedupe_status", DEDUPE_REVIEW)
			sync_candidate_profile(applicant)
			updated += 1
	finally:
		frappe.flags.in_candidate_profile_backfill = previous_backfill_flag
	return updated


def sync_candidate_profile(doc, method=None) -> None:
	profile_name = doc.get("custom_candidate_profile")
	if not profile_name or not frappe.db.exists(PROFILE_DOCTYPE, profile_name):
		return

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
