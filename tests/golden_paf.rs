//! Rust-only snapshot test: pins the current binary's PAF output against a
//! checked-in expected file for the tiny fixture. No C++ binary needed —
//! this is the test that should run in the normal dev loop; see
//! `cross_validate.rs` (not present yet — optional per the plan) for the
//! occasional deeper check against the original C++ binary.
//!
//! Only the fields that are actually deterministic are compared: `t:f:`
//! (wall-clock) is stripped from both sides before comparing, since it can
//! never match run-to-run by design.

use assert_cmd::Command;

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

#[test]
fn paf_output_matches_the_checked_in_golden_file() {
    let mut cmd = Command::cargo_bin("shmap").unwrap();
    let assert = cmd
        .arg("-s")
        .arg("tests/fixtures/tiny_ref.fa")
        .arg("-p")
        .arg("tests/fixtures/tiny_reads.fa")
        .arg("-k")
        .arg("8")
        .arg("-r")
        .arg("1.0")
        .arg("-t")
        .arg("0.1")
        .assert()
        .success();

    let output = assert.get_output();
    let stdout = String::from_utf8_lossy(&output.stdout);
    let actual = strip_timing_field(&stdout);

    let expected =
        std::fs::read_to_string("tests/fixtures/tiny.golden.paf").expect("golden file should exist");
    let expected = strip_timing_field(expected.trim_end());

    assert_eq!(actual, expected, "PAF output drifted from the golden file (timing field excluded)");
}
