#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 00 - prepare references for Table 1 (CHM13v2.0 = hs1.fa).
#   * stages the whole genome onto the fast ext4 disk (fair timings + fast sim)
#   * extracts Chromosome Y  -> data/_ref/chrY.fa      (types 1 & 3)
#   * records the whole-genome path -> data/_ref/genome_path.txt (types 2 & 4)
#
# Override the source genome with:  CHM13=/path/to/hs1.fa  bash 00_prepare_references.sh
# ---------------------------------------------------------------------------
set -euo pipefail
SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SD/lib_pbsim.sh"

DATA_RW="$(cd "$SD/../../data_rw" && pwd)"
REF_DIR="$SD/../data/_ref"
mkdir -p "$REF_DIR"

CHM13="${CHM13:-$DATA_RW/hs1.fa}"
[ -f "$CHM13" ] || { echo "ERROR: reference not found: $CHM13" >&2; exit 1; }

echo "staging CHM13v2.0 ($CHM13) onto $WORK ..."
GEN="$(stage "$CHM13")"
echo "$GEN" > "$REF_DIR/genome_path.txt"
echo "whole-genome reference (types 2 & 4): $GEN"

echo "extracting Chromosome Y -> $REF_DIR/chrY.fa ..."
awk -v n="chrY" '/^>/{p=($0==">"n)} p' "$GEN" > "$REF_DIR/chrY.fa"
nseq="$(grep -c '^>' "$REF_DIR/chrY.fa" || true)"
if [ "${nseq:-0}" -ne 1 ]; then
  echo "ERROR: expected exactly 1 chrY record, got $nseq. Contig names in genome:" >&2
  grep '^>' "$GEN" | head -30 >&2
  exit 1
fi
bp="$(grep -v '^>' "$REF_DIR/chrY.fa" | tr -d '\n' | wc -c)"
echo "OK: chrY.fa has 1 sequence, $bp bp"
