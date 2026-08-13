# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt


import datetime
import hashlib
from functools import partial

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder.functions import Avg
from frappe.utils import cint, cstr, get_datetime, get_link_to_form, getdate, nowtime

from hrms.recruitment.interview_decision_domain import (
	InterviewDecisionValidationError,
	validate_interview_backed_application_decision,
)
from hrms.recruitment.interview_governance import (
	CONCURRENT_CHANGE_MESSAGE,
	lock_ayp_interview_decision_dependencies,
)


class DuplicateInterviewRoundError(frappe.ValidationError):
	pass


AYP_INTERVIEW_TYPE = "AyP - Entrevista estructurada"
INTERVIEW_LOCK_TIMEOUT_SECONDS = 10


def _release_interview_lock(lock_name: str) -> None:
	frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
	getattr(frappe.local, "ayp_interview_locks", set()).discard(lock_name)


def acquire_ayp_interview_lock(job_applicant: str, interview_type: str) -> None:
	digest = hashlib.sha256(f"{job_applicant}\0{interview_type}".encode()).hexdigest()[:40]
	lock_name = f"ayp-interview:{digest}"
	held = getattr(frappe.local, "ayp_interview_locks", set())
	frappe.local.ayp_interview_locks = held
	if lock_name in held:
		return
	result = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock_name, INTERVIEW_LOCK_TIMEOUT_SECONDS))
	if not result or result[0][0] != 1:
		frappe.throw(_("No pudimos reservar la entrevista. Intenta nuevamente."), frappe.ValidationError)
	held.add(lock_name)
	release = partial(_release_interview_lock, lock_name)
	frappe.db.after_commit.add(release)
	frappe.db.after_rollback.add(release)


