#!/usr/bin/env bash
# Bloquea cualquier commit cuyo mensaje meta un co-autor de IA / rastro de Claude.
# Stage: commit-msg (recibe la ruta del archivo del mensaje como $1).
# Repo de autoría única (jrebull): JAMÁS Co-Authored-By, anthropic, ni "Generated with Claude".
#
# F1: el regex original NO atrapaba `Claude-Session: https://claude.ai/...` (el trailer
# que el harness añade por defecto) ni URLs claude.ai. 'claude' a secas sigue permitido
# (commits legítimos sobre CLAUDE.md); se bloquean las formas-trailer y las URLs.
# El mismo patrón lo consume el job de CI (scan del push) — mantenerlos alineados.
set -euo pipefail
msg_file="${1:?uso: check_no_coauthor.sh <commit-msg-file>}"

PATTERN='co-authored-by|@anthropic|anthropic\.com|generated with .*claude|claude code|claude-session|claude\.ai|noreply@anthropic'

if grep -qiE "$PATTERN" "$msg_file"; then
  echo "✗ Commit BLOQUEADO: el mensaje contiene un co-autor de IA / rastro de Claude." >&2
  grep -inE "$PATTERN" "$msg_file" | sed 's/^/    /' >&2
  echo "  Quita esa línea. Este repo es de autoría única (jrebull)." >&2
  exit 1
fi
