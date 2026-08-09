#!/usr/bin/env python3
"""Aggregate every architecture's tables and charts into one document.

  crossarch.py                 build paper/generated/cross-arch/
  crossarch.py --check         exit 1 if any artifact would change
  crossarch.py --list          each artifact's inputs and transformation
  crossarch.py --pdf           build the artifacts and typeset them

---------------------------------------------------------------------------
The chain
---------------------------------------------------------------------------
    shmap -x  ->  run.py  ->  results.tsv + profiles.tsv     (per architecture)
                                     |
                     charts.py  ->  chart-*.svg              (per architecture)
                     paper.py   ->  paper/generated/<arch>/  (per architecture)
                                     |
                     crossarch.py -> paper/generated/cross-arch/
                                     |
                     build_pdf.py -> cross-arch/artifacts.pdf

Everything upstream of this script describes one machine. `paper.py` reads a
single result set by construction, and `compare.py` refuses outright to diff
two sets measured on different hosts. That refusal is right for a regression
gate and wrong for a paper: the whole point of running on two machines is to
put them next to each other. This script is the one place allowed to, and it
carries the caveat that makes doing so honest.

---------------------------------------------------------------------------
What "comparable" does and does not mean here
---------------------------------------------------------------------------
The two hosts are not a controlled experiment. They differ in core count,
socket count, NUMA topology, memory and clock, and nothing here holds any of
that fixed. A ratio in the headline table is the answer to "how long did this
take on each machine we own", not "how much faster is this ISA".

What *is* a controlled comparison is the agreement table: the algorithm is
deterministic, so every counter -- reads mapped, mapq-60 calls, buckets
seeded, refined and reported -- must be identical on both machines for the
same commit. Those columns are not performance figures at all. They are the
evidence that the two performance stories describe the same computation, and
if they ever diverge the rest of the document is void. The table says so, and
`--check` fails rather than typesetting a document whose premise is broken.

Result sets are read from each architecture's `current/`, so this describes
exactly what is promoted -- never a staging directory.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import load_suite  # noqa: E402
from compare import load_set  # noqa: E402
from layout import HOSTS_TOML, REPO, available_arches, current_dir  # noqa: E402
from charts import read_profiles, time_slices, time_stages  # noqa: E402
from paper import Artifact, dash, fnum, tex_escape  # noqa: E402

# A sibling of the per-architecture directories rather than a child of one,
# because it belongs to no single architecture. The hyphen keeps it clearly
# apart from a real `uname -m` value, none of which contain one -- so this
# cannot be mistaken for, or collide with, an architecture we start running on.
OUT = REPO / "paper" / "generated" / "cross-arch"

SUBJECT = "shmap-rs"
METRIC_ORDER = ["Containment", "Jaccard", "bucket_SH"]

# One benchmark carries the two figures. B01 is D1-HIFI23K, the real-HiFi read
# set RESULTS.md argues thread scaling on, so the figures line up with the
# existing narrative instead of starting a second one on a different dataset.
# Every benchmark is still covered by the tables and by charts.html.
FOCUS_BENCHMARK = "B01"

# Charts are drawn for these throughout the project; the cross-architecture
# pies match so they can be read against the per-architecture ones.
CHART_METRIC = "Containment"
CHART_THREADS = "1"

# Colour carries the metric, line style carries the architecture. Two visual
# channels for two variables means the figure survives greyscale printing,
# where six same-coloured curves would not.
METRIC_STYLE = {
    "Containment": ("blue!70!black", "*"),
    "Jaccard": ("red!70!black", "square*"),
    "bucket_SH": ("green!45!black", "triangle*"),
}
ARCH_DASH = ["solid", "densely dashed", "dotted", "dashdotted"]

# Counters that must be bit-identical across architectures for the same
# commit. (column in profiles.tsv, heading, what a difference would mean)
INVARIANTS = [
    ("n_mapped_reads", "mapped", "a read mapped on one machine and not the other"),
    ("n_mapq60", "mapq 60", "a mapping was called confident on one machine only"),
    ("n_seeded_buckets", "seeded", "seeding produced different candidates"),
    ("n_refined_buckets", "refined", "the seed heuristic pruned differently"),
    ("n_final_buckets", "reported", "a different set of mappings was reported"),
]


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

@dataclass
class ArchSet:
    """One architecture's promoted result set, plus who produced it."""
    arch: str
    rs: dict
    profiles: list[dict]

    @property
    def host(self) -> str:
        return str(self.rs["manifest"].get("host", "?"))

    @property
    def commit(self) -> str:
        return str(self.rs["manifest"].get("commit", "?"))[:12]

    @property
    def measured(self) -> str:
        return str(self.rs["manifest"].get("finished", "?"))[:10]

    @property
    def rustc(self) -> str:
        # Recorded since the multi-host work; older sets predate it and get an
        # em-dash rather than a guess, because "which compiler" is exactly the
        # question a reader asks when two runs of one commit differ.
        return str(self.rs["manifest"].get("rustc") or "")

    def row(self, bid: str, metric: str, threads: int, impl: str = SUBJECT) -> dict | None:
        for r in self.rs["rows"]:
            if (r["benchmark"] == bid and r["metric"] == metric
                    and r["threads"] == threads and r["impl"] == impl):
                return r
        return None

    def profile(self, bid: str, metric: str = CHART_METRIC,
                threads: str = CHART_THREADS) -> dict | None:
        for r in self.profiles:
            if (r["benchmark"] == bid and r["metric"] == metric
                    and r["threads"] == threads):
                return r
        return None


