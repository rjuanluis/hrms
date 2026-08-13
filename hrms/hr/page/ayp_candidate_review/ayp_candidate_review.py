from __future__ import annotations

import json
import hashlib

import frappe
from frappe import _

from hrms.recruitment.candidate_review_domain import (
	BatchReviewRequest,
	CandidateReviewValidationError,
	FilteredReviewRunRequest,
	ReviewFilters,
	validate_transition,
)
from hrms.recruitment.candidate_scoring_domain import (
	DEFAULT_CRITERIA,
	CandidateScoringValidationError,
	Scorecard,
)
from hrms.recruitment.candidate_profile_governance import candidate_profile_governance_update
from hrms.recruitment.candidate_document_service import MANUAL_REVIEWABLE as CV_MANUAL_REVIEWABLE
from hrms.recruitment.candidate_document_service import (
	revalidate_candidate_document,
	validate_candidate_ready_for_scoring,
)
from hrms.recruitment.matching import normalize_email, normalize_phone
from hrms.recruitment.talent_pool import acquire_candidate_identity_lock, resolve_candidate_profile

EVENT_DOCTYPE = "AYP Candidate Review Event"
RUN_DOCTYPE = "AYP Candidate Review Run"
MEMBER_DOCTYPE = "AYP Candidate Review Member"
AYP_INTERVIEW_TYPE = "AyP - Entrevista estructurada"
PAGE_ROLES = ("HR User", "HR Manager", "System Manager")
PROFILE_ACTIONS = {
	"retain": "Activo",
	"priority": "Prioritario",
	"no_interest": "Sin interés",
	"disposed": "Dispuesto",
}
CV_SCORE_READY = frozenset({"Procesado", "Verificado manualmente"})
CV_DECISIVE_TARGETS = frozenset({"Shortlisted", "Rejected"})


def _validation_error(exc: CandidateReviewValidationError):
	frappe.throw(_(str(exc)), frappe.ValidationError)


def _query_filters(review_filters: ReviewFilters) -> dict:
	filters = {}
	if review_filters.status:
		filters["status"] = review_filters.status
	if review_filters.job_title:
		filters["job_title"] = review_filters.job_title
	if review_filters.source:
		filters["source"] = review_filters.source
	if review_filters.dedupe_status:
		filters["custom_dedupe_status"] = review_filters.dedupe_status
	if review_filters.cv_processing_status:
		filters["custom_cv_processing_status"] = review_filters.cv_processing_status
	if review_filters.minimum_rating is not None:
		filters["applicant_rating"] = [">=", review_filters.minimum_rating / 5]
	if review_filters.minimum_score is not None:
		filters["custom_candidate_score"] = [">=", review_filters.minimum_score]
	return filters


def _query_or_filters(review_filters: ReviewFilters) -> list:
	if not review_filters.search:
		return []
	pattern = "%{0}%".format(review_filters.search)
	return [
		["Job Applicant", "applicant_name", "like", pattern],
		["Job Applicant", "email_id", "like", pattern],
		["Job Applicant", "phone_number", "like", pattern],
		["Job Applicant", "name", "like", pattern],
	]


def _interview_queue_applicants(queue: str) -> list[str]:
	if not queue:
		return []
	frappe.has_permission("Interview", "read", throw=True)
	conditions = {
		"no_interview": "i.name IS NULL",
		"unassigned": "i.name IS NOT NULL AND NOT EXISTS (SELECT 1 FROM `tabInterview Detail` d WHERE d.parent = i.name)",
		"missing_feedback": """i.name IS NOT NULL AND EXISTS (
			SELECT 1 FROM `tabInterview Detail` d
			WHERE d.parent = i.name AND NOT EXISTS (
				SELECT 1 FROM `tabInterview Feedback` f
				WHERE f.interview = i.name AND f.interviewer = d.interviewer AND f.docstatus = 1
			)
		)""",
		"disagreement": """i.name IS NOT NULL AND (
			SELECT COUNT(DISTINCT f.result) FROM `tabInterview Feedback` f
			WHERE f.interview = i.name AND f.docstatus = 1 AND COALESCE(f.result, '') != ''
		) > 1""",
		"overdue": "i.name IS NOT NULL AND i.scheduled_on < CURDATE() AND i.docstatus = 0 AND i.status NOT IN ('Cleared', 'Rejected', 'Cancelled')",
		"ready_final_decision": "i.name IS NOT NULL AND i.docstatus = 1 AND i.status IN ('Cleared', 'Rejected') AND ja.status NOT IN ('Accepted', 'Rejected')",
	}
	return frappe.db.sql(
		"""
		SELECT ja.name
		FROM `tabJob Applicant` ja
		LEFT JOIN `tabInterview` i ON i.name = (
			SELECT i2.name FROM `tabInterview` i2
			WHERE i2.job_applicant = ja.name AND i2.interview_type = %s AND i2.docstatus != 2
			ORDER BY i2.modified DESC, i2.name DESC LIMIT 1
		)
		WHERE {condition}
		""".format(condition=conditions[queue]),
		(AYP_INTERVIEW_TYPE,),
		pluck=True,
	)


def _candidate_query_parts(review_filters: ReviewFilters) -> tuple[dict, list]:
	filters = _query_filters(review_filters)
	queue_applicants = _interview_queue_applicants(review_filters.interview_queue)
	if review_filters.interview_queue:
		filters["name"] = ["in", queue_applicants or ["__no_matching_candidate__"]]
	return filters, _query_or_filters(review_filters)


def _candidate_count(filters: dict, or_filters: list) -> int:
	rows = frappe.get_list(
		"Job Applicant",
		fields=[{"COUNT": "name", "as": "total"}],
		filters=filters,
		or_filters=or_filters,
		limit=1,
	)
	return int(rows[0].get("total") or 0) if rows else 0


