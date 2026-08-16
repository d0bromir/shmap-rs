//! Putting one optimization back the way it was, at run time.
//!
//! `SHMAP_ABLATE=bucket-array,sketch-loop` restores the pre-optimization code
//! path for each switch named, and leaves everything else alone. One binary,
//! one machine, one input, one compiler — so the difference between two runs
//! is the switch and nothing else. That is what makes a cumulative ladder
//! (`benchmarks/scripts/ablation.py`) a measurement of the optimizations
//! rather than of the builds.
//!
//! **Every switch is output-preserving in both positions.** These are the
//! changes `PORT_CHANGES.md` records as exact, so ablating one may cost time
//! or memory but must never change a mapping. The harness checks that by
//! comparing the PAF of every rung byte for byte; a switch that fails it is a
//! bug in the switch or in the optimization, and either way the ladder is
//! wrong until it is fixed.
//!
//! Not every optimization can be a switch. Row 8 (`PMatches` inline, `Match`
//! borrowing its `Seed`, `lto = "fat"`) is type- and build-level: reversing it
//! is a different binary, not a different branch, so it is absent here and
//! reported as not ablated rather than estimated.
//!
//! Read once per process and hoisted out of every hot loop by the code that
//! uses it — see the `ablate_*` fields on [`crate::buckets::Buckets`] and
//! [`crate::sketch::FracMinHash`] — so a run with no switches set pays
//! nothing.

use std::sync::OnceLock;

/// One optimization that can be put back.
///
/// The numbers are the rows of `PORT_CHANGES.md`'s current-state table, which
/// is what the paper's Table 1 is generated from; they are not dense (row 4 is
/// the thread count, a CLI flag rather than a switch, and row 8 is not
/// ablatable at all) and that is deliberate.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Opt {
    /// Row 1: size the dense bucket array by the reference, as `buckets.h`
    /// does, instead of by the read's own half-length.
    BucketArray,
    /// Row 2: accumulate a multi-hit seed in a per-seed scratch hash map, as
    /// `shmap.h` does, instead of streaming its already-sorted hits.
    StreamSeeds,
    /// Row 3: recompute the second-best search instead of replaying the
    /// first's scores.
    RefineMemo,
    /// Row 5: build the index without chunked sketching or a parallel shard
    /// fill, on one thread.
    ParallelIndex,
    /// Row 6: read the reference with the serial single-pass FASTA reader.
    ParallelFasta,
    /// Row 7: sketch with the pre-optimization rolling loop — rotations
    /// recomputed per base, indexed rather than iterated, output buffer sized
    /// by a flat factor.
    SketchLoop,
    /// Row 9: order the final buckets with a stable sort over the whole
    /// records instead of an unstable sort over packed keys.
    PackedSort,
}

/// `(switch name, optimization, PORT_CHANGES.md row)`.
const SWITCHES: [(&str, Opt, u8); 7] = [
    ("bucket-array", Opt::BucketArray, 1),
    ("stream-seeds", Opt::StreamSeeds, 2),
    ("refine-memo", Opt::RefineMemo, 3),
    ("parallel-index", Opt::ParallelIndex, 5),
    ("parallel-fasta", Opt::ParallelFasta, 6),
    ("sketch-loop", Opt::SketchLoop, 7),
    ("packed-sort", Opt::PackedSort, 9),
];

pub const ENV: &str = "SHMAP_ABLATE";

fn bit(opt: Opt) -> u32 {
    1 << (opt as u32)
}

/// The set named by `SHMAP_ABLATE`, parsed once.
///
/// An unrecognised name exits rather than being ignored: a ladder whose rung
/// silently ablated nothing would be a measurement of the same build twice,
/// reported as a step.
fn mask() -> u32 {
    static MASK: OnceLock<u32> = OnceLock::new();
    *MASK.get_or_init(|| {
        let Some(raw) = std::env::var_os(ENV) else { return 0 };
        let raw = raw.to_string_lossy().to_string();
        let mut m = 0u32;
        for name in raw.split(',').map(str::trim).filter(|s| !s.is_empty()) {
            if name == "all" {
                return SWITCHES.iter().fold(0, |acc, (_, o, _)| acc | bit(*o));
            }
            match SWITCHES.iter().find(|(n, _, _)| *n == name) {
                Some((_, o, _)) => m |= bit(*o),
                None => {
                    let known: Vec<&str> = SWITCHES.iter().map(|(n, _, _)| *n).collect();
                    eprintln!(
                        "ERROR: {ENV}: unknown switch {name:?}; known: {}, all",
                        known.join(", ")
                    );
                    std::process::exit(2);
                }
            }
        }
        m
    })
}

/// Whether `opt` is switched off for this run.
pub fn off(opt: Opt) -> bool {
    mask() & bit(opt) != 0
}

/// `"row 1 bucket-array, row 7 sketch-loop"`, or `None` when nothing is
/// ablated — printed once so a run's own log says what it measured.
pub fn banner() -> Option<String> {
    let m = mask();
    if m == 0 {
        return None;
    }
    Some(
        SWITCHES
            .iter()
            .filter(|(_, o, _)| m & bit(*o) != 0)
            .map(|(n, _, row)| format!("row {row} {n}"))
            .collect::<Vec<_>>()
            .join(", "),
    )
}
