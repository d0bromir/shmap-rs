# benchmarks/data

The declarative inputs to a benchmark run, and the root the corpus resolves
against. Nothing here is generated; nothing here is large.

| file | what it is |
|---|---|
| `suite.toml` | what gets measured: benchmarks × metrics × thread counts, thresholds, checks |
| `datasets.tsv` | the dataset registry: identity triple and path for every corpus file |
| `hosts.toml` | operational facts per benchmark host (address, cores, corpus location) |
| `files/` | **not in git** — the corpus itself, ~46 GB. See below. |

## The corpus is not in this repository

`files/` is gitignored. It holds ~46 GB for the five suite benchmarks (97 GB
for the whole registry, most of it in exploratory sets no benchmark uses), so
it lives on disk and is referenced, never committed.

`datasets.tsv` therefore stores paths **relative to `files/`**:

```
D4-HIFI10X   reads-real   a2   hifi_real/hifi_10x.fa   31219607763 …
```

and `files/` is a symlink to wherever that host actually keeps the data:

```sh
ln -sfn /home/mpiuser benchmarks/data/files      # a2
```

This is what lets the same registry resolve on every host without a single
per-host branch in the runner: the relative tree below `files/` is identical
everywhere, so `run.py` never learns a hostname. On a host that already has
the corpus laid out, pointing the symlink at it costs nothing and copies
nothing — which is exactly how a2 was migrated, with every existing absolute
path preserved byte for byte.

`$SHMAP_DATA` overrides the location for a host that cannot use the symlink.

A handful of registry entries are still absolute (the one `local-wsl`
dataset). Those are honoured as written — `layout.resolve_dataset()` only
joins a path against the root when it is relative — because `datasets.tsv` is
append-only and rewriting an existing entry's identity is not allowed.

## Provisioning a new host

```sh
python3 benchmarks/scripts/sync_data.py --to galaxy      # copy + verify
```

The copy is verified against the identity triple (`bytes`, `records`,
`bases`) that `datasets.tsv` already carries and that every run re-checks
before measuring — so a truncated or corrupted transfer fails loudly instead
of quietly benchmarking a different file.

## Changing these files

`suite.toml` and `datasets.tsv` are versioned: see [`../../VERSIONING.md`](../../VERSIONING.md).
`datasets.tsv` is append-only — a regenerated file gets a **new id**, never a
redefinition, so historical results keep pointing at what they actually
measured. Run [`../scripts/validate_suite.py`](../scripts/validate_suite.py)
after any edit; it is cheap and catches the errors that would otherwise
surface an hour into a run.
