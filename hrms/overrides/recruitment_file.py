from __future__ import annotations

from hrms.security.candidate_cv import prevent_recruitment_cv_file_deletion


class RecruitmentFileGovernance:
	"""Run recruitment-retention governance before Frappe deletes physical bytes."""

	def on_trash(self):
		prevent_recruitment_cv_file_deletion(self)
		super().on_trash()  # type: ignore[attr-defined]
