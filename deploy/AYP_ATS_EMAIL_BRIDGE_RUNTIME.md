# AyP ATS email bridge runtime

This host runner is a separately gated part of the AyP HRMS release. Application deployment does **not** imply email-intake activation.

## Install an audited build

From an exact clean checkout, calculate all three hashes and invoke:

```bash
runner_sha=$(shasum -a 256 deploy/ayp_ats_email_bridge.py | cut -d ' ' -f 1)
test_sha=$(shasum -a 256 deploy/test_ayp_ats_email_bridge.py | cut -d ' ' -f 1)
launcher_sha=$(shasum -a 256 deploy/run_ayp_ats_email_bridge.sh | cut -d ' ' -f 1)
deploy/install_ayp_ats_email_bridge.sh deploy "$runner_sha" "$test_sha" "$launcher_sha"
```

The installer verifies all source hashes, runs the runner suite from a temporary directory, installs the runner and launcher as `0700`, its test as `0600`, ensures the state directory is `0700`, verifies installed hashes, and reports `installed_not_scheduled`.

The host Graph client must expose the backward-compatible `immutable_message_ids` option and emit `Prefer: IdType="ImmutableId"`. The runner verifies that capability at load time and fails closed if it is absent.

## Activation gate

Do not create a scheduler until all of these are true:

1. PR and immutable production image are audited at the exact SHA.
2. Exchange RBAC authorizes `Application Mail.Read` for `empleos@aroypedal.com` and denies mail/calendar/settings access outside the approved matrix.
3. The Frappe endpoint compatibility check passes against the deployed image, and the exact `empleos@aroypedal.com` Email Account (if present) has `enable_incoming = 0`, `enable_auto_reply = 0`, and no account/folder `append_to = Job Applicant` route.
4. The deployed schema includes the unique Graph message key and hidden immutable email-provenance marker; Bench-native tests are green.
5. A single synthetic canary and complete cleanup are approved.

## Intended scheduler definition

After approval, create a Hermes **no-agent** recurring job whose `script` is the installed launcher:

```text
~/.hermes/scripts/run_ayp_ats_email_bridge.sh
```

The launcher accepts no arguments and invokes `~/.hermes/venvs/msgraph-app/bin/python ~/.hermes/scripts/ayp_ats_email_bridge.py --limit 10`. Do not register the Python file itself as the no-agent script: Hermes would otherwise use a generic Python environment that may not contain `msal`. Before activation, verify locally—without making a Graph request—that the Graph venv can import `msal` and that `msgraph_app_cli.request_graph` exposes `immutable_message_ids`.

Recommended interval: every 5 minutes. Empty stdout means success/no alert. The runner emits only sanitized attention/error JSON and exits non-zero on blocked/error outcomes. No scheduler is installed by this repository.

## Rollback

1. Pause/remove the no-agent job.
2. Restore the prior audited runner/test/launcher set using the same SHA-verifying installer, or remove all three installed files.
3. Restore the previous immutable HRMS image if application rollback is required.
4. Preserve the mailbox: the runner uses Graph GET only and never marks, moves, deletes, or replies.
