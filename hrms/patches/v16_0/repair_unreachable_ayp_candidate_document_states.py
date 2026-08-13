import frappe

REPAIRABLE_STATUSES = ("Revisión manual", "Ilegible")


def execute():
	"""Repair historical CV states that cannot satisfy exact-SHA revalidation."""

	frappe.db.sql(
		"""
		UPDATE `tabJob Applicant`
		SET custom_cv_processing_status = CASE
				WHEN COALESCE(custom_cv_sha256, '') != '' THEN 'Pendiente'
				ELSE 'Error de seguridad'
			END,
			custom_cv_processing_detail = CASE
				WHEN COALESCE(custom_cv_sha256, '') != '' THEN 'CV histórico recuperado; extracción pendiente.'
				ELSE 'El CV histórico no conserva una huella de integridad verificada; debe cargarse nuevamente.'
			END,
			custom_cv_processing_queued_on = CASE
				WHEN COALESCE(custom_cv_sha256, '') != '' THEN COALESCE(modified, creation)
				ELSE NULL
			END,
			custom_cv_processing_started_on = NULL,
			custom_cv_processing_claim = ''
		WHERE custom_cv_processing_status IN %s
			AND COALESCE(resume_attachment, '') != ''
			AND (
				COALESCE(custom_cv_sha256, '') = ''
				OR COALESCE(custom_cv_processed_sha256, '') != COALESCE(custom_cv_sha256, '')
			)
		""",
		(REPAIRABLE_STATUSES,),
	)
	frappe.clear_cache()
