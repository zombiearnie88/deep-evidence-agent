"""Structural lint checks for Evidence Brain wiki taxonomy."""

from __future__ import annotations

import re
from pathlib import Path

WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
EXCLUDED_FILES = {"AGENTS.md", "log.md"}
INDEXED_DIRS = {
    "summaries",
    "topics",
    "regulations",
    "procedures",
    "conflicts",
    "evidence",
}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _all_pages(wiki: Path) -> dict[str, Path]:
    pages: dict[str, Path] = {}
    for md in wiki.rglob("*.md"):
        rel = md.relative_to(wiki)
        key = str(rel.with_suffix("")).replace("\\", "/")
        pages[key] = md
        pages[md.stem] = md
    return pages


def _extract_wikilinks(text: str) -> list[str]:
    raw = WIKILINK_RE.findall(text)
    return [item.split("|")[0].strip() for item in raw]


def find_broken_links(wiki: Path) -> list[str]:
    pages = _all_pages(wiki)
    errors: list[str] = []

    for md in wiki.rglob("*.md"):
        if md.name in EXCLUDED_FILES:
            continue
        rel_parts = md.relative_to(wiki).parts
        if rel_parts and rel_parts[0] == "sources":
            continue
        text = _read_text(md)
        for link in _extract_wikilinks(text):
            normalized = link.strip().strip("/")
            if normalized and normalized not in pages:
                errors.append(f"Broken link [[{link}]] in {md.relative_to(wiki)}")

    return sorted(errors)


def find_orphans(wiki: Path) -> list[str]:
    pages = [
        path
        for path in wiki.rglob("*.md")
        if path.name not in {"index.md", *EXCLUDED_FILES}
        and "sources" not in path.relative_to(wiki).parts
    ]
    if not pages:
        return []

    outgoing: dict[str, set[str]] = {}
    for path in pages:
        rel = str(path.relative_to(wiki).with_suffix("")).replace("\\", "/")
        outgoing[rel] = set(_extract_wikilinks(_read_text(path)))

    incoming: set[str] = set()
    for links in outgoing.values():
        for target in links:
            normalized = target.strip().strip("/")
            if normalized:
                incoming.add(normalized)
                incoming.add(Path(normalized).stem)

    orphans: list[str] = []
    for rel, links in outgoing.items():
        stem = Path(rel).stem
        if rel not in incoming and stem not in incoming and not links:
            orphans.append(rel)
    return sorted(orphans)


def find_missing_entries(raw: Path, wiki: Path) -> list[str]:
    known: set[str] = set()
    for folder in INDEXED_DIRS:
        page_dir = wiki / folder
        if not page_dir.exists():
            continue
        known.update(path.stem for path in page_dir.glob("*.md"))

    missing: list[str] = []
    if raw.exists():
        for path in raw.iterdir():
            if path.is_file() and path.stem not in known:
                missing.append(path.name)
    return sorted(missing)


def check_index_sync(wiki: Path) -> list[str]:
    index_path = wiki / "index.md"
    if not index_path.exists():
        return ["index.md does not exist"]
    issues: list[str] = []
    text = _read_text(index_path)
    links = set(_extract_wikilinks(text))
    pages = _all_pages(wiki)

    for link in links:
        normalized = link.strip().strip("/")
        if normalized and normalized not in pages:
            issues.append(f"index.md links to missing page: [[{link}]]")

    index_stems = {Path(link).stem for link in links}
    index_lower = text.lower()
    for folder in INDEXED_DIRS:
        page_dir = wiki / folder
        if not page_dir.exists():
            continue
        for page in sorted(page_dir.glob("*.md")):
            stem = page.stem
            if stem not in index_stems and stem.lower() not in index_lower:
                issues.append(f"{folder}/{stem}.md not mentioned in index.md")

    return sorted(issues)


def run_structural_lint(workspace: Path) -> str:
    wiki = workspace / "wiki"
    raw = workspace / "raw"
    broken = find_broken_links(wiki)
    orphans = find_orphans(wiki)
    missing = find_missing_entries(raw, wiki)
    sync = check_index_sync(wiki)

    lines = ["## Structural Lint Report", ""]
    sections: list[tuple[str, list[str], str]] = [
        ("Broken Links", broken, "No broken links found."),
        ("Orphaned Pages", orphans, "No orphaned pages found."),
        ("Raw Files Without Wiki Entry", missing, "All raw files have wiki entries."),
        ("Index Sync Issues", sync, "Index is in sync."),
    ]

    for title, items, ok_message in sections:
        lines.append(f"### {title} ({len(items)})")
        if items:
            lines.extend(f"- {item}" for item in items)
        else:
            lines.append(ok_message)
        lines.append("")

    return "\n".join(lines).strip() + "\n"
