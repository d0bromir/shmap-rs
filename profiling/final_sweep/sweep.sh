#!/bin/bash
set -u
RS=$HOME/shmap_2pass; REF=$HOME/_paper_work/hs1.fa
P="-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment"
D=$HOME/twopass_sweep; mkdir -p $D
run_set() {
  local tag=$1 reads=$2
  for t in 1 2 4 8 16 32 64; do
    /usr/bin/time -v -o $D/${tag}_t${t}.time $RS -s $REF -p $reads $P -@ $t -x --profile-log $D/${tag}_t${t}.json > $D/${tag}_t${t}.paf 2>/dev/null
    echo "$tag t=$t wall=$(grep -oP "Elapsed.*: \K.*" $D/${tag}_t${t}.time) rssKB=$(grep -oP "Maximum resident set size \(kbytes\): \K[0-9]+" $D/${tag}_t${t}.time)"
  done
  ok=1; for t in 2 4 8 16 32 64; do cmp -s <(sed "s/\tt:f:[0-9.-]*//" $D/${tag}_t1.paf) <(sed "s/\tt:f:[0-9.-]*//" $D/${tag}_t${t}.paf) || ok=0; done
  [ $ok = 1 ] && echo "$tag OUTPUT IDENTICAL across all thread counts" || echo "$tag OUTPUT DIFFERS <<<"
  rm -f $D/${tag}_t*.paf
}
run_set w24k $HOME/wgs24k/reads_24kbp_1x.fa
run_set hifi1x $HOME/hifi_real/hifi_1x.fa
run_set hifi10x $HOME/hifi_real/hifi_10x.fa
echo TWOPASS DONE