def _interview_state_by_applicant(applicant_names: list[str]) -> dict:
	if not applicant_names:
		return {}
	frappe.has_permission("Interview", "read", throw=True)
	interviews = frappe.get_list(
		"Interview",
		filters={
			"job_applicant": ["in", applicant_names],
			"interview_type": AYP_INTERVIEW_TYPE,
			"docstatus": ["!=", 2],
		},
		fields=["name", "job_applicant", "status", "scheduled_on", "docstatus", "modified"],
		order_by="modified desc, name desc",
		page_length=min(len(applicant_names) * 5, 500),
	)
	latest = {}
	for row in interviews:
		latest.setdefault(row["job_applicant"], dict(row))
	if not latest:
		return {}

	interview_names = [row["name"] for row in latest.values()]
	assigned = frappe.get_all(
		"Interview Detail",
		filters={"parent": ["in", interview_names]},
		fields=["parent", "interviewer"],
	)
	feedback = frappe.get_all(
		"Interview Feedback",
		filters={"interview": ["in", interview_names], "docstatus": 1},
		fields=["interview", "interviewer", "result"],
	)
	assigned_by_interview = {}
	for row in assigned:
		assigned_by_interview.setdefault(row["parent"], set()).add(row["interviewer"])
	feedback_by_interview = {}
	results_by_interview = {}
	for row in feedback:
		feedback_by_interview.setdefault(row["interview"], set()).add(row["interviewer"])
		if row.get("result"):
			results_by_interview.setdefault(row["interview"], set()).add(row["result"])

	for row in latest.values():
		interview_name = row["name"]
		assigned_users = assigned_by_interview.get(interview_name, set())
		feedback_users = feedback_by_interview.get(interview_name, set())
		row["assigned_interviewers"] = len(assigned_users)
		row["submitted_feedback"] = len(feedback_users)
		row["missing_feedback"] = len(assigned_users - feedback_users)
		row["feedback_disagreement"] = len(results_by_interview.get(interview_name, set())) > 1
	return latest


def _profile_state_by_name(profile_names: list[str]) -> dict:
	profile_names = [name for name in dict.fromkeys(profile_names) if name]
	if not profile_names:
		return {}
	frappe.has_permission("AYP Candidate Profile", "read", throw=True)
	rows = frappe.get_list(
		"AYP Candidate Profile",
		filters={"name": ["in", profile_names]},
		fields=["name", "talent_pool_status", "dedupe_status", "do_not_contact"],
		page_length=len(profile_names),
	)
	return {row["name"]: dict(row) for row in rows}


@frappe.whitelist()
def get_candidates(filters=None, start=0, page_length=50):
	frappe.only_for(PAGE_ROLES)
	frappe.has_permission("Job Applicant", "read", throw=True)
	try:
		review_filters = ReviewFilters.from_input(filters, start=start, page_length=page_length)
	except CandidateReviewValidationError as exc:
		return _validation_error(exc)

	query_filters, query_or_filters = _candidate_query_parts(review_filters)
	total_count = _candidate_count(query_filters, query_or_filters)
	rows = frappe.get_list(
		"Job Applicant",
		fields=[
			"name",
			"applicant_name",
			"email_id",
			"job_title",
			"designation",
			"status",
			"applicant_rating",
			"source",
			"creation",
			"modified",
			"resume_attachment",
			"custom_candidate_profile",
			"custom_dedupe_status",
			"custom_cv_processing_status",
			"custom_cv_processing_method",
			"custom_cv_processing_detail",
			"custom_candidate_score",
			"custom_candidate_recommendation",
			"custom_candidate_scorecard",
			"custom_candidate_scored_on",
		],
		filters=query_filters,
		or_filters=query_or_filters,
		order_by=(
			"custom_candidate_score desc, creation desc, name desc"
			if review_filters.sort_by == "score"
			else "creation desc, name desc"
		),
		start=review_filters.start,
		page_length=review_filters.page_length + 1,
	)
	has_more = len(rows) > review_filters.page_length
	page = []
	for source_row in rows[: review_filters.page_length]:
		row = dict(source_row)
		rating = float(row.get("applicant_rating") or 0)
		row["rating_out_of_five"] = round(max(0, min(5, rating * 5)), 1)
		page.append(row)
	interview_state = _interview_state_by_applicant([row["name"] for row in page])
	profile_state = _profile_state_by_name([row.get("custom_candidate_profile") for row in page])
	for row in page:
		state = interview_state.get(row["name"], {})
		profile = profile_state.get(row.get("custom_candidate_profile"), {})
		row.update(
			{
				"existing_interview": state.get("name"),
				"interview_status": state.get("status"),
				"interview_scheduled_on": state.get("scheduled_on"),
				"interview_docstatus": state.get("docstatus"),
				"assigned_interviewers": state.get("assigned_interviewers", 0),
				"submitted_feedback": state.get("submitted_feedback", 0),
				"missing_feedback": state.get("missing_feedback", 0),
				"feedback_disagreement": state.get("feedback_disagreement", False),
				"talent_pool_status": profile.get("talent_pool_status"),
				"talent_pool_do_not_contact": bool(profile.get("do_not_contact")),
			}
		)
	return {
		"rows": page,
		"total_count": total_count,
		"has_more": has_more,
		"start": review_filters.start,
		"page_length": review_filters.page_length,
	}


def _load_and_validate_documents(request: BatchReviewRequest) -> list:
	placeholders = ", ".join(["%s"] * len(request.applicant_names))
	frappe.db.sql(
		"SELECT name FROM `tabJob Applicant` WHERE name IN ({0}) ORDER BY name FOR UPDATE".format(
			placeholders
		),
		tuple(sorted(request.applicant_names)),
	)
	documents = []
	for applicant_name in request.applicant_names:
		frappe.has_permission("Job Applicant", "write", applicant_name, throw=True)
		doc = frappe.get_doc("Job Applicant", applicant_name, for_update=True)
		if (doc.job_title or "") != request.job_title:
			frappe.throw(
				_("La aplicación {0} no pertenece a la vacante seleccionada.").format(applicant_name),
				frappe.ValidationError,
			)
		if request.target_status in CV_DECISIVE_TARGETS:
			if doc.get("custom_cv_processing_status") not in CV_SCORE_READY:
				frappe.throw(
					_("La aplicación {0} no tiene un CV procesado o verificado manualmente.").format(applicant_name),
					frappe.ValidationError,
				)
			revalidate_candidate_document(doc)
		try:
			validate_transition(doc.status, request.target_status)
		except CandidateReviewValidationError as exc:
			_validation_error(exc)
		documents.append(doc)
	return documents


def _insert_review_event(doc, *, previous_status: str, request: BatchReviewRequest, batch_id: str) -> None:
	frappe.get_doc(
		{
			"doctype": EVENT_DOCTYPE,
			"batch_id": batch_id,
			"applicant": doc.name,
			"candidate_profile": doc.custom_candidate_profile or "",
			"job_opening": doc.job_title,
			"action": "Cambio de estado",
			"previous_status": previous_status,
			"new_status": request.target_status,
			"reason": request.reason,
			"actor": frappe.session.user,
			"occurred_on": frappe.utils.now_datetime(),
		}
	).insert(ignore_permissions=True)


