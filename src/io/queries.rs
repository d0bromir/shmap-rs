use anyhow::Result;

use crate::utils::Timers;

pub fn read_queries(
    path: &str,
    threads: usize,
    timers: &mut Timers,
    callback: impl FnMut(&str, Vec<u8>, f32),
) -> Result<()> {
    #[cfg(unix)]
    if threads > 1 {
        return read_ranges(path, threads.min(8), 4 << 20, timers, callback);
    }
    let _ = threads;
    super::read_fasta(path, timers, callback)
}

#[cfg(unix)]
fn next_header(file: &std::fs::File, start: u64, length: u64) -> Result<u64> {
    use std::io::{BufRead, BufReader, Read, Seek, SeekFrom};
    if start == 0 || start >= length {
        return Ok(start.min(length));
    }
    let mut reader = BufReader::new(file);
    reader.seek(SeekFrom::Start(start - 1))?;
    let mut previous = [0u8];
    reader.read_exact(&mut previous)?;
    let mut position = start;
    let mut at_line_start = previous[0] == b'\n';
    loop {
        let buffer = reader.fill_buf()?;
        if buffer.is_empty() {
            return Ok(length);
        }
        for (offset, &byte) in buffer.iter().enumerate() {
            if at_line_start && byte == b'>' {
                return Ok(position + offset as u64);
            }
            at_line_start = byte == b'\n';
        }
        let consumed = buffer.len();
        reader.consume(consumed);
        position += consumed as u64;
    }
}

#[cfg(unix)]
fn read_ranges(
    path: &str,
    threads: usize,
    chunk_bytes: u64,
    timers: &mut Timers,
    mut callback: impl FnMut(&str, Vec<u8>, f32),
) -> Result<()> {
    use super::Semantics;
    use anyhow::Context;
    use std::fs::File;
    use std::io::{Cursor, Read};
    use std::sync::mpsc;

    let mut probe = File::open(path)?;
    let length = probe.metadata()?.len();
    let mut prefix = [0u8];
    if probe.read(&mut prefix)? != 1 || prefix[0] != b'>' {
        return super::read_fasta(path, timers, callback);
    }
    let chunks = length.div_ceil(chunk_bytes) as usize;
    let workers = threads.min(chunks).max(1);
    std::thread::scope(|scope| -> Result<()> {
        let mut receivers = Vec::new();
        for worker in 0..workers {
            let (sender, receiver) = mpsc::sync_channel(1);
            receivers.push(receiver);
            scope.spawn(move || {
                let file = match File::open(path) {
                    Ok(file) => file,
                    Err(error) => {
                        let _ = sender.send(Err(anyhow::Error::from(error)));
                        return;
                    }
                };
                for chunk in (worker..chunks).step_by(workers) {
                    let result = (|| -> Result<_> {
                        let mut local = Timers::new();
                        local.start("query_parse_parallel");
                        let start = next_header(&file, chunk as u64 * chunk_bytes, length)?;
                        let end = next_header(&file, (chunk as u64 + 1) * chunk_bytes, length)?;
                        let mut records = Vec::new();
                        if start < end {
                            let mut bytes = vec![0; (end - start) as usize];
                            file.read_exact_at(&mut bytes, start)?;
                            let mut parser = needletail::parse_fastx_reader(Cursor::new(bytes))?;
                            while let Some(record) = parser.next() {
                                let record = record?;
                                let name = record
                                    .id()
                                    .split(|&byte| byte == b' ' || byte == b'\t')
                                    .next()
                                    .unwrap_or_default();
                                let name = String::from_utf8_lossy(name).into_owned();
                                let sequence = record.seq().into_owned();
                                let progress =
                                    ((start + parser.position().byte()) as f64 / length as f64).min(1.0) as f32;
                                records.push((name, sequence, progress));
                            }
                        }
                        local.stop("query_parse_parallel");
                        Ok((records, local))
                    })();
                    let failed = result.is_err();
                    if sender.send(result).is_err() || failed {
                        break;
                    }
                }
            });
        }
        for chunk in 0..chunks {
            let (records, local) = receivers[chunk % workers]
                .recv()
                .context("query parser worker stopped")??;
            *timers += &local;
            for (name, sequence, progress) in records {
                callback(&name, sequence, progress);
            }
        }
        Ok(())
    })
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn parallel_queries_preserve_records_across_every_boundary() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("reads.fa");
        let fasta = format!(
            ">first long header\r\n{}\r\n{}\r\n>second\tcomment\r\nACGT\r\n>third\nGATTACA",
            "ACGT".repeat(70),
            "TGCA".repeat(100)
        );
        std::fs::write(&path, fasta).unwrap();
        let collect = |chunk| {
            let mut records = Vec::new();
            read_ranges(
                path.to_str().unwrap(),
                4,
                chunk,
                &mut Timers::new(),
                |name, sequence, _| records.push((name.to_owned(), sequence)),
            )
            .unwrap();
            records
        };
        let mut expected = Vec::new();
        super::super::read_fasta(path.to_str().unwrap(), &mut Timers::new(), |name, sequence, _| {
            expected.push((name.to_owned(), sequence))
        })
        .unwrap();
        for chunk in [1, 7, 17, 64, 128, 701, 4096] {
            assert_eq!(collect(chunk), expected, "chunk size {chunk}");
        }
        std::fs::write(&path, b"@fastq\nACGT\n+\n!!!!\n").unwrap();
        assert_eq!(collect(2), vec![("fastq".into(), b"ACGT".to_vec())]);
    }
}
