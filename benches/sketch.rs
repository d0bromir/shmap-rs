use criterion::{Criterion, Throughput, criterion_group, criterion_main};
use rand::{RngExt, SeedableRng};
use shmap::sketch::FracMinHash;
use std::hint::black_box;

/// Generate a sequence of DNA bases of length `len`. All values are guaranteed to be valid, and the
/// RNG is seeded with a constant for reproducability.
pub fn bases(len: usize) -> Vec<u8> {
    const ALPHABET: &[u8; 4] = b"ACGT";
    let mut rng = rand::rngs::SmallRng::seed_from_u64(0x1badb007deadbeef);
    (0..len).map(|_| ALPHABET[rng.random_range(0..4)]).collect()
}

const SAMPLE_LEN: usize = 100_000;

fn fracminhash_benchmark(c: &mut Criterion) {
    let data = bases(SAMPLE_LEN);

    let mut group = c.benchmark_group("synthetic");
    group.sample_size(10);
    group.throughput(Throughput::Bytes(SAMPLE_LEN as u64));

    for k in [6, 10, 16, 25, 32, 64] {
        group.bench_function(format!("hash_window::{k}"), |b| {
            b.iter(|| {
                for window in data.windows(k) {
                    black_box(FracMinHash::hash_window(black_box(window)));
                }
            });
        });

        #[cfg(feature = "simd")]
        group.bench_function(format!("simd::hash_window::{k}"), |b| {
            b.iter(|| {
                for window in data.windows(k) {
                    black_box(shmap::sketch::simd::hash_window(black_box(window)));
                }
            });
        });
    }

    group.finish();
}

criterion_group!(benches, fracminhash_benchmark);
criterion_main!(benches);
