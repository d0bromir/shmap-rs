# Adaptive MAPQ Qualification: Not Approved

**Disposition:** adaptive placement is retired in the current source. The CLI
and library reject its flags, and its placement implementation is test-only.
The study below describes the pre-retirement binary, whose MAPQ remains 255
in the archived outputs. No confidence model was deployed.

This synthetic stress study tested whether existing adaptive evidence can support
numeric MAPQ. It **does not justify removing the experimental label or replacing
production MAPQ 255**. No mapping or scoring code was changed during this study;
the subsequent retirement disables the failed path rather than relabeling it.

## Design

- Mapper source: `442928c9fabe063f321b1d57ade3650fefa589e2`; exact binary hash in `report.json`.
- 13,200 reads on six independently generated references: 6,600 for calibration,
  6,600 held out; both default and adaptive mapping, 12 CLI invocations total.
- 200 reads per category per reference, lengths 4,096/12,000/24,000 bases before
  errors (adversarial window cases use 12,000), both strands; four mapping workers.
- Eleven categories: unique HiFi, near-repeat HiFi, identical copies, unique 3%
  and 6% error, repetitive 3% error, indel-rich reads, chimeras, absent-reference
  reads, differences outside sampled windows, and sample-preserving mosaics.
- Reads use independent substitutions/indels, not a platform-calibrated error
  model. References are small synthetic sequences, not human whole genomes.
- Model features are only emitted adaptive method, read length, anchor count,
  and sampled count. Truth category and reference identity are not predictors.
- Evidence bins are fixed in advance. MAPQ is the floored negative log10 of an
  exact binomial upper error bound, with Bonferroni adjustment across 12 cells
  and a cap of 30. Missing/out-of-scope evidence returns 255.
- The fitted model was written before held-out references were generated or mapped.
  No held-out errors were used to refit it. Scores are **offline predictions**;
  all actual adaptive PAF records retain MAPQ 255.

The strict truth criterion requires the original segment and strand and both
endpoints within 1,000 bases. Chimeras, absent-reference reads, and mosaics are
not credited as correct single-locus placements. Exact identical copies cannot
establish a unique origin and are not credited as confident adaptive placements.
This is not the looser interval-IoU criterion used in the historical WGS tables.

## Held-Out Results

Each category contains 600 held-out reads. The mapped column includes native
fallback; the adaptive columns count only fast-path placements.

| Category | Mapped | Adaptive Placements | Adaptive Errors |
| --- | ---: | ---: | ---: |
| Unique HiFi | 600 | 489 | 0 |
| Near-repeat HiFi | 600 | 9 | 0 |
| Identical repeats | 600 | 0 | 0 |
| Unique 3% error | 528 | 31 | 0 |
| Unique 6% error | 1 | 0 | 0 |
| Repeat 3% error | 525 | 1 | 0 |
| Indel-rich | 596 | 152 | 0 |
| Chimera | 594 | 0 | 0 |
| Absent reference | 0 | 0 | 0 |
| Unsampled differences | 600 | 0 | 0 |
| Sample-preserving mosaic | 600 | 591 | 591 |

The mosaic case preserves the windows sampled at all three expansion widths,
but replaces intervening sequence with sequence from another locus. The fast
path accepted 591 of 600 mosaics as full-read placements. This is an adversarial
failure mode, **not an estimate of real-read error prevalence**. Default mapping
also emits single-locus mappings for these mosaics; switching to fallback alone
does not establish correct split-read handling.

The model assigned all 591 errors MAPQ 0. Its held-out score distribution was:

| Offline MAPQ | Placements | Errors |
| --- | ---: | ---: |
| 0 | 667 | 591 |
| 2 | 9 | 0 |
| 11 | 224 | 0 |
| 13 | 239 | 0 |
| 14 | 134 | 0 |

Of 36 predeclared category/threshold checks (MAPQ 10/20/30), three passed their
nominal bounds, three were not demonstrated, and 30 had no qualifying evidence.
Near-repeat HiFi, unique 3% error, and repeat 3% error have only 8, 31, and 1
held-out placements at MAPQ >=10; their adjusted upper error bounds are 0.561,
0.191, and 0.999, above the required 0.1. No MAPQ >=20 was issued.

High sampled survival earns low confidence in this deliberately adversarial
mixture because it also describes mosaics. That dependence on the simulated
mixture prevents interpreting these fitted probabilities as platform-independent
MAPQ. Reads overlap within each reference and are correlated; the nominal
binomial bounds are therefore screening diagnostics, not a production guarantee.

## Published Metadata and Local Evidence

`report.json` contains commands, binary/simulator hashes, per-category default
and adaptive results, independent reference hashes, and held-out checks.
Only this documentation and the report are published. Experimental source,
tests, fitted model, per-read record files, and raw data are not committed.
The model, record files, and raw PAF/truth/stderr archive are retained locally
outside the checkout in `/home/dobro/shmap-experiment-archive/adaptive-mapq-442928c/`.
They are historical evidence, not a supported runtime model. Other local
experiment outputs, including generated reads and references, are moved from
the build directory to a separate local archive before compiled artifacts are
cleaned.

Before cleanup, the frozen fit and held-out evaluation were reconstructed from
the retained records and their hashes and independent references verified.
Seven offline statistical/scoring tests also passed. No external mapper was run.
The recorded commands describe the historical run, not runnable instructions for
the current checkout: without the unpublished generator, hashes alone do not
provide independent reproduction of the synthetic inputs.

## Required Before Promotion

1. Add evidence from outside the fixed sampled windows, or a validated way to
   reject mosaics/split reads before assigning full-read confidence.
2. Calibrate on broader reference families and realistic repeat annotations,
   with platform-specific error structure and independent held-out genomes.
3. Establish calibration and retained mapping coverage separately in near-repeat,
   noisy, and indel-rich strata. No predictions is not a successful accuracy gate.
4. Test numeric MAPQ integration, parameter/model compatibility, serialization,
   diagnostic counters, determinism, and throughput before deployment.

Historical PAFs and charts must keep their original MAPQ and experimental labels;
later qualification cannot retroactively change the algorithm that produced them.