//! Q9: does software prefetching hide DRAM latency for the kind of random
//! hashtable lookup `seed_heuristic_pass`/`matches_in_bucket` does against
//! the reference index (`tidx.single_hit`/`multi_hits`)?
//!
//! That loop's own doc comment already calls the workload memory-latency
//! bound, and it is the one that actually touches the multi-GB index
//! randomly (`best_fixed_length`'s own hashmap, `p_ht`, is per-read and
//! small enough to be L1/L2-resident by the time it matters — not the
//! same kind of access at all, and not what this probes).
//!
//! A plain open-addressing table here, not `std`/`hashbrown`, so an
//! explicit `_mm_prefetch` has a real address to target — neither exposes
//! one for an arbitrary key without doing the lookup itself. The DRAM
//! round-trip is the dominant cost either way, so this is a fair proxy for
//! whether prefetching helps *this class* of access on this host,
//! independent of which concrete hash table implementation is doing it.
//!
//! Three variants, same table, same query stream:
//!   1. baseline  -- one lookup at a time, nothing hidden
//!   2. lookahead -- issue lookup i+D before consuming lookup i's result
//!                   (pure safe Rust; relies on out-of-order execution
//!                   noticing the loads are independent)
//!   3. prefetch  -- lookahead, plus an explicit `_mm_prefetch` hint issued
//!                   D iterations ahead of consumption
//!
//! Build and run: `rustc -O -C target-cpu=native prefetch_probe.rs -o
//! /tmp/prefetch_probe && /tmp/prefetch_probe`

use std::arch::x86_64::{_MM_HINT_T0, _mm_prefetch};
use std::hint::black_box;
use std::time::Instant;

// Minimal FxHash-style mix, not linking `rustc-hash` for a standalone probe.
#[inline(always)]
fn mix(mut h: u64, k: u64) -> u64 {
    const SEED: u64 = 0x51_7c_c1_b7_27_22_0a_95;
    h = (h.rotate_left(5) ^ k).wrapping_mul(SEED);
    h
}

#[inline(always)]
fn splitmix64(x: &mut u64) -> u64 {
    *x = x.wrapping_add(0x9E3779B97F4A7C15);
    let mut z = *x;
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
    z ^ (z >> 31)
}

const EMPTY: u64 = u64::MAX;

struct Table {
    keys: Vec<u64>,
    vals: Vec<u32>,
    mask: usize,
}

impl Table {
    fn build(n: usize, mut seed: u64) -> (Self, Vec<u64>) {
        let capacity = (n * 2).next_power_of_two();
        let mask = capacity - 1;
        let mut keys = vec![EMPTY; capacity];
        let mut vals = vec![0u32; capacity];
        let mut real_keys = Vec::with_capacity(n);
        for i in 0..n {
            let mut k = splitmix64(&mut seed);
            if k == EMPTY {
                k -= 1;
            }
            let mut idx = (mix(0, k) as usize) & mask;
            while keys[idx] != EMPTY {
                idx = (idx + 1) & mask;
            }
            keys[idx] = k;
            vals[idx] = i as u32;
            real_keys.push(k);
        }
        (Table { keys, vals, mask }, real_keys)
    }

    #[inline(always)]
    fn probe_index(&self, k: u64) -> usize {
        (mix(0, k) as usize) & self.mask
    }

    #[inline(always)]
    fn get_from(&self, mut idx: usize, k: u64) -> Option<u32> {
        loop {
            let stored = self.keys[idx];
            if stored == k {
                return Some(self.vals[idx]);
            }
            if stored == EMPTY {
                return None;
            }
            idx = (idx + 1) & self.mask;
        }
    }

    #[inline(always)]
    fn get(&self, k: u64) -> Option<u32> {
        let idx = self.probe_index(k);
        self.get_from(idx, k)
    }

    #[inline(always)]
    fn prefetch(&self, idx: usize) {
        unsafe {
            _mm_prefetch::<{ _MM_HINT_T0 }>(self.keys.as_ptr().add(idx) as *const i8);
        }
    }
}

