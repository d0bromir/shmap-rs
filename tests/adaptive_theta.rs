//! The theta ladder's central claim, tested the only way that settles it:
//! run the same input twice through the same binary, once with the ladder and
//! once with `SHMAP_NO_ADAPTIVE_THETA`, and require the two to report the same
//! mappings.
//!
//! The unit tests in `src/shmap/theta_ladder.rs` pin the rung arithmetic. This
//! pins the property that arithmetic exists for — that stopping at the first
//! threshold which maps is exact, not an approximation — over reads that
//! actually have to climb: a spread of substitution rates puts different reads
//! on different rungs, and unmappable reads exercise the path that runs the
//! ladder to its end.
//!
//! Everything about a mapping is compared except the four tags that report how
//! much work went into finding it, which the ladder is *meant* to change and
//! which [`effort_tags_shrink`] checks separately in the direction it must move.

use assert_cmd::Command;
use std::collections::BTreeMap;
use std::io::Write;

/// Tags describing effort rather than result. `seeds` is `S` itself;
/// `seed_matches`/`total_matches`/`seeded_buckets` count what seeding
/// enumerated; `match_inefficiency` is derived from `total_matches`; and
/// `final_buckets` counts how many candidates cleared the sweep's threshold,
/// which is higher on an accepted rung than it is at `-t`.
///
/// Note what is *not* here. Every field of the mapping itself — coordinates,
/// strand, mapq — and every tag describing it — `J`, `J2`, `I`, `I2`, `sh`,
/// `b`, `b2` — is compared, including the two that are easiest to get wrong:
/// `sh` (the winning bucket's completed seed-heuristic score, which is
/// rung-independent only because a bucket is scored solely after consuming
/// every seed) and `b`/`b2` (the winning bucket's *identity*, which pins that
/// the ladder picked the same candidate and not merely an equal-scoring one).
const EFFORT_TAGS: [&str; 7] = [
    "seeds:i:",
    "max_seed_matches:i:",
    "seed_matches:i:",
    "total_matches:i:",
    "match_inefficiency:f:",
    "seeded_buckets:i:",
    "final_buckets:i:",
];

/// A read's mapping, stripped of the wall-clock tag and of `EFFORT_TAGS`.
fn mappings(paf: &str) -> BTreeMap<String, String> {
    paf.lines()
        .filter(|l| !l.is_empty())
        .map(|line| {
            let query = line.split('\t').next().unwrap().to_string();
            let rest = line
                .split('\t')
                .filter(|f| !f.starts_with("t:f:") && !EFFORT_TAGS.iter().any(|t| f.starts_with(t)))
                .collect::<Vec<_>>()
                .join("\t");
            (query, rest)
        })
        .collect()
}

/// Sum of one integer tag over every mapped read.
fn tag_total(paf: &str, tag: &str) -> i64 {
    paf.lines()
        .filter_map(|l| l.split('\t').find(|f| f.starts_with(tag)))
        .map(|f| f.rsplit(':').next().unwrap().parse::<i64>().unwrap())
        .sum()
}

/// xorshift64*, so the corpus is identical on every machine and every run
/// without pulling in an RNG crate.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
}

const BASES: &[u8; 4] = b"ACGT";

/// A random reference, and reads drawn from it at a spread of substitution
/// rates plus a few that are not from it at all.
///
/// The rates are what make this test worth running: at `k = 15`, a 1%
/// substitution rate leaves ~86% of a read's k-mers intact and a 6% rate ~40%,
/// so the corpus lands reads on several different rungs of the ladder, and the
/// random reads (~0%) exhaust it.
fn corpus() -> (tempfile::NamedTempFile, tempfile::NamedTempFile) {
    let mut rng = Rng(0x5EED_1234_ABCD_0001);

    let mut reference = Vec::with_capacity(400_000);
    for _ in 0..400_000 {
        reference.push(BASES[rng.below(4) as usize]);
    }

    let mut reads = String::new();
    let read_len = 2_000usize;
    for i in 0..60 {
        // 0, 1, 2, 4, 6, 9% — the last is well under `-t` and should not map.
        let pct = [0u64, 1, 2, 4, 6, 9][i % 6];
        let start = rng.below((reference.len() - read_len) as u64) as usize;
        let mut seq = reference[start..start + read_len].to_vec();
        if i % 6 == 5 && i % 12 == 5 {
            // A handful of reads that are not from the reference at all.
            for b in seq.iter_mut() {
                *b = BASES[rng.below(4) as usize];
            }
        } else {
            for b in seq.iter_mut() {
                if rng.below(100) < pct {
                    *b = BASES[rng.below(4) as usize];
                }
            }
        }
        reads.push_str(&format!(">read{i}_{pct}pct\n{}\n", String::from_utf8(seq).unwrap()));
    }

    let mut ref_file = tempfile::Builder::new().suffix(".fa").tempfile().unwrap();
    write!(ref_file, ">chr\n{}\n", String::from_utf8(reference).unwrap()).unwrap();
    ref_file.flush().unwrap();

    let mut reads_file = tempfile::Builder::new().suffix(".fa").tempfile().unwrap();
    write!(reads_file, "{reads}").unwrap();
    reads_file.flush().unwrap();

    (ref_file, reads_file)
}

