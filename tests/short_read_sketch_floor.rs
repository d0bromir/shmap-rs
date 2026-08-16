//! Reads whose sketch is below `MIN_HALFLEN` must not be reported as mapped.
//!
//! B06 (41.8 M Illumina reads) made `validate_paf` fail for the first time in
//! this suite's history: 172 records with `qend <= qstart`, every one from a
//! read of length k or k+1. The reported query end is `read_length - k - 1`,
//! which is -1 at `k` and 0 at `k+1`, and PAF requires `0 <= qstart < qend`.
//!
//! The cause was a dropped return value: `Buckets::set_halflen` already
//! reports "too small to map", and the algorithm documentation already says
//! `if buckets.halflen < MIN_HALFLEN: goto unmapped`. The caller ignored both.
//!
//! No previous benchmark could reach it — the shortest read in the suite was
//! 12.8 kb, and reads this short only occur in real adapter-trimmed Illumina
//! data. This test is the thing that would have caught it, so it exists at the
//! size the bug lives at rather than at the sizes the suite happens to use.

use assert_cmd::Command;
use std::io::Write;

const K: usize = 8;

fn fixture(content: &str) -> tempfile::NamedTempFile {
    let mut f = tempfile::Builder::new().suffix(".fa").tempfile().unwrap();
    write!(f, "{content}").unwrap();
    f.flush().unwrap();
    f
}

/// A reference long enough to index, and reads cut from it at lengths that
/// straddle `MIN_HALFLEN` in sketch size. At `-r 1.0` a read of length L has
/// `L - K + 1` k-mers, so the sketch reaches 5 at `L = K + 4`.
fn run(read_lens: &[usize]) -> String {
    let reference: String = {
        let mut s = String::from(">ref\n");
        // Deterministic, non-repetitive enough that short reads have somewhere
        // unambiguous to land.
        let mut x: u64 = 0x2545_F491_4F6C_DD1D;
        for _ in 0..4000 {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            s.push(match x % 4 {
                0 => 'A',
                1 => 'C',
                2 => 'G',
                _ => 'T',
            });
        }
        s.push('\n');
        s
    };
    let ref_seq: String = reference.lines().nth(1).unwrap().to_string();

    let mut reads = String::new();
    for (i, &l) in read_lens.iter().enumerate() {
        reads.push_str(&format!(">len{l}_{i}\n{}\n", &ref_seq[100 + i * 7..100 + i * 7 + l]));
    }

    let ref_f = fixture(&reference);
    let reads_f = fixture(&reads);
    let out = Command::cargo_bin("shmap")
        .unwrap()
        .args([
            "-s",
            ref_f.path().to_str().unwrap(),
            "-p",
            reads_f.path().to_str().unwrap(),
            "-k",
            &K.to_string(),
            "-r",
            "1.0",
            "-t",
            "0.4",
            "-m",
            "Containment",
        ])
        .assert()
        .success();
    String::from_utf8(out.get_output().stdout.clone()).unwrap()
}

/// The invariant PAF itself requires. Any violation is malformed output, which
/// is what `profiling/validate_paf.py` blocks a merge on.
fn assert_every_record_well_formed(paf: &str) {
    for line in paf.lines().filter(|l| !l.trim().is_empty()) {
        let c: Vec<&str> = line.split('\t').collect();
        assert!(c.len() >= 12, "short record: {line}");
        let (qlen, qs, qe): (i64, i64, i64) = (
            c[1].parse().unwrap(),
            c[2].parse().unwrap(),
            c[3].parse().unwrap(),
        );
        assert!(
            0 <= qs && qs < qe && qe <= qlen,
            "malformed query interval {qs}..{qe} of {qlen} in: {line}"
        );
    }
}

#[test]
fn reads_at_and_below_the_sketch_floor_are_not_reported_as_mapped() {
    // K..K+3 give sketches of 1..4, all below MIN_HALFLEN = 5.
    let paf = run(&[K, K + 1, K + 2, K + 3]);
    assert_every_record_well_formed(&paf);
    assert!(
        paf.trim().is_empty(),
        "reads below the sketch floor must produce no mapped record, got:\n{paf}"
    );
}

#[test]
fn reads_above_the_sketch_floor_still_map_and_are_well_formed() {
    // K+4 is the first length with a sketch of MIN_HALFLEN, and must be
    // unaffected — the fix must not swallow reads that were mapping correctly.
    let paf = run(&[K + 4, K + 12, K + 40]);
    assert_every_record_well_formed(&paf);
    assert_eq!(
        paf.lines().filter(|l| !l.trim().is_empty()).count(),
        3,
        "every read at or above the floor should still map:\n{paf}"
    );
}

#[test]
fn a_short_read_mixed_in_does_not_disturb_the_others() {
    // The rejection happens mid-stream, so it must not corrupt the reads
    // around it — an early return that left a timer or counter inconsistent
    // would show up here and nowhere else.
    let paf = run(&[K + 20, K, K + 20, K + 1, K + 20]);
    assert_every_record_well_formed(&paf);
    assert_eq!(
        paf.lines().filter(|l| !l.trim().is_empty()).count(),
        3,
        "the three long reads should map, the two short ones should not:\n{paf}"
    );
}