@dataclass
class XCtx:
    """Every architecture's data, loaded once.

    `sets` is ordered with the reference architecture first; every delta in
    the document is against `ref`, and the tables name it so no reader has to
    infer the direction of a difference.
    """
    sets: list[ArchSet]
    hosts: dict
    digest: str

    @property
    def ref(self) -> ArchSet:
        return self.sets[0]

    @property
    def others(self) -> list[ArchSet]:
        return self.sets[1:]

    def benchmarks(self) -> list[str]:
        """Benchmarks measured on *every* architecture.

        The intersection, not the union: a row present on one machine only is
        not a comparison, and padding it with em-dashes in a table headed
        "cross-architecture" invites exactly the misreading this document
        exists to prevent.
        """
        common: set[str] | None = None
        for s in self.sets:
            here = {r["benchmark"] for r in s.rs["rows"] if r["impl"] == SUBJECT}
            common = here if common is None else (common & here)
        return sorted(common or set())

    def metrics(self, bid: str) -> list[str]:
        common: set[str] | None = None
        for s in self.sets:
            here = {r["metric"] for r in s.rs["rows"]
                    if r["impl"] == SUBJECT and r["benchmark"] == bid}
            common = here if common is None else (common & here)
        return [m for m in METRIC_ORDER if m in (common or set())]

    def thread_counts(self, bid: str, metric: str) -> list[int]:
        common: set[int] | None = None
        for s in self.sets:
            here = {r["threads"] for r in s.rs["rows"]
                    if r["impl"] == SUBJECT and r["benchmark"] == bid
                    and r["metric"] == metric}
            common = here if common is None else (common & here)
        return sorted(common or set())

    def focus(self) -> str | None:
        bs = self.benchmarks()
        if not bs:
            return None
        return FOCUS_BENCHMARK if FOCUS_BENCHMARK in bs else bs[0]

    def host_facts(self, host: str) -> dict:
        return self.hosts.get(host, {})


def load_hosts() -> dict:
    """hosts.toml, for the machine facts no result set records.

    Cores and topology are operational configuration, not measurements, so
    they live there rather than in a manifest. Missing file is survivable:
    the machines table degrades to what the manifests know.
    """
    if not HOSTS_TOML.exists():
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        return {}
    try:
        doc = tomllib.loads(HOSTS_TOML.read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in doc.items() if isinstance(v, dict)}


def load_sets(suite_version: str, arches: list[str] | None = None) -> list[ArchSet]:
    out = []
    for a in (arches or available_arches(suite_version)):
        d = current_dir(suite_version, a)
        if not d.is_dir():
            continue
        out.append(ArchSet(arch=a, rs=load_set(d), profiles=read_profiles(d)))
    return out


def input_digest(sets: list[ArchSet]) -> str:
    """sha256 over every file this document reads, so a draft can be tied to
    an exact pair of measurements even after the sets are moved or renamed."""
    h = hashlib.sha256()
    for s in sets:
        h.update(s.arch.encode())
        for name in ("results.tsv", "profiles.tsv", "manifest.json"):
            p = s.rs["dir"] / name
            if p.exists():
                h.update(name.encode())
                h.update(p.read_bytes())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def tabular(colspec: str, head: list[str], body: list[list[str]],
            group_every: Callable[[int], bool] | None = None) -> str:
    lines = [r"\begin{tabular}{" + colspec + "}", r"\toprule",
             " & ".join(head) + r" \\", r"\midrule"]
    for i, row in enumerate(body):
        if group_every and i and group_every(i):
            lines.append(r"\midrule")
        lines.append(" & ".join(row) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines)


def pct(x: float) -> str:
    return f"{x:.1f}\\%"


def signed(x: float, places: int = 1) -> str:
    """A signed delta, with a real minus sign and a zero that is not '-0.0'."""
    if abs(x) < 0.5 * 10 ** -places:
        x = 0.0
    return f"{x:+.{places}f}"


def stage_shares(s: ArchSet, bid: str) -> tuple[dict[str, float], float]:
    """Every stage's share of the run's CPU time.

    Returns (label -> share of total, total CPU-seconds), from
    `charts.time_stages` — the unfolded pipeline, not the pies' folded
    wedges. Folding is a rule for pies: a wedge under 5% cannot be drawn
    honestly. Applied to a table it does real damage, because the fold's
    label depends on how many stages it swallowed, so three benchmarks
    produce three different 'other' rows and any stage that is always small
    disappears from the table altogether.
    """
    row = s.profile(bid)
    if not row:
        return {}, 0.0
    idx, mp = time_stages(row)
    total = sum(v for _, v, _ in idx + mp)
    if total <= 0:
        return {}, 0.0
    return {lab: v / total for lab, v, _ in idx + mp}, total


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------

def build_machines(c: XCtx) -> tuple[str, list[str], list[list]]:
    """Who measured what, so every later number has a machine attached."""
    cols = ["architecture", "host", "cores", "sockets_numa", "commit", "rustc",
            "measured", "result_set"]
    head = [r"Architecture", r"Host", r"Cores", r"Topology", r"Commit",
            r"\texttt{rustc}", r"Measured", r"Result set"]
    body, data = [], []
    for s in c.sets:
        f = c.host_facts(s.host)
        cores = str(f.get("cores", "")) or dash()
        topo = str(f.get("topology", "")) or dash()
        rustc = s.rustc or dash()
        body.append([
            r"\texttt{" + tex_escape(s.arch) + "}",
            r"\texttt{" + tex_escape(s.host) + "}",
            cores,
            tex_escape(topo),
            r"\texttt{" + tex_escape(s.commit) + "}",
            tex_escape(rustc),
            s.measured,
            r"{\scriptsize\texttt{" + tex_escape(s.rs["dir"].parent.name + "/current") + "}}",
        ])
        data.append([s.arch, s.host, f.get("cores"), topo, s.commit,
                     s.rustc or None, s.measured, str(s.rs["dir"])])
    return tabular("llrlllll", head, body), cols, data


def build_agreement(c: XCtx) -> tuple[str, list[str], list[list]]:
    """The premise check: same commit, same computation, both machines.

    Reported as agree/differ rather than as five pairs of large integers,
    because the only question a reader has is whether any of them moved.
    """
    cols = ["benchmark", "metric"] + [n for _, n, _ in INVARIANTS] + ["verdict"]
    head = [r"Benchmark", r"Metric"] + [tex_escape(n) for _, n, _ in INVARIANTS] + [r"Verdict"]
    body, data = [], []
    for bid in c.benchmarks():
        for metric in c.metrics(bid):
            cells, raw, ok = [], [], True
            ref_row = c.ref.profile(bid, metric)
            for col, _, _ in INVARIANTS:
                ref_v = _int(ref_row, col)
                deltas = [_int(s.profile(bid, metric), col) for s in c.others]
                if ref_v is None or any(v is None for v in deltas):
                    # Not measured on one side. An em-dash, never "agree":
                    # a missing counter is the absence of evidence, and this
                    # table's whole job is to be evidence.
                    cells.append(dash())
                    raw.append(None)
                    continue
                moved = [v - ref_v for v in deltas if v != ref_v]
                if not moved:
                    cells.append(r"$=$")
                    raw.append("=")
                else:
                    cells.append(r"\textbf{" + f"{moved[0]:+d}" + "}")
                    raw.append(moved[0])
                    ok = False
            verdict = r"agree" if ok else r"\textbf{DIFFER}"
            body.append([tex_escape(bid), tex_escape(metric), *cells, verdict])
            data.append([bid, metric, *raw, "agree" if ok else "differ"])
    return (tabular("ll" + "c" * len(INVARIANTS) + "l", head, body,
                    group_every=lambda i: body[i][0] != body[i - 1][0]),
            cols, data)


def _int(row: dict | None, col: str) -> int | None:
    if not row:
        return None
    v = row.get(col)
    if v in (None, ""):
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def build_headline(c: XCtx) -> tuple[str, list[str], list[list]]:
    """Indexing, mapping and memory on each machine, at the like-for-like -@1."""
    ref = c.ref
    cols = ["benchmark", "metric"]
    for s in c.sets:
        cols += [f"index_s_{s.arch}", f"map_s_{s.arch}", f"peak_rss_gb_{s.arch}"]
    for s in c.others:
        cols.append(f"map_s_ratio_{s.arch}_over_{ref.arch}")

    head = [r"Benchmark", r"Metric"]
    for _ in c.sets:
        head += [r"index (s)", r"map (s)", r"RSS (GB)"]
    for _ in c.others:
        head.append(r"map $\times$")

    body, data = [], []
    for bid in c.benchmarks():
        for metric in c.metrics(bid):
            cells, raw = [tex_escape(bid), tex_escape(metric)], [bid, metric]
            ref_map = None
            for s in c.sets:
                r = s.row(bid, metric, 1)
                if not r:
                    cells += [dash(), dash(), dash()]
                    raw += [None, None, None]
                    continue
                idx = r.get("index_s")
                mp = r.get("map_s")
                gb = float(r["peak_rss_kb"]) / 1048576
                if s is ref:
                    ref_map = mp
                cells += [fnum(idx) if idx is not None else dash(),
                          fnum(mp) if mp is not None else dash(),
                          fnum(gb)]
                raw += [round(idx, 2) if idx is not None else None,
                        round(mp, 2) if mp is not None else None,
                        round(gb, 3)]
            for s in c.others:
                r = s.row(bid, metric, 1)
                mp = r.get("map_s") if r else None
                if mp and ref_map:
                    cells.append(fnum(mp / ref_map))
                    raw.append(round(mp / ref_map, 2))
                else:
                    cells.append(dash())
                    raw.append(None)
            body.append(cells)
            data.append(raw)

    spec = "ll" + "rrr" * len(c.sets) + "r" * len(c.others)
    # A header band naming the architecture over each block of three, so the
    # columns are readable without counting across to the caption.
    band = ["", ""]
    for s in c.sets:
        band.append(r"\multicolumn{3}{c}{\texttt{" + tex_escape(s.arch) + "}}")
    for s in c.others:
        band.append(r"{\scriptsize " + tex_escape(s.arch) + "/" + tex_escape(ref.arch) + "}")
    lines = [r"\begin{tabular}{" + spec + "}", r"\toprule",
             " & ".join(band) + r" \\",
             " & ".join(head) + r" \\", r"\midrule"]
    for i, row in enumerate(body):
        if i and row[0] != body[i - 1][0]:
            lines.append(r"\midrule")
        lines.append(" & ".join(row) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines), cols, data


def build_stages(c: XCtx) -> tuple[str, list[str], list[list]]:
    """Does the shape of the run change with the machine?

    One row per stage: its share on the reference, then how many percentage
    points that share moves on each other architecture, per benchmark. Shares
    rather than seconds, because seconds differ for the uninteresting reason
    that the machines differ in speed; a shift in *share* is the interesting
    one -- it means a stage got relatively cheaper or dearer.
    """
    bench = c.benchmarks()
    ref_shares = {b: stage_shares(c.ref, b)[0] for b in bench}
    labels: list[str] = []
    for b in bench:
        for lab in ref_shares[b]:
            if lab not in labels:
                labels.append(lab)

    cols = ["stage", f"share_{c.ref.arch}_mean_pct"]
    for s in c.others:
        cols += [f"delta_pp_{s.arch}_{b}" for b in bench]
    head = [r"Stage", r"\texttt{" + tex_escape(c.ref.arch) + "} share"]
    for s in c.others:
        head += [tex_escape(b) for b in bench]

    # Hoisted: recomputing these inside the label loop would re-read every
    # profile once per stage.
    other_shares = {s.arch: {b: stage_shares(s, b)[0] for b in bench} for s in c.others}

    body, data = [], []
    for lab in labels:
        present = [ref_shares[b][lab] for b in bench if lab in ref_shares[b]]
        mean = 100 * sum(present) / len(present) if present else 0.0
        cells = [tex_escape(lab), pct(mean)]
        raw = [lab, round(mean, 1)]
        for s in c.others:
            other = other_shares[s.arch]
            for b in bench:
                if lab in ref_shares[b] and lab in other[b]:
                    d = 100 * (other[b][lab] - ref_shares[b][lab])
                    cells.append(signed(d))
                    # `+ 0.0` because round() preserves the sign of a negative
                    # zero, and a column of deltas reading "-0.0" invites the
                    # reader to look for a difference that is not there.
                    raw.append(round(d, 1) + 0.0)
                else:
                    cells.append(dash())
                    raw.append(None)
        body.append(cells)
        data.append(raw)

    spec = "lr" + "r" * (len(bench) * len(c.others))
    band = ["", ""]
    for s in c.others:
        band.append(r"\multicolumn{" + str(len(bench)) +
                    r"}{c}{$\Delta$ pp on \texttt{" + tex_escape(s.arch) + "}}")
    lines = [r"\begin{tabular}{" + spec + "}", r"\toprule",
             " & ".join(band) + r" \\",
             " & ".join(head) + r" \\", r"\midrule"]
    for row in body:
        lines.append(" & ".join(row) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines), cols, data


def build_thread_scaling(c: XCtx) -> tuple[str, list[str], list[list]]:
    """Speedup against thread count, every architecture on one axis.

    The figure the second machine was provisioned for. Each curve is measured
    against its *own* single-threaded time, so what is being compared is how
    well each machine scales, not how fast it is -- two different questions
    that share an axis if the curves are normalised to one baseline.
    """
    bid = c.focus()
    cols = ["arch", "benchmark", "metric", "threads", "wall_s", "speedup_vs_1"]
    data: list[list] = []
    body: list[str] = []
    if not bid:
        return "", cols, data

    ticks: list[int] = []
    for ai, s in enumerate(c.sets):
        dashstyle = ARCH_DASH[ai % len(ARCH_DASH)]
        for metric in c.metrics(bid):
            colour, mark = METRIC_STYLE.get(metric, ("black", "o"))
            threads = c.thread_counts(bid, metric)
            base = s.row(bid, metric, 1)
            if not base:
                continue
            pts = []
            for t in threads:
                r = s.row(bid, metric, t)
                if not r or not r["wall_s"]:
                    continue
                sp = base["wall_s"] / r["wall_s"]
                pts.append((t, sp))
                data.append([s.arch, bid, metric, t, round(r["wall_s"], 2), round(sp, 2)])
                if t not in ticks:
                    ticks.append(t)
            if pts:
                body.append(f"\\addplot[color={colour},mark={mark},{dashstyle},thick] "
                            "coordinates {" + " ".join(f"({t},{sp:.2f})" for t, sp in pts) + "};")
                body.append(r"\addlegendentry{" + tex_escape(f"{s.arch} · {metric}") + "}")

    ticks.sort()
    if ticks:
        body.append("\\addplot[dashed,gray,forget plot] coordinates {"
                    + " ".join(f"({t},{t})" for t in ticks) + "};")
    opts = ["xlabel={threads (\\texttt{-@})}", "ylabel={speedup vs \\texttt{-@1}}",
            "width=0.78\\linewidth", "height=7cm",
            "legend pos=north west", "legend cell align=left",
            "legend style={font=\\scriptsize}",
            "grid=major", "grid style={gray!25}", "tick align=outside",
            "xmode=log", "ymode=log", "log basis x=2", "log basis y=2",
            f"xtick={{{','.join(str(t) for t in ticks)}}}",
            "xticklabels={" + ",".join(str(t) for t in ticks) + "}"]
    tex = "\n".join([r"\begin{tikzpicture}", r"\begin{axis}[",
                     "  " + ",\n  ".join(opts), "]", *body,
                     r"\end{axis}", r"\end{tikzpicture}"])
    return tex, cols, data


# ---------------------------------------------------------------------------
# TikZ pies
# ---------------------------------------------------------------------------

def _ink(hexcolor: str) -> str:
    """Readable label colour for a wedge, by perceived luminance."""
    h = hexcolor.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return "black" if (0.299 * r + 0.587 * g + 0.114 * b) > 140 else "white"


