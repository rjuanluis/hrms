from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

DISPOSITION_STATUSES = {"Sin interés", "Eliminación solicitada", "Dispuesto"}
DELETION_ELIGIBLE_STATUSES = {"Sin interés", "Eliminación solicitada"}


class AYPCandidateProfile(Document):
	def validate(self):
		self._validate_disposition()
		self._validate_private_photo()

	def after_insert(self):
		self._bind_private_photo()

	def on_trash(self):
		if self.talent_pool_status not in DELETION_ELIGIBLE_STATUSES:
			frappe.throw(
				_("Solo puedes eliminar un perfil marcado como Sin interés o Eliminación solicitada.")
			)

	def _validate_disposition(self) -> None:
		requires_reason = self.talent_pool_status in DISPOSITION_STATUSES or self.do_not_contact
		if requires_reason and not (self.disposition_reason or "").strip():
			frappe.throw(_("Debes registrar una razón antes de disponer el candidato."))
		status_changed = (
			self.has_value_changed("talent_pool_status") and self.talent_pool_status in DISPOSITION_STATUSES
		)
		contact_blocked = self.has_value_changed("do_not_contact") and self.do_not_contact
		if status_changed or contact_blocked:
			self.disposition_on = now_datetime()
			self.disposition_by = frappe.session.user

	def _validate_private_photo(self) -> None:
		if not self.candidate_photo:
			return
		file_record = frappe.db.get_value(
			"File",
			{"file_url": self.candidate_photo},
			["is_private", "attached_to_doctype", "attached_to_name", "attached_to_field"],
			as_dict=True,
		)
		unbound_attachment = (
			file_record
			and not file_record.attached_to_doctype
			and not file_record.attached_to_name
			and not file_record.attached_to_field
		)
		valid_attachment = (
			file_record
			and file_record.is_private
			and self.candidate_photo.startswith("/private/files/")
			and (
				(
					file_record.attached_to_doctype == self.doctype
					and file_record.attached_to_name == self.name
					and file_record.attached_to_field == "candidate_photo"
				)
				or (self.is_new() and unbound_attachment)
			)
		)
		if not valid_attachment:
			frappe.throw(
				_("La foto del candidato debe almacenarse como archivo privado vinculado al perfil.")
			)

	def _bind_private_photo(self) -> None:
		if not self.candidate_photo:
			return
		file_name = frappe.db.get_value("File", {"file_url": self.candidate_photo}, "name")
		if file_name:
			frappe.db.set_value(
				"File",
				file_name,
				{
					"attached_to_doctype": self.doctype,
					"attached_to_name": self.name,
					"attached_to_field": "candidate_photo",
				},
				update_modified=False,
			)
