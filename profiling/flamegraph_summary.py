#!/usr/bin/env python3
"""Self and inclusive time per function, read back out of flamegraph SVGs.

`perf report --sort symbol` splits one function into several rows when LTO
emits it under different hashes, and it cannot be re-derived once perf.data is
deleted (it is gigabytes). The SVG keeps the whole call tree as rectangles, so
this recomputes exclusive ("self") time from their geometry: a frame's self
time is its width minus the widths of the frames drawn directly above it.

    python3 profiling/flamegraph_summary.py DIR [--top 12] [--out summary.md]

DIR holds `<bench>.svg` files, as written by `flamegraph_suite.sh`.
Names are normalised (rustc `$LT$`-style escapes decoded, `::h<hash>` dropped)
so the same function under two hashes is one row. Kernel frames without a
symbol are folded into `[kernel]`.
"""
import argparse
import html
import re
import sys
from collections import defaultdict
from pathlib import Path

RECT = re.compile(
    r'<g><title>(?P<title>[^<]*)</title><rect [^>]*?y="(?P<y>[\d.]+)"[^>]*?'
    r'fg:x="(?P<x>\d+)" fg:w="(?P<w>\d+)"'
)
UNESC = {"$u7b$": "{", "$u7d$": "}", "$LT$": "<", "$GT$": ">", "$C$": ",",
         "$u20$": " ", "$RF$": "&", "$BP$": "*", "$SP$": "@"}


def norm(name: str) -> str:
    name = html.unescape(re.sub(r" \([\d,]+ samples, [\d.]+%\)$", "", name))
    for k, v in UNESC.items():
        name = name.replace(k, v)
    name = re.sub(r"::h[0-9a-f]{16}$", "", name)
    if re.match(r"^(\[unknown\]|0xffff[0-9a-f]+)", name):
        return "[kernel]"
    return name


def frames(svg: str):
    out = []
    for m in RECT.finditer(svg):
        out.append((float(m["y"]), int(m["x"]), int(m["w"]), norm(m["title"])))
    return out


def analyse(path: Path):
    fr = frames(path.read_text(encoding="utf-8"))
    if not fr:
        return None
    ys = sorted({f[0] for f in fr}, reverse=True)  # root first (largest y)
    level = {y: i for i, y in enumerate(ys)}
    by_level = defaultdict(list)
    for y, x, w, n in fr:
        by_level[level[y]].append((x, w, n))
    total = max(w for _, _, w, _ in fr)
    self_t, incl = defaultdict(int), defaultdict(int)
    for lv, items in by_level.items():
        kids = sorted(by_level.get(lv + 1, []))
        ki = 0
        for x, w, n in sorted(items):
            used = 0
            while ki < len(kids) and kids[ki][0] < x:
                ki += 1
            j = ki
            while j < len(kids) and kids[j][0] < x + w:
                used += kids[j][1]
                j += 1
            self_t[n] += w - used
            incl[n] += w  # recursion would double-count; this code has none that matters
    return total, self_t, incl


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", type=Path)
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    lines = []
    for svg in sorted(a.dir.glob("B*.svg")):
        r = analyse(svg)
        if r is None:
            continue
        total, self_t, _ = r
        lines.append(f"### {svg.stem}\n")
        lines.append("| self % | function |\n|---:|---|")
        for n, t in sorted(self_t.items(), key=lambda kv: -kv[1])[: a.top]:
            short = n if len(n) <= 110 else n[:107] + "..."
            lines.append(f"| {100 * t / total:5.1f} | `{short}` |")
        lines.append("")
    text = "\n".join(lines)
    (a.out.write_text(text) if a.out else sys.stdout.write(text + "\n"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