@frappe.whitelist(methods=["POST"])
def apply_batch_action(applicant_names, target_status, reason, job_title):
	frappe.only_for(PAGE_ROLES)
	frappe.has_permission("Job Applicant", "write", throw=True)
	try:
		request = BatchReviewRequest.from_input(
			applicant_names,
			target_status=target_status,
			reason=reason,
			job_title=job_title,
		)
	except CandidateReviewValidationError as exc:
		return _validation_error(exc)

	batch_id = frappe.generate_hash(length=12)
	savepoint = "ayp_candidate_review_{0}".format(batch_id)
	frappe.db.savepoint(savepoint)
	try:
		# Lock first, then load and validate fresh state. Concurrent batches over
		# the same candidates serialize instead of both accepting stale status.
		documents = _load_and_validate_documents(request)
		for doc in documents:
			previous_status = doc.status
			doc.custom_ayp_governed = 1
			frappe.flags.ayp_candidate_review_batch = True
			try:
				doc.status = request.target_status
				doc.save()
			finally:
				frappe.flags.ayp_candidate_review_batch = False
			_insert_review_event(
				doc,
				previous_status=previous_status,
				request=request,
				batch_id=batch_id,
			)
			doc.add_comment(
				comment_type="Info",
				text=_("Candidate Review lote {0}: estado cambiado de {1} a {2}.").format(
					batch_id,
					previous_status,
					request.target_status,
				),
			)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	return {"batch_id": batch_id, "updated": len(documents)}


def _frozen_filters_payload(review_filters: ReviewFilters) -> dict:
	return {
		"search": review_filters.search,
		"status": review_filters.status,
		"job_title": review_filters.job_title,
		"source": review_filters.source,
		"dedupe_status": review_filters.dedupe_status,
		"cv_processing_status": review_filters.cv_processing_status,
		"minimum_rating": review_filters.minimum_rating,
		"minimum_score": review_filters.minimum_score,
		"sort_by": review_filters.sort_by,
		"interview_queue": review_filters.interview_queue,
	}


def _run_progress(run_name: str) -> dict:
	rows = frappe.db.sql(
		"""
		SELECT member_status, COUNT(*) AS total
		FROM `tabAYP Candidate Review Member`
		WHERE parent = %s AND parenttype = %s AND parentfield = 'members'
		GROUP BY member_status
		""",
		(run_name, RUN_DOCTYPE),
		as_dict=True,
	)
	counts = {row.member_status: int(row.total or 0) for row in rows}
	total = sum(counts.values())
	processed = counts.get("Processed", 0)
	skipped = counts.get("Skipped", 0)
	return {
		"total": total,
		"processed": processed,
		"skipped": skipped,
		"remaining": counts.get("Pending", 0),
	}


def _run_payload(run_name: str) -> dict:
	run = frappe.get_doc(RUN_DOCTYPE, run_name)
	frappe.has_permission(RUN_DOCTYPE, "read", run_name, throw=True)
	return {
		"run": run.name,
		"run_status": run.run_status,
		"job_title": run.job_opening,
		"source_status": run.source_status,
		"target_status": run.target_status,
		"reason": run.reason,
		"frozen_by": run.frozen_by,
		"frozen_on": run.frozen_on,
		"confirmed_on": run.confirmed_on,
		"skipped_reviewed_on": run.get("skipped_reviewed_on"),
		**_run_progress(run.name),
	}


@frappe.whitelist(methods=["POST"])
def freeze_filtered_run(filters, target_status, reason):
	"""Freeze the complete matching cohort before an operator confirms it."""

	frappe.only_for(PAGE_ROLES)
	frappe.has_permission("Job Applicant", "write", throw=True)
	# User is an existing row, so this transaction lock serializes the active
	# run check and insert without relying on a uniqueness rule over statuses.
	frappe.db.sql("SELECT name FROM `tabUser` WHERE name = %s FOR UPDATE", (frappe.session.user,))
	active = frappe.get_list(
		RUN_DOCTYPE,
		filters={"frozen_by": frappe.session.user, "run_status": ["in", ["Frozen", "In Progress"]]},
		fields=["name"],
		order_by="frozen_on desc, name desc",
		page_length=1,
	)
	if active:
		frappe.throw(
			_("Ya existe una cohorte activa ({0}). Reanúdala antes de congelar otra.").format(active[0]["name"]),
			frappe.ValidationError,
		)
	try:
		request = FilteredReviewRunRequest.from_input(
			filters,
			target_status=target_status,
			reason=reason,
		)
	except CandidateReviewValidationError as exc:
		return _validation_error(exc)
	query_filters, query_or_filters = _candidate_query_parts(request.filters)
	# This is the only live-query read. Its complete result becomes immutable
	# membership before confirmation; processing never paginates this query.
	rows = frappe.get_list(
		"Job Applicant",
		fields=["name", "status"],
		filters=query_filters,
		or_filters=query_or_filters,
		order_by="creation asc, name asc",
		start=0,
		page_length=0,
	)
	now = frappe.utils.now_datetime()
	run = frappe.get_doc(
		{
			"doctype": RUN_DOCTYPE,
			"run_status": "Frozen",
			"job_opening": request.filters.job_title,
			"source_status": request.filters.status,
			"target_status": request.target_status,
			"reason": request.reason,
			"filters_json": json.dumps(
				_frozen_filters_payload(request.filters), ensure_ascii=False, sort_keys=True, separators=(",", ":")
			),
			"frozen_by": frappe.session.user,
			"frozen_on": now,
			"total_members": len(rows),
			"processed_members": 0,
			"skipped_members": 0,
			"members": [
				{
					"applicant": row["name"],
					"frozen_status": row["status"],
					"member_status": "Pending",
				}
				for row in rows
			],
		}
	).insert(ignore_permissions=True)
	return {
		"run": run.name,
		"run_status": run.run_status,
		"total": len(rows),
		"processed": 0,
		"skipped": 0,
		"remaining": len(rows),
		"job_title": run.job_opening,
		"source_status": run.source_status,
		"target_status": run.target_status,
		"reason": run.reason,
		"confirmed_on": None,
	}


@frappe.whitelist()
def get_active_filtered_run():
	frappe.only_for(PAGE_ROLES)
	rows = frappe.get_list(
		RUN_DOCTYPE,
		filters={"frozen_by": frappe.session.user, "run_status": ["in", ["Frozen", "In Progress"]]},
		fields=["name"],
		order_by="frozen_on desc, name desc",
		page_length=1,
	)
	if rows:
		return _run_payload(rows[0]["name"])
	attention = frappe.get_list(
		RUN_DOCTYPE,
		filters={
			"frozen_by": frappe.session.user,
			"run_status": "Completed",
			"skipped_members": [">", 0],
			"skipped_reviewed_on": ["is", "not set"],
		},
		fields=["name"],
		order_by="completed_on desc, name desc",
		page_length=1,
	)
	return _run_payload(attention[0]["name"]) if attention else None


