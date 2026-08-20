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
import binascii
import fcntl
import hashlib
import importlib
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
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

try:
	_vacancy_reference = importlib.import_module("ayp_ats_vacancy_reference")
except ModuleNotFoundError as exc:
	if exc.name != "ayp_ats_vacancy_reference":
		raise
	_vacancy_reference_source = (
		Path(__file__).resolve().parents[1] / "hrms" / "recruitment" / "ats_vacancy_reference.py"
	)
	_vacancy_reference_spec = importlib.util.spec_from_file_location(
		"ayp_ats_vacancy_reference", _vacancy_reference_source
	)
	if not _vacancy_reference_spec or not _vacancy_reference_spec.loader:
		raise RuntimeError("vacancy reference parser is unavailable") from exc
	_vacancy_reference = importlib.util.module_from_spec(_vacancy_reference_spec)
	_vacancy_reference_spec.loader.exec_module(_vacancy_reference)

AUTHORIZED_JOB_OPENING = _vacancy_reference.AUTHORIZED_JOB_OPENING
subject_has_only_authorized_vacancy_references = (
	_vacancy_reference.subject_has_only_authorized_vacancy_references
)

MAILBOX = "empleos@aroypedal.com"
DEFAULT_LIMIT = 10
MAX_LIMIT = 20
MAX_MESSAGE_PAGES = 10
MAX_MESSAGE_CURSOR_HISTORY = 512
STATE_VERSION = 2
MESSAGE_PAGE_SIZE = 100
INTAKE_START_UTC = "2026-08-13T00:00:00Z"
MAX_CV_BYTES = 5 * 1024 * 1024
MAX_GRAPH_ATTACHMENT_DECLARED_BYTES = MAX_CV_BYTES + (64 * 1024)
MAX_CANDIDATE_FILENAME_LENGTH = 140
MAX_CANDIDATE_FILENAME_BYTES = 240
MAX_ATTACHMENT_PAGES = 100
MAX_ATTACHMENT_ROWS = 1000
MAX_SUBJECT_CHARS = 4096
MAX_STORED_SUBJECT_CHARS = 140
MAX_GRAPH_CONTENT_CHARS = ((MAX_CV_BYTES + 2) // 3) * 4 + 16
ALLOWED_EXTENSIONS = frozenset({".pdf", ".docx", ".heic", ".heif", ".jpeg", ".jpg", ".png"})
CONSENT_NOTICE_VERSION = "AYP-RH-EMAIL-DIRECT-SUBMISSION-2026-08-19-v1"
CONSENT_EVIDENCE_FORMAT = "AYP-EMAIL-DIRECT-SUBMISSION-EVIDENCE-V1"
CONSENT_BASIS = "direct_email_submission_to_recruitment_mailbox"
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
    from hrms.recruitment.email_bridge import EmailBridgeAdmissionError,ingest_email_payload
    result=ingest_email_payload(json.load(sys.stdin))
    frappe.db.commit()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
except EmailBridgeAdmissionError as exc:
    frappe.db.rollback()
    print(json.dumps({"status":"blocked","code":exc.code}, ensure_ascii=False, sort_keys=True))
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


class AdmissionBlock(RuntimeError):
	"""Deterministic message-level rejection safe to persist."""


REMOTE_ADMISSION_CODES = frozenset(
	{
		"blocked_authorized_vacancy_configuration",
		"blocked_candidate_subject",
		"blocked_candidate_cv_security",
		"blocked_explicit_vacancy_mismatch",
		"blocked_sender_identity",
		"blocked_single_open_vacancy_required",
	}
)
REMOTE_TERMINAL_ADMISSION_CODES = frozenset(
	{
		"blocked_candidate_cv_security",
		"blocked_candidate_subject",
		"blocked_explicit_vacancy_mismatch",
		"blocked_sender_identity",
	}
)


@dataclass(frozen=True)
class CandidateMessage:
	key: str
	graph_id: str
	payload: dict[str, Any]


@dataclass(frozen=True)
class MessageBatch:
	messages: list[dict[str, Any]]
	resume_url: str | None
	cursor_history: tuple[str, ...]


JOB_OPENING = AUTHORIZED_JOB_OPENING


def _fingerprint(value: str) -> str:
	return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _consent_evidence_sha256(*, graph_id: str, received_on: str) -> str:
	evidence = {
		"basis": CONSENT_BASIS,
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


def _validate_message_scan_url(value: Any) -> str:
	if not isinstance(value, str) or not value or len(value) > 8192:
		raise BridgeError("graph_message_next_link_invalid")
	parsed = urlparse(value)
	decoded_path = unquote(parsed.path)
	expected_paths = {
		f"/v1.0/users/{MAILBOX}/mailFolders/inbox/messages".casefold(),
		f"/v1.0/users/{MAILBOX}/mailFolders('inbox')/messages".casefold(),
	}
	if (
		parsed.scheme != "https"
		or parsed.netloc.casefold() != "graph.microsoft.com"
		or decoded_path.casefold() not in expected_paths
		or "\\" in decoded_path
		or parsed.fragment
	):
		raise BridgeError("graph_message_next_link_invalid")
	return value


def _message_scan_url_digest(value: str) -> str:
	parsed = urlparse(value)
	identity = f"https://graph.microsoft.com{unquote(parsed.path).casefold()}"
	if parsed.query:
		identity = f"{identity}?{parsed.query}"
	return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _load_state(path: Path = STATE_PATH) -> dict[str, Any]:
	if not path.exists():
		return {"version": STATE_VERSION, "messages": {}}
	try:
		mode = stat.S_IMODE(path.stat().st_mode)
		if mode & 0o077:
			raise BridgeError("state_permissions_unsafe")
		value = json.loads(path.read_text(encoding="utf-8"))
	except BridgeError:
		raise
	except Exception as exc:
		raise BridgeError("state_invalid") from exc
	state_version = value.get("version")
	if state_version not in {1, STATE_VERSION} or not isinstance(value.get("messages"), dict):
		raise BridgeError("state_schema_invalid")
	resume_url = value.get("message_scan_url")
	if resume_url is not None:
		try:
			_validate_message_scan_url(resume_url)
		except BridgeError:
			raise BridgeError("state_schema_invalid")
	history = value.get("message_scan_history", [])
	if (
		not isinstance(history, list)
		or len(history) > MAX_MESSAGE_CURSOR_HISTORY
		or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in history)
		or len(set(history)) != len(history)
		or (history and resume_url is None)
	):
		raise BridgeError("state_schema_invalid")
	if state_version == 1 and (resume_url is not None or history):
		raise BridgeError("state_cursor_digest_version_legacy")
	value["version"] = STATE_VERSION
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
	graph_id = message.get("id")
	if not isinstance(graph_id, str) or not graph_id or len(graph_id) > 4096 or graph_id != graph_id.strip():
		raise BridgeError("message_identity_invalid")
	return f"{MAILBOX}\n{graph_id}"


def _candidate_extension(filename: str) -> str:
	return Path(filename or "").suffix.casefold()


def _unsafe_candidate_filename(value: Any) -> bool:
	if not isinstance(value, str):
		return False
	try:
		encoded_length = len(value.encode("utf-8"))
	except UnicodeEncodeError:
		return True
	return (
		len(value) > MAX_CANDIDATE_FILENAME_LENGTH
		or encoded_length > MAX_CANDIDATE_FILENAME_BYTES
		or value in {".", ".."}
		or "/" in value
		or "\\" in value
		or any(ord(character) < 32 or ord(character) == 127 for character in value)
	)


def _select_candidate_attachment(
	rows: list[dict[str, Any]], *, require_content: bool = True
) -> tuple[dict[str, Any] | None, str]:
	non_inline = [row for row in rows if not bool(row.get("isInline"))]
	if len(non_inline) != 1:
		return None, "blocked_multiple_or_ambiguous_attachments"
	row = non_inline[0]
	if row.get("@odata.type") != "#microsoft.graph.fileAttachment":
		return None, "blocked_attachment_type"
	filename = row["name"]
	if _unsafe_candidate_filename(filename):
		return None, "blocked_candidate_filename"
	if _candidate_extension(filename.strip()) not in ALLOWED_EXTENSIONS:
		return None, "ignored_no_candidate_cv"
	declared_size = row.get("size")
	if not isinstance(declared_size, int) or isinstance(declared_size, bool):
		return None, "blocked_candidate_attachment_size"
	if declared_size <= 0 or declared_size > MAX_GRAPH_ATTACHMENT_DECLARED_BYTES:
		return None, "blocked_candidate_cv_size"
	if require_content:
		content = row.get("contentBytes")
		if not isinstance(content, str):
			raise BridgeError("graph_attachment_content_invalid")
		if not content:
			return None, "blocked_candidate_cv_size"
		if not _has_strict_base64_syntax(content):
			raise BridgeError("graph_attachment_content_invalid")
		if len(content) > MAX_GRAPH_CONTENT_CHARS:
			return None, "blocked_candidate_cv_size"
	return row, "candidate"


def _has_strict_base64_syntax(content: str) -> bool:
	return len(content) % 4 == 0 and re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", content) is not None


def _email_identity_values(message: dict[str, Any], field: str) -> tuple[str, str | None]:
	container = message.get(field)
	if not isinstance(container, dict):
		raise BridgeError("message_identity_invalid")
	address = container.get("emailAddress")
	if not isinstance(address, dict):
		raise BridgeError("message_identity_invalid")
	email_value = address.get("address")
	name_value = address.get("name")
	if not isinstance(email_value, str) or (name_value is not None and not isinstance(name_value, str)):
		raise BridgeError("message_identity_invalid")
	return email_value, name_value


def _email_identity(message: dict[str, Any], field: str) -> tuple[str, str]:
	email_value, name_value = _email_identity_values(message, field)
	# Validate the provider's raw identity before canonicalizing it. Trimming
	# first would launder malformed addresses and could hide sender/from drift.
	email = email_value.casefold()
	name = " ".join((name_value or "").split())
	if (
		not email
		or email_value != email_value.strip()
		or len(email) > 140
		or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in email)
		or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
	):
		raise AdmissionBlock("blocked_sender_identity")
	if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in name):
		raise AdmissionBlock("blocked_sender_identity")
	return email, name