def tikz_pie(slices: list[tuple[str, float, str]], cx: float, cy: float,
             radius: float, title: str, tag: str) -> list[str]:
    """One pie, in plain TikZ.

    Deliberately no `pie` package and no `calc` library: every coordinate is
    computed here and emitted as a literal, so the figure needs nothing the
    paper does not already load, and the geometry is testable in Python
    rather than only visible after a LaTeX run.
    """
    out: list[str] = []
    total = sum(v for _, v, _ in slices)
    if total <= 0:
        return out
    out.append(rf"\node[font=\small\bfseries] at ({cx:.2f},{cy + radius + 0.55:.2f}) "
               rf"{{{title}}};")
    for i, (_, _, colour) in enumerate(slices):
        out.append(rf"\definecolor{{{tag}c{i}}}{{HTML}}{{{colour.lstrip('#').upper()}}}")

    a0 = 90.0
    for i, (_, value, _) in enumerate(slices):
        frac = value / total
        a1 = a0 - 360.0 * frac
        if len(slices) == 1:
            out.append(rf"\fill[{tag}c{i}] ({cx:.2f},{cy:.2f}) circle[radius={radius:.2f}];")
        else:
            out.append(rf"\fill[{tag}c{i}] ({cx:.2f},{cy:.2f}) -- ++({a0:.3f}:{radius:.2f}) "
                       rf"arc[start angle={a0:.3f}, end angle={a1:.3f}, radius={radius:.2f}] "
                       r"-- cycle;")
        # Percentage on the wedge's bisector; the full label goes in the
        # legend, where it cannot overlap a neighbouring wedge.
        mid = math.radians((a0 + a1) / 2.0)
        lx, ly = cx + 0.62 * radius * math.cos(mid), cy + 0.62 * radius * math.sin(mid)
        out.append(rf"\node[font=\tiny,text={_ink(slices[i][2])}] at ({lx:.2f},{ly:.2f}) "
                   rf"{{{100 * frac:.0f}\%}};")
        a0 = a1

    ly = cy - radius - 0.45
    for i, (label, value, _) in enumerate(slices):
        y = ly - 0.34 * i
        out.append(rf"\fill[{tag}c{i}] ({cx - radius:.2f},{y:.2f}) "
                   rf"rectangle ++(0.26,0.26);")
        out.append(rf"\node[anchor=west,font=\tiny] at ({cx - radius + 0.36:.2f},"
                   rf"{y + 0.13:.2f}) {{{tex_escape(label)} — {value:.1f}\,s}};")
    return out


def build_stage_pies(c: XCtx) -> tuple[str, list[str], list[list]]:
    """The same pie charts.py draws, one per architecture, side by side.

    Drawn from `charts.time_slices`, the function that also produces the
    SVGs, so a wedge here is the wedge there.
    """
    bid = c.focus()
    cols = ["arch", "benchmark", "metric", "threads", "stage", "cpu_s", "share_pct"]
    data: list[list] = []
    if not bid:
        return "", cols, data

    radius, gap = 2.1, 6.4
    body: list[str] = []
    for i, s in enumerate(c.sets):
        row = s.profile(bid, CHART_METRIC, CHART_THREADS)
        if not row:
            continue
        sl = time_slices(row)["time"]
        total = sum(v for _, v, _ in sl)
        body += tikz_pie(sl, i * gap, 0.0, radius,
                         r"\texttt{" + tex_escape(s.arch) + "}" +
                         f" — {total:.0f} CPU-s", f"p{i}")
        for lab, v, _ in sl:
            data.append([s.arch, bid, CHART_METRIC, int(CHART_THREADS), lab,
                         round(v, 2), round(100 * v / total, 1) if total else None])
    if not body:
        return "", cols, data
    tex = "\n".join([r"\begin{tikzpicture}[x=1cm,y=1cm]", *body, r"\end{tikzpicture}"])
    return tex, cols, data


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------

_NOT_CONTROLLED = (
    "The machines differ in more than instruction set — core count, sockets, NUMA "
    "topology, memory and clock all differ, and none is held fixed. A ratio here "
    "compares two machines we own, not two architectures.",
)

