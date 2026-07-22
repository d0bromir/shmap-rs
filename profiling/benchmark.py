#!/usr/bin/env python3
"""Reproduce Table 1 of main_1.pdf across its FOUR datasets (data types).

Each dataset lives in pesho_table1/data/<name>/reads.fa and is mapped against the
reference its type requires (chrY for the Chromosome-Y rows, the whole CHM13v2.0
genome for the All-chromosomes rows). Simulated reads carry the ground truth in
their FASTA header (name!chr!start!end!strand) and are scored by Pesho's rule:

    a mapping is CORRECT iff it lands on the truth's chromosome AND overlaps the
    ground truth by > 10% of the UNITED length (IoU-style, > 0.10).

Real reads (dataset 4) have no ground truth, so Wrong Q60 = n/a and Mapped Q60 is
simply the number of reads reported with mapq = 60.

Datasets (paper Table 1, Sec. 5.1):
    1  chrY_sim_10kbp_10x   Chromosome Y, simulated 10kbp reads, 10x
    2  allchr_sim_10kbp_1x  All chromosomes, simulated 10kbp reads, 1x
    3  chrY_sim_24kbp_10x   Chromosome Y, simulated 24kbp reads, 10x
    4  allchr_real_24kbp     All chromosomes, real 24kbp HG002 reads, 1.6x (no truth)

Mappers (paper Table 1 list): minimap2, winnowmap2, blend, mapquik, map-shmap
(Pesho's ORIGINAL shmap, the tool under evaluation); optional tools auto-skip if
not installed. minshmap (our educational reimplementation) is an extra tool.

Run from WSL (all datasets):
    PYTHONPATH=~/pylib python3 pesho_table1/scripts/benchmark.py
One dataset only:
    ... benchmark.py --datasets chrY_sim_10kbp_10x

`--only` now defaults to `shmap-rs` only: the other mappers' Table 1 numbers are
already captured in results/table1_20260718-103540.csv and don't need re-running;
pass an explicit --only list (see --help) to reproduce the full comparison.

`--profile` has shmap-rs write its own per-stage-timing + per-thread + memory JSON
report (profiles/<dataset>-t<threads>.profile.json) for finding/optimizing
bottlenecks -- see shmap-rs's src/profiling.rs. No effect on the other mappers.
"""
import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent            # .../pesho_table1/scripts
T1 = HERE.parent                                  # .../pesho_table1
REALWORLD = T1.parent                             # .../realworld
MINSHMAP_DIR = REALWORLD.parent                   # .../minshmap
ROOT = MINSHMAP_DIR.parent                        # workspace root (holds shmap/)
DATA = T1 / "data"
RESULTS = T1 / "results"
PROFILES = T1 / "profiles"
DATA_RW = REALWORLD / "data_rw"
HOME = Path(os.path.expanduser("~"))

MINIMAP2 = HOME / "bin" / "minimap2"
# map-shmap == Pesho's ORIGINAL shmap project (the mapper under evaluation).
MAP_SHMAP = (ROOT / "shmap" / "release" / "shmap").resolve()
# minshmap == our minimal educational reimplementation (extra, under its own name).
MINSHMAP = (MINSHMAP_DIR / "minshmap_linux").resolve()
# shmap-rs == the Rust port of Pesho's shmap (identical CLI + parameters as map-shmap).
SHMAP_RS = Path(os.environ.get("SHMAP_RS", HOME / "shmap-rs" / "target" / "release" / "shmap"))
WINNOWMAP = Path(os.environ.get("WINNOWMAP", HOME / "bin" / "winnowmap"))
MERYL = Path(os.environ.get("MERYL", HOME / "bin" / "meryl"))
BLEND = Path(os.environ.get("BLEND", HOME / "bin" / "blend"))
MAPQUIK = Path(os.environ.get("MAPQUIK", HOME / "bin" / "mapquik"))
K8 = HOME / "bin" / "k8"
PAFTOOLS = (ROOT / "shmap" / "ext" / "paftools.js").resolve()

# Heavy I/O (ref, reads, .mmi, .paf, time file) on the fast ext4 disk; the /mnt/c
# OneDrive mount is ~20x slower and would dominate index/map timings.
WORK = HOME / "_paper_work"

