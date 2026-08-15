#!/usr/bin/env bash
#
# Provision bwa-mem2 on a benchmark host: install the binary and build the
# FM-index for REF-HS1. Idempotent — re-running it does nothing if both are
# already in place.
#
#   ./setup_bwa_mem2.sh              install and index
#   ./setup_bwa_mem2.sh --check      report what is present, change nothing
#
# This is committed rather than typed by hand because the aarch64 build is not
# the same artefact as the x86_64 one (see BINARY below) and the index costs
# ~87 GB of RAM to build. Anyone reproducing the corpus needs both facts, and a
# shell history on one machine is not where they will find them.
#
# ---------------------------------------------------------------------------
# BINARY
# ---------------------------------------------------------------------------
# From bioconda, not from upstream's release page, because upstream ships an
# x86_64 tarball only and this corpus is measured on two architectures. The
# recipe is bioconda-recipes/recipes/bwa-mem2 at version 2.3, and it builds the
# two architectures DIFFERENTLY:
#
#   x86_64   `make multi` — the five SIMD variants plus upstream's runsimd
#            dispatcher, which execs the best one for the host CPU. On a2
#            (Xeon Gold 5218) that is bwa-mem2.avx512bw.
#   aarch64  a single binary built with `-march=armv8-a` after grafting in
#            sse2neon v1.8.0, which reimplements the SSE intrinsics on NEON.
#
# So the ARM binary is a TRANSLATION of the x86 SIMD code, not a native NEON
# port, and it is compiled for baseline ARMv8 with no Neoverse-N1 tuning. Both
# facts belong beside any cross-architecture ratio taken from these numbers.
# What they do NOT do is change the alignments: sse2neon is semantics-
# preserving, and `verify_bwa_mem2_arch_parity.sh` checks that claim against
# the two corpora rather than trusting it.
#
# `bwa-mem2 version` reports "2.2.1" from the 2.3 package. That is an upstream
# bug — the version string was not bumped for the 2.3 release — and it means
# the cache key in reference_mappers.py cannot tell 2.2.1 from 2.3 on its own.
# suite.toml's version_cmd works around it; see the comment there.
#
# ---------------------------------------------------------------------------
# INDEX
# ---------------------------------------------------------------------------
# `bwa-mem2 index` needs ~28 bytes of RAM per reference base — ~87 GB for the
# 3.1 Gbp hs1 — and writes ~10 GB of index files. Both hosts have the memory
# (a2 376 GB, galaxy 246 GB); a smaller machine will be OOM-killed partway
# through and leave a truncated .bwt.2bit.64 behind, which is why this script
# checks the memory before starting and removes a partial index on failure.
#
# The index is keyed to the REGISTRY ID, not to the FASTA's filename, for the
# same reason Winnowmap2's repetitive k-mer set is: a replaced reference gets a
# new id (VERSIONING.md §3), which forces a rebuild instead of a silent reuse
# against different sequence.
set -euo pipefail

MAMBA_BIN="$HOME/tools/bin/micromamba"
export MAMBA_ROOT_PREFIX="$HOME/micromamba"
ENV_NAME="refmappers"
ENV_PREFIX="$MAMBA_ROOT_PREFIX/envs/$ENV_NAME"
BWA="$ENV_PREFIX/bin/bwa-mem2"
BWA_VERSION="2.3"

REFS="$HOME/bench-refs"
REF_ID="REF-HS1"
SOURCE_FA="$HOME/shmap-rs/benchmarks/data/files/_paper_work/hs1.fa"
PREFIX="$REFS/$REF_ID.bwamem2.fa"
# `bwa-mem2 index` writes these five beside the prefix; all must exist for the
# index to be usable. Checking only one is how a half-written index gets reused.
SUFFIXES=(0123 amb ann bwt.2bit.64 pac)

CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

have_index() {
    local s
    for s in "${SUFFIXES[@]}"; do
        [[ -s "$PREFIX.$s" ]] || return 1
    done
    return 0
}

echo "host    $(hostname) ($(uname -m)), $(nproc) cores, $(free -g | awk '/Mem:/{print $2}') GB"
echo "binary  $BWA"
echo "prefix  $PREFIX"

if [[ $CHECK_ONLY -eq 1 ]]; then
    if [[ -x "$BWA" ]]; then
        echo "binary  present, reports $("$BWA" version 2>/dev/null)"
    else
        echo "binary  MISSING"
    fi
    if have_index; then
        echo "index   present"
        ls -la "$PREFIX".* 2>/dev/null
    else
        echo "index   MISSING or incomplete"
    fi
    exit 0
fi

# --- binary ----------------------------------------------------------------
if [[ ! -x "$BWA" ]]; then
    if [[ ! -x "$MAMBA_BIN" ]]; then
        echo "installing micromamba into $MAMBA_BIN"
        mkdir -p "$(dirname "$MAMBA_BIN")"
        case "$(uname -m)" in
            x86_64)  MM_ARCH=linux-64 ;;
            aarch64) MM_ARCH=linux-aarch64 ;;
            *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;;
        esac
        curl -Ls "https://micro.mamba.pm/api/micromamba/$MM_ARCH/latest" \
            | tar -xj -C "$(dirname "$(dirname "$MAMBA_BIN")")" bin/micromamba
    fi
    echo "installing bwa-mem2=$BWA_VERSION from bioconda"
    # `install` rather than `create`: a2's refmappers env already carries meryl,
    # which Winnowmap2's k-mer set is built with, and recreating it would drop it.
    "$MAMBA_BIN" install -y -n "$ENV_NAME" -c conda-forge -c bioconda \
        "bwa-mem2=$BWA_VERSION"
else
    echo "binary  already installed"
fi
"$BWA" version 2>/dev/null

# --- index -----------------------------------------------------------------
mkdir -p "$REFS"
if have_index; then
    echo "index   already built; nothing to do"
    exit 0
fi

[[ -s "$SOURCE_FA" ]] || { echo "reference not found: $SOURCE_FA" >&2; exit 1; }

ref_gb=$(( $(stat -c %s "$SOURCE_FA") / 1000000000 ))
need_gb=$(( ref_gb * 28 ))
have_gb=$(free -g | awk '/Mem:/{print $7}')
echo "index   needs ~${need_gb} GB RAM, ${have_gb} GB available"
if (( have_gb < need_gb )); then
    echo "not enough free memory to build the index; refusing rather than" >&2
    echo "leaving a truncated one behind" >&2
    exit 1
fi

# bwa-mem2 derives the index prefix from the path it is given, so the symlink
# puts the ~10 GB of index files in bench-refs beside the other derived inputs
# rather than in the dataset corpus, which is shared and must stay pristine.
ln -sfn "$SOURCE_FA" "$PREFIX"

echo "index   building — this takes about an hour"
if ! /usr/bin/time -v -o "$REFS/$REF_ID.bwamem2.index.time" "$BWA" index "$PREFIX"; then
    echo "index build FAILED; removing the partial index" >&2
    rm -f "${SUFFIXES[@]/#/$PREFIX.}"
    exit 1
fi

have_index || { echo "index build produced an incomplete set" >&2; exit 1; }
echo "index   done"
grep -E 'Elapsed \(wall|Maximum resident' "$REFS/$REF_ID.bwamem2.index.time"
ls -la "$PREFIX".*
