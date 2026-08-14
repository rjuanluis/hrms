from __future__ import annotations

from frappe.core.doctype.file.file import File

from hrms.security.candidate_cv import prevent_recruitment_cv_file_deletion


class RecruitmentProtectedFile(File):
	"""Run recruitment-retention governance before Frappe deletes physical bytes."""

	def on_trash(self):
		prevent_recruitment_cv_file_deletion(self)
		return super().on_trash()
