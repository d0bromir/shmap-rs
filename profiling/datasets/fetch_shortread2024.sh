#!/bin/bash
# Real, recent short-read WGS data for shmap-rs benchmarking, distinct from
# the Element AVITI sets the suite already uses (B06-B08) so the comparison
# is not confined to one instrument, one chemistry, or one sample.
#
# WHY THIS SET
# ------------
# The existing short-read tier (SR-AVITI2X/4X/7X) is all one instrument
# (Element AVITI), one chemistry (UltraQ), one sample (HG002), and one year
# window (2023-08 through 2024-09). This set varies the platform and the
# sample instead of the depth, so a reader can see whether the shmap-rs /
# bwa-mem2 comparison holds off the one sequencer and one genome the AVITI
# ladder is built on.
#
# Source: GIAB Cancer Genome in a Bottle HG008, the pancreatic-cancer
# tumor/normal cell line pair with a 2025 Scientific Data paper
# (doi:10.1038/s41597-025-05438-2). Specifically the normal duodenal
# tissue (HG008-N-D), whole-genome, from
#   ftp-trace.ncbi.nlm.nih.gov/ReferenceSamples/giab/data_somatic/HG008/
#       Liss_lab/BCM_Illumina-WGS_20240313/
# NovaSeq 6000, PCR-free, 2x150 paired-end, sequenced 2023-12-01 through
# 2024-03-13 (README_BCM_ILMN.md). That is comfortably inside the requested
# recent window, and is a genuinely different platform and sample than the
# AVITI sets.
#
# Only R1 is used (SINGLE-END), for the same reason fetch_aviti150.sh
# documents: shmap-rs has no paired-end mode, and running bwa-mem2 paired
# would hand it mate rescue -- a large advantage on exactly the repetitive
# regions this comparison exists to measure, and one shmap-rs structurally
# cannot use. Both tools see the identical R1 file. This understates
# bwa-mem2, and that should be said wherever the numbers appear.
#
# The single fastq pair is ~169x whole-genome (~129 GB compressed). That is
# far deeper than a useful benchmark needs to be, and downloading all of it
# just to use ~2% of it is both slow and (at 90% disk on the benchmark host)
# a genuine space risk. So this script streams the fastq straight into the
# FASTA conversion and stops the transfer as soon as the target coverage is
# reached -- the same approach fetch_aviti150.sh already uses, which is what
# makes a ~40-130 GB source file cost only a few GB and a few minutes on
# disk and on the wire.

set -uo pipefail

HS1_BASES=3117292070

BASE="https://ftp-trace.ncbi.nlm.nih.gov/ReferenceSamples/giab/data_somatic/HG008/Liss_lab/BCM_Illumina-WGS_20240313/HG008-N-D_fastqs"
R1_URL="$BASE/HV5TMDSX7-1-IDUDI0034_S1_L001_R1_001.fastq.gz"

# Default target: 1x of hs1 in bases -- the same depth B06 plays for the
# AVITI set, so this is a platform/sample counterpart at matched depth, not
# a depth confound.
TARGET_COVERAGE="${TARGET_COVERAGE:-1.0}"
TARGET_BASES=$(awk -v c="$TARGET_COVERAGE" -v b="$HS1_BASES" 'BEGIN{ printf "%d", c*b }')

OUTDIR="${OUTDIR:-$HOME/shortread2024}"
mkdir -p "$OUTDIR"

fa="$OUTDIR/reads_hg008_1x.fa"

echo "source   $R1_URL"
echo "outdir   $OUTDIR"
echo "target   ${TARGET_COVERAGE}x = $TARGET_BASES bases"

# Stream-decompress straight into FASTA, truncating read names at the first
# space (same convention as fetch_aviti150.sh: the trailing "1:N:0:<index>"
# is constant per file and costs bytes for no distinguishing power) and
# stopping the whole pipeline as soon as TARGET_BASES of sequence have been
# emitted -- the source file is ~129 GB, and only a few GB of it are needed.
# The `> ` conversion below (strip the leading `@`, prefix `>`) matters:
# a fasta parser reads a header by its `>`, and leaving the `@` in place
# (which an earlier version of this script did) produces a file no fasta
# reader sees any records in at all.
if [ -s "$fa" ]; then
    echo "[$(date +%H:%M:%S)] $fa exists; not re-downloading"
else
    echo "[$(date +%H:%M:%S)] streaming -> $fa"
    curl -sS --retry 5 --retry-delay 5 "$R1_URL" | zcat | awk -v target="$TARGET_BASES" '
        NR % 4 == 1 { split(substr($0, 2), h, " "); name = h[1] }
        NR % 4 == 2 {
            if (bases < target) { print ">" name; print $0; bases += length($0) }
            else exit
        }
        NR % 4 == 0 && bases >= target { exit }
    ' > "$fa"
    echo "[$(date +%H:%M:%S)] $fa done ($(wc -c < "$fa") bytes)"
fi

echo "[$(date +%H:%M:%S)] done; outputs in $OUTDIR"
