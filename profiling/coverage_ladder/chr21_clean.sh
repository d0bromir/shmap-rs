#!/usr/bin/env bash
set -u; cd "$(dirname "$0")"
REF=$HOME/_paper_work/chr21.fa
P=(-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment)
for depth in 1 10 30 100; do
  reads="chr21_${depth}x.fa"
  cat "$reads" > /dev/null; cat "$REF" > /dev/null     # pre-warm page cache for all three
  for impl in rs8 cpp rs1; do                          # C++ no longer first
    case $impl in
      cpp) bin=./shmap_cpp;   ex=() ;;
      rs1) bin=./shmap_final; ex=(-@ 1) ;;
      rs8) bin=./shmap_final; ex=(-@ 8) ;;
    esac
    /usr/bin/time -v -o "cl_${depth}x-${impl}.time" "$bin" -s "$REF" -p "$reads" "${P[@]}" "${ex[@]}" >/dev/null 2>/dev/null
    echo "${depth}x ${impl} wall=$(grep -oP 'Elapsed.*: \K.*' cl_${depth}x-${impl}.time) rss=$(grep -oP 'Maximum resident set size \(kbytes\): \K[0-9]+' cl_${depth}x-${impl}.time)"
  done
done
echo "CLEAN PASS COMPLETE"
