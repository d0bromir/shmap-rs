#!/usr/bin/env python3
"""Self-test for ablation.py.

The ladder is the note's only claim about *what each optimization is worth*,
so its failure modes are worth pinning by name. What is checked here:

  - the ladder is genuinely cumulative: rung `i` ablates exactly the switches
    of rungs `i+1..`, so two adjacent rungs differ by one change and no other.
    A ladder that got this wrong would still typeset, and every step in it
    would be attributed to the wrong change;
  - the declared ladder matches the *recorded* one. The switches live on a
    never-merged branch, so there is no `src/ablate.rs` in this tree to check
    against; the committed result set is the better witness anyway, because it
    says what the measured binary was actually told to ablate at each rung;
  - every optimization in `PORT_CHANGES.md` is either a rung or declared in
    `NOT_ABLATED` with a reason -- the same both-directions check the
    optimization table gets, one document further out;
  - the derived columns say what they claim: a step is measured against the
    rung to its left, not against the baseline, and the end-to-end ratio is
    measured against the baseline, not against the rung to its left;
  - the figure and its `.tsv` cannot state different numbers;
  - and generation is a pure function of the ladder, which is what makes
    `--check` an equality test rather than a smoke test.

  python3 benchmarks/scripts/test_ablation.py
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ablation as a  # noqa: E402
from layout import REPO  # noqa: E402
from optimizations import parse as parse_optimizations  # noqa: E402

FAIL: list[str] = []


def check(name: str, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name:62} got {got!r}")
    if not ok:
        FAIL.append(f"{name}: got {got!r}, want {want!r}")


def make_ladder(d: Path, walls: dict[tuple[int, int], float],
                rss_mb: dict[tuple[int, int], float]) -> Path:
    """A result set with the numbers a test wants, in the real file format."""
    cols = (["threads", "rung", "label", "row", "switch", "stage", "ablated",
             "wall_s", "wall_min_s", "wall_max_s", "peak_rss_kb", "mapped", "paf_sha"]
            + [f"s_{s}" for s in a.STAGES])
    lines = ["# test fixture\n", "\t".join(cols) + "\n"]
    for (t, i), wall in sorted(walls.items()):
        row = {
            "threads": t, "rung": i, "label": a.rung_label(i), "row": a.rung_row(i),
            "switch": "" if i == 0 else a.LADDER[i - 1][1], "stage": a.rung_stage(i),
            "ablated": ",".join(a.switches_at(i)),
            "wall_s": f"{wall:.3f}", "wall_min_s": f"{wall - 0.1:.3f}",
            "wall_max_s": f"{wall + 0.1:.3f}",
            "peak_rss_kb": int(rss_mb[(t, i)] * 1024), "mapped": 100, "paf_sha": "deadbeefdeadbeef",
        }
        for s in a.STAGES:
            row[f"s_{s}"] = f"{wall / 2:.3f}"
        lines.append("\t".join(str(row[c]) for c in cols) + "\n")
    (d / "ladder.tsv").write_text("".join(lines))
    (d / "manifest.json").write_text(json.dumps(dict(
        schema=1, kind="ablation-ladder", host="testbox", arch="x86_64",
        cpu_model="test", cores=8, commit="0" * 40, dirty=False, rustc="rustc 1.0.0",
        repeats=3, reduce="median", threads=sorted({t for t, _ in walls}),
        params="ablation", metric="Containment", param_flags=["-k", "25"],
        datasets={"REF-X": dict(rel="ref.fa", bytes="1", records="1", bases="1")},
        identity_verified="bytes", paf_sha="deadbeefdeadbeef",
        finished="2026-01-01T00:00:00+00:00",
    ), indent=2) + "\n")
    return d


# ---------------------------------------------------------------------------

print("the ladder is cumulative")
# Rung 0 ablates everything; rung i ablates exactly the switches to its right;
# the last rung ablates nothing. Anything else and a step is not one change.
check("rung 0 ablates every switch",
      a.switches_at(0), [s for _, s, _, _ in a.LADDER])
check("the last rung ablates nothing", a.switches_at(len(a.LADDER)), [])
adjacent = [set(a.switches_at(i)) - set(a.switches_at(i + 1)) for i in range(len(a.LADDER))]
check("adjacent rungs differ by exactly one switch",
      sorted(len(s) for s in adjacent), [1] * len(a.LADDER))
check("each rung enables the switch it names",
      [next(iter(adjacent[i])) for i in range(len(a.LADDER))],
      [s for _, s, _, _ in a.LADDER])

print("the ladder and the recorded measurement agree about what was switched")
# The switches themselves live on a never-merged branch, so there is no
# src/ablate.rs here to read. The committed ladder is the better witness
# anyway: it records what the measured binary was actually told to ablate at
# each rung, so this checks the declaration against the run rather than
# against source that could drift from it.
measured, man, _ = a.load_ladder()
by_rung = {r["rung"]: r for r in measured if r["threads"] == min(x["threads"] for x in measured)}
check("rung 0 of the recorded run ablated exactly the ladder's switches",
      sorted(filter(None, by_rung[0]["ablated"].split(","))),
      sorted(s for _, s, _, _ in a.LADDER))
check("the last recorded rung ablated nothing",
      by_rung[max(by_rung)]["ablated"], "")
check("every recorded rung enables the switch the ladder says it does",
      [by_rung[i]["switch"] for i in range(1, len(a.LADDER) + 1)],
      [s for _, s, _, _ in a.LADDER])
check("the recorded rows are the ladder's rows",
      [by_rung[i]["row"] for i in range(1, len(a.LADDER) + 1)],
      [str(row) for _, _, row, _ in a.LADDER])
check("the result set names the instrumentation it was built from",
      bool(man.get("instrumentation", {}).get("commit")), True)

print("no optimization is silently missing")
opt_rows, _ = parse_optimizations()
declared = {row for _, _, row, _ in a.LADDER} | set(a.NOT_ABLATED)
check("every PORT_CHANGES.md row is a rung or declared not-a-rung",
      sorted({r["n"] for r in opt_rows} - declared), [])
check("nothing is declared that PORT_CHANGES.md does not list",
      sorted(declared - {r["n"] for r in opt_rows}), [])
check("every not-a-rung carries a reason",
      [n for n, why in a.NOT_ABLATED.items() if not why.strip()], [])

print("the derived columns measure what they name")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    n = len(a.LADDER)
    # A ladder that halves at rung 1 and is flat afterwards: the step column
    # must put the whole gain on rung 1, and the end-to-end column must carry
    # it on every rung after it. Swapping the two references (a plausible
    # slip) makes exactly one of those two wrong.
    walls = {(1, i): (100.0 if i == 0 else 50.0) for i in range(n + 1)}
    rss = {(1, i): (400.0 if i == 0 else 100.0) for i in range(n + 1)}
    walls.update({(8, i): 10.0 for i in range(n + 1)})
    rss.update({(8, i): 50.0 for i in range(n + 1)})
    make_ladder(d, walls, rss)

    # load_ladder resolves its own path from the architecture, so the fixture
    # is read here through the same parse, pointed at this directory.
    import csv
    with open(d / "ladder.tsv") as fh:
        rows = list(csv.DictReader((l for l in fh if not l.startswith("#")), delimiter="\t"))
    for r in rows:
        for k in ("threads", "rung", "peak_rss_kb", "mapped"):
            r[k] = int(r[k])
        for k in ("wall_s", "wall_min_s", "wall_max_s"):
            r[k] = float(r[k])
        for k in [f"s_{s}" for s in a.STAGES]:
            r[k] = float(r[k])
    man = json.loads((d / "manifest.json").read_text())

    _, cols, data = a.build(rows, man)
    idx = {c: i for i, c in enumerate(cols)}
    one = [row for row in data if row[idx["threads"]] == 1]
    check("step is against the previous rung, not the baseline",
          [one[1][idx["step_wall_pct"]], one[2][idx["step_wall_pct"]]], ["50.0", "0.0"])
    check("end-to-end ratio is against the baseline, not the previous rung",
          [one[1][idx["speedup_vs_rung0"]], one[2][idx["speedup_vs_rung0"]]], ["2.000", "2.000"])
    check("the baseline has no step", one[0][idx["step_wall_pct"]], "")
    check("rss ratio is against the baseline",
          one[-1][idx["rss_ratio_vs_rung0"]], "4.000")
    check("every rung names the stage its row targets",
          [row[idx["targeted_stage"]] for row in one],
          [""] + [s for _, _, _, s in a.LADDER])

    files = a.render(rows, man, "0" * 16)
    tex, tsv = files[f"{a.NAME}.tex"], files[f"{a.NAME}.tsv"]

    # The figure and its audit trail come from one build() call, so they
    # cannot disagree -- but only if the figure really is drawn from those
    # numbers, which is what this checks rather than assumes.
    missing = [row[idx["rung"]] for row in one
               if f"({row[idx['rung']]},{float(row[idx['wall_s']]):.4g})" not in tex]
    check("every wall value in the figure is the .tsv's", missing, [])
    check("the whiskers are the measured min..max, not a guess",
          all(f"-= (0,{float(row[idx['wall_s']]) - float(row[idx['wall_min_s']]):.4g})" in tex
              for row in one), True)
    check("the .tsv carries every stage column",
          all(f"s_{s}" in tsv.splitlines()[8] for s in a.STAGES), True)
    check("generation is pure (same input, same bytes)",
          a.render(rows, man, "0" * 16) == files, True)
    check("the figure declares its provenance",
          tex.splitlines()[0].startswith("% GENERATED by benchmarks/scripts/ablation.py"), True)
    check("the caption says both absences out loud",
          all(w in files[f"{a.NAME}.tex"] for w in ("row~4", "Row~8")), True)

    prov = a.provenance(rows, man, "0" * 16)
    for row, _ in a.NOT_ABLATED.items():
        check(f"provenance names row {row} as not a rung", f"**Row {row}**" in prov, True)

print()
if FAIL:
    print(f"{len(FAIL)} failure(s):")
    for f in FAIL:
        print("  " + f)
    sys.exit(1)
print("all ablation checks passed")
