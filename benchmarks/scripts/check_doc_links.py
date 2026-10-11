#!/usr/bin/env python3
"""Fail if a relative link or `#anchor` in a tracked Markdown file points nowhere.

Docs move; links do not follow on their own. This is the check that says a
move is finished. Only repo-relative links are checked (external URLs are not
fetched), and `benchmarks/results/**/comparison.md` is skipped: those are
immutable archives whose absolute GitHub URLs predate later moves.

    python3 benchmarks/scripts/check_doc_links.py
"""
import posixpath
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LINK = re.compile(r"\]\(([^)\s]+)\)")
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")


def slug(heading: str) -> str:
    """GitHub's anchor slug: lowercase, drop punctuation, spaces to hyphens."""
    h = re.sub(r"[`*_]|<[^>]+>", "", heading).strip().lower()
    h = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", h)
    h = re.sub(r"[^\w\- ]", "", h)
    return h.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    out: set[str] = set()
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            fenced = not fenced
        if fenced:
            continue
        m = HEADING.match(line)
        if m:
            s = slug(m.group(1))
            n = seen.get(s, 0)
            seen[s] = n + 1
            out.add(s if n == 0 else f"{s}-{n}")
    return out


def main() -> int:
    tracked = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
        cwd=REPO, text=True,
    ).split()
    tracked = sorted(r for r in set(tracked) if (REPO / r).is_file())
    cache: dict[Path, set[str]] = {}
    bad = []
    for rel in tracked:
        if rel.startswith("benchmarks/results/") and rel.endswith("comparison.md"):
            continue
        fenced = False
        for no, line in enumerate((REPO / rel).read_text(encoding="utf-8").splitlines(), 1):
            if FENCE.match(line):
                fenced = not fenced
            if fenced:
                continue
            for target in LINK.findall(line):
                if re.match(r"[a-z][a-z0-9+.-]*:", target) or target.startswith("/"):
                    continue
                path, _, frag = target.partition("#")
                dest = (REPO / posixpath.dirname(rel) / path) if path else REPO / rel
                dest = Path(posixpath.normpath(dest))
                if not dest.exists():
                    bad.append(f"{rel}:{no}: missing {target}")
                elif frag and dest.suffix == ".md" and dest.is_file():
                    if dest not in cache:
                        cache[dest] = anchors(dest)
                    if frag.lower() not in cache[dest]:
                        bad.append(f"{rel}:{no}: no anchor #{frag} in {path or rel}")
    for b in bad:
        print(b)
    print(f"{len(tracked)} Markdown files, {len(bad)} broken link(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
