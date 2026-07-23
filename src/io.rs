//! FASTA/FASTA.gz reading.
//!
//! Port of the `read_fasta_klib` half of `shmap/src/io.h`, using `needletail`
//! instead of `klib`/`kseq.h`.

use anyhow::{Context, Result};
use needletail::parse_fastx_file;

use crate::utils::Timers;

/// Reads a FASTA (optionally gzip/bzip2/xz/zstd-compressed) file, invoking
/// `callback` with `(id, sequence, progress)` for each record, where
/// `progress` is in `[0, 1]`.
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
/// id/sequence copying and name-splitting). Added to answer "is reading
/// parsing-bound or I/O-bound" from `PROFILING.md` rather than guess.
pub fn read_fasta(path: &str, timers: &mut Timers, mut callback: impl FnMut(&str, &[u8], f32)) -> Result<()> {
    let total_bytes = std::fs::metadata(path)
        .with_context(|| format!("failed to stat {path}"))?
        .len()
        .max(1);

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
        let full_id = record.id().to_vec();
        let seq = record.seq().into_owned();
        drop(record); // end the borrow of `reader` before calling `.position()`

        let name_bytes = full_id
            .split(|&b| b == b' ' || b == b'\t')
            .next()
            .unwrap_or(&full_id);
        let name = String::from_utf8_lossy(name_bytes);
        let progress = (reader.position().byte() as f64 / total_bytes as f64).min(1.0) as f32;
        timers.stop("fasta_extract");

        callback(&name, &seq, progress);
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
            seen.push((id.to_string(), seq.to_vec(), progress));
        })
        .unwrap();

        assert_eq!(seen.len(), 2);
        assert_eq!(seen[0].0, "read1");
        assert_eq!(seen[0].1, b"ACGT");
        assert_eq!(seen[1].0, "read2");
        assert_eq!(seen[1].1, b"GGGG");
        assert!(seen[1].2 > 0.0 && seen[1].2 <= 1.0);
    }
}
