# AyP ATS email bridge runtime

This host runner is a separately gated part of the AyP HRMS release. Application deployment does **not** imply email-intake activation.

## Install an audited build

From an exact clean checkout, calculate all three hashes and invoke:

```bash
runner_sha=$(shasum -a 256 deploy/ayp_ats_email_bridge.py | cut -d ' ' -f 1)
test_sha=$(shasum -a 256 deploy/test_ayp_ats_email_bridge.py | cut -d ' ' -f 1)
launcher_sha=$(shasum -a 256 deploy/run_ayp_ats_email_bridge.sh | cut -d ' ' -f 1)
vacancy_parser_sha=$(shasum -a 256 hrms/recruitment/ats_vacancy_reference.py | cut -d ' ' -f 1)
deploy/install_ayp_ats_email_bridge.sh deploy "$runner_sha" "$test_sha" "$launcher_sha" "$vacancy_parser_sha"
```

The installer verifies all source hashes, runs the runner suite from a temporary directory, installs the runner and launcher as `0700`, its test as `0600`, ensures the state directory is `0700`, verifies installed hashes, and reports `installed_not_scheduled`.

The host Graph client must expose the backward-compatible `immutable_message_ids` option and emit `Prefer: IdType="ImmutableId"`. The runner verifies that capability at load time and fails closed if it is absent.

## Activation gate

Do not create a scheduler until all of these are true:

1. PR and immutable production image are audited at the exact SHA; the authenticated Frappe intake session reads back `REPEATABLE-READ` immediately before its authoritative vacancy lock and fails closed under any other isolation. The isolated MariaDB authority-lock job proves that both a third-vacancy insert and a second vacancy's `Closed` → `Open` transition remain blocked through simulated private-file storage, applicant insertion, and transaction commit.
2. Exchange RBAC authorizes `Application Mail.Read` for `empleos@aroypedal.com` and denies mail/calendar/settings access outside the approved matrix.
3. The Frappe endpoint compatibility check passes against the deployed image, and the exact `empleos@aroypedal.com` Email Account (if present) has `enable_incoming = 0`, `enable_auto_reply = 0`, and no account/folder `append_to = Job Applicant` route.
4. The deployed schema includes the unique Graph message key and hidden immutable email-provenance marker; Bench-native tests are green.
5. A single synthetic canary and complete cleanup are approved.

## Intended scheduler definition

After approval, create a Hermes **no-agent** recurring job whose `script` is the installed launcher:

```text
~/.hermes/scripts/run_ayp_ats_email_bridge.sh
```

The launcher accepts no arguments and invokes `~/.hermes/venvs/msgraph-app/bin/python ~/.hermes/scripts/ayp_ats_email_bridge.py --limit 10`. Do not register the Python file itself as the no-agent script: Hermes would otherwise use a generic Python environment that may not contain `msal`. Before activation, verify locally—without making a Graph request—that the Graph venv can import `msal` and that `msgraph_app_cli.request_graph` exposes `immutable_message_ids`. Also read back `@@transaction_isolation` through the deployed Frappe session and require `REPEATABLE-READ`; the endpoint performs the same check on every intake before acquiring the authority lock.

Recommended interval: every 5 minutes. Empty stdout means success/no alert. The runner scans message headers in bounded 100-item pages from the 2026-08-13 channel activation boundary, while processing at most the configured per-run limit. State schema v2 identifies canonical cursor digests; a v1 messages-only state migrates in memory, but any v1 cursor/history fails closed because raw digests cannot be safely reinterpreted. A private, validated Graph continuation URL plus a bounded PII-free history of canonical page-request digests (case-normalized HTTPS authority/path, opaque query preserved) are persisted so a mailbox with more than 1,000 matching messages advances across runs instead of repeatedly scanning only the newest pages; every returned page and its cross-run cycle identity are validated completely before the per-run processing limit is applied, message IDs require exact bounded strings, a present continuation must be a non-empty exact mailbox-collection URL without a fragment, cycles within or across runs fail closed before any remote ingestion, and provider or HRMS faults do not advance that cursor. Legitimate attachment collection pages are followed metadata-only through exact mailbox/message/collection HTTPS continuations: fixed route segments are normalized, but the opaque Graph message ID is compared and hashed byte-for-byte; cycles are checked immediately on every received continuation before terminal page/count bounds. Malformed, cross-message, case-aliased, fragmented, or cyclic attachment continuations remain retryable provider faults. Unsafe non-inline filenames are terminally recorded before consent-body or attachment-byte reads, while unsafe inline names cannot preempt selection; both runner and backend enforce 140 characters and 240 UTF-8 bytes so storage cannot reach filesystem `NAME_MAX`. Standard Graph HTML wrappers (`html/head/meta/body`) and a narrowly allowlisted visible formatting subset are parsed to exact visible consent text while hidden, scripted, commented, overlaid, styled-ambiguous, negated, forwarded, or extra content remains rejected. Oversized string bodies are deterministic missing-consent blocks rather than retryable provider faults. The full bounded subject is validated and transported to the backend; only after matching vacancy references does the backend truncate it to the 140-character provenance field, so truncation cannot manufacture a malformed reference. The runner binds `sender` and `from` and blocks delegated/send-on-behalf mismatches before reading consent or attachment metadata. It validates every metadata member and requires exact JSON types for every hydrated identity field before checking base64 and declared byte length; floats, booleans-as-integers, and integers-as-booleans are retryable provider faults and can never become a durable rejection or accepted candidate. Consent bodies likewise require Graph's string `text` or `html` content type; malformed types are retryable and are never persisted as missing consent. Canonically decomposed Spanish accents are recomposed with NFC, while remaining combining overlays or Unicode format controls make the body retryable-invalid instead of being stripped into an apparent authorization. Deterministic admission blockers emit one sanitized aggregate Evidence Gate object with `Estado: requiere decisión` and exit zero so they are not mislabeled as a crashed bridge; infrastructure/runtime failures emit `Estado: bloqueado`, sanitized error evidence, remain retryable without a terminal message-state write, and exit non-zero. A vacancy code is optional in the subject: the runner and authenticated server use the same SHA-verified parser and assign only when `HR-OPN-2026-0001` is the sole open vacancy, while every malformed, incomplete, padded, altered, different, or conflicting ASCII `HR-OPN-` reference is a terminal admission block before attachment reads. Exact current-vacancy consent and all CV security gates remain mandatory; deterministic format/security rejection of hydrated CV bytes is terminally recorded, while antivirus or infrastructure availability failures remain retryable. No scheduler is installed by this repository.

## Rollback

1. Pause/remove the no-agent job.
2. Preserve a private `0600` backup of the state before changing runner versions. Do not feed state v2 to a v1 runner: the old runner fails closed. For rollback, restore the private pre-v2 state; if none exists, use an audited offline conversion that preserves `messages`, removes cursor/history, and sets version 1 while the scheduler remains paused.
3. Restore the prior audited runner/test/launcher/parser set using the same SHA-verifying installer, or remove all four installed files.
4. Restore the previous immutable HRMS image if application rollback is required.
5. Preserve the mailbox: the runner uses Graph GET only and never marks, moves, deletes, or replies.
