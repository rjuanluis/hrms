from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("ayp_ats_email_bridge.py")
spec = importlib.util.spec_from_file_location("ayp_ats_email_bridge", SCRIPT)
assert spec and spec.loader
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


class GraphModule:
	def __init__(self, message, attachments, body=None):
		self.message = message
		self.attachments = attachments
		self.body = body or {
			"contentType": "html",
			"content": f"<p>{bridge.CONSENT_PHRASE}</p>",
		}
		self.calls = []

	def request_graph(self, *, role, method, url, body, approval_ref, immutable_message_ids=False):
		if immutable_message_ids is not True:
			raise AssertionError("ATS Graph reads must request immutable message IDs")
		self.calls.append((role, method, url, body, approval_ref, immutable_message_ids))
		base_url = url.split("?", 1)[0]
		if base_url.endswith("/attachments"):
			return {
				"value": [
					{key: value for key, value in row.items() if key != "contentBytes"}
					for row in self.attachments
				]
			}
		if "/attachments/" in base_url:
			attachment_id = base_url.rsplit("/", 1)[-1]
			return next(row for row in self.attachments if row.get("id") == attachment_id)
		if "?$select=body" in url:
			return {"body": self.body}
		return {"value": [self.message]}


class TestAyPEmailBridge(unittest.TestCase):
	def message(self):
		return {
			"id": "GRAPH-ID-1",
			"internetMessageId": "<synthetic-1@example.test>",
			"subject": "Solicitud sintética HR-OPN-2026-0001",
			"receivedDateTime": "2026-08-15T12:00:00Z",
			"sender": {"emailAddress": {"address": "candidate@example.test", "name": "Candidata Sintética"}},
			"from": {"emailAddress": {"address": "candidate@example.test", "name": "Candidata Sintética"}},
			"hasAttachments": True,
		}

	def attachment(self, name="cv.pdf"):
		return {
			"@odata.type": "#microsoft.graph.fileAttachment",
			"id": "ATT-1",
			"name": name,
			"contentType": "application/pdf",
			"size": 12,
			"isInline": False,
			"contentBytes": "JVBERi0xLjQKYWJj",
		}

	def test_selects_one_candidate_and_ignores_inline_logo(self):
		logo = {**self.attachment("logo.png"), "isInline": True}
		selected, status = bridge._select_candidate_attachment([logo, self.attachment()])
		self.assertEqual(status, "candidate")
		self.assertEqual(selected["name"], "cv.pdf")

	def test_blocks_multiple_candidate_documents(self):
		selected, status = bridge._select_candidate_attachment(
			[self.attachment(), self.attachment("other.docx")]
		)
		self.assertIsNone(selected)
		self.assertEqual(status, "blocked_multiple_or_ambiguous_attachments")

	def test_blocks_cv_plus_unrelated_non_inline_attachment(self):
		unrelated = {**self.attachment("notes.txt"), "contentType": "text/plain"}
		selected, status = bridge._select_candidate_attachment([self.attachment(), unrelated])
		self.assertIsNone(selected)
		self.assertEqual(status, "blocked_multiple_or_ambiguous_attachments")

	def test_blocks_missing_or_wrong_graph_attachment_type(self):
		for odata_type in (None, "#microsoft.graph.itemAttachment", "fileAttachment"):
			attachment = self.attachment()
			if odata_type is None:
				attachment.pop("@odata.type")
			else:
				attachment["@odata.type"] = odata_type
			with self.subTest(odata_type=odata_type):
				selected, status = bridge._select_candidate_attachment([attachment])
				self.assertIsNone(selected)
				self.assertEqual(status, "blocked_attachment_type")

	def test_rejects_filename_over_255_without_truncating_away_extension(self):
		selected, status = bridge._select_candidate_attachment([self.attachment(f"{'a' * 252}.pdf")])
		self.assertIsNone(selected)
		self.assertEqual(status, "blocked_candidate_filename")

	def test_dry_run_has_no_remote_write_or_state(self):
		graph = GraphModule(self.message(), [self.attachment()])
		with (
			tempfile.TemporaryDirectory() as tmp,
			patch.object(bridge, "_load_graph_client", return_value=graph),
			patch.object(bridge, "_remote_ingest") as remote,
		):
			path = Path(tmp) / "state.json"
			output = io.StringIO()
			with contextlib.redirect_stdout(output):
				result = bridge.run(dry_run=True, limit=10, report_json=True, state_path=path)
			self.assertEqual(result["created"], 0)
			self.assertEqual(result["would_create"], 1)
			self.assertFalse(path.exists())
			remote.assert_not_called()
			report = json.loads(output.getvalue())
			self.assertEqual(report["created"], 0)
			self.assertEqual(report["would_create"], 1)
			message_list_url = graph.calls[0][2]
			self.assertIn("$orderby=receivedDateTime%20desc", message_list_url)
			self.assertIn("hasAttachments%20eq%20true", message_list_url)
			self.assertIn("receivedDateTime%20ge%202026-08-13T00:00:00Z", message_list_url)
			self.assertIn("$top=100", message_list_url)
			attachment_list_urls = [
				call[2] for call in graph.calls if call[2].split("?", 1)[0].endswith("/attachments")
			]
			self.assertEqual(len(attachment_list_urls), 1)
			self.assertIn("$select=id,name,contentType,size,isInline", attachment_list_urls[0])
			attachment_content_urls = [call[2] for call in graph.calls if "/attachments/ATT-1" in call[2]]
			self.assertEqual(len(attachment_content_urls), 1)

	def test_success_is_silent_and_state_is_private_and_dedupes(self):
		graph = GraphModule(self.message(), [self.attachment()])
		with (
			tempfile.TemporaryDirectory() as tmp,
			patch.object(bridge, "_load_graph_client", return_value=graph),
			patch.object(
				bridge,
				"_remote_ingest",
				return_value={"status": "created", "applicant": "APP-1"},
			) as remote,
		):
			path = Path(tmp) / "state.json"
			output = io.StringIO()
			with contextlib.redirect_stdout(output):
				first = bridge.run(dry_run=False, limit=10, report_json=False, state_path=path)
				second = bridge.run(dry_run=False, limit=10, report_json=False, state_path=path)
			self.assertEqual(first["created"], 1)
			self.assertEqual(second["checked"], 0)
			self.assertEqual(second["ignored"], 0)
			self.assertEqual(remote.call_count, 1)
			payload = remote.call_args.args[0]
			self.assertIs(payload["consent_current_vacancy"], True)
			self.assertEqual(payload["consent_notice_version"], bridge.CONSENT_NOTICE_VERSION)
			self.assertRegex(payload["consent_evidence_sha256"], r"^[0-9a-f]{64}$")
			self.assertNotIn("body", payload)
			self.assertNotIn(bridge.CONSENT_PHRASE, json.dumps(payload, ensure_ascii=False))
			self.assertEqual(output.getvalue(), "")
			self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
			persisted = json.loads(path.read_text())
			self.assertNotIn("candidate@example.test", json.dumps(persisted))
			self.assertNotIn("synthetic-1", json.dumps(persisted))

	def test_missing_current_vacancy_consent_blocks_without_remote_write(self):
		graph = GraphModule(
			self.message(),
			[self.attachment()],
			body={"contentType": "text", "content": "Adjunto mi currículum."},
		)
		with (
			tempfile.TemporaryDirectory() as tmp,
			patch.object(bridge, "_load_graph_client", return_value=graph),
			patch.object(bridge, "_remote_ingest") as remote,
			contextlib.redirect_stdout(io.StringIO()),
		):
			result = bridge.run(
				dry_run=True,
				limit=10,
				report_json=False,
				state_path=Path(tmp) / "state.json",
			)
		self.assertEqual(result["created"], 0)
		self.assertEqual(result["blocked"], 1)
		self.assertEqual(result["errors"][0]["code"], "blocked_missing_current_vacancy_consent")
		self.assertFalse(any("/attachments/ATT-1" in call[2] for call in graph.calls))
		remote.assert_not_called()

	def test_admission_block_exits_zero_and_reports_only_aggregate_reason(self):
		graph = GraphModule(
			self.message(),
			[self.attachment()],
			body={"contentType": "text", "content": "Adjunto mi currículum."},
		)
		with (
			tempfile.TemporaryDirectory() as tmp,
			patch.object(bridge, "_load_graph_client", return_value=graph),
			patch.object(bridge, "_exclusive_lock", return_value=contextlib.nullcontext()),
			patch.object(sys, "argv", [str(SCRIPT), "--state", str(Path(tmp) / "state.json")]),
			contextlib.redirect_stdout(io.StringIO()) as output,
		):
			return_code = bridge.main()
		self.assertEqual(return_code, 0)
		report = json.loads(output.getvalue())
		self.assertEqual(report["ats_email_bridge"], "attention")
		self.assertEqual(report["blocked"], 1)
		self.assertEqual(report["faults"], 0)
		self.assertEqual(report["reasons"], {"blocked_missing_current_vacancy_consent": 1})
		self.assertNotIn("message", report)

	def test_infrastructure_error_still_exits_nonzero(self):
		with (
			patch.object(bridge, "_exclusive_lock", side_effect=bridge.BridgeError("graph_unavailable")),
			patch.object(sys, "argv", [str(SCRIPT)]),
			contextlib.redirect_stdout(io.StringIO()) as output,
		):
			return_code = bridge.main()
		self.assertEqual(return_code, 2)
		self.assertEqual(json.loads(output.getvalue())["ats_email_bridge"], "error")

	def test_per_message_remote_failure_still_exits_nonzero(self):
		graph = GraphModule(self.message(), [self.attachment()])
		with (
			tempfile.TemporaryDirectory() as tmp,
			patch.object(bridge, "_load_graph_client", return_value=graph),
			patch.object(
				bridge, "_remote_ingest", side_effect=bridge.BridgeError("remote_ingest_failed:test")
			),
			patch.object(bridge, "_exclusive_lock", return_value=contextlib.nullcontext()),
			patch.object(sys, "argv", [str(SCRIPT), "--state", str(Path(tmp) / "state.json")]),
			contextlib.redirect_stdout(io.StringIO()) as output,
		):
			return_code = bridge.main()
		self.assertEqual(return_code, 2)
		report = json.loads(output.getvalue())
		self.assertEqual(report["ats_email_bridge"], "error")
		self.assertEqual(report["faults"], 1)
		self.assertEqual(report["reasons"], {"remote_ingest_failed:test": 1})

	def test_remote_admission_block_is_attention_without_state_commit(self):
		graph = GraphModule(self.message(), [self.attachment()])
		with (
			tempfile.TemporaryDirectory() as tmp,
			patch.object(bridge, "_load_graph_client", return_value=graph),
			patch.object(
				bridge,
				"_remote_ingest",
				return_value={"status": "blocked", "code": "blocked_single_open_vacancy_required"},
			),
			contextlib.redirect_stdout(io.StringIO()) as output,
		):
			state_path = Path(tmp) / "state.json"
			result = bridge.run(dry_run=False, limit=10, report_json=False, state_path=state_path)
		self.assertEqual(result["blocked"], 1)
		self.assertEqual(result["faults"], 0)
		self.assertEqual(result["errors"][0]["code"], "blocked_single_open_vacancy_required")
		self.assertFalse(state_path.exists())
		report = json.loads(output.getvalue())
		self.assertEqual(report["ats_email_bridge"], "attention")

	def test_remote_ingest_accepts_only_allowlisted_admission_codes(self):
		allowed = types.SimpleNamespace(
			returncode=0,
			stdout='{"status":"blocked","code":"blocked_single_open_vacancy_required"}\n',
			stderr="",
		)
		with patch.object(bridge.subprocess, "run", return_value=allowed):
			self.assertEqual(bridge._remote_ingest({})["status"], "blocked")

		unknown = types.SimpleNamespace(
			returncode=0,
			stdout='{"status":"blocked","code":"blocked_untrusted"}\n',
			stderr="",
		)
		with (
			patch.object(bridge.subprocess, "run", return_value=unknown),
			self.assertRaisesRegex(bridge.BridgeError, "remote_result_unexpected"),
		):
			bridge._remote_ingest({})

	def test_launcher_preserves_infrastructure_exit_and_keeps_admission_zero(self):
		launcher = SCRIPT.with_name("run_ayp_ats_email_bridge.sh")
		with tempfile.TemporaryDirectory() as tmp:
			home = Path(tmp)
			guard = home / ".hermes" / "scripts" / "decision_ledger.py"
			guard.parent.mkdir(parents=True)
			guard.write_text("import sys\nsys.stdout.write(sys.stdin.read())\n")
			guard.chmod(0o700)
			runner = home / "runner.py"
			env = {
				**os.environ,
				"HOME": str(home),
				"AYP_ATS_GRAPH_PYTHON": sys.executable,
				"AYP_ATS_BRIDGE_RUNNER": str(runner),
			}

			for runner_exit, expected_exit in ((0, 0), (2, 2)):
				report = {
					"ats_email_bridge": "attention",
					"blocked": 1,
					"faults": int(runner_exit != 0),
					"reasons": {"test": 1},
				}
				runner.write_text(
					"import json\n"
					+ f"print(json.dumps({report!r}))\n"
					+ f"raise SystemExit({runner_exit})\n"
				)
				with self.subTest(runner_exit=runner_exit):
					proc = subprocess.run(
						["bash", str(launcher)], text=True, capture_output=True, env=env, check=False
					)
					self.assertEqual(proc.returncode, expected_exit)
					self.assertIn("Aro y Pedal", proc.stdout)

			env["AYP_ATS_GRAPH_PYTHON"] = str(home / "missing-python")
			proc = subprocess.run(
				["bash", str(launcher)], text=True, capture_output=True, env=env, check=False
			)
			self.assertEqual(proc.returncode, 78)
			self.assertIn("graph_python_unavailable", proc.stdout)

			env["AYP_ATS_GRAPH_PYTHON"] = sys.executable
			env["AYP_ATS_BRIDGE_RUNNER"] = str(home / "missing-runner.py")
			proc = subprocess.run(
				["bash", str(launcher)], text=True, capture_output=True, env=env, check=False
			)
			self.assertEqual(proc.returncode, 78)
			self.assertIn("runner_unavailable", proc.stdout)

	def test_consent_rejects_hidden_or_ambiguous_html(self):
		bodies = (
			f'<span style="display:none">{bridge.CONSENT_PHRASE}</span>',
			f"<script>{bridge.CONSENT_PHRASE}</script>",
			f"<template>{bridge.CONSENT_PHRASE}</template>",
			f"<blockquote>{bridge.CONSENT_PHRASE}</blockquote>",
			f'<p class="hidden">{bridge.CONSENT_PHRASE}</p>',
			f"<!-- {bridge.CONSENT_PHRASE} -->",
		)
		for content in bodies:
			graph = GraphModule(
				self.message(),
				[self.attachment()],
				body={"contentType": "html", "content": content},
			)
			with self.subTest(content=content):
				self.assertFalse(bridge._has_current_vacancy_consent(graph.request_graph, "MSG-1"))

	def test_consent_accepts_only_exact_plain_text_or_simple_visible_html(self):
		for body in (
			{"contentType": "text", "content": bridge.CONSENT_PHRASE},
			{"contentType": "html", "content": f"<p>{bridge.CONSENT_PHRASE}</p>"},
		):
			graph = GraphModule(self.message(), [self.attachment()], body=body)
			with self.subTest(body=body):
				self.assertTrue(bridge._has_current_vacancy_consent(graph.request_graph, "MSG-1"))

	def test_negated_quoted_or_signed_consent_text_fails_closed(self):
		bodies = (
			f"No. {bridge.CONSENT_PHRASE}",
			f"---------- Forwarded message ----------\n{bridge.CONSENT_PHRASE}",
			f"{bridge.CONSENT_PHRASE}\nNo autorizo el tratamiento.",
			f"{bridge.CONSENT_PHRASE}\nFirma automática",
		)
		for body in bodies:
			with self.subTest(body=body), tempfile.TemporaryDirectory() as tmp:
				graph = GraphModule(
					self.message(),
					[self.attachment()],
					body={"contentType": "text", "content": body},
				)
				with (
					patch.object(bridge, "_load_graph_client", return_value=graph),
					patch.object(bridge, "_remote_ingest") as remote,
					contextlib.redirect_stdout(io.StringIO()),
				):
					result = bridge.run(
						dry_run=True,
						limit=10,
						report_json=False,
						state_path=Path(tmp) / "state.json",
					)
				self.assertEqual(result["would_create"], 0)
				self.assertEqual(result["blocked"], 1)
				remote.assert_not_called()

	def test_consent_rejects_unconsumed_unicode_text(self):
		for extra in (" 我不同意处理我的个人资料", " 🚫"):
			with self.subTest(extra=extra):
				graph = GraphModule(
					self.message(),
					[self.attachment()],
					body={"contentType": "text", "content": bridge.CONSENT_PHRASE + extra},
				)
				self.assertFalse(bridge._has_current_vacancy_consent(graph.request_graph, "MSG-1"))

	def test_subject_without_vacancy_code_reaches_authorized_single_vacancy_flow(self):
		message = self.message()
		message["subject"] = "Solicitud para otra vacante"
		graph = GraphModule(message, [self.attachment()])
		with (
			tempfile.TemporaryDirectory() as tmp,
			patch.object(bridge, "_load_graph_client", return_value=graph),
			patch.object(bridge, "_remote_ingest") as remote,
			contextlib.redirect_stdout(io.StringIO()),
		):
			result = bridge.run(
				dry_run=True,
				limit=10,
				report_json=False,
				state_path=Path(tmp) / "state.json",
			)
		self.assertEqual(result["blocked"], 0)
		self.assertEqual(result["would_create"], 1)
		self.assertTrue(any(call[2].split("?", 1)[0].endswith("/attachments") for call in graph.calls))
		self.assertTrue(any("/attachments/ATT-1" in call[2] for call in graph.calls))
		remote.assert_not_called()

	def test_consent_html_rejects_processing_instruction(self):
		message = self.message()
		message["body"] = {
			"contentType": "html",
			"content": "<?manufactured consent?><p>" + bridge.CONSENT_PHRASE + "</p>",
		}
		self.assertFalse(
			bridge._has_current_vacancy_consent(lambda **kwargs: {"body": message["body"]}, "GRAPH-ID")
		)

	def test_attachment_with_malformed_declared_size_fails_closed(self):
		attachment = self.attachment()
		attachment["size"] = "not-an-integer"
		selected, status = bridge._select_candidate_attachment([attachment])
		self.assertIsNone(selected)
		self.assertEqual(status, "blocked_candidate_attachment_size")

	def test_attachment_change_between_metadata_and_content_fails_closed(self):
		metadata = {key: value for key, value in self.attachment().items() if key != "contentBytes"}
		hydrated = {**self.attachment(), "name": "replaced.pdf"}
		self.assertFalse(bridge._same_attachment(metadata, hydrated))
		with self.assertRaisesRegex(bridge.BridgeError, "graph_attachment_changed_after_preflight"):
			bridge._validate_hydrated_attachment(metadata, hydrated)

	def test_incomplete_graph_hydration_is_retryable_fault_and_not_persisted(self):
		attachment = self.attachment()
		attachment.pop("contentBytes")
		graph = GraphModule(self.message(), [attachment])
		with (
			tempfile.TemporaryDirectory() as tmp,
			patch.object(bridge, "_load_graph_client", return_value=graph),
			patch.object(bridge, "_remote_ingest") as remote,
			patch.object(bridge, "_exclusive_lock", return_value=contextlib.nullcontext()),
			contextlib.redirect_stdout(io.StringIO()) as output,
		):
			state_path = Path(tmp) / "state.json"
			with patch.object(sys, "argv", [str(SCRIPT), "--state", str(state_path)]):
				return_code = bridge.main()
			self.assertEqual(return_code, 2)
			self.assertFalse(state_path.exists())
			remote.assert_not_called()
			report = json.loads(output.getvalue())
			self.assertEqual(report["faults"], 1)
			self.assertEqual(report["reasons"], {"graph_attachment_content_invalid": 1})

	def test_hydrated_attachment_requires_valid_base64_and_exact_declared_length(self):
		metadata = {key: value for key, value in self.attachment().items() if key != "contentBytes"}
		for content in ("not base64!", "JVBERi0xLjQK"):
			with (
				self.subTest(content=content),
				self.assertRaisesRegex(bridge.BridgeError, "graph_attachment_content_invalid"),
			):
				bridge._validate_hydrated_attachment(metadata, {**metadata, "contentBytes": content})

	def test_malformed_attachment_collection_member_is_graph_fault(self):
		valid = {key: value for key, value in self.attachment().items() if key != "contentBytes"}
		with self.assertRaisesRegex(bridge.BridgeError, "graph_attachment_metadata_invalid"):
			bridge._fetch_attachment_metadata(lambda **kwargs: {"value": [valid, None]}, "GRAPH-ID")

	def test_sender_from_mismatch_blocks_before_body_or_attachment_reads(self):
		message = self.message()
		message["sender"] = {
			"emailAddress": {"address": "delegate@example.test", "name": "Delegada Sintética"}
		}
		graph = GraphModule(message, [self.attachment()])
		with (
			tempfile.TemporaryDirectory() as tmp,
			patch.object(bridge, "_load_graph_client", return_value=graph),
			patch.object(bridge, "_remote_ingest") as remote,
			contextlib.redirect_stdout(io.StringIO()),
		):
			state_path = Path(tmp) / "state.json"
			result = bridge.run(dry_run=False, limit=10, report_json=False, state_path=state_path)
			persisted = json.loads(state_path.read_text())
		self.assertEqual(result["blocked"], 1)
		self.assertEqual(result["faults"], 0)
		self.assertEqual(result["errors"][0]["code"], "blocked_sender_from_mismatch")
		self.assertEqual(len(graph.calls), 1)
		self.assertIn("blocked_sender_from_mismatch", persisted["messages"].values())
		remote.assert_not_called()

	def test_attachment_size_accepts_only_exact_integer_type(self):
		for malformed in (True, False, 12.0, "12", None, float("inf"), float("nan")):
			attachment = self.attachment()
			attachment["size"] = malformed
			with self.subTest(size=malformed):
				selected, status = bridge._select_candidate_attachment([attachment])
				self.assertIsNone(selected)
				self.assertEqual(status, "blocked_candidate_attachment_size")

	def test_message_pagination_skips_known_page_and_reaches_pending_message(self):
		known = self.message()
		pending = {**self.message(), "id": "GRAPH-ID-2", "internetMessageId": "<synthetic-2@example.test>"}
		responses = iter(
			(
				{
					"value": [known],
					"@odata.nextLink": (
						"https://graph.microsoft.com/v1.0/users/empleos@aroypedal.com/"
						"mailFolders/inbox/messages?$skiptoken=safe"
					),
				},
				{"value": [pending]},
			)
		)
		calls = []

		def request_graph(**kwargs):
			calls.append(kwargs)
			return next(responses)

		batch = bridge._fetch_messages(
			request_graph,
			1,
			{bridge._fingerprint(bridge._message_key(known))},
		)
		self.assertEqual([row["id"] for row in batch.messages], ["GRAPH-ID-2"])
		self.assertIsNotNone(batch.resume_url)
		self.assertEqual(len(calls), 2)

	def test_message_pagination_persists_validated_progress_beyond_one_thousand(self):
		known = self.message()
		known_set = {bridge._fingerprint(bridge._message_key(known))}
		mailbox_path = "/v1.0/users/empleos@aroypedal.com/mailFolders/inbox/messages"
		page_calls = 0

		def first_scan(**kwargs):
			nonlocal page_calls
			page_calls += 1
			return {
				"value": [known],
				"@odata.nextLink": f"https://graph.microsoft.com{mailbox_path}?$skiptoken=page-{page_calls + 1}",
			}

		first = bridge._fetch_messages(first_scan, 1, known_set)
		self.assertEqual(first.messages, [])
		self.assertEqual(page_calls, bridge.MAX_MESSAGE_PAGES)
		self.assertIn("page-11", first.resume_url or "")

		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "state.json"
			bridge._save_state(
				{
					"version": 1,
					"messages": {next(iter(known_set)): "created"},
					"message_scan_url": first.resume_url,
				},
				path,
			)
			persisted = bridge._load_state(path)
			pending = {
				**self.message(),
				"id": "GRAPH-ID-1001",
				"internetMessageId": "<synthetic-1001@example.test>",
			}
			second = bridge._fetch_messages(
				lambda **kwargs: {"value": [pending]},
				1,
				known_set,
				start_url=persisted["message_scan_url"],
			)
		self.assertEqual([row["id"] for row in second.messages], ["GRAPH-ID-1001"])

	def test_untrusted_next_link_is_nonzero_fault_and_never_persisted(self):
		known = self.message()
		known_fingerprint = bridge._fingerprint(bridge._message_key(known))

		def request_graph(**kwargs):
			return {
				"value": [known],
				"@odata.nextLink": (
					"https://graph.microsoft.com/v1.0/users/other@example.test/"
					"mailFolders/inbox/messages?$skiptoken=poison"
				),
			}

		graph = types.SimpleNamespace(request_graph=request_graph)
		with (
			tempfile.TemporaryDirectory() as tmp,
			patch.object(bridge, "_load_graph_client", return_value=graph),
			patch.object(bridge, "_exclusive_lock", return_value=contextlib.nullcontext()),
			contextlib.redirect_stdout(io.StringIO()) as output,
		):
			state_path = Path(tmp) / "state.json"
			bridge._save_state(
				{"version": 1, "messages": {known_fingerprint: "created"}},
				state_path,
			)
			with patch.object(sys, "argv", [str(SCRIPT), "--state", str(state_path)]):
				return_code = bridge.main()
			persisted = bridge._load_state(state_path)
		self.assertEqual(return_code, 2)
		self.assertNotIn("message_scan_url", persisted)
		self.assertEqual(persisted["messages"], {known_fingerprint: "created"})
		self.assertEqual(json.loads(output.getvalue())["code"], "graph_message_next_link_invalid")

	def test_identity_uses_full_digest_of_mailbox_and_immutable_graph_id(self):
		first = self.message()
		second = {**first, "internetMessageId": "<sender-controlled-different@example.test>"}
		self.assertEqual(bridge._message_key(first), bridge._message_key(second))
		self.assertEqual(len(bridge._fingerprint(bridge._message_key(first))), 64)

	def test_graph_get_rejects_any_target_outside_exact_ats_mailbox(self):
		for url in (
			"https://graph.microsoft.com/v1.0/users/juanluis@aroypedal.com/messages",
			"https://graph.microsoft.com/v1.0/users/empleos@aroypedal.com/../juanluis@aroypedal.com/messages",
		):
			with (
				self.subTest(url=url),
				self.assertRaisesRegex(bridge.BridgeError, "graph_target_outside_ats_mailbox"),
			):
				bridge._graph_get(lambda **kwargs: {"value": []}, url)

	def test_remote_transport_keeps_payload_out_of_process_arguments(self):
		payload = {"sender_email": "private@example.test", "attachments": [{"content_base64": "SECRET-CV"}]}
		completed = types.SimpleNamespace(
			returncode=0, stdout='{"status":"created","applicant":"APP-1"}\n', stderr=""
		)
		with patch.object(bridge.subprocess, "run", return_value=completed) as run:
			result = bridge._remote_ingest(payload)
		self.assertEqual(result["status"], "created")
		args = run.call_args.args[0]
		self.assertNotIn("private@example.test", " ".join(args))
		self.assertNotIn("SECRET-CV", " ".join(args))
		self.assertIn("private@example.test", run.call_args.kwargs["input"])
		self.assertTrue(run.call_args.kwargs["capture_output"])

	def test_unsafe_existing_state_permissions_fail_closed(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "state.json"
			path.write_text('{"version":1,"messages":{}}')
			path.chmod(0o644)
			with self.assertRaisesRegex(bridge.BridgeError, "state_permissions_unsafe"):
				bridge._load_state(path)

	def test_state_rejects_continuation_url_outside_exact_mailbox_collection(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "state.json"
			path.write_text(
				json.dumps(
					{
						"version": 1,
						"messages": {},
						"message_scan_url": "https://graph.microsoft.com/v1.0/users/other@example.test/messages",
					}
				)
			)
			path.chmod(0o600)
			with self.assertRaisesRegex(bridge.BridgeError, "state_schema_invalid"):
				bridge._load_state(path)

	def test_process_lock_is_private_and_fails_closed_on_overlap(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "bridge.lock"
			with bridge._exclusive_lock(path):
				self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
				with (
					self.assertRaisesRegex(bridge.BridgeError, "bridge_already_running"),
					bridge._exclusive_lock(path),
				):
					pass


if __name__ == "__main__":
	unittest.main()
