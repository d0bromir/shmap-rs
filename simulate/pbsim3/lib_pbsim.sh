#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Shared PBSIM3 -> reads.fa pipeline for Pesho's Table 1 datasets.
#
# Exposes:
#   stage <file>                       -> echoes a fast-ext4 copy of <file>
#   gen_reads_sample <ref> <sample.fastq> <depth> <outdir> [extra pbsim args]
#   gen_reads_errhmm <ref> <model> <len_mean> <len_sd> <acc_mean> <depth> <outdir>
#
# Both write <outdir>/reads.fa whose FASTA headers carry the ground truth as
#   >S<c>_<n>!<chr>!<start>!<end>!<strand>
# produced by `paftools.js pbsim2fq`, so the benchmark can score correctness.
#
# This pbsim3 build GZIPS its MAF output and offers no --no-fastq flag, so we
# gunzip *_*.maf.gz before converting. paftools pbsim2fq only reads column 0 of
# the .fai (the contig NAME, in genome order), so a name-only list is enough.
# ---------------------------------------------------------------------------
set -euo pipefail

PBSIM="${PBSIM:-$HOME/libs/pbsim3/src/pbsim}"
PBSIM_DATA="${PBSIM_DATA:-$HOME/libs/pbsim3/data}"
K8="${K8:-$HOME/bin/k8}"

_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"          # .../pesho_table1/scripts
_MINSHMAP_DIR="$(cd "$_LIB_DIR/../../.." && pwd)"                 # .../minshmap
PAFTOOLS="${PAFTOOLS:-$_MINSHMAP_DIR/../shmap/ext/paftools.js}"

# All heavy pbsim I/O happens on the fast ext4 disk; the /mnt/c OneDrive mount is
# ~20x slower and would dominate the timings and the simulation itself.
WORK="${WORK:-$HOME/_paper_work}"
mkdir -p "$WORK"

# stage <src> : copy to the fast disk (skip if same size already there), echo path
stage() {
  local src dst
  src="$(readlink -f "$1")"
  dst="$WORK/$(basename "$src")"
  if [ ! -f "$dst" ] || [ "$(stat -c%s "$dst" 2>/dev/null)" != "$(stat -c%s "$src")" ]; then
    cp -f "$src" "$dst"
  fi
  echo "$dst"
}

# _maf_to_fasta <ref.fa> <prefix> <out_reads.fa>
_maf_to_fasta() {
  local ref="$1" prefix="$2" out="$3" g fai
  shopt -s nullglob
  for g in "$prefix"_*.maf.gz; do gunzip -f "$g"; done
  fai="$prefix.chrlist"                       # name-only contig list, in genome order
  grep '^>' "$ref" | sed 's/^>//; s/[[:space:]].*//' > "$fai"
  "$K8" "$PAFTOOLS" pbsim2fq "$fai" "$prefix"_*.maf > "$out"
  rm -f "$prefix"_*.maf "$prefix"_*.ref "$prefix"_*.fastq "$prefix"_*.fq.gz "$prefix"_*.fastq.gz "$fai"
}

# gen_reads_sample <ref.fa> <sample.fastq> <depth> <outdir> [extra pbsim args...]
# Type 1 & 2: realistic HiFi via PBSIM3 --method sample (length+error from real reads).
gen_reads_sample() {
  local ref="$1" sample="$2" depth="$3" outdir="$4"; shift 4
  local tmp; tmp="$(mktemp -d "$WORK/pbsim.XXXXXX")"
  ( cd "$tmp" && "$PBSIM" --strategy wgs --method sample --sample "$sample" \
        --genome "$ref" --depth "$depth" --prefix sim "$@" )
  mkdir -p "$outdir"
  _maf_to_fasta "$ref" "$tmp/sim" "$outdir/reads.fa"
  rm -rf "$tmp"
}

# gen_reads_errhmm <ref.fa> <model> <len_mean> <len_sd> <acc_mean> <depth> <outdir>
# Type 3: length-controlled 24 kbp reads via PBSIM3 --method errhmm (PacBio model),
# because our HiFi sample tops out ~15 kb and cannot reach 24 kb with --method sample.
gen_reads_errhmm() {
  local ref="$1" model="$2" lmean="$3" lsd="$4" acc="$5" depth="$6" outdir="$7"
  local tmp; tmp="$(mktemp -d "$WORK/pbsim.XXXXXX")"
  ( cd "$tmp" && "$PBSIM" --strategy wgs --method errhmm --errhmm "$PBSIM_DATA/$model" \
        --length-mean "$lmean" --length-sd "$lsd" --accuracy-mean "$acc" \
        --genome "$ref" --depth "$depth" --prefix sim )
  mkdir -p "$outdir"
  _maf_to_fasta "$ref" "$tmp/sim" "$outdir/reads.fa"
  rm -rf "$tmp"
}