# The four Table 1 datasets. ref: "chrY" -> data/_ref/chrY.fa ; "genome" -> the
# whole CHM13v2.0 recorded by 00_prepare_references.sh. real -> no ground truth.
DATASETS = [
    dict(name="chrY_sim_10kbp_10x", ref="chrY", real=False,
         title="Chromosome Y, simulated 10kbp reads with 10x coverage"),
    dict(name="allchr_sim_10kbp_1x", ref="genome", real=False,
         title="All chromosomes, simulated 10kbp reads with 1x coverage"),
    dict(name="chrY_sim_24kbp_10x", ref="chrY", real=False,
         title="Chromosome Y, simulated 24kbp reads with 10x coverage"),
    dict(name="allchr_real_24kbp", ref="genome", real=True,
         title="All chromosomes, real 24kbp reads from HG002 with 1.6x coverage"),
]

_TIME_RSS = re.compile(r"Maximum resident set size \(kbytes\):\s+(\d+)")
_TIME_WALL = re.compile(r"wall clock.*?:\s+([0-9:.]+)")
_BENCH = re.compile(
    r"index_s=(\S+)\s+map_s=(\S+)\s+reads=(\S+)\s+mapped=(\S+)\s+"
    r"index_rss_mb=(\S+)\s+peak_rss_mb=(\S+)")


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, text=True, capture_output=True, **kw)


def shquote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def timed(cmd):
    """Run `cmd` under /usr/bin/time -v; return (completed, peak_rss_mb, wall_s)."""
    tf = WORK / "_time.txt"
    r = sh(f"/usr/bin/time -v -o {tf} bash -c {shquote(cmd)}")
    txt = tf.read_text() if tf.exists() else ""
    tf.unlink(missing_ok=True)
    rss = _TIME_RSS.search(txt)
    mb = int(rss.group(1)) / 1024.0 if rss else 0.0
    return r, mb, parse_wall(txt)


def parse_wall(txt):
    m = _TIME_WALL.search(txt)
    if not m:
        return 0.0
    s = 0.0
    for p in m.group(1).split(":"):
        s = s * 60 + float(p)
    return s


def count_reads(path):
    n = 0
    with open(path) as f:
        for line in f:
            if line[:1] == ">":
                n += 1
    return n


# ---- correctness: parse truth from the read name, apply IoU > 0.10 ----
_TRUTH = re.compile(r"^(.*)!([^!]+)!(\d+)!(\d+)!([+-])$")


def parse_truth(qname):
    m = _TRUTH.match(qname)
    if not m:
        return None
    return m.group(2), int(m.group(3)), int(m.group(4))


def is_correct(qname, tchr, tstart, tend):
    truth = parse_truth(qname)
    if truth is None:
        return False
    gchr, gs, ge = truth
    if tchr != gchr:
        return False
    lo, hi = max(tstart, gs), min(tend, ge)
    overlap = max(0, hi - lo)
    union = max(tend, ge) - min(tstart, gs)
    return union > 0 and overlap / union > 0.10          # Pesho's rule


def score_paf(paf_path, total_reads, is_real, no_mapq=False):
    """Per read keep the highest (mapq, nmatch) mapping. Returns
    (mapped_q60, q60_total, wrong_q60_or_None, q_lt60_or_missed_pct).
    For real reads (no truth): mapped_q60 = #reads at mapq 60, wrong = None.

    no_mapq: for mappers (mapquik) that do not populate PAF column 12 and always
    emit mapq=0. mapquik only outputs mappings it is confident about (governed by
    its minimum chain length), so each reported mapping is treated as confident
    (Q60-equivalent) and its correctness is still judged by the IoU rule. Without
    this, filtering on mapq>=60 would discard every mapquik mapping (0 mapped)."""
    best = {}
    with open(paf_path) as f:
        for line in f:
            c = line.rstrip("\n").split("\t")
            if len(c) < 12:
                continue
            q = c[0]
            tchr, tstart, tend = c[5], int(c[7]), int(c[8])
            mapq = 60 if no_mapq else int(c[11])
            nmatch = int(c[9])
            ok = False if is_real else is_correct(q, tchr, tstart, tend)
            cur = best.get(q)
            if cur is None or (mapq, nmatch) > (cur[0], cur[1]):
                best[q] = (mapq, nmatch, ok)
    q60 = correct_q60 = 0
    for mapq, _, ok in best.values():
        if mapq >= 60:
            q60 += 1
            correct_q60 += ok
    if is_real:
        mapped_q60 = q60          # cannot verify correctness -> report all Q60
        wrong_q60 = None
    else:
        mapped_q60 = correct_q60
        wrong_q60 = q60 - correct_q60
    q_lt60_or_missed = total_reads - q60
    pct = 100.0 * q_lt60_or_missed / total_reads if total_reads else 0.0
    return mapped_q60, q60, wrong_q60, pct


