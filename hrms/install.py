from importlib import import_module

import click

from hrms.setup import after_install as setup


def setup_ayp_recruitment():
	"""Create AyP recruitment fixtures on fresh installs.

	Frappe marks patches as applied during install-app, so these idempotent
	initializers must also run explicitly for a brand-new site.
	"""
	for module_name in (
		"create_ayp_candidate_profiles",
		"add_ayp_candidate_review_fields",
		"add_ayp_candidate_document_processing",
		"add_ayp_interview_governance",
		"create_ayp_interview_kit",
	):
		import_module(f"hrms.patches.v16_0.{module_name}").execute()


def after_install():
	try:
		print("Setting up Frappe HR...")
		setup()
		setup_ayp_recruitment()

		click.secho("Thank you for installing Frappe HR!", fg="green")

	except Exception as e:
		BUG_REPORT_URL = "https://github.com/frappe/hrms/issues/new"
		click.secho(
			"Installation for Frappe HR app failed due to an error."
			" Please try re-installing the app or"
			f" report the issue on {BUG_REPORT_URL} if not resolved.",
			fg="bright_red",
		)
		raise e
