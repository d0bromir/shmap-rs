#!/bin/bash
# The designed experiment behind RESULTS.md section 5d, for profiling/costmodel.py.
#
# Spans the (rungs x walk-skip) design space so the per-operation costs are
# identifiable -- each configuration moves a different counter, which is what
# lets least squares separate them -- with every configuration visited inside
# each repeat so the members of a comparison share their drift, and the global
# run order recorded so what drift remains can be estimated rather than averaged
# away. It also contains a negative control by construction: the walk skip is
# inert under bucket_SH, so those pairs are provably identical work.
#
# Reads come from subsets of the suite's own datasets; set READS to a directory
# holding B01.fa/B02.fa/B03.fa/B05.fa. ~50 minutes, and it takes the host lock
# because a measured run must not have company.
#
#   READS=/tmp/q12 OUT=/tmp/q12m flock ~/.shmap-bench.lock profiling/costmodel_run.sh
#   python3 profiling/costmodel.py /tmp/q12m
set -u
BIN=${BIN:-./target/release/shmap}
REF=${REF:-$HOME/_paper_work/hs1.fa}
READS=${READS:-/tmp/q12}
OUT=${OUT:-/tmp/q12m}
mkdir -p "$OUT"
P="-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3"
IDX=0
cat "$REF" > /dev/null
for B in B01 B02 B03 B05; do cat "$READS/$B.fa" > /dev/null; done
echo -e "idx\tunix\tbench\tmetric\tsteps\tskip\trep" > $OUT/design.tsv
for R in 0 1 2; do
  for B in B01 B02 B03 B05; do
    for M in Containment Jaccard bucket_SH; do
      for S in 0 1 2; do
        for K in 1 0; do
          E="SHMAP_THETA_STEPS=$S"
          [ "$K" = 0 ] && E="$E SHMAP_NO_PRUNE_SKIP=1"
          printf "%d\t%d\t%s\t%s\t%s\t%s\t%s\n" "$IDX" "$(date +%s)" "$B" "$M" "$S" "$K" "$R" >> $OUT/design.tsv
          env $E $BIN -s "$REF" -p "$READS/$B.fa" $P -m "$M" -@ 1 \
              -x --profile-log "$OUT/run$IDX.json" > /dev/null 2>&1
          IDX=$((IDX+1))
        done
      done
    done
  done
  echo "repeat $R done"
done
