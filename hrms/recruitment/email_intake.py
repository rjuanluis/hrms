from __future__ import annotations

import hashlib
import uuid

import frappe
from frappe.utils import add_to_date, now_datetime

from hrms.recruitment.email_intake_domain import (
	EmailIntakeDomainError,
	EmailIntakeReviewRequired,
	same_vacancy_application,
	select_candidate_cv,
	sender_identity,
)
from hrms.recruitment.talent_pool import acquire_candidate_identity_lock
from hrms.security.candidate_cv import (
	CandidateCVInfrastructureError,
	CandidateCVSecurityError,
	candidate_cv_file_identity,
	scan_stored_candidate_cv,
)

RECRUITMENT_MAILBOX = "empleos@aroypedal.com"
DEFAULT_JOB_OPENING = "HR-OPN-2026-0001"
APPLICANT_SOURCE = "Email Recursos Humanos"
QUEUE_NAME = "documents"
INTAKE_PENDING = "Pendiente"
INTAKE_PROCESSING = "Procesando"
INTAKE_COMPLETED = "Completado"
INTAKE_BLOCKED = "Bloqueado"
INTAKE_STATUS_FIELD = "custom_ayp_email_intake_status"
INTAKE_STALE_MINUTES = 15
ALLOWED_RECRUITMENT_IMAP_FOLDERS = frozenset({"inbox"})


def _configured_mailbox() -> str:
	return str(frappe.conf.get("ayp_recruitment_mailbox") or RECRUITMENT_MAILBOX).strip().casefold()


def _configured_job_opening() -> str:
	return str(frappe.conf.get("ayp_recruitment_job_opening") or DEFAULT_JOB_OPENING).strip()


def _is_recruitment_mailbox_message(doc) -> bool:
	if doc.sent_or_received != "Received" or doc.communication_medium != "Email" or not doc.email_account:
		return False
	email_id = frappe.db.get_value("Email Account", doc.email_account, "email_id")
	return str(email_id or "").strip().casefold() == _configured_mailbox()


def _is_recruitment_email(doc) -> bool:
	return bool(
		_is_recruitment_mailbox_message(doc)
		and doc.has_attachment
		and str(doc.get("email_status") or "").casefold() not in {"spam", "trash"}
	)


def _has_intake_fields() -> bool:
	return frappe.db.has_column("Communication", INTAKE_STATUS_FIELD)


def enforce_recruitment_email_account_safety(doc, method=None) -> None:
	"""Keep recruitment mail inside the governed Communication-only intake."""

	if str(doc.email_id or "").strip().casefold() == _configured_mailbox():
		doc.enable_auto_reply = 0
		doc.notify_if_unreplied = 0
		doc.enable_outgoing = 0
		doc.default_outgoing = 0
		doc.send_notification_to = ""
		doc.append_to = "Communication"
		for folder in doc.get("imap_folder") or []:
			folder_name = str(folder.get("folder_name") or "").strip().casefold()
			if folder_name not in ALLOWED_RECRUITMENT_IMAP_FOLDERS:
				frappe.throw(frappe._("La cuenta de reclutamiento solo puede sincronizar INBOX."))
			folder.append_to = "Communication"


def disable_existing_recruitment_mailbox_auto_reply() -> list[str]:
	"""Reconcile and read back existing recruitment-account safety settings."""

	accounts = frappe.get_all(
		"Email Account",
		filters={"email_id": _configured_mailbox()},
		pluck="name",
	)
	for account_name in accounts:
		frappe.db.set_value(
			"Email Account",
			account_name,
			{
				"enable_auto_reply": 0,
				"notify_if_unreplied": 0,
				"enable_outgoing": 0,
				"default_outgoing": 0,
				"send_notification_to": "",
				"append_to": "Communication",
			},
			update_modified=False,
		)
		folders = frappe.get_all(
			"IMAP Folder",
			filters={"parenttype": "Email Account", "parent": account_name, "parentfield": "imap_folder"},
			fields=["name", "folder_name"],
		)
		for folder in folders:
			if str(folder.folder_name or "").strip().casefold() not in ALLOWED_RECRUITMENT_IMAP_FOLDERS:
				frappe.db.delete("IMAP Folder", folder.name)
				continue
			frappe.db.set_value(
				"IMAP Folder", folder.name, "append_to", "Communication", update_modified=False
			)
	unsafe = [account_name for account_name in accounts if _unsafe_recruitment_email_account(account_name)]
	if unsafe:
		raise RuntimeError(f"Configuración insegura en cuentas de reclutamiento: {unsafe}")
	return accounts