def _sender(message: dict[str, Any]) -> tuple[str, str]:
	raw_sender_email, _ = _email_identity_values(message, "sender")
	raw_from_email, _ = _email_identity_values(message, "from")
	if raw_sender_email != raw_from_email:
		raise AdmissionBlock("blocked_sender_from_mismatch")
	sender_email, sender_name = _email_identity(message, "sender")
	from_email, from_name = _email_identity(message, "from")
	if sender_email != from_email:
		raise AdmissionBlock("blocked_sender_from_mismatch")
	name = from_name or sender_name or from_email.split("@", 1)[0]
	return from_email, name[:140]


def _validate_message_header(message: dict[str, Any]) -> None:
	_message_key(message)
	if not isinstance(message.get("hasAttachments"), bool):
		raise BridgeError("graph_message_list_invalid")
	subject = message.get("subject")
	if subject is not None and not isinstance(subject, str):
		raise BridgeError("graph_message_list_invalid")
	received = message.get("receivedDateTime")
	if not isinstance(received, str) or not received or len(received) > 64:
		raise BridgeError("received_datetime_invalid")
	_email_identity_values(message, "sender")
	_email_identity_values(message, "from")


def _require_compatible_subject_vacancy(message: dict[str, Any]) -> None:
	subject = message.get("subject")
	if subject is None:
		return
	if len(subject) > MAX_SUBJECT_CHARS or any(
		ord(character) < 32 or ord(character) == 127 for character in subject
	):
		raise AdmissionBlock("blocked_candidate_subject")
	if not subject_has_only_authorized_vacancy_references(subject):
		raise AdmissionBlock("blocked_explicit_vacancy_mismatch")


