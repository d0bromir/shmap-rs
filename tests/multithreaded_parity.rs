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
        reads_fa.push_str(&format!(
            ">mapped_{i}\n{}\n",
            String::from_utf8(read_seq.to_vec()).unwrap()
        ));

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

/// `--per-read-stats` writes from the same in-order collector as the PAF, so
/// it inherits the same guarantee — and it must not perturb the PAF itself.
///
/// Both halves matter. A per-read file whose row order depended on which
/// worker finished first would be useless as a regression baseline and would
/// silently reorder under `-@`. And instrumentation that changed the output it
/// measures would invalidate every figure drawn from it.
#[test]
fn per_read_stats_are_deterministic_and_do_not_perturb_output() {
    let reference = pseudo_random_dna(7, 20_000);
    let ref_fa = format!(">chr1\n{}\n", String::from_utf8(reference.clone()).unwrap());

    let mut reads_fa = String::new();
    for i in 0..40u64 {
        let start = ((i * 211) % 19_000) as usize;
        let read_seq = &reference[start..start + 300];
        reads_fa.push_str(&format!(">r_{i}\n{}\n", String::from_utf8(read_seq.to_vec()).unwrap()));
        if i % 5 == 0 {
            let junk = pseudo_random_dna(5000 + i, 150);
            reads_fa.push_str(&format!(">junk_{i}\n{}\n", String::from_utf8(junk).unwrap()));
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

    let dir = tempfile::tempdir().unwrap();
    let run = |threads: &str, out: &std::path::Path| -> String {
        let mut cmd = Command::cargo_bin("shmap").unwrap();
        let assert = cmd
            .args(["-s", ref_path, "-p", reads_path, "-k", "12", "-r", "0.5", "-t", "0.2"])
            .args(["-@", threads])
            .arg("--per-read-stats")
            .arg(out)
            .assert()
            .success();
        strip_timing_field(&String::from_utf8_lossy(&assert.get_output().stdout))
    };

    // The measured time varies run to run by construction, so the comparison
    // is over every column except it. If the timing column were included this
    // test would flake, and dropping the whole file from the comparison would
    // check nothing.
    let drop_time = |s: &str| -> String {
        s.lines()
            .map(|l| l.rsplit_once('\t').map(|(rest, _)| rest).unwrap_or(l).to_string())
            .collect::<Vec<_>>()
            .join("\n")
    };

    let p1 = dir.path().join("t1.tsv");
    let p3 = dir.path().join("t3.tsv");
    let p8 = dir.path().join("t8.tsv");
    let paf1 = run("1", &p1);
    let paf3 = run("3", &p3);
    run("8", &p8);

    let s1 = drop_time(&std::fs::read_to_string(&p1).unwrap());
    let s3 = drop_time(&std::fs::read_to_string(&p3).unwrap());
    let s8 = drop_time(&std::fs::read_to_string(&p8).unwrap());

    assert!(s1.lines().count() > 40, "expected a row per read plus a header");
    assert!(s1.starts_with("query_id\t"), "expected the header row first");
    assert_eq!(s1, s3, "per-read stats diverged at -@3");
    assert_eq!(s1, s8, "per-read stats diverged at -@8");

    // Every read must appear, mapped or not: a scatter built from mapped reads
    // only would silently drop the cheapest ones and bend the curve.
    assert!(s1.contains("\njunk_0\t"), "unmapped reads must be present too");

    // And the instrumentation must be inert with respect to the PAF.
    let mut plain = Command::cargo_bin("shmap").unwrap();
    let bare = plain
        .args(["-s", ref_path, "-p", reads_path, "-k", "12", "-r", "0.5", "-t", "0.2"])
        .assert()
        .success();
    let bare = strip_timing_field(&String::from_utf8_lossy(&bare.get_output().stdout));
    assert_eq!(bare, paf1, "--per-read-stats changed the PAF it is measuring");
    assert_eq!(bare, paf3, "--per-read-stats changed the PAF at -@3");

    // Sampling keeps every Nth read by index, so it is a strict subsequence
    // and reproducible without reference to thread count.
    let p_s = dir.path().join("sampled.tsv");
    let mut cmd = Command::cargo_bin("shmap").unwrap();
    cmd.args(["-s", ref_path, "-p", reads_path, "-k", "12", "-r", "0.5", "-t", "0.2"])
        .args(["--per-read-stats-sample", "10"])
        .arg("--per-read-stats")
        .arg(&p_s)
        .assert()
        .success();
    let sampled = std::fs::read_to_string(&p_s).unwrap();
    let full_rows = s1.lines().count() - 1;
    let sampled_rows = sampled.lines().count() - 1;
    assert_eq!(
        sampled_rows,
        full_rows.div_ceil(10),
        "sampling every 10th read should keep ceil(n/10) rows"
    );
}
