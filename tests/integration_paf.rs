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
        assert!(
            fields.len() >= 12,
            "PAF line has fewer than 12 mandatory columns: {line}"
        );
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

#[test]
fn compact_and_cached_indexes_preserve_paf() {
    let directory = tempfile::tempdir().unwrap();
    let cache = directory.path().join("reference.idx");
    let run = |extra: &[&str]| {
        let mut command = Command::cargo_bin("shmap").unwrap();
        let output = command
            .args([
                "-s",
                "tests/fixtures/tiny_ref.fa",
                "-p",
                "tests/fixtures/tiny_reads.fa",
                "-k",
                "8",
                "-r",
                "1",
                "-t",
                "0.1",
            ])
            .args(extra)
            .assert()
            .success()
            .get_output()
            .stdout
            .clone();
        String::from_utf8(output)
            .unwrap()
            .lines()
            .map(|line| {
                line.split('\t')
                    .filter(|field| !field.starts_with("t:f:"))
                    .collect::<Vec<_>>()
                    .join("\t")
            })
            .collect::<Vec<_>>()
    };
    let expected = run(&[]);
    assert_eq!(run(&["--compact-index"]), expected);
    assert_eq!(run(&["--adaptive"]), expected);
    assert_eq!(run(&["--adaptive", "--adaptive-dense"]), expected);
    assert_eq!(run(&["--read-batch-size", "2"]), expected);
    assert_eq!(run(&["--read-batch-size", "2", "--reader-threads", "4"]), expected);
    assert_eq!(run(&["--index-cache", cache.to_str().unwrap()]), expected);
    assert_eq!(
        run(&[
            "--index-cache",
            cache.to_str().unwrap(),
            "--verify-index-reference",
            "-@",
            "2"
        ]),
        expected
    );
}

#[test]
fn adaptive_cli_maps_long_reads_deterministically() {
    let directory = tempfile::tempdir().unwrap();
    let reference = directory.path().join("ref.fa");
    let reads = directory.path().join("reads.fa");
    let mut state = 123u64;
    let sequence: String = (0..40_000)
        .map(|_| {
            state = state.wrapping_mul(6364136223846793005).wrapping_add(1);
            b"ACGT"[(state >> 32) as usize & 3] as char
        })
        .collect();
    let forward = &sequence[5000..17_000];
    let reverse: String = forward
        .chars()
        .rev()
        .map(|base| match base {
            'A' => 'T',
            'C' => 'G',
            'G' => 'C',
            _ => 'A',
        })
        .collect();
    std::fs::write(&reference, format!(">ref\n{sequence}\n")).unwrap();
    let records: String = (0..33)
        .map(|number| format!(">forward{number}\n{forward}\n>reverse{number}\n{reverse}\n"))
        .collect();
    std::fs::write(&reads, records).unwrap();
    let run = |threads: &str| {
        let mut command = Command::cargo_bin("shmap").unwrap();
        command
            .args([
                "-s",
                reference.to_str().unwrap(),
                "-p",
                reads.to_str().unwrap(),
                "-k",
                "25",
                "-r",
                "0.05",
                "-t",
                "0.4",
                "--adaptive",
                "--compact-index",
                "--read-batch-size",
                "16",
                "-@",
                threads,
            ])
            .assert()
            .success()
            .get_output()
            .stdout
            .clone()
    };
    let output = run("1");
    assert_eq!(run("4"), output);
    let output = String::from_utf8(output).unwrap();
    assert_eq!(output.lines().count(), 66);
    for (line, strand) in output.lines().zip(["+", "-"].into_iter().cycle()) {
        let fields: Vec<_> = line.split('\t').collect();
        assert_eq!(
            (fields[4], fields[7], fields[8], fields[11]),
            (strand, "5000", "17000", "255")
        );
        assert!(line.contains("am:Z:adaptive-v1"));
        assert!(!line.contains("J:f:"));
    }
}
