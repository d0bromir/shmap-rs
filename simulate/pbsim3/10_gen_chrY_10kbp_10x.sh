#!/usr/bin/env bash
# Type 1: Chromosome Y, simulated ~10 kbp HiFi reads, 10x coverage.
# Reference = chrY (from CHM13v2.0). Reads = PBSIM3 --method sample from the real
# HG002 HiFi profile (hifi_sample.fastq, mean ~12.8 kb ~ paper's "10kbp").
set -euo pipefail
SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SD/lib_pbsim.sh"

REF="$SD/../data/_ref/chrY.fa"
SAMPLE="${HIFI_SAMPLE:-$SD/../../data_rw/hifi_sample.fastq}"
OUT="$SD/../data/chrY_sim_10kbp_10x"
[ -f "$REF" ] || { echo "run 00_prepare_references.sh first (missing $REF)" >&2; exit 1; }

echo "[type 1] chrY | simulated ~10kbp HiFi (--method sample) | depth 10x"
REF_S="$(stage "$REF")"; SAMPLE_S="$(stage "$SAMPLE")"
gen_reads_sample "$REF_S" "$SAMPLE_S" 10 "$OUT"
echo "reads: $(grep -c '^>' "$OUT/reads.fa")  ->  $OUT/reads.fa"
