#!/usr/bin/env bash
# WGS coverage ladder for shmap-rs, 1x -> 100x against T2T-CHM13 (hs1.fa).
#
# Reads are streamed through a FIFO as N repetitions of a 1.0000x whole-genome
# read set, so the 100x point costs no disk (materializing it would be ~312 GB).
# stdout goes to /dev/null: this measures mapping throughput, not PAF writeout,
# and keeps every depth on equal footing.
set -u
cd "$(dirname "$0")"

REF=$HOME/_paper_work/hs1.fa
READS=$HOME/_paper_work/reads.fa
BIN=./shmap_final
PARAMS=(-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment)

run() {
    local depth=$1 threads=$2 tag="${1}x-t${2}"
    local fifo="reads_${tag}.fifo"
    rm -f "$fifo" "$fifo.unmapped.paf"
    mkfifo "$fifo"
    ( for _ in $(seq "$depth"); do cat "$READS"; done > "$fifo" 2>/dev/null & )
    echo "=== ${tag} starting $(date +%H:%M:%S) ==="
    /usr/bin/time -v -o "time_${tag}.txt" \
        "$BIN" -s "$REF" -p "$fifo" "${PARAMS[@]}" -@ "$threads" \
        -x --profile-log "wgs_${tag}.json" > /dev/null 2> "stderr_${tag}.txt"
    local rc=$?
    echo "=== ${tag} rc=$rc $(date +%H:%M:%S) wall=$(grep -oP 'Elapsed.*: \K.*' "time_${tag}.txt") rss=$(grep -oP 'Maximum resident set size \(kbytes\): \K[0-9]+' "time_${tag}.txt")KB ==="
    rm -f "$fifo" "$fifo.unmapped.paf"
}

run 1 1
run 1 8
run 10 8
run 30 8
run 100 8
echo "=== LADDER COMPLETE $(date +%H:%M:%S) ==="
