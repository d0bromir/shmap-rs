#!/bin/bash
# Real HG002 HiFi reads >=22 kb from the 20 kb-insert library (chemistry2),
# streamed and length-filtered on the fly so only the kept reads hit disk.
set -u
B=https://ftp-trace.ncbi.nlm.nih.gov/ReferenceSamples/giab/data/AshkenazimTrio/HG002_NA24385_son/PacBio_CCS_15kb_20kb_chemistry2/reads
OUT=$HOME/real24k/reads_real24k.fa
: > "$OUT"
for m in m64011_190830_220126 m64011_190901_095311; do
  echo "[$(date +%H:%M:%S)] streaming $m"
  curl -fsSL --retry 3 --retry-delay 10 "$B/$m.fastq.gz" \
    | zcat \
    | awk -v OUT="$OUT" 'NR%4==1{h=substr($0,2)} NR%4==2{if(length($0)>=22000){print ">"h"\n"$0 >> OUT; k++}} END{print "  kept "k" reads" > "/dev/stderr"}'
  echo "[$(date +%H:%M:%S)] after $m: $(grep -c "^>" "$OUT") reads"
done
awk "NR%2==0{n++;b+=length(\$0)} END{printf \"FINAL reads=%d bases=%d mean=%d coverage=%.4fx\n\", n, b, b/n, b/3117292070}" "$OUT"
echo FETCH24K DONE
