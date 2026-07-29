#!/bin/bash
set -u
D=$HOME/a2_bench; RS=$HOME/shmap-rs/target/release/shmap
REF=$HOME/_paper_work/hs1.fa
ARGS="-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment"
for depth in 1x 10x; do
  f=$HOME/hifi_real/hifi_${depth}.fa
  for t in 1 2 4 8 16 32 64; do
    tag="sweep_${depth}_t${t}"
    /usr/bin/time -v -o "$D/$tag.time" $RS -s "$REF" -p "$f" $ARGS -@ $t \
      -x --profile-log "$D/$tag.json" > "$D/$tag.paf" 2>/dev/null
    echo "$depth t=$t wall=$(grep -oP 'Elapsed.*: \K.*' $D/$tag.time) rssKB=$(grep -oP 'Maximum resident set size \(kbytes\): \K[0-9]+' $D/$tag.time)"
  done
  # determinism: every thread count must produce identical output
  ok=1; for t in 2 4 8 16 32 64; do cmp -s <(sed 's/\tt:f:[0-9.-]*//' $D/sweep_${depth}_t1.paf) <(sed 's/\tt:f:[0-9.-]*//' $D/sweep_${depth}_t${t}.paf) || ok=0; done
  [ $ok = 1 ] && echo "$depth OUTPUT IDENTICAL across all thread counts" || echo "$depth OUTPUT DIFFERS <<<"
  rm -f $D/sweep_${depth}_t*.paf
done
echo "SWEEP DONE"
