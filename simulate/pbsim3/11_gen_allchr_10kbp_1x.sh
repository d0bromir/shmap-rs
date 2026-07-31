#!/usr/bin/env bash
# Type 2: All chromosomes, simulated ~10 kbp HiFi reads, 1x coverage.
# Reference = whole CHM13v2.0. Reads = PBSIM3 --method sample (same real HiFi
# profile as type 1), but --genome hs1.fa --depth 1.
set -euo pipefail
SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SD/lib_pbsim.sh"

GENPATH="$SD/../data/_ref/genome_path.txt"
[ -f "$GENPATH" ] || { echo "run 00_prepare_references.sh first (missing $GENPATH)" >&2; exit 1; }
GEN="$(cat "$GENPATH")"
SAMPLE="${HIFI_SAMPLE:-$SD/../../data_rw/hifi_sample.fastq}"
OUT="$SD/../data/allchr_sim_10kbp_1x"

echo "[type 2] all chromosomes | simulated ~10kbp HiFi (--method sample) | depth 1x"
SAMPLE_S="$(stage "$SAMPLE")"
gen_reads_sample "$GEN" "$SAMPLE_S" 1 "$OUT"
echo "reads: $(grep -c '^>' "$OUT/reads.fa")  ->  $OUT/reads.fa"