ARTIFACTS: tuple[Artifact, ...] = (
    Artifact(
        name="table_crossarch_machines",
        kind="table",
        caption="The machines behind every other number in this document. A cross-architecture "
                "comparison is only meaningful if both sets measure the same commit; that column "
                "is here so a reader can confirm it rather than assume it.",
        label="tab:xarch:machines",
        sources=(
            "benchmarks/results/suite-<v>/<arch>/current/manifest.json :: host, commit, finished, rustc",
            "benchmarks/data/hosts.toml :: cores, topology",
        ),
        transform=(
            "One row per architecture that has a promoted current/, in the order layout.py "
            "lists them (known architectures first, so the reference is stable).",
            "Cores and topology come from hosts.toml, which is configuration rather than "
            "measurement; a host absent from it gets em-dashes, not invented facts.",
            "rustc is em-dashed for result sets measured before the runner recorded it.",
        ),
        presentation="booktabs tabular, one row per architecture.",
        caveats=(
            "Differing commits do not stop the document being built, because seeing that they "
            "differ is more useful than a refusal; every performance table is meaningless if "
            "they do, so check this row first.",
        ),
        build=build_machines,
    ),
    Artifact(
        name="table_crossarch_agreement",
        kind="table",
        caption="Cross-architecture agreement of the algorithm's own counters. The mapper is "
                "deterministic, so for one commit these must be identical on every machine: "
                "they describe the computation, not its speed. Every cell reading $=$ is what "
                "licenses the timing tables to be read as two views of one algorithm.",
        label="tab:xarch:agreement",
        sources=(
            "benchmarks/results/suite-<v>/<arch>/current/profiles.tsv :: "
            "n_mapped_reads, n_mapq60, n_seeded_buckets, n_refined_buckets, n_final_buckets",
        ),
        transform=(
            "For each (benchmark, metric) at -@1, compare every architecture's counter with "
            "the reference architecture's.",
            "Equal counters print as =; a difference prints as the signed delta, in bold, and "
            "sets the row's verdict to DIFFER.",
            "A counter missing on either side prints as an em-dash and is not called agreement.",
        ),
        presentation="booktabs tabular grouped by benchmark; one column per invariant.",
        caveats=(
            "This is the one genuinely controlled comparison in the document. Everything else "
            "measures two different machines; this measures one algorithm twice.",
            "Agreement at -@1 does not by itself establish agreement at higher thread counts — "
            "that is what the suite's own thread_determinism check covers, per architecture.",
        ),
        build=build_agreement,
    ),
    Artifact(
        name="table_crossarch_headline",
        kind="table",
        caption="Indexing time, mapping time and peak memory on each machine, single-threaded. "
                "The -@1 column is the like-for-like one: it is the only thread count at which "
                "the machines are doing comparable amounts of work per unit of hardware.",
        label="tab:xarch:headline",
        sources=(
            "benchmarks/results/suite-<v>/<arch>/current/results.tsv :: "
            "index_s, map_s, peak_rss_kb, for impl=shmap-rs at -@1",
        ),
        transform=(
            "index_s and map_s are the two-run split described in RESULTS.md: a run mapping a "
            "single read gives indexing, and the full run minus that gives mapping.",
            "peak_rss_gb = peak_rss_kb / 1048576.",
            "The ratio column is the other architecture's map_s over the reference's; above 1 "
            "means slower there.",
        ),
        presentation="booktabs tabular, grouped by benchmark, one three-column block per "
                     "architecture with a header band naming it.",
        caveats=_NOT_CONTROLLED + (
            "Single measurements, not medians: RESULTS.md 10 records per-row variance reaching "
            "~10%, so a ratio within a few percent of 1.0 is not a difference.",
        ),
        build=build_headline,
    ),
    Artifact(
        name="table_crossarch_stages",
        kind="table",
        caption="Whether the shape of the run changes with the machine. Each stage's share of "
                "total CPU time on the reference architecture, and how many percentage points "
                "that share moves elsewhere. Shares, not seconds: seconds differ because the "
                "machines differ in speed, which says nothing; a moved share says a stage "
                "became relatively dearer.",
        label="tab:xarch:stages",
        sources=(
            "benchmarks/results/suite-<v>/<arch>/current/profiles.tsv :: cpu_* stage timers, "
            f"at {CHART_METRIC}/-@{CHART_THREADS}",
        ),
        transform=(
            "Stages come from charts.time_stages — the same pipeline the SVG pies are drawn "
            "from, including the computed unattributed remainder — but unfolded.",
            "Share is the stage's CPU-seconds over the run's total CPU-seconds.",
            "The reference column is the mean share across benchmarks; the delta columns are "
            "per benchmark, because a mean of deltas would hide a single large one.",
        ),
        presentation="booktabs tabular, one row per stage, one delta column per benchmark.",
        caveats=(
            "cpu_* timers are summed across threads and are never mixed with wall_*. At -@1 "
            "the two are close, but they are still different units.",
            "The pies fold everything under 5% into one grey wedge and this table does not, so "
            "a stage with a row here may have no wedge of its own in the figure. The numbers "
            "are the same; only the presentation rule differs.",
        ),
        build=build_stages,
    ),
    Artifact(
        name="fig_crossarch_thread_scaling",
        kind="figure",
        caption=f"Thread scaling on each machine, benchmark {FOCUS_BENCHMARK}. Colour is the "
                "metric, line style the architecture; the grey diagonal is linear speedup. "
                "Each curve is normalised to its own machine's single-threaded time, so the "
                "figure compares how well the machines scale, not how fast they are.",
        label="fig:xarch:scaling",
        sources=(
            "benchmarks/results/suite-<v>/<arch>/current/results.tsv :: wall_s, for "
            "impl=shmap-rs across the thread sweep",
        ),
        transform=(
            "speedup = wall_s at -@1 on that architecture / wall_s at -@t on that architecture.",
            "Only thread counts measured on every architecture are plotted, so no curve ends "
            "early for a reason the figure does not show.",
            "Log-log axes with base-2 ticks, matching the per-architecture scaling figure.",
        ),
        presentation="pgfplots axis; one series per (architecture, metric), plus a linear "
                     "speedup reference.",
        caveats=_NOT_CONTROLLED + (
            "A machine with more cores has more room to scale before it saturates; the curves "
            "are not evidence about the ISA.",
        ),
        build=build_thread_scaling,
    ),
    Artifact(
        name="fig_crossarch_stages",
        kind="figure",
        caption=f"Where the CPU time goes on each machine, benchmark {FOCUS_BENCHMARK} "
                f"({CHART_METRIC}, -@{CHART_THREADS}). Indexing is blue, mapping orange, and "
                "wedges under 5\\% are folded into a single grey 'other'. Read against "
                "Table~\\ref{tab:xarch:stages}, which carries every benchmark.",
        label="fig:xarch:stages",
        sources=(
            "benchmarks/results/suite-<v>/<arch>/current/profiles.tsv :: cpu_* stage timers, "
            f"at {CHART_METRIC}/-@{CHART_THREADS}",
        ),
        transform=(
            "Wedges come from charts.time_slices, the function that also produces the SVG "
            "pies, so this figure and chart-<B>-time.svg cannot disagree.",
            "Drawn in plain TikZ with coordinates computed in Python, so the figure needs no "
            "LaTeX package the paper does not already load.",
        ),
        presentation="one TikZ pie per architecture, side by side, each with its own legend "
                     "in CPU-seconds.",
        caveats=(
            "CPU-seconds summed across threads, not wall-clock.",
            "Nested timers (seeding, collect_kmer_info, refine) are not wedges — they sit "
            "inside the stages above them and would double-count. The per-architecture SVGs "
            "name their values in a footer.",
        ),
        build=build_stage_pies,
    ),
)


