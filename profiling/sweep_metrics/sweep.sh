#!/bin/bash
# Thread sweep across all three scoring modes, on all three whole-genome sets.
set -u
D=$HOME/sweep_metrics; mkdir -p "$D"
RS=$HOME/shmap-rs/target/release/shmap
REF=$HOME/_paper_work/hs1.fa
BASE="-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3"

run_set() {
    local tag=$1 reads=$2 M=$3
    for t in 1 2 4 8 16 32 64; do
        /usr/bin/time -v -o "$D/${M}_${tag}_t${t}.time" \
            $RS -s "$REF" -p "$reads" $BASE -m "$M" -@ "$t" \
            -x --profile-log "$D/${M}_${tag}_t${t}.json" \
            > "$D/${M}_${tag}_t${t}.paf" 2>/dev/null
        echo "$M $tag t=$t wall=$(grep -oP 'Elapsed.*: \K.*' "$D/${M}_${tag}_t${t}.time") rssKB=$(grep -oP 'Maximum resident set size \(kbytes\): \K[0-9]+' "$D/${M}_${tag}_t${t}.time") mapped=$(wc -l < "$D/${M}_${tag}_t${t}.paf")"
    done
    ok=1
    for t in 2 4 8 16 32 64; do
        cmp -s <(sed 's/\tt:f:[0-9.-]*//' "$D/${M}_${tag}_t1.paf") \
               <(sed 's/\tt:f:[0-9.-]*//' "$D/${M}_${tag}_t${t}.paf") || ok=0
    done
    [ $ok = 1 ] && echo "$M $tag: IDENTICAL across thread counts" || echo "$M $tag: DIFFERS <<<"
    rm -f "$D/${M}_${tag}_t"*.paf
}

for M in Containment Jaccard bucket_SH; do
    run_set w24k    "$HOME/wgs24k/reads_24kbp_1x.fa" "$M"
    run_set hifi1x  "$HOME/hifi_real/hifi_1x.fa"     "$M"
    run_set hifi10x "$HOME/hifi_real/hifi_10x.fa"    "$M"
done
echo SWEEPMETRICS DONE