def _fetch_messages(
	request_graph: Callable[..., Any],
	limit: int,
	known_fingerprints: set[str] | None = None,
	start_url: str | None = None,
	cursor_history: list[str] | None = None,
) -> MessageBatch:
	mailbox = quote(MAILBOX, safe="@")
	select = "id,internetMessageId,subject,receivedDateTime,sender,from,hasAttachments"
	initial_url = (
		f"https://graph.microsoft.com/v1.0/users/{mailbox}/mailFolders/inbox/messages"
		"?$select="
		f"{select}&$filter=receivedDateTime%20ge%20{INTAKE_START_UTC}%20and%20hasAttachments%20eq%20true"
		f"&$orderby=receivedDateTime%20desc&$top={MESSAGE_PAGE_SIZE}"
	)
	url = _validate_message_scan_url(start_url) if start_url is not None else initial_url
	known = known_fingerprints or set()
	pending: list[dict[str, Any]] = []
	seen_url_digests: set[str] = set()
	history = list(cursor_history or [])
	history_set = set(history)
	if start_url is not None and _message_scan_url_digest(url) in history_set:
		raise BridgeError("graph_message_next_link_cycle")
	for _ in range(MAX_MESSAGE_PAGES):
		url_digest = _message_scan_url_digest(url)
		if url_digest in seen_url_digests:
			raise BridgeError("graph_message_next_link_cycle")
		seen_url_digests.add(url_digest)
		page_url = url
		response = _graph_get(request_graph, url)
		value = response.get("value")
		if not isinstance(value, list):
			raise BridgeError("graph_message_list_invalid")
		for row in value:
			if not isinstance(row, dict):
				raise BridgeError("graph_message_list_invalid")
			_validate_message_header(row)
		for row in value:
			if not row.get("hasAttachments"):
				continue
			fingerprint = _fingerprint(_message_key(row))
			if fingerprint not in known:
				pending.append(row)
		next_url: str | None = None
		if "@odata.nextLink" not in response:
			next_url = None
		else:
			next_url = _validate_message_scan_url(response["@odata.nextLink"])
			if _message_scan_url_digest(next_url) in seen_url_digests:
				raise BridgeError("graph_message_next_link_cycle")
		if next_url is not None and _message_scan_url_digest(next_url) in history_set:
			raise BridgeError("graph_message_next_link_cycle")
		if len(pending) >= limit:
			return MessageBatch(pending[:limit], page_url, tuple(history))
		if next_url is None:
			return MessageBatch(pending, None, ())
		if len(history) >= MAX_MESSAGE_CURSOR_HISTORY:
			raise BridgeError("graph_message_cursor_history_exhausted")
		page_digest = _message_scan_url_digest(page_url)
		if page_digest in history_set:
			raise BridgeError("graph_message_next_link_cycle")
		history.append(page_digest)
		history_set.add(page_digest)
		url = next_url
	return MessageBatch(pending, url, tuple(history))