# ---- mappers ----
class MapperUnavailable(Exception):
    """Raised when a mapper's binary is not installed, so it is skipped."""


def need(path):
    if not Path(path).exists():
        raise MapperUnavailable(f"{Path(path).name} not installed ({path})")
    return str(path)


def run_minimap2(ref, reads, threads, preset):
    idx = WORK / "_mm2.mmi"; paf = WORK / "_mm2.paf"
    _, mem_i, index_s = timed(f"{MINIMAP2} -x {preset} -t {threads} -d {idx} {ref}")
    _, mem_m, map_s = timed(f"{MINIMAP2} -x {preset} -t {threads} {idx} {reads} > {paf}")
    idx.unlink(missing_ok=True)
    return dict(paf=str(paf), index_s=index_s, map_s=map_s, mem_gb=max(mem_i, mem_m) / 1024.0)


def run_winnowmap2(ref, reads, wm_threads, k):
    need(WINNOWMAP); need(MERYL); need(K8); need(PAFTOOLS)
    db = WORK / "_meryl"; rep = WORK / "_rep.txt"; sam = WORK / "_wm.sam"; paf = WORK / "_wm.paf"
    shutil.rmtree(db, ignore_errors=True)
    # winnowmap2 is the paper's ONE exception to single-thread execution: Pesho's Table 1
    # footnote (†) states winnowmap2 uses a hardcoded 3-thread parallelization. To reproduce
    # his timings we run BOTH meryl and winnowmap at `wm_threads` (default 3); forcing -t 1
    # here makes winnowmap2 ~3-5x slower and non-comparable to the paper. All OTHER mappers
    # stay single-threaded.
    _, mem_i, index_s = timed(
        f"{MERYL} count k={k} threads={wm_threads} output {db} {ref} && "
        f"{MERYL} print greater-than distinct=0.9998 {db} > {rep}")
    _, mem_m, map_s = timed(
        f"{WINNOWMAP} -W {rep} -ax map-pb -t {wm_threads} {ref} {reads} > {sam}")
    sh(f"{K8} {PAFTOOLS} sam2paf {sam} > {paf}")
    shutil.rmtree(db, ignore_errors=True)
    for f in (rep, sam):
        f.unlink(missing_ok=True)
    return dict(paf=str(paf), index_s=index_s, map_s=map_s, mem_gb=max(mem_i, mem_m) / 1024.0)


def run_blend(ref, reads, threads, preset):
    need(BLEND)
    idx = WORK / "_blend.mmi"; paf = WORK / "_blend.paf"
    _, mem_i, index_s = timed(f"{BLEND} -x {preset} -t {threads} -d {idx} {ref}")
    _, mem_m, map_s = timed(f"{BLEND} -x {preset} -t {threads} {idx} {reads} > {paf}")
    idx.unlink(missing_ok=True)
    return dict(paf=str(paf), index_s=index_s, map_s=map_s, mem_gb=max(mem_i, mem_m) / 1024.0)


def _oneline_fasta(src, dst):
    """mapquik requires a single-line (unwrapped) reference: a wrapped FASTA makes it
    treat the embedded newlines as bases, which corrupts every target coordinate (a
    62.46 Mb chrY is read as 63.71 Mb, so reads land ~megabases off and score wrong).
    The mapquik authors' own scripts pre-build a ".oneline.fa" for exactly this reason.
    We unwrap once into WORK and reuse (cached per source by mtime/size)."""
    src, dst = Path(src), Path(dst)
    if dst.exists() and dst.stat().st_size > 0 and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    with open(src) as fi, open(dst, "w") as fo:
        seq = []
        for line in fi:
            if line.startswith(">"):
                if seq:
                    fo.write("".join(seq)); fo.write("\n"); seq = []
                fo.write(line if line.endswith("\n") else line + "\n")
            else:
                seq.append(line.strip())
        if seq:
            fo.write("".join(seq)); fo.write("\n")
    return dst