def _unsafe_recruitment_email_account(account_name: str) -> bool:
	settings = frappe.db.get_value(
		"Email Account",
		account_name,
		(
			"enable_auto_reply",
			"notify_if_unreplied",
			"enable_outgoing",
			"default_outgoing",
			"send_notification_to",
			"append_to",
		),
	)
	if not settings:
		return True
	(
		enable_auto_reply,
		notify_if_unreplied,
		enable_outgoing,
		default_outgoing,
		send_notification_to,
		append_to,
	) = settings
	if (
		enable_auto_reply
		or notify_if_unreplied
		or enable_outgoing
		or default_outgoing
		or send_notification_to
		or append_to != "Communication"
	):
		return True
	folders = frappe.get_all(
		"IMAP Folder",
		filters={"parenttype": "Email Account", "parent": account_name, "parentfield": "imap_folder"},
		fields=["folder_name", "append_to"],
	)
	return any(
		folder.append_to != "Communication"
		or str(folder.folder_name or "").strip().casefold() not in ALLOWED_RECRUITMENT_IMAP_FOLDERS
		for folder in folders
	)


def _enqueue_pending_intake(communication_name: str) -> bool:
	"""Enqueue only the authoritative committed Pending state."""

	if not _has_intake_fields():
		return False
	status = frappe.db.get_value("Communication", communication_name, INTAKE_STATUS_FIELD)
	if status != INTAKE_PENDING:
		return False
	frappe.enqueue(
		"hrms.recruitment.email_intake.process_recruitment_email_safely",
		queue=QUEUE_NAME,
		timeout=600,
		job_id=f"ayp-email-intake:{communication_name}",
		deduplicate=True,
		communication_name=communication_name,
	)
	return True


def enqueue_recruitment_email_intake(doc, method=None) -> None:
	"""Persist enqueue intent with the email; after-commit enqueue is only an accelerator."""

	if not _is_recruitment_mailbox_message(doc):
		return
	if doc.get(INTAKE_STATUS_FIELD) in (INTAKE_COMPLETED, INTAKE_BLOCKED):
		return
	if doc.reference_doctype or doc.reference_name:
		# Frappe's inbound thread notifier only has recipients when the
		# Communication retains a parent/reference. Recruitment mail must remain
		# standalone until the governed worker links it after a clean CV scan.
		doc.db_set({"reference_doctype": None, "reference_name": None}, update_modified=False)
	if not _is_recruitment_email(doc):
		return
	if not _has_intake_fields():
		frappe.throw(frappe._("Durable recruitment intake state is not installed; run migrate."))
	doc.db_set(
		{
			INTAKE_STATUS_FIELD: INTAKE_PENDING,
			"custom_ayp_email_intake_queued_on": now_datetime(),
			"custom_ayp_email_intake_started_on": None,
			"custom_ayp_email_intake_claim": "",
			"custom_ayp_email_intake_completed_on": None,
			"custom_ayp_email_intake_error_code": "",
		},
		update_modified=False,
	)
	callback_key = f"ayp-email-intake:{doc.name}"
	registered = getattr(frappe.local, "ayp_email_intake_callbacks", set())
	frappe.local.ayp_email_intake_callbacks = registered
	if callback_key in registered:
		return
	registered.add(callback_key)
	frappe.db.after_rollback.add(lambda: registered.discard(callback_key))

	def enqueue_after_commit():
		try:
			try:
				_enqueue_pending_intake(doc.name)
			except Exception:
				# The committed Pending row is the source of truth. Redis/RQ is only
				# an accelerator; the scheduler will retry without duplicating the
				# raw inbound message in Frappe's Unhandled Email store.
				frappe.logger("email_intake").error(
					"Recruitment intake enqueue failed; durable Pending will be reconciled."
				)
		finally:
			registered.discard(callback_key)

	frappe.db.after_commit.add(enqueue_after_commit)


def _candidate_files(communication_name: str) -> list[dict]:
	return frappe.db.sql(
		"""
		SELECT name, file_name, file_url, file_size, creation
		FROM `tabFile`
		WHERE attached_to_doctype = 'Communication'
			AND attached_to_name = %s
			AND is_private = 1
			AND (LOWER(file_name) LIKE %s OR LOWER(file_name) LIKE %s)
		ORDER BY creation, name
		LIMIT 2
		""",
		(communication_name, "%.pdf", "%.docx"),
		as_dict=True,
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


def _claim_intake(communication) -> str | None:
	status = communication.get(INTAKE_STATUS_FIELD)
	if status == INTAKE_COMPLETED:
		return None
	if status != INTAKE_PENDING:
		raise EmailIntakeDomainError("El correo no tiene un estado Pendiente recuperable.")
	claim = uuid.uuid4().hex
	communication.db_set(
		{
			INTAKE_STATUS_FIELD: INTAKE_PROCESSING,
			"custom_ayp_email_intake_started_on": now_datetime(),
			"custom_ayp_email_intake_claim": claim,
		},
		update_modified=False,
	)
	return claim


def _complete_intake(communication, *, claim: str, applicant_name: str) -> None:
	if communication.get(INTAKE_STATUS_FIELD) != INTAKE_PROCESSING:
		raise EmailIntakeDomainError("El estado del intake cambió durante el procesamiento.")
	if communication.get("custom_ayp_email_intake_claim") != claim:
		raise EmailIntakeDomainError("El claim del intake cambió durante el procesamiento.")
	communication.db_set(
		{
			INTAKE_STATUS_FIELD: INTAKE_COMPLETED,
			"custom_ayp_email_intake_completed_on": now_datetime(),
			"custom_ayp_email_intake_claim": "",
			"custom_ayp_email_intake_error_code": "",
			"custom_ayp_email_intake_applicant": applicant_name,
		},
		update_modified=False,
	)


def _verified_completed_applicant(communication) -> str:
	applicant_name = str(communication.get("custom_ayp_email_intake_applicant") or "")
	file_name = str(communication.get("custom_ayp_email_intake_file") or "")
	expected_sha = str(communication.get("custom_ayp_email_intake_cv_sha256") or "")
	if (
		not applicant_name
		or not file_name
		or len(expected_sha) != 64
		or communication.reference_doctype != "Job Applicant"
		or communication.reference_name != applicant_name
	):
		raise EmailIntakeDomainError("El estado Completed no tiene evidencia durable completa.")
	applicant = frappe.db.get_value(
		"Job Applicant",
		applicant_name,
		["name", "resume_attachment", "custom_cv_sha256", "custom_candidate_cv_file"],
		as_dict=True,
	)
	file_record = frappe.db.get_value(
		"File",
		file_name,
		[
			"name",
			"file_url",
			"attached_to_doctype",
			"attached_to_name",
			"custom_av_scan_status",
			"custom_cv_sha256",
		],
		as_dict=True,
	)
	file_link_is_exact = file_record and (
		(
			file_record.attached_to_doctype == "Job Applicant"
			and file_record.attached_to_name == applicant_name
			and applicant
			and applicant.custom_candidate_cv_file == file_name
			and applicant.resume_attachment == file_record.file_url
		)
		or (
			file_record.attached_to_doctype == "Communication"
			and file_record.attached_to_name == communication.name
		)
	)
	if (
		not applicant
		or not file_record
		or not file_link_is_exact
		or file_record.custom_av_scan_status != "Clean"
		or file_record.custom_cv_sha256 != expected_sha
	):
		raise EmailIntakeDomainError("El estado Completed no supera el readback de CV limpio.")
	return applicant_name


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
		"custom_consent_capture_method": "",
		"custom_consent_evidence_id": "",
		"custom_consent_recorded_on": None,
		"custom_consent_form_route": "",
	}


