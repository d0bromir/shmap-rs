#!/bin/bash
# Reproduce, and record, bwa-mem2's crash on long reads.
#
#   bwa_mem2_longread_crash.sh > crash-report.txt
#
# WHY THIS EXISTS
# ---------------
# bwa-mem2 is in the reference-mapper corpus but is skipped on B01-B05, the
# long-read benchmarks. A `skip_benchmarks` line in suite.toml is an assertion,
# and an assertion about another project's tool is exactly the kind of claim
# that should not be taken on trust in a paper. This script is the evidence:
# it runs the tool, shows it dying, and shows that no documented knob avoids
# it.
#
# WHAT IS BEING CLAIMED, PRECISELY
# --------------------------------
# Not "bwa-mem2 is broken". It is a short-read aligner and its authors say so:
#
#   Heng Li, bwa-mem2 issue #4 (2019-05-24), closing a request for long-read
#   support: "The option is still there in the code. It is just that the
#   command line help doesn't show it. Nonetheless, minimap2 is and will be the
#   preferred mapper for long reads. Bwa-mem2 is only optimized for short
#   reads."
#
# The claim is narrower and factual: `-x pacbio`, bwa's own documented PacBio
# preset, inherited by bwa-mem2 and still accepted on its command line,
# segfaults on real 22-34 kb HiFi reads against a human genome, and the failure
# is not avoidable through the batch-size or re-seeding options. That is why
# there are no long-read numbers for it in the corpus, and it is the reason
# the short-read benchmarks exist rather than being a nicety.
#
# The same signature is open upstream as issue #133 (2021-03-08), reported on
# PacBio reads, with the same "Re-allocating SMEM data structures due to
# enc_qdb" line immediately before the fault. Unresolved at the time of
# writing.
#
# RUN IT ON BOTH ARCHITECTURES. The x86_64 and aarch64 binaries are different
# builds (see setup_bwa_mem2.sh), so "it crashes" has to be established on each
# rather than assumed to transfer.
set -uo pipefail

BWA="${BWA:-$HOME/micromamba/envs/refmappers/bin/bwa-mem2}"
REF="${REF:-$HOME/bench-refs/REF-HS1.bwamem2.fa}"
READS="${READS:-$HOME/shmap-rs/benchmarks/data/files/real24k/reads_real24k.fa}"
WORK="${WORK:-/tmp/bwamem2-crash}"

mkdir -p "$WORK"
for f in "$BWA" "$REF" "$READS"; do
    [ -e "$f" ] || { echo "not found: $f" >&2; exit 1; }
done

echo "bwa-mem2 long-read crash report"
echo "==============================="
echo "date     $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "host     $(hostname) ($(uname -m))"
echo "kernel   $(uname -r)"
echo "binary   $BWA"
echo "version  $("$BWA" version 2>/dev/null) [$(basename "$(ls "$(dirname "$BWA")"/../conda-meta/bwa-mem2-*.json | head -1)" .json)]"
echo "dispatch $("$BWA" version 2>&1 >/dev/null | grep -o 'simd = [^,]*' | head -1)"
echo "index    $REF"
echo "reads    $READS"
echo

echo "read lengths in the first 200 reads"
echo "-----------------------------------"
head -400 "$READS" | awk 'NR%2==0{print length($0)}' \
  | sort -n | awk '{a[NR]=$1} END {printf "n=%d  min=%d  median=%d  max=%d\n", NR, a[1], a[int(NR/2)], a[NR]}'
echo

run() {   # run <label> <nreads> <args...>
    local label="$1" n="$2"; shift 2
    local fa="$WORK/n$n.fa" sam="$WORK/out.sam"
    head -$((n * 2)) "$READS" > "$fa"
    # Subshell: a fatal signal is reported by the shell that waited for the
    # child, so redirecting the child's stderr does not suppress "Segmentation
    # fault (core dumped)". Letting a subshell take the death and discarding
    # its stderr keeps the report readable; the verdict column carries the
    # same fact, and section 5 shows the raw stderr on purpose.
    ( "$BWA" mem "$@" "$REF" "$fa" > "$sam" 2>"$WORK/err.txt" ) 2>/dev/null
    local rc=$?
    # `grep -c` exits 1 on a zero count, so `|| echo 0` printed a second zero.
    local recs; recs=$(grep -v '^@' "$sam" 2>/dev/null | wc -l)
    local verdict="ok"
    # 139 = 128 + SIGSEGV. Named rather than left as the shell's encoding,
    # because "rc=139" reads like an ordinary error exit and this is not one.
    [ $rc -eq 139 ] && verdict="SIGSEGV"
    [ $rc -ne 0 ] && [ $rc -ne 139 ] && verdict="exit $rc"
    printf '%-46s reads=%-6s %-8s records=%s\n' "$label" "$n" "$verdict" "$recs"
}

