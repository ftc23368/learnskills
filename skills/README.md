# Skills

Each skill is a folder containing a `SKILL.md` file. The agent loads only the *names* and *descriptions* of all skills into its system prompt at startup. The full body of a skill is loaded on demand via the `read_skill` tool — this is **progressive disclosure**, the canonical Anthropic Skills pattern.

## Anatomy of a skill

```
skills/
└── my-skill/
    └── SKILL.md
```

`SKILL.md` has YAML frontmatter followed by the body:

```markdown
---
name: my-skill
description: A short, action-oriented description that helps the agent decide when to use this skill. Should describe the task domain and the trigger condition. ≤ 1024 chars.
---

# My Skill

Detailed instructions, process, examples, output format, what NOT to do.
This body is loaded by `read_skill("my-skill")` only when the agent
decides this skill is relevant.
```

## Naming rules

- Folder name and `name` in frontmatter **must match**.
- Names use lowercase letters, digits, and hyphens only: `^[a-z0-9][a-z0-9-]{0,63}$`.
- A malformed skill is logged at startup and skipped — it will not crash the server.

## Writing good skill descriptions

The description sits in the system prompt on **every request**, so:

- Keep it short (one or two sentences).
- Lead with the verb: "Reviews code...", "Analyzes CSV data...", "Designs frontend interfaces..."
- State the trigger explicitly: "Use when the user pastes code and asks for a review."
- Avoid marketing language. The audience is the model deciding whether to call `read_skill`.

## Writing good skill bodies

The body is loaded only when the skill is invoked, so it can be longer (target ≤ 50 KB). Useful sections:

- **Process**: a numbered list of how to approach the task
- **Output format**: exactly what the response should look like
- **What NOT to do**: failure modes you want to head off
- **Examples**: zero-shot or few-shot demonstrations

## Testing a skill

After adding or editing a skill:

1. Restart the server (or hit `POST /api/skills/reload` for a hot reload during development).
2. Ask the agent something the skill should handle.
3. Watch for the `📖 Reading skill: <name>` chip in the UI — that confirms the agent chose to load it.

## Future work (not yet supported)

Anthropic's full Skills spec also supports skill sub-files (`reference/`, `scripts/`, `assets/`) that the model can load alongside `SKILL.md` for very large skills. This minimal implementation only loads `SKILL.md`. See the project README for the roadmap.
