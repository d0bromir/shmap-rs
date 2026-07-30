#!/bin/bash
# Every scoring mode, not just Containment:
#   Containment  intersection / m                        (refines)
#   Jaccard      intersection / (m + s_sz - intersection) (refines)
#   bucket_SH    no refinement at all - scores the bucket straight from `sh`
#
# Reads: real HG002 HiFi >=22 kb (149 438 reads, mean 23.2 kb) vs whole CHM13.
set -u
D=$HOME/metrics_bench; mkdir -p "$D"
REF=$HOME/_paper_work/hs1.fa
READS=$HOME/real24k/reads_real24k.fa
RS=$HOME/shmap-rs/target/release/shmap
CPP=$HOME/Pesho/shmap/release/shmap
BASE="-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3"

cat "$REF" > /dev/null
cat "$READS" > /dev/null

for M in Containment Jaccard bucket_SH; do
    for tag in rs1 rs4 cpp; do
        case $tag in
            rs1) BIN=$RS;  EXTRA="-@ 1 -x --profile-log $D/${M}_${tag}.json" ;;
            rs4) BIN=$RS;  EXTRA="-@ 4 -x --profile-log $D/${M}_${tag}.json" ;;
            cpp) BIN=$CPP; EXTRA="" ;;
        esac
        /usr/bin/time -v -o "$D/${M}_${tag}.time" \
            $BIN -s "$REF" -p "$READS" $BASE -m "$M" $EXTRA \
            > "$D/${M}_${tag}.paf" 2> "$D/${M}_${tag}.err"
        n=$(wc -l < "$D/${M}_${tag}.paf")
        q=$(awk -F'\t' '$12==60' "$D/${M}_${tag}.paf" | wc -l)
        w=$(grep -oP 'Elapsed.*: \K.*' "$D/${M}_${tag}.time")
        r=$(grep -oP 'Maximum resident set size \(kbytes\): \K[0-9]+' "$D/${M}_${tag}.time")
        echo "$M $tag wall=$w rssKB=$r mapped=$n mapq60=$q"
    done
    if cmp -s <(sed 's/\tt:f:[0-9.-]*//' "$D/${M}_rs1.paf") <(sed 's/\tt:f:[0-9.-]*//' "$D/${M}_rs4.paf"); then
        echo "$M: rs -@1 vs -@4 IDENTICAL"
    else
        echo "$M: rs -@1 vs -@4 DIFFERS <<<"
    fi
    agree=$(comm -12 <(cut -f1-12 "$D/${M}_rs1.paf" | sort) <(cut -f1-12 "$D/${M}_cpp.paf" | sort) | wc -l)
    echo "$M: rs-vs-cpp core agreement $agree / $(wc -l < "$D/${M}_rs1.paf")"
done
echo METRICS DONE
