#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 0 ]; then
  printf '{"status":"blocked","reason":"launcher_accepts_no_arguments"}\n' >&2
  exit 64
fi

graph_python=${AYP_ATS_GRAPH_PYTHON:-"$HOME/.hermes/venvs/msgraph-app/bin/python"}
runner=${AYP_ATS_BRIDGE_RUNNER:-"$HOME/.hermes/scripts/ayp_ats_email_bridge.py"}

if [ ! -x "$graph_python" ]; then
  printf '{"status":"blocked","reason":"graph_python_unavailable"}\n' >&2
  exit 78
fi
if [ ! -r "$runner" ]; then
  printf '{"status":"blocked","reason":"runner_unavailable"}\n' >&2
  exit 78
fi

exec "$graph_python" "$runner" --limit 10
