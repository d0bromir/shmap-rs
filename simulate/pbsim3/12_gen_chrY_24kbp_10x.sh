#!/usr/bin/env bash
# Type 3: Chromosome Y, simulated ~24 kbp reads, 10x coverage.
# Reference = chrY. Our HiFi sample tops out ~15 kb, so 24 kbp is produced with a
# length-controlled PacBio error model (PBSIM3 --method errhmm, ERRHMM-SEQUEL) at
# high accuracy (0.99) instead of --method sample.
set -euo pipefail
SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SD/lib_pbsim.sh"

REF="$SD/../data/_ref/chrY.fa"
OUT="$SD/../data/chrY_sim_24kbp_10x"
[ -f "$REF" ] || { echo "run 00_prepare_references.sh first (missing $REF)" >&2; exit 1; }

echo "[type 3] chrY | simulated ~24kbp (--method errhmm ERRHMM-SEQUEL, acc 0.99) | depth 10x"
REF_S="$(stage "$REF")"
gen_reads_errhmm "$REF_S" ERRHMM-SEQUEL.model 24000 4000 0.99 10 "$OUT"
echo "reads: $(grep -c '^>' "$OUT/reads.fa")  ->  $OUT/reads.fa"