def _attachment_url_identity(value: Any, graph_id: str) -> str:
	if not isinstance(value, str) or not value or len(value) > 8192:
		raise BridgeError("graph_attachment_next_link_invalid")
	parsed = urlparse(value)
	decoded_path = unquote(parsed.path)
	segments = decoded_path.split("/")
	if (
		parsed.scheme != "https"
		or parsed.netloc.casefold() != "graph.microsoft.com"
		or len(segments) != 7
		or segments[0] != ""
		or segments[1].casefold() != "v1.0"
		or segments[2].casefold() != "users"
		or segments[3].casefold() != MAILBOX.casefold()
		or segments[4].casefold() != "messages"
		or segments[5] != graph_id
		or segments[6].casefold() != "attachments"
		or "\\" in decoded_path
		or parsed.fragment
	):
		raise BridgeError("graph_attachment_next_link_invalid")
	normalized_path = f"/v1.0/users/{MAILBOX.casefold()}/messages/{graph_id}/attachments"
	identity = f"https://graph.microsoft.com{normalized_path}"
	if parsed.query:
		identity = f"{identity}?{parsed.query}"
	return identity


def _validate_attachment_next_link(value: Any, graph_id: str) -> str:
	_attachment_url_identity(value, graph_id)
	return value


def _attachment_url_digest(value: str, graph_id: str) -> str:
	identity = _attachment_url_identity(value, graph_id)
	return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _fetch_attachment_metadata(request_graph: Callable[..., Any], graph_id: str) -> list[dict[str, Any]]:
	mailbox = quote(MAILBOX, safe="@")
	message_id = quote(graph_id, safe="")
	# Read only metadata until identity, vacancy, and attachment-shape gates pass.
	url = (
		f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{message_id}/attachments"
		"?$select=id,name,contentType,size,isInline"
	)
	rows: list[dict[str, Any]] = []
	seen: set[str] = set()
	pages = 0
	while True:
		page_digest = _attachment_url_digest(url, graph_id)
		if page_digest in seen:
			raise BridgeError("graph_attachment_next_link_cycle")
		pages += 1
		if pages > MAX_ATTACHMENT_PAGES:
			raise AdmissionBlock("blocked_multiple_or_ambiguous_attachments")
		seen.add(page_digest)
		response = _graph_get(request_graph, url)
		value = response.get("value")
		if not isinstance(value, list):
			raise BridgeError("graph_attachment_list_invalid")
		for row in value:
			if not isinstance(row, dict):
				raise BridgeError("graph_attachment_metadata_invalid")
			_validate_attachment_identity_fields(
				row,
				code="graph_attachment_metadata_invalid",
				allow_unsafe_candidate_filename=True,
			)
		rows.extend(value)
		if len(rows) > MAX_ATTACHMENT_ROWS:
			raise AdmissionBlock("blocked_multiple_or_ambiguous_attachments")
		if "@odata.nextLink" not in response:
			return rows
		next_url = _validate_attachment_next_link(response["@odata.nextLink"], graph_id)
		if _attachment_url_digest(next_url, graph_id) in seen:
			raise BridgeError("graph_attachment_next_link_cycle")
		url = next_url


