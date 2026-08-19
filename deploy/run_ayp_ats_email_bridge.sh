#!/usr/bin/env bash
set -euo pipefail

umask 077

if [ "$#" -ne 0 ]; then
  printf '{"status":"blocked","reason":"launcher_accepts_no_arguments"}\n' >&2
  exit 64
fi

graph_python=${AYP_ATS_GRAPH_PYTHON:-"$HOME/.hermes/venvs/msgraph-app/bin/python"}
runner=${AYP_ATS_BRIDGE_RUNNER:-"$HOME/.hermes/scripts/ayp_ats_email_bridge.py"}
stdout_guard="$HOME/.hermes/scripts/decision_ledger.py"

blocked() {
  printf 'Tema: Aro y Pedal — ATS correo CV\n'
  printf 'Estado: bloqueado\n'
  printf 'Evidencia: %s\n' "$1"
  printf 'Próximo paso: revisar el código local reportado antes de reintentar.\n'
}

if [ ! -x "$graph_python" ]; then
  blocked "graph_python_unavailable"
  exit 0
fi
if [ ! -r "$runner" ]; then
  blocked "runner_unavailable"
  exit 0
fi
if [ ! -r "$stdout_guard" ]; then
  blocked "decision_ledger_stdout_guard_unavailable"
  exit 0
fi

tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/ayp-ats-email-bridge.XXXXXX") || {
  blocked "output_capture_unavailable"
  exit 0
}
trap 'rm -rf "$tmp_dir"' EXIT HUP INT TERM
runner_stdout="$tmp_dir/runner.stdout"
runner_stderr="$tmp_dir/runner.stderr"
delivery="$tmp_dir/delivery.txt"
guarded="$tmp_dir/guarded.txt"

set +e
"$graph_python" "$runner" --limit 10 >"$runner_stdout" 2>"$runner_stderr"
runner_rc=$?
set -e

if [ "$runner_rc" -eq 0 ] && [ ! -s "$runner_stdout" ]; then
  exit 0
fi

if [ "$runner_rc" -ne 0 ]; then
  {
    printf 'Tema: Aro y Pedal — ATS correo CV\n'
    printf 'Estado: bloqueado\n'
    printf 'Evidencia: bridge local terminó con código %s.' "$runner_rc"
    if [ -s "$runner_stdout" ]; then
      printf ' Detalle sanitizado: '
      tr '\n' ' ' <"$runner_stdout"
      printf '\n'
    else
      diagnostic=$(shasum -a 256 "$runner_stderr")
      diagnostic=${diagnostic%% *}
      printf ' Diagnóstico stderr sha256=%s.\n' "$diagnostic"
    fi
    printf 'Próximo paso: revisar el código local reportado antes de reintentar.\n'
  } >"$delivery"
else
  {
    printf 'Tema: Aro y Pedal — ATS correo CV\n'
    printf 'Estado: revisión de admisión requerida\n'
    printf 'Evidencia: el bridge terminó correctamente y no creó los mensajes rechazados. Resumen sanitizado: '
    tr '\n' ' ' <"$runner_stdout"
    printf '\n'
    printf 'Próximo paso: revisar consentimiento o adjuntos; no es una caída del bridge.\n'
  } >"$delivery"
fi

# decision_ledger_stdout_guard equivalent for a shell no-agent launcher: only
# filtered stdout is deliverable; guard failure preserves the original alert.
set +e
"$graph_python" "$stdout_guard" filter-output \
  --domain ayp --mode block --threshold 0.38 \
  <"$delivery" >"$guarded" 2>/dev/null
guard_rc=$?
set -e
if [ "$guard_rc" -eq 0 ]; then
  cat "$guarded"
else
  cat "$delivery"
fi

exit 0