echo "1. how many reads it takes"
echo "--------------------------"
echo "Same preset, same everything, only the number of reads changes. The"
echo "crash is not one pathological read: 40 reads map fine and 100 do not,"
echo "and at 100 it dies before emitting a single record, so the failure is in"
echo "a batch rather than in the read that would have been next."
for n in 20 40 60 100 200; do run "-x pacbio" "$n" -t 1 -x pacbio; done
echo

echo "2. thread count is irrelevant"
echo "-----------------------------"
echo "Rules out a race: it is the same fault single-threaded."
for t in 1 4 8 32; do run "-x pacbio -t $t" 200 -t "$t" -x pacbio; done
echo

echo "3. no documented knob avoids it"
echo "-------------------------------"
echo "-K sets the batch size, which is the obvious lever if the fault is a"
echo "per-batch buffer. It moves the crash later — more records come out"
echo "before it dies — and never prevents it. Nor does relaxing the preset's"
echo "aggressive re-seeding (-r), nor spelling the preset out by hand."
run "-x pacbio -K 10000000"   200 -t 1 -x pacbio -K 10000000
run "-x pacbio -K 1000000"    200 -t 1 -x pacbio -K 1000000
run "-x pacbio -K 100000"     200 -t 1 -x pacbio -K 100000
run "-x pacbio -K 50000"      200 -t 1 -x pacbio -K 50000
run "-x pacbio -r 1.5"        200 -t 1 -x pacbio -r 1.5
run "-k17 -W40 -A1 -B1 -O1 -E1 -L0" 200 -t 1 -k17 -W40 -A1 -B1 -O1 -E1 -L0
run "-x ont2d"                200 -t 1 -x ont2d
echo

echo "4. the control: defaults do not crash"
echo "-------------------------------------"
echo "Without a preset the same reads and the same index are fine, so the"
echo "index is not corrupt and the reads are not malformed. Short-read"
echo "defaults on 24 kb reads is not a configuration anyone should use, but"
echo "it isolates the fault to the long-read preset path."
run "(no preset)" 200 -t 1
run "(no preset)" 1000 -t 1
echo

echo "5. stderr immediately before the fault"
echo "--------------------------------------"
head -400 "$READS" > "$WORK/n200.fa"
"$BWA" mem -t 1 -x pacbio "$REF" "$WORK/n200.fa" > /dev/null 2>"$WORK/err.txt"
echo "exit: $?"
tail -12 "$WORK/err.txt"
echo
echo "The 'Re-allocating SMEM data structures due to enc_qdb' line is the same"
echo "one reported in upstream issue #133."
echo

echo "6. every benchmark's own reads"
echo "------------------------------"
echo "This is what decides skip_benchmarks in suite.toml, per benchmark rather"
echo "than by assuming one dataset speaks for the rest. B03/B04 are 12.8 kb,"
echo "roughly half the length of B01/B02/B05, so they are a separate question."
echo "The 150 bp set is the control: the same binary, index and code path, on"
echo "the reads the tool was built for."
DATA="${DATA:-$HOME/shmap-rs/benchmarks/data/files}"
bench_case() {   # bench_case <label> <file> [preset args...]
    local label="$1" file="$2"; shift 2
    if [ ! -s "$file" ]; then
        printf '%-46s %s\n' "$label" "(not on this host)"
        return
    fi
    local n=200 fa="$WORK/bench.fa" sam="$WORK/bench.sam"
    head -$((n * 2)) "$file" > "$fa"
    local len; len=$(awk 'NR%2==0{n++; b+=length($0)} END {printf "%d", b/n}' "$fa")
    ( "$BWA" mem -t 1 "$@" "$REF" "$fa" > "$sam" 2>/dev/null ) 2>/dev/null
    local rc=$? recs verdict
    recs=$(grep -v '^@' "$sam" 2>/dev/null | wc -l)
    verdict="ok"
    [ $rc -eq 139 ] && verdict="SIGSEGV"
    [ $rc -ne 0 ] && [ $rc -ne 139 ] && verdict="exit $rc"
    printf '%-46s mean_len=%-7s %-8s records=%s\n' "$label" "$len" "$verdict" "$recs"
}
bench_case "B01 real HiFi 23.2 kb   -x pacbio" "$DATA/real24k/reads_real24k.fa"      -x pacbio
bench_case "B02 simulated 24 kb     -x pacbio" "$DATA/wgs24k/reads_24kbp_1x.fa"      -x pacbio
bench_case "B03 real HiFi 12.8 kb   -x pacbio" "$DATA/hifi_real/hifi_1x.fa"          -x pacbio
bench_case "B04 real HiFi 12.8 kb   -x pacbio" "$DATA/hifi_real/hifi_10x.fa"         -x pacbio
bench_case "B05 real ONT 23.8 kb    -x ont2d"  "$DATA/ont24k/reads_ont24k.fa"        -x ont2d
bench_case "B06 AVITI 150 bp        (default)" "$DATA/aviti150/reads_aviti_2x.fa"
echo
echo "END OF REPORT"