# ---------------------------------------------------------------------------
# emission
# ---------------------------------------------------------------------------

def set_id(s: ArchSet) -> str:
    """How a promoted set identifies itself.

    Not the directory name: every promoted set is called `current`, so the
    identity has to come from the manifest. Host, commit and measurement date
    are what a reader needs to find this exact run again.
    """
    return f"{s.arch}/current on {s.host} @ {s.commit} ({s.measured})"


def header(a: Artifact, c: XCtx, comment: str) -> list[str]:
    return [
        f"{comment} GENERATED by benchmarks/scripts/crossarch.py -- do not edit.",
        f"{comment} artifact:   {a.name} ({a.kind})",
        *[f"{comment} set:        {set_id(s)}" for s in c.sets],
        f"{comment} reference:  {c.ref.arch} (every delta is against this)",
        f"{comment} inputs:     sha256:{c.digest}",
        f"{comment} provenance: paper/generated/cross-arch/PROVENANCE.md#{a.name}",
    ]


def render(a: Artifact, c: XCtx) -> dict[str, str]:
    body, cols, data = a.build(c)
    env = "table" if a.kind == "table" else "figure"
    tex = "\n".join([
        *header(a, c, "%"),
        "%",
        r"\begin{" + env + "}[tb]",
        r"\centering",
        body,
        r"\caption{" + a.caption + "}",
        r"\label{" + a.label + "}",
        r"\end{" + env + "}",
        "",
    ])
    tsv = [*header(a, c, "#"), "\t".join(cols)]
    for row in data:
        tsv.append("\t".join("" if v is None else str(v) for v in row))
    return {f"{a.name}.tex": tex, f"{a.name}.tsv": "\n".join(tsv) + "\n"}


def render_charts_html(c: XCtx, out: Path) -> str:
    """Every SVG pie from every architecture, in matched rows.

    The PDF carries one representative pie pair because a publication figure
    should. This carries all of them, for the reading where you actually want
    to look at thirty charts side by side.

    The charts are linked, not inlined: they are already committed under each
    architecture's result set, and copying ~60 SVGs in here would double them
    in the repository and let the copy go stale against the original.

    Links are relative to where this file is actually written rather than to
    the repository root, so they resolve under `--out` too -- and so a result
    set outside the repository, which a fixture or a one-off comparison uses,
    does not blow up on a `relative_to` that cannot succeed.
    """
    rel = {s.arch: Path(os.path.relpath(s.rs["dir"], out)) for s in c.sets}
    names: list[str] = []
    for s in c.sets:
        for p in sorted(s.rs["dir"].glob("chart-*.svg")):
            if p.name not in names:
                names.append(p.name)
    cells = []
    for n in names:
        row = "".join(
            f'<figure><img src="{rel[s.arch].as_posix()}/{n}" alt="{s.arch} {n}" '
            f'loading="lazy"><figcaption>{s.arch}</figcaption></figure>'
            for s in c.sets if (s.rs["dir"] / n).exists())
        cells.append(f'<section><h2>{n}</h2><div class="row">{row}</div></section>')
    arches = ", ".join(f"<code>{s.arch}</code> ({s.host})" for s in c.sets)
    return f"""<!doctype html>
<meta charset="utf-8">
<title>shmap-rs profiling charts — every architecture</title>
<style>
 body {{ font-family: DejaVu Sans, Helvetica, Arial, sans-serif; margin: 2rem; color: #222; }}
 h2 {{ font-size: .95rem; font-family: monospace; color: #555; margin: 2rem 0 .5rem; }}
 .row {{ display: flex; gap: 1rem; flex-wrap: wrap; align-items: flex-start; }}
 figure {{ margin: 0; flex: 1 1 32rem; min-width: 20rem; }}
 img {{ max-width: 100%; border: 1px solid #e2e2e2; }}
 figcaption {{ font-size: .8rem; color: #777; margin-top: .25rem; font-family: monospace; }}
 code {{ background: #f4f4f4; padding: .1rem .3rem; }}
</style>
<h1>shmap-rs profiling charts, every architecture</h1>
<p>{arches}. Generated by <code>benchmarks/scripts/crossarch.py</code>; regenerate with it.</p>
<p>Charts are linked from each architecture's own result set rather than copied here, so what
   you see is always the committed chart. Open this file from inside a checkout.</p>
<p>The publication document is <code>artifacts.pdf</code> beside this file; it carries the
   tables and one representative pie pair. This page is for looking at all of them.</p>
{chr(10).join(cells)}
"""