def run_mapquik(ref, reads, threads):
    need(MAPQUIK)
    # mapquik needs an unwrapped (single-line-per-sequence) reference; see _oneline_fasta.
    # Unwrapping is one-time input prep (as in the authors' scripts) and is NOT timed --
    # only the mapping call is measured, keeping map_sec comparable to the other mappers.
    ref_one = _oneline_fasta(ref, WORK / f"_mq_oneline_{Path(ref).name}")
    prefix = WORK / "_mapquik"
    _, mem, wall = timed(
        f"{MAPQUIK} {reads} --reference {ref_one} --threads {threads} -p {prefix}")
    return dict(paf=f"{prefix}.paf", index_s=None, map_s=wall, mem_gb=mem / 1024.0)


def run_map_shmap(ref, reads, threads, k, r_frac, theta, min_diff, max_overlap):
    """map-shmap == Pesho's ORIGINAL shmap. One sketch+map pass (no separate index).

    Uses shmap's OWN parameters (Pesho's Makefile defaults: k=25, r=0.01, t=0.4,
    d=0.075, o=0.3, Containment) -- NOT the minimap2/minshmap k/w. Feeding shmap the
    minimap2-style k=15 makes 15-mers hugely repetitive on a whole genome, which
    explodes the A* seed-heuristic search (~100-470x slower) and also hurts accuracy.
    """
    paf = WORK / "_mapshmap.paf"
    cmd = (f"{MAP_SHMAP} -s {ref} -p {reads} -k {k} -r {r_frac} -t {theta} "
           f"-d {min_diff} -o {max_overlap} -m Containment > {paf}")
    _, mem, wall = timed(cmd)
    return dict(paf=str(paf), index_s=None, map_s=wall, mem_gb=mem / 1024.0)


def run_shmap_rs(ref, reads, threads, k, r_frac, theta, min_diff, max_overlap, profile_path=None):
    """shmap-rs == the Rust port of Pesho's shmap. Identical CLI and parameters as
    map-shmap (k=25, r=0.01, t=0.4, d=0.075, o=0.3, Containment): one sketch+map
    pass, no separate index. Unlike the single-threaded C++ shmap, shmap-rs adds a
    `-@`/`--threads` flag that parallelises the mapping phase (output is byte-identical
    regardless of thread count); we pass the harness's global --threads. Auto-skips if
    the binary is not built (e.g. locally).

    `profile_path`: if given, passes shmap-rs's own `-x`/`--profile-log` so it writes
    a per-stage-timing + per-thread + memory-usage JSON report there (see
    shmap-rs's `src/profiling.rs`). `None` (the default) leaves profiling off, so
    non-profiling benchmark runs pay none of its (small) cost."""
    need(SHMAP_RS)
    paf = WORK / "_shmaprs.paf"
    profile_flags = f" -x --profile-log {profile_path}" if profile_path else ""
    cmd = (f"{SHMAP_RS} -s {ref} -p {reads} -k {k} -r {r_frac} -t {theta} "
           f"-d {min_diff} -o {max_overlap} -m Containment -@ {threads}{profile_flags} > {paf}")
    _, mem, wall = timed(cmd)
    return dict(paf=str(paf), index_s=None, map_s=wall, mem_gb=mem / 1024.0)



def run_minshmap(ref, reads, threads, k, w, theta):
    """minshmap == our minimal educational reimplementation of shmap (extra tool)."""
    paf = WORK / "_minshmap.paf"
    cmd = f"MINSHMAP_BENCH=1 {MINSHMAP} {ref} {reads} -k {k} -w {w} -t {theta} -j {threads} > {paf}"
    r, mem, wall = timed(cmd)
    m = _BENCH.search(r.stderr)
    if m:
        index_s, map_s, peak_mb = float(m.group(1)), float(m.group(2)), float(m.group(6))
    else:
        index_s, map_s, peak_mb = 0.0, wall, mem
    return dict(paf=str(paf), index_s=index_s, map_s=map_s, mem_gb=peak_mb / 1024.0)


def stage(src):
    """Copy an input file to the fast WORK dir (skip if already present, same size)."""
    src = Path(src).resolve()
    dst = WORK / src.name
    if dst.exists() and dst.stat().st_size == src.stat().st_size:
        return dst
    shutil.copy2(src, dst)
    return dst


def resolve_ref(kind):
    if kind == "chrY":
        p = DATA / "_ref" / "chrY.fa"
        if not p.exists():
            raise FileNotFoundError(f"{p} missing - run 00_prepare_references.sh")
        return p
    # whole CHM13v2.0: use the fast-disk copy recorded by 00, else data_rw/hs1.fa
    gp = DATA / "_ref" / "genome_path.txt"
    if gp.exists():
        cand = Path(gp.read_text().strip())
        if cand.exists():
            return cand
    p = DATA_RW / "hs1.fa"
    if not p.exists():
        raise FileNotFoundError("whole-genome reference missing - run 00_prepare_references.sh")
    return p


