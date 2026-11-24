"""Validate token-conscious repository documentation and local links."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def markdown_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not any(part in SKIP_PARTS for part in path.relative_to(ROOT).parts)
    )


def has_exact_case(path: Path) -> bool:
    """Return whether every repository-relative path component matches disk casing."""
    current = ROOT
    for part in path.relative_to(ROOT).parts:
        names = {child.name for child in current.iterdir()}
        if part not in names:
            return False
        current /= part
    return True


def check_markdown(path: Path) -> list[str]:
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")
    relative = path.relative_to(ROOT)

    if "file:///" in text.lower():
        errors.append(f"{relative}: use a repository-relative link, not file:///")

    for raw_target in LINK_RE.findall(text):
        target = raw_target.strip().strip("<>")
        if not target or target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        target = unquote(target.split("#", 1)[0])
        if not target:
            continue
        resolved = (path.parent / target).resolve()
        try:
            resolved.relative_to(ROOT)
        except ValueError:
            errors.append(f"{relative}: link escapes repository: {raw_target}")
            continue
        if not resolved.exists():
            errors.append(f"{relative}: missing local link target: {raw_target}")
        elif not has_exact_case(resolved):
            errors.append(f"{relative}: local link has incorrect path casing: {raw_target}")
    return errors


def parse_frontmatter(path: Path) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        return {}, [f"{path.relative_to(ROOT)}: missing YAML frontmatter"]
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}, [f"{path.relative_to(ROOT)}: unterminated YAML frontmatter"]

    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            errors.append(f"{path.relative_to(ROOT)}: invalid frontmatter line: {line}")
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    return metadata, errors


def check_skills() -> list[str]:
    errors: list[str] = []
    skill_root = ROOT / ".skills"
    for path in sorted(skill_root.glob("*/SKILL.md")):
        metadata, parse_errors = parse_frontmatter(path)
        errors.extend(parse_errors)
        if set(metadata) != {"name", "description"}:
            errors.append(
                f"{path.relative_to(ROOT)}: frontmatter must contain only name and description"
            )
        name = metadata.get("name", "")
        if name != path.parent.name or not NAME_RE.fullmatch(name):
            errors.append(f"{path.relative_to(ROOT)}: skill name must match its kebab-case folder")
        if not metadata.get("description"):
            errors.append(f"{path.relative_to(ROOT)}: description is required")
        if len(path.read_text(encoding="utf-8").splitlines()) > 120:
            errors.append(f"{path.relative_to(ROOT)}: keep SKILL.md at or below 120 lines")

    legacy = sorted(skill_root.glob("skill-*.md"))
    for path in legacy:
        errors.append(f"{path.relative_to(ROOT)}: move legacy flat skill to <name>/SKILL.md")
    return errors


def main() -> int:
    errors: list[str] = []
    agents = ROOT / "AGENTS.md"
    if agents.stat().st_size > 6_000:
        errors.append("AGENTS.md: exceeds the 6 KB always-loaded context budget")

    for path in markdown_files():
        errors.extend(check_markdown(path))
    errors.extend(check_skills())

    if errors:
        print("Documentation checks failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"Documentation checks passed ({len(markdown_files())} Markdown files).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