@frappe.whitelist(methods=["POST"])
def cancel_filtered_run(run: str, reason: str):
	frappe.only_for(PAGE_ROLES)
	frappe.has_permission(RUN_DOCTYPE, "read", run, throw=True)
	reason = str(reason or "").strip()
	if len(reason) < 20 or len(reason) > 500:
		frappe.throw(_("El motivo de cancelación debe tener entre 20 y 500 caracteres."), frappe.ValidationError)
	frappe.db.sql("SELECT name FROM `tabAYP Candidate Review Run` WHERE name = %s FOR UPDATE", (run,))
	run_doc = frappe.get_doc(RUN_DOCTYPE, run, for_update=True)
	if run_doc.frozen_by != frappe.session.user:
		frappe.throw(_("Solo quien congeló la cohorte puede cancelarla."), frappe.PermissionError)
	progress = _run_progress(run)
	if run_doc.run_status not in {"Frozen", "In Progress"}:
		frappe.throw(_("Solo se puede cancelar una cohorte congelada o pausada."), frappe.ValidationError)
	now = frappe.utils.now_datetime()
	frappe.db.sql(
		"""
		UPDATE `tabAYP Candidate Review Member`
		SET member_status = 'Skipped', processed_on = %s,
			outcome_detail = %s
		WHERE parent = %s AND parenttype = %s AND parentfield = 'members'
			AND member_status = 'Pending'
		""",
		(now, _("Cohorte cancelada por el operador: {0}").format(reason), run, RUN_DOCTYPE),
	)
	progress = _run_progress(run)
	frappe.db.set_value(
		RUN_DOCTYPE,
		run,
		{
			"run_status": "Cancelled",
			"cancelled_on": now,
			"cancelled_by": frappe.session.user,
			"cancellation_reason": reason,
			"processed_members": progress["processed"],
			"skipped_members": progress["skipped"],
		},
		update_modified=False,
	)
	return {**_run_payload(run), "run_status": "Cancelled"}


@frappe.whitelist()
def get_filtered_run_members(run: str, member_status: str = "Skipped"):
	frappe.only_for(PAGE_ROLES)
	frappe.has_permission(RUN_DOCTYPE, "read", run, throw=True)
	if member_status not in {"Pending", "Processed", "Skipped"}:
		frappe.throw(_("El estado de miembro solicitado no es válido."), frappe.ValidationError)
	# Child tables have no standalone permission rows in Frappe v16. The parent
	# was authorized above, so read only its bounded immutable membership.
	return frappe.db.sql(
		"""
		SELECT applicant, frozen_status, previous_status, member_status,
			processed_on, outcome_detail
		FROM `tabAYP Candidate Review Member`
		WHERE parent = %s AND parenttype = %s AND parentfield = 'members'
			AND member_status = %s
		ORDER BY idx, name
		LIMIT 500
		""",
		(run, RUN_DOCTYPE, member_status),
		as_dict=True,
	)


@frappe.whitelist(methods=["POST"])
def acknowledge_filtered_run_skips(run: str, reason: str):
	frappe.only_for(PAGE_ROLES)
	frappe.has_permission(RUN_DOCTYPE, "read", run, throw=True)
	reason = str(reason or "").strip()
	if len(reason) < 20 or len(reason) > 500:
		frappe.throw(_("El cierre de omitidos debe tener entre 20 y 500 caracteres."), frappe.ValidationError)
	frappe.db.sql("SELECT name FROM `tabAYP Candidate Review Run` WHERE name = %s FOR UPDATE", (run,))
	run_doc = frappe.get_doc(RUN_DOCTYPE, run, for_update=True)
	if run_doc.frozen_by != frappe.session.user:
		frappe.throw(_("Solo quien congeló la cohorte puede cerrar sus omitidos."), frappe.PermissionError)
	progress = _run_progress(run)
	if run_doc.run_status != "Completed" or not progress["skipped"]:
		frappe.throw(_("La cohorte no tiene omitidos terminados que revisar."), frappe.ValidationError)
	frappe.db.set_value(
		RUN_DOCTYPE,
		run,
		{
			"skipped_reviewed_on": frappe.utils.now_datetime(),
			"skipped_reviewed_by": frappe.session.user,
			"skipped_review_reason": reason,
		},
		update_modified=False,
	)
	return {**_run_payload(run), "skipped_reviewed_on": frappe.utils.now_datetime()}


def _pending_run_members_for_update(run_name: str) -> list[dict]:
	return frappe.db.sql(
		"""
		SELECT name, applicant, frozen_status
		FROM `tabAYP Candidate Review Member`
		WHERE parent = %s AND parenttype = %s AND parentfield = 'members'
			AND member_status = 'Pending'
		ORDER BY idx, name
		LIMIT 100 FOR UPDATE
		""",
		(run_name, RUN_DOCTYPE),
		as_dict=True,
	)


def _mark_run_member(member_name: str, values: dict) -> None:
	frappe.db.set_value(MEMBER_DOCTYPE, member_name, values, update_modified=False)