def write_csv(csv_path, rows):
    if not rows:
        return
    with open(csv_path, "w", newline="") as f:
        wtr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wtr.writeheader()
        wtr.writerows(rows)


def run_dataset(ds, args, rows, csv_path, done=None):
    done = done or set()
    reads_path = DATA / ds["name"] / "reads.fa"
    print(f"\n########## {ds['title']} ##########")
    if not reads_path.exists():
        print(f"  reads.fa missing ({reads_path}); run the matching generator. SKIPPED dataset.")
        return
    total = count_reads(reads_path)
    ref_src = resolve_ref(ds["ref"])
    print(f"staging inputs into {WORK} ... (ref={ref_src.name}, reads={total})")
    ref = str(stage(ref_src))
    reads = str(stage(reads_path))

    runs = [
        ("minimap2",   lambda: run_minimap2(ref, reads, args.threads, args.preset)),
        ("winnowmap2", lambda: run_winnowmap2(ref, reads, args.winnowmap_threads, args.k)),
        ("blend",      lambda: run_blend(ref, reads, args.threads, args.preset)),
        ("mapquik",    lambda: run_mapquik(ref, reads, args.threads)),
        ("map-shmap",  lambda: run_map_shmap(ref, reads, args.threads, args.shmap_k,
                                             args.shmap_r, args.shmap_t,
                                             args.shmap_d, args.shmap_o)),
        ("shmap-rs",   lambda: run_shmap_rs(ref, reads, args.threads, args.shmap_k,
                                            args.shmap_r, args.shmap_t,
                                            args.shmap_d, args.shmap_o,
                                            profile_path=(PROFILES / f"{ds['name']}-t{args.threads}.profile.json"
                                                          if args.profile else None))),
    ]
    if not args.no_minshmap:
        runs.append(("minshmap", lambda: run_minshmap(ref, reads, args.threads,
                                                       args.k, args.w, args.theta)))
    if args.only:
        want = {n.strip() for n in args.only.split(",")}
        runs = [(n, fn) for (n, fn) in runs if n in want]
    for name, fn in runs:
        if (ds["name"], name) in done:
            print(f"\n=== [{ds['name']}] {name} === (skip, already in resume CSV)")
            continue
        print(f"\n=== [{ds['name']}] {name} ===")
        try:
            info = fn()
        except MapperUnavailable as e:
            print(f"  SKIPPED: {e}")
            rows.append(dict(dataset=ds["name"], mapper=name, mapped_q60="n/a",
                             q_lt60_or_missed_pct="n/a", wrong_q60="n/a", index_sec="n/a",
                             map_sec="n/a", memory_gb="n/a", reads=total))
            write_csv(csv_path, rows)
            continue
        mq60, q60, wrong, pct = score_paf(info["paf"], total, ds["real"],
                                          no_mapq=(name == "mapquik"))
        index_s = info["index_s"]
        wrong_str = "n/a" if wrong is None else wrong
        row = dict(dataset=ds["name"], mapper=name, mapped_q60=mq60,
                   q_lt60_or_missed_pct=round(pct, 1), wrong_q60=wrong_str,
                   index_sec=("n/a" if index_s is None else round(index_s, 1)),
                   map_sec=round(info["map_s"], 1),
                   memory_gb=round(info["mem_gb"], 2), reads=total)
        rows.append(row)
        write_csv(csv_path, rows)
        idx_str = "n/a" if index_s is None else f"{index_s:.1f}s"
        print(f"  Mapped Q60={mq60}  Q<60/missed={pct:.1f}%  Wrong Q60={wrong_str}  "
              f"Index={idx_str}  Map={info['map_s']:.1f}s  Mem={info['mem_gb']:.2f}GB")


