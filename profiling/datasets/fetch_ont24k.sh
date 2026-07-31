#!/bin/bash
# Real HG002 Oxford Nanopore reads in a 20-28 kb band (mean lands at ~23.8 kb),
# streamed from ENA and length-filtered on the fly so only kept reads hit disk.
#
# 29 runs with mean read length 20-34 kb, where a 24 kb band has the most mass.
# Ultra-long runs are deliberately excluded: their 20-28 kb yield is only
# 1.7-2.2% because most reads are far longer.
set -u
OUT=$HOME/ont24k/reads_ont24k.fa
mkdir -p "$(dirname "$OUT")"
: > "$OUT"
n=0
while IFS=$'\t' read -r acc reads meanlen gb; do
    n=$((n + 1))
    url=$(curl -fsSL "https://www.ebi.ac.uk/ena/portal/api/filereport?accession=${acc}&result=read_run&fields=fastq_ftp&format=tsv" 2>/dev/null \
          | tail -1 | tr -d '\r' | cut -f2 | cut -d';' -f1)
    if [ -z "$url" ]; then
        echo "[$(date +%H:%M:%S)] $acc: no url, skipping"
        continue
    fi
    echo "[$(date +%H:%M:%S)] ($n) $acc  ${gb} GB"
    curl -fsSL --retry 3 --retry-delay 10 "https://$url" 2>/dev/null | zcat 2>/dev/null | \
        awk -v OUT="$OUT" 'NR%4==1{h=substr($0,2)} NR%4==2{L=length($0); if(L>=20000 && L<=28000){print ">"h"\n"$0 >> OUT}}'
    echo "[$(date +%H:%M:%S)]     total kept: $(grep -c '^>' "$OUT")"
done < /tmp/ont_pick.tsv
awk 'NR%2==0{n++; b+=length($0)} END{printf "FINAL reads=%d bases=%d mean=%d coverage=%.4fx\n", n, b, b/n, b/3117292070}' "$OUT"
echo FETCHONT DONE
