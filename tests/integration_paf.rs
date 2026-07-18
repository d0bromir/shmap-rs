//! End-to-end CLI test: runs the built `shmap` binary against the checked-in
//! fixtures and asserts a successful, non-trivial PAF result. Replaces the
//! C++ Makefile's shell-based `integration_test` target (same fixture:
//! `test/data/ref.fa`/`reads.fa` there, `tests/fixtures/tiny_*.fa` here).

use assert_cmd::Command;

#[test]
fn maps_reads_against_the_tiny_fixture_and_prints_paf() {
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

    let lines: Vec<&str> = stdout.lines().collect();
    assert_eq!(lines.len(), 2, "expected one PAF line per read, got: {stdout}");

    for line in &lines {
        let fields: Vec<&str> = line.split('\t').collect();
        assert!(fields.len() >= 12, "PAF line has fewer than 12 mandatory columns: {line}");
        assert_eq!(fields[5], "ref", "target name column");
        assert_eq!(fields[4], "+", "strand column");
    }

    assert!(lines[0].starts_with("read1\t"));
    assert!(lines[1].starts_with("read2\t"));
}

#[test]
fn rejects_an_invalid_parameter_with_a_clear_error() {
    let mut cmd = Command::cargo_bin("shmap").unwrap();
    cmd.arg("-s")
        .arg("tests/fixtures/tiny_ref.fa")
        .arg("-p")
        .arg("tests/fixtures/tiny_reads.fa")
        .arg("-k")
        .arg("0") // invalid: k must be positive
        .assert()
        .failure()
        .stderr(predicates::str::contains("K-mer length"));
}