def _fetch_attachment_content(
	request_graph: Callable[..., Any], graph_id: str, attachment_id: str
) -> dict[str, Any]:
	mailbox = quote(MAILBOX, safe="@")
	message_id = quote(graph_id, safe="")
	attachment = quote(attachment_id, safe="")
	value = _graph_get(
		request_graph,
		f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{message_id}/attachments/{attachment}",
	)
	if value.get("@odata.type") != "#microsoft.graph.fileAttachment":
		raise BridgeError("graph_attachment_content_invalid")
	return value


def _same_attachment(metadata: dict[str, Any], hydrated: dict[str, Any]) -> bool:
	return all(
		metadata.get(field) == hydrated.get(field)
		for field in ("@odata.type", "id", "name", "contentType", "size", "isInline")
	)


def _validate_attachment_identity_fields(
	value: dict[str, Any], *, code: str, allow_unsafe_candidate_filename: bool = False
) -> None:
	name = value.get("name")
	unsafe_candidate_filename = allow_unsafe_candidate_filename and _unsafe_candidate_filename(name)
	if (
		not isinstance(value.get("@odata.type"), str)
		or not value["@odata.type"].strip()
		or value["@odata.type"] != value["@odata.type"].strip()
		or len(value["@odata.type"]) > 512
		or not isinstance(value.get("id"), str)
		or not value["id"].strip()
		or value["id"] != value["id"].strip()
		or len(value["id"]) > 4096
		or not isinstance(name, str)
		or (not unsafe_candidate_filename and (not name.strip() or len(name) > 4096))
		or not isinstance(value.get("contentType"), str)
		or not value["contentType"].strip()
		or value["contentType"] != value["contentType"].strip()
		or len(value["contentType"]) > 512
		or not isinstance(value.get("size"), int)
		or isinstance(value.get("size"), bool)
		or value["size"] < 0
		or not isinstance(value.get("isInline"), bool)
	):
		raise BridgeError(code)


def _validate_hydrated_attachment(metadata: dict[str, Any], hydrated: dict[str, Any]) -> None:
	_validate_attachment_identity_fields(metadata, code="graph_attachment_metadata_invalid")
	_validate_attachment_identity_fields(hydrated, code="graph_attachment_content_invalid")
	if not _same_attachment(metadata, hydrated):
		raise BridgeError("graph_attachment_changed_after_preflight")
	content = hydrated.get("contentBytes")
	if not isinstance(content, str):
		raise BridgeError("graph_attachment_content_invalid")
	if not content:
		raise AdmissionBlock("blocked_candidate_cv_size")
	# Validate provider syntax before using encoded length as proof of an
	# oversized file. This keeps malformed provider data retryable without
	# decoding arbitrarily large strings.
	if not _has_strict_base64_syntax(content):
		raise BridgeError("graph_attachment_content_invalid")
	if len(content) > MAX_GRAPH_CONTENT_CHARS:
		raise AdmissionBlock("blocked_candidate_cv_size")
	try:
		decoded = base64.b64decode(content.encode("ascii"), validate=True)
	except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
		raise BridgeError("graph_attachment_content_invalid") from exc
	# Graph's attachment `size` includes provider overhead and is not the exact
	# decoded file length. Keep metadata/hydration identity exact, but enforce
	# the security boundary against the actual bytes.
	if not decoded or len(decoded) > MAX_CV_BYTES:
		raise AdmissionBlock("blocked_candidate_cv_size")


