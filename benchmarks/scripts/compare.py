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
import re
import statistics
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import REPO, load_suite  # noqa: E402

from layout import HOSTS_TOML, RESULTS, current_dir  # noqa: E402

ACCEPT, REVIEW, BLOCK, ERROR = 0, 1, 2, 3
NAMES = {ACCEPT: "ACCEPT", REVIEW: "REVIEW", BLOCK: "BLOCK", ERROR: "ERROR"}
SUBJECT = "shmap-rs"
REFERENCE_IMPL = "cpp-shmap"

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
        # Present only for the subject (the C++ emits no -x report) and absent
        # from result sets written before the split existed.
        for k in ("index_s", "map_s"):
            v = r.get(k, "")
            r[k] = float(v) if v not in ("", None) else None
    checks = []
    if (d / "checks.tsv").exists():
        with open(d / "checks.tsv") as f:
            checks = list(csv.DictReader(f, delimiter="\t"))
        for c in checks:
            c["passed"] = c["passed"].strip().lower() == "true"
    return dict(dir=d, manifest=man, rows=rows, checks=checks)


# Only these may be overridden per host. Everything else in [thresholds]
# describes the code -- a drop in mapped reads is a regression on any machine --
# and letting a host relax those would let a noisy box hide a real defect.
HOST_OVERRIDABLE = {"wall_regression_review", "wall_regression_block",
                    "peak_rss_regression_review"}


def host_thresholds(suite: dict, host: str | None) -> tuple[dict, str | None]:
    """suite.toml's thresholds, with this host's wall-time overrides applied.

    Returns (thresholds, host-name-if-anything-was-overridden). The host comes
    from the candidate's manifest rather than from whoever is running the
    comparison: the noise belongs to the machine that measured, and a
    comparison run on a laptop must still judge an a2 set by a2's numbers.
    """
    thr = dict(suite["thresholds"])
    if not host or not HOSTS_TOML.exists():
        return thr, None
    try:
        entry = tomllib.loads(HOSTS_TOML.read_text()).get(host) or {}
    except (OSError, ValueError):
        return thr, None
    applied = False
    for k in HOST_OVERRIDABLE:
        if k in entry:
            thr[k] = entry[k]
            applied = True
    for k in entry:
        if k in thr and k not in HOST_OVERRIDABLE:
            sys.exit(f"{ERROR}: hosts.toml [{host}] overrides {k}, which is not "
                     f"host-overridable. Accuracy thresholds describe the code, not the "
                     f"machine; allowing a host to relax one would let a noisy box hide a "
                     f"real regression.")
    return thr, (host if applied else None)


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


def concordance_of(checks: list[dict]) -> dict[tuple, float]:
    """`good` per (mapper, benchmark, metric) from `concordance_<mapper>` rows,
    whose detail looks like `good=0.9633 recall=0.9988 agreement=0.9644 ref=149376`.

    `good` is the headline — the fraction of the external mapper's placements we
    reproduce. `agreement` alone is gameable by mapping less.
    """
    out = {}
    for c in checks:
        if not c["check"].startswith("concordance_"):
            continue
        mo = re.search(r"good=([0-9.]+)", c["detail"])
        if mo:
            try:
                out[(c["check"].removeprefix("concordance_"),
                     c["benchmark"], c["metric"])] = float(mo.group(1))
            except ValueError:
                pass
    return out


def ground_truth_of(checks: list[dict]) -> dict[tuple, float]:
    """ground_truth details look like `124008/125000 = 0.992064 (need 0.99)`.

    Older sets recorded only `0.9921 (need 0.99)`, so a bare leading float is
    accepted too — at lower precision, which is exactly why the format changed.
    """
    out = {}
    for c in checks:
        if c["check"] != "ground_truth":
            continue
        mo = re.search(r"=\s*([0-9.]+)", c["detail"]) or re.match(r"\s*([0-9.]+)", c["detail"])
        if mo:
            try:
                out[(c["benchmark"], c["metric"])] = float(mo.group(1))
            except ValueError:
                pass
    return out


