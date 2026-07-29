#!/bin/bash
# C++ shmap vs shmap-rs (-@1, -@4) on real >=22kb HG002 HiFi reads vs whole CHM13.
set -u
D=$HOME/real24k
REF=$HOME/_paper_work/hs1.fa
READS=$D/reads_real24k.fa
RS=$HOME/shmap-rs/target/release/shmap
CPP=$HOME/Pesho/shmap/release/shmap
P="-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment"
run() {
  local tag=$1; shift
  /usr/bin/time -v -o $D/$tag.time "$@" > $D/$tag.paf 2>$D/$tag.err
  local rc=$?
  echo "$tag rc=$rc wall=$(grep -oP "Elapsed.*: \K.*" $D/$tag.time) rssKB=$(grep -oP "Maximum resident set size \(kbytes\): \K[0-9]+" $D/$tag.time) mapped=$(wc -l < $D/$tag.paf)"
}
# warm the page cache identically for every run
cat "$REF" > /dev/null; cat "$READS" > /dev/null
run rs_t1  $RS  -s $REF -p $READS $P -@ 1 -x --profile-log $D/rs_t1.json
run rs_t4  $RS  -s $REF -p $READS $P -@ 4 -x --profile-log $D/rs_t4.json
run cpp    $CPP -s $REF -p $READS $P
# determinism + cross-implementation agreement
cmp -s <(sed "s/\tt:f:[0-9.-]*//" $D/rs_t1.paf) <(sed "s/\tt:f:[0-9.-]*//" $D/rs_t4.paf) \
  && echo "shmap-rs -@1 vs -@4: IDENTICAL" || echo "shmap-rs -@1 vs -@4: DIFFERS <<<"
echo "core-PAF agreement rs vs cpp: $(comm -12 <(cut -f1-12 $D/rs_t1.paf|sort) <(cut -f1-12 $D/cpp.paf|sort)|wc -l) / $(wc -l < $D/rs_t1.paf)"
echo BENCH24K DONE
