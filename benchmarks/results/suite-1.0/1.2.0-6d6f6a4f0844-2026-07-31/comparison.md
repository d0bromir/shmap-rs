# Benchmark comparison — ACCEPT

**ACCEPT** — no regression beyond this host's noise.

| | baseline | candidate |
|---|---|---|
| commit | `93ced641d8b9` | `6d6f6a4f0844` |
| suite | 1.0 | 1.0 |
| datasets | v1 | v1 |
| host | a2 | a2 |
| measured | 2026-07-30T22:44:32Z | 2026-07-31T12:51:43Z |
| invocations | 150 | 150 |

No blocking or reviewable differences.

## Wall time by benchmark

Geometric mean of the per-thread-count ratios; a single thread count is too noisy to judge on its own, so the worst one is shown for context but does not decide the verdict.

| benchmark | metric | impl | threads | baseline | candidate | change | worst |
|---|---|---|---|---|---|---|---|
| B01 | Containment | shmap-rs | 7 | 128.7s | 129.3s | -0.0% | +6.0% @-@4 |
| B01 | Jaccard | shmap-rs | 7 | 150.9s | 154.3s | +1.9% | +6.3% @-@64 |
| B01 | bucket_SH | shmap-rs | 7 | 115.4s | 112.6s | -1.7% | +5.4% @-@16 |
| B02 | Containment | shmap-rs | 7 | 112.0s | 113.8s | +0.8% | +3.7% @-@1 |
| B02 | Jaccard | shmap-rs | 7 | 136.2s | 139.6s | -0.2% | +6.6% @-@1 |
| B02 | bucket_SH | shmap-rs | 7 | 106.0s | 105.1s | +0.0% | +4.1% @-@16 |
| B03 | Containment | shmap-rs | 7 | 148.3s | 152.4s | +2.3% | +9.1% @-@16 |
| B03 | Jaccard | shmap-rs | 7 | 172.8s | 177.7s | +2.8% | +6.6% @-@32 |
| B03 | bucket_SH | shmap-rs | 7 | 135.2s | 133.7s | -2.2% | +2.8% @-@8 |
| B04 | Containment | shmap-rs | 7 | 1038.0s | 1047.7s | +0.0% | +3.4% @-@8 |
| B04 | Jaccard | shmap-rs | 7 | 1264.2s | 1337.3s | +2.7% | +11.6% @-@2 |
| B04 | bucket_SH | shmap-rs | 7 | 880.1s | 873.0s | +0.0% | +9.3% @-@16 |
| B05 | Containment | shmap-rs | 7 | 79.7s | 79.2s | -1.0% | +3.2% @-@4 |
| B05 | Jaccard | shmap-rs | 7 | 85.5s | 87.0s | +1.8% | +6.6% @-@16 |
| B05 | bucket_SH | shmap-rs | 7 | 77.6s | 76.5s | -1.3% | +9.7% @-@4 |
| B01 | Containment | cpp-shmap | 1 | 103.7s | 104.7s | +0.9% | +0.9% @-@1 |
| B01 | Jaccard | cpp-shmap | 1 | 131.6s | 131.6s | +0.0% | +0.0% @-@1 |
| B01 | bucket_SH | cpp-shmap | 1 | 87.1s | 86.1s | -1.1% | -1.1% @-@1 |
| B02 | Containment | cpp-shmap | 1 | 88.4s | 87.5s | -0.9% | -0.9% @-@1 |
| B02 | Jaccard | cpp-shmap | 1 | 117.8s | 117.9s | +0.1% | +0.1% @-@1 |
| B02 | bucket_SH | cpp-shmap | 1 | 81.1s | 80.9s | -0.3% | -0.3% @-@1 |
| B03 | Containment | cpp-shmap | 1 | 116.1s | 115.7s | -0.4% | -0.4% @-@1 |
| B03 | Jaccard | cpp-shmap | 1 | 144.3s | 144.3s | +0.0% | +0.0% @-@1 |
| B03 | bucket_SH | cpp-shmap | 1 | 99.0s | 99.2s | +0.2% | +0.2% @-@1 |
| B04 | Containment | cpp-shmap | 1 | 841.2s | 855.5s | +1.7% | +1.7% @-@1 |
| B04 | Jaccard | cpp-shmap | 1 | 1138.9s | 1196.5s | +5.1% | +5.1% @-@1 |
| B04 | bucket_SH | cpp-shmap | 1 | 664.4s | 660.1s | -0.6% | -0.6% @-@1 |
| B05 | Containment | cpp-shmap | 1 | 54.8s | 54.9s | +0.2% | +0.2% @-@1 |
| B05 | Jaccard | cpp-shmap | 1 | 60.4s | 61.3s | +1.6% | +1.6% @-@1 |
| B05 | bucket_SH | cpp-shmap | 1 | 54.0s | 51.8s | -4.0% | -4.0% @-@1 |

## Agreement with the C++ reference

| benchmark | metric | baseline | candidate |
|---|---|---|---|
| B01 | Containment | 0.9832 | 0.9832 |
| B01 | Jaccard | 0.9961 | 0.9961 |
| B01 | bucket_SH | 0.9813 | 0.9813 |
| B02 | Containment | 0.9864 | 0.9864 |
| B02 | Jaccard | 0.9951 | 0.9951 |
| B02 | bucket_SH | 0.9828 | 0.9828 |
| B03 | Containment | 0.9855 | 0.9855 |
| B03 | Jaccard | 0.9927 | 0.9927 |
| B03 | bucket_SH | 0.9787 | 0.9787 |
| B04 | Containment | 0.9852 | 0.9852 |
| B04 | Jaccard | 0.9925 | 0.9925 |
| B04 | bucket_SH | 0.9784 | 0.9784 |
| B05 | Containment | 0.9792 | 0.9792 |
| B05 | Jaccard | 0.9978 | 0.9978 |
| B05 | bucket_SH | 0.9818 | 0.9818 |

---

Rules: [VERSIONING.md](../VERSIONING.md). Thresholds from `suite.toml`: wall review >3%, block >10%; any drop in mapped reads, mapq-60 reads or C++ agreement blocks.

Baseline: `/home/mpiuser/shmap-rs/benchmarks/results/suite-1.0/current`  
Candidate: `/home/mpiuser/shmap-rs/benchmarks/results/suite-1.0/6d6f6a4f0844-2026-07-31`
