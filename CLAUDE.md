# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

LearnSkills is a locally-run FastAPI + vanilla-JS chat app built on `claude-opus-4-7`. It demonstrates Anthropic's Skills pattern: progressive-disclosure instruction loading via a custom `read_skill` tool. Treat the source as the primary teaching artifact — every cache marker, stop-reason branch, and design choice carries an inline comment explaining *why*.

## Run / develop

First-time setup needs one extra step beyond the README quickstart — running the symlink script that pulls in Anthropic's official document skills (`pdf`/`docx`/`xlsx`/`pptx`/`skill-creator`):

```bash
cp .env.example .env                    # then add ANTHROPIC_API_KEY
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
bash scripts/link-document-skills.sh    # creates gitignored symlinks under skills/
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

The symlinks point to `~/.claude/plugins/marketplaces/anthropic-agent-skills/skills/<name>/`. Without the script, only the 4 custom skills load (vs. 9 expected). Without Claude Code installed locally, that path won't exist — the script exits with a helpful error.

**Hot-reload skills without restarting the server:**

```bash
curl -X POST http://127.0.0.1:8000/api/skills/reload
```

Use this after editing any `SKILL.md`. Uvicorn's `--reload` only watches Python; it doesn't reload skills (cached at startup) or static frontend (cached by the browser — hard-refresh with Cmd+Shift+R after `frontend/index.html` edits).

**Run the agent loop without the UI:**

```bash
python -m backend.agent_cli "Review this Python: def divide(a, b): return a / b"
```

There is no test suite. Verification is manual: run a prompt through the CLI harness or the UI and watch the cache-hit % footer climb on follow-ups.

## Architecture (the parts that take multiple files to understand)

### The agent loop and the cache strategy live together in `backend/agent.py`

`run_turn` is a manual agentic loop, not `messages.tool_runner` — every `stop_reason` is handled explicitly:

- `end_turn` / `refusal` / `max_tokens` → close the stream
- `tool_use` → execute `read_skill` client-side (server-side tools auto-execute), append tool_results, loop
- `pause_turn` → re-send the messages array unchanged; server resumes the server-side tool

**Three cache_control markers are placed on every request, in this exact order on the wire (`tools` → `system` → `messages`):**

1. Last `tools` entry (`read_skill`) — caches all tool defs (~stable across all turns)
2. Second `system` block (the alphabetically-sorted skills catalog) — biggest single token win
3. Last block of the last prior message — caches conversation history (turn 2+ only)

Anything that perturbs prefix bytes invalidates everything from that point onward. Specifically: do not interpolate timestamps/UUIDs into the system prompt, do not reorder skills (always alphabetical via `render_skill_catalog`), do not change the tool list mid-conversation.

**Thinking blocks must round-trip verbatim**, including their `signature` field. `_content_to_dict_list` converts the SDK's response content to plain dicts; subsequent turns echo it back unchanged. Stripping or reordering thinking blocks breaks adaptive thinking.

### Skills system

Skills live as `skills/<name>/SKILL.md` with YAML frontmatter (`name`, `description`). The loader (`backend/skills.py`):

- Walks `skills/` at startup; symlinks are followed transparently
- Requires `frontmatter.name == directory_basename` (line 79 in skills.py); won't load otherwise
- Logs and skips malformed skills — never crashes the server
- Always sorts the catalog alphabetically (cache determinism)

Only the catalog (name + description per skill) lives in the system prompt. Full bodies are loaded by the model via the `read_skill(name)` tool when relevant. This is progressive disclosure — keeps the cached prefix small even with many skills.

**Symlinked skills** (`pdf`, `docx`, `xlsx`, `pptx`, `skill-creator`) are gitignored (they reference absolute paths under the user's home). They're managed by `scripts/link-document-skills.sh`. The 4 committed custom skills are `code-reviewer`, `data-analyst`, `frontend-designer`, `polished-email`.

### File attachments

`backend/files.py` validates uploads, calls `client.beta.files.upload(..., betas=["files-api-2025-04-14"])`, and produces content blocks:

- **PDFs** → `document` block with `source: {type: "file", file_id}` (Claude reads natively)
- **Everything else** (CSV/JSON/DOCX/XLSX/PPTX) → `container_upload` block (file lands in the `code_execution` sandbox at `/mnt/user-data/uploads/<filename>`)
- Each file block is paired with a `text` "[Attached: name]" block so the model has the filename in plain language

File blocks carry a leading-underscore `_filename` (and `_mime`) hint for the UI to render chips after a page refresh. **`agent._strip_local_fields` removes any `_`-prefixed keys before the API call** — Anthropic rejects unknown keys.

The `/api/conversations/{id}/messages` endpoint takes `multipart/form-data` (`content: str = Form(""), attachments: list[UploadFile] = File(default=[])`), not JSON.

### Streaming

Server uses `sse-starlette` which emits frames separated by **CRLF (`\r\n\r\n`)**. The frontend parser at `frontend/index.html:sendMessage` must accept all three SSE-spec separators (`\r\n\r\n`, `\n\n`, `\r\r`) — splitting on `\n\n` only silently drops every event because there's no `\n\n` substring inside `\r\n\r\n`. This was a real bug; don't reintroduce it.

### Persistence

`backend/db.py` stores message content as JSON (`content_json` column) so it round-trips the SDK's block list intact. Conversations + messages share a single SQLite DB at `learnskills.db` (gitignored). No schema change needed for new block types — `append_message` accepts any `list[dict]`.

A per-conversation `asyncio.Lock` (`backend/main.py:_lock_for`) rejects concurrent streams with HTTP 409. Locks are created lazily, never garbage-collected — fine for single-user local; would need pruning for multi-user.

## Things that will break the build / API call

- **Do not pass `temperature`, `top_p`, `top_k`, or `budget_tokens`** to `messages.stream()`. All four were removed on Opus 4.7 and return 400. The codebase does not set any of them; keep it that way.
- **Do not assistant-prefill the last turn.** Opus 4.6/4.7 reject this.
- **Do not set `cache_control` on more than 4 blocks total** across the whole request — it's the API limit. We use exactly 3.

## When authoring or polishing a skill

Invoke `document-skills:skill-creator` first via the Skill tool — it has Anthropic's authoritative SKILL.md authoring rules. Without consulting it, common mistakes:

- Body opens declaratively ("You are a data analyst.") instead of imperatively
- Body restates the description (wasted tokens — description is already in context when the body loads)
- "When to use" is in the body instead of the description (wrong — body is loaded *after* the trigger fires, so the model can't use it for triggering)
- Adding `README.md` / `INSTALLATION_GUIDE.md` / `CHANGELOG.md` inside the skill folder (skill-creator forbids these)
- Bundling `scripts/` for code the model would write fluently inline anyway (chart code, generic data manipulation)

Skills should add: sandbox-specific paths, behavioral rules easy to forget under pressure, project-specific knowledge the model can't have, output-shape contracts. They should not add: general programming or stats education the model already has from pretraining.

## Frontend specifics

- Single `frontend/index.html` (no build step). Uses Tailwind via CDN, marked + DOMPurify for markdown, highlight.js for code blocks.
- The chat input is a single-line `<input type="text">` (not a textarea). Plain Enter sends; there is intentionally no Shift+Enter newline.
- The trash icon on each sidebar conversation deletes immediately with no confirm dialog (a deliberate UX choice — user prefers single-click).
- Cache footer shows running cache-hit % across the conversation; it's the headline metric for verifying cache strategy is working.
