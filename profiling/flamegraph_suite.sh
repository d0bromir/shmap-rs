#!/usr/bin/env bash
# Flame graphs for every suite benchmark (B01-B09), Containment, paper/short-read
# parameters exactly as suite.toml defines them. One host, one commit.
#
#   SHMAP_SRC=~/shmap-rs DATA=~/shmap-rs/benchmarks/data/files OUT=~/flame-<sha> \
#       profiling/flamegraph_suite.sh [B01 B02 ...]
#
# Needs: cargo, `cargo install flamegraph` (the `flamegraph` binary), and a perf that
# the unprivileged user may run (kernel.perf_event_paranoid <= 1 for user+kernel,
# <= 2 for user-space only, which is all this needs).
#
# Long reads run at -@1 (one stack per stage, no scheduler noise); short reads at the
# suite's -@32, because -@1 on 150 bp reads is hours. Build uses frame pointers so
# `--call-graph fp` works: cheap and small, unlike dwarf unwinding at 32 threads.
# Frame pointers and debug info change the layout a little; these graphs say where
# time goes, they are not timings (see profiling/README.md on lto layout).
#
# Takes the host benchmark lock for the whole run so a measured run cannot overlap.
set -euo pipefail

SRC=${SHMAP_SRC:?set SHMAP_SRC to a checkout of the commit to profile}
DATA=${DATA:?set DATA to benchmarks/data/files}
OUT=${OUT:?set OUT to an output directory}
FREQ=${FREQ:-99}
LOCK=${LOCK:-$HOME/.shmap-bench.lock}
export PATH="$HOME/.cargo/bin:$PATH"

# id  reads                                 threads  -r
BENCH=(
  "B01 real24k/reads_real24k.fa              1  0.01"
  "B02 wgs24k/reads_24kbp_1x.fa             1  0.01"
  "B03 hifi_real/hifi_1x.fa                 1  0.01"
  "B04 hifi_real/hifi_10x.fa                1  0.01"
  "B05 ont24k/reads_ont24k.fa               1  0.01"
  "B06 aviti150/reads_aviti_2x.fa          32  0.1"
  "B07 aviti150/reads_aviti_4x.fa          32  0.1"
  "B08 aviti150/reads_aviti_7x.fa          32  0.1"
  "B09 shortread2024/reads_hg008_1x.fa     32  0.1"
)
WANT=("$@")

exec 9>"$LOCK"
flock 9
mkdir -p "$OUT"

SHA=$(git -C "$SRC" rev-parse HEAD)
echo "commit $SHA" | tee "$OUT/RUN.txt"
{ date -u +%FT%TZ; uname -srm; nproc; uptime; perf --version; flamegraph --version; } >> "$OUT/RUN.txt"
cat /proc/sys/kernel/perf_event_paranoid >> "$OUT/RUN.txt"

TARGET=${TARGET_DIR:-$HOME/bench-work/flame-$SHA}
RUSTFLAGS="-C force-frame-pointers=yes" CARGO_PROFILE_RELEASE_DEBUG=true \
    cargo build --release --locked --manifest-path "$SRC/Cargo.toml" --target-dir "$TARGET"
BIN=$TARGET/release/shmap
"$BIN" --version | tee -a "$OUT/RUN.txt"

printf 'bench\tthreads\twall_s\tpeak_rss_kb\n' > "$OUT/summary.tsv"
for row in "${BENCH[@]}"; do
  read -r id reads thr ratio <<<"$row"
  if ((${#WANT[@]})) && [[ ! " ${WANT[*]} " == *" $id "* ]]; then continue; fi
  echo "== $id ($reads, -@$thr, -r $ratio)"
  perf_data=$OUT/$id.perf.data
  /usr/bin/time -v -o "$OUT/$id.time" \
    perf record -F "$FREQ" --call-graph fp -o "$perf_data" -- \
      "$BIN" -s "$DATA/_paper_work/hs1.fa" -p "$DATA/$reads" \
        -k 25 -r "$ratio" -t 0.4 -d 0.075 -o 0.3 -m Containment -@ "$thr" \
        > /dev/null 2> "$OUT/$id.stderr"
  flamegraph --perfdata "$perf_data" -o "$OUT/$id.svg" --title "shmap $id -@$thr"
  perf report -i "$perf_data" --no-children --sort symbol -g none --stdio \
      --percent-limit 0.5 2>/dev/null > "$OUT/$id.top.txt"
  wall=$(awk -F': ' '/Elapsed \(wall/{n=split($2,a,":"); s=0; for(i=1;i<=n;i++) s=s*60+a[i]; print s}' "$OUT/$id.time")
  rss=$(awk -F': ' '/Maximum resident/{print $2}' "$OUT/$id.time")
  printf '%s\t%s\t%s\t%s\n' "$id" "$thr" "$wall" "$rss" >> "$OUT/summary.tsv"
  rm -f "$perf_data"
done
echo done
