#!/bin/bash
# Real HG002 HiFi WGS at 1x / 3x / 10x of T2T-CHM13: shmap-rs vs the C++ shmap.
# Single-threaded, strictly sequential (never parallel), one retry per crashed run.
#
# Parameters are the paper / Table-1 real-world set. The k=15 "stress" set used
# by the 6000-read WGS comparison would need ~22 days at these depths.
set -u

D=$HOME/hifi_real
REF=$HOME/_paper_work/hs1.fa
RS=$HOME/shmap-rs/target/release/shmap
CPP=$HOME/Pesho/shmap/release/shmap
U=https://ftp-trace.ncbi.nlm.nih.gov/ReferenceSamples/giab/data/AshkenazimTrio/HG002_NA24385_son/PacBio_CCS_15kb/
ARGS="-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment"
GENOME=3117292070
MEANLEN=12853

log() { echo "[$(date '+%F %H:%M:%S')] $*"; }

mkdir -p "$D"

# ---------------- phase 1: download + convert to FASTA ----------------
if [ ! -f "$D/master.done" ]; then
    : > "$D/master.fa"
    n=0
    while read -r cell; do
        n=$((n + 1))
        log "downloading cell $n/18: $cell"
        curl -s --retry 3 --retry-delay 10 --max-time 7200 "${U}${cell}" \
            | awk 'NR%4==1 {split($0, a, " "); print ">" substr(a[1], 2)} NR%4==2 {print}' \
            >> "$D/master.fa"
        log "  master.fa now $(( $(stat -c %s "$D/master.fa") / 1000000000 )) GB"
    done < "$D/cells18.txt"
    touch "$D/master.done"
fi
log "master.fa complete: $(stat -c %s "$D/master.fa") bytes"

# ---------------- phase 2: exact-coverage subsets ----------------
for depth in 1 3 10; do
    f="$D/hifi_${depth}x.fa"
    if [ ! -f "$f" ]; then
        want=$(python3 -c "print(int($GENOME * $depth / $MEANLEN))")
        log "building ${depth}x subset = $want reads"
        awk -v w="$want" '/^>/ {n++; if (n > w) exit} {print}' "$D/master.fa" > "$f"
        log "  built: $(grep -c '^>' "$f") reads, $(stat -c %s "$f") bytes"
    fi
done

# ---------------- phase 3: the runs ----------------
run() {
    tag=$1
    shift
    log "START $tag"
    /usr/bin/time -v "$@" > "$D/$tag.paf" 2> "$D/$tag.time"
    rc=$?
    if [ $rc -ne 0 ]; then
        log "  $tag FAILED rc=$rc -- retrying once"
        /usr/bin/time -v "$@" > "$D/$tag.paf" 2> "$D/$tag.time"
        rc=$?
        [ $rc -ne 0 ] && log "  $tag FAILED AGAIN rc=$rc (giving up on this run)"
    fi
    wall=$(grep -m1 Elapsed "$D/$tag.time" | sed 's/.*: //')
    rss=$(grep -m1 Maximum "$D/$tag.time" | sed 's/.*: //')
    lines=$(grep -vc '^@' "$D/$tag.paf" 2>/dev/null || echo 0)
    log "END $tag rc=$rc wall=$wall rssKB=$rss paf_lines=$lines"
}

for depth in 1 3 10; do
    f="$D/hifi_${depth}x.fa"
    run "rs_${depth}x" "$RS" -s "$REF" -p "$f" $ARGS -@ 1 -x --profile-log "$D/rs_${depth}x.profile.json"
    run "cpp_${depth}x" "$CPP" -s "$REF" -p "$f" $ARGS
done

log "ALL RUNS DONE"
