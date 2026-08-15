from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REVIEW = ROOT / "hrms" / "hr" / "page" / "ayp_candidate_review" / "ayp_candidate_review.py"
MUTATING_PREFIXES = (
	"apply_",
	"save_",
	"update_",
	"resolve_",
	"create_filtered_",
	"freeze_",
	"process_",
	"confirm_",
)


def _decorator_is_post_only(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
	for decorator in node.decorator_list:
		if not isinstance(decorator, ast.Call):
			continue
		function = decorator.func
		if not (
			isinstance(function, ast.Attribute)
			and function.attr == "whitelist"
			and isinstance(function.value, ast.Name)
			and function.value.id == "frappe"
		):
			continue
		for keyword in decorator.keywords:
			if keyword.arg == "methods" and isinstance(keyword.value, ast.List | ast.Tuple):
				methods = [
					element.value for element in keyword.value.elts if isinstance(element, ast.Constant)
				]
				return methods == ["POST"]
	return False


class TestCandidateReviewSecurityContract(unittest.TestCase):
	def test_all_known_cross_controller_mutations_are_post_only(self):
		controllers = {
			ROOT / "hrms" / "hr" / "doctype" / "interview" / "interview.py": {"reschedule_interview"},
			ROOT / "hrms" / "hr" / "doctype" / "job_applicant" / "job_applicant.py": {"create_kanban_board"},
		}
		for path, names in controllers.items():
			tree = ast.parse(path.read_text(encoding="utf-8"))
			functions = {
				node.name: node
				for node in ast.walk(tree)
				if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in names
			}
			self.assertEqual(set(functions), names)
			self.assertEqual(
				[name for name, node in functions.items() if not _decorator_is_post_only(node)], []
			)

	def test_fresh_install_runs_idempotent_ayp_initializers(self):
		install = (ROOT / "hrms" / "install.py").read_text(encoding="utf-8")
		for initializer in (
			"create_ayp_candidate_profiles",
			"add_ayp_candidate_review_fields",
			"add_ayp_interview_governance",
			"create_ayp_interview_kit",
		):
			self.assertIn(initializer, install)

	def test_candidate_interview_email_respects_no_contact(self):
		controller = (ROOT / "hrms" / "hr" / "doctype" / "interview" / "interview.py").read_text(
			encoding="utf-8"
		)
		self.assertIn("candidate_contact_is_blocked", controller)
		self.assertIn("if not candidate_contact_is_blocked(interview.job_applicant)", controller)

	def test_get_feedback_requires_permission_on_requested_interview(self):
		path = ROOT / "hrms" / "hr" / "doctype" / "interview" / "interview.py"
		text = path.read_text(encoding="utf-8")
		module = ast.parse(text)
		function = next(
			node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "get_feedback"
		)
		source = ast.get_source_segment(text, function) or ""
		self.assertIn('frappe.has_permission("Interview", "read", interview, throw=True)', source)

	def test_every_candidate_review_mutation_is_post_only(self):
		tree = ast.parse(REVIEW.read_text(encoding="utf-8"))
		mutations = [
			node
			for node in tree.body
			if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
			and node.name.startswith(MUTATING_PREFIXES)
		]
		self.assertTrue(mutations)
		self.assertEqual(
			[node.name for node in mutations if not _decorator_is_post_only(node)],
			[],
		)

	def test_cancelled_run_is_terminal_for_chunk_processing(self):
		text = REVIEW.read_text(encoding="utf-8")
		module = ast.parse(text)
		function = next(
			node
			for node in module.body
			if isinstance(node, ast.FunctionDef) and node.name == "process_filtered_run_chunk"
		)
		source = ast.get_source_segment(text, function) or ""
		self.assertIn('run_doc.run_status not in {"Frozen", "In Progress"}', source)
		self.assertLess(
			source.index('run_doc.run_status not in {"Frozen", "In Progress"}'),
			source.index("_pending_run_members_for_update"),
		)

	def test_talent_pool_update_serializes_with_identity_and_reloads_applicant(self):
		text = REVIEW.read_text(encoding="utf-8")
		module = ast.parse(text)
		function = next(
			node
			for node in module.body
			if isinstance(node, ast.FunctionDef) and node.name == "update_candidate_profile"
		)
		source = ast.get_source_segment(text, function) or ""
		self.assertIn("acquire_candidate_identity_lock()", source)
		self.assertIn("SELECT name FROM `tabJob Applicant`", source)
		reload_call = 'frappe.get_doc("Job Applicant", applicant, for_update=True)'
		self.assertGreater(source.count(reload_call), 0)
		self.assertLess(source.index("acquire_candidate_identity_lock()"), source.rindex(reload_call))

	def test_child_member_worklist_uses_parent_authorized_sql(self):
		text = REVIEW.read_text(encoding="utf-8")
		module = ast.parse(text)
		function = next(
			node
			for node in module.body
			if isinstance(node, ast.FunctionDef) and node.name == "get_filtered_run_members"
		)
		source = ast.get_source_segment(text, function) or ""
		self.assertIn("frappe.db.sql", source)
		self.assertNotIn("frappe.get_list", source)

	def test_scorecard_reloads_applicant_after_row_lock(self):
		text = REVIEW.read_text(encoding="utf-8")
		module = ast.parse(text)
		function = next(
			node
			for node in module.body
			if isinstance(node, ast.FunctionDef) and node.name == "save_scorecard"
		)
		source = ast.get_source_segment(text, function) or ""
		lock_at = source.index("SELECT name FROM `tabJob Applicant`")
		reload_at = source.rindex('frappe.get_doc("Job Applicant", applicant, for_update=True)')
		self.assertLess(lock_at, reload_at)

	def test_interview_mutations_are_post_only_and_feedback_is_object_authorized(self):
		controller = (ROOT / "hrms" / "hr" / "doctype" / "interview" / "interview.py").read_text(
			encoding="utf-8"
		)
		for function_name in ("update_job_applicant_status", "create_interview_feedback"):
			marker = f"def {function_name}"
			prefix = controller[: controller.index(marker)].rstrip().splitlines()[-1]
			self.assertEqual(prefix, '@frappe.whitelist(methods=["POST"])')
		self.assertIn('frappe.has_permission("Interview", "read", interview, throw=True)', controller)

	def test_fresh_site_patch_creates_governance_column_before_profile_backfill(self):
		patch = (ROOT / "hrms" / "patches" / "v16_0" / "create_ayp_candidate_profiles.py").read_text(
			encoding="utf-8"
		)
		self.assertIn('"fieldname": "custom_ayp_governed"', patch)
		self.assertLess(
			patch.index('"fieldname": "custom_ayp_governed"'), patch.index("backfill_candidate_profiles()")
		)

	def test_frozen_cohort_and_durable_merge_have_schema_support(self):
		run = json.loads(
			(
				ROOT
				/ "hrms"
				/ "hr"
				/ "doctype"
				/ "ayp_candidate_review_run"
				/ "ayp_candidate_review_run.json"
			).read_text()
		)
		member = json.loads(
			(
				ROOT
				/ "hrms"
				/ "hr"
				/ "doctype"
				/ "ayp_candidate_review_member"
				/ "ayp_candidate_review_member.json"
			).read_text()
		)
		profile = json.loads(
			(
				ROOT / "hrms" / "hr" / "doctype" / "ayp_candidate_profile" / "ayp_candidate_profile.json"
			).read_text()
		)
		self.assertTrue(
			any(
				field.get("fieldname") == "members" and field.get("fieldtype") == "Table"
				for field in run["fields"]
			)
		)
		self.assertEqual(member.get("istable"), 1)
		self.assertEqual(member.get("permissions"), [])
		self.assertIn("merged_into", {field["fieldname"] for field in profile["fields"]})
		status = next(field for field in run["fields"] if field["fieldname"] == "run_status")
		self.assertIn("Cancelled", status["options"])

	def test_new_applicant_without_persisted_profile_skips_redirect_resolution(self):
		talent_pool = (ROOT / "hrms" / "recruitment" / "talent_pool.py").read_text(encoding="utf-8")
		tree = ast.parse(talent_pool)
		function = next(
			node
			for node in tree.body
			if isinstance(node, ast.FunctionDef) and node.name == "_profile_name_for_update"
		)
		self.assertTrue(
			any(
				isinstance(node, ast.If)
				and isinstance(node.test, ast.UnaryOp)
				and isinstance(node.test.op, ast.Not)
				for node in function.body
			),
			"Los documentos nuevos sin perfil persistido no deben entrar al resolver de redirects.",
		)

	def test_finalized_interview_and_feedback_cancellation_have_guards(self):
		hooks = (ROOT / "hrms" / "hooks.py").read_text(encoding="utf-8")
		self.assertIn("validate_ayp_interview_cancellation", hooks)
		self.assertIn("validate_ayp_feedback_cancellation", hooks)

	def test_application_received_notification_excludes_email_intake(self):
		notification = json.loads(
			(
				ROOT
				/ "hrms"
				/ "hr"
				/ "notification"
				/ "ayp_candidate_application_received"
				/ "ayp_candidate_application_received.json"
			).read_text(encoding="utf-8")
		)
		self.assertIn("doc.source != 'Email Recursos Humanos'", notification["condition"])


if __name__ == "__main__":
	unittest.main()
