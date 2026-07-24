#!/usr/bin/env python3
"""Run shmap-rs on the minshmap/realworld WGS benchmark datasets (hifi/ont/clr
long reads vs the whole T2T-CHM13 genome), producing a row-compatible CSV to
compare against that benchmark's stored `shmap` (C++) and `cpp`/`py` (minSHmap)
results in `results_rw/`.

shmap-rs has the identical CLI to the C++ shmap the benchmark already drives
(`-p <reads> -s <ref> -k -r -t -m Containment`), so this reuses the reference
benchmark's exact read-subsetting and PAF-parsing so numbers line up 1:1 — it
just points at the shmap-rs binary and adds `-@ <threads>` for the real
multithreading the C++ original doesn't have. Peak RSS via `/usr/bin/time -v`.

Usage (on the benchmark host, from realworld/ with data_rw/{hs1,hifi,ont,clr}.fa):
    python3 bench_shmaprs_wgs.py --data data_rw --out shmaprs_wgs.csv \
        --threads 4 --max-reads 6000 --datasets hifi ont clr
"""
import argparse, csv, os, re, subprocess, time
from pathlib import Path

THETA = {"hifi": 0.20, "ont": 0.15, "clr": 0.18}
_TIME_RSS_RE = re.compile(r"Maximum resident set size \(kbytes\):\s+(\d+)")


def subset_reads(src, dst, max_reads):
    """Uniform strided sample of `max_reads` records (0 = all) — verbatim from
    the reference 11_bench_3way.py so the input matches exactly."""
    total = 0
    with open(src) as f:
        for line in f:
            if line.startswith(">"):
                total += 1
    if max_reads <= 0 or max_reads >= total:
        stride, want = 1, total
    else:
        stride, want = total // max_reads, max_reads
    idx, written, keep = -1, 0, False
    with open(src) as fi, open(dst, "w") as fo:
        for line in fi:
            if line.startswith(">"):
                idx += 1
                keep = (idx % stride == 0) and written < want
                if keep:
                    written += 1
            if keep:
                fo.write(line)
    return written


def parse_paf(stdout):
    """(#mapped unique queries, mean mapq) — column 12 is mapq; shmap appends
    extra tags after it (ignored). Verbatim logic from the reference script."""
    names, mapqs = set(), []
    for line in stdout.splitlines():
        if not line or line[0] == "@":
            continue
        f = line.split("\t")
        if len(f) < 12:
            continue
        names.add(f[0])
        try:
            mapqs.append(int(f[11]))
        except ValueError:
            pass
    return len(names), (sum(mapqs) / len(mapqs) if mapqs else 0.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shmap-rs", default=os.path.expanduser("~/shmap-rs/target/release/shmap"))
    ap.add_argument("--data", default="data_rw")
    ap.add_argument("--out", default="shmaprs_wgs.csv")
    ap.add_argument("--ref", default=None, help="reference (default <data>/hs1.fa)")
    ap.add_argument("--datasets", nargs="+", default=["hifi", "ont", "clr"])
    ap.add_argument("-k", type=int, default=15)
    ap.add_argument("-w", type=int, default=31)
    ap.add_argument("--max-reads", type=int, default=6000)
    ap.add_argument("--threads", type=int, default=4)
    a = ap.parse_args()

    data = Path(a.data)
    ref = Path(a.ref) if a.ref else data / "hs1.fa"
    density = 2.0 / (a.w + 1)
    rows = []
    print(f"shmap-rs={a.shmap_rs}  ref={ref}  k={a.k} r={density:.4f} threads={a.threads} "
          f"max_reads={a.max_reads}")

    for ds in a.datasets:
        src = data / f"{ds}.fa"
        if not src.exists():
            print(f"[skip] {ds}: {src} not found"); continue
        theta = THETA.get(ds, 0.15)
        subset = data / f"_subset_{ds}.fa"
        n = subset_reads(src, subset, a.max_reads)

        cmd = ["/usr/bin/time", "-v", a.shmap_rs, "-p", str(subset), "-s", str(ref),
               "-k", str(a.k), "-r", f"{density:.6f}", "-t", str(theta),
               "-m", "Containment", "-@", str(a.threads)]
        t0 = time.perf_counter()
        r = subprocess.run(cmd, capture_output=True, text=True)
        total_s = time.perf_counter() - t0
        if r.returncode != 0:
            print(f"  ! shmap-rs rc={r.returncode}: {r.stderr.strip()[:300]}")
        mm = _TIME_RSS_RE.search(r.stderr)
        peak_mb = int(mm.group(1)) / 1024.0 if mm else 0.0
        mapped, mapq = parse_paf(r.stdout)
        pct = 100.0 * mapped / n if n else 0.0
        rps = n / total_s if total_s else 0.0
        row = dict(scope="wholegenome", dataset=ds, mapper="shmap-rs", ref=ref.name,
                   reads_in=n, mapped=mapped, map_pct=round(pct, 2),
                   mean_mapq=round(mapq, 1), total_s=round(total_s, 2),
                   peak_rss_mb=round(peak_mb, 1), map_reads_per_s=round(rps, 1),
                   k=a.k, w=a.w, density_or_r=f"{density:.4f}", theta=theta, threads=a.threads)
        rows.append(row)
        print(f"  {ds:4s} mapped={mapped:<6d} ({pct:5.2f}%) mapq={mapq:5.1f} "
              f"total={total_s:8.2f}s peak={peak_mb:.0f}MB")

    fields = ["scope", "dataset", "mapper", "ref", "reads_in", "mapped", "map_pct",
              "mean_mapq", "total_s", "peak_rss_mb", "map_reads_per_s",
              "k", "w", "density_or_r", "theta", "threads"]
    with open(a.out, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=fields)
        wr.writeheader()
        wr.writerows(rows)
    print(f"CSV -> {a.out}")


if __name__ == "__main__":
    main()