@frappe.whitelist(methods=["POST"])
def process_filtered_run_chunk(run: str):
	"""Process at most 100 immutable cohort members and return durable progress."""

	frappe.only_for(PAGE_ROLES)
	frappe.has_permission("Job Applicant", "write", throw=True)
	frappe.has_permission(RUN_DOCTYPE, "read", run, throw=True)
	savepoint = "ayp_filtered_run_{0}".format(frappe.generate_hash(length=12))
	frappe.db.savepoint(savepoint)
	try:
		frappe.db.sql("SELECT name FROM `tabAYP Candidate Review Run` WHERE name = %s FOR UPDATE", (run,))
		run_doc = frappe.get_doc(RUN_DOCTYPE, run, for_update=True)
		if run_doc.frozen_by != frappe.session.user:
			frappe.throw(_("Solo quien congeló la cohorte puede procesarla."), frappe.PermissionError)
		if run_doc.run_status not in {"Frozen", "In Progress"}:
			return _run_payload(run)
		members = _pending_run_members_for_update(run)
		if members:
			placeholders = ", ".join(["%s"] * len(members))
			frappe.db.sql(
				"SELECT name FROM `tabJob Applicant` WHERE name IN ({0}) ORDER BY name FOR UPDATE".format(
					placeholders
				),
				tuple(sorted(member["applicant"] for member in members)),
			)
		now = frappe.utils.now_datetime()
		batch_id = "{0}:{1}".format(run, frappe.generate_hash(length=12))
		for member in members:
			applicant_name = member["applicant"]
			frappe.has_permission("Job Applicant", "write", applicant_name, throw=True)
			doc = frappe.get_doc("Job Applicant", applicant_name, for_update=True)
			previous_status = doc.status
			if (doc.job_title or "") != run_doc.job_opening or doc.status != member["frozen_status"]:
				detail = _("Omitido: la vacante o el estado cambió después de congelar la cohorte.")
				_mark_run_member(
					member["name"],
					{
						"member_status": "Skipped",
						"batch_id": batch_id,
						"previous_status": previous_status,
						"processed_on": now,
						"outcome_detail": detail,
					},
				)
				frappe.get_doc(
					{
						"doctype": EVENT_DOCTYPE,
						"batch_id": batch_id,
						"applicant": doc.name,
						"candidate_profile": doc.custom_candidate_profile or "",
						"job_opening": doc.job_title or "",
						"action": "Cohorte congelada omitida",
						"previous_status": previous_status,
						"new_status": previous_status,
						"reason": detail,
						"actor": frappe.session.user,
						"occurred_on": now,
					}
				).insert(ignore_permissions=True)
				continue
			document_skip_detail = ""
			if run_doc.target_status in CV_DECISIVE_TARGETS:
				if doc.get("custom_cv_processing_status") not in CV_SCORE_READY:
					document_skip_detail = _("Omitido: el CV no está procesado ni verificado manualmente.")
				else:
					try:
						revalidate_candidate_document(doc)
					except frappe.ValidationError:
						document_skip_detail = _(
							"Omitido: el CV no conserva un archivo privado, limpio y ligado a su huella procesada."
						)
			if document_skip_detail:
				detail = document_skip_detail
				_mark_run_member(
					member["name"],
					{
						"member_status": "Skipped",
						"batch_id": batch_id,
						"previous_status": previous_status,
						"processed_on": now,
						"outcome_detail": detail,
					},
				)
				frappe.get_doc(
					{
						"doctype": EVENT_DOCTYPE,
						"batch_id": batch_id,
						"applicant": doc.name,
						"candidate_profile": doc.custom_candidate_profile or "",
						"job_opening": doc.job_title or "",
						"action": "Cohorte congelada omitida",
						"previous_status": previous_status,
						"new_status": previous_status,
						"reason": detail,
						"actor": frappe.session.user,
						"occurred_on": now,
					}
				).insert(ignore_permissions=True)
				continue
			try:
				validate_transition(previous_status, run_doc.target_status)
			except CandidateReviewValidationError as exc:
				frappe.throw(_(str(exc)), frappe.ValidationError)
			doc.custom_ayp_governed = 1
			frappe.flags.ayp_candidate_review_batch = True
			try:
				doc.status = run_doc.target_status
				doc.save()
			finally:
				frappe.flags.ayp_candidate_review_batch = False
			request = BatchReviewRequest.from_input(
				[doc.name],
				target_status=run_doc.target_status,
				reason=run_doc.reason,
				job_title=run_doc.job_opening,
			)
			_insert_review_event(doc, previous_status=previous_status, request=request, batch_id=batch_id)
			doc.add_comment(
				comment_type="Info",
				text=_("Candidate Review cohorte {0}: estado cambiado de {1} a {2}.").format(
					run,
					previous_status,
					run_doc.target_status,
				),
			)
			_mark_run_member(
				member["name"],
				{
					"member_status": "Processed",
					"batch_id": batch_id,
					"previous_status": previous_status,
					"processed_on": now,
					"outcome_detail": _("Estado actualizado."),
				},
			)
		progress = _run_progress(run)
		run_values = {
			"run_status": "Completed" if progress["remaining"] == 0 else "In Progress",
			"confirmed_on": run_doc.confirmed_on or now,
			"last_processed_on": now,
			"completed_on": now if progress["remaining"] == 0 else None,
			"processed_members": progress["processed"],
			"skipped_members": progress["skipped"],
		}
		frappe.db.set_value(RUN_DOCTYPE, run, run_values, update_modified=False)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	return _run_payload(run)


def _scorecard_criteria_payload() -> list:
	return [
		{
			"criterion_key": criterion.key,
			"criterion_label": criterion.label,
			"weight": criterion.weight,
			"description": criterion.description,
			"rating": 0,
			"evidence": "",
		}
		for criterion in DEFAULT_CRITERIA
	]


@frappe.whitelist()
def get_scorecard(applicant):
	frappe.only_for(PAGE_ROLES)
	frappe.has_permission("Job Applicant", "read", applicant, throw=True)
	frappe.get_doc("Job Applicant", applicant)
	latest_rows = frappe.get_list(
		"AYP Candidate Scorecard",
		filters={"applicant": applicant},
		fields=[
			"name",
			"version",
			"total_score",
			"recommendation",
			"explanation",
			"scored_by",
			"scored_on",
		],
		order_by="version desc, creation desc",
		limit=1,
	)
	criteria = _scorecard_criteria_payload()
	latest = dict(latest_rows[0]) if latest_rows else None
	if latest:
		frappe.has_permission("AYP Candidate Scorecard", "read", latest["name"], throw=True)
		persisted_rows = frappe.get_doc("AYP Candidate Scorecard", latest["name"]).criteria
		persisted_by_key = {row["criterion_key"]: row for row in persisted_rows}
		for criterion in criteria:
			persisted = persisted_by_key.get(criterion["criterion_key"])
			if persisted:
				criterion["rating"] = persisted["rating"]
				criterion["evidence"] = persisted["evidence"]
	return {"criteria": criteria, "latest": latest}


