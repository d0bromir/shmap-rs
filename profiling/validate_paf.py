#!/usr/bin/env python3
"""Logical validation of shmap PAF output.

Byte-identical output against a previous build only proves nothing changed.
This checks the output is internally consistent and semantically plausible:
structural invariants, score invariants that follow from the score definitions,
and -- where the reads carry ground truth in their header -- that reads land
where they actually came from.

usage: validate_paf.py <paf> [--ref-lengths name=len,...] [--truth]
"""
import re
import sys

paf = sys.argv[1]
want_truth = "--truth" in sys.argv
reflen = {}
for a in sys.argv[2:]:
    if a.startswith("--ref-lengths="):
        for kv in a.split("=", 1)[1].split(","):
            k, v = kv.rsplit(":", 1)
            reflen[k] = int(v)

TRUTH = re.compile(r"^S\d+_\d+!([^!]+)!(\d+)!(\d+)!([+-])")
tag = lambda fields, key: next((f.split(":")[-1] for f in fields if f.startswith(key)), None)

n = 0
fail = {}
truth_ok = truth_bad = truth_absent = 0
mapq = {}
spans = []


def bad(kind, line_no, detail):
    fail.setdefault(kind, []).append((line_no, detail))


for i, line in enumerate(open(paf), 1):
    f = line.rstrip("\n").split("\t")
    if len(f) < 12:
        bad("short record", i, f"{len(f)} fields")
        continue
    n += 1
    qname, qlen, qs, qe, strand, tname, tlen, ts, te, nmatch, blen, mq = f[:12]
    qlen, qs, qe, tlen, ts, te, mq = int(qlen), int(qs), int(qe), int(tlen), int(ts), int(te), int(mq)

    # --- structural ---
    if not (0 <= qs < qe <= qlen):
        bad("query coords", i, f"{qs}..{qe} of {qlen}")
    if not (0 <= ts <= te):
        bad("target coords", i, f"{ts}..{te}")
    if te >= tlen:
        bad("target beyond segment", i, f"{te} >= {tlen}")
    if strand not in "+-":
        bad("strand", i, strand)
    if not (0 <= mq <= 60):
        bad("mapq range", i, str(mq))
    if reflen and tname in reflen and tlen != reflen[tname]:
        bad("segment length mismatch", i, f"{tname} {tlen} != {reflen[tname]}")
    span = te - ts + 1
    spans.append(span / qlen)
    if span > 10 * qlen:
        bad("span >> read", i, f"span {span} vs read {qlen}")

    # --- score invariants, from the definitions in mapping_score/hseed ---
    J, J2, sh = tag(f, "J:f:"), tag(f, "J2:f:"), tag(f, "sh:f:")
    if J is not None:
        Jv = float(J)
        if not (-0.001 <= Jv <= 1.001):
            bad("J out of [0,1]", i, J)
        if J2 is not None:
            J2v = float(J2)
            if J2v > Jv + 1e-9:
                bad("J2 > J", i, f"{J2} > {J}")
        if sh is not None:
            shv = float(sh)
            # sh is an upper bound on the achievable score; if it were ever
            # below J, pruning could discard a bucket that actually scores.
            if shv + 1e-9 < Jv:
                bad("sh < J (pruning unsound)", i, f"sh={sh} J={J}")

    mapq[mq] = mapq.get(mq, 0) + 1

    # --- ground truth, where the read header carries it ---
    m = TRUTH.match(qname)
    if m:
        gchrom, gstart, gend = m.group(1), int(m.group(2)), int(m.group(3))
        if tname != gchrom:
            truth_bad += 1
        else:
            # one bucket is ~ the read's own half-length; allow a read-length slack
            if min(abs(ts - gstart), abs(te - gend)) <= qlen:
                truth_ok += 1
            else:
                truth_bad += 1
    else:
        truth_absent += 1

print(f"records: {n}")
print(f"mapq: 60={mapq.get(60,0)}  0={mapq.get(0,0)}  other={n - mapq.get(60,0) - mapq.get(0,0)}")
if spans:
    spans.sort()
    print(f"span/readlen: median={spans[len(spans)//2]:.2f}  p99={spans[int(len(spans)*0.99)]:.2f}  max={spans[-1]:.2f}")
if want_truth:
    tot = truth_ok + truth_bad
    if tot:
        print(f"ground truth: {truth_ok}/{tot} within one read length ({truth_ok/tot*100:.2f}%), {truth_bad} wrong")
    if truth_absent:
        print(f"  ({truth_absent} records had no ground-truth header)")
if fail:
    print("\nINVARIANT VIOLATIONS:")
    for k, v in fail.items():
        print(f"  {k}: {len(v)}  e.g. line {v[0][0]}: {v[0][1]}")
    sys.exit(1)
print("\nall structural and score invariants hold")
