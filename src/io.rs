//! FASTA/FASTA.gz reading.
//!
//! Port of the `read_fasta_klib` half of `shmap/src/io.h`, using `needletail`
//! instead of `klib`/`kseq.h`.

use anyhow::{Context, Result};
use needletail::parse_fastx_file;

use crate::utils::Timers;

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

/// One byte range's contribution, in file order.
struct RangeOut {
    idx: usize,
    /// `(offset in `seq`, name)` for each header line found here: at that
    /// offset a new segment begins. Bases before the first entry belong to
    /// whichever segment was already open.
    headers: Vec<(usize, String)>,
    /// This range's sequence bases, newlines removed, concatenated.
    seq: Vec<u8>,
    /// Absolute end offset, for the progress fraction.
    end_byte: u64,
}

/// True if the file starts with a magic number this reader can't split.
fn is_compressed(file: &std::fs::File) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::FileExt;
        let mut magic = [0u8; 4];
        if file.read_at(&mut magic, 0).is_err() {
            // Too short to hold a magic number, or unreadable: let the serial
            // reader produce the real error.
            true
        } else {
            magic[..2] == [0x1f, 0x8b]                        // gzip
                || magic[..3] == *b"BZh"                      // bzip2
                || magic == [0xfd, b'7', b'z', b'X']          // xz
                || magic == [0x28, 0xb5, 0x2f, 0xfd] // zstd
        }
    }
    #[cfg(not(unix))]
    {
        let _ = file;
        true
    }
}

#[cfg(unix)]
fn read_exact_at(file: &std::fs::File, buf: &mut [u8], mut off: u64) -> std::io::Result<()> {
    use std::os::unix::fs::FileExt;
    let mut done = 0;
    while done < buf.len() {
        match file.read_at(&mut buf[done..], off) {
            Ok(0) => return Err(std::io::ErrorKind::UnexpectedEof.into()),
            Ok(n) => {
                done += n;
                off += n as u64;
            }
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
            Err(e) => return Err(e),
        }
    }
    Ok(())
}

