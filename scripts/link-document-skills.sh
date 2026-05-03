#!/usr/bin/env bash
# Symlink Anthropic's official document-handling skills into skills/.
#
# These skills (pdf, docx, xlsx, pptx) ship with Claude Code as part of the
# anthropic-agent-skills marketplace plugin. We symlink them rather than
# copy so they track upstream updates automatically. The symlinks are
# .gitignored because they reference absolute paths under the user's home.
#
# Usage:  bash scripts/link-document-skills.sh
# Idempotent: safe to re-run.

set -euo pipefail

SOURCE_BASE="${HOME}/.claude/plugins/marketplaces/anthropic-agent-skills/skills"
TARGET_BASE="$(cd "$(dirname "$0")/.." && pwd)/skills"

if [[ ! -d "$SOURCE_BASE" ]]; then
  echo "ERROR: official skills not found at $SOURCE_BASE" >&2
  echo "Install Claude Code (https://claude.com/claude-code) and run it once" >&2
  echo "to populate the plugin marketplace, then re-run this script." >&2
  exit 1
fi

linked=0
for name in pdf docx xlsx pptx; do
  src="$SOURCE_BASE/$name"
  dst="$TARGET_BASE/$name"
  if [[ ! -d "$src" ]]; then
    echo "WARN: skill not found at $src — skipping" >&2
    continue
  fi
  ln -sfn "$src" "$dst"
  echo "linked: skills/$name -> $src"
  linked=$((linked + 1))
done

echo
echo "Linked $linked document skill(s). Restart the server to pick them up."
