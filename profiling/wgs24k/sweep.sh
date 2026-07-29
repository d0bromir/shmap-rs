#!/bin/bash
set -u
D=$HOME/wgs24k; RS=$HOME/shmap-rs/target/release/shmap
REF=$HOME/_paper_work/hs1.fa; READS=$D/reads_24kbp_1x.fa
P="-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment"
for t in 1 2 4 8 16 32 64; do
  /usr/bin/time -v -o $D/t${t}.time $RS -s $REF -p $READS $P -@ $t -x --profile-log $D/t${t}.json > $D/t${t}.paf 2>/dev/null
  echo "t=$t wall=$(grep -oP "Elapsed.*: \K.*" $D/t${t}.time) rssKB=$(grep -oP "Maximum resident set size \(kbytes\): \K[0-9]+" $D/t${t}.time) mapped=$(wc -l < $D/t${t}.paf)"
done
ok=1; for t in 2 4 8 16 32 64; do cmp -s <(sed "s/\tt:f:[0-9.-]*//" $D/t1.paf) <(sed "s/\tt:f:[0-9.-]*//" $D/t${t}.paf) || ok=0; done
[ $ok = 1 ] && echo "OUTPUT IDENTICAL across all thread counts" || echo "OUTPUT DIFFERS <<<"
rm -f $D/t*.paf
echo "SWEEP24K DONE"
