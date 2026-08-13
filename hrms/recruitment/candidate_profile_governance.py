from __future__ import annotations

from functools import wraps

import frappe


def candidate_profile_governance_update(function):
	"""Allow governed profile fields to change only inside an audited server operation."""

	@wraps(function)
	def wrapper(*args, **kwargs):
		previous = frappe.flags.get("ayp_candidate_profile_governance_update")
		frappe.flags.ayp_candidate_profile_governance_update = True
		try:
			return function(*args, **kwargs)
		finally:
			frappe.flags.ayp_candidate_profile_governance_update = previous

	return wrapper