def process_recruitment_email(communication_name: str) -> dict:
	"""Create or update one same-vacancy Job Applicant from a native email."""

	frappe.db.sql(
		"SELECT name FROM `tabCommunication` WHERE name = %s FOR UPDATE",
		(communication_name,),
	)
	communication = frappe.get_doc("Communication", communication_name, for_update=True)
	claim = _claim_intake(communication)
	if claim is None:
		return {
			"status": "already_processed",
			"applicant": _verified_completed_applicant(communication),
		}
	if not _is_recruitment_email(communication):
		raise EmailIntakeDomainError("El correo Pendiente ya no corresponde al buzón de reclutamiento.")
	if communication.reference_doctype or communication.reference_name:
		communication.db_set({"reference_doctype": None, "reference_name": None}, update_modified=False)

	job_opening = _configured_job_opening()
	if not frappe.db.exists("Job Opening", {"name": job_opening, "status": "Open"}):
		raise EmailIntakeDomainError("La vacante configurada no existe o no está abierta.")

	email, applicant_name = sender_identity(communication.sender, communication.sender_full_name)
	file_row = select_candidate_cv(_candidate_files(communication.name))
	file_doc = frappe.get_doc("File", file_row["name"], for_update=True)
	cv_sha256 = scan_stored_candidate_cv(file_doc)
	communication.db_set(
		{
			"custom_ayp_email_intake_file": file_doc.name,
			"custom_ayp_email_intake_cv_sha256": cv_sha256,
		},
		update_modified=False,
	)

	# Serialize same-person/same-vacancy lookup with the canonical profile linker.
	acquire_candidate_identity_lock()
	existing_name = same_vacancy_application(
		_matching_applications(job_opening, email, cv_sha256),
		email=email,
		cv_sha256=cv_sha256,
		applicant_name=applicant_name,
	)

	if existing_name:
		raise EmailIntakeReviewRequired("La identidad del remitente requiere revisión manual.")
	_detach_for_candidate(file_doc)
	with candidate_cv_file_identity(file_doc.name):
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
	_complete_intake(communication, claim=claim, applicant_name=applicant.name)
	return {
		"status": status,
		"applicant": applicant.name,
		"communication": communication.name,
		"cv_sha256": cv_sha256,
	}


def process_recruitment_email_safely(communication_name: str) -> dict:
	"""Record a PII-free terminal block while transient failures remain recoverable Pending work."""

	try:
		return process_recruitment_email(communication_name)
	except CandidateCVInfrastructureError:
		frappe.db.rollback()
		raise
	except EmailIntakeReviewRequired as exc:
		# Security validation already marked the exact File Clean. Preserve that
		# evidence and a terminal human-review state in the same transaction.
		frappe.db.set_value(
			"Communication",
			communication_name,
			{
				INTAKE_STATUS_FIELD: INTAKE_BLOCKED,
				"custom_ayp_email_intake_started_on": None,
				"custom_ayp_email_intake_claim": "",
				"custom_ayp_email_intake_error_code": type(exc).__name__,
			},
			update_modified=False,
		)
		frappe.db.commit()  # nosemgrep
		return {"status": "review_required", "communication": communication_name}
	except (CandidateCVSecurityError, EmailIntakeDomainError) as exc:
		frappe.db.rollback()
		fingerprint = hashlib.sha256(communication_name.encode()).hexdigest()[:12]
		if _has_intake_fields() and frappe.db.exists("Communication", communication_name):
			frappe.db.sql(
				"SELECT name FROM `tabCommunication` WHERE name = %s FOR UPDATE",
				(communication_name,),
			)
			state = frappe.db.get_value(
				"Communication",
				communication_name,
				[
					INTAKE_STATUS_FIELD,
					"custom_ayp_email_intake_applicant",
					"custom_ayp_email_intake_completed_on",
					"reference_doctype",
					"reference_name",
				],
				as_dict=True,
			)
			if state and state.get(INTAKE_STATUS_FIELD) == INTAKE_COMPLETED:
				completed = frappe.get_doc("Communication", communication_name)
				return {
					"status": "already_processed",
					"applicant": _verified_completed_applicant(completed),
				}
			if state and state.get(INTAKE_STATUS_FIELD) == INTAKE_PENDING:
				frappe.db.set_value(
					"Communication",
					communication_name,
					{
						INTAKE_STATUS_FIELD: INTAKE_BLOCKED,
						"custom_ayp_email_intake_started_on": None,
						"custom_ayp_email_intake_claim": "",
						"custom_ayp_email_intake_error_code": type(exc).__name__,
					},
					update_modified=False,
				)
				# Persist terminal Blocked state before re-raising; the job runner rolls back exceptions.
				frappe.db.commit()  # nosemgrep
		frappe.log_error(
			title=f"Recruitment email intake blocked {fingerprint}",
			message=frappe.get_traceback(),
		)
		raise


