//! FASTA/FASTA.gz reading.
//!
//! Port of the `read_fasta_klib` half of `shmap/src/io.h`, using `needletail`
//! instead of `klib`/`kseq.h`.

use std::path::Path;

use anyhow::{Context, Result};
use needletail::parse_fastx_file;

use crate::utils::Timers;

pub mod semantics;

use semantics::Semantics;

/// Byte range handed to one reader worker.
const RANGE_BYTES: u64 = 16 << 20;
/// How far past its range a worker reads to finish the last line that *starts*
/// inside it. Grown on demand, so this is only a "no header line is longer
/// than this in practice" hint, not a limit.
const LINE_OVERSHOOT: u64 = 1 << 16;
/// Reading only has ~3.5 s of CPU to spread, so a handful of workers covers
/// it; every extra one costs another pair of range-sized buffers, and the
/// remaining cores are wanted for sketching anyway.
const MAX_READERS: usize = 8;
/// Below this, the split isn't worth the thread setup.
const MIN_PARALLEL_BYTES: u64 = 32 << 20;

/// One item of a byte range, in file order.
#[cfg(unix)]
enum RangeItem<'a> {
    /// Header line, `>` already stripped.
    Header(&'a [u8]),
    /// One sequence line, newline (and any `\r`) already stripped.
    Bases(&'a [u8]),
}

/// What one byte range contributes, as counts only.
///
/// `run_bases[i]` is how many bases sit between header `i - 1` and header `i`
/// of this range, so `run_bases.len() == headers.len() + 1`: run 0 belongs to
/// whichever segment was already open when the range started.
#[cfg(unix)]
struct ScanOut {
    idx: usize,
    headers: Vec<String>,
    run_bases: Vec<u64>,
}

/// Where one range's run of bases lands inside a segment's buffer.
#[cfg(unix)]
struct Part {
    range_idx: usize,
    run_idx: usize,
    offset: u64,
    len: u64,
}

/// A segment's exact size and the disjoint pieces that make it up.
#[cfg(unix)]
struct SegPlan {
    name: String,
    len: u64,
    parts: Vec<Part>,
    end_byte: u64,
}

/// Walks the lines that *start* inside `[start, end)`, in order.
///
/// A line belongs to the range containing its first byte, which is what makes
/// the split unambiguous: a range skips the partial line it opens in, and
/// reads past its own end to finish the last line it owns.
///
/// Both passes go through here, so the counting pass and the filling pass can
/// never disagree about where a line begins or which run it belongs to.
#[cfg(unix)]
fn walk_range(
    file: &std::fs::File,
    start: u64,
    end: u64,
    file_len: u64,
    mut on: impl FnMut(RangeItem<'_>),
) -> Result<()> {
    // One byte of lookbehind distinguishes "a line starts exactly at `start`"
    // from "`start` is mid-line".
    let from = start.saturating_sub(1);
    let mut len = (end + LINE_OVERSHOOT).min(file_len) - from;
    let mut buf = vec![0u8; len as usize];
    file.read_exact_at(&mut buf, from)?;

    // Grow until the last line owned by this range is terminated (or EOF).
    let tail_from = (end - from) as usize;
    while from + len < file_len && memchr::memchr(b'\n', &buf[tail_from.min(buf.len())..]).is_none() {
        let grown = (len + LINE_OVERSHOOT).min(file_len - from);
        if grown == len {
            break;
        }
        let old = len as usize;
        buf.resize(grown as usize, 0);
        file.read_exact_at(&mut buf[old..], from + old as u64)?;
        len = grown;
    }

    // Skip into the first line this range owns.
    let mut pos = if start == 0 {
        0
    } else if buf[0] == b'\n' {
        1
    } else {
        match memchr::memchr(b'\n', &buf) {
            Some(i) => i + 1,
            None => buf.len(), // no line starts here at all
        }
    };

    while pos < buf.len() && (from + pos as u64) < end {
        let rel_end = memchr::memchr(b'\n', &buf[pos..]).map_or(buf.len(), |i| pos + i);
        let mut line = &buf[pos..rel_end];
        if line.last() == Some(&b'\r') {
            line = &line[..line.len() - 1];
        }
        if line.first() == Some(&b'>') {
            on(RangeItem::Header(&line[1..]));
        } else {
            on(RangeItem::Bases(line));
        }
        pos = rel_end + 1;
    }
    Ok(())
}

/// Pass 1: counts only, no sequence bytes are kept.
#[cfg(unix)]
fn scan_range(file: &std::fs::File, idx: usize, start: u64, end: u64, file_len: u64) -> Result<ScanOut> {
    let mut headers = Vec::new();
    let mut run_bases = vec![0u64];
    walk_range(file, start, end, file_len, |item| match item {
        RangeItem::Header(h) => {
            let name = h.split(|&b| b == b' ' || b == b'\t').next().unwrap_or(h);
            headers.push(String::from_utf8_lossy(name).into_owned());
            run_bases.push(0);
        }
        RangeItem::Bases(b) => {
            *run_bases.last_mut().expect("run_bases is never empty") += b.len() as u64;
        }
    })?;
    Ok(ScanOut {
        idx,
        headers,
        run_bases,
    })
}

/// Pass 2: copies one run's bases straight into its slice of the segment
/// buffer. `dest` is exactly the length pass 1 counted.
#[cfg(unix)]
fn fill_run(file: &std::fs::File, start: u64, end: u64, file_len: u64, run_idx: usize, dest: &mut [u8]) -> Result<()> {
    let mut cur = 0usize;
    let mut at = 0usize;
    walk_range(file, start, end, file_len, |item| match item {
        RangeItem::Header(_) => cur += 1,
        RangeItem::Bases(b) => {
            if cur == run_idx {
                dest[at..at + b.len()].copy_from_slice(b);
                at += b.len();
            }
        }
    })?;
    debug_assert_eq!(at, dest.len(), "pass 2 wrote a different length than pass 1 counted");
    Ok(())
}

/// Same contract as [`read_fasta`], but splits an uncompressed file into byte
/// ranges parsed in parallel, in two passes.
///
/// Reading was the last serial phase of indexing. It is not I/O bound — the
/// 3.18 GB human reference streams in 0.87 s at 3.7 GB/s — so the cost is line
/// splitting, newline stripping and copying, and that parallelises.
///
/// The two passes exist to keep the *copy* parallel too. A single pass has to
/// concatenate every range into a growing per-segment buffer on one thread,
/// and measured on the whole genome that concatenation was 2.8-3.2 s of a
/// 2.9-3.2 s reader — not the memcpy itself but the doubling reallocations and
/// ~780 k first-touch page faults, all serialised. So pass 1 only counts, which
/// gives every segment's exact size and every range's exact offset within it;
/// pass 2 then lets workers write straight into disjoint slices of a
/// right-sized buffer, with no reallocation and the page faults spread across
/// threads.
///
/// Records are still delivered **in file order**, which the index build depends
/// on for `segm_id` assignment and the `max_matches` cap, and only one
/// segment's buffer is live at a time, so peak memory is unchanged.
///
/// Falls back to [`read_fasta`] for compressed input (byte offsets are
/// meaningless there), for small files, for `n_threads <= 1`, and on non-Unix
/// targets, so behaviour is unchanged wherever the split doesn't apply.
pub fn read_fasta_parallel(
    path: &str,
    n_threads: usize,
    timers: &mut Timers,
    callback: impl FnMut(&str, Vec<u8>, f32),
) -> Result<()> {
    read_fasta_ranged(path, n_threads, RANGE_BYTES, MIN_PARALLEL_BYTES, timers, callback)
}

/// [`read_fasta_parallel`] with the split geometry injected, so tests can use
/// ranges small enough to land boundaries inside headers and sequence lines.
fn read_fasta_ranged(
    path: &str,
    n_threads: usize,
    range_bytes: u64,
    min_parallel_bytes: u64,
    timers: &mut Timers,
    mut callback: impl FnMut(&str, Vec<u8>, f32),
) -> Result<()> {
    #[cfg(not(unix))]
    {
        let _ = (n_threads, range_bytes, min_parallel_bytes);
        return read_fasta(path, timers, callback);
    }
    #[cfg(unix)]
    {
        let file = std::fs::File::open(path).with_context(|| format!("failed to open {path}"))?;
        let file_len = file.metadata().with_context(|| format!("failed to stat {path}"))?.len();
        if n_threads <= 1 || file_len < min_parallel_bytes || file.is_compressed() {
            return read_fasta(path, timers, callback);
        }

        let n_readers = n_threads.min(MAX_READERS);
        let n_ranges = file_len.div_ceil(range_bytes) as usize;
        let bounds = |idx: usize| {
            let start = idx as u64 * range_bytes;
            (start, ((idx as u64 + 1) * range_bytes).min(file_len))
        };

        // ---- pass 1: count, in parallel ----
        timers.start("fasta_scan");
        let next = std::sync::atomic::AtomicUsize::new(0);
        let scans: std::sync::Mutex<Vec<ScanOut>> = std::sync::Mutex::new(Vec::with_capacity(n_ranges));
        let failure: std::sync::Mutex<Option<anyhow::Error>> = std::sync::Mutex::new(None);
        std::thread::scope(|scope| {
            for _ in 0..n_readers {
                scope.spawn(|| {
                    loop {
                        let idx = next.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                        if idx >= n_ranges || failure.lock().unwrap().is_some() {
                            break;
                        }
                        let (start, end) = bounds(idx);
                        match scan_range(&file, idx, start, end, file_len) {
                            Ok(s) => scans.lock().unwrap().push(s),
                            Err(e) => {
                                *failure.lock().unwrap() = Some(e);
                                break;
                            }
                        }
                    }
                });
            }
        });
        if let Some(e) = failure.lock().unwrap().take() {
            return Err(e);
        }
        let mut scans = scans.into_inner().unwrap();
        scans.sort_by_key(|s| s.idx);
        timers.stop("fasta_scan");

        // ---- serial: plan every segment's size and its pieces ----
        // O(ranges), not O(bases): a few hundred iterations on a human genome.
        let mut segs: Vec<SegPlan> = Vec::new();
        for s in &scans {
            let (_, end) = bounds(s.idx);
            for (run_idx, &n) in s.run_bases.iter().enumerate() {
                if run_idx > 0 {
                    segs.push(SegPlan {
                        name: s.headers[run_idx - 1].clone(),
                        len: 0,
                        parts: Vec::new(),
                        end_byte: end,
                    });
                }
                // Bases before the very first header have no segment to belong
                // to; a well-formed FASTA has none.
                if n > 0
                    && let Some(seg) = segs.last_mut()
                {
                    seg.parts.push(Part {
                        range_idx: s.idx,
                        run_idx,
                        offset: seg.len,
                        len: n,
                    });
                    seg.len += n;
                }
                if let Some(seg) = segs.last_mut() {
                    seg.end_byte = end;
                }
            }
        }

        // ---- pass 2: fill each segment in parallel, one at a time ----
        timers.start("fasta_fill");
        let total = file_len.max(1);
        for seg in segs {
            // Lazily-zeroed and exactly sized: no reallocation, and the pages
            // are faulted in by whichever worker first writes them.
            let mut buf = vec![0u8; seg.len as usize];

            // Carve `buf` into the disjoint pieces the parts describe. Parts
            // are built in increasing offset order, so this walks forward once.
            let mut rest = &mut buf[..];
            let mut jobs: Vec<(usize, usize, &mut [u8])> = Vec::with_capacity(seg.parts.len());
            let mut at = 0u64;
            for part in &seg.parts {
                // The carve below assumes the parts tile the buffer with no
                // gaps, which is how the planner builds them; check it rather
                // than trust it, since a gap would silently leave zero bases
                // in the middle of a chromosome.
                debug_assert_eq!(part.offset, at, "segment parts are not contiguous");
                at += part.len;
                let (piece, tail) = rest.split_at_mut(part.len as usize);
                jobs.push((part.range_idx, part.run_idx, piece));
                rest = tail;
            }
            debug_assert!(rest.is_empty(), "segment parts did not cover the whole buffer");

            let per = jobs.len().div_ceil(n_readers).max(1);
            let failure: std::sync::Mutex<Option<anyhow::Error>> = std::sync::Mutex::new(None);
            std::thread::scope(|scope| {
                for chunk in jobs.chunks_mut(per) {
                    let file = &file;
                    let failure = &failure;
                    scope.spawn(move || {
                        for (range_idx, run_idx, dest) in chunk {
                            let (start, end) = bounds(*range_idx);
                            if let Err(e) = fill_run(file, start, end, file_len, *run_idx, dest) {
                                *failure.lock().unwrap() = Some(e);
                                return;
                            }
                        }
                    });
                }
            });
            if let Some(e) = failure.into_inner().unwrap() {
                return Err(e);
            }

            let progress = (seg.end_byte as f64 / total as f64).min(1.0) as f32;
            callback(&seg.name, buf, progress);
        }
        timers.stop("fasta_fill");
        Ok(())
    }
}

/// Reads a FASTA (optionally gzip/bzip2/xz/zstd-compressed) file, invoking
/// `callback` with `(id, sequence, progress)` for each record, where
/// `progress` is in `[0, 1]`.
///
/// The sequence is passed **by value**: every caller here needs to own it
/// (both pipelines send it down a channel to a worker thread), and the
/// buffer needletail hands back is already a fresh allocation per record,
/// so moving it through costs nothing and saves the caller a full copy.
///
/// `id` is truncated at the first whitespace, matching `klib`'s
/// name/comment split (needletail's own `record.id()` returns the whole
/// header line instead).
///
/// The progress fraction is approximated from needletail's *decompressed*
/// stream position divided by the file's on-disk size. For uncompressed
/// FASTA this is exact (same as the C++'s `gztell`-based version); for
/// compressed input it's only a rough proxy, since decompressed volume can
/// vastly exceed the compressed file size — acceptable since this feeds a
/// cosmetic progress bar only and has no effect on mapping correctness.
///
/// `timers` gets two sub-stage entries nested inside whatever bracket the
/// caller already times this whole call under (`index_reading`/
/// `query_reading`): `fasta_parse_next` (needletail's own record parsing —
/// I/O plus line/sequence assembly) and `fasta_extract` (this function's own
/// name-splitting and sequence hand-off). Added to answer "is reading
/// parsing-bound or I/O-bound" from `PORT_CHANGES.md` §6 rather than guess.
pub fn read_fasta<P>(path: P, timers: &mut Timers, mut callback: impl FnMut(&str, Vec<u8>, f32)) -> Result<()>
where
    P: AsRef<Path> + std::fmt::Debug,
{
    let total_bytes = std::fs::metadata(&path)
        .with_context(|| format!("failed to stat {path:?}"))?
        .len()
        .max(1);

    // Reused across records so the per-record name costs no allocation after
    // the first few; `String::from_utf8_lossy` itself only allocates when the
    // header is not valid UTF-8.
    let mut name = String::new();

    let mut reader = parse_fastx_file(&path).with_context(|| format!("failed to open {path:?}"))?;

    while let Some(record) = {
        timers.start("fasta_parse_next");
        let next = reader.next();
        timers.stop("fasta_parse_next");
        next
    } {
        timers.start("fasta_extract");
        let record = record.with_context(|| format!("invalid FASTA record in {path:?}"))?;
        let full_id = record.id();
        let name_bytes = full_id.split(|&b| b == b' ' || b == b'\t').next().unwrap_or(full_id);
        name.clear();
        name.push_str(&String::from_utf8_lossy(name_bytes));
        // `into_owned` on a multi-line FASTA record is free: needletail has
        // already had to build an owned, newline-stripped copy to return the
        // `Cow`. Handing that buffer to the callback by value rather than by
        // reference is what lets the caller store it without copying it a
        // second time — for a chromosome-sized segment that second copy was
        // both a memcpy of hundreds of MB and hundreds of MB of extra peak RSS.
        let seq = record.seq().into_owned();
        // `seq` is owned, so the borrow of `reader` that `record` holds ends
        // here on its own and `.position()` below is free to take `&reader`.

        let progress = (reader.position().byte() as f64 / total_bytes as f64).min(1.0) as f32;
        timers.stop("fasta_extract");

        callback(&name, seq, progress);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn reads_records_and_truncates_id_at_whitespace() {
        let mut f = tempfile::Builder::new().suffix(".fa").tempfile().unwrap();
        writeln!(f, ">read1 some description\nACGT\n>read2\nGGGG").unwrap();
        f.flush().unwrap();

        let mut seen = Vec::new();
        read_fasta(f.path().to_str().unwrap(), &mut Timers::new(), |id, seq, progress| {
            seen.push((id.to_string(), seq, progress));
        })
        .unwrap();

        assert_eq!(seen.len(), 2);
        assert_eq!(seen[0].0, "read1");
        assert_eq!(seen[0].1, b"ACGT");
        assert_eq!(seen[1].0, "read2");
        assert_eq!(seen[1].1, b"GGGG");
        assert!(seen[1].2 > 0.0 && seen[1].2 <= 1.0);
    }

    /// Builds a FASTA whose line widths, segment lengths and header lengths are
    /// all mutually coprime-ish, so that sweeping the range size lands split
    /// points inside headers, inside sequence lines, and exactly on newlines.
    fn synthetic_fasta(n_segments: usize, seed: u64) -> (tempfile::NamedTempFile, Vec<(String, Vec<u8>)>) {
        let mut f = tempfile::Builder::new().suffix(".fa").tempfile().unwrap();
        let mut expect = Vec::new();
        let mut state = seed;
        let mut rnd = || {
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            (state >> 33) as usize
        };
        for s in 0..n_segments {
            let name = format!("chr{s}_{}", "x".repeat(rnd() % 40));
            let len = 1 + rnd() % 900;
            let width = 1 + rnd() % 97;
            let seq: Vec<u8> = (0..len).map(|i| b"ACGT"[(i * 7 + s) % 4]).collect();
            writeln!(f, ">{name} some description here").unwrap();
            for line in seq.chunks(width) {
                f.write_all(line).unwrap();
                f.write_all(b"\n").unwrap();
            }
            expect.push((name, seq));
        }
        f.flush().unwrap();
        (f, expect)
    }

    #[test]
    fn parallel_ranges_match_the_serial_reader_at_every_split_size() {
        let (f, expect) = synthetic_fasta(24, 0xC0FFEE);
        let path = f.path().to_str().unwrap();
        let file_len = std::fs::metadata(f.path()).unwrap().len();

        // Range sizes from "smaller than one line" up to "whole file", which
        // between them put boundaries in every structurally distinct place.
        for range in [
            1u64,
            2,
            3,
            7,
            16,
            31,
            64,
            127,
            256,
            1000,
            4096,
            file_len - 1,
            file_len,
            file_len + 1,
        ] {
            for threads in [2usize, 4, 8] {
                let mut seen = Vec::new();
                read_fasta_ranged(path, threads, range, 0, &mut Timers::new(), |id, seq, _| {
                    seen.push((id.to_string(), seq));
                })
                .unwrap_or_else(|e| panic!("range={range} threads={threads}: {e}"));
                assert_eq!(
                    seen, expect,
                    "parallel read diverged from the source at range={range} threads={threads}"
                );
            }
        }
    }

    #[test]
    fn parallel_reader_handles_crlf_and_a_missing_final_newline() {
        let mut f = tempfile::Builder::new().suffix(".fa").tempfile().unwrap();
        // No trailing newline on the last line, CRLF throughout.
        write!(f, ">a desc\r\nACGT\r\nGGTT\r\n>b\r\nTTTT").unwrap();
        f.flush().unwrap();
        for range in [1u64, 3, 8, 17, 64] {
            let mut seen = Vec::new();
            read_fasta_ranged(
                f.path().to_str().unwrap(),
                4,
                range,
                0,
                &mut Timers::new(),
                |id, seq, _| {
                    seen.push((id.to_string(), seq));
                },
            )
            .unwrap();
            assert_eq!(
                seen,
                vec![
                    ("a".to_string(), b"ACGTGGTT".to_vec()),
                    ("b".to_string(), b"TTTT".to_vec())
                ],
                "range={range}"
            );
        }
    }

    #[test]
    fn parallel_reader_falls_back_and_still_agrees_with_the_serial_path() {
        let (f, expect) = synthetic_fasta(6, 42);
        let path = f.path().to_str().unwrap();
        // n_threads == 1 and the size floor both route to `read_fasta`.
        for (threads, min_bytes) in [(1usize, 0u64), (8, u64::MAX)] {
            let mut seen = Vec::new();
            read_fasta_ranged(path, threads, 64, min_bytes, &mut Timers::new(), |id, seq, _| {
                seen.push((id.to_string(), seq));
            })
            .unwrap();
            assert_eq!(seen, expect, "threads={threads} min_bytes={min_bytes}");
        }
    }
}
