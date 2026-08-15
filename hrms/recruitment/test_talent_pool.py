from __future__ import annotations

import unittest

if __package__:
	from hrms.recruitment.matching import (
		DEDUPE_MATCHED,
		DEDUPE_NEW,
		DEDUPE_REVIEW,
		EMAIL_RECRUITMENT_SOURCE,
		candidate_lock_names,
		choose_profile_match,
		names_are_compatible,
		normalize_email,
		normalize_name,
		normalize_phone,
		requires_name_compatibility,
		should_enroll_in_talent_pool,
	)
else:
	from matching import (
		DEDUPE_MATCHED,
		DEDUPE_NEW,
		DEDUPE_REVIEW,
		EMAIL_RECRUITMENT_SOURCE,
		candidate_lock_names,
		choose_profile_match,
		names_are_compatible,
		normalize_email,
		normalize_name,
		normalize_phone,
		requires_name_compatibility,
		should_enroll_in_talent_pool,
	)


class TestTalentPoolMatching(unittest.TestCase):
	def test_normalizes_email_case_and_whitespace(self):
		self.assertEqual(normalize_email("  Persona@Ejemplo.COM "), "persona@ejemplo.com")

	def test_normalizes_dominican_local_phone(self):
		self.assertEqual(normalize_phone("(809) 555-0123"), "+18095550123")
		self.assertEqual(normalize_phone("1-829-555-0123"), "+18295550123")

	def test_preserves_explicit_international_prefix(self):
		self.assertEqual(normalize_phone("+34 612 34 56 78"), "+34612345678")

	def test_normalizes_candidate_names(self):
		self.assertEqual(normalize_name("  José  Núñez-Rodríguez "), "jose nunez rodriguez")

	def test_name_compatibility_allows_middle_name_variation(self):
		self.assertTrue(names_are_compatible("Juan Luis Rodríguez", "Juan Rodríguez"))
		self.assertFalse(names_are_compatible("Juan Rodríguez", "María Rodríguez"))

	def test_no_match_creates_new_profile(self):
		self.assertEqual(
			choose_profile_match({"email": set(), "phone": set(), "cv": set()}),
			(None, DEDUPE_NEW),
		)

	def test_matching_identifiers_reuse_one_profile(self):
		self.assertEqual(
			choose_profile_match(
				{
					"email": {"AYP-CAND-2026-00001"},
					"phone": {"AYP-CAND-2026-00001"},
					"cv": set(),
				}
			),
			("AYP-CAND-2026-00001", DEDUPE_MATCHED),
		)

	def test_conflicting_identifiers_never_auto_merge(self):
		self.assertEqual(
			choose_profile_match(
				{
					"email": {"AYP-CAND-2026-00001"},
					"phone": {"AYP-CAND-2026-00002"},
					"cv": set(),
				}
			),
			(None, DEDUPE_REVIEW),
		)

	def test_duplicate_identifier_never_auto_merges(self):
		self.assertEqual(
			choose_profile_match(
				{
					"email": {"AYP-CAND-2026-00001", "AYP-CAND-2026-00002"},
					"phone": set(),
					"cv": set(),
				}
			),
			(None, DEDUPE_REVIEW),
		)

	def test_same_email_uses_overlapping_lock_even_when_phone_changes(self):
		first = set(
			candidate_lock_names(email="persona@example.com", phone="+18095550123", cv_sha256="a" * 64)
		)
		second = set(
			candidate_lock_names(email="persona@example.com", phone="+18295550123", cv_sha256="b" * 64)
		)
		self.assertEqual(len(first & second), 1)
		self.assertTrue(all("persona@example.com" not in lock_name for lock_name in first | second))
		self.assertTrue(all(len(lock_name) <= 64 for lock_name in first | second))

	def test_single_cv_signal_requires_name_compatibility(self):
		self.assertTrue(requires_name_compatibility(["cv"]))

	def test_multiple_matching_signals_do_not_require_name_gate(self):
		self.assertFalse(requires_name_compatibility(["email", "cv"]))

	def test_email_without_consent_stays_out_of_talent_pool(self):
		self.assertFalse(
			should_enroll_in_talent_pool(
				source=EMAIL_RECRUITMENT_SOURCE,
				has_data_processing_consent=False,
			)
		)

	def test_web_form_behavior_is_unchanged_but_email_checkbox_cannot_enroll(self):
		self.assertTrue(should_enroll_in_talent_pool(source=None, has_data_processing_consent=True))
		self.assertFalse(
			should_enroll_in_talent_pool(
				source=EMAIL_RECRUITMENT_SOURCE,
				has_data_processing_consent=True,
			)
		)


if __name__ == "__main__":
	unittest.main()
