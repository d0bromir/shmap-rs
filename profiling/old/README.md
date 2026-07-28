# Superseded profiling data

The previous generation of shmap-rs profiling data, kept for reference. It was measured
before the dense bucket accumulator (`src/buckets.rs`) and the indexing work, at commit
`1de2a54` or earlier, and on a different benchmark session from the current data — so the
numbers here are **not** a valid "before" column for the current ones. Where this repo
quotes a before/after, both sides were re-measured back-to-back; see `PROFILING.md`.

Contents mirror the current `profiling/` layout: eight `*.profile.json` reports (four
Table-1 datasets x `-@1`/`-@16`), the `table1_t*.csv` summaries they came from, the two
shmap-rs comparison CSVs, and the `tables.md` dump generated from them.

Not superseded, and therefore still in `profiling/`: `comparison_other_mappers.csv` and
`comparison_wgs_others.csv`, which describe minimap2 / winnowmap2 / blend / mapquik /
minSHmap / the C++ `shmap`. Those tools did not change, so their numbers stand.
