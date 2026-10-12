# Where the time goes, and what to change

From the 2.0.0 flame graphs ([a2](../benchmarks/results/flamegraphs-2.0.0-x86_64/README.md),
[galaxy](../benchmarks/results/flamegraphs-2.0.0-aarch64/README.md)) plus four follow-up probes
([`profiling/speedup_probes.sh`](../profiling/speedup_probes.sh)). Everything here is a
**proposal with a measured ceiling, not a result**: none is implemented. Gate for any of them:
Mapped Q60 counts and the golden PAF unchanged, judged inside one binary with a runtime switch
([RESULTS.md §11](../RESULTS.md#11-what-to-try-next): comparing two builds inflates a change 3-6x).

## Where it is slow

| workload | share of CPU | what |
|---|---|---|
| short reads B06-B09 (95-99% of their wall is mapping) | **refine 48%** of `query_mapping` | 19 buckets refined per read, ~6 us each; 94% of them yield no mapping (1.1 admitted per mapped read) |
| | **bucket merge 19%** | 1 504 buckets built, sorted and walked per read; **98.6% are dead before any lookup** |
| | `match_seeds` 20% | 2 800 index hits per read, one binary search each |
| | sorting ~24% of samples | the `(i32,i32,i32)` anchor sort ~8.5%, `u64` bucket-order sort ~6%, `u32` ~4%, merge ~7% |
| long reads B01-B05 | `map_reads` closure + `match_rest` 17-55% (B04, 10x: 55%) | seeding, pruning, refine, inlined together |
| | `sketch_slice_into` 12-29% | partly the reference at `-@1` (index is 2-33% of wall, [§3b](../RESULTS.md#3b-index-vs-mapping)); load-port bound |
| shallow long reads at 16+ threads | index build 3-7 s = **43-60% of wall** | mapping is 1.3-3.0 s; B01 at `-@32`: 3.5 s index, 2.4 s mapping |

Short-read stage split, 400k real 150 bp reads, `-@1`
([`stage-short-400k.profile.json`](../benchmarks/results/flamegraphs-2.0.0-x86_64/probes/stage-short-400k.profile.json)):
`query_mapping` 99.3 s = `match_rest` 57.9 (`refine` 48.1) + `match_seeds` 20.3 + `bucket_merge` 18.8.

## Proposals, by expected payoff

### 1. Drop dead buckets before sorting them (short reads)

At the first `seed_heuristic_pass` check a bucket's bound depends only on `(seeds, matches)`,
both already final after the merge. Measured on the 400k reads
([`dead-on-arrival-400k.profile.json`](../benchmarks/results/flamegraphs-2.0.0-x86_64/probes/dead-on-arrival-400k.profile.json),
`git apply profiling/dead_on_arrival.patch`): of 601.4 M seeded buckets, **593.9 M fail that check
at `thr = theta`, 593.1 M at the exact floor `theta*(1-d)`** (98.6%). Only 7.6 M reach `refine`.

Change: while deduplicating in `Buckets::merge_entries`, keep an entry only if
`hseed(m, seeds, matches) >= theta*(1-d)`. The floor, not `theta`, because the second-best pass
runs at `best*(1-d)`, which is above `theta*(1-d)` but can be below `theta`. A bucket that fails
at the floor fails every later check (`thr` only rises), so output is unchanged by construction.
This removes the `u64` order sort, the 32-byte output records and ~99% of the `match_rest` loop.
`seeded_buckets` then counts survivors; say so in the counter's doc.

Ceiling: the `u64` sort + `get_sorted_buckets` (~9% of samples) plus most of the 10% of
`query_mapping` that `match_rest` spends outside `refine`; the radix sort and dedup remain.
**Expect up to ~15% of short-read mapping time.**
A second step on the same path: each hit pushes two entries (buckets `b` and `b-1`); counting per
half-length cell and combining neighbours halves the radix input. Exact only if the content merge
is associative, which it is, but the `min(occs_in_p)` clamp needs checking.

### 2. Make `refine` cheaper per bucket (short reads, 48% of mapping)

`best_containment_window_via_anchors` is already exact and sparse; what surrounds it is not:

- **Reuse the anchor buffer.** `collect_anchors` starts a fresh `Vec` per call and grows it
  4, 8, 16...; 7.6 M calls. Allocator frames are 3-6% of samples. Exact, a few lines.
- **Cheaper sort.** `anchors.sort_unstable_by_key(a.0)` on 12-byte tuples is the largest sort
  frame (8.5% aarch64, 7.7% x86). Each seed's hits are already ascending in `tpos`, and `tpos`
  is unique per anchor (one k-mer per position, seeds are unique), so any correct order is the
  same order: pack to `u64` and sort, or k-way merge the per-seed runs.
- **Stop searching twice.** `seed_heuristic_pass` already binary-searches every not-yet-consumed
  seed inside the bucket, and `collect_anchors` repeats it ([§11](../RESULTS.md#11-what-to-try-next)
  names this). Record `(tpos, seed_num, codir)` during pruning; only the seeds `match_seeds`
  consumed need a lookup afterwards.

Ceiling: sort + allocator is ~11-14% of samples, the fused lookups an unmeasured part of the rest.
**Expect 5-10%**; the first two are safe to try now.

### 3. Make `--index-cache` faster than rebuilding

Measured on B01's parameters, `-@32`, a2, output identical modulo the timing tag
([`index-cache-hit-b01.profile.json`](../benchmarks/results/flamegraphs-2.0.0-x86_64/probes/index-cache-hit-b01.profile.json)):

| | wall | index phase |
|---|---:|---:|
| no cache | 6.7 s | 3.1 s |
| cache build | 22.7 s | |
| **cache hit** | **21.1 s** | **18.9 s** `index_load` |

The cache is **6x slower to load than the index is to build**. `load_cached` is one thread:
BLAKE3 over the whole file, then serde-decode of the hash map, then a validation pass over every
posting. ROADMAP's "cache reuse not measured" is now measured. Fix the loader first (flat
mmap-able postings, parallel or deferred validation); only then does it pay: if load reaches ~1 s,
B01 at `-@32` goes 6.7 s -> ~3.5 s (**~1.9x**). Nothing for B04 or B06-B08, where the index is
1-12% of wall.

### 4. Not proposals

- **Large pages.** Both hosts run THP in `madvise` mode with `AnonHugePages: 0`, and nothing in the
  code madvises, so the multi-GB index runs on 4 KB pages. `MIMALLOC_LARGE_OS_PAGES=1` changed
  nothing (short mapping 8.2 vs 8.0-8.1 s, long 3.0 vs 3.0 s, output identical), but I could not
  confirm pages were granted, so this is **inconclusive**, not a negative. An explicit
  `madvise(MADV_HUGEPAGE)` on the posting arrays is the untested form.
- **Long-read sketching, SIMD, prefetch, NUMA replication, `-C target-cpu=native`**: measured and
  rejected in [§11](../RESULTS.md#11-what-to-try-next); the flame graphs do not reopen them.
  Proposal 1 does little for long reads (a read touches ~250 buckets, merge is 2-5%).

## Limits

One 400k-read subset, one host, `-@1` or `-@16`; the flame graphs are sampled at 99 Hz with frame
pointers. Percentages from the SVGs are self time. The ceilings above are upper bounds from
those shares, not predictions; proposal 1's *dead fraction* is the one directly measured number.
The probe binary carried the counter patch; nothing from it is in the tree.