def main():
    ap = argparse.ArgumentParser(description="reproduce paper Table 1 across all four datasets")
    ap.add_argument("--datasets", default="all",
                    help="comma list of dataset names, or 'all' (default)")
    ap.add_argument("--threads", type=int, default=1)
    # winnowmap2 is the paper's single documented multi-thread exception: Pesho's Table 1
    # footnote (†) says winnowmap2 uses a hardcoded 3-thread parallelization. We match that
    # (default 3) so winnowmap2 timings are comparable to the paper; every other mapper is -t 1.
    ap.add_argument("--winnowmap-threads", type=int, default=3,
                    help="winnowmap2 thread count (default 3, per paper Table 1 footnote †)")
    ap.add_argument("--preset", default="map-hifi", help="minimap2/blend preset")
    ap.add_argument("--k", type=int, default=15)
    ap.add_argument("--w", type=int, default=31)
    ap.add_argument("--theta", type=float, default=0.20)
    # map-shmap (Pesho's shmap) uses its OWN parameters, decoupled from the minimap2/minshmap
    # k/w/theta above. Defaults match shmap's Makefile (k=25, r=0.01, t=0.4, d=0.075, o=0.3);
    # using the minimap2 k=15 here makes whole-genome map-shmap ~100-470x slower and less accurate.
    ap.add_argument("--shmap-k", type=int, default=25, help="map-shmap -k (default 25, per shmap Makefile)")
    ap.add_argument("--shmap-r", type=float, default=0.01, help="map-shmap -r FracMinHash ratio (default 0.01)")
    ap.add_argument("--shmap-t", type=float, default=0.4, help="map-shmap -t homology threshold (default 0.4)")
    ap.add_argument("--shmap-d", type=float, default=0.075, help="map-shmap -d min_diff (default 0.075)")
    ap.add_argument("--shmap-o", type=float, default=0.3, help="map-shmap -o max_overlap (default 0.3)")
    ap.add_argument("--no-minshmap", action="store_true",
                    help="skip our minshmap reimplementation (included by default)")
    ap.add_argument("--only", default="shmap-rs",
                    help="comma list of mapper names to run (default: shmap-rs only -- "
                         "the other mappers' Table 1 numbers are already recorded in "
                         "results/table1_20260718-103540.csv and don't need re-running; "
                         "pass e.g. --only minimap2,winnowmap2,blend,mapquik,map-shmap,"
                         "minshmap,shmap-rs to reproduce the full comparison again)")
    ap.add_argument("--resume", default=None,
                    help="path to an existing table1 CSV; skip cells already present and append to it")
    ap.add_argument("--profile", action="store_true",
                    help="have shmap-rs write its own per-stage-timing + per-thread + "
                         "memory-usage JSON profile (via -x/--profile-log) to "
                         "profiles/<dataset>-t<threads>.profile.json. No effect on the "
                         "other mappers (they have no equivalent instrumentation).")
    args = ap.parse_args()

    WORK.mkdir(exist_ok=True)
    if args.profile:
        PROFILES.mkdir(exist_ok=True)
    if args.datasets == "all":
        chosen = DATASETS
    else:
        want = {n.strip() for n in args.datasets.split(",")}
        chosen = [d for d in DATASETS if d["name"] in want]
        if not chosen:
            sys.exit(f"no matching datasets in {want}; valid: {[d['name'] for d in DATASETS]}")

    RESULTS.mkdir(exist_ok=True)
    rows = []
    done = set()
    if args.resume:
        csv_path = Path(args.resume)
        if not csv_path.is_absolute():
            csv_path = RESULTS / csv_path.name
        with open(csv_path) as f:
            for r in csv.DictReader(f):
                rows.append(r)
                done.add((r["dataset"], r["mapper"]))
        print(f"resuming {csv_path}: {len(done)} cells already done, will skip them")
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        csv_path = RESULTS / f"table1_{stamp}.csv"

    for ds in chosen:
        run_dataset(ds, args, rows, csv_path, done)

    if not rows:
        sys.exit("no results produced (generate the data first).")
    print(f"\nwrote {csv_path}")
    print_table(chosen, rows)


def print_table(chosen, rows):
    hdr = ["Mapper", "Mapped Q60", "Q<60 or missed", "Wrong Q60",
           "Index sec", "Map sec", "Memory GB"]
    print("\n# Table 1\n")
    for ds in chosen:
        drows = [r for r in rows if r["dataset"] == ds["name"]]
        if not drows:
            continue
        print(f"\n### {ds['title']}\n")
        print("| " + " | ".join(hdr) + " |")
        print("|" + "|".join(["---"] * len(hdr)) + "|")
        for r in drows:
            pct = r["q_lt60_or_missed_pct"]
            pct = pct if pct == "n/a" else f"{pct}%"
            print(f"| {r['mapper']} | {r['mapped_q60']} | {pct} | "
                  f"{r['wrong_q60']} | {r['index_sec']} | {r['map_sec']} | {r['memory_gb']} |")


if __name__ == "__main__":
    main()
