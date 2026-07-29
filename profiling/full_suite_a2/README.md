# Full suite on the benchmark host: tiny → 13.2x real WGS

Everything shmap-rs is benchmarked against, small and large, re-measured on the
64-core benchmark host at `f85d9a2` — the first numbers taken there since the
`refine` memo and the preceding allocation work landed.

This supersedes `../realworld_hifi/` as the current figures. That directory
stays as the record of what was measured at `b0121aa`.

## Setup

- **Host**: a2 — 64-core AVX-512, 376 GB RAM, Ubuntu 24.04.3, idle (load ~0.03).
  Runs are strictly sequential, never parallel with each other.
- **Parameters**: `-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment`.
- **Method**: `a2_suite.sh`, same shape as `../realworld_hifi/driver.sh` —
  `/usr/bin/time -v`, PAF written to a real file (not `/dev/null`).
- **C++**: `~/Pesho/shmap/release/shmap`, single-threaded by design. Verified
  **not** Tracy-instrumented: zero live Tracy strings, against 123 in a known
  instrumented build; size 3,830,576 B matches a clean build (6,189,384 B
  instrumented). Its timings here reproduce `../realworld_hifi/` to within 0.5%,
  which is a useful cross-check that the host has not drifted.

## Small and tiny

| tier | reference | reads | shmap-rs `-@1` | shmap-rs `-@64` | C++ | rs RSS | C++ RSS |
|---|---|---|---:|---:|---:|---:|---:|
| tiny | 101 KB | 2.8 MB | 0.40 s | — | **0.09 s** | 3.0 MB | 5.0 MB |
| small | chrY, 63.7 MB | 25.7 MB | **0.61 s** | 0.29 s | 0.77 s | 135 MB | 383 MB |

**The C++ wins the tiny tier, 0.09 s against 0.40 s.** That is fixed startup
cost, not throughput, and it is worth knowing before quoting a blanket speedup:
on a 101 KB reference shmap-rs spends most of its wall time on process and
pipeline setup that the depth tiers amortize to nothing. By chrY the ordering
has already reversed.

## Big: real HG002 HiFi against the whole T2T-CHM13 genome

`hifi_{1,3,10}x.fa` are exact-coverage subsets; `master.fa` is all 18 SMRT cells
— **13.160x**, 3,168,388 reads, 41.0 Gbp, the entire distinct real read set that
exists for this sample.

Single-threaded (`-@1`), the like-for-like column since the C++ has no threading:

| depth | reads | shmap-rs | C++ | speedup | rs RSS | C++ RSS | memory |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0.999x | 242,534 | **53.05 s** | 108.16 s | **2.04x** | 2.30 GB | 18.85 GB | 8.2x less |
| 2.995x | 727,602 | **136.57 s** | 263.91 s | **1.93x** | 1.98 GB | 18.85 GB | 9.5x less |
| 9.987x | 2,425,341 | **430.56 s** | 813.60 s | **1.89x** | 2.17 GB | 18.85 GB | 8.7x less |
| 13.160x | 3,168,388 | **559.46 s** | 1059.39 s | **1.89x** | 1.96 GB | 18.85 GB | 9.6x less |

Mapping counts are identical between the two implementations at every depth
(241,991 / 725,892 / 2,419,796 / 3,161,136). Per-read cost is flat in coverage
(160.6 / 158.0 / 158.8 / 158.5 µs) and so is peak memory, while the C++ sits at
18.85 GB regardless.

### The published speedups were understated

`../realworld_hifi/` reports 1.62x / 1.49x / 1.43x. This run gives **2.04x /
1.93x / 1.89x**. The C++ side is unchanged (108.16 vs 108.6, 263.91 vs 264.2,
813.60 vs 810.7), so the whole difference is on our side: shmap-rs went from
66.9 / 177.8 / 566.1 s to 53.05 / 136.57 / 430.56 s, **21-24% faster**. Roughly
10 points of that is the `refine` memo; the rest is the inline `PMatches`,
allocation-free `Timers`/`Counters` and dense `diff_hist` work that was
uncommitted when the older numbers were taken.

## The `refine` memo, measured against its own A/B

`SHMAP_NO_REFINE_MEMO=1` disables the memo, so both columns come from one
binary with nothing else differing:

| depth | `match_rest_for_best2` | `refine` | `match_rest` | `query_mapping` | wall | scoring calls memoized |
|---|---:|---:|---:|---:|---:|---:|
| 1x | −66.4% | −39.2% | −26.4% | −9.6% | −7.4% | 44% |
| 3x | −67.1% | −41.3% | −28.5% | −10.5% | −8.7% | 44% |
| 10x | −66.5% | −39.8% | −27.2% | −9.4% | −8.5% | 44% |
| 13x | −66.5% | −39.5% | −27.2% | −9.9% | −8.8% | 44% |

Flat to within a point across a thirteenfold range of input, and PAF output is
byte-identical at every depth (checked in-run by `a2_suite.sh`). Wall-clock gain
is smaller than the `query_mapping` gain because wall includes the ~9-10 s index
build, which the memo does not touch.

## Threading

| depth | `-@1` | `-@64` | wall speedup | per-read CPU inflation |
|---|---:|---:|---:|---:|
| 1x | 53.05 s | 15.53 s | 3.42x | 2.22x |
| 3x | 136.57 s | 23.70 s | 5.76x | 2.08x |
| 10x | 430.56 s | 47.40 s | 9.08x | 1.96x |
| 13x | 559.46 s | 58.47 s | 9.57x | 1.96x |

Wall speedup climbs with depth purely because the fixed index build amortizes.

**Per-read CPU cost inflates only ~2x across 64 workers**, and that ratio
*falls* as depth rises. This corrects a claim made from an 8-core box in
`../coverage_ladder/README.md`, where 1.60x inflation across 8 workers was read
as memory-bandwidth saturation that would keep compounding. It does not compound:
64 workers cost about the same per-read multiple as 8. Memory contention is real
but it plateaus, and it accounts for only about half the gap between 64 cores and
the 9.6x actually achieved. The remaining loss is not per-read work at all —
candidates are the single serial collector, which reorders and writes every
read's output, and the index build itself, which is 10 s of the 58 s at 13x.
That is where threading effort should go, and it is a different target than the
per-read prefetching the 8-core result pointed at.

## Files

- `results.csv` — the big-tier table, machine-readable
- `rs_*.json`, `tiny_rs1.json`, `small_rs1.json` — full `-x` reports
  (`_nomemo` = memo disabled, `_t64` = 64 threads)
- `*.time` — `/usr/bin/time -v` records
- `a2_suite.sh` — the exact driver
- `suite.log` — the run log, including the per-depth memo output checks
