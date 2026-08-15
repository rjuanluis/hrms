from frappe.model.document import Document

from hrms.security.candidate_cv import (
	prevent_candidate_cv_file_deletion,
	validate_candidate_cv_file_evidence,
)


class CandidateCVFileMixin(Document):
	"""Run candidate-CV guards before Frappe mutates or deletes file bytes."""

	def validate(self):
		validate_candidate_cv_file_evidence(self)
		super().validate()

	def on_trash(self):
		prevent_candidate_cv_file_deletion(self)
		super().on_trash()
