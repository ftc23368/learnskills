# LearnSkills

A locally-runnable AI agent that demonstrates Anthropic's **Skills** pattern — progressive-disclosure instruction loading via a `read_skill` tool — built on Claude Opus 4.7 with prompt caching, adaptive thinking, streaming, and a Claude.ai-style chat UI.

This is a **learning project**. Read the source — every cache marker, stop-reason branch, and design choice has an inline comment explaining why.

---

## Quick start

```bash
cp .env.example .env                    # then edit .env and add your ANTHROPIC_API_KEY
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000.

Try:
- "Review this Python: `def divide(a, b): return a / b`" → triggers `read_skill("code-reviewer")`
- "Compute the mean of [1,2,3,4,5,6,7,8,9,10] and the std dev" → triggers `read_skill("data-analyst")` + `code_execution`
- "Design a landing page for a coffee shop" → triggers `read_skill("frontend-designer")`

Send a follow-up in the same conversation and watch the **cache-hit %** climb in the footer — that's the prompt cache in action.

---

## Architecture

```
                                     ┌──────────────────────────────────────────┐
                                     │  frontend/index.html   (single-file UI)  │
                                     │   - fetch + ReadableStream SSE consumer  │
                                     │   - markdown + thinking + tool chips     │
                                     │   - cache-hit footer                     │
                                     └──────────────┬───────────────────────────┘
                                                    │ HTTP / SSE
                                                    ▼
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │  backend/main.py   (FastAPI)                                                │
 │   - /api/conversations  CRUD                                                │
 │   - /api/skills         catalog                                             │
 │   - /api/conversations/{id}/messages   ← SSE streaming endpoint             │
 │   - per-conversation asyncio.Lock (rejects concurrent streams with 409)     │
 └──────────────┬──────────────────────────────────────────────────┬───────────┘
                │                                                  │
                ▼                                                  ▼
   ┌─────────────────────────┐                  ┌─────────────────────────────────┐
   │  backend/db.py          │                  │  backend/agent.py               │
   │   aiosqlite, JSON       │                  │   manual agentic loop           │
   │   preserves thinking +  │                  │   3 cache_control markers       │
   │   tool blocks verbatim  │                  │   handles all 5 stop_reasons    │
   └─────────────────────────┘                  └────────────────┬────────────────┘
                                                                 │
                                                                 ▼
                                                    ┌─────────────────────────────┐
                                                    │  Anthropic API              │
                                                    │   claude-opus-4-7           │
                                                    │   adaptive thinking         │
                                                    │   web_search server tool    │
                                                    │   code_execution server tool│
                                                    └─────────────────────────────┘
                                  ▲
                                  │  on-demand load via read_skill(name)
                                  │
                      ┌───────────┴────────────┐
                      │  skills/<name>/SKILL.md│
                      │  YAML frontmatter      │
                      │  full body on disk     │
                      └────────────────────────┘
```

### Agent turn — sequence

```
user message ──► persist user msg ──► acquire conversation lock (409 if held)
                                            │
                                            ▼
                                  history = SELECT * FROM messages
                                            │
                                            ▼
        ┌── apply cache_control to last prior message (turn 2+ only)
        │
        ▼
   client.messages.stream(
       model=claude-opus-4-7,
       thinking={type: adaptive},
       output_config={effort: high},
       system=[instructions, skills_catalog(cache_control)],
       tools=[web_search, code_execution, read_skill(cache_control)],
       messages=...,
   )
        │
        ▼
   stream events ──► forward to SSE: text_delta / thinking_delta / tool_use / usage
        │
        ▼
   final_message.stop_reason ?
        │
        ├── end_turn   → persist assistant msg → close stream
        ├── tool_use   → append assistant blocks (thinking preserved verbatim)
        │              → execute read_skill (server-side ones auto-execute)
        │              → append tool_results
        │              → loop
        ├── pause_turn → re-send messages array → loop (server resumes)
        ├── refusal    → persist → close
        └── max_tokens → persist (truncated) → close

   loop capped at 10 iterations
