#!/usr/bin/env python3
"""Compare a candidate result set against the baseline and return a verdict.

  compare.py <candidate-dir>                    compare against results/<suite>/current/
  compare.py <candidate-dir> <baseline-dir>     compare against an explicit set
  compare.py --list                             show available result sets

Exit codes are the verdict, so this can gate a merge directly:

  0  ACCEPT   no regression beyond host noise
  1  REVIEW   a wall-time or memory regression that needs a human to justify
  2  BLOCK    an accuracy regression, a failed blocking check, or an unusable run
  3  ERROR    the two sets are not comparable at all (different suite MAJOR, host, ...)

The rules implemented here are the table in ../VERSIONING.md; the numbers come
from `[thresholds]` in suite.toml. Nothing is hard-coded in this file.

Speed never outranks accuracy: a candidate that is faster everywhere and maps
one read fewer is BLOCK, not a trade-off to weigh.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import REPO, load_suite  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"

ACCEPT, REVIEW, BLOCK, ERROR = 0, 1, 2, 3
NAMES = {ACCEPT: "ACCEPT", REVIEW: "REVIEW", BLOCK: "BLOCK", ERROR: "ERROR"}
SUBJECT = "shmap-rs"

# One measurement per configuration for shmap-rs, so a single row carries this
# host's run-to-run noise (~1-2%). Comparing ~105 of them against a 3% line
# would flag several every run by chance. The verdict therefore reads the
# geometric mean of the per-thread ratios within a (benchmark, metric) — which
# is also what VERSIONING.md means by "regresses on any benchmark" — and
# individual thread counts are reported but never decide on their own.
MIN_THREADS_FOR_AGGREGATE = 3


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def load_set(d: Path) -> dict:
    d = Path(d)
    if not d.is_dir():
        sys.exit(f"{ERROR}: no such result set: {d}")
    man_p, res_p = d / "manifest.json", d / "results.tsv"
    for p in (man_p, res_p):
        if not p.exists():
            sys.exit(f"{ERROR}: {d} is not a result set (missing {p.name})")
    man = json.loads(man_p.read_text())
    with open(res_p) as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    for r in rows:
        for k in ("threads", "rc", "mapped", "mapq60"):
            r[k] = int(r[k])
        for k in ("wall_s", "peak_rss_kb"):
            r[k] = float(r[k])
    checks = []
    if (d / "checks.tsv").exists():
        with open(d / "checks.tsv") as f:
            checks = list(csv.DictReader(f, delimiter="\t"))
        for c in checks:
            c["passed"] = c["passed"].strip().lower() == "true"
    return dict(dir=d, manifest=man, rows=rows, checks=checks)


def key(r: dict) -> tuple:
    return (r["benchmark"], r["impl"], r["metric"], r["threads"])


def agreement_of(checks: list[dict]) -> dict[tuple, float]:
    """impl_agreement details look like `123456/130000 = 0.9792`."""
    out = {}
    for c in checks:
        if c["check"] != "impl_agreement" or "=" not in c["detail"]:
            continue
        try:
            out[(c["benchmark"], c["metric"])] = float(c["detail"].rsplit("=", 1)[1])
        except ValueError:
            pass
    return out


# --------------------------------------------------------------------------
# comparability
# --------------------------------------------------------------------------

def guard(base: dict, cand: dict) -> list[str]:
    """Reasons the two sets must not be diffed at all.

    This is the guard that keeps a comparison honest. Two sets measured under
    different suite definitions, different input files or on different hardware
    produce differences that have nothing to do with the code, and presenting
    them as a regression would be worse than not comparing.
    """
    bm, cm, bad = base["manifest"], cand["manifest"], []

    b_major = str(bm["suite_version"]).split(".")[0]
    c_major = str(cm["suite_version"]).split(".")[0]
    if b_major != c_major:
        bad.append(f"suite_version MAJOR differs ({bm['suite_version']} vs {cm['suite_version']}) "
                   f"— results across a MAJOR boundary are not comparable (VERSIONING.md §2)")
    if bm["dataset_version"] != cm["dataset_version"]:
        bad.append(f"dataset_version differs ({bm['dataset_version']} vs {cm['dataset_version']}) "
                   f"— the inputs are not the same files")
    if bm["host"] != cm["host"]:
        bad.append(f"host differs ({bm['host']} vs {cm['host']}) — timings are not comparable")

    # Identity triples, not just ids: catches a file regenerated in place.
    for did, bd in (bm.get("datasets") or {}).items():
        cd = (cm.get("datasets") or {}).get(did)
        if cd and (bd.get("bytes"), bd.get("records")) != (cd.get("bytes"), cd.get("records")):
            bad.append(f"dataset {did} changed identity between the two runs "
                       f"({bd.get('bytes')}B/{bd.get('records')} vs {cd.get('bytes')}B/{cd.get('records')})")
    return bad


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------

def compare(base: dict, cand: dict, thr: dict, allow_output_change: bool) -> dict:
    brows = {key(r): r for r in base["rows"]}
    crows = {key(r): r for r in cand["rows"]}

    findings: list[dict] = []          # verdict-bearing
    notes: list[str] = []              # informational

    def add(level, kind, what, detail):
        findings.append(dict(level=level, kind=kind, what=what, detail=detail))

    # -- the run itself has to be usable -----------------------------------
    nonzero = [k for k, r in crows.items() if r["rc"] != 0]
    if nonzero:
        add(BLOCK, "run", "non-zero exit",
            f"{len(nonzero)} invocation(s) failed, e.g. {nonzero[0]}")

    missing = [k for k in brows if k[1] == SUBJECT and k not in crows]
    if missing:
        add(BLOCK, "run", "incomplete",
            f"{len(missing)} baseline configuration(s) absent from the candidate, "
            f"e.g. {missing[0]} — an incomplete run cannot be judged")

    added = [k for k in crows if k not in brows]
    if added:
        notes.append(f"{len(added)} configuration(s) are new and have no baseline "
                     f"(e.g. {added[0]}); they are reported but do not affect the verdict")

    # -- blocking checks ---------------------------------------------------
    suite_checks = load_suite()["checks"]
    for c in cand["checks"]:
        if c["passed"]:
            continue
        blocking = suite_checks.get(c["check"], {}).get("blocking", False)
        add(BLOCK if blocking else REVIEW, "check", f"{c['check']} on {c['benchmark']}/{c['metric']}",
            c["detail"])

    # impl_agreement is not blocking on its own, but a *drop* is: it means this
    # commit moved away from the reference implementation.
    ba, ca = agreement_of(base["checks"]), agreement_of(cand["checks"])
    for k, cv in sorted(ca.items()):
        bv = ba.get(k)
        if bv is not None and cv < bv - 1e-9:
            add(BLOCK, "accuracy", f"impl_agreement {k[0]}/{k[1]}",
                f"{bv:.4f} -> {cv:.4f} — diverged from the C++ reference")

    # -- accuracy, per row: any drop blocks --------------------------------
    acc_level = REVIEW if allow_output_change else BLOCK
    for field, key_name in (("mapped", "mapped_regression_block"),
                            ("mapq60", "mapq60_regression_block")):
        tol = thr.get(key_name, 0)
        worst = None
        for k, cr in sorted(crows.items()):
            br = brows.get(k)
            if not br or br[field] == 0:
                continue
            drop = br[field] - cr[field]
            if drop > tol * br[field]:
                if worst is None or drop > worst[1]:
                    worst = (k, drop, br[field], cr[field])
        if worst:
            k, drop, bv, cv = worst
            add(acc_level, "accuracy", f"{field} regressed",
                f"worst {k[0]}/{k[2]} -@{k[3]}: {bv} -> {cv} ({drop} fewer)"
                + ("; downgraded to REVIEW by --allow-output-change" if allow_output_change else ""))

    # -- wall time and memory, aggregated per (benchmark, metric) ----------
    perf: list[dict] = []
    groups: dict[tuple, list[tuple]] = {}
    for k, cr in crows.items():
        br = brows.get(k)
        if br and br["wall_s"] > 0:
            groups.setdefault((k[0], k[1], k[2]), []).append(
                (k[3], br["wall_s"], cr["wall_s"], br["peak_rss_kb"], cr["peak_rss_kb"]))

    for (bid, impl, metric), items in sorted(groups.items()):
        items.sort()
        wall_ratios = [c / b for _, b, c, _, _ in items]
        rss_ratios = [c / b for _, _, _, b, c in items if b > 0]
        gm = math.exp(statistics.fmean(math.log(x) for x in wall_ratios))
        rss_gm = math.exp(statistics.fmean(math.log(x) for x in rss_ratios)) if rss_ratios else 1.0
        row = dict(benchmark=bid, impl=impl, metric=metric, n=len(items),
                   wall=gm - 1, rss=rss_gm - 1,
                   base_s=sum(b for _, b, _, _, _ in items),
                   cand_s=sum(c for _, _, c, _, _ in items),
                   worst_thread=max(items, key=lambda t: t[2] / t[1])[0],
                   worst=max(wall_ratios) - 1)
        perf.append(row)

        if impl != SUBJECT:      # the reference implementation is not on trial
            continue
        if len(items) < MIN_THREADS_FOR_AGGREGATE:
            continue
        if gm - 1 > thr["wall_regression_block"]:
            add(BLOCK, "perf", f"{bid}/{metric} wall",
                f"{(gm-1)*100:+.1f}% over {len(items)} thread counts "
                f"(block at {thr['wall_regression_block']*100:.0f}%)")
        elif gm - 1 > thr["wall_regression_review"]:
            add(REVIEW, "perf", f"{bid}/{metric} wall",
                f"{(gm-1)*100:+.1f}% over {len(items)} thread counts "
                f"(review at {thr['wall_regression_review']*100:.0f}%)")
        if rss_gm - 1 > thr.get("peak_rss_regression_review", 0.05):
            add(REVIEW, "perf", f"{bid}/{metric} peak RSS", f"{(rss_gm-1)*100:+.1f}%")

    verdict = max([f["level"] for f in findings], default=ACCEPT)
    return dict(verdict=verdict, findings=findings, notes=notes, perf=perf,
                agreement=(ba, ca))


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def pct(x: float) -> str:
    return f"{x*100:+.1f}%"


def render(base: dict, cand: dict, res: dict, thr: dict) -> str:
    bm, cm = base["manifest"], cand["manifest"]
    v = res["verdict"]
    verdict_line = {
        ACCEPT: "**ACCEPT** — no regression beyond this host's noise.",
        REVIEW: "**REVIEW** — needs a human decision; see the findings below.",
        BLOCK: "**BLOCK** — do not merge.",
    }[v]

    L = [f"# Benchmark comparison — {NAMES[v]}",
         "",
         verdict_line,
         "",
         "| | baseline | candidate |",
         "|---|---|---|",
         f"| commit | `{bm['commit'][:12]}` | `{cm['commit'][:12]}` |",
         f"| suite | {bm['suite_version']} | {cm['suite_version']} |",
         f"| datasets | v{bm['dataset_version']} | v{cm['dataset_version']} |",
         f"| host | {bm['host']} | {cm['host']} |",
         f"| measured | {bm['finished'][:19]}Z | {cm['finished'][:19]}Z |",
         f"| invocations | {bm['invocations']} | {cm['invocations']} |",
         ""]

    if res["findings"]:
        L += ["## Findings", "",
              "| level | kind | what | detail |", "|---|---|---|---|"]
        order = {BLOCK: 0, REVIEW: 1, ACCEPT: 2}
        for f in sorted(res["findings"], key=lambda f: (order[f["level"]], f["kind"])):
            L.append(f"| **{NAMES[f['level']]}** | {f['kind']} | {f['what']} | {f['detail']} |")
        L.append("")
    else:
        L += ["No blocking or reviewable differences.", ""]

    L += ["## Wall time by benchmark",
          "",
          "Geometric mean of the per-thread-count ratios; a single thread count is too "
          "noisy to judge on its own, so the worst one is shown for context but does not "
          "decide the verdict.",
          "",
          "| benchmark | metric | impl | threads | baseline | candidate | change | worst |",
          "|---|---|---|---|---|---|---|---|"]
    for p in sorted(res["perf"], key=lambda p: (p["impl"] != SUBJECT, p["benchmark"], p["metric"])):
        flag = ""
        if p["impl"] == SUBJECT:
            if p["wall"] > thr["wall_regression_block"]:
                flag = " ⛔"
            elif p["wall"] > thr["wall_regression_review"]:
                flag = " ⚠️"
            elif p["wall"] < -thr["wall_regression_review"]:
                flag = " ✅"
        L.append(f"| {p['benchmark']} | {p['metric']} | {p['impl']} | {p['n']} | "
                 f"{p['base_s']:.1f}s | {p['cand_s']:.1f}s | {pct(p['wall'])}{flag} | "
                 f"{pct(p['worst'])} @-@{p['worst_thread']} |")
    L.append("")

    ba, ca = res["agreement"]
    if ca:
        L += ["## Agreement with the C++ reference", "",
              "| benchmark | metric | baseline | candidate |", "|---|---|---|---|"]
        for k in sorted(ca):
            b = f"{ba[k]:.4f}" if k in ba else "—"
            L.append(f"| {k[0]} | {k[1]} | {b} | {ca[k]:.4f} |")
        L.append("")

    if res["notes"]:
        L += ["## Notes", ""] + [f"- {n}" for n in res["notes"]] + [""]

    L += ["---", "",
          f"Rules: [VERSIONING.md](../VERSIONING.md). Thresholds from `suite.toml`: "
          f"wall review >{thr['wall_regression_review']*100:.0f}%, "
          f"block >{thr['wall_regression_block']*100:.0f}%; any drop in mapped reads, "
          f"mapq-60 reads or C++ agreement blocks.",
          "",
          f"Baseline: `{base['dir']}`  ",
          f"Candidate: `{cand['dir']}`"]
    return "\n".join(L)


# --------------------------------------------------------------------------

def list_sets() -> int:
    for suite_dir in sorted(RESULTS.glob("suite-*")):
        print(f"{suite_dir.name}/")
        for d in sorted(suite_dir.iterdir()):
            if not (d / "manifest.json").exists():
                continue
            m = json.loads((d / "manifest.json").read_text())
            print(f"  {d.name:32} {m['commit'][:12]}  {m['finished'][:19]}Z  "
                  f"{m['invocations']:3} invocations  {m.get('failures', 0)} failures")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("candidate", nargs="?", help="candidate result-set directory")
    ap.add_argument("baseline", nargs="?", help="baseline (default: the suite's current/)")
    ap.add_argument("--list", action="store_true", help="list available result sets")
    ap.add_argument("--out", help="write the markdown report here as well as stdout")
    ap.add_argument("--allow-output-change", action="store_true",
                    help="downgrade mapped/mapq60 drops from BLOCK to REVIEW. Only for an "
                         "argued correctness fix, where the previous output was wrong "
                         "(VERSIONING.md, 'Accept, output changed').")
    a = ap.parse_args()

    if a.list:
        return list_sets()
    if not a.candidate:
        ap.error("a candidate result set is required (or --list)")

    cand = load_set(Path(a.candidate))
    if a.baseline:
        base = load_set(Path(a.baseline))
    else:
        d = RESULTS / f"suite-{cand['manifest']['suite_version']}" / "current"
        if not d.is_dir():
            print(f"no baseline at {d} — nothing to compare against.\n"
                  f"If this run is meant to become the baseline, copy it there:\n"
                  f"  cp -r {a.candidate} {d}", file=sys.stderr)
            return ERROR
        base = load_set(d)

    suite = load_suite()
    thr = suite["thresholds"]

    bad = guard(base, cand)
    if bad:
        print("# Benchmark comparison — ERROR\n\nThese result sets must not be compared:\n")
        for b in bad:
            print(f"- {b}")
        return ERROR

    res = compare(base, cand, thr, a.allow_output_change)
    md = render(base, cand, res, thr)
    print(md)
    if a.out:
        Path(a.out).write_text(md + "\n")
    return res["verdict"]


if __name__ == "__main__":
    sys.exit(main())
