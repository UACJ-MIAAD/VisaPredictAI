#!/usr/bin/env bash
# Bloquea cualquier commit cuyo mensaje meta un co-autor de IA / rastro de Claude.
# Stage: commit-msg (recibe la ruta del archivo del mensaje como $1).
# Repo de autoría única (jrebull): JAMÁS Co-Authored-By, anthropic, ni "Generated with Claude".
set -euo pipefail
msg_file="${1:?uso: check_no_coauthor.sh <commit-msg-file>}"

# 'claude' a secas NO se bloquea (hay commits legítimos sobre CLAUDE.md).
if grep -qiE 'co-authored-by|@anthropic|generated with .*claude|claude code' "$msg_file"; then
  echo "✗ Commit BLOQUEADO: el mensaje contiene un co-autor de IA / rastro de Claude." >&2
  grep -inE 'co-authored-by|@anthropic|generated with .*claude|claude code' "$msg_file" | sed 's/^/    /' >&2
  echo "  Quita esa línea. Este repo es de autoría única (jrebull)." >&2
  exit 1
fi
