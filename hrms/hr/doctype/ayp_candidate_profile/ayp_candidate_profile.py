from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

DISPOSITION_STATUSES = {"Sin interés", "Eliminación solicitada", "Dispuesto"}


class AYPCandidateProfile(Document):
	def validate(self):
		self._validate_disposition()
		self._validate_private_photo()

	def _validate_disposition(self) -> None:
		if self.talent_pool_status in DISPOSITION_STATUSES and not (self.disposition_reason or "").strip():
			frappe.throw(_("Debes registrar una razón antes de disponer el candidato."))
		if self.has_value_changed("talent_pool_status") and self.talent_pool_status in DISPOSITION_STATUSES:
			self.disposition_on = now_datetime()
			self.disposition_by = frappe.session.user

	def _validate_private_photo(self) -> None:
		if not self.candidate_photo:
			return
		file_record = frappe.db.get_value(
			"File",
			{"file_url": self.candidate_photo},
			["is_private", "attached_to_doctype", "attached_to_name"],
			as_dict=True,
		)
		valid_attachment = (
			file_record
			and file_record.is_private
			and file_record.attached_to_doctype
			in (
				None,
				"",
				self.doctype,
			)
		)
		if valid_attachment and file_record.attached_to_name not in (None, "", self.name):
			valid_attachment = False
		if not valid_attachment:
			frappe.throw(
				_("La foto del candidato debe almacenarse como archivo privado vinculado al perfil.")
			)
