#!/usr/bin/env bash
# Verifica TODO el rango saliente antes de un push: autoría única del autor del repo.
#
# Regla dura (autor, 26-ago-2026): todos los commits y pushes con autoría única de jrebull.
#   - Autor Y committer = la identidad del autor (committer "GitHub <noreply@github.com>" se
#     admite solo porque es lo que GitHub estampa en los squash merges hechos desde la UI).
#   - Cero Co-Authored-By; cero trailers/firmas de IA (Claude/ChatGPT/Codex/Copilot/OpenAI...).
#   - Cero menciones de IA en el mensaje (mismo PATTERN que tools/check_no_coauthor.sh y el job
#     `commit-policy` de CI, ampliado a otros asistentes).
# El push publica los commits tal cual: si algo falla aquí, NO se sube y se corrige antes.
#
# Uso: bash tools/check_outgoing_authorship.sh [<rango>]
#   - como hook pre-push de pre-commit usa PRE_COMMIT_FROM_REF..PRE_COMMIT_TO_REF;
#   - sin argumentos ni env: origin/main..HEAD.
set -euo pipefail

EXPECTED_NAME="$(git config user.name)"
EXPECTED_EMAIL="$(git config user.email)"
GITHUB_COMMITTER="GitHub <noreply@github.com>"
PATTERN='co-authored-by|@anthropic|anthropic\.com|generated with .*(claude|chatgpt|codex|copilot|openai|gpt)|claude code|claude-session|claude\.ai|noreply@anthropic|chatgpt|openai|codex|copilot|ai-generated|assisted-by|generated-by'

if [ $# -ge 1 ]; then
  range="$1"
elif [ -n "${PRE_COMMIT_FROM_REF:-}" ] && [ -n "${PRE_COMMIT_TO_REF:-}" ]; then
  if [ "$PRE_COMMIT_FROM_REF" = "0000000000000000000000000000000000000000" ]; then
    range="origin/main..${PRE_COMMIT_TO_REF}"   # rama nueva en el remoto
  else
    range="${PRE_COMMIT_FROM_REF}..${PRE_COMMIT_TO_REF}"
  fi
else
  range="origin/main..HEAD"
fi

fail=0
n=0
for c in $(git rev-list "$range" 2>/dev/null); do
  n=$((n + 1))
  author="$(git log -1 --format='%an <%ae>' "$c")"
  committer="$(git log -1 --format='%cn <%ce>' "$c")"
  if [ "$author" != "$EXPECTED_NAME <$EXPECTED_EMAIL>" ]; then
    echo "✗ $c autor '$author' != '$EXPECTED_NAME <$EXPECTED_EMAIL>'" >&2; fail=1
  fi
  if [ "$committer" != "$EXPECTED_NAME <$EXPECTED_EMAIL>" ] && [ "$committer" != "$GITHUB_COMMITTER" ]; then
    echo "✗ $c committer '$committer' no es el autor (ni GitHub squash)" >&2; fail=1
  fi
  if git log -1 --format='%B' "$c" | grep -qiE "$PATTERN"; then
    echo "✗ $c mensaje con rastro de IA/coautoría:" >&2
    git log -1 --format='%B' "$c" | grep -inE "$PATTERN" | sed 's/^/    /' >&2; fail=1
  fi
  if git log -1 --format='%B' "$c" | grep -qiE '^(signed-off-by|reviewed-by):' \
     && ! git log -1 --format='%B' "$c" | grep -iE '^(signed-off-by|reviewed-by):' | grep -qF "$EXPECTED_EMAIL"; then
    echo "✗ $c trailer de firma ajeno al autor" >&2; fail=1
  fi
done

if [ "$fail" = "0" ]; then
  echo "✓ autoría única en $range ($n commits): $EXPECTED_NAME <$EXPECTED_EMAIL>"
else
  echo "✗ push BLOQUEADO: corrige los commits de arriba (amend/reset --soft) antes de subir." >&2
fi
exit "$fail"