class Interview(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from hrms.hr.doctype.interview_detail.interview_detail import InterviewDetail

		amended_from: DF.Link | None
		average_rating: DF.Rating
		designation: DF.Link | None
		expected_average_rating: DF.Rating
		from_time: DF.Time
		interview_details: DF.Table[InterviewDetail]
		interview_summary: DF.Text | None
		interview_type: DF.Link
		job_applicant: DF.Link
		job_opening: DF.Link | None
		reminded: DF.Check
		resume_link: DF.Data | None
		scheduled_on: DF.Date
		status: DF.Literal["Pending", "Under Review", "Cleared", "Rejected", "Cancelled"]
		to_time: DF.Time
	# end: auto-generated types

	def validate(self):
		self.validate_duplicate_interview()
		self.validate_designation()

	def on_submit(self):
		if self.status not in ["Cleared", "Rejected"]:
			frappe.throw(
				_("Only Interviews with Cleared or Rejected status can be submitted."),
				title=_("Not Allowed"),
			)
		self.show_job_applicant_update_dialog()

	def cancel(self):
		is_ayp = bool(str(self.get("custom_ayp_questions_snapshot") or "").strip())
		try:
			return super().cancel()
		except frappe.QueryDeadlockError:
			if is_ayp:
				raise frappe.ValidationError(CONCURRENT_CHANGE_MESSAGE)
			raise

	def validate_duplicate_interview(self):
		if self.interview_type == AYP_INTERVIEW_TYPE:
			acquire_ayp_interview_lock(self.job_applicant, self.interview_type)
		duplicate_interview = frappe.db.exists(
			"Interview",
			{
				"name": ["!=", self.name],
				"job_applicant": self.job_applicant,
				"interview_type": self.interview_type,
				"docstatus": ["!=", 2] if self.interview_type == AYP_INTERVIEW_TYPE else 1,
			},
		)

		if duplicate_interview:
			frappe.throw(
				_(
					"Job Applicants are not allowed to appear twice for the same Interview Type. Interview {0} already scheduled for Job Applicant {1}"
				).format(
					frappe.bold(get_link_to_form("Interview", duplicate_interview)),
					frappe.bold(self.job_applicant),
				)
			)

	def validate_designation(self):
		applicant_designation = frappe.db.get_value("Job Applicant", self.job_applicant, "designation")
		if self.designation:
			if self.designation != applicant_designation:
				frappe.throw(
					_(
						"Interview Type {0} is only for Designation {1}. Job Applicant has applied for the role {2}"
					).format(self.interview_type, frappe.bold(self.designation), applicant_designation),
					exc=DuplicateInterviewRoundError,
				)
		else:
			self.designation = applicant_designation

	def show_job_applicant_update_dialog(self):
		job_applicant_status = self.get_job_applicant_status()
		if not job_applicant_status:
			return

		job_application_name = frappe.db.get_value("Job Applicant", self.job_applicant, "applicant_name")

		frappe.msgprint(
			_("Do you want to update the Job Applicant {0} as {1} based on this interview result?").format(
				frappe.bold(job_application_name), frappe.bold(job_applicant_status)
			),
			title=_("Update Job Applicant"),
			primary_action={
				"label": _("Mark as {0}").format(job_applicant_status),
				"server_action": "hrms.hr.doctype.interview.interview.update_job_applicant_status",
				"args": {
					"job_applicant": self.job_applicant,
					"status": job_applicant_status,
					"interview": self.name,
				},
			},
		)

	def get_job_applicant_status(self) -> str | None:
		status_map = {"Cleared": "Accepted", "Rejected": "Rejected"}
		return status_map.get(self.status, None)

	@frappe.whitelist(methods=["POST"])
	def reschedule_interview(
		self, scheduled_on: datetime.date, from_time: datetime.time, to_time: datetime.time
	) -> None:
		if scheduled_on == self.scheduled_on and from_time == self.from_time and to_time == self.to_time:
			frappe.msgprint(
				_("No changes found in timings."), indicator="orange", title=_("Interview Not Rescheduled")
			)
			return

		original_date = self.scheduled_on
		original_from_time = self.from_time
		original_to_time = self.to_time

		self.db_set({"scheduled_on": scheduled_on, "from_time": from_time, "to_time": to_time})
		self.notify_update()

		recipients = get_recipients(self.name)

		try:
			frappe.sendmail(
				recipients=recipients,
				subject=_("Interview: {0} Rescheduled").format(self.name),
				message=_("Your Interview session is rescheduled from {0} {1} - {2} to {3} {4} - {5}").format(
					original_date,
					original_from_time,
					original_to_time,
					self.scheduled_on,
					self.from_time,
					self.to_time,
				),
				reference_doctype=self.doctype,
				reference_name=self.name,
			)
		except Exception:
			frappe.msgprint(
				_(
					"Failed to send the Interview Reschedule notification. Please configure your email account."
				)
			)

		frappe.msgprint(_("Interview Rescheduled successfully"), indicator="green")

	def on_discard(self):
		self.db_set("status", "Cancelled")


@frappe.whitelist()
def get_interviewers(interview_type: str) -> list[dict]:
	frappe.has_permission("Interview Type", "read", interview_type, throw=True)
	return frappe.get_all("Interviewer", filters={"parent": interview_type}, fields=["user as interviewer"])


def get_recipients(name, for_feedback=0):
	interview = frappe.get_doc("Interview", name)
	interviewers = [d.interviewer for d in interview.interview_details]

	if for_feedback:
		feedback_given_interviewers = frappe.get_all(
			"Interview Feedback", filters={"interview": name, "docstatus": 1}, pluck="interviewer"
		)
		recipients = [d for d in interviewers if d not in feedback_given_interviewers]
	else:
		recipients = interviewers
		if not candidate_contact_is_blocked(interview.job_applicant):
			recipients.append(frappe.db.get_value("Job Applicant", interview.job_applicant, "email_id"))

	return [recipient for recipient in recipients if recipient]


def candidate_contact_is_blocked(job_applicant: str) -> bool:
	profile = frappe.db.get_value("Job Applicant", job_applicant, "custom_candidate_profile")
	if not profile:
		return False
	return bool(frappe.db.get_value("AYP Candidate Profile", profile, "do_not_contact"))


@frappe.whitelist()
def get_feedback(interview: str) -> list[dict]:
	frappe.has_permission("Interview", "read", interview, throw=True)
	frappe.has_permission("Interview Feedback", "read", throw=True)

	interview_feedback = frappe.qb.DocType("Interview Feedback")
	employee = frappe.qb.DocType("Employee")

	return (
		frappe.qb.from_(interview_feedback)
		.select(
			interview_feedback.name,
			interview_feedback.result,
			interview_feedback.modified.as_("added_on"),
			interview_feedback.interviewer.as_("user"),
			interview_feedback.feedback,
			(interview_feedback.average_rating * 5).as_("total_score"),
			employee.employee_name.as_("reviewer_name"),
			employee.designation.as_("reviewer_designation"),
		)
		.left_join(employee)
		.on(interview_feedback.interviewer == employee.user_id)
		.where((interview_feedback.interview == interview) & (interview_feedback.docstatus == 1))
		.orderby(interview_feedback.creation)
	).run(as_dict=True)


@frappe.whitelist()
def get_skill_wise_average_rating(interview: str) -> list[dict]:
	frappe.has_permission("Interview", "read", interview, throw=True)
	skill_assessment = frappe.qb.DocType("Skill Assessment")
	interview_feedback = frappe.qb.DocType("Interview Feedback")
	return (
		frappe.qb.select(
			skill_assessment.skill,
			Avg(skill_assessment.rating).as_("rating"),
		)
		.from_(skill_assessment)
		.join(interview_feedback)
		.on(skill_assessment.parent == interview_feedback.name)
		.where((interview_feedback.interview == interview) & (interview_feedback.docstatus == 1))
		.groupby(skill_assessment.skill)
		.orderby(skill_assessment.idx)
	).run(as_dict=True)


@frappe.whitelist(methods=["POST"])
def update_job_applicant_status(status: str, job_applicant: str, interview: str | None = None):
	if not job_applicant:
		frappe.throw(_("Please specify the job applicant to be updated."))
	if status not in {"Accepted", "Rejected"}:
		frappe.throw(_("Only final interview decisions can update the applicant from this action."))
	if not interview:
		frappe.throw(_("A submitted interview is required for the final decision."), frappe.ValidationError)

	frappe.has_permission("Job Applicant", "write", job_applicant, throw=True)
	try:
		interview_doc, applicant_doc, feedback_rows = lock_ayp_interview_decision_dependencies(
			interview,
			job_applicant,
		)
		frappe.has_permission("Interview", "read", interview_doc, throw=True)
		locked_feedback_names = {row.name for row in feedback_rows if row.docstatus == 1}
		locked_feedback_interviewers = {row.interviewer for row in feedback_rows if row.docstatus == 1}
		if applicant_doc.get("custom_ayp_governed"):
			assigned_interviewers = {
				row.interviewer for row in interview_doc.interview_details if row.interviewer
			}
			if not locked_feedback_names or not assigned_interviewers.issubset(locked_feedback_interviewers):
				frappe.throw(
					_(
						"La decisión final exige feedback enviado y vigente de todas las personas entrevistadoras."
					),
					frappe.ValidationError,
				)
		try:
			validate_interview_backed_application_decision(
				job_applicant,
				status,
				interview_doc,
				require_ayp=bool(applicant_doc.get("custom_ayp_governed")),
			)
		except InterviewDecisionValidationError as exc:
			frappe.throw(_(str(exc)), frappe.ValidationError)

		previous_status = applicant_doc.status
		if previous_status == status:
			frappe.msgprint(_("The Job Applicant is already marked as {0}.").format(status), alert=True)
			return
		frappe.flags.ayp_interview_decision = True
		try:
			applicant_doc.status = status
			if applicant_doc.get("custom_ayp_governed"):
				applicant_doc.custom_ayp_final_interview = interview_doc.name
			applicant_doc.save()
		finally:
			frappe.flags.ayp_interview_decision = False
		frappe.get_doc(
			{
				"doctype": "AYP Candidate Review Event",
				"batch_id": f"interview:{interview_doc.name}",
				"applicant": applicant_doc.name,
				"candidate_profile": applicant_doc.custom_candidate_profile or "",
				"job_opening": applicant_doc.job_title or "",
				"action": "Interview Decision",
				"previous_status": previous_status,
				"new_status": status,
				"reason": interview_doc.custom_ayp_decision_rationale
				or _("Decision recorded from a submitted standard interview."),
				"actor": frappe.session.user,
				"occurred_on": frappe.utils.now_datetime(),
			}
		).insert(ignore_permissions=True)
		applicant_doc.add_comment(
			comment_type="Info",
			text=_("Final decision {0} confirmed from submitted Interview {1}.").format(
				status,
				interview_doc.name,
			),
		)
	except frappe.QueryDeadlockError:
		raise frappe.ValidationError(CONCURRENT_CHANGE_MESSAGE)

	frappe.msgprint(
		_("Updated the Job Applicant status to {0}").format(applicant_doc.status),
		alert=True,
		indicator="green",
	)


def send_interview_reminder():
	reminder_settings = frappe.db.get_value(
		"HR Settings",
		"HR Settings",
		["send_interview_reminder", "interview_reminder_template", "hiring_sender_email"],
		as_dict=True,
	)

	if not cint(reminder_settings.send_interview_reminder):
		return

	remind_before = cstr(frappe.db.get_single_value("HR Settings", "remind_before")) or "01:00:00"
	remind_before = datetime.datetime.strptime(remind_before, "%H:%M:%S")
	reminder_date_time = datetime.datetime.now() + datetime.timedelta(
		hours=remind_before.hour, minutes=remind_before.minute, seconds=remind_before.second
	)

	interviews = frappe.get_all(
		"Interview",
		filters=[
			["scheduled_on", "between", [datetime.datetime.now(), reminder_date_time]],
			["status", "=", "Pending"],
			["reminded", "=", 0],
			["docstatus", "!=", 2],
		],
	)

	interview_template = frappe.get_doc("Email Template", reminder_settings.interview_reminder_template)

	for d in interviews:
		doc = frappe.get_doc("Interview", d.name)
		context = doc.as_dict()
		message = frappe.render_template(interview_template.response, context)
		recipients = get_recipients(doc.name)

		frappe.sendmail(
			sender=reminder_settings.hiring_sender_email,
			recipients=recipients,
			subject=interview_template.subject,
			message=message,
			reference_doctype=doc.doctype,
			reference_name=doc.name,
		)

		doc.db_set("reminded", 1)


def send_daily_feedback_reminder():
	reminder_settings = frappe.db.get_value(
		"HR Settings",
		"HR Settings",
		[
			"send_interview_feedback_reminder",
			"feedback_reminder_notification_template",
			"hiring_sender_email",
		],
		as_dict=True,
	)

	if not cint(reminder_settings.send_interview_feedback_reminder):
		return

	interview_feedback_template = frappe.get_doc(
		"Email Template", reminder_settings.feedback_reminder_notification_template
	)

	interviews = frappe.get_all(
		"Interview",
		filters={
			"status": "Under Review",
			"docstatus": ["!=", 2],
			"scheduled_on": ["<=", getdate()],
			"to_time": ["<=", nowtime()],
		},
		pluck="name",
	)

	for interview in interviews:
		recipients = get_recipients(interview, for_feedback=1)

		doc = frappe.get_doc("Interview", interview)
		context = doc.as_dict()

		message = frappe.render_template(interview_feedback_template.response, context)

		if len(recipients):
			frappe.sendmail(
				sender=reminder_settings.hiring_sender_email,
				recipients=recipients,
				subject=interview_feedback_template.subject,
				message=message,
				reference_doctype="Interview",
				reference_name=interview,
			)


@frappe.whitelist()
def get_expected_skill_set(interview_type: str) -> list[dict]:
	frappe.has_permission("Interview Type", "read", interview_type, throw=True)
	return frappe.get_all(
		"Expected Skill Set", filters={"parent": interview_type}, fields=["skill"], order_by="idx"
	)


@frappe.whitelist(methods=["POST"])
def create_interview_feedback(data: str | dict, interview_name: str, interviewer: str, job_applicant: str):
	import json

	if isinstance(data, str):
		data = json.loads(data)
	data = frappe._dict(data)

	if frappe.session.user != interviewer:
		frappe.throw(_("Only Interviewer Are allowed to submit Interview Feedback"))
	interview = frappe.get_doc("Interview", interview_name)
	frappe.has_permission("Interview", "read", interview, throw=True)
	if interview.job_applicant != job_applicant:
		frappe.throw(_("Interview and Job Applicant do not match."), frappe.ValidationError)
	assigned_interviewers = {row.interviewer for row in interview.interview_details if row.interviewer}
	if interviewer not in assigned_interviewers:
		frappe.throw(_("Only an assigned interviewer can submit feedback."), frappe.PermissionError)

	interview_feedback = frappe.new_doc("Interview Feedback")
	interview_feedback.interview = interview_name
	interview_feedback.interviewer = interviewer
	interview_feedback.job_applicant = job_applicant

	for d in data.skill_set:
		d = frappe._dict(d)
		interview_feedback.append("skill_assessment", d)

	interview_feedback.feedback = data.feedback
	interview_feedback.result = data.result
	interview_feedback.custom_ayp_question_evidence = data.get("custom_ayp_question_evidence") or ""

	interview_feedback.save()
	interview_feedback.submit()

	frappe.msgprint(
		_("Interview Feedback {0} submitted successfully").format(
			get_link_to_form("Interview Feedback", interview_feedback.name)
		)
	)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_interviewer_list(
	doctype: str, txt: str, searchfield: str, start: int, page_len: int, filters: dict
) -> list:
	filters = [
		["Has Role", "parent", "like", f"%{txt}%"],
		["Has Role", "role", "=", "interviewer"],
		["Has Role", "parenttype", "=", "User"],
	]

	if filters and isinstance(filters, list):
		filters.extend(filters)

	return frappe.get_all(
		"Has Role",
		limit_start=start,
		limit_page_length=page_len,
		filters=filters,
		fields=["parent"],
		as_list=1,
	)


@frappe.whitelist()
def get_events(start: str, end: str, filters: str | None = None):
	"""Returns events for Gantt / Calendar view rendering.

	:param start: Start date-time.
	:param end: End date-time.
	:param filters: Filters (JSON).
	"""
	from frappe.desk.calendar import get_event_conditions

	events = []

	event_color = {
		"Pending": "#fff4f0",
		"Under Review": "#d3e8fc",
		"Cleared": "#eaf5ed",
		"Rejected": "#fce7e7",
	}

	conditions = get_event_conditions("Interview", filters)

	# nosemgrep: frappe-semgrep-rules.rules.frappe-using-db-sql
	interviews = frappe.db.sql(
		f"""
			SELECT DISTINCT
				`tabInterview`.name, `tabInterview`.job_applicant, `tabInterview`.interview_type,
				`tabInterview`.scheduled_on, `tabInterview`.status, `tabInterview`.from_time as from_time,
				`tabInterview`.to_time as to_time
			from
				`tabInterview`
			where
				(`tabInterview`.scheduled_on between %(start)s and %(end)s)
				and docstatus != 2
				{conditions}
			""",
		{"start": start, "end": end},
		as_dict=True,
		update={"allDay": 0},
	)

	for d in interviews:
		subject_data = []
		for field in ["name", "job_applicant", "interview_type"]:
			if not d.get(field):
				continue
			subject_data.append(d.get(field))

		color = event_color.get(d.status)
		interview_data = {
			"from": get_datetime(
				"{scheduled_on} {from_time}".format(
					scheduled_on=d.scheduled_on, from_time=d.from_time or "00:00:00"
				)
			),
			"to": get_datetime(
				"{scheduled_on} {to_time}".format(
					scheduled_on=d.scheduled_on, to_time=d.to_time or "00:00:00"
				)
			),
			"name": d.name,
			"subject": "\n".join(subject_data),
			"color": color if color else "#89bcde",
		}

		events.append(interview_data)

	return events
