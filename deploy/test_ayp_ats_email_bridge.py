from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import stat
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
		if "/attachments?" in url:
			return {"value": self.attachments}
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
			"contentBytes": "JVBERi0xLjQK",
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
		remote.assert_not_called()

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

	def test_missing_authoritative_vacancy_blocks_before_attachment_download(self):
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
		self.assertEqual(result["blocked"], 1)
		self.assertEqual(result["errors"][0]["code"], "blocked_missing_authoritative_vacancy")
		self.assertFalse(any("/attachments?" in call[2] for call in graph.calls))
		remote.assert_not_called()

	def test_embedded_vacancy_token_does_not_count_as_authoritative(self):
		message = self.message()
		message["subject"] = "Solicitud XHR-OPN-2026-0001-FAKE"
		self.assertFalse(bridge._has_authoritative_vacancy(message))

	def test_message_pagination_skips_known_page_and_reaches_pending_message(self):
		known = self.message()
		pending = {**self.message(), "id": "GRAPH-ID-2", "internetMessageId": "<synthetic-2@example.test>"}
		responses = iter(
			(
				{
					"value": [known],
					"@odata.nextLink": (
						"https://graph.microsoft.com/v1.0/users/empleos@aroypedal.com/messages?$skiptoken=safe"
					),
				},
				{"value": [pending]},
			)
		)
		calls = []

		def request_graph(**kwargs):
			calls.append(kwargs)
			return next(responses)

		result = bridge._fetch_messages(
			request_graph,
			1,
			{bridge._fingerprint(bridge._message_key(known))},
		)
		self.assertEqual([row["id"] for row in result], ["GRAPH-ID-2"])
		self.assertEqual(len(calls), 2)

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
