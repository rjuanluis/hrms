#!/usr/bin/env python3
"""Fail-closed Microsoft 365 -> AyP HRMS recruitment bridge.

The Graph application is read-only. Incoming messages remain in the shared
mailbox; this runner only creates an HRMS Job Applicant through a transaction
inside the already-deployed backend. It never sends, replies, marks read, moves,
or deletes mail.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import importlib.util
import inspect
import json
import os
import re
import stat
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

MAILBOX = "empleos@aroypedal.com"
DEFAULT_LIMIT = 10
MAX_LIMIT = 20
MAX_MESSAGE_PAGES = 10
MAX_CV_BYTES = 5 * 1024 * 1024
MAX_GRAPH_CONTENT_CHARS = ((MAX_CV_BYTES + 2) // 3) * 4 + 16
ALLOWED_EXTENSIONS = frozenset({".pdf", ".docx", ".heic", ".heif", ".jpeg", ".jpg", ".png"})
CONSENT_NOTICE_VERSION = "AYP-RH-EMAIL-CURRENT-VACANCY-2026-08-15-v1"
CONSENT_EVIDENCE_FORMAT = "AYP-EMAIL-CONSENT-EVIDENCE-V1"
CONSENT_PHRASE = (
	"He leído el aviso de privacidad de Aro y Pedal y autorizo el tratamiento "
	"de mis datos exclusivamente para esta vacante."
)
STATE_PATH = Path.home() / ".hermes" / "state" / "ayp-ats-email-bridge.json"
LOCK_PATH = Path.home() / ".hermes" / "state" / "ayp-ats-email-bridge.lock"
GRAPH_CLIENT_PATH = Path.home() / ".hermes" / "scripts" / "msgraph_app_cli.py"
SSH_HOST = "hostinger-vps"
_REMOTE_BOOTSTRAP = """import json,os,sys,frappe
SITES="/home/frappe/frappe-bench/sites"
os.chdir(SITES)
frappe.init(site="hr.aroypedal.com", sites_path=SITES)
frappe.connect()
frappe.set_user("Administrator")
try:
    from hrms.recruitment.email_bridge import ingest_email_payload
    result=ingest_email_payload(json.load(sys.stdin))
    frappe.db.commit()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
except Exception:
    frappe.db.rollback()
    raise
finally:
    frappe.destroy()
