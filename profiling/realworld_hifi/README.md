# Real HG002 HiFi WGS at 1x / 3x / 10x

First benchmark of this project against **real** whole-genome long-read data at meaningful
coverage. Everything before this used 6 000-read subsets (0.02-0.07x of the genome) or simulated
reads; this is 0.24-2.4 million real PacBio CCS reads.

## Setup

- **Reads**: HG002 PacBio CCS 15 kb, 18 SMRT cells streamed from the GIAB FTP
  (`ReferenceSamples/giab/data/AshkenazimTrio/HG002_NA24385_son/PacBio_CCS_15kb/`), converted to
  FASTA and truncated to exact coverage. 41 GB of FASTA total.
- **Reference**: T2T-CHM13v2.0 (`hs1.fa`), 3 117 292 070 bp, 25 segments.
- **Parameters**: `-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment`, single-threaded.
  These are the paper / Table-1 real-world parameters. The `k=15` stress parameters used by the
  6 000-read WGS comparison would need roughly **22 days** of compute at these depths and were
  not attempted.
- **Method**: `driver.sh` in this directory. Strictly sequential, never parallel, one automatic
  retry per run. All six runs completed first time (`rc=0`, no retries used).
- Versus **C++ `shmap`** (`~/Pesho/shmap/release/shmap`), which is single-threaded by design.

### Input files

Reference:

| file | path | size | bases | segments |
|---|---|---:|---:|---:|
| `hs1.fa` | `~/_paper_work/hs1.fa` | 3.18 GB (3 179 638 084 B) | 3 117 292 070 | 25 |

Read sets (all derived from `master.fa`, 41.14 GB / 41 136 602 249 B, 18 SMRT cells):

| file | size | reads | total bases | read length mean / min / max | coverage of hs1 |
|---|---:|---:|---:|---:|---:|
| `hifi_1x.fa` | 3.12 GB (3 122 427 506 B) | 242 534 | 3 113 721 004 | 12 838 / 114 / 17 603 bp | **0.9989x** |
| `hifi_3x.fa` | 9.36 GB (9 362 987 268 B) | 727 602 | 9 336 852 851 | 12 832 / 114 / 18 106 bp | **2.9952x** |
| `hifi_10x.fa` | 31.22 GB (31 219 607 763 B) | 2 425 341 | 31 132 446 551 | 12 836 / 62 / 18 314 bp | **9.9870x** |

### Exact invocations

Verbatim from `Command being timed:` in each `/usr/bin/time -v` record. `$N` is `1`, `3` or `10`.

shmap-rs (1.1.0, `b0121aa`):

```
/home/mpiuser/shmap-rs/target/release/shmap \
    -s /home/mpiuser/_paper_work/hs1.fa \
    -p /home/mpiuser/hifi_real/hifi_${N}x.fa \
    -k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment \
    -@ 1 -x --profile-log /home/mpiuser/hifi_real/rs_${N}x.profile.json
```

C++ shmap (`~/Pesho/shmap/release/shmap`, no threading flag — single-threaded by design):

```
/home/mpiuser/Pesho/shmap/release/shmap \
    -s /home/mpiuser/_paper_work/hs1.fa \
    -p /home/mpiuser/hifi_real/hifi_${N}x.fa \
    -k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment
```

## Results

| depth | shmap-rs | C++ shmap | speedup | shmap-rs RSS | C++ RSS | memory ratio |
|---|---:|---:|---:|---:|---:|---:|
| 1x | **66.9 s** | 108.6 s | **1.62x** | **2.11 GB** | 19.30 GB | **9.15x less** |
| 3x | **177.8 s** | 264.2 s | **1.49x** | **2.35 GB** | 19.30 GB | **8.22x less** |
| 10x | **566.1 s** | 810.7 s | **1.43x** | **2.54 GB** | 19.31 GB | **7.62x less** |

