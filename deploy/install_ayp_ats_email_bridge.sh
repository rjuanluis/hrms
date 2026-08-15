#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 4 ]; then
  printf 'usage: %s SOURCE_DIR EXPECTED_RUNNER_SHA256 EXPECTED_TEST_SHA256 EXPECTED_LAUNCHER_SHA256\n' "$0" >&2
  exit 64
fi

source_dir=$1
expected_runner_sha=$2
expected_test_sha=$3
expected_launcher_sha=$4
install_dir=${AYP_ATS_BRIDGE_INSTALL_DIR:-"$HOME/.hermes/scripts"}
state_dir=${AYP_ATS_BRIDGE_STATE_DIR:-"$HOME/.hermes/state"}
runner_name=ayp_ats_email_bridge.py
test_name=test_ayp_ats_email_bridge.py
launcher_name=run_ayp_ats_email_bridge.sh
runner_source="$source_dir/$runner_name"
test_source="$source_dir/$test_name"
launcher_source="$source_dir/$launcher_name"

sha256() {
  shasum -a 256 "$1" | cut -d ' ' -f 1
}

validate_sha() {
  if [[ ! "$1" =~ ^[0-9a-f]{64}$ ]]; then
    printf 'invalid sha256\n' >&2
    exit 65
  fi
}

validate_sha "$expected_runner_sha"
validate_sha "$expected_test_sha"
validate_sha "$expected_launcher_sha"
test -f "$runner_source"
test -f "$test_source"
test -f "$launcher_source"
[ "$(sha256 "$runner_source")" = "$expected_runner_sha" ]
[ "$(sha256 "$test_source")" = "$expected_test_sha" ]
[ "$(sha256 "$launcher_source")" = "$expected_launcher_sha" ]

tmp_dir=$(mktemp -d)
trap 'rm -rf "$tmp_dir"' EXIT
install -m 0700 "$runner_source" "$tmp_dir/$runner_name"
install -m 0600 "$test_source" "$tmp_dir/$test_name"
install -m 0700 "$launcher_source" "$tmp_dir/$launcher_name"
python3 "$tmp_dir/$test_name"
bash -n "$tmp_dir/$launcher_name"

install -m 0700 -d "$install_dir"
install -m 0700 -d "$state_dir"
install -m 0700 "$runner_source" "$install_dir/$runner_name"
install -m 0600 "$test_source" "$install_dir/$test_name"
install -m 0700 "$launcher_source" "$install_dir/$launcher_name"
[ "$(sha256 "$install_dir/$runner_name")" = "$expected_runner_sha" ]
[ "$(sha256 "$install_dir/$test_name")" = "$expected_test_sha" ]
[ "$(sha256 "$install_dir/$launcher_name")" = "$expected_launcher_sha" ]

printf '{"status":"installed_not_scheduled","runner_sha256":"%s","test_sha256":"%s","launcher_sha256":"%s"}\n' \
  "$expected_runner_sha" "$expected_test_sha" "$expected_launcher_sha"