"""
_REMOTE_BOOTSTRAP_B64 = base64.b64encode(_REMOTE_BOOTSTRAP.encode("utf-8")).decode("ascii")
REMOTE_COMMAND = f"""set -euo pipefail
backend=$(docker ps --filter "label=com.docker.compose.project=web_ayp-hrms" --filter "label=com.docker.compose.service=backend" --format "{{{{.ID}}}}" | head -1)
test -n "$backend"
docker exec -i "$backend" bash -lc 'cd /home/frappe/frappe-bench && ./env/bin/python -c "import base64;exec(base64.b64decode(\\"{_REMOTE_BOOTSTRAP_B64}\\"))"'
"""


class BridgeError(RuntimeError):
	"""Sanitized operational failure safe for cron delivery."""


@dataclass(frozen=True)
class CandidateMessage:
	key: str
	graph_id: str
	payload: dict[str, Any]


SIMPLE_CONSENT_HTML_TAGS = frozenset({"html", "body", "div", "p", "span", "br"})
SIMPLE_CONSENT_HTML_VOID_TAGS = frozenset({"br"})


class _HTMLTextExtractor(HTMLParser):
	def __init__(self) -> None:
		super().__init__(convert_charrefs=True)
		self.parts: list[str] = []
		self.stack: list[str] = []
		self.valid = True

	def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		tag = tag.casefold()
		if tag not in SIMPLE_CONSENT_HTML_TAGS or attrs:
			self.valid = False
			return
		if tag not in SIMPLE_CONSENT_HTML_VOID_TAGS:
			self.stack.append(tag)

	def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		if tag.casefold() not in SIMPLE_CONSENT_HTML_VOID_TAGS or attrs:
			self.valid = False

	def handle_endtag(self, tag: str) -> None:
		tag = tag.casefold()
		if tag in SIMPLE_CONSENT_HTML_VOID_TAGS:
			return
		if tag not in SIMPLE_CONSENT_HTML_TAGS or not self.stack or self.stack.pop() != tag:
			self.valid = False

	def handle_data(self, data: str) -> None:
		self.parts.append(data)

	def handle_comment(self, data: str) -> None:
		self.valid = False

	def handle_decl(self, decl: str) -> None:
		self.valid = False

	def unknown_decl(self, data: str) -> None:
		self.valid = False

	def handle_pi(self, data: str) -> None:
		self.valid = False


def _normalized_words(value: str) -> str:
	decomposed = unicodedata.normalize("NFKD", value).casefold()
	without_marks = "".join(character for character in decomposed if not unicodedata.combining(character))
	return " ".join(re.findall(r"[a-z0-9]+", without_marks))


NORMALIZED_CONSENT_PHRASE = _normalized_words(CONSENT_PHRASE)
JOB_OPENING = "HR-OPN-2026-0001"
JOB_OPENING_SUBJECT_PATTERN = re.compile(
	rf"(?<![A-Z0-9-]){re.escape(JOB_OPENING)}(?![A-Z0-9-])",
	re.IGNORECASE,
)


def _fingerprint(value: str) -> str:
	return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _consent_evidence_sha256(*, graph_id: str, received_on: str) -> str:
	evidence = {
		"canonical_consent": NORMALIZED_CONSENT_PHRASE,
		"format": CONSENT_EVIDENCE_FORMAT,
		"graph_message_id": graph_id,
		"mailbox": MAILBOX,
		"notice_version": CONSENT_NOTICE_VERSION,
		"received_on": received_on,
	}
	canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
	return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_graph_client():
	if not GRAPH_CLIENT_PATH.is_file():
		raise BridgeError("graph_client_missing")
	spec = importlib.util.spec_from_file_location("ayp_msgraph_app_cli", GRAPH_CLIENT_PATH)
	if spec is None or spec.loader is None:
		raise BridgeError("graph_client_unloadable")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	if "immutable_message_ids" not in inspect.signature(module.request_graph).parameters:
		raise BridgeError("graph_client_missing_immutable_id_support")
	return module


def _graph_get(request_graph: Callable[..., Any], url: str) -> dict[str, Any]:
	parsed = urlparse(url)
	expected_prefix = f"/v1.0/users/{MAILBOX}/"
	decoded_path = unquote(parsed.path)
	path_segments = decoded_path.split("/")
	if (
		parsed.scheme != "https"
		or parsed.netloc.casefold() != "graph.microsoft.com"
		or not decoded_path.casefold().startswith(expected_prefix.casefold())
		or any(segment in {".", ".."} for segment in path_segments)
		or "\\" in decoded_path
	):
		raise BridgeError("graph_target_outside_ats_mailbox")
	value = request_graph(
		role="reader",
		method="GET",
		url=url,
		body=None,
		approval_ref="",
		immutable_message_ids=True,
	)
	if not isinstance(value, dict):
		raise BridgeError("graph_response_invalid")
	return value


def _load_state(path: Path = STATE_PATH) -> dict[str, Any]:
	if not path.exists():
		return {"version": 1, "messages": {}}
	try:
		mode = stat.S_IMODE(path.stat().st_mode)
		if mode & 0o077:
			raise BridgeError("state_permissions_unsafe")
		value = json.loads(path.read_text(encoding="utf-8"))
	except BridgeError:
		raise
	except Exception as exc:
		raise BridgeError("state_invalid") from exc
	if value.get("version") != 1 or not isinstance(value.get("messages"), dict):
		raise BridgeError("state_schema_invalid")
	return value


def _save_state(value: dict[str, Any], path: Path = STATE_PATH) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
	try:
		os.fchmod(fd, 0o600)
		with os.fdopen(fd, "w", encoding="utf-8") as handle:
			json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
			handle.write("\n")
			handle.flush()
			os.fsync(handle.fileno())
		os.replace(tmp_name, path)
		os.chmod(path, 0o600)
	finally:
		if os.path.exists(tmp_name):
			os.unlink(tmp_name)


@contextmanager
def _exclusive_lock(path: Path = LOCK_PATH):
	path.parent.mkdir(parents=True, exist_ok=True)
	fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
	os.fchmod(fd, 0o600)
	try:
		try:
			fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except BlockingIOError as exc:
			raise BridgeError("bridge_already_running") from exc
		yield
	finally:
		try:
			fcntl.flock(fd, fcntl.LOCK_UN)
		finally:
			os.close(fd)


def _message_key(message: dict[str, Any]) -> str:
	graph_id = str(message.get("id") or "").strip()
	if not graph_id or len(graph_id) > 4096:
		raise BridgeError("message_identity_invalid")
	return f"{MAILBOX}\n{graph_id}"


def _candidate_extension(filename: str) -> str:
	return Path(filename or "").suffix.casefold()


def _select_candidate_attachment(rows: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
	non_inline = [row for row in rows if not bool(row.get("isInline"))]
	if len(non_inline) != 1:
		return None, "blocked_multiple_or_ambiguous_attachments"
	row = non_inline[0]
	if row.get("@odata.type") != "#microsoft.graph.fileAttachment":
		return None, "blocked_attachment_type"
	filename = str(row.get("name") or "")
	if len(filename) > 255:
		return None, "blocked_candidate_filename"
	if _candidate_extension(filename) not in ALLOWED_EXTENSIONS:
		return None, "ignored_no_candidate_cv"
	declared_size = row.get("size")
	if not isinstance(declared_size, int) or isinstance(declared_size, bool):
		return None, "blocked_candidate_attachment_size"
	content = str(row.get("contentBytes") or "")
	if (
		declared_size <= 0
		or declared_size > MAX_CV_BYTES
		or not content
		or len(content) > MAX_GRAPH_CONTENT_CHARS
	):
		return None, "blocked_candidate_cv_size"
	return row, "candidate"


def _sender(message: dict[str, Any]) -> tuple[str, str]:
	address = (message.get("sender") or {}).get("emailAddress") or {}
	email = str(address.get("address") or "").strip().casefold()
	name = str(address.get("name") or "").strip()
	if not email or len(email) > 140 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
		raise BridgeError("sender_invalid")
	if not name:
		name = email.split("@", 1)[0]
	return email, name[:140]


def _fetch_messages(
	request_graph: Callable[..., Any],
	limit: int,
	known_fingerprints: set[str] | None = None,
) -> list[dict[str, Any]]:
	mailbox = quote(MAILBOX, safe="@")
	select = "id,internetMessageId,subject,receivedDateTime,sender,hasAttachments"
	url = (
		f"https://graph.microsoft.com/v1.0/users/{mailbox}/mailFolders/inbox/messages"
		"?$select="
		f"{select}&$filter=receivedDateTime%20ge%202000-01-01T00:00:00Z%20and%20hasAttachments%20eq%20true"
		f"&$orderby=receivedDateTime%20desc&$top={limit}"
	)
	known = known_fingerprints or set()
	pending: list[dict[str, Any]] = []
	for _ in range(MAX_MESSAGE_PAGES):
		response = _graph_get(request_graph, url)
		value = response.get("value")
		if not isinstance(value, list):
			raise BridgeError("graph_message_list_invalid")
		for row in value:
			if not isinstance(row, dict) or not row.get("hasAttachments"):
				continue
			try:
				fingerprint = _fingerprint(_message_key(row))
			except BridgeError:
				pending.append(row)
			else:
				if fingerprint not in known:
					pending.append(row)
			if len(pending) >= limit:
				return pending
		next_link = response.get("@odata.nextLink")
		if not next_link:
			return pending
		if not isinstance(next_link, str) or not next_link.startswith("https://graph.microsoft.com/v1.0/"):
			raise BridgeError("graph_message_next_link_invalid")
		url = next_link
	raise BridgeError("graph_message_backlog_exceeds_scan_limit")


def _has_authoritative_vacancy(message: dict[str, Any]) -> bool:
	return bool(JOB_OPENING_SUBJECT_PATTERN.search(str(message.get("subject") or "")[:140]))


def _fetch_attachments(request_graph: Callable[..., Any], graph_id: str) -> list[dict[str, Any]]:
	mailbox = quote(MAILBOX, safe="@")
	message_id = quote(graph_id, safe="")
	# `attachments` is a heterogeneous base collection. Exchange rejects a
	# collection-level `$select=contentBytes` because that property belongs only
	# to fileAttachment. The unfiltered endpoint returns the derived resource,
	# including contentBytes, as documented by Microsoft Graph.
	url = f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{message_id}/attachments"
	response = _graph_get(request_graph, url)
	if response.get("@odata.nextLink"):
		raise BridgeError("graph_attachment_list_paginated")
	value = response.get("value")
	if not isinstance(value, list):
		raise BridgeError("graph_attachment_list_invalid")
	return [row for row in value if isinstance(row, dict)]


def _has_current_vacancy_consent(request_graph: Callable[..., Any], graph_id: str) -> bool:
	mailbox = quote(MAILBOX, safe="@")
	message_id = quote(graph_id, safe="")
	body = _graph_get(
		request_graph,
		f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{message_id}?$select=body",
	).get("body")
	if not isinstance(body, dict):
		raise BridgeError("message_body_invalid")
	content = body.get("content")
	if not isinstance(content, str) or len(content) > 1024 * 1024:
		raise BridgeError("message_body_invalid")
	content_type = str(body.get("contentType") or "").casefold()
	if content_type == "html":
		parser = _HTMLTextExtractor()
		try:
			parser.feed(content)
			parser.close()
		except Exception as exc:
			raise BridgeError("message_body_invalid") from exc
		if not parser.valid or parser.stack:
			return False
		content = " ".join(parser.parts)
	elif content_type != "text":
		return False
	return _normalized_words(content) == NORMALIZED_CONSENT_PHRASE


def _build_candidate(message: dict[str, Any], attachment: dict[str, Any]) -> CandidateMessage:
	key = _message_key(message)
	email, name = _sender(message)
	received = str(message.get("receivedDateTime") or "").strip()
	if not received or len(received) > 64:
		raise BridgeError("received_datetime_invalid")
	subject = str(message.get("subject") or "").strip()[:140]
	content = str(attachment.get("contentBytes") or "")
	payload = {
		"graph_message_id": str(message["id"]),
		"received_on": received,
		"sender_email": email,
		"sender_name": name,
		"subject": subject,
		"consent_current_vacancy": True,
		"consent_notice_version": CONSENT_NOTICE_VERSION,
		"consent_evidence_sha256": _consent_evidence_sha256(
			graph_id=str(message["id"]),
			received_on=received,
		),
		"attachments": [
			{
				"name": str(attachment.get("name") or "")[:255],
				"content_type": str(attachment.get("contentType") or "")[:140],
				"size": int(attachment.get("size") or 0),
				"content_base64": content,
			}
		],
	}
	return CandidateMessage(key=key, graph_id=str(message["id"]), payload=payload)


def _remote_ingest(payload: dict[str, Any]) -> dict[str, Any]:
	serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
	proc = subprocess.run(
		["ssh", "-o", "BatchMode=yes", SSH_HOST, REMOTE_COMMAND],
		input=serialized,
		text=True,
		capture_output=True,
		timeout=180,
		check=False,
	)
	if proc.returncode != 0:
		diagnostic = hashlib.sha256((proc.stderr or "").encode("utf-8", errors="replace")).hexdigest()[:12]
		raise BridgeError(f"remote_ingest_failed:{diagnostic}")
	lines = [line.strip() for line in proc.stdout.splitlines() if line.strip().startswith("{")]
	if not lines:
		raise BridgeError("remote_result_missing")
	try:
		value = json.loads(lines[-1])
	except json.JSONDecodeError as exc:
		raise BridgeError("remote_result_invalid") from exc
	if not isinstance(value, dict) or value.get("status") not in {"created", "already_processed"}:
		raise BridgeError("remote_result_unexpected")
	return value


def run(*, dry_run: bool, limit: int, report_json: bool, state_path: Path = STATE_PATH) -> dict[str, Any]:
	graph = _load_graph_client()
	state = _load_state(state_path)
	summary: dict[str, Any] = {
		"checked": 0,
		"would_create": 0,
		"created": 0,
		"already_processed": 0,
		"ignored": 0,
		"blocked": 0,
		"errors": [],
	}
	dirty = False
	messages = _fetch_messages(graph.request_graph, limit, set(state["messages"]))
	for message in messages:
		summary["checked"] += 1
		try:
			key = _message_key(message)
			fingerprint = _fingerprint(key)
			if fingerprint in state["messages"]:
				summary["ignored"] += 1
				continue
			if not _has_authoritative_vacancy(message):
				outcome = "blocked_missing_authoritative_vacancy"
				summary["blocked"] += 1
				summary["errors"].append({"message": fingerprint, "code": outcome})
				if not dry_run:
					state["messages"][fingerprint] = outcome
					dirty = True
				continue
			attachments = _fetch_attachments(graph.request_graph, str(message["id"]))
			selected, outcome = _select_candidate_attachment(attachments)
			if selected is None:
				if outcome.startswith("ignored_"):
					summary["ignored"] += 1
				else:
					summary["blocked"] += 1
					summary["errors"].append({"message": fingerprint, "code": outcome})
				if not dry_run:
					state["messages"][fingerprint] = outcome
					dirty = True
				continue
			if not _has_current_vacancy_consent(graph.request_graph, str(message["id"])):
				outcome = "blocked_missing_current_vacancy_consent"
				summary["blocked"] += 1
				summary["errors"].append({"message": fingerprint, "code": outcome})
				if not dry_run:
					state["messages"][fingerprint] = outcome
					dirty = True
				continue
			candidate = _build_candidate(message, selected)
			if dry_run:
				summary["would_create"] += 1
				continue
			result = _remote_ingest(candidate.payload)
			result_status = str(result["status"])
			summary[result_status] += 1
			state["messages"][fingerprint] = result_status
			dirty = True
		except BridgeError as exc:
			fingerprint = _fingerprint(str(message.get("id") or "unknown"))
			summary["blocked"] += 1
			summary["errors"].append({"message": fingerprint, "code": str(exc)[:80]})
	if dirty:
		_save_state(state, state_path)
	if report_json:
		print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
	elif summary["blocked"] or summary["errors"]:
		print(
			json.dumps(
				{"ats_email_bridge": "attention", "blocked": summary["blocked"], "errors": summary["errors"]},
				ensure_ascii=False,
				sort_keys=True,
			)
		)
	return summary


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--dry-run", action="store_true")
	parser.add_argument("--report-json", action="store_true")
	parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
	parser.add_argument("--state", type=Path, default=STATE_PATH)
	args = parser.parse_args()
	if not 1 <= args.limit <= MAX_LIMIT:
		parser.error(f"--limit must be between 1 and {MAX_LIMIT}")
	try:
		with _exclusive_lock():
			summary = run(
				dry_run=args.dry_run, limit=args.limit, report_json=args.report_json, state_path=args.state
			)
	except BridgeError as exc:
		print(json.dumps({"ats_email_bridge": "error", "code": str(exc)[:100]}, sort_keys=True))
		return 2
	return 1 if summary["blocked"] else 0


if __name__ == "__main__":
	raise SystemExit(main())