Both mappers report the same number of mappings at every depth (241 991 / 725 892 / 2 419 796)
and the same mean mapq (56.41 / 56.40 / 56.36).

**shmap-rs memory is nearly flat in coverage** — 2.11 -> 2.54 GB for a 10x increase in reads,
because the index dominates and per-read state is bounded. The C++ sits at 19.3 GB regardless.

Per-read mapping cost is constant across depths (0.232 / 0.230 / 0.229 ms), i.e. throughput scales
linearly with input and nothing degrades at 10x.

## Accuracy versus the C++

Not byte-identical, and this quantifies for the first time how far apart they actually are on real
data. At 1x, 3 519 of 241 991 records (**1.45%**) differ in some column:

| difference | count | share | mapq |
|---|---:|---:|---|
| different target chromosome | 165 | 0.07% | **all mapq 0** |
| same chromosome, different coordinates | 3 354 | 1.39% | 1 990 at mapq 0, 1 363 at mapq 60, 1 at mapq 5 |

The same set of reads is mapped by both, and mean mapq is identical to two decimals.

Every cross-chromosome disagreement is on a read the mapper itself reports as **mapq 0** — i.e.
it is flagged ambiguous, and the two implementations pick different members of a genuine tie. The
coordinate disagreements have a **median shift of 11 924 bp**, which is about one bucket
(`halflen` = the read's k-mer count ~128, and one reference k-mer ~100 bp, so a bucket spans
~12.8 kb). These are adjacent-bucket ties resolving differently, which is the documented
consequence of shmap-rs using a stable sort where the C++ uses `std::sort` (see `buckets.rs`).

1 363 reads (0.56% of mappings) disagree at mapq 60, so this is not purely a low-confidence
phenomenon — worth knowing before quoting "identical output" against the C++ on real data. Against
*itself* shmap-rs remains byte-identical across thread counts.

## Where the time goes — and what to optimise next

This is the important part, and it says the recent optimisation work was aimed at a different
regime than real-world use.

Top-level breakdown of per-read work at 10x (`query_mapping` = 504.9 s; these five partition it):

| stage | 10x | share |
|---|---:|---:|
| `match_rest` | 161.0 s | **31.9%** |
| `prepare` | 131.3 s | **26.0%** |
| `match_seeds` | 130.6 s | **25.9%** |
| `sketching` | 62.2 s | 12.3% |
| `bucket_merge` | 18.2 s | **3.6%** |

Nested inside those:

- `match_rest` ⊃ `refine` **114.5 s** (71% of it), plus `match_rest_for_best` 90.9 s and
  `match_rest_for_best2` 69.4 s
- `prepare` ⊃ `seeding` 103.8 s ⊃ `collect_kmer_info` **88.9 s**, `group_kmers` 8.2 s,
  `sort_kmers` 5.5 s

**`bucket_merge` is 3.6% here.** It was 68% of mapping in the k=15 stress regime, which is what
the dense-accumulator work targeted and cut by 36x. That win is real but it barely moves this
workload. The honest read is that the k=15 whole-genome configuration is a pathological corner,
and the real-world k=25 configuration is bottlenecked somewhere else entirely.

The next targets, in order of measured cost:

1. **`refine`** — 114.5 s, 22.7% of per-read work, the single largest named stage.
2. **`collect_kmer_info`** — 88.9 s, 17.6%, and the bulk of seeding.
3. **`match_seeds`** — 130.6 s, 25.9%; already streamlined for k=15 but still substantial here.
4. **`sketching`** — 62.2 s, 12.3%; the per-read sketch, distinct from `index_sketching`.

Indexing is a fixed ~9.1 s at every depth and is irrelevant at this scale (1.6% of the 10x run).

## Files

- `results.csv` — the summary table above, machine-readable
- `rs_{1,3,10}x.profile.json` — full `-x` instrumentation reports (all timers, counters, memory marks)
- `driver.sh` — the exact script used, including download and subsetting
