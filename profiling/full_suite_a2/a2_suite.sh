#!/bin/bash
# Full benchmark suite on a2 (64-core AVX-512, 376 GB RAM) at shmap-rs f85d9a2.
#
# Same methodology as ~/hifi_real/driver.sh: sequential, never parallel,
# /usr/bin/time -v, PAF written to a real file. Extends it with
#   - the refine-memo A/B (SHMAP_NO_REFINE_MEMO)
#   - -@ 64 runs
#   - the small/tiny tiers
#   - master.fa (all 18 SMRT cells, ~13.2x of distinct real reads)
set -u

D=$HOME/a2_bench
REF_BIG=$HOME/_paper_work/hs1.fa
REF_SMALL=$HOME/_paper_work/chrY.fa
REF_TINY=$HOME/minshmap_bench/data/ref.fa
RS=$HOME/shmap-rs/target/release/shmap
CPP=$HOME/Pesho/shmap/release/shmap
ARGS="-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment"
mkdir -p "$D"

log() { echo "[$(date '+%F %H:%M:%S')] $*"; }

# run <tag> <keep_paf:0|1> <command...>
run() {
    tag=$1; keep=$2; shift 2
    out=/dev/null; [ "$keep" = 1 ] && out="$D/$tag.paf"
    log "START $tag"
    /usr/bin/time -v -o "$D/$tag.time" "$@" > "$out" 2>"$D/$tag.err"
    rc=$?
    wall=$(grep -m1 Elapsed "$D/$tag.time" | sed 's/.*: //')
    rss=$(grep -m1 Maximum "$D/$tag.time" | sed 's/.*: //')
    lines=0; [ "$keep" = 1 ] && lines=$(wc -l < "$D/$tag.paf")
    log "END   $tag rc=$rc wall=$wall rssKB=$rss paf=$lines"
}

# ---- tier: tiny + small (fast, exercises the non-WGS path) ----
run tiny_rs1  1 "$RS"  -s "$REF_TINY" -p "$HOME/minshmap_bench/data/reads.fa" $ARGS -@ 1 -x --profile-log "$D/tiny_rs1.json"
run tiny_cpp  1 "$CPP" -s "$REF_TINY" -p "$HOME/minshmap_bench/data/reads.fa" $ARGS
run small_rs1 1 "$RS"  -s "$REF_SMALL" -p "$HOME/_paper_work/reads.fa" $ARGS -@ 1 -x --profile-log "$D/small_rs1.json"
run small_rs64 0 "$RS" -s "$REF_SMALL" -p "$HOME/_paper_work/reads.fa" $ARGS -@ 64
run small_cpp 1 "$CPP" -s "$REF_SMALL" -p "$HOME/_paper_work/reads.fa" $ARGS
log "TIER small/tiny complete"

# ---- tier: big, real HiFi reads vs the whole genome ----
# hifi_{1,3,10}x are exact-coverage subsets; master.fa is all 18 cells (~13.2x).
for depth in 1 3 10 master; do
    case $depth in
        master) f=$HOME/hifi_real/master.fa; t=13x ;;
        *)      f=$HOME/hifi_real/hifi_${depth}x.fa; t=${depth}x ;;
    esac
    [ -f "$f" ] || { log "SKIP $t (missing $f)"; continue; }

    run "rs_${t}"        1 "$RS"  -s "$REF_BIG" -p "$f" $ARGS -@ 1 -x --profile-log "$D/rs_${t}.json"
    # `env` on the command itself, not a prefix on the `run` function: a
    # var assignment prefixing a function call can persist afterwards and
    # would silently disable the memo for every later run.
    run "rs_${t}_nomemo" 1 env SHMAP_NO_REFINE_MEMO=1 \
        "$RS"  -s "$REF_BIG" -p "$f" $ARGS -@ 1 -x --profile-log "$D/rs_${t}_nomemo.json"
    run "cpp_${t}"       1 "$CPP" -s "$REF_BIG" -p "$f" $ARGS
    run "rs_${t}_t64"    0 "$RS"  -s "$REF_BIG" -p "$f" $ARGS -@ 64 -x --profile-log "$D/rs_${t}_t64.json"

    # memo must not change output
    if cmp -s <(sed 's/\tt:f:[0-9.-]*//' "$D/rs_${t}.paf") \
              <(sed 's/\tt:f:[0-9.-]*//' "$D/rs_${t}_nomemo.paf"); then
        log "CHECK $t memo output IDENTICAL"
    else
        log "CHECK $t memo output DIFFERS <<< INVESTIGATE"
    fi
    log "TIER $t complete"
done

log "ALL RUNS DONE"
