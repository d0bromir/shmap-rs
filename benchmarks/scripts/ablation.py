#!/usr/bin/env python3
"""The cumulative optimization ladder: what each change is worth, in order.

  ablation.py --measure           run the ladder here and write a result set
  ablation.py                     build paper/generated/figure_ablation.{tex,tsv}
  ablation.py --check             exit 1 if the figure would change
  ablation.py --list              the rungs, in order, with the switch each adds

---------------------------------------------------------------------------
What this measures, and why it is not the table
---------------------------------------------------------------------------
`PORT_CHANGES.md` records what each optimization was worth *against the build
it landed on*, months apart, on different inputs. Those figures are real and
deliberately not refreshed, but they cannot be put on one axis: they do not
share a baseline, a machine, a compiler or a read set, and they do not sum.
Table 1 of the companion note says so in as many words.

This is the other measurement, the one a reader actually wants: one binary,
one machine, one input, one compiler, and a *ladder*. Rung 0 has every
ablatable optimization switched off; each rung to the right switches exactly
one more back on; the last rung is the shipped build. Adjacent rungs differ by
one change and nothing else, so the step between them is that change's worth
*in the presence of the ones before it* — which is the only sense in which the
question has an answer, because these changes interact. (Row 1 removes a
per-worker genome-sized array; how much rows 5 and 6 are then worth depends
entirely on whether that array is still there.)

The switches live in the mapper itself (`src/ablate.rs`, `SHMAP_ABLATE`), not
in a second build. That is the point: a ladder built from nine binaries would
measure nine compilations as much as nine changes.

---------------------------------------------------------------------------
The order of the rungs
---------------------------------------------------------------------------
The companion note's own layer order — data structure, then the algorithmic
identities, then the parallel decomposition, then the code — which is the
order it argues the changes had to be discovered in. Not the order of largest
effect: choosing that order after seeing the numbers is how a cumulative
ladder is made to say whatever its author wants.

Two of the nine are not rungs, and both absences are reported rather than
estimated:

  * Row 4 (threaded read mapping) is a capability, not a branch. It is the
    difference between the two *series* — the whole ladder is measured at one
    worker and again at `-@N` — so the figure shows it as the gap between the
    curves instead of as a step in one.
  * Row 8 (`PMatches` inline, `Match` borrowing its `Seed`, `lto = "fat"`) is
    type- and build-level. Reversing it is a different binary, so it cannot be
    a switch, and it is stated as not ablated.

---------------------------------------------------------------------------
What makes the numbers checkable
---------------------------------------------------------------------------
1. **Every rung's output is compared byte for byte.** All nine changes claim
   to preserve the mapping exactly; if a rung's PAF differs from rung 0's, the
   ladder is not measuring what it says and the run fails rather than
   reporting. This is a stronger check than the suite's, because it holds the
   input fixed and varies only the optimization.
2. **Repeats are round-robin, reduced by median.** The whole ladder runs, then
   runs again. Measuring one rung five times in a row and then the next would
   let a thermal or scheduler drift over the run land entirely on one rung and
   be read as that optimization's effect.
3. **The inputs are pinned by identity.** Reference and reads are checked
   against `benchmarks/data/datasets.tsv` (bytes, records, bases) before
   anything is measured, exactly as `run.py` does.
4. **The result set is committed.** The figure is regenerated from
   `ladder.tsv` by a pure function — no timestamps, sorted iteration — so
   `--check` is a real equality test and a reader can read the numbers the
   bars are drawn from without a LaTeX toolchain.

Wall time and peak RSS are the whole process, `/usr/bin/time -v`, which is
what a user pays. Indexing is included: two of the rungs are index-side
changes and hiding them behind a mapping-only figure would understate them.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import shlex
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from layout import REPO, RESULTS, arch, resolve_dataset  # noqa: E402
from run import load_registry, load_suite, parse_time_v, rustc_version  # noqa: E402

OUT = REPO / "paper" / "generated"
NAME = "figure_ablation"
# One directory per architecture, for the reason layout.py separates result
# sets: a wall-clock second is a property of the machine that spent it.
SET_ROOT = RESULTS / "ablation"

# The parameter set every headline number in the paper is measured at, so the
# ladder describes the same program the rest of the paper does.
PARAM_SET = "paper"
METRIC = "Containment"

# `(rung label, SHMAP_ABLATE switch this rung turns back on, PORT_CHANGES row)`
# in the companion note's layer order. Rung 0 is everything off; each entry
# below adds exactly one switch to the ones above it.
LADDER: list[tuple[str, str, int]] = [
    ("read-sized buckets", "bucket-array", 1),
    ("streamed seeds", "stream-seeds", 2),
    ("refine memo", "refine-memo", 3),
    ("packed sort key", "packed-sort", 9),
    ("parallel index", "parallel-index", 5),
    ("parallel FASTA", "parallel-fasta", 6),
    ("sketch loop", "sketch-loop", 7),
]
BASELINE = "pre-optimization"

# Rows that exist but cannot be a rung, and why. Printed in the provenance and
# the caption: an optimization missing from a figure that claims to account
# for all of them must be visibly missing, not quietly.
NOT_ABLATED: dict[int, str] = {
    4: "threaded read mapping is the gap between the two series, not a step in one",
    8: "type- and build-level (PMatches inline, Match borrows its Seed, lto=fat) — "
       "reversing it is a different binary, not a different branch",
}


def switches_at(i: int) -> list[str]:
    """The switches still ablated at rung `i` (0 = baseline, everything off)."""
    return [s for _, s, _ in LADDER[i:]]


def rung_label(i: int) -> str:
    return BASELINE if i == 0 else "+ " + LADDER[i - 1][0]


def rung_row(i: int) -> str:
    return "" if i == 0 else str(LADDER[i - 1][2])


# ---------------------------------------------------------------------------
# measuring
# ---------------------------------------------------------------------------

def verify_dataset(reg: dict, ds_id: str, full: bool) -> Path:
    """The registry's path for `ds_id`, or exit. Same rule as run.py.

    Benchmarking a quietly regenerated file and attributing the difference to
    code is the failure this system most needs to be incapable of, and it is
    silent. Size always; records and bases on request, because counting them
    costs a full pass.
    """
    if ds_id not in reg:
        sys.exit(f"dataset {ds_id} is not in datasets.tsv")
    e = reg[ds_id]
    p = Path(e["path"])
    if not p.exists():
        sys.exit(f"dataset {ds_id} missing at {p}")
    if str(p.stat().st_size) != e["bytes"]:
        sys.exit(f"dataset {ds_id} changed: registry says {e['bytes']} bytes, "
                 f"file is {p.stat().st_size}. Register a NEW id rather than "
                 f"editing the row — see VERSIONING.md §3.")
    if full:
        records = bases = 0
        with open(p) as fh:
            for line in fh:
                if line.startswith(">"):
                    records += 1
                else:
                    bases += len(line.strip())
        if (str(records), str(bases)) != (e["records"], e["bases"]):
            sys.exit(f"dataset {ds_id}: registry says {e['records']} records / "
                     f"{e['bases']} bases, file has {records} / {bases}")
    return p


def measure(binary: Path, ref: Path, reads: Path, params: list[str],
            threads: int, ablate: list[str], workdir: Path, tag: str) -> dict:
    """One invocation: wall seconds, peak RSS, and a digest of the mapping."""
    paf, tf = workdir / f"{tag}.paf", workdir / f"{tag}.time"
    cmd = ["/usr/bin/time", "-v", "-o", str(tf), str(binary),
           "-s", str(ref), "-p", str(reads), *params, "-m", METRIC, "-@", str(threads)]
    env = dict(os.environ)
    if ablate:
        env["SHMAP_ABLATE"] = ",".join(ablate)
    else:
        env.pop("SHMAP_ABLATE", None)
    with open(paf, "w") as fo:
        rc = subprocess.run(cmd, stdout=fo, stderr=subprocess.DEVNULL, env=env).returncode
    if rc != 0:
        sys.exit(f"{tag} exited {rc}: {shlex.join(cmd)}")
    wall, rss = parse_time_v(tf)

    # Columns 1-12 only: column 13 is the per-read wall clock, which differs
    # between two runs of the *same* build and would defeat the check this
    # digest exists for.
    h, mapped = hashlib.sha256(), 0
    with open(paf) as fh:
        for line in fh:
            mapped += 1
            h.update("\t".join(line.rstrip("\n").split("\t")[:12]).encode())
            h.update(b"\n")
    paf.unlink()
    tf.unlink()
    return dict(wall_s=wall, peak_rss_kb=rss, mapped=mapped, paf_sha=h.hexdigest()[:16])


def run_ladder(a: argparse.Namespace) -> int:
    suite = load_suite()
    reg = load_registry()
    ref = verify_dataset(reg, a.reference, a.verify_full)
    reads = verify_dataset(reg, a.reads, a.verify_full)
    binary = Path(a.binary) if a.binary else REPO / "target" / "release" / "shmap"
    if not binary.exists():
        sys.exit(f"no binary at {binary}; cargo build --release first")

    p = suite["params"][PARAM_SET]
    params = ["-k", str(p["k"]), "-r", str(p["hashratio"]), "-t", str(p["threshold"]),
              "-d", str(p["min_diff"]), "-o", str(p["max_overlap"])]
    thread_counts = [int(t) for t in a.threads.split(",")]
    workdir = Path(a.workdir).expanduser()
    workdir.mkdir(parents=True, exist_ok=True)

    # Read both inputs once so the first rung does not pay for a cold page
    # cache that every later rung gets for free.
    for f in (ref, reads):
        subprocess.run(["bash", "-c", f"cat {shlex.quote(str(f))} > /dev/null"], check=True)

    t0 = time.time()
    raw: list[dict] = []
    for rep in range(a.repeats):
        for threads in thread_counts:
            for i in range(len(LADDER) + 1):
                off = switches_at(i)
                r = measure(binary, ref, reads, params, threads, off, workdir,
                            f"t{threads}_r{i}_rep{rep}")
                raw.append(dict(threads=threads, rung=i, repeat=rep, **r))
                print(f"  rep{rep} -@{threads} rung {i} {rung_label(i):<22} "
                      f"{r['wall_s']:7.2f}s {r['peak_rss_kb']/1024:8.1f} MB "
                      f"mapped={r['mapped']} {r['paf_sha']}", flush=True)

    # Every rung must produce the same mapping. This is the claim all nine
    # changes make, and the ladder is only a measurement of them if it holds.
    digests = {r["paf_sha"] for r in raw}
    if len(digests) != 1:
        by_rung = {}
        for r in raw:
            by_rung.setdefault(r["paf_sha"], []).append(f"-@{r['threads']} rung {r['rung']}")
        detail = "; ".join(f"{d}: {', '.join(sorted(set(v)))}" for d, v in sorted(by_rung.items()))
        sys.exit(f"OUTPUT CHANGED ACROSS THE LADDER — {len(digests)} distinct mappings.\n"
                 f"  {detail}\n"
                 f"Every switch is supposed to be output-preserving in both positions. "
                 f"Either a switch is wrong or an optimization is; do not publish this.")

    rows = []
    for threads in thread_counts:
        for i in range(len(LADDER) + 1):
            got = [r for r in raw if r["threads"] == threads and r["rung"] == i]
            rows.append(dict(
                threads=threads, rung=i, label=rung_label(i), row=rung_row(i),
                switch="" if i == 0 else LADDER[i - 1][1],
                ablated=",".join(switches_at(i)),
                wall_s=round(statistics.median(g["wall_s"] for g in got), 3),
                wall_min_s=round(min(g["wall_s"] for g in got), 3),
                wall_max_s=round(max(g["wall_s"] for g in got), 3),
                peak_rss_kb=int(statistics.median(g["peak_rss_kb"] for g in got)),
                mapped=got[0]["mapped"], paf_sha=got[0]["paf_sha"]))

    outdir = SET_ROOT / arch() / "current"
    outdir.mkdir(parents=True, exist_ok=True)
    cols = ["threads", "rung", "label", "row", "switch", "ablated", "wall_s",
            "wall_min_s", "wall_max_s", "peak_rss_kb", "mapped", "paf_sha"]
    with open(outdir / "ladder.tsv", "w") as fo:
        fo.write("# Cumulative optimization ladder — GENERATED by benchmarks/scripts/ablation.py\n")
        fo.write("# rung 0 has every ablatable optimization off; each rung adds one back.\n")
        fo.write("# wall_s and peak_rss_kb are medians over the repeats; paf_sha is identical\n")
        fo.write("# on every row by construction (the run fails otherwise).\n")
        fo.write("\t".join(cols) + "\n")
        for r in rows:
            fo.write("\t".join(str(r[c]) for c in cols) + "\n")

    git = lambda *xs: subprocess.run(["git", "-C", str(REPO), *xs], capture_output=True,
                                     text=True).stdout.strip()
    manifest = dict(
        schema=1,
        kind="ablation-ladder",
        host=platform.node(), arch=arch(),
        cpu_model=cpu_model(), cores=os.cpu_count(),
        commit=git("rev-parse", "HEAD"),
        dirty=bool(git("status", "--porcelain")),
        rustc=rustc_version(),
        suite_version=str(suite["suite_version"]),
        params=PARAM_SET, metric=METRIC, param_flags=params,
        threads=thread_counts, repeats=a.repeats, reduce="median",
        datasets={a.reference: reg[a.reference], a.reads: reg[a.reads]},
        identity_verified="bytes+records+bases" if a.verify_full else "bytes",
        binary=subprocess.run([str(binary), "--version"], capture_output=True,
                              text=True).stdout.strip(),
        started=datetime.fromtimestamp(t0, timezone.utc).isoformat(),
        finished=datetime.now(timezone.utc).isoformat(),
        duration_s=round(time.time() - t0, 1),
        invocations=len(raw),
        paf_sha=sorted(digests)[0],
        not_ablated={str(k): v for k, v in sorted(NOT_ABLATED.items())},
    )
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"\nwrote {outdir}/ladder.tsv ({len(rows)} rows, {len(raw)} invocations, "
          f"{manifest['duration_s']:.0f}s)")
    return 0


def cpu_model() -> str:
    """The CPU as /proc names it — the machine is part of the measurement."""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


# ---------------------------------------------------------------------------
# the figure
# ---------------------------------------------------------------------------

def load_ladder(a: str | None = None) -> tuple[list[dict], dict, Path]:
    d = SET_ROOT / (a or arch()) / "current"
    t, m = d / "ladder.tsv", d / "manifest.json"
    if not t.exists() or not m.exists():
        sys.exit(f"no ablation result set in {d}; measure one with:\n"
                 f"  python3 benchmarks/scripts/ablation.py --measure")
    with open(t) as fh:
        rows = list(csv.DictReader((l for l in fh if not l.startswith("#")), delimiter="\t"))
    for r in rows:
        for k in ("threads", "rung", "peak_rss_kb", "mapped"):
            r[k] = int(r[k])
        for k in ("wall_s", "wall_min_s", "wall_max_s"):
            r[k] = float(r[k])
    return rows, json.loads(m.read_text()), d


def digest(d: Path) -> str:
    h = hashlib.sha256()
    for name in ("ladder.tsv", "manifest.json"):
        h.update(name.encode())
        h.update((d / name).read_bytes())
    return h.hexdigest()[:16]


def tex_escape(s: str) -> str:
    for a, b in (("\\", ""), ("&", r"\&"), ("#", r"\#"), ("%", r"\%"),
                 ("$", r"\$"), ("_", r"\_"), ("{", r"\{"), ("}", r"\}")):
        s = s.replace(a, b)
    return s


def header(man: dict, dig: str, comment: str) -> list[str]:
    return [
        f"{comment} GENERATED by benchmarks/scripts/ablation.py -- do not edit.",
        f"{comment} artifact:   {NAME} (figure)",
        f"{comment} result set: benchmarks/results/ablation/{man['arch']}/current",
        f"{comment} commit:     {man.get('commit', '?')[:12]}"
        + ("  (working tree dirty)" if man.get("dirty") else ""),
        f"{comment} host:       {man.get('host', '?')} ({man.get('cores', '?')} cores)",
        f"{comment} measured:   {man.get('finished', '?')[:10]}",
        f"{comment} inputs:     sha256:{dig}",
        f"{comment} provenance: paper/generated/ABLATION.md",
    ]


# Two series, distinguished by mark as well as colour: the note is printed in
# greyscale as often as not.
SERIES = [("blue!70!black", "*"), ("orange!85!black", "square*")]


def axis(rows: list[dict], threads: list[int], key: str, scale: float,
         ylabel: str, extra: str = "") -> list[str]:
    """One panel: rung on x, `key` on y, one plot per thread count."""
    out = [
        r"\begin{axis}[",
        r"  width=0.47\textwidth, height=4.3cm,",
        r"  ybar, bar width=3.6pt, ymin=0,",
        r"  xtick=data, xticklabels={" + ",".join(
            "{" + tex_escape(r["label"]) + "}" for r in rows if r["threads"] == threads[0]) + "},",
        r"  x tick label style={rotate=40, anchor=east, font=\tiny},",
        r"  y tick label style={font=\tiny}, ylabel style={font=\scriptsize},",
        r"  ylabel={" + ylabel + "},",
        r"  enlarge x limits=0.08, grid=major, grid style={gray!20},",
        r"  legend style={font=\tiny, draw=none, fill=none, at={(0.02,0.97)},",
        r"                anchor=north west, legend columns=1},",
        *( [f"  {extra}"] if extra else [] ),
        r"]",
    ]
    for (colour, mark), t in zip(SERIES, threads):
        pts = " ".join(f"({r['rung']},{r[key] * scale:.4g})"
                       for r in rows if r["threads"] == t)
        out += [rf"\addplot+[{colour}, fill={colour}!35, mark={mark}, mark size=1pt] "
                rf"coordinates {{{pts}}};",
                rf"\addlegendentry{{\texttt{{-@{t}}}}}"]
    out.append(r"\end{axis}")
    return out


def build(rows: list[dict], man: dict) -> tuple[str, list[str], list[list]]:
    threads = sorted({r["threads"] for r in rows})
    ordered = sorted(rows, key=lambda r: (r["threads"], r["rung"]))

    body = [r"\begin{tikzpicture}"]
    body += axis(ordered, threads, "wall_s", 1.0, r"wall (s)")
    body += [r"\begin{scope}[xshift=0.50\textwidth]"]
    body += axis(ordered, threads, "peak_rss_kb", 1 / 1024.0, r"peak RSS (MB)")
    body += [r"\end{scope}", r"\end{tikzpicture}"]

    cols = ["threads", "rung", "label", "port_changes_row", "switch_enabled",
            "still_ablated", "wall_s", "wall_min_s", "wall_max_s", "peak_rss_mb",
            "speedup_vs_rung0", "rss_ratio_vs_rung0", "step_wall_pct", "mapped", "paf_sha"]
    data: list[list] = []
    for t in threads:
        series = [r for r in ordered if r["threads"] == t]
        base = series[0]
        for i, r in enumerate(series):
            prev = series[i - 1] if i else None
            data.append([
                t, r["rung"], r["label"], r["row"] or "", r["switch"], r["ablated"],
                f"{r['wall_s']:.3f}", f"{r['wall_min_s']:.3f}", f"{r['wall_max_s']:.3f}",
                f"{r['peak_rss_kb'] / 1024:.1f}",
                f"{base['wall_s'] / r['wall_s']:.3f}" if r["wall_s"] else "",
                f"{base['peak_rss_kb'] / r['peak_rss_kb']:.3f}" if r["peak_rss_kb"] else "",
                "" if prev is None else f"{100.0 * (prev['wall_s'] - r['wall_s']) / prev['wall_s']:.1f}",
                r["mapped"], r["paf_sha"],
            ])
    return "\n".join(body), cols, data


def caption(rows: list[dict], man: dict) -> str:
    threads = sorted({r["threads"] for r in rows})
    return (
        r"Every optimization, put back one at a time. Rung "
        r"\emph{" + tex_escape(BASELINE) + r"} runs the shipped binary with every "
        r"ablatable change switched off (\texttt{SHMAP\_ABLATE}, \texttt{src/ablate.rs}); "
        r"each rung to its right switches exactly one more back on, in the layer order "
        r"this note argues. Adjacent rungs therefore differ by one change and nothing "
        r"else --- same binary, machine, compiler and input --- so a step is that change's "
        r"worth \emph{given the ones before it}, which is the only sense in which these "
        r"interacting changes have individual values. The two series are the two thread "
        r"counts; the gap between them is row~4, threaded read mapping, which is a "
        r"capability rather than a branch and so cannot be a rung. Row~8 is type- and "
        r"build-level and is not ablated at all. Left: wall clock. Right: peak resident "
        r"set --- one worker's genome-sized accumulator is invisible beside the index, "
        r"but \texttt{-@" + str(threads[-1]) + r"} workers' copies are not, which is the "
        r"scaling argument in one picture. Every rung's mapping is byte-identical; the "
        r"run fails otherwise. Numbers in \texttt{" + NAME + r".tsv}."
    )


def render(rows: list[dict], man: dict, dig: str) -> dict[str, str]:
    body, cols, data = build(rows, man)
    tex = "\n".join([
        *header(man, dig, "%"), "%",
        r"\begin{figure*}[t]", r"\centering", body,
        r"\caption{" + caption(rows, man) + "}",
        r"\label{fig:ablation}", r"\end{figure*}", "",
    ])
    tsv = [*header(man, dig, "#"), "\t".join(cols)]
    for row in data:
        tsv.append("\t".join(str(v) for v in row))
    return {f"{NAME}.tex": tex, f"{NAME}.tsv": "\n".join(tsv) + "\n"}


def provenance(rows: list[dict], man: dict, dig: str) -> str:
    threads = sorted({r["threads"] for r in rows})
    ds = man.get("datasets", {})
    out = [
        "# Provenance of the cumulative ablation ladder",
        "",
        "GENERATED by `benchmarks/scripts/ablation.py` — do not edit.",
        "",
        "| | |",
        "|---|---|",
        f"| result set | `benchmarks/results/ablation/{man['arch']}/current` |",
        f"| host | `{man.get('host')}` — {man.get('cpu_model')}, {man.get('cores')} cores |",
        f"| commit | `{man.get('commit', '?')[:12]}`"
        + (" **(working tree dirty)**" if man.get("dirty") else "") + " |",
        f"| rustc | `{man.get('rustc')}` |",
        f"| measured | {man.get('finished', '?')[:10]} |",
        f"| parameters | suite.toml `params.{man.get('params')}`: "
        f"`{' '.join(man.get('param_flags', []))}`, metric `{man.get('metric')}` |",
        f"| thread counts | {', '.join(f'`-@{t}`' for t in threads)} |",
        f"| repeats | {man.get('repeats')}, round-robin over the whole ladder, reduced by {man.get('reduce')} |",
        f"| inputs verified | {man.get('identity_verified')} against `benchmarks/data/datasets.tsv` |",
        f"| mapping digest | `{man.get('paf_sha')}` — identical on every rung |",
        f"| input digest | `sha256:{dig}` |",
        "",
        "## Inputs",
        "",
        "| id | rel path | bytes | records | bases |",
        "|---|---|---|---|---|",
    ]
    for k in sorted(ds):
        e = ds[k]
        out.append(f"| `{k}` | `{e.get('rel', '')}` | {e.get('bytes')} | "
                   f"{e.get('records')} | {e.get('bases')} |")
    out += [
        "",
        "## Rungs",
        "",
        "Cumulative and left to right: rung 0 has every ablatable optimization off, and each",
        "rung switches exactly one more back on.",
        "",
        "| rung | label | PORT_CHANGES row | switch turned back on |",
        "|---|---|---|---|",
        "| 0 | " + BASELINE + " | — | — (all off) |",
    ]
    for i, (label, sw, row) in enumerate(LADDER, 1):
        out.append(f"| {i} | + {label} | {row} | `{sw}` |")
    out += [
        "",
        "## Not on the ladder",
        "",
        "An optimization missing from a figure that claims to account for all of them has to be",
        "visibly missing rather than quietly.",
        "",
    ]
    for row, why in sorted(NOT_ABLATED.items()):
        out.append(f"- **Row {row}** — {why}.")
    out += [
        "",
        "## Read with",
        "",
        "- A step is the change's worth **given every change to its left**, not in isolation.",
        "  These interact: how much the parallel index is worth depends on whether each worker",
        "  is still carrying a genome-sized accumulator. Steps therefore do not commute and do",
        "  not sum to the end-to-end ratio in any meaningful way beyond arithmetic.",
        "- Not comparable with `PORT_CHANGES.md`'s per-row effects, which were each measured",
        "  against the build that change landed on, months apart and on other inputs. That is",
        "  the reason this exists.",
        "- Peak RSS is the whole process, so at one worker the accumulator sits under the",
        "  index-build peak and row 1 looks free. It is not: the array is per worker, which is",
        f"  what the `-@{threads[-1]}` series shows.",
        "- This is a chromosome-scale reference on a laptop, not the whole-genome suite. It is",
        "  a measurement of the *relative* worth of the changes on one input; the absolute",
        "  end-to-end figures the paper quotes come from the promoted suite result sets.",
        "",
    ]
    return "\n".join(out)


def build_all() -> tuple[dict[str, str], dict]:
    rows, man, d = load_ladder()
    dig = digest(d)
    files = render(rows, man, dig)
    files["ABLATION.md"] = provenance(rows, man, dig)
    return files, man


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--measure", action="store_true", help="run the ladder and write a result set")
    ap.add_argument("--check", action="store_true", help="exit 1 if the figure would change")
    ap.add_argument("--list", action="store_true", help="print the rungs and stop")
    ap.add_argument("--reference", default="REF-CHR21", help="dataset id of the reference")
    ap.add_argument("--reads", default="LOC-CHR21SIM12K", help="dataset id of the reads")
    ap.add_argument("--threads", default="1,8", help="comma-separated thread counts (two series)")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--binary", help="mapper to measure (default: target/release/shmap)")
    ap.add_argument("--workdir", default="~/ablation-work", help="scratch for PAFs, deleted as it goes")
    ap.add_argument("--verify-full", action="store_true",
                    help="check records and bases too, not just size")
    ap.add_argument("--out", help=f"output directory (default: {OUT})")
    a = ap.parse_args()

    if a.list:
        print(f"rung 0  {BASELINE:<24} (every ablatable optimization off)")
        for i, (label, sw, row) in enumerate(LADDER, 1):
            print(f"rung {i}  + {label:<22} row {row}, switch {sw!r}")
        for row, why in sorted(NOT_ABLATED.items()):
            print(f"not a rung: row {row} — {why}")
        return 0

    if a.measure:
        return run_ladder(a)

    files, man = build_all()
    out = Path(a.out) if a.out else OUT

    if a.check:
        stale = [n for n, body in sorted(files.items())
                 if not (out / n).exists() or (out / n).read_text() != body]
        if stale:
            print(f"ablation figure out of date in {out}: {', '.join(stale)}\n"
                  f"regenerate with: python3 benchmarks/scripts/ablation.py", file=sys.stderr)
            return 1
        print(f"ablation figure is current with {man['host']} ({man['arch']})")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    for n, body in sorted(files.items()):
        (out / n).write_text(body)
    print(f"wrote {NAME}.{{tex,tsv}} and ABLATION.md to {out} "
          f"from {man['host']} ({man['arch']}, {man['finished'][:10]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
