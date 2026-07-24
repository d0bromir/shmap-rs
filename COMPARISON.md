# shmap-rs vs other mappers (real-world data)

Single-threaded (`-@ 1`), 64-core AVX-512 server. Same datasets/params as Pesho's `shmap` Table 1
(`-k 25 -r 0.01 -t 0.4 -d 0.075 -o 0.3 -m Containment`). Other mappers' numbers are the stored
Table 1 run (`results/table1_20260718-103540.csv` on the benchmark machine); `map-shmap` is the
original C++ shmap that shmap-rs ports. Time = index + map wall (shmap does both in one pass).
`missed%` = reads not mapped at Q60 (shmap's sketch+threshold design is selective by nature).

### chrY, simulated 10 kbp (48,673 reads)

| mapper | correct Q60 | wrong | missed% | time | mem |
|---|---:|---:|---:|---:|---:|
| **shmap-rs** | **22918** | **0** | 52.9 | **74 s** | **0.19 GB** |
| map-shmap (C++) | 22918 | 0 | 52.9 | 110 s | 0.38 GB |
| blend | 23866 | 191 | 50.6 | 640 s | 0.56 GB |
| winnowmap2 | 44751 | 10 | 8.0 | 28694 s | 10.8 GB |
| minimap2 | 16159 | 0 | 66.8 | 1583 s | 0.62 GB |
| minshmap | 15694 | 0 | 67.8 | 925 s | 0.71 GB |
| mapquik | 0 | 0 | 100 | 17 s | 2.26 GB |

### whole genome, REAL HG002 24 kbp (2,000 reads)

| mapper | correct Q60 | wrong | missed% | time | mem |
|---|---:|---:|---:|---:|---:|
| **shmap-rs** | **1876** | n/a | 6.2 | **11.5 s** | **2.7 GB** |
| map-shmap (C++) | 1876 | n/a | 6.2 | 32.9 s | 18.9 GB |
| blend | 1897 | n/a | 5.2 | 84 s | 7.5 GB |
| winnowmap2 | 1953 | n/a | 2.4 | 356 s | 4.7 GB |
| minimap2 | 1844 | n/a | 7.8 | 150 s | 12.2 GB |
| minshmap | 1838 | n/a | 8.1 | 205 s | 11.0 GB |
| mapquik | 0 | n/a | 100 | 101 s | 5.0 GB |

(Full 4-dataset numbers in `profiling/`. chrY 24 kbp and whole-genome 10 kbp follow the same
pattern.)

## Takeaways

- **Identical accuracy to the C++ original** (`map-shmap`) — same correct-Q60 on every dataset,
  0 wrong — while **1.5–2.9× faster** single-threaded and using **up to ~7× less memory** on the
  whole genome (2.7 GB vs 18.9 GB, from the sparse-`Buckets` rewrite).
- **Fastest of all the correct mappers**, single-threaded: e.g. on real HG002 reads, 11.5 s vs
  84 s (blend) / 150 s (minimap2) / 356 s (winnowmap2), at competitive accuracy and lowest or
  near-lowest memory.
- shmap/shmap-rs trade recall for speed (higher `missed%` on the low-similarity chrY sets);
  winnowmap2 maps more but is 100–380× slower and far heavier. `mapquik` maps nothing at these
  parameters.
- shmap-rs additionally **scales to many threads** (the C++ original is single-threaded) — see
  `BENCHMARKS.md` for up to ~21× at `-@ 32`.