@frappe.whitelist(methods=["POST"])
def save_scorecard(applicant, criteria):
	frappe.only_for(PAGE_ROLES)
	frappe.has_permission("Job Applicant", "write", applicant, throw=True)
	doc = frappe.get_doc("Job Applicant", applicant)
	if not (doc.job_title or ""):
		frappe.throw(_("La aplicación debe pertenecer a una vacante antes de evaluarla."), frappe.ValidationError)
	if doc.get("custom_cv_processing_status") not in CV_SCORE_READY:
		frappe.throw(
			_("El CV debe estar Procesado o Verificado manualmente antes de crear un scorecard."),
			frappe.ValidationError,
		)
	try:
		scorecard = Scorecard.from_input(criteria)
	except CandidateScoringValidationError as exc:
		frappe.throw(_(str(exc)), frappe.ValidationError)

	now = frappe.utils.now_datetime()
	savepoint = "ayp_candidate_scorecard_{0}".format(frappe.generate_hash(length=12))
	frappe.db.savepoint(savepoint)
	try:
		# Serialize evaluations for one applicant so two reviewers cannot assign
		# the same history version or overwrite the latest projection out of order.
		frappe.db.sql(
			"SELECT name FROM `tabJob Applicant` WHERE name = %s FOR UPDATE",
			(applicant,),
		)
		doc = frappe.get_doc("Job Applicant", applicant, for_update=True)
		if not (doc.job_title or ""):
			frappe.throw(_("La aplicación debe pertenecer a una vacante antes de evaluarla."), frappe.ValidationError)
		# The status is only a cached projection. Revalidate the locked applicant's
		# exact current private/Clean attachment and SHA binding before persisting
		# any score or latest-score projection.
		validate_candidate_ready_for_scoring(doc)
		version = frappe.db.count("AYP Candidate Scorecard", {"applicant": applicant}) + 1
		scorecard_doc = frappe.get_doc(
			{
				"doctype": "AYP Candidate Scorecard",
				"applicant": doc.name,
				"candidate_profile": doc.custom_candidate_profile or "",
				"job_opening": doc.job_title,
				"version": version,
				"total_score": scorecard.total_score,
				"recommendation": scorecard.recommendation,
				"explanation": scorecard.explanation,
				"scored_by": frappe.session.user,
				"scored_on": now,
				"criteria": [
					{
						"criterion_key": row.criterion_key,
						"criterion_label": row.criterion_label,
						"weight": row.weight,
						"rating": row.rating,
						"weighted_score": row.weighted_score,
						"evidence": row.evidence,
					}
					for row in scorecard.rows
				],
			}
		).insert(ignore_permissions=True)
		doc.custom_ayp_governed = 1
		doc.db_set(
			{
				"custom_ayp_governed": 1,
				"custom_candidate_score": scorecard.total_score,
				"custom_candidate_recommendation": scorecard.recommendation,
				"custom_candidate_scorecard": scorecard_doc.name,
				"custom_candidate_scored_on": now,
			}
		)
		doc.add_comment(
			comment_type="Info",
			text=_("Scorecard AyP {0}: {1}/100 · {2}.").format(
				scorecard_doc.name,
				scorecard.total_score,
				scorecard.recommendation,
			),
		)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	return {
		"scorecard": scorecard_doc.name,
		"total_score": scorecard.total_score,
		"recommendation": scorecard.recommendation,
		"explanation": scorecard.explanation,
	}


@frappe.whitelist(methods=["POST"])
def verify_candidate_document_manually(applicant: str, reason: str):
	frappe.only_for(PAGE_ROLES)
	frappe.has_permission("Job Applicant", "write", applicant, throw=True)
	reason = str(reason or "").strip()
	if len(reason) < 20 or len(reason) > 500:
		frappe.throw(_("El motivo debe tener entre 20 y 500 caracteres."), frappe.ValidationError)
	frappe.db.sql("SELECT name FROM `tabJob Applicant` WHERE name = %s FOR UPDATE", (applicant,))
	doc = frappe.get_doc("Job Applicant", applicant, for_update=True)
	previous_status = doc.get("custom_cv_processing_status") or ""
	if previous_status not in CV_MANUAL_REVIEWABLE:
		frappe.throw(
			_("Este estado documental no permite verificación manual. Corrige o reemplaza el archivo."),
			frappe.ValidationError,
		)
	revalidate_candidate_document(doc)
	now = frappe.utils.now_datetime()
	doc.db_set(
		{
			"custom_cv_processing_status": "Verificado manualmente",
			"custom_cv_processing_detail": reason,
			"custom_cv_manual_verified_by": frappe.session.user,
			"custom_cv_manual_verified_on": now,
			"custom_cv_manual_verification_reason": reason,
			"custom_cv_processing_claim": "",
			"custom_cv_processing_started_on": None,
		}
	)
	frappe.get_doc(
		{
			"doctype": EVENT_DOCTYPE,
			"batch_id": "cv-manual:{0}".format(frappe.generate_hash(length=12)),
			"applicant": doc.name,
			"candidate_profile": doc.custom_candidate_profile or "",
			"job_opening": doc.job_title or "",
			"action": "Verificación manual de CV",
			"previous_status": previous_status,
			"new_status": "Verificado manualmente",
			"reason": reason,
			"actor": frappe.session.user,
			"occurred_on": now,
		}
	).insert(ignore_permissions=True)
	return {"applicant": doc.name, "cv_processing_status": "Verificado manualmente"}


