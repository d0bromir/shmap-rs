#!/usr/bin/env bash
# The measurements behind docs/SPEEDUP_PROPOSALS.md, one host, one binary, bench lock held.
#
#   SHMAP_BIN=<release shmap> DATA=<benchmarks/data/files> OUT=<dir> profiling/speedup_probes.sh
#
# 1  stage breakdown of 400k real 150 bp reads, -@1            -> stage-short-400k.profile.json
# 2  --index-cache: build, then two hits, B01 parameters -@32  -> index-cache-hit-b01.profile.json
# 3  large pages via mimalloc, A/B, outputs compared           -> stdout
#
# The dead-on-arrival counter needs a rebuilt binary: `git apply profiling/dead_on_arrival.patch` (counters probe_buckets_pass1, probe_dead_on_arrival in the -x report).
set -euo pipefail
BIN=${SHMAP_BIN:?}; DATA=${DATA:?}; OUT=${OUT:?}
LOCK=${LOCK:-$HOME/.shmap-bench.lock}
mkdir -p "$OUT"; cd "$OUT"
exec 9>"$LOCK"; flock 9
REF=$DATA/_paper_work/hs1.fa
strip_t() { sed -E 's/\tt:f:[0-9.eE+-]+//' "$1" | md5sum | cut -c1-8; }   # drop the per-read timing tag

head -n 800000 "$DATA/aviti150/reads_aviti_2x.fa" > r400k.fa
"$BIN" -s "$REF" -p r400k.fa -k 25 -r 0.1 -t 0.4 -d 0.075 -o 0.3 -m Containment -@ 1 \
    -x --profile-log stage-short-400k.profile.json > /dev/null 2> /dev/null

L=(-s "$REF" -p "$DATA/real24k/reads_real24k.fa" -k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment -@ 32)
for mode in nocache build hit1 hit2; do
  args=("${L[@]}"); [[ $mode != nocache ]] && args+=(--index-cache hs1.idx)
  [[ $mode == hit2 ]] && args+=(-x --profile-log index-cache-hit-b01.profile.json)
  /usr/bin/time -f "$mode %e s wall, %M KB" "$BIN" "${args[@]}" > "$mode.paf" 2> "$mode.err"
  tail -1 "$mode.err"
done
echo "outputs (timing tag stripped): $(strip_t nocache.paf) $(strip_t hit2.paf)"
rm -f hs1.idx

S=(-s "$REF" -p r400k.fa -k 25 -r 0.1 -t 0.4 -d 0.075 -o 0.3 -m Containment -@ 16)
for rep in 1 2; do for hp in 0 1; do
  MIMALLOC_LARGE_OS_PAGES=$hp /usr/bin/time -f "short-reads large_pages=$hp %e s wall" "$BIN" "${S[@]}" > "lp$hp.paf" 2> lp.err
  tail -1 lp.err
done; done
echo "outputs (timing tag stripped): $(strip_t lp0.paf) $(strip_t lp1.paf)"
