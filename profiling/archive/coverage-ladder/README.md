# Coverage ladder: 1x → 100x whole-genome

The archived run behind [`RESULTS.md`](../../../RESULTS.md) §4 — **311.7 Gbp of reads (100x of the
human genome) in a single run**. The results themselves are in that section; this file is the
provenance, and the caveats that decide what those results are allowed to mean.

The benchmark suite tops out at 10x (B04) because a 100x ladder costs hours and told us what it had
to tell us, so this run is kept as the only evidence for the claim that nothing degrades at depth.

## Read carefully: what this is, and what it is not

**The reads are one 1.0000x whole-genome read set repeated N times**, streamed through a FIFO. That
is a hard limit of the available data, not a shortcut: the GIAB HG002 PacBio CCS 15 kb set is ~13x
of a 3.117 Gbp genome across all 18 SMRT cells, so 100x of *distinct* real HiFi reads does not exist
in it at any subsetting.

Consequences, stated plainly:

- `mapped%` and `mapq60%` being identical at every depth is **trivially expected** and is *not*
  evidence of quality holding up. Ignore those columns.
- What the repetition *does* validly measure: throughput, peak memory, per-read CPU cost, counter
  overflow, and per-read state isolation across 24.3 M reads. Those are properties of the
  implementation, not of the reads, and repetition does not weaken them.

The reads are also **simulated** (ground-truth-encoded headers), matched to the HiFi length profile
— 242,845 reads, 3,117,432,629 bases, mean 12,837 bp.

**The per-read counters are identical to two decimals at every depth and both thread counts**
(`kmers` 127.46, `seeded_buckets` 296.34, `refined_buckets` 2.74, and the rest). That is the real
payoff of a repeated input: it makes them exactly predictable, so any drift would be a bug. It
confirms the per-read `Counters` reset holds across 24.3 M reads — the C++ bug this port fixed would
show up here as unbounded growth. Nothing overflowed; the largest counter reached 1.74e12 against an
`i64` budget.

## Setup

- **Reference**: T2T-CHM13v2.0 (`hs1.fa`), 3,117,292,070 bp, 25 segments.
- **Parameters**: `-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment` — the paper / Table-1
  parameters.
- **Method**: `wgs_ladder.sh`, in this directory. Reads are `cat`ed N times into a FIFO;
  materializing the 100x point on disk would have cost ~312 GB. stdout goes to `/dev/null`, so this
  measures mapping throughput and not PAF writeout, identically at every depth.
- **Host**: an 8-core WSL2 box, 13 GB RAM. **Not** the 64-core benchmark host, so absolute times
  here are not comparable to anything in `RESULTS.md`. The depth-to-depth *trends* are the result;
  the absolute numbers are not.
- **Binary**: commit `8bc38f1`, including the `refine` memo (`RefineCache`).

## The finding worth acting on: 8 cores buy 4.25x

Mapping throughput at `-@8` is only **4.25x** the `-@1` rate, and per-read CPU cost rises from
**166.4 µs at 1 thread to ~265 µs at 8** — the same work costing 60% more CPU per read once 8
workers run concurrently. Nothing is being serialized: the per-read invariants above are identical
and `indexing` is excluded. The workers are contending for memory.

> **Partly corrected by later measurement, on better hardware.** The same A/B on the 64-core
> benchmark host found per-read CPU inflating only ~2x across 64 workers, with the ratio *falling*
> as depth rises — where this 8-core box showed 1.60x across 8. The contention does not compound the
> way an extrapolation from here would suggest; it plateaus. Read this number as evidence that
> contention exists, not as a prediction of how it scales — this box has 8 cores and one memory
> controller. The maintained diagnosis is [`RESULTS.md`](../../../RESULTS.md) §3 and §11.

## Caveat on stage mix

As a share of `query_mapping` at 100x: `match_seeds` 39.7%, `prepare` 19.6%, `match_rest` 18.5%,
`sketching` 15.0%, `bucket_merge` 6.9% — stable across depth to within a point. But this is **not**
the real-read mix: on real HiFi the same binary gives `match_seeds` ~29% and `refine` ~19%.
Simulated reads materially under-weight `refine`. Use [`RESULTS.md`](../../../RESULTS.md) §5 for
stage attribution and this directory only for scaling behaviour.

## Files

- `results.csv` — the ladder, machine-readable
- `chr21_results.csv` — the C++ head-to-head, machine-readable
- `wgs_ladder.sh`, `chr21_ladder.sh`, `chr21_clean.sh` — the exact drivers, including the FIFO setup
- `cpp_comparison.md` — head-to-head against the C++ original, run on chr21; see that file for why
  the whole genome was not possible on this host

The per-run `-x` JSON and `time -v` records were dropped: the CSVs carry every figure quoted.