@frappe.whitelist(methods=["POST"])
@candidate_profile_governance_update
def update_candidate_profile(applicant: str, action: str, reason: str):
	frappe.only_for(PAGE_ROLES)
	frappe.has_permission("Job Applicant", "write", applicant, throw=True)
	applicant_doc = frappe.get_doc("Job Applicant", applicant)
	profile_name = applicant_doc.custom_candidate_profile
	if not profile_name:
		frappe.throw(_("La aplicación no tiene un perfil canónico vinculado."), frappe.ValidationError)
	frappe.has_permission("AYP Candidate Profile", "write", profile_name, throw=True)
	action = str(action or "").strip()
	reason = str(reason or "").strip()
	if action not in PROFILE_ACTIONS:
		frappe.throw(_("La acción de Talent Pool no es válida."), frappe.ValidationError)
	if len(reason) < 20 or len(reason) > 500:
		frappe.throw(_("El motivo debe tener entre 20 y 500 caracteres."), frappe.ValidationError)

	savepoint = "ayp_profile_{0}".format(frappe.generate_hash(length=12))
	frappe.db.savepoint(savepoint)
	try:
		# Serialize with intake and manual identity corrections, then reload the
		# application before trusting its canonical profile binding.
		acquire_candidate_identity_lock()
		frappe.db.sql("SELECT name FROM `tabJob Applicant` WHERE name = %s FOR UPDATE", (applicant,))
		applicant_doc = frappe.get_doc("Job Applicant", applicant, for_update=True)
		if applicant_doc.custom_candidate_profile != profile_name:
			frappe.throw(_("La aplicación cambió de perfil; recarga antes de continuar."), frappe.ValidationError)
		resolved_profile = resolve_candidate_profile(profile_name, for_update=True)
		if not resolved_profile:
			frappe.throw(_("El perfil canónico ya no existe."), frappe.ValidationError)
		if resolved_profile.name != profile_name:
			frappe.throw(_("El perfil fue fusionado; recarga antes de continuar."), frappe.ValidationError)
		frappe.db.sql(
			"SELECT name FROM `tabAYP Candidate Profile` WHERE name = %s FOR UPDATE",
			(profile_name,),
		)
		profile = frappe.get_doc("AYP Candidate Profile", profile_name, for_update=True)
		previous_state = "{0} / {1}".format(profile.talent_pool_status, profile.dedupe_status)
		if action in {"retain", "priority"} and profile.do_not_contact:
			frappe.throw(
				_("El perfil está marcado como No contactar. Usa el flujo aprobado de privacidad para levantar ese bloqueo."),
				frappe.ValidationError,
			)
		profile.talent_pool_status = PROFILE_ACTIONS[action]
		if action in {"no_interest", "disposed"}:
			profile.do_not_contact = 1
		profile.disposition_reason = reason
		profile.save()
		frappe.get_doc(
			{
				"doctype": EVENT_DOCTYPE,
				"batch_id": "profile:{0}".format(frappe.generate_hash(length=12)),
				"applicant": applicant_doc.name,
				"candidate_profile": profile.name,
				"job_opening": applicant_doc.job_title or "",
				"action": "Decisión Talent Pool",
				"previous_status": previous_state,
				"new_status": "{0} / {1}".format(profile.talent_pool_status, profile.dedupe_status),
				"reason": reason,
				"actor": frappe.session.user,
				"occurred_on": frappe.utils.now_datetime(),
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	return {
		"profile": profile.name,
		"talent_pool_status": profile.talent_pool_status,
		"dedupe_status": profile.dedupe_status,
	}


def _lock_profiles(profile_names: list[str]) -> None:
	profile_names = sorted({name for name in profile_names if name})
	if not profile_names:
		return
	placeholders = ", ".join(["%s"] * len(profile_names))
	frappe.db.sql(
		"SELECT name FROM `tabAYP Candidate Profile` WHERE name IN ({0}) ORDER BY name FOR UPDATE".format(
			placeholders
		),
		tuple(profile_names),
	)


def _linked_applications_for_update(profile_name: str) -> list[str]:
	return frappe.db.sql(
		"""
		SELECT name FROM `tabJob Applicant`
		WHERE custom_candidate_profile = %s
		ORDER BY name FOR UPDATE
		""",
		(profile_name,),
		pluck=True,
	)


def _candidate_profile_projection(profile_name: str) -> tuple[int, str]:
	rows = frappe.db.sql(
		"""
		SELECT name FROM `tabJob Applicant`
		WHERE custom_candidate_profile = %s
		ORDER BY creation DESC, name DESC
		LIMIT 1
		""",
		(profile_name,),
		pluck=True,
	)
	return frappe.db.count("Job Applicant", {"custom_candidate_profile": profile_name}), (rows[0] if rows else "")


def _identity_preview_payload(applicant_doc, source_profile, target_profile, operation: str, source_applications: list[str]):
	moved_applications = source_applications if operation == "merge" else [applicant_doc.name]
	result_do_not_contact = bool(source_profile.do_not_contact) or bool(
		target_profile and target_profile.do_not_contact
	)
	result_status = target_profile.talent_pool_status if target_profile else source_profile.talent_pool_status
	if result_do_not_contact and result_status not in {"Sin interés", "Eliminación solicitada", "Dispuesto"}:
		result_status = "Sin interés"
	payload = {
		"operation": operation,
		"applicant": applicant_doc.name,
		"source_profile": source_profile.name,
		"source_name": source_profile.candidate_name,
		"source_applications": list(source_applications),
		"moved_applications": list(moved_applications),
		"target_profile": target_profile.name if target_profile else "",
		"target_name": target_profile.candidate_name if target_profile else "",
		"target_application_count": int(target_profile.application_count or 0) if target_profile else 0,
		"source_do_not_contact": bool(source_profile.do_not_contact),
		"target_do_not_contact": bool(target_profile and target_profile.do_not_contact),
		"result_do_not_contact": result_do_not_contact,
		"result_talent_pool_status": result_status,
		"source_becomes_tombstone": operation == "merge",
	}
	payload["binding"] = hashlib.sha256(
		json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
	).hexdigest()
	return payload


@frappe.whitelist(methods=["POST"])
def preview_candidate_identity(applicant: str, operation: str, target_profile: str | None = None):
	frappe.only_for(PAGE_ROLES)
	frappe.has_permission("Job Applicant", "write", applicant, throw=True)
	applicant_doc = frappe.get_doc("Job Applicant", applicant)
	source_name = applicant_doc.custom_candidate_profile
	operation = str(operation or "").strip()
	target_name = str(target_profile or "").strip()
	if not source_name:
		frappe.throw(_("La aplicación no tiene un perfil canónico vinculado."), frappe.ValidationError)
	if operation not in {"split", "relink", "merge"}:
		frappe.throw(_("La operación de identidad no es válida."), frappe.ValidationError)
	source = resolve_candidate_profile(source_name)
	if not source or source.name != source_name:
		frappe.throw(_("El perfil origen ya fue fusionado; recarga antes de continuar."), frappe.ValidationError)
	frappe.has_permission("AYP Candidate Profile", "read", source.name, throw=True)
	source = frappe.get_doc("AYP Candidate Profile", source.name)
	target = None
	if operation in {"relink", "merge"}:
		if not target_name or target_name == source.name:
			frappe.throw(_("Selecciona un perfil objetivo diferente."), frappe.ValidationError)
		target = resolve_candidate_profile(target_name)
		if not target or target.name == source.name:
			frappe.throw(_("El perfil objetivo no existe o resuelve al origen."), frappe.ValidationError)
		frappe.has_permission("AYP Candidate Profile", "read", target.name, throw=True)
		target = frappe.get_doc("AYP Candidate Profile", target.name)
	applications = frappe.get_list(
		"Job Applicant",
		filters={"custom_candidate_profile": source.name},
		pluck="name",
		order_by="name asc",
		page_length=0,
	)
	if applicant not in applications:
		frappe.throw(_("La aplicación cambió de perfil; recarga antes de continuar."), frappe.ValidationError)
	return _identity_preview_payload(applicant_doc, source, target, operation, applications)


@frappe.whitelist(methods=["POST"])
@candidate_profile_governance_update
def resolve_candidate_identity(
	applicant: str,
	operation: str,
	target_profile: str | None,
	reason: str,
	preview_binding: str | None = None,
):
	frappe.only_for(PAGE_ROLES)
	frappe.has_permission("Job Applicant", "write", applicant, throw=True)
	applicant_doc = frappe.get_doc("Job Applicant", applicant)
	source_profile_name = applicant_doc.custom_candidate_profile
	if not source_profile_name:
		frappe.throw(_("La aplicación no tiene un perfil canónico vinculado."), frappe.ValidationError)
	operation = str(operation or "").strip()
	target_profile = str(target_profile or "").strip()
	reason = str(reason or "").strip()
	if operation not in {"split", "relink", "merge"}:
		frappe.throw(_("La operación de identidad no es válida."), frappe.ValidationError)
	if len(reason) < 20 or len(reason) > 500:
		frappe.throw(_("El motivo debe tener entre 20 y 500 caracteres."), frappe.ValidationError)
	if operation in {"relink", "merge"}:
		if not target_profile or target_profile == source_profile_name:
			frappe.throw(_("Selecciona un perfil objetivo diferente."), frappe.ValidationError)
		if not frappe.db.exists("AYP Candidate Profile", target_profile):
			frappe.throw(_("El perfil objetivo no existe."), frappe.ValidationError)
		frappe.has_permission("AYP Candidate Profile", "write", target_profile, throw=True)
	frappe.has_permission("AYP Candidate Profile", "write", source_profile_name, throw=True)
	if operation == "merge" and not str(preview_binding or "").strip():
		frappe.throw(_("Revisa el impacto autoritativo antes de fusionar identidades."), frappe.ValidationError)

	savepoint = "ayp_identity_{0}".format(frappe.generate_hash(length=12))
	frappe.db.savepoint(savepoint)
	try:
		# The same advisory lock guards automatic intake matching, so a new
		# application cannot bind to a source while it is being tombstoned.
		acquire_candidate_identity_lock()
		# Re-lock and reload the applicant before trusting its profile binding.
		frappe.db.sql("SELECT name FROM `tabJob Applicant` WHERE name = %s FOR UPDATE", (applicant,))
		applicant_doc = frappe.get_doc("Job Applicant", applicant, for_update=True)
		if applicant_doc.custom_candidate_profile != source_profile_name:
			frappe.throw(_("La aplicación cambió de perfil; recarga antes de continuar."), frappe.ValidationError)
		resolved_source = resolve_candidate_profile(source_profile_name, for_update=True)
		if not resolved_source or resolved_source.name != source_profile_name:
			frappe.throw(_("El perfil origen ya fue fusionado; recarga antes de continuar."), frappe.ValidationError)
		if target_profile:
			resolved_target = resolve_candidate_profile(target_profile, for_update=True)
			if not resolved_target:
				frappe.throw(_("El perfil objetivo no existe."), frappe.ValidationError)
			target_profile = resolved_target.name
			if target_profile == source_profile_name:
				frappe.throw(_("Selecciona un perfil objetivo diferente."), frappe.ValidationError)
		_lock_profiles([name for name in (source_profile_name, target_profile) if name])
		source_profile = frappe.get_doc("AYP Candidate Profile", source_profile_name, for_update=True)
		source_applications = _linked_applications_for_update(source_profile_name)
		if applicant not in source_applications:
			frappe.throw(_("La aplicación cambió de perfil; recarga antes de continuar."), frappe.ValidationError)
		if operation == "merge":
			target_for_preview = frappe.get_doc("AYP Candidate Profile", target_profile, for_update=True)
			locked_preview = _identity_preview_payload(
				applicant_doc,
				source_profile,
				target_for_preview,
				operation,
				source_applications,
			)
			if locked_preview["binding"] != str(preview_binding or ""):
				frappe.throw(_("El impacto de la fusión cambió; revisa el preview nuevamente."), frappe.ValidationError)

		if operation == "split":
			new_profile = frappe.get_doc(
				{
					"doctype": "AYP Candidate Profile",
					"candidate_name": (applicant_doc.applicant_name or "").strip(),
					"primary_email": (applicant_doc.email_id or "").strip(),
					"primary_phone": (applicant_doc.phone_number or "").strip(),
					"normalized_email": normalize_email(applicant_doc.email_id),
					"normalized_phone": normalize_phone(applicant_doc.phone_number),
					"latest_cv_sha256": (applicant_doc.get("custom_cv_sha256") or "").strip().lower(),
					"privacy_notice_version": applicant_doc.get("custom_privacy_notice_version") or "",
					"talent_pool_status": source_profile.talent_pool_status,
					"dedupe_status": "Manual",
					"do_not_contact": source_profile.do_not_contact,
					"disposition_reason": reason if source_profile.do_not_contact else "",
					"latest_application": applicant,
					"application_count": 1,
				}
			).insert(ignore_permissions=True)
			target_profile = new_profile.name
			target = new_profile
			moved_applications = [applicant]
			action_label = "Separar identidad"
		elif operation == "relink":
			target = frappe.get_doc("AYP Candidate Profile", target_profile)
			moved_applications = [applicant]
			action_label = "Revincular identidad"
		else:
			target = frappe.get_doc("AYP Candidate Profile", target_profile)
			moved_applications = source_applications
			action_label = "Fusionar identidad"

		target_profile = str(target_profile)
		for moved_name in moved_applications:
			frappe.has_permission("Job Applicant", "write", moved_name, throw=True)
			frappe.db.set_value(
				"Job Applicant",
				moved_name,
				{
					"custom_candidate_profile": target_profile,
					"custom_dedupe_status": "Manual",
					"custom_ayp_governed": 1,
				},
				update_modified=False,
			)

		remaining_source, source_latest = _candidate_profile_projection(source_profile_name)
		source_values = {
			"dedupe_status": "Manual",
			"application_count": remaining_source,
			"latest_application": source_latest,
		}
		if operation == "merge":
			source_values.update(
				{
					"merged_into": target_profile,
					"merged_on": frappe.utils.now_datetime(),
					"merged_by": frappe.session.user,
					"talent_pool_status": "Fusionado",
				}
			)
		frappe.db.set_value("AYP Candidate Profile", source_profile_name, source_values, update_modified=False)
		target_count, target_latest = _candidate_profile_projection(target_profile)
		target.dedupe_status = "Manual"
		target.application_count = target_count
		target.latest_application = target_latest
		# A no-contact flag can only move upward through an identity operation.
		if source_profile.do_not_contact and not target.do_not_contact:
			target.do_not_contact = 1
			target.disposition_reason = reason
		if target.do_not_contact and target.talent_pool_status not in {
			"Sin interés",
			"Eliminación solicitada",
			"Dispuesto",
		}:
			target.talent_pool_status = "Sin interés"
			target.disposition_reason = target.disposition_reason or reason
		target.save()

		frappe.get_doc(
			{
				"doctype": EVENT_DOCTYPE,
				"batch_id": "identity:{0}".format(frappe.generate_hash(length=12)),
				"applicant": applicant,
				"candidate_profile": target_profile,
				"job_opening": applicant_doc.job_title or "",
				"action": action_label,
				"previous_status": source_profile_name,
				"new_status": target_profile,
				"reason": reason,
				"actor": frappe.session.user,
				"occurred_on": frappe.utils.now_datetime(),
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	return {
		"source_profile": source_profile_name,
		"target_profile": target_profile,
		"moved_applications": len(moved_applications),
	}
