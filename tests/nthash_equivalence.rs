//! Is our sketching hash the same function as the `nthash` crate's, and would
//! the crate be faster?
//!
//! Q1 asks whether `sketch.rs` can be replaced by an already-optimised
//! library. This answers the two questions that decides it: whether a library
//! can reproduce our hashes bit for bit (if not, every mapping changes and the
//! merge gate blocks it), and whether it would actually be faster.
//!
//! Run with `cargo test --release --test nthash_equivalence -- --nocapture`.

use nthash::{NtHashIterator, ntf64, ntr64};
use shmap::sketch::FracMinHash;
use shmap::utils::Counters;

fn pseudo_random_dna(seed: u64, len: usize) -> Vec<u8> {
    let bases = b"ACGT";
    let mut state = seed;
    (0..len)
        .map(|_| {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            bases[(state % 4) as usize]
        })
        .collect()
}

/// Our per-window forward and reverse hashes, recomputed directly from the
/// definition in `sketch.rs` rather than by rolling — the point is to compare
/// the *function*, not our implementation of it.
fn ours_fw_rc(seq: &[u8], k: usize) -> Vec<(u64, u64)> {
    const A: u64 = 0x3c8b_fbb3_95c6_0474;
    const C: u64 = 0x3193_c185_62a0_2b4c;
    const G: u64 = 0x2032_3ed0_8257_2324;
    const TN: u64 = 0x2955_49f5_4be2_4456;
    let fw = |c: u8| match c {
        b'A' | b'a' => A,
        b'C' | b'c' => C,
        b'G' | b'g' => G,
        b'T' | b't' => TN,
        _ => 0,
    };
    let rc = |c: u8| match c {
        b'A' | b'a' => TN,
        b'C' | b'c' => G,
        b'G' | b'g' => C,
        b'T' | b't' => A,
        _ => 0,
    };
    (0..=seq.len() - k)
        .map(|i| {
            let mut h_fw = 0u64;
            let mut h_rc = 0u64;
            for j in 0..k {
                h_fw ^= fw(seq[i + j]).rotate_left((k - 1 - j) as u32);
                h_rc ^= rc(seq[i + j]).rotate_left(j as u32);
            }
            (h_fw, h_rc)
        })
        .collect()
}

/// The finding this whole question turns on: the *hash* is ntHash, so a
/// library could in principle supply it — but the *canonical form* is not.
#[test]
fn our_hash_is_nthash_but_our_canonical_form_is_not() {
    let seq = pseudo_random_dna(11, 5_000);
    for k in [15usize, 21, 25, 31] {
        let ours = ours_fw_rc(&seq, k);
        for (i, &(h_fw, h_rc)) in ours.iter().enumerate() {
            assert_eq!(h_fw, ntf64(&seq, i, k), "forward hash differs at {i}, k={k}");
            assert_eq!(h_rc, ntr64(&seq, i, k), "reverse hash differs at {i}, k={k}");
        }

        // ...but the crate's canonical value is min(), and ours is xor. The
        // two agree only by coincidence, so a drop-in swap would change which
        // k-mers are sketched and therefore every mapping in the output.
        let crate_canonical: Vec<u64> = NtHashIterator::new(&seq, k).unwrap().collect();
        let ours_canonical: Vec<u64> = ours.iter().map(|&(f, r)| f ^ r).collect();
        assert_eq!(crate_canonical.len(), ours_canonical.len());
        let agreeing = crate_canonical
            .iter()
            .zip(&ours_canonical)
            .filter(|(a, b)| a == b)
            .count();
        assert!(
            agreeing * 100 < crate_canonical.len(),
            "k={k}: min() and xor agreed on {agreeing}/{} windows, which would mean \
             the canonical forms are not actually different",
            crate_canonical.len()
        );
    }
}

/// Our rolling implementation agrees with the direct definition, so the
/// comparison above is about the function and not about our loop.
#[test]
fn our_rolling_loop_matches_the_direct_definition() {
    let seq = pseudo_random_dna(12, 5_000);
    let k = 25usize;
    // h_frac 1.0 keeps every k-mer, so positions line up with windows.
    let sketcher = FracMinHash::new(k as i32, 1.0);
    let mut counters = Counters::new();
    let sketched = sketcher.sketch(&seq, &mut counters);
    let direct = ours_fw_rc(&seq, k);
    assert_eq!(sketched.len(), direct.len());
    for (i, kmer) in sketched.iter().enumerate() {
        let (h_fw, h_rc) = direct[i];
        assert_eq!(kmer.h, h_fw ^ h_rc, "hash differs at window {i}");
        assert_eq!(kmer.strand, h_fw > h_rc, "strand differs at window {i}");
    }
}

/// Speed at the production sampling rate, doing the same work on both sides.
///
/// Getting this comparison fair is most of the point. At `h_frac = 1.0` our
/// side pushes a record for every window — 20 M allocations' worth — while an
/// iterator that only folds a checksum does none, which measures `Vec` growth
/// rather than hashing and flatters the crate by ~2x. Both sides here run at
/// `-r 0.01`, the rate the suite actually uses, and both push the selected
/// windows into a pre-reserved buffer.
///
/// The crate side still gets the easier job: `NtHashIterator` yields only
/// `min(fw, rc)`, so it cannot produce the strand bit and pushes a smaller
/// record. Read the result as an upper bound on what the crate could offer.
///
/// Not a merge gate — a timing assertion on a shared host would be flaky. It
/// prints, and the numbers go in QUESTIONS.md.
#[test]
fn report_relative_speed() {
    let seq = pseudo_random_dna(13, 20_000_000);
    let k = 25usize;
    let h_frac = 0.01f64;
    let thres = (h_frac * u64::MAX as f64) as u64;
    let sketcher = FracMinHash::new(k as i32, h_frac);
    let mut counters = Counters::new();

    // Halved, so both sides select the same number of k-mers. `min(fw, rc)`
    // clears a threshold about twice as often as `fw ^ rc` does — P(min <= t)
    // is ~2t for small t, against t for a uniform value — so at the same
    // threshold the crate would push twice as many records and lose on
    // allocation rather than on hashing.
    let crate_thres = thres / 2;
    let run_crate = |seq: &[u8]| -> usize {
        let mut out: Vec<(usize, u64)> = Vec::with_capacity(1 + (seq.len() as f64 * h_frac) as usize);
        for (i, h) in NtHashIterator::new(seq, k).unwrap().enumerate() {
            if h <= crate_thres {
                out.push((i, h));
            }
        }
        out.len()
    };

    // Warm the allocator and the branch predictors identically for both.
    let _ = sketcher.sketch(&seq[..1_000_000], &mut counters);
    let _ = run_crate(&seq[..1_000_000]);

    let t0 = std::time::Instant::now();
    let ours = sketcher.sketch(&seq, &mut counters);
    let ours_s = t0.elapsed().as_secs_f64();

    let t1 = std::time::Instant::now();
    let n_crate = run_crate(&seq);
    let crate_s = t1.elapsed().as_secs_f64();

    let mb = seq.len() as f64 / 1e6;
    println!("\n  at -r {h_frac}, k={k}, {mb:.0} Mbase");
    println!(
        "  sketch.rs        {ours_s:.3} s  ({:.1} Mbase/s)  {} kmers",
        mb / ours_s,
        ours.len()
    );
    println!(
        "  nthash crate     {crate_s:.3} s  ({:.1} Mbase/s)  {n_crate} kmers",
        mb / crate_s
    );
    println!("  crate / ours     {:.2}x\n", crate_s / ours_s);
    assert!(!ours.is_empty() && n_crate > 0);
}