def wrong_q60_of(checks: list[dict]) -> dict[tuple, int]:
    """wrong_q60 details look like `6/119065 = 0.000050`.

    The absolute count is what is compared, not the fraction: the denominator
    is the mapq-60 population, which moves on its own, so a rate can fall while
    the number of reads told a falsehood rises.
    """
    out = {}
    for c in checks:
        if c["check"] != "wrong_q60":
            continue
        mo = re.match(r"\s*(\d+)/", c["detail"])
        if mo:
            out[(c["benchmark"], c["metric"])] = int(mo.group(1))
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

    # Ground truth is compared against the baseline, not just against its
    # absolute floor. The floor alone cannot do this job: set tight enough to
    # catch drift it fails metrics that are working correctly, and set loose
    # enough not to, it misses the drift. Output is deterministic, so any
    # movement here is a real change in where reads are placed.
    bg, cg = ground_truth_of(base["checks"]), ground_truth_of(cand["checks"])
    for k, cv in sorted(cg.items()):
        bv = bg.get(k)
        if bv is not None and cv < bv - 1e-9:
            add(acc_level, "accuracy", f"ground_truth {k[0]}/{k[1]}",
                f"{bv:.6f} -> {cv:.6f} — reads moved away from their true positions"
                + ("; downgraded by --allow-output-change" if allow_output_change else ""))
    # Confident-but-wrong placements, same treatment and for a stronger reason:
    # accuracy can hold steady while errors migrate from mapq 0 to mapq 60, and
    # that trade is strictly bad for a caller even though ground_truth above
    # would not move. §8 records three separate tuning attempts (`-M`,
    # --rarity-weight, the mapq-gated dense substitution) that all raised
    # confidence without raising correctness, so this is a live failure mode,
    # not a hypothetical one.
    bw, cw = wrong_q60_of(base["checks"]), wrong_q60_of(cand["checks"])
    for k, cv in sorted(cw.items()):
        bv = bw.get(k)
        if bv is not None and cv > bv:
            add(acc_level, "accuracy", f"wrong_q60 {k[0]}/{k[1]}",
                f"{bv} -> {cv} — more wrong placements now claim mapq 60"
                + ("; downgraded by --allow-output-change" if allow_output_change else ""))

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

    # Concordance with the external mappers. REVIEW rather than BLOCK, and the
    # distinction is deliberate: ground truth and C++ agreement block because
    # one is truth and the other is the implementation we are a port of.
    # Winnowmap2 is neither — it is a very good estimate, so falling behind it
    # is a fact a human should see, not a fact that should stop a merge on its
    # own. Keeping up with it is the stated development goal, so a silent drop
    # would be worse than a noisy one.
    bc, cc = concordance_of(base["checks"]), concordance_of(cand["checks"])
    for k, cv in sorted(cc.items()):
        bv = bc.get(k)
        if bv is not None and cv < bv - 1e-9:
            add(REVIEW, "concordance", f"{k[0]} {k[1]}/{k[2]}",
                f"good {bv:.4f} -> {cv:.4f} — reproducing fewer of its placements")

    # -- host drift, measured against the unchanged reference implementation --
    #
    # The C++ is re-measured in every full run from a binary that does not
    # change between our commits, on the same inputs. Its movement between two
    # result sets is therefore host drift and nothing else — a free control that
    # was already being collected and thrown away.
    #
    # Without it, a run on a slightly busier machine shifts every shmap-rs row
    # together and the 3% line catches whichever benchmark was nearest it. That
    # is a false REVIEW on identical output, which is how a gate loses its
    # authority.
    drift, drift_note = 1.0, None
    bref = {k: r["wall_s"] for k, r in brows.items() if k[1] != SUBJECT and r["wall_s"] > 0}
    cref = {k: r["wall_s"] for k, r in crows.items() if k[1] != SUBJECT}
    shared = sorted(set(bref) & set(cref))
    bbin = (base["manifest"].get("binaries") or {}).get(REFERENCE_IMPL)
    cbin = (cand["manifest"].get("binaries") or {}).get(REFERENCE_IMPL)

    if not thr.get("drift_normalize", False):
        drift_note = "disabled in suite.toml"
    elif len(shared) < thr.get("drift_min_samples", 6):
        drift_note = f"only {len(shared)} reference measurements in common; not enough to trust"
    elif bbin != cbin:
        # A rebuilt reference is not a control: its own change is confounded
        # with the host's.
        drift_note = f"reference binary differs ({bbin} vs {cbin}); not a control"
    else:
        ratios = [cref[k] / bref[k] for k in shared]
        d = math.exp(statistics.fmean(math.log(x) for x in ratios))
        cap = thr.get("drift_max", 0.10)
        if abs(d - 1) > cap:
            # Large drift means the host was in a different state, not that we
            # can correct for it. Correcting a 20% shift would be inventing a
            # measurement.
            add(REVIEW, "host", "reference implementation moved",
                f"{(d-1)*100:+.1f}% on an unchanged binary over {len(shared)} measurements — "
                f"beyond the {cap*100:.0f}% correctable range, so timings are not normalised "
                f"and this run should be repeated")
            drift_note = f"{(d-1)*100:+.1f}% — too large to correct"
        else:
            drift = d
            drift_note = (f"{(d-1)*100:+.2f}% measured on the unchanged reference binary "
                          f"over {len(shared)} measurements")

    # -- wall time and memory, aggregated per (benchmark, metric) ----------
    perf: list[dict] = []
    groups: dict[tuple, list[tuple]] = {}
    for k, cr in crows.items():
        br = brows.get(k)
        if br and br["wall_s"] > 0:
            groups.setdefault((k[0], k[1], k[2]), []).append(
                (k[3], br["wall_s"], cr["wall_s"], br["peak_rss_kb"], cr["peak_rss_kb"]))

    # Mapping time judged separately from the total. A change to the mapper
    # moves `map_s`; `index_s` is a fixed cost that dilutes it — at -@16 on a 1x
    # read set the index is over half the wall, so a 10% mapper regression shows
    # up as under 5% of the total and can slip under the review line.
    map_groups: dict[tuple, list[tuple]] = {}
    for k, cr in crows.items():
        br = brows.get(k)
        if (br and k[1] == SUBJECT
                and br.get("map_s") and cr.get("map_s") and br["map_s"] > 0):
            map_groups.setdefault((k[0], k[2]), []).append((br["map_s"], cr["map_s"]))
    for (bid, metric), items in sorted(map_groups.items()):
        if len(items) < MIN_THREADS_FOR_AGGREGATE:
            continue
        gm = math.exp(statistics.fmean(math.log(c / b) for b, c in items)) / drift
        if gm - 1 > thr["wall_regression_block"]:
            add(BLOCK, "perf", f"{bid}/{metric} MAPPING time",
                f"{(gm-1)*100:+.1f}% over {len(items)} thread counts — the mapper itself, "
                f"with indexing excluded")
        elif gm - 1 > thr["wall_regression_review"]:
            add(REVIEW, "perf", f"{bid}/{metric} MAPPING time",
                f"{(gm-1)*100:+.1f}% over {len(items)} thread counts — the mapper itself, "
                f"with indexing excluded")

    for (bid, impl, metric), items in sorted(groups.items()):
        items.sort()
        wall_ratios = [c / b for _, b, c, _, _ in items]
        rss_ratios = [c / b for _, _, _, b, c in items if b > 0]
        gm_raw = math.exp(statistics.fmean(math.log(x) for x in wall_ratios))
        # The subject is corrected; the reference is what defines the
        # correction, so normalising it would flatten it to zero by
        # construction and hide the evidence.
        gm = gm_raw / drift if impl == SUBJECT else gm_raw
        # MEDIAN, not geometric mean. Peak RSS is a max-over-time statistic:
        # its run-to-run distribution is right-skewed and heavy-tailed, because
        # the peak depends on how many reads happen to be in flight when the
        # sampler fires. Measured on identical code, B04/bucket_SH moved ±2.5%
        # at -@1..8 and +28% and +30% at -@32 and -@64 — two outliers that drag
        # a geometric mean over the review line while the typical configuration
        # did not move. A median resists that; the max is reported alongside so
        # a genuine tail regression is still visible.
        rss_gm = statistics.median(rss_ratios) if rss_ratios else 1.0
        rss_max = max(rss_ratios) if rss_ratios else 1.0
        row = dict(benchmark=bid, impl=impl, metric=metric, n=len(items),
                   wall=gm - 1, wall_raw=gm_raw - 1, rss=rss_gm - 1, rss_max=rss_max - 1,
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
            add(REVIEW, "perf", f"{bid}/{metric} peak RSS",
                f"median {(rss_gm-1)*100:+.1f}% across {len(items)} thread counts "
                f"(worst {(rss_max-1)*100:+.1f}%)")

    verdict = max([f["level"] for f in findings], default=ACCEPT)
    return dict(verdict=verdict, findings=findings, notes=notes, perf=perf,
                agreement=(ba, ca), drift=drift, drift_note=drift_note)


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

    # `arch` is absent from result sets measured before the multi-architecture
    # split; fall back rather than crash on a historical baseline.
    b_arch, c_arch = bm.get("arch", "—"), cm.get("arch", "—")
    where = f"{cm['host']} ({c_arch})" if c_arch != "—" else cm["host"]

    L = [f"# Benchmark comparison on {where} — {NAMES[v]}",
         "",
         verdict_line,
         "",
         "| | baseline | candidate |",
         "|---|---|---|",
         f"| commit | `{bm['commit'][:12]}` | `{cm['commit'][:12]}` |",
         f"| suite | {bm['suite_version']} | {cm['suite_version']} |",
         f"| datasets | v{bm['dataset_version']} | v{cm['dataset_version']} |",
         f"| host | {bm['host']} | {cm['host']} |",
         f"| arch | {b_arch} | {c_arch} |",
         f"| rustc | {bm.get('rustc', '—')} | {cm.get('rustc', '—')} |",
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

    dn = res.get("drift_note")
    corrected = abs(res.get("drift", 1.0) - 1) > 1e-9
    L += ["## Wall time by benchmark",
          "",
          "Geometric mean of the per-thread-count ratios; a single thread count is too "
          "noisy to judge on its own, so the worst one is shown for context but does not "
          "decide the verdict.",
          ""]
    if corrected:
        L += [f"**Host drift: {dn}.** The reference implementation's binary does not change "
              f"between our commits, so its movement is the host's, not ours. shmap-rs rows "
              f"are divided by it; the `raw` column is before that correction and the "
              f"reference's own rows are never corrected.",
              ""]
    elif dn:
        L += [f"*No drift correction applied: {dn}.*", ""]
    L += ["| benchmark | metric | impl | threads | baseline | candidate | "
          + ("raw | corrected" if corrected else "change") + " | worst |",
          "|---|---|---|---|---|---|---|---|" + ("---|" if corrected else "")]
    for p in sorted(res["perf"], key=lambda p: (p["impl"] != SUBJECT, p["benchmark"], p["metric"])):
        flag = ""
        if p["impl"] == SUBJECT:
            if p["wall"] > thr["wall_regression_block"]:
                flag = " ⛔"
            elif p["wall"] > thr["wall_regression_review"]:
                flag = " ⚠️"
            elif p["wall"] < -thr["wall_regression_review"]:
                flag = " ✅"
        change = (f"{pct(p['wall_raw'])} | {pct(p['wall'])}{flag}" if corrected
                  else f"{pct(p['wall'])}{flag}")
        L.append(f"| {p['benchmark']} | {p['metric']} | {p['impl']} | {p['n']} | "
                 f"{p['base_s']:.1f}s | {p['cand_s']:.1f}s | {change} | "
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
          f"Rules: [VERSIONING.md](../VERSIONING.md). Thresholds from "
          f"{'`hosts.toml` for `' + res['threshold_host'] + '`' if res.get('threshold_host') else '`suite.toml`'}: "
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
        d = current_dir(cand["manifest"]["suite_version"])
        if not d.is_dir():
            print(f"no baseline at {d} — nothing to compare against.\n"
                  f"If this run is meant to become the baseline, copy it there:\n"
                  f"  cp -r {a.candidate} {d}", file=sys.stderr)
            return ERROR
        base = load_set(d)

    suite = load_suite()
    thr, thr_host = host_thresholds(suite, cand["manifest"].get("host"))

    bad = guard(base, cand)
    if bad:
        print("# Benchmark comparison — ERROR\n\nThese result sets must not be compared:\n")
        for b in bad:
            print(f"- {b}")
        return ERROR

    res = compare(base, cand, thr, a.allow_output_change)
    res["threshold_host"] = thr_host
    md = render(base, cand, res, thr)
    print(md)
    if a.out:
        Path(a.out).write_text(md + "\n")
    return res["verdict"]


if __name__ == "__main__":
    sys.exit(main())
