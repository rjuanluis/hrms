from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from hrms.recruitment.candidate_profile_governance import candidate_profile_governance_update

DISPOSITION_STATUSES = {"Sin interés", "Eliminación solicitada", "Dispuesto"}
PRIVACY_GOVERNANCE_FIELDS = frozenset(
	{
		"talent_pool_status",
		"do_not_contact",
		"disposition_reason",
		"disposition_on",
		"disposition_by",
		"merged_into",
		"merged_on",
		"merged_by",
	}
)


class AYPCandidateProfile(Document):
	def validate(self):
		self._validate_privacy_governance()
		self._validate_merge_tombstone()
		self._validate_contact_monotonic()
		self._validate_disposition()
		self._validate_private_photo()

	def after_insert(self):
		self._bind_private_photo()

	def on_trash(self):
		frappe.throw(
			_("La eliminación directa está deshabilitada. Usa el flujo aprobado de privacidad y retención.")
		)

	def _validate_privacy_governance(self) -> None:
		if self.is_new() or frappe.flags.get("ayp_candidate_profile_governance_update"):
			return
		changed = sorted(field for field in PRIVACY_GOVERNANCE_FIELDS if self.has_value_changed(field))
		if changed:
			frappe.throw(
				_(
					"El estado de privacidad y Talent Pool solo puede cambiar mediante una operación auditada."
				),
				frappe.PermissionError,
			)

	def _validate_merge_tombstone(self) -> None:
		if not self.merged_into:
			return
		if not getattr(frappe.flags, "ayp_identity_merge", False):
			frappe.throw(_("Un perfil fusionado es un alias histórico de solo lectura."))
		if self.merged_into == self.name:
			frappe.throw(_("Un perfil no puede redirigirse a sí mismo."))
		if not self.is_new() and self.has_value_changed("merged_into"):
			frappe.throw(
				_("La redirección de identidad solo puede establecerse mediante el flujo de fusión.")
			)
		if self.talent_pool_status != "Fusionado" or self.dedupe_status != "Manual":
			frappe.throw(
				_("Un perfil fusionado debe permanecer neutralizado y marcado para auditoría manual.")
			)

	def _validate_contact_monotonic(self) -> None:
		if not self.is_new() and self.has_value_changed("do_not_contact") and not self.do_not_contact:
			frappe.throw(
				_("No contactar es monotónico. Usa un flujo aprobado de privacidad para levantar el bloqueo.")
			)

	def _validate_disposition(self) -> None:
		requires_reason = self.talent_pool_status in DISPOSITION_STATUSES or self.do_not_contact
		if self.do_not_contact and self.talent_pool_status not in DISPOSITION_STATUSES | {"Fusionado"}:
			frappe.throw(
				_("Un perfil marcado No contactar no puede permanecer activo o prioritario."),
				frappe.ValidationError,
			)
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