@frappe.whitelist(methods=["POST"])
def resolve_recruitment_email_review(
	communication_name: str, action: str, applicant_name: str | None = None
) -> dict:
	"""Resolve an identity conflict without editing intake state in the database."""

	frappe.only_for(("HR Manager", "System Manager"))
	if action not in {"create_new", "link_existing", "reject"}:
		frappe.throw(frappe._("Acción de revisión no permitida."))
	frappe.db.sql(
		"SELECT name FROM `tabCommunication` WHERE name = %s FOR UPDATE",
		(communication_name,),
	)
	communication = frappe.get_doc("Communication", communication_name)
	if (
		communication.get(INTAKE_STATUS_FIELD) != INTAKE_BLOCKED
		or communication.get("custom_ayp_email_intake_error_code") != "EmailIntakeReviewRequired"
	):
		frappe.throw(frappe._("La Communication no tiene una revisión de identidad pendiente."))
	file_row = select_candidate_cv(_candidate_files(communication.name))
	file_doc = frappe.get_doc("File", file_row["name"], for_update=True)
	if file_doc.get("custom_av_scan_status") != "Clean" or not file_doc.get("custom_cv_sha256"):
		frappe.throw(frappe._("El CV no tiene evidencia antivirus Clean verificable."))

	if action == "reject":
		communication.db_set(
			{
				"custom_ayp_email_intake_error_code": "ReviewRejected",
				"custom_ayp_email_intake_completed_on": now_datetime(),
			},
			update_modified=False,
		)
		return {"status": "rejected", "communication": communication.name}

	if action == "link_existing":
		if not applicant_name or not frappe.db.exists("Job Applicant", applicant_name):
			frappe.throw(frappe._("La solicitud seleccionada no existe."))
		resolved_applicant = str(applicant_name)
	else:
		email, resolved_name = sender_identity(communication.sender, communication.sender_full_name)
		_detach_for_candidate(file_doc)
		with candidate_cv_file_identity(file_doc.name):
			resolved_applicant = (
				frappe.get_doc(
					_new_applicant_data(
						applicant_name=resolved_name,
						email=email,
						job_opening=_configured_job_opening(),
						resume_attachment=file_doc.file_url,
					)
				)
				.insert(ignore_permissions=True)
				.name
			)
		_assert_candidate_file_link(file_doc.name, resolved_applicant)

	claim = uuid.uuid4().hex
	communication.db_set(
		{
			INTAKE_STATUS_FIELD: INTAKE_PROCESSING,
			"custom_ayp_email_intake_claim": claim,
			"custom_ayp_email_intake_started_on": now_datetime(),
		},
		update_modified=False,
	)
	_link_communication(communication, resolved_applicant)
	_complete_intake(communication, claim=claim, applicant_name=resolved_applicant)
	return {
		"status": "created" if action == "create_new" else "linked",
		"communication": communication.name,
		"applicant": resolved_applicant,
	}


def recover_stale_recruitment_email_intakes(communication_name: str | None = None) -> int:
	"""Recover committed Pending work and abandoned Processing claims."""

	if not _has_intake_fields():
		return 0
	stale_before = add_to_date(now_datetime(), minutes=-INTAKE_STALE_MINUTES)
	rows = frappe.db.sql(
		"""
		SELECT name
		FROM `tabCommunication`
		WHERE (
			(custom_ayp_email_intake_status = %s AND (
				custom_ayp_email_intake_queued_on IS NULL
				OR custom_ayp_email_intake_queued_on < %s
			))
			OR (custom_ayp_email_intake_status = %s AND (
				custom_ayp_email_intake_started_on IS NULL
				OR custom_ayp_email_intake_started_on < %s
			))
		)
		AND (%s IS NULL OR name = %s)
		ORDER BY creation, name
		LIMIT 100
		FOR UPDATE
		""",
		(
			INTAKE_PENDING,
			stale_before,
			INTAKE_PROCESSING,
			stale_before,
			communication_name,
			communication_name,
		),
		as_dict=True,
	)
	row_names = [row.name for row in rows]
	for row_name in row_names:
		frappe.db.set_value(
			"Communication",
			row_name,
			{
				INTAKE_STATUS_FIELD: INTAKE_PENDING,
				"custom_ayp_email_intake_queued_on": now_datetime(),
				"custom_ayp_email_intake_started_on": None,
				"custom_ayp_email_intake_claim": "",
				"custom_ayp_email_intake_error_code": "",
			},
			update_modified=False,
		)

	def enqueue_recovered_batch():
		for row_name in row_names:
			try:
				_enqueue_pending_intake(row_name)
			except Exception:
				frappe.logger("email_intake").error(
					"Recovered recruitment intake enqueue failed; row remains durable Pending."
				)

	if row_names:
		frappe.db.after_commit.add(enqueue_recovered_batch)
	return len(row_names)