fn make_queries(n: usize, real_keys: &[u64], hit_rate: f64, mut seed: u64) -> Vec<u64> {
    let mut out = Vec::with_capacity(n);
    for _ in 0..n {
        let r = splitmix64(&mut seed);
        if (r as f64 / u64::MAX as f64) < hit_rate {
            let i = (splitmix64(&mut seed) as usize) % real_keys.len();
            out.push(real_keys[i]);
        } else {
            out.push(splitmix64(&mut seed));
        }
    }
    out
}

fn bench_baseline(table: &Table, queries: &[u64]) -> (u64, u128) {
    let mut acc = 0u64;
    let t0 = Instant::now();
    for &k in queries {
        if let Some(v) = table.get(k) {
            acc = acc.wrapping_add(v as u64);
        }
    }
    (black_box(acc), t0.elapsed().as_nanos())
}

fn bench_lookahead(table: &Table, queries: &[u64], depth: usize) -> (u64, u128) {
    let mut acc = 0u64;
    let n = queries.len();
    let mut idxs = vec![0usize; depth];
    let t0 = Instant::now();
    for i in 0..depth.min(n) {
        idxs[i] = table.probe_index(queries[i]);
    }
    for i in 0..n {
        let slot = i % depth;
        if let Some(v) = table.get_from(idxs[slot], queries[i]) {
            acc = acc.wrapping_add(v as u64);
        }
        let ahead = i + depth;
        if ahead < n {
            idxs[slot] = table.probe_index(queries[ahead]);
        }
    }
    (black_box(acc), t0.elapsed().as_nanos())
}

fn bench_prefetch(table: &Table, queries: &[u64], depth: usize) -> (u64, u128) {
    let mut acc = 0u64;
    let n = queries.len();
    let mut idxs = vec![0usize; depth];
    let t0 = Instant::now();
    for i in 0..depth.min(n) {
        idxs[i] = table.probe_index(queries[i]);
        table.prefetch(idxs[i]);
    }
    for i in 0..n {
        let slot = i % depth;
        if let Some(v) = table.get_from(idxs[slot], queries[i]) {
            acc = acc.wrapping_add(v as u64);
        }
        let ahead = i + depth;
        if ahead < n {
            idxs[slot] = table.probe_index(queries[ahead]);
            table.prefetch(idxs[slot]);
        }
    }
    (black_box(acc), t0.elapsed().as_nanos())
}

fn main() {
    // ~4M entries: this host's real per-shard scale (~31M hits / 8 shards),
    // large enough to guarantee blowing past L3 (a few tens of MB here).
    let n_entries = 4_000_000usize;
    let n_queries = 20_000_000usize;
    let (table, real_keys) = Table::build(n_entries, 0xC0FFEE);
    println!(
        "table: {} entries, capacity {} ({} MB keys+vals)",
        n_entries,
        table.keys.len(),
        table.keys.len() * (8 + 4) / 1_000_000
    );

    for hit_rate in [0.1, 0.5, 0.9] {
        let queries = make_queries(n_queries, &real_keys, hit_rate, 0xBADF00D ^ (hit_rate.to_bits()));
        println!("\n-- hit_rate={hit_rate:.1} --");

        let (acc_b, ns_b) = bench_baseline(&table, &queries);
        let ns_per_b = ns_b as f64 / n_queries as f64;
        println!("baseline           acc={acc_b:20}  {ns_per_b:6.2} ns/query");

        for depth in [4usize, 8, 16, 32] {
            let (acc_l, ns_l) = bench_lookahead(&table, &queries, depth);
            let ns_per_l = ns_l as f64 / n_queries as f64;
            let (acc_p, ns_p) = bench_prefetch(&table, &queries, depth);
            let ns_per_p = ns_p as f64 / n_queries as f64;
            assert_eq!(acc_b, acc_l, "lookahead changed the result!");
            assert_eq!(acc_b, acc_p, "prefetch changed the result!");
            println!(
                "lookahead D={depth:<3}     acc={acc_l:20}  {ns_per_l:6.2} ns/query  ({:5.2}x baseline)",
                ns_per_b / ns_per_l
            );
            println!(
                "prefetch  D={depth:<3}     acc={acc_p:20}  {ns_per_p:6.2} ns/query  ({:5.2}x baseline)",
                ns_per_b / ns_per_p
            );
        }
    }
}
