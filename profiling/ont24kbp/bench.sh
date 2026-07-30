#!/bin/bash
# ONT at ~24 kb vs the C++, all three scoring modes, plus an ONT-tuned control.
#
# Primary block uses the paper parameters (k=25) so the numbers are directly
# comparable with the HiFi tables in RESULTS.md. ONT's ~5-10% error rate means
# far fewer exact 25-mers survive, so the mapping rate is much lower than HiFi's
# -- that is a property of the data, not a failure, and both mappers face it
# equally. The k=15 block at the end shows what ONT-appropriate parameters do.
set -u
D=$HOME/ont_bench; mkdir -p "$D"
REF=$HOME/_paper_work/hs1.fa
READS=$HOME/ont24k/reads_ont24k.fa
RS=$HOME/shmap-rs/target/release/shmap
CPP=$HOME/Pesho/shmap/release/shmap

cat "$REF" > /dev/null
cat "$READS" > /dev/null
echo "dataset: $(grep -c '^>' "$READS") reads"

run () {   # run <tag> <binary> <params...>
    local tag=$1; shift
    /usr/bin/time -v -o "$D/$tag.time" "$@" > "$D/$tag.paf" 2> "$D/$tag.err"
    local n q w r
    n=$(wc -l < "$D/$tag.paf")
    q=$(awk -F'\t' '$12==60' "$D/$tag.paf" | wc -l)
    w=$(grep -oP 'Elapsed.*: \K.*' "$D/$tag.time")
    r=$(grep -oP 'Maximum resident set size \(kbytes\): \K[0-9]+' "$D/$tag.time")
    echo "$tag wall=$w rssKB=$r mapped=$n mapq60=$q"
}

BASE="-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3"
for M in Containment Jaccard bucket_SH; do
    run "${M}_rs1" $RS  -s "$REF" -p "$READS" $BASE -m "$M" -@ 1 -x --profile-log "$D/${M}_rs1.json"
    run "${M}_rs4" $RS  -s "$REF" -p "$READS" $BASE -m "$M" -@ 4 -x --profile-log "$D/${M}_rs4.json"
    run "${M}_cpp" $CPP -s "$REF" -p "$READS" $BASE -m "$M"
    if cmp -s <(sed 's/\tt:f:[0-9.-]*//' "$D/${M}_rs1.paf") <(sed 's/\tt:f:[0-9.-]*//' "$D/${M}_rs4.paf"); then
        echo "$M: rs -@1 vs -@4 IDENTICAL"
    else
        echo "$M: rs -@1 vs -@4 DIFFERS <<<"
    fi
    echo "$M: rs-vs-cpp core agreement $(comm -12 <(cut -f1-12 "$D/${M}_rs1.paf" | sort) <(cut -f1-12 "$D/${M}_cpp.paf" | sort) | wc -l) / $(wc -l < "$D/${M}_rs1.paf")"
done

# ONT-tuned control: k=15 is what the minshmap ONT benchmark uses.
ONT="-k 15 -r 0.0625 -t 0.15 -d 0.075 -o 0.3 -m Containment"
run k15_rs1 $RS  -s "$REF" -p "$READS" $ONT -@ 1 -x --profile-log "$D/k15_rs1.json"
run k15_cpp $CPP -s "$REF" -p "$READS" $ONT
echo BENCHONT DONE