```

---

## The cache strategy (the load-bearing learning artifact)

Prompt caching on the Anthropic API is a **byte-exact prefix match**. Any byte change anywhere in the prefix invalidates the cache for everything at or after that position. Render order is `tools` → `system` → `messages`.

This implementation places **three `cache_control` markers** per request to maximize the cached prefix:

| # | Where (source) | What gets cached | Why |
|---|---|---|---|
| 1 | `backend/agent.py:build_tools()` — last entry (`read_skill`) | Every tool definition (`web_search`, `code_execution`, `read_skill`) | Tool defs are fully stable across all turns — guaranteed hit on every request after the first |
| 2 | `backend/agent.py:build_system()` — second `system` block (skills catalog) | System instructions + alphabetically-sorted skills catalog | Largest stable chunk in the prefix; biggest single token win |
| 3 | `backend/agent.py:apply_message_cache_control()` — last block of last message | Entire conversation history up to (and including) the prior turn | Skipped on turn 1 (no prior history); kicks in turn 2+ |

### Things that would silently invalidate the cache (and how this code avoids them)

| Foot-gun | How it would break the cache | What we do |
|---|---|---|
| Timestamp / UUID in the system prompt | Prefix bytes differ on every request | System prompt is static |
| Skills loaded in non-deterministic order | Catalog bytes differ between requests | `render_skill_catalog` always sorts alphabetically |
| `json.dumps()` without `sort_keys` | Tool args / schemas serialize differently | We never feed serialized JSON into the prefix |
| Tool list changes mid-conversation | Position 1 byte differs → entire prefix invalid | Tools are built once per turn from a stable spec |
| `temperature` / `top_p` / `top_k` / `budget_tokens` | Would 400 on Opus 4.7 | We never set these |
| Assistant prefill on the last turn | Would 400 on Opus 4.6/4.7 | We never prefill |

### How to verify caching is working

Send a message, then send a follow-up. The second `usage` event will show `cache_read_input_tokens > 0`. The footer in the UI displays the running cache-hit percentage — that's the metric to watch.

You can also inspect the raw `usage` events by running the CLI harness twice with the same prompt:

```bash
python -m backend.agent_cli "Review this code: def f(): pass"
# second run reuses the cached system + tools
python -m backend.agent_cli "Review this code: def g(): pass"
```

---

## Skills

See `skills/README.md` for the authoring guide. The short version:

- Each skill is `skills/<name>/SKILL.md` with YAML frontmatter (`name`, `description`).
- Folder name must match `name`; both must match `^[a-z0-9][a-z0-9-]{0,63}$`.
- The agent sees only `name` + `description` upfront (in the system prompt). It loads the full body via `read_skill(name)` when a skill is relevant to the task.
- Malformed skills are logged and skipped — they will not crash the server.
- Hot-reload during development: `curl -X POST http://127.0.0.1:8000/api/skills/reload`.

The three included example skills (`code-reviewer`, `data-analyst`, `frontend-designer`) demonstrate three different skill shapes: one with a strict output rubric, one that mandates a specific tool (`code_execution`), one with anti-patterns and an optional propose-then-build flow.

---

## Anthropic API best practices applied

This project intentionally exercises the practices that matter for production work:

