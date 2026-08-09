#!/bin/bash
# Enumerate every FASTA/FASTQ this project benchmarks against, with the
# identity triple (bytes, records, bases) that the dataset registry keys on.
# TSV to stdout so it can be pasted straight into the registry.
set -u

printf 'id\tpath\tbytes\trecords\tbases\tmean_len\tcoverage_hs1\tkind\n'

GENOME=3117292070

stat_fa () {   # id path kind
    local id=$1 p=$2 kind=$3
    [ -f "$p" ] || { printf '%s\t%s\tMISSING\t-\t-\t-\t-\t%s\n' "$id" "$p" "$kind"; return; }
    local bytes; bytes=$(stat -c %s "$p")
    # handles 2-line FASTA, wrapped FASTA and FASTQ alike
    case "$p" in
        *.fastq|*.fq)
            awk -v id="$id" -v p="$p" -v b="$bytes" -v g="$GENOME" -v k="$kind" \
                'NR%4==2{n++; s+=length($0)} END{printf "%s\t%s\t%s\t%d\t%d\t%d\t%.4f\t%s\n", id, p, b, n, s, (n?s/n:0), s/g, k}' "$p" ;;
        *)
            awk -v id="$id" -v p="$p" -v b="$bytes" -v g="$GENOME" -v k="$kind" \
                '/^>/{n++; next}{s+=length($0)} END{printf "%s\t%s\t%s\t%d\t%d\t%d\t%.4f\t%s\n", id, p, b, n, s, (n?s/n:0), s/g, k}' "$p" ;;
    esac
}

# --- references ---
stat_fa REF-HS1     "$HOME/_paper_work/hs1.fa"                        reference
stat_fa REF-CHRY    "$HOME/_paper_work/chrY.fa"                       reference
stat_fa REF-CHR21   "$HOME/_paper_work/chr21.fa"                      reference
stat_fa REF-TINY    "$HOME/minshmap_bench/data/ref.fa"                reference

# --- read sets, real ---
stat_fa D1-HIFI23K  "$HOME/real24k/reads_real24k.fa"                  reads-real
stat_fa D3-HIFI1X   "$HOME/hifi_real/hifi_1x.fa"                      reads-real
stat_fa D4-HIFI10X  "$HOME/hifi_real/hifi_10x.fa"                     reads-real
stat_fa DX-HIFI3X   "$HOME/hifi_real/hifi_3x.fa"                      reads-real
stat_fa DX-HIFIMAST "$HOME/hifi_real/master.fa"                       reads-real
stat_fa D6-ONT24K   "$HOME/ont24k/reads_ont24k.fa"                    reads-real
stat_fa DX-ONT6K    "$HOME/minshmap_bench/realworld/data_rw/ont.fa"   reads-real
stat_fa DX-HIFI6K   "$HOME/minshmap_bench/realworld/data_rw/hifi.fa"  reads-real
stat_fa DX-CLR6K    "$HOME/minshmap_bench/realworld/data_rw/clr.fa"   reads-real

# --- read sets, simulated ---
stat_fa D2-SIM24K   "$HOME/wgs24k/reads_24kbp_1x.fa"                  reads-sim
stat_fa DX-HIFI2K   "$HOME/_paper_work/reads.fa"                      reads-real
stat_fa DX-TINY     "$HOME/minshmap_bench/data/reads.fa"              reads-sim
echo "ENUMERATE DONE" >&2
