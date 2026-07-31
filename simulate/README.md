# simulate/

How the simulated read sets are made, and the tools for asking what error rate does to the mapper.

Simulated reads are the only datasets here that support an **accuracy** claim, because they are the
only ones whose true position is known. Everything in `../RESULTS.md` §7 rests on them, so how they
were generated is not a footnote.

| path | what |
|---|---|
| `pbsim3/` | the scripts that actually produced the published datasets — provenance, imported from minshmap |
| `simulate_reads.py` | controlled generator: independent substitution / insertion / deletion rates |
| `measure_error_rate.py` | measures a read set's real error rate against the reference |
| `sweep_error_rates.py` | the degradation sweep: accuracy against error rate and error *type* |

---

## What the published datasets actually contain

`D2-SIM24K` is the accuracy dataset. Its documented description was "0.5% substitution noise", and
that was taken on trust until it was measured (`measure_error_rate.py`, 200 reads):

| | |
|---|---|
| mean 25-mer survival | 88.28% |
| implied total error rate | **0.498% per base** |
| mean length delta | **+0.0042%** |

So the documented figure is right, and the second row is the one that matters: **length is
preserved, so the errors are substitutions and there are essentially no indels.**

That is a real limit on what the accuracy numbers mean. Every figure in §7 — the 99.21% ground
truth, the satellite analysis, the Winnowmap2 comparison — is measured on substitution-only reads.
Real HiFi carries homopolymer indels, and ONT is indel-dominated. `sweep_error_rates.py` exists to
say how far those numbers carry.

## pbsim3/ — the provenance scripts

Imported from `minshmap_bench/realworld/pesho_table1/scripts/`. They are kept as the record of how
the published datasets were produced, and they are what to use to reproduce those datasets.

Two error models, and the distinction matters when quoting a rate:

| script | method | error model |
|---|---|---|
| `10_gen_chrY_10kbp_10x.sh`, `11_gen_allchr_10kbp_1x.sh` | `--method sample` | length *and* error profile drawn from a real HiFi FASTQ — no nominal rate, it is whatever the sample carries |
| `12_gen_chrY_24kbp_10x.sh` | `--method errhmm` | `ERRHMM-SEQUEL` at `--accuracy-mean 0.99`, i.e. a nominal **1%** error, length 24 000 ± 4 000 |

Ground truth comes from `paftools.js pbsim2fq`, which encodes it in the FASTA header as
`>S<c>_<n>!<chr>!<start>!<end>!<strand>` — the format every tool here reads.

**These do not run as imported.** They expect PBSIM3 at `~/libs/pbsim3`, `k8`, and a `paftools.js`
from a sibling checkout, none of which is a dependency of this repo. Treat them as documentation of
what was done; `simulate_reads.py` is what runs today.

## simulate_reads.py — controlled rates

PBSIM3's HMM models couple substitutions and indels: you can ask for 99% accuracy, not for "1%
substitutions and no indels" against "0.5% substitutions and 0.5% indels". Separating those is the
whole question, so this generates reads with exact, independent rates.

```sh
# controlled: every read identical, for comparing error TYPES at equal rate
./simulate_reads.py --ref hs1.fa --n 20000 --sub 0.01 -o subs.fa
./simulate_reads.py --ref hs1.fa --n 20000 --ins 0.005 --del 0.005 -o indels.fa

# realistic: rate spread across reads, indels clustered in homopolymers
./simulate_reads.py --ref hs1.fa --n 20000 --sub 0.03 --ins 0.01 --del 0.01 \
    --error-sd 0.5 --hp-bias 5 -o ont_like.fa
```

| flag | why it matters |
|---|---|
| `--error-sd` | spread of the per-read error rate. **Defaulting this to 0 is the most misleading thing this tool can do.** shmap maps a read when `(1-e)^k > t` — a threshold on that read's *own* rate — so near the threshold the mean predicts almost nothing. Real ONT maps ~43% at k=25 where a uniform simulation at the same mean mapped 0.08%. |
| `--hp-bias` | concentrates indels in homopolymer runs, where real long-read indels overwhelmingly fall. Clustered damage leaves more intact k-mers than the same count spread evenly. |

Use `--error-sd 0` deliberately, for a controlled comparison between error *types*; use a realistic
spread whenever the number is meant to say something about real data.

It is still **not** a sequencer model — error is not position-independent and context effects beyond
homopolymers are ignored. Use `pbsim3/` to reproduce the published datasets; use this to ask
controlled questions. The headers it writes are `pbsim2fq`-compatible, so every existing tool here
reads them unchanged.

Output is fully determined by `--seed`, so a dataset can be re-derived instead of archived.

## Reproducing

Simulated sets used in results should be registered in `../benchmarks/datasets.tsv` like any other
input, with the exact command in the provenance column. Regenerating with different parameters
means a **new id**, never editing a row — historical results have to keep resolving to what they
actually measured.
