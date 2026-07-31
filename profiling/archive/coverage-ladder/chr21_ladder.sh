#!/usr/bin/env bash
# chr21 coverage ladder, shmap-rs vs C++ shmap, 1x -> 100x.
# chr21 is used because the C++ needs ~19.3 GB on the whole genome and this
# host has 14.3 GB — see cpp_hs1_probe notes. Both fit easily at chr21 scale.
set -u
cd "$(dirname "$0")"
REF=$HOME/_paper_work/chr21.fa
PARAMS=(-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment)

for depth in 1 10 30 100; do
    reads="chr21_${depth}x.fa"
    for impl in cpp rs1 rs8; do
        case $impl in
            cpp) bin=./shmap_cpp;   extra=() ;;
            rs1) bin=./shmap_final; extra=(-@ 1) ;;
            rs8) bin=./shmap_final; extra=(-@ 8) ;;
        esac
        tag="${depth}x-${impl}"
        /usr/bin/time -v -o "c21_time_${tag}.txt" \
            "$bin" -s "$REF" -p "$reads" "${PARAMS[@]}" "${extra[@]}" \
            > "c21_${tag}.paf" 2> "c21_err_${tag}.txt"
        echo "=== ${tag} rc=$? wall=$(grep -oP 'Elapsed.*: \K.*' "c21_time_${tag}.txt") rss=$(grep -oP 'Maximum resident set size \(kbytes\): \K[0-9]+' "c21_time_${tag}.txt")KB lines=$(wc -l < "c21_${tag}.paf") ==="
    done
done

# Tracy-instrumentation overhead check, same input, C++ only.
/usr/bin/time -v -o c21_time_10x-cpptracy.txt ./shmap_cpp_tracy -s "$REF" -p chr21_10x.fa "${PARAMS[@]}" \
    > c21_10x-cpptracy.paf 2> c21_err_10x-cpptracy.txt
echo "=== 10x-cpptracy wall=$(grep -oP 'Elapsed.*: \K.*' c21_time_10x-cpptracy.txt) ==="
echo "=== CHR21 LADDER COMPLETE ==="
