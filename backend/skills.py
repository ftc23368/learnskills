"""Skills loader.

Anthropic Skills convention: each skill is a folder with a SKILL.md whose
YAML frontmatter defines `name` and `description`. The body of SKILL.md is the
detailed instruction set, loaded on demand via the `read_skill` tool — keeping
the system prompt small and the cache prefix stable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

logger = logging.getLogger(__name__)

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DESCRIPTION_MAX = 1024
BODY_WARN_BYTES = 50 * 1024  # 50 KB


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    path: Path


def _parse_skill_md(path: Path) -> tuple[dict, str] | None:
    """Split a SKILL.md into (frontmatter dict, body string).

    Returns None on parse error. Logs and skips rather than raising — a single
    broken skill should never crash the server at startup.
    """
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        logger.warning("Skipping empty skill file: %s", path)
        return None

    if not text.startswith("---"):
        logger.warning("Skipping skill missing YAML frontmatter: %s", path)
        return None

    # Split on the second '---' that closes the frontmatter
    parts = text.split("---", 2)
    if len(parts) < 3:
        logger.warning("Skipping skill with malformed frontmatter delimiter: %s", path)
        return None

    raw_frontmatter, body = parts[1], parts[2]
    try:
        frontmatter = yaml.safe_load(raw_frontmatter) or {}
    except yaml.YAMLError as exc:
        logger.warning("Skipping skill with invalid YAML frontmatter (%s): %s", exc, path)
        return None

    if not isinstance(frontmatter, dict):
        logger.warning("Skipping skill: frontmatter must be a mapping: %s", path)
        return None

    return frontmatter, body.lstrip("\n")


def _validate_skill(
    frontmatter: dict, body: str, dir_name: str, path: Path
) -> Skill | None:
    name = frontmatter.get("name")
    description = frontmatter.get("description")

    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        logger.warning("Skipping skill with invalid 'name' (%r): %s", name, path)
        return None

    if name != dir_name:
        logger.warning(
            "Skipping skill: frontmatter name %r does not match directory name %r (%s)",
            name,
            dir_name,
            path,
        )
        return None

    if not isinstance(description, str) or not description.strip():
        logger.warning("Skipping skill missing non-empty 'description': %s", path)
        return None

    if len(description) > DESCRIPTION_MAX:
        logger.warning(
            "Truncating long description (%d chars) for skill %s",
            len(description),
            name,
        )
        description = description[:DESCRIPTION_MAX].rstrip() + "…"

    body_size = len(body.encode("utf-8"))
    if body_size > BODY_WARN_BYTES:
        logger.warning(
            "Skill %s body is %d bytes (>%d). Consider splitting.",
            name,
            body_size,
            BODY_WARN_BYTES,
        )

    return Skill(name=name, description=description.strip(), body=body, path=path)


def load_skills(skills_dir: str | Path) -> dict[str, Skill]:
    """Load all skills from `<skills_dir>/<name>/SKILL.md`.

    Malformed skills are logged and skipped, never raised.
    """
    root = Path(skills_dir).resolve()
    if not root.is_dir():
        logger.warning("Skills directory does not exist: %s", root)
        return {}

    skills: dict[str, Skill] = {}
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file():
            logger.debug("No SKILL.md in %s; skipping", child)
            continue

        parsed = _parse_skill_md(skill_md)
        if parsed is None:
            continue

        frontmatter, body = parsed
        skill = _validate_skill(frontmatter, body, child.name, skill_md)
        if skill is None:
            continue

        if skill.name in skills:
            logger.warning("Duplicate skill name %r; keeping first occurrence", skill.name)
            continue

        skills[skill.name] = skill

    logger.info("Loaded %d skill(s): %s", len(skills), sorted(skills))
    return skills


def render_skill_catalog(skills: dict[str, Skill] | Iterable[Skill]) -> str:
    """Render a deterministic, alphabetically-sorted markdown catalog.

    Determinism matters: any byte change to this string invalidates the
    prompt cache. Always sort by name.
    """
    if isinstance(skills, dict):
        items = sorted(skills.values(), key=lambda s: s.name)
    else:
        items = sorted(skills, key=lambda s: s.name)

    if not items:
        return "_No skills available._"

    lines = [f"- **{s.name}**: {s.description}" for s in items]
    return "\n".join(lines)


def get_skill_body(name: str, skills: dict[str, Skill]) -> str:
    """Return the body of a skill by name. Re-validates against the regex
    even though `name` came from the model, as a defense-in-depth check
    against any future code path that might call this with untrusted input.
    """
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise ValueError(f"Invalid skill name: {name!r}")

    skill = skills.get(name)
    if skill is None:
        available = ", ".join(sorted(skills)) or "(none)"
        raise KeyError(f"Skill {name!r} not found. Available: {available}")

    return skill.body