def render_provenance(c: XCtx) -> str:
    out = [
        "# Provenance of the cross-architecture artifacts",
        "",
        "GENERATED by `benchmarks/scripts/crossarch.py` — do not edit. Every entry is built from the",
        "`Artifact` declaration that also produced the file, so this cannot describe a transformation",
        "the code does not perform.",
        "",
        "| | |",
        "|---|---|",
    ]
    for s in c.sets:
        out.append(f"| `{s.arch}` | {set_id(s)} |")
    out += [
        f"| reference | `{c.ref.arch}` — every delta in the document is against it |",
        f"| input digest | `sha256:{c.digest}` |",
        "",
        "Regenerate with `python3 benchmarks/scripts/crossarch.py`; verify with `--check`, which",
        "fails if any artifact would change. Each artifact is emitted twice: a `.tex` fragment to",
        "`\\input`, and a `.tsv` holding exactly the numbers the fragment draws.",
        "",
        "LaTeX requirements: `booktabs` for the tables, `pgfplots` for the scaling figure, plain",
        "`tikz` for the pies — no `pie` package and no `calc` library, because every coordinate is",
        "computed in Python and emitted as a literal.",
        "",
        "## Reading this document at all",
        "",
        "The architectures are **not a controlled experiment**. The machines differ in core count,",
        "sockets, NUMA topology, memory and clock, and nothing here holds any of that fixed. Timing",
        "comparisons answer *how long did this take on each machine we own*.",
        "",
        "The exception is `table_crossarch_agreement`, which compares counters rather than times.",
        "The mapper is deterministic, so those must be identical for one commit on every machine.",
        "That table is the premise of the rest: if it reports DIFFER, the timing tables are",
        "comparing two different computations and mean nothing until that is explained.",
        "",
    ]
    for a in ARTIFACTS:
        out += [f"## {a.name}", "",
                f"**{a.kind.capitalize()}** — {a.caption}", "",
                f"Files: `{a.name}.tex`, `{a.name}.tsv`. LaTeX label: `{a.label}`.", "",
                "**Taken from**", ""]
        out += [f"- `{s}`" for s in a.sources]
        out += ["", "**Transformed by**", ""]
        out += [f"{i}. {s}" for i, s in enumerate(a.transform, 1)]
        out += ["", "**Presented as**", "", a.presentation, ""]
        if a.caveats:
            out += ["**Read with**", ""]
            out += [f"- {s}" for s in a.caveats]
            out += [""]
    out += [
        "## Not generated",
        "",
        "- **A per-architecture version of RESULTS.md.** Those documents carry one running",
        "  narrative about one machine rather than being a pure function of a result set, so",
        "  generating a second copy is the wrong shape; an architecture section inside the",
        "  existing narrative is the right one.",
        "- **Every benchmark's pies in the PDF.** Six pies per benchmark per architecture is sixty",
        "  figures, which is a data dump rather than a document. `charts.html` beside this file",
        "  shows them all, matched row by row.",
        "",
    ]
    return "\n".join(out)


def build_all(c: XCtx, out: Path = OUT) -> dict[str, str]:
    files: dict[str, str] = {}
    for a in ARTIFACTS:
        files.update(render(a, c))
    files["PROVENANCE.md"] = render_provenance(c)
    files["charts.html"] = render_charts_html(c, out)
    return files


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help=f"output directory (default: {OUT})")
    ap.add_argument("--arch", action="append", default=None,
                    help="restrict to these architectures (repeatable); default: every one "
                         "with a promoted current/")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if any artifact would change (does not write)")
    ap.add_argument("--list", action="store_true",
                    help="print each artifact's sources and transformation, and stop")
    ap.add_argument("--pdf", action="store_true",
                    help="also typeset the artifacts into cross-arch/artifacts.pdf")
    a = ap.parse_args()

    if a.list:
        for art in ARTIFACTS:
            print(f"{art.name}  ({art.kind})")
            for s in art.sources:
                print(f"    from      {s}")
            for t in art.transform:
                print(f"    transform {t}")
            print(f"    presented {art.presentation}")
            for cv in art.caveats:
                print(f"    caveat    {cv}")
            print()
        return 0

    suite_version = load_suite()["suite_version"]
    sets = load_sets(suite_version, a.arch)
    if len(sets) < 2:
        have = ", ".join(s.arch for s in sets) or "none"
        # Not an error. There is nothing stale about a comparison that does not
        # exist yet, and failing here would block CI on every repository that
        # has only ever run on one machine.
        print(f"cross-architecture artifacts need at least two promoted result sets; "
              f"suite {suite_version} has {len(sets)} ({have}).\n"
              f"  Promote a second architecture with benchmarks/scripts/promote.py, "
              f"then run this again.")
        return 0

    c = XCtx(sets=sets, hosts=load_hosts(), digest=input_digest(sets))
    out = Path(a.out) if a.out else OUT
    files = build_all(c, out)

    commits = {s.commit for s in c.sets}
    if len(commits) > 1:
        # Built anyway -- the machines table shows the mismatch, and seeing it
        # is more useful than a refusal -- but never quietly.
        print(f"WARNING: the result sets measure different commits ({', '.join(sorted(commits))}). "
              f"Every timing comparison in this document is between two different programs.",
              file=sys.stderr)

    if a.check:
        stale = [n for n, body in sorted(files.items())
                 if not (out / n).exists() or (out / n).read_text() != body]
        if stale:
            print(f"cross-architecture artifacts out of date in {out}: {', '.join(stale)}\n"
                  f"regenerate with: python3 benchmarks/scripts/crossarch.py", file=sys.stderr)
            return 1
        print(f"{len(files)} cross-architecture artifacts are current with "
              f"{', '.join(s.arch for s in c.sets)}")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    for n, body in sorted(files.items()):
        (out / n).write_text(body)
    print(f"wrote {len(files)} artifacts to {out} from "
          f"{', '.join(f'{s.arch} ({s.host})' for s in c.sets)}")

    disagree = [ln for ln in files["table_crossarch_agreement.tsv"].splitlines()
                if ln.endswith("\tdiffer")]
    if disagree:
        print(f"\nWARNING: {len(disagree)} (benchmark, metric) rows disagree across "
              f"architectures. The algorithm is deterministic, so this is a bug, and every "
              f"timing table in the document is comparing two different computations until it "
              f"is explained. See table_crossarch_agreement.tsv.", file=sys.stderr)

    if a.pdf:
        r = subprocess.run([sys.executable, str(Path(__file__).parent / "build_pdf.py"),
                            "--dir", str(out)])
        return r.returncode
    print(f"typeset with: python3 benchmarks/scripts/build_pdf.py --dir {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
