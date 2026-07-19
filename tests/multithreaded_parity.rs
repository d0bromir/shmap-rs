//! Multithreading (`-@`/`--threads`) regression test: the mapping pipeline
//! always reorders worker output back into input order before writing it
//! (see the module doc comment on `shmap::shmap`), so running the same
//! input through 1, 3, and 8 threads should produce byte-identical PAF
//! output (timing field aside) regardless of how the reads race across
//! worker threads.

use assert_cmd::Command;
use std::io::Write;

/// A small deterministic (xorshift-seeded) ACGT sequence generator — no
/// `rand` dependency needed, matches the helper already used in
/// `src/analyse_simulated.rs`'s own tests.
fn pseudo_random_dna(seed: u64, len: usize) -> Vec<u8> {
    let bases = [b'A', b'C', b'G', b'T'];
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

fn strip_timing_field(paf: &str) -> String {
    paf.lines()
        .map(|line| {
            line.split('\t')
                .filter(|field| !field.starts_with("t:f:"))
                .collect::<Vec<_>>()
                .join("\t")
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn run_with_threads(ref_path: &str, reads_path: &str, threads: &str) -> String {
    let mut cmd = Command::cargo_bin("shmap").unwrap();
    let assert = cmd
        .arg("-s")
        .arg(ref_path)
        .arg("-p")
        .arg(reads_path)
        .arg("-k")
        .arg("12")
        .arg("-r")
        .arg("0.5")
        .arg("-t")
        .arg("0.2")
        .arg("-@")
        .arg(threads)
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(&assert.get_output().stdout).into_owned();
    strip_timing_field(&stdout)
}

#[test]
fn threaded_output_matches_single_threaded_output() {
    let reference = pseudo_random_dna(42, 20_000);
    let ref_fa = format!(">chr1\n{}\n", String::from_utf8(reference.clone()).unwrap());

    // A mix of mapped (real substrings, varying length/position) and
    // unmapped (random, unrelated) reads, interleaved, so the reorder
    // buffer actually has to do work across both outcome kinds.
    let mut reads_fa = String::new();
    for i in 0..60u64 {
        let start = ((i * 137) % 19_000) as usize;
        let len = 200 + (i as usize % 5) * 50;
        let read_seq = &reference[start..start + len];
        reads_fa.push_str(&format!(">mapped_{i}\n{}\n", String::from_utf8(read_seq.to_vec()).unwrap()));

        if i % 4 == 0 {
            let junk = pseudo_random_dna(9000 + i, 150);
            reads_fa.push_str(&format!(">unmapped_{i}\n{}\n", String::from_utf8(junk).unwrap()));
        }
    }

    let mut ref_file = tempfile::Builder::new().suffix(".fa").tempfile().unwrap();
    ref_file.write_all(ref_fa.as_bytes()).unwrap();
    ref_file.flush().unwrap();
    let mut reads_file = tempfile::Builder::new().suffix(".fa").tempfile().unwrap();
    reads_file.write_all(reads_fa.as_bytes()).unwrap();
    reads_file.flush().unwrap();

    let ref_path = ref_file.path().to_str().unwrap();
    let reads_path = reads_file.path().to_str().unwrap();

    let single = run_with_threads(ref_path, reads_path, "1");
    let three = run_with_threads(ref_path, reads_path, "3");
    let eight = run_with_threads(ref_path, reads_path, "8");

    assert!(!single.is_empty(), "expected at least some reads to map");
    assert_eq!(single, three, "3-thread output diverged from single-threaded output");
    assert_eq!(single, eight, "8-thread output diverged from single-threaded output");
}
