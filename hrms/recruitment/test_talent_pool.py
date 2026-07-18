from __future__ import annotations

import unittest

if __package__:
	from hrms.recruitment.matching import (
		DEDUPE_MATCHED,
		DEDUPE_NEW,
		DEDUPE_REVIEW,
		choose_profile_match,
		names_are_compatible,
		normalize_email,
		normalize_name,
		normalize_phone,
	)
else:
	from matching import (
		DEDUPE_MATCHED,
		DEDUPE_NEW,
		DEDUPE_REVIEW,
		choose_profile_match,
		names_are_compatible,
		normalize_email,
		normalize_name,
		normalize_phone,
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


if __name__ == "__main__":
	unittest.main()