/// Parses the lines that *start* inside `[start, end)`.
///
/// A line belongs to the range containing its first byte, which is what makes
/// the split unambiguous: a range skips the partial line it opens in, and
/// reads past its own end to finish the last line it owns.
#[cfg(unix)]
fn parse_range(file: &std::fs::File, idx: usize, start: u64, end: u64, file_len: u64) -> Result<RangeOut> {
    // One byte of lookbehind distinguishes "a line starts exactly at `start`"
    // from "`start` is mid-line".
    let from = start.saturating_sub(1);
    let mut len = (end + LINE_OVERSHOOT).min(file_len) - from;
    let mut buf = vec![0u8; len as usize];
    read_exact_at(file, &mut buf, from)?;

    // Grow until the last line owned by this range is terminated (or EOF).
    let tail_from = (end - from) as usize;
    while from + len < file_len && memchr::memchr(b'\n', &buf[tail_from.min(buf.len())..]).is_none() {
        let grown = (len + LINE_OVERSHOOT).min(file_len - from);
        if grown == len {
            break;
        }
        let old = len as usize;
        buf.resize(grown as usize, 0);
        read_exact_at(file, &mut buf[old..], from + old as u64)?;
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

    let mut out = RangeOut {
        idx,
        headers: Vec::new(),
        seq: Vec::new(),
        end_byte: end.min(file_len),
    };
    while pos < buf.len() && (from + pos as u64) < end {
        let rel_end = memchr::memchr(b'\n', &buf[pos..]).map_or(buf.len(), |i| pos + i);
        let mut line = &buf[pos..rel_end];
        if line.last() == Some(&b'\r') {
            line = &line[..line.len() - 1];
        }
        if line.first() == Some(&b'>') {
            let name_bytes = line[1..]
                .split(|&b| b == b' ' || b == b'\t')
                .next()
                .unwrap_or(&line[1..]);
            out.headers.push((out.seq.len(), String::from_utf8_lossy(name_bytes).into_owned()));
        } else {
            out.seq.extend_from_slice(line);
        }
        pos = rel_end + 1;
    }
    Ok(out)
}

/// Same contract as [`read_fasta`], but splits an uncompressed file into byte
/// ranges parsed in parallel.
///
/// Reading was the last serial phase of indexing: ~4.4 s flat from 1 thread to
/// 64, ~70-80% of all indexing time once the index build itself was sharded.
/// It is not I/O bound — the 3.18 GB human reference streams in 0.87 s at
/// 3.7 GB/s — so the cost is line splitting, newline stripping and copying,
/// and that parallelises.
///
/// Records are still delivered **in file order**, which the index build
/// depends on for `segm_id` assignment and the `max_matches` cap. Workers pull
/// ranges in increasing order and at most one each, so the reorder buffer
/// holds at most `n_readers` entries.
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
        if n_threads <= 1 || file_len < min_parallel_bytes || is_compressed(&file) {
            return read_fasta(path, timers, callback);
        }

        let n_readers = n_threads.min(MAX_READERS);
        let n_ranges = file_len.div_ceil(range_bytes) as usize;
        let next_range = std::sync::atomic::AtomicUsize::new(0);
        let (tx, rx) = std::sync::mpsc::sync_channel::<Result<RangeOut>>(n_readers * 2);

        let total = file_len.max(1);
        let mut err: Option<anyhow::Error> = None;
        std::thread::scope(|scope| {
            for _ in 0..n_readers {
                let tx = tx.clone();
                let file = &file;
                let next_range = &next_range;
                scope.spawn(move || {
                    loop {
                        let idx = next_range.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                        if idx >= n_ranges {
                            break;
                        }
                        let start = idx as u64 * range_bytes;
                        let end = ((idx as u64 + 1) * range_bytes).min(file_len);
                        if tx.send(parse_range(file, idx, start, end, file_len)).is_err() {
                            break;
                        }
                    }
                });
            }
            drop(tx);

            // Reassemble in file order. A segment stays open across ranges
            // until some range reports the header that ends it.
            let mut pending: std::collections::HashMap<usize, RangeOut> = std::collections::HashMap::new();
            let mut next_idx = 0usize;
            let mut open: Option<String> = None;
            let mut seq: Vec<u8> = Vec::new();
            for got in rx {
                let got = match got {
                    Ok(g) => g,
                    Err(e) => {
                        err = Some(e);
                        break;
                    }
                };
                pending.insert(got.idx, got);
                while let Some(r) = pending.remove(&next_idx) {
                    let progress = (r.end_byte as f64 / total as f64).min(1.0) as f32;
                    let mut prev = 0usize;
                    for (off, name) in r.headers {
                        seq.extend_from_slice(&r.seq[prev..off]);
                        if let Some(open_name) = open.take() {
                            callback(&open_name, std::mem::take(&mut seq), progress);
                        }
                        seq.clear();
                        open = Some(name);
                        prev = off;
                    }
                    seq.extend_from_slice(&r.seq[prev..]);
                    next_idx += 1;
                }
            }
            if err.is_none()
                && let Some(open_name) = open.take()
            {
                callback(&open_name, seq, 1.0);
            }
        });
        if let Some(e) = err {
            return Err(e);
        }
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
/// parsing-bound or I/O-bound" from `PROFILING.md` rather than guess.
pub fn read_fasta(path: &str, timers: &mut Timers, mut callback: impl FnMut(&str, Vec<u8>, f32)) -> Result<()> {
    let total_bytes = std::fs::metadata(path)
        .with_context(|| format!("failed to stat {path}"))?
        .len()
        .max(1);

    // Reused across records so the per-record name costs no allocation after
    // the first few; `String::from_utf8_lossy` itself only allocates when the
    // header is not valid UTF-8.
    let mut name = String::new();

    let mut reader = parse_fastx_file(path).with_context(|| format!("failed to open {path}"))?;
    loop {
        timers.start("fasta_parse_next");
        let next = reader.next();
        timers.stop("fasta_parse_next");
        let Some(record) = next else {
            break;
        };

        timers.start("fasta_extract");
        let record = record.with_context(|| format!("invalid FASTA record in {path}"))?;
        let full_id = record.id();
        let name_bytes = full_id
            .split(|&b| b == b' ' || b == b'\t')
            .next()
            .unwrap_or(full_id);
        name.clear();
        name.push_str(&String::from_utf8_lossy(name_bytes));
        // `into_owned` on a multi-line FASTA record is free: needletail has
        // already had to build an owned, newline-stripped copy to return the
        // `Cow`. Handing that buffer to the callback by value rather than by
        // reference is what lets the caller store it without copying it a
        // second time — for a chromosome-sized segment that second copy was
        // both a memcpy of hundreds of MB and hundreds of MB of extra peak RSS.
        let seq = record.seq().into_owned();
        drop(record); // end the borrow of `reader` before calling `.position()`

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
            state = state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
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
        for range in [1u64, 2, 3, 7, 16, 31, 64, 127, 256, 1000, 4096, file_len - 1, file_len, file_len + 1] {
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
            read_fasta_ranged(f.path().to_str().unwrap(), 4, range, 0, &mut Timers::new(), |id, seq, _| {
                seen.push((id.to_string(), seq));
            })
            .unwrap();
            assert_eq!(
                seen,
                vec![("a".to_string(), b"ACGTGGTT".to_vec()), ("b".to_string(), b"TTTT".to_vec())],
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