- **`claude-opus-4-7`** with adaptive thinking — `thinking={"type": "adaptive"}`, `output_config={"effort": "high"}`. No `temperature`, `top_p`, `top_k`, or `budget_tokens` — all four were removed on Opus 4.7 and would 400.
- **Streaming via `client.messages.stream()` + `get_final_message()`** — keeps long responses under SDK timeouts; per-token UI updates feel responsive.
- **Manual agentic loop** (rather than `messages.tool_runner`) — every `stop_reason` (`end_turn`, `tool_use`, `pause_turn`, `refusal`, `max_tokens`) is handled explicitly. See `backend/agent.py:run_turn`.
- **Thinking blocks preserved verbatim across tool turns** — the assistant `content` (including `thinking` blocks with their `signature` fields) is round-tripped untouched. Stripping or reordering would break adaptive thinking. Stored as JSON in SQLite to survive page refresh.
- **Three-marker cache strategy** — see above.
- **Typed exception handling** — `anthropic.RateLimitError`, `APIStatusError`, `APIConnectionError` mapped to `error` SSE events with sanitized messages (no API key leak).
- **Server-side tools** — `web_search_20260209` and `code_execution_20260120` declared but executed by Anthropic; we handle `pause_turn` to resume server-side loops.
- **Path-traversal defense** — `read_skill` validates `name` against `^[a-z0-9][a-z0-9-]{0,63}$` before any filesystem access.
- **Per-conversation concurrency lock** — second concurrent stream returns 409 instead of racing message ordering in SQLite.
- **Local-only by default** — binds to `127.0.0.1`, CORS scoped to localhost. The frontend never sees the API key.

---

## Project layout

```
LearnSkills/
├── README.md                # this file
├── .env.example
├── .gitignore
├── requirements.txt
├── backend/
│   ├── config.py            # pydantic-settings (env validation)
│   ├── skills.py            # SKILL.md parser + catalog renderer
│   ├── db.py                # async SQLite (conversations + messages as JSON)
│   ├── agent.py             # agentic loop, streaming, cache strategy ← READ THIS
│   ├── agent_cli.py         # CLI harness for verifying the loop
│   └── main.py              # FastAPI app, SSE endpoint
├── skills/
│   ├── README.md            # how to author a skill
│   ├── code-reviewer/SKILL.md
│   ├── data-analyst/SKILL.md
│   └── frontend-designer/SKILL.md
└── frontend/
    └── index.html           # single-file Tailwind + vanilla JS chat UI
```

---

## Future work

Things explicitly out of scope for the learning version, but useful real-world extensions:

- **Skill sub-files** — Anthropic's full Skills spec supports `reference/`, `scripts/`, `assets/` directories alongside `SKILL.md` for very large skills. This implementation loads only `SKILL.md`.
- **Sticky-disclosure hybrid** — once a skill has been invoked in a conversation, optionally cache its body in the system prompt for the rest of the conversation (avoids re-loading on every turn that reuses it).
- **Auto-titling** — currently uses the first 60 chars of the user's first message; a cheap secondary completion call could generate a better title.
- **Multi-user / auth** — single-user local app today.
- **File uploads / attachments** — for skills like `data-analyst` that should accept CSV uploads.
- **Hot-reload watcher** — `POST /api/skills/reload` exists, but a `watchdog`-based watcher would be nicer.
- **Token-cost dollar estimates** — multiply tokens by current pricing.
- **Export conversation as markdown / JSON.**
- **Hosted deploy** — would require auth, rate limiting, key rotation, and a managed DB.

---

## Troubleshooting

**`401 Unauthorized` from Anthropic** — `.env` not loaded, or `ANTHROPIC_API_KEY` is wrong. Check with `python -c "from backend.config import settings; print(settings.anthropic_api_key[:10])"`.

**`400` mentioning `temperature` or `budget_tokens`** — something is sneaking those parameters in. Grep `backend/` for them; they must not appear anywhere in API calls on Opus 4.7.

**Cache-hit % stays at 0** — likely a non-deterministic byte in the system prompt or skills catalog. Check that:
1. Skills load in the same order between runs.
2. No timestamp / UUID is interpolated into the system prompt.
3. The model hasn't been switched mid-session.

**Two browser tabs of the same conversation conflict (409)** — that's by design. Per-conversation lock prevents racing on message ordering. Send from one tab at a time.

**A skill change isn't picked up** — restart the server, or `curl -X POST http://127.0.0.1:8000/api/skills/reload`.