def _build_candidate(message: dict[str, Any], attachment: dict[str, Any]) -> CandidateMessage:
	key = _message_key(message)
	email, name = _sender(message)
	received = str(message.get("receivedDateTime") or "").strip()
	if not received or len(received) > 64:
		raise BridgeError("received_datetime_invalid")
	subject = str(message.get("subject") or "").strip()
	if len(subject) > MAX_SUBJECT_CHARS:
		raise BridgeError("graph_message_list_invalid")
	content = str(attachment.get("contentBytes") or "")
	payload = {
		"graph_message_id": str(message["id"]),
		"received_on": received,
		"sender_email": email,
		"sender_name": name,
		"subject": subject,
		"consent_current_vacancy": True,
		"consent_basis": CONSENT_BASIS,
		"consent_notice_version": CONSENT_NOTICE_VERSION,
		"consent_evidence_sha256": _consent_evidence_sha256(
			graph_id=str(message["id"]),
			received_on=received,
		),
		"attachments": [
			{
				"name": str(attachment.get("name") or "").strip(),
				"content_type": str(attachment.get("contentType") or "")[:140],
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
	if not isinstance(value, dict):
		raise BridgeError("remote_result_unexpected")
	if value.get("status") == "blocked" and value.get("code") in REMOTE_ADMISSION_CODES:
		return value
	if value.get("status") not in {"created", "already_processed"}:
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
		"faults": 0,
		"errors": [],
	}
	dirty = False
	advance_scan = True
	batch = _fetch_messages(
		graph.request_graph,
		limit,
		set(state["messages"]),
		start_url=state.get("message_scan_url"),
		cursor_history=state.get("message_scan_history", []),
	)
	messages = batch.messages
	for message in messages:
		summary["checked"] += 1
		fingerprint = _fingerprint(str(message.get("id") or "unknown"))
		try:
			key = _message_key(message)
			fingerprint = _fingerprint(key)
			if fingerprint in state["messages"]:
				summary["ignored"] += 1
				continue
			# Bind the applicant identity before reading CV bytes.
			_sender(message)
			_require_compatible_subject_vacancy(message)
			attachments = _fetch_attachment_metadata(graph.request_graph, str(message["id"]))
			selected, outcome = _select_candidate_attachment(attachments, require_content=False)
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
			hydrated = _fetch_attachment_content(
				graph.request_graph, str(message["id"]), str(selected.get("id") or "")
			)
			_validate_hydrated_attachment(selected, hydrated)
			selected, outcome = _select_candidate_attachment([hydrated])
			if selected is None:
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
			if result_status == "blocked":
				outcome = str(result["code"])
				summary["blocked"] += 1
				summary["errors"].append({"message": fingerprint, "code": outcome})
				if outcome in REMOTE_TERMINAL_ADMISSION_CODES:
					state["messages"][fingerprint] = outcome
					dirty = True
				else:
					advance_scan = False
				continue
			summary[result_status] += 1
			state["messages"][fingerprint] = result_status
			dirty = True
		except AdmissionBlock as exc:
			outcome = str(exc)[:80]
			summary["blocked"] += 1
			summary["errors"].append({"message": fingerprint, "code": outcome})
			if not dry_run:
				state["messages"][fingerprint] = outcome
				dirty = True
		except BridgeError as exc:
			summary["blocked"] += 1
			summary["faults"] += 1
			summary["errors"].append({"message": fingerprint, "code": str(exc)[:80]})
			advance_scan = False
	if not dry_run and advance_scan:
		if batch.resume_url is None:
			if "message_scan_url" in state or "message_scan_history" in state:
				state.pop("message_scan_url", None)
				state.pop("message_scan_history", None)
				dirty = True
		elif state.get("message_scan_url") != batch.resume_url or state.get(
			"message_scan_history", []
		) != list(batch.cursor_history):
			state["message_scan_url"] = batch.resume_url
			state["message_scan_history"] = list(batch.cursor_history)
			dirty = True
	if dirty:
		_save_state(state, state_path)
	if report_json:
		print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
	elif summary["blocked"]:
		reasons: dict[str, int] = {}
		for error in summary["errors"]:
			code = str(error.get("code") or "blocked_unknown")
			reasons[code] = reasons.get(code, 0) + 1
		print(
			json.dumps(
				{
					"ats_email_bridge": "error" if summary["faults"] else "attention",
					"blocked": summary["blocked"],
					"faults": summary["faults"],
					"reasons": reasons,
				},
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
	# Deterministic admission blockers are domain outcomes, not runner failures.
	# They remain visible on stdout but must not be mislabeled as a crashed bridge.
	return 2 if summary["faults"] else 0


if __name__ == "__main__":
	raise SystemExit(main())
