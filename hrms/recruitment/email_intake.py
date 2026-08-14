from __future__ import annotations

import hashlib

import frappe

from hrms.recruitment.email_intake_domain import (
	EmailIntakeDomainError,
	same_vacancy_application,
	select_candidate_cv,
	sender_identity,
)
from hrms.recruitment.talent_pool import acquire_candidate_identity_lock
from hrms.security.candidate_cv import CandidateCVSecurityError, scan_stored_candidate_cv

RECRUITMENT_MAILBOX = "empleos@aroypedal.com"
DEFAULT_JOB_OPENING = "HR-OPN-2026-0001"
APPLICANT_SOURCE = "Email Recursos Humanos"
QUEUE_NAME = "documents"


def _configured_mailbox() -> str:
	return str(frappe.conf.get("ayp_recruitment_mailbox") or RECRUITMENT_MAILBOX).strip().casefold()


def _configured_job_opening() -> str:
	return str(frappe.conf.get("ayp_recruitment_job_opening") or DEFAULT_JOB_OPENING).strip()


def _is_recruitment_email(doc) -> bool:
	if (
		doc.sent_or_received != "Received"
		or doc.communication_medium != "Email"
		or not doc.email_account
		or not doc.has_attachment
	):
		return False
	email_id = frappe.db.get_value("Email Account", doc.email_account, "email_id")
	return str(email_id or "").strip().casefold() == _configured_mailbox()


def enqueue_recruitment_email_intake(doc, method=None) -> None:
	"""Queue one native inbound Communication after Frappe has saved attachments."""

	if not _is_recruitment_email(doc) or (doc.reference_doctype == "Job Applicant" and doc.reference_name):
		return
	callback_key = f"ayp-email-intake:{doc.name}"
	registered = getattr(frappe.local, "ayp_email_intake_callbacks", set())
	frappe.local.ayp_email_intake_callbacks = registered
	if callback_key in registered:
		return
	registered.add(callback_key)
	frappe.db.after_rollback.add(lambda: registered.discard(callback_key))

	def enqueue_after_commit():
		try:
			frappe.enqueue(
				"hrms.recruitment.email_intake.process_recruitment_email_safely",
				queue=QUEUE_NAME,
				timeout=600,
				job_id=callback_key,
				deduplicate=True,
				communication_name=doc.name,
			)
		finally:
			registered.discard(callback_key)

	frappe.db.after_commit.add(enqueue_after_commit)


def _candidate_files(communication_name: str) -> list[dict]:
	return frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": "Communication",
			"attached_to_name": communication_name,
			"is_private": 1,
		},
		fields=["name", "file_name", "file_url", "file_size", "creation"],
		order_by="creation asc, name asc",
		limit_page_length=20,
	)


def _matching_applications(job_opening: str, email: str, cv_sha256: str) -> list[dict]:
	return frappe.db.sql(
		"""
		SELECT name, applicant_name, custom_normalized_email, custom_cv_sha256
		FROM `tabJob Applicant`
		WHERE job_title = %s
			AND (
				custom_normalized_email = %s
				OR custom_cv_sha256 = %s
			)
		ORDER BY creation, name
		LIMIT 10 FOR UPDATE
		""",
		(job_opening, email, cv_sha256),
		as_dict=True,
	)


def _detach_for_candidate(file_doc) -> None:
	file_doc.db_set(
		{
			"attached_to_doctype": None,
			"attached_to_name": None,
			"attached_to_field": None,
		},
		update_modified=False,
	)


def _link_communication(communication, applicant_name: str) -> None:
	communication.db_set(
		{"reference_doctype": "Job Applicant", "reference_name": applicant_name},
		update_modified=False,
	)


def _assert_candidate_file_link(file_name: str, applicant_name: str) -> None:
	link = frappe.db.get_value(
		"File",
		file_name,
		["attached_to_doctype", "attached_to_name", "attached_to_field"],
	)
	if tuple(link or ()) != ("Job Applicant", applicant_name, "resume_attachment"):
		raise EmailIntakeDomainError("El CV no quedó vinculado de forma privada a la solicitud.")


def _new_applicant_data(*, applicant_name: str, email: str, job_opening: str, resume_attachment: str) -> dict:
	"""Build an email-origin application without inferring future-contact consent."""

	return {
		"doctype": "Job Applicant",
		"status": "Open",
		"applicant_name": applicant_name,
		"email_id": email,
		"job_title": job_opening,
		"source": APPLICANT_SOURCE,
		"resume_attachment": resume_attachment,
		"custom_data_processing_consent": 0,
		"custom_privacy_notice_version": "",
	}


def process_recruitment_email(communication_name: str) -> dict:
	"""Create or update one same-vacancy Job Applicant from a native email."""

	frappe.db.sql(
		"SELECT name FROM `tabCommunication` WHERE name = %s FOR UPDATE",
		(communication_name,),
	)
	communication = frappe.get_doc("Communication", communication_name, for_update=True)
	if not _is_recruitment_email(communication):
		return {"status": "ignored"}
	if communication.reference_doctype == "Job Applicant" and communication.reference_name:
		return {"status": "already_processed", "applicant": communication.reference_name}

	job_opening = _configured_job_opening()
	if not frappe.db.exists("Job Opening", {"name": job_opening, "status": "Open"}):
		raise EmailIntakeDomainError("La vacante configurada no existe o no está abierta.")

	email, applicant_name = sender_identity(communication.sender, communication.sender_full_name)
	file_row = select_candidate_cv(_candidate_files(communication.name))
	file_doc = frappe.get_doc("File", file_row["name"], for_update=True)
	cv_sha256 = scan_stored_candidate_cv(file_doc)

	# Serialize same-person/same-vacancy lookup with the canonical profile linker.
	acquire_candidate_identity_lock()
	existing_name = same_vacancy_application(
		_matching_applications(job_opening, email, cv_sha256),
		email=email,
		cv_sha256=cv_sha256,
		applicant_name=applicant_name,
	)

	if existing_name:
		applicant = frappe.get_doc("Job Applicant", existing_name, for_update=True)
		if (applicant.custom_cv_sha256 or "") == cv_sha256:
			_link_communication(communication, applicant.name)
			return {
				"status": "duplicate_message",
				"applicant": applicant.name,
				"communication": communication.name,
				"cv_sha256": cv_sha256,
			}
		_detach_for_candidate(file_doc)
		applicant.resume_attachment = file_doc.file_url
		applicant.save(ignore_permissions=True)
		_assert_candidate_file_link(file_doc.name, applicant.name)
		status = "updated"
	else:
		_detach_for_candidate(file_doc)
		applicant = frappe.get_doc(
			_new_applicant_data(
				applicant_name=applicant_name,
				email=email,
				job_opening=job_opening,
				resume_attachment=file_doc.file_url,
			)
		).insert(ignore_permissions=True)
		_assert_candidate_file_link(file_doc.name, applicant.name)
		status = "created"

	_link_communication(communication, applicant.name)
	return {
		"status": status,
		"applicant": applicant.name,
		"communication": communication.name,
		"cv_sha256": cv_sha256,
	}


def process_recruitment_email_safely(communication_name: str) -> dict:
	"""Optional manual wrapper that records a PII-free error title for triage."""

	try:
		return process_recruitment_email(communication_name)
	except (CandidateCVSecurityError, EmailIntakeDomainError):
		frappe.db.rollback()
		fingerprint = hashlib.sha256(communication_name.encode()).hexdigest()[:12]
		frappe.log_error(
			title=f"Recruitment email intake blocked {fingerprint}",
			message=frappe.get_traceback(),
		)
		raise
