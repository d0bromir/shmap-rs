#!/bin/bash
# Real HG002 Illumina-class short reads: GIAB Element AVITI 2x150, UltraQ
# chemistry, sequenced 2023-08-24 through 2024-09-20.
#
# WHY THIS SET
# ------------
# The suite had no short reads at all, so bwa-mem2 — a short-read aligner —
# could only ever be measured outside its design range, and shmap-rs could only
# ever be measured inside its own. Neither tells a reader much. These three
# subsets are the other half of that comparison.
#
# HG002, deliberately: it is the same sample as D1-HIFI23K, D3-HIFI1X,
# D4-HIFI10X and D6-ONT24K, so a short-read row and a long-read row differ in
# the READS and not in the genome underneath them. AVITI rather than an older
# HiSeq/NovaSeq set because it was sequenced in the last two years and is a
# current instrument; the GIAB 2x250 Illumina sets are from 2016.
#
# SINGLE-END, R1 ONLY
# -------------------
# shmap-rs has no paired-end mode. Running bwa-mem2 paired would give it mate
# rescue — placing a read from its mate's position when its own sequence is
# ambiguous — which is a large accuracy advantage on exactly the repetitive
# regions the comparison is about, and one shmap-rs structurally cannot use.
# So R1 alone, and both tools see the identical file. This UNDERSTATES what
# bwa-mem2 does in production, and that should be said wherever these numbers
# appear rather than left for a reader to notice.
#
# THREE NESTED SUBSETS, ONE DOWNLOAD
# ----------------------------------
# 2x, 4x and 7x of hs1. They are prefixes of one stream, so the 2x file is the
# first 2x of the 4x file, which is the first 4x of the 7x file — the depth
# ladder varies depth and NOTHING else, the same property that makes B03/B04
# readable. Downloading three independent samples would confound depth with
# which reads were drawn.
#
# Coverage is counted in BASES against hs1 (3 117 292 070 bp), matching how
# D3-HIFI1X and D4-HIFI10X were cut, so "1x" means the same thing across the
# whole registry.
set -uo pipefail

SRC="https://ftp-trace.ncbi.nlm.nih.gov/ReferenceSamples/giab/data/AshkenazimTrio/HG002_NA24385_son/Element_AVITI_20240920/HG002_StdInsert_Fastqs/GAT-APP-C138_R1.fastq.gz"
HS1_BASES=3117292070
OUTDIR="${OUTDIR:-$HOME/aviti150}"
DEEPEST=7          # the file actually downloaded; the others are cut from it
LADDER="2 4 7"

mkdir -p "$OUTDIR"
deep_fa="$OUTDIR/reads_aviti_${DEEPEST}x.fa"
target_bases=$(( HS1_BASES * DEEPEST ))

echo "source   $SRC"
echo "outdir   $OUTDIR"
echo "deepest  ${DEEPEST}x = $target_bases bases"

if [ -s "$deep_fa" ]; then
    echo "[$(date +%H:%M:%S)] $deep_fa exists; not re-downloading"
else
    echo "[$(date +%H:%M:%S)] streaming; stops as soon as ${DEEPEST}x is reached"
    # Read names are truncated at the first space: the trailing
    # "1:N:0:<index>" is the same for every record in the file, so it costs
    # ~25 bytes a read and distinguishes nothing. concordance.py joins on this
    # field, and bwa-mem2 truncates SAM QNAME at whitespace anyway — keeping it
    # would make the two tools' read names disagree.
    curl -fsSL --retry 5 --retry-delay 10 "$SRC" \
      | zcat \
      | awk -v target="$target_bases" -v out="$deep_fa" '
          BEGIN { bases = 0; n = 0 }
          NR % 4 == 1 { split(substr($0, 2), h, " "); name = h[1] }
          NR % 4 == 2 {
              print ">" name "\n" $0 > out
              bases += length($0); n++
              if (n % 5000000 == 0)
                  printf "  %d reads, %.2f Gbp (%.2fx)\n", n, bases/1e9, bases/3117292070 \
                      > "/dev/stderr"
              if (bases >= target) {
                  printf "reached %.4fx at %d reads\n", bases/3117292070, n > "/dev/stderr"
                  exit 0
              }
          }'
    rc=$?
    # SIGPIPE on curl is expected: awk exits the moment the target is reached
    # and the remaining ~90% of a 77 GB file is never transferred.
    if [ ! -s "$deep_fa" ]; then
        echo "download produced nothing (rc=$rc)" >&2
        exit 1
    fi
fi

# --- cut the shallower rungs as prefixes -----------------------------------
for cov in $LADDER; do
    [ "$cov" = "$DEEPEST" ] && continue
    out="$OUTDIR/reads_aviti_${cov}x.fa"
    if [ -s "$out" ]; then
        echo "[$(date +%H:%M:%S)] ${cov}x exists; skipping"
        continue
    fi
    echo "[$(date +%H:%M:%S)] cutting ${cov}x"
    awk -v target=$(( HS1_BASES * cov )) '
        NR % 2 == 0 { bases += length($0) }
        { print }
        NR % 2 == 0 && bases >= target { exit 0 }' "$deep_fa" > "$out"
done

# --- the identity triple datasets.tsv needs --------------------------------
echo
printf '%-22s %14s %12s %14s %8s %10s\n' id bytes records bases mean_len coverage
for cov in $LADDER; do
    f="$OUTDIR/reads_aviti_${cov}x.fa"
    [ -s "$f" ] || continue
    awk -v f="$f" -v cov="$cov" '
        NR % 2 == 0 { n++; b += length($0) }
        END {
            cmd = "stat -c %s \"" f "\""; cmd | getline sz; close(cmd)
            printf "%-22s %14d %12d %14d %8d %9.4fx\n", \
                   "SR-AVITI" cov "X", sz, n, b, b/n, b/3117292070
        }' "$f"
done
echo FETCHAVITI DONE