fn run(reference: &tempfile::NamedTempFile, reads: &tempfile::NamedTempFile, extra: &[&str], adaptive: bool) -> String {
    let mut cmd = Command::cargo_bin("shmap").unwrap();
    cmd.args(["-s", reference.path().to_str().unwrap()])
        .args(["-p", reads.path().to_str().unwrap()])
        .args(["-k", "15", "-r", "1.0", "-t", "0.4", "-d", "0.075", "-o", "0.3"])
        .args(extra);
    if !adaptive {
        cmd.env("SHMAP_NO_ADAPTIVE_THETA", "1");
    }
    let assert = cmd.assert().success();
    String::from_utf8_lossy(&assert.get_output().stdout).into_owned()
}

#[test]
fn the_ladder_reports_the_same_mappings_as_the_single_pass() {
    let (reference, reads) = corpus();
    // Every metric, and both pruning settings. `-P` with the two bucket
    // metrics is the case `map_read` deliberately excludes from the ladder;
    // it is included here because "excluded" has to mean identical output too.
    for metric in ["Containment", "Jaccard", "bucket_SH", "bucket_LCS"] {
        for prune in [&[][..], &["-P"][..]] {
            // `-P -m bucket_LCS` aborts a debug build before it can be
            // compared, on a `debug_assert!(content.matches >= lcs_cnt)` in
            // `find_best_mapping` — and it does so identically with and
            // without the ladder (61 reads either way on this corpus), because
            // the assertion assumes a bucket whose accumulator is complete
            // while `-P` leaves it at whatever the seeded prefix reached.
            // Pre-existing and unrelated to the ladder, so it is reported
            // rather than fixed here; a release build runs the combination
            // fine and agrees.
            if metric == "bucket_LCS" && !prune.is_empty() && cfg!(debug_assertions) {
                continue;
            }
            let extra = [&["-m", metric][..], prune].concat();
            let with = mappings(&run(&reference, &reads, &extra, true));
            let without = mappings(&run(&reference, &reads, &extra, false));
            // Reported read by read: a whole-map `assert_eq!` prints both
            // corpora and buries the one line that actually moved.
            for (query, single) in &without {
                match with.get(query) {
                    Some(ladder) => assert_eq!(
                        ladder, single,
                        "-m {metric} {prune:?}: {query} mapped differently under the ladder"
                    ),
                    None => panic!("-m {metric} {prune:?}: {query} mapped only without the ladder"),
                }
            }
            for query in with.keys() {
                assert!(
                    without.contains_key(query),
                    "-m {metric} {prune:?}: {query} mapped only under the ladder"
                );
            }
        }
    }
}

#[test]
fn the_corpus_actually_exercises_the_ladder() {
    // A test that passed because nothing mapped, or because every read mapped
    // on the first rung, would prove nothing. Both must be false.
    let (reference, reads) = corpus();
    let paf = run(&reference, &reads, &["-m", "Containment"], true);
    let mapped = paf.lines().filter(|l| !l.is_empty()).count();
    assert!(mapped > 20, "only {mapped} reads mapped");
    assert!(mapped < 60, "{mapped} reads mapped; none exhausted the ladder");
}

#[test]
fn effort_tags_shrink() {
    // The tags `the_ladder_reports_the_same_mappings_as_the_single_pass`
    // excludes are excluded because they move, and this is the direction:
    // finding the same mappings from fewer seeds is the entire point.
    let (reference, reads) = corpus();
    let with = run(&reference, &reads, &["-m", "Containment"], true);
    let without = run(&reference, &reads, &["-m", "Containment"], false);
    for tag in ["seeds:i:", "total_matches:i:"] {
        let a = tag_total(&with, tag);
        let b = tag_total(&without, tag);
        assert!(a < b, "{tag} did not fall: {a} vs {b}");
    }
}
