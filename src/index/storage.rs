use std::fs::File;
use std::io::{BufReader, BufWriter, Read, Seek, SeekFrom, Write};
use std::path::Path;
use std::time::UNIX_EPOCH;

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};

use super::{CompactIndex, Posting, SketchIndex};
use crate::sketch::{FracMinHash, RefSegment};
use crate::utils::Counters;

const MAGIC: &[u8; 8] = b"SHMAPIDX";
const SCHEMA: u32 = 1;

#[derive(Serialize, Deserialize)]
struct Metadata {
    schema: u32,
    k: i32,
    fraction: f64,
    max_matches: Option<i32>,
    reference_bytes: u64,
    reference_modified: u128,
    reference_hash: [u8; 32],
}

type Payload = (Metadata, Vec<RefSegment>, CompactIndex, Vec<(String, i64)>);

struct DigestWriter<'writer, Writer> {
    writer: &'writer mut Writer,
    hasher: blake3::Hasher,
}

impl<Writer: Write> Write for DigestWriter<'_, Writer> {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        let written = self.writer.write(bytes)?;
        self.hasher.update(&bytes[..written]);
        Ok(written)
    }

    fn flush(&mut self) -> std::io::Result<()> {
        self.writer.flush()
    }
}

fn reference_stamp(path: &Path) -> Result<(u64, u128)> {
    let metadata = path.metadata()?;
    Ok((
        metadata.len(),
        metadata.modified()?.duration_since(UNIX_EPOCH)?.as_nanos(),
    ))
}

fn reference_hash(path: &Path) -> Result<[u8; 32]> {
    let mut hasher = blake3::Hasher::new();
    hasher.update_reader(BufReader::new(File::open(path)?))?;
    Ok(*hasher.finalize().as_bytes())
}

impl SketchIndex {
    pub fn save_cached(
        &mut self,
        path: &Path,
        reference: &Path,
        sketcher: &FracMinHash,
        max_matches: Option<i32>,
        counters: &Counters,
    ) -> Result<()> {
        self.compact();
        let (reference_bytes, reference_modified) = reference_stamp(reference)?;
        let metadata = Metadata {
            schema: SCHEMA,
            k: sketcher.k,
            fraction: sketcher.h_frac,
            max_matches,
            reference_bytes,
            reference_modified,
            reference_hash: reference_hash(reference)?,
        };
        ensure!(
            reference_stamp(reference)? == (reference_bytes, reference_modified),
            "reference changed while caching"
        );
        let stats: Vec<_> = counters
            .iter_counts()
            .map(|(name, value)| (name.to_owned(), value))
            .collect();
        let nonce = std::time::SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
        let temporary = path.with_extension(format!("tmp.{}.{nonce}", std::process::id()));
        let result = (|| -> Result<()> {
            let file = File::options().write(true).create_new(true).open(&temporary)?;
            let mut writer = BufWriter::new(file);
            writer.write_all(MAGIC)?;
            writer.write_all(&[0; 32])?;
            let mut digest = DigestWriter {
                writer: &mut writer,
                hasher: blake3::Hasher::new(),
            };
            bincode::serde::encode_into_std_write(
                (&metadata, &self.segments, self.compact.as_ref().unwrap(), &stats),
                &mut digest,
                bincode::config::standard(),
            )?;
            let checksum = digest.hasher.finalize();
            writer.seek(SeekFrom::Start(8))?;
            writer.write_all(checksum.as_bytes())?;
            writer.flush()?;
            writer.get_ref().sync_all()?;
            std::fs::hard_link(&temporary, path).context("publish index cache (destination must not exist)")?;
            Ok(())
        })();
        let _ = std::fs::remove_file(&temporary);
        result
    }

    pub fn load_cached(
        path: &Path,
        reference: &Path,
        sketcher: &FracMinHash,
        max_matches: Option<i32>,
        verify_reference: bool,
        counters: &mut Counters,
    ) -> Result<Self> {
        let mut reader = BufReader::new(File::open(path)?);
        let mut magic = [0; 8];
        reader.read_exact(&mut magic)?;
        ensure!(&magic == MAGIC, "not a shmap index cache");
        let mut expected = [0; 32];
        reader.read_exact(&mut expected)?;
        let mut hasher = blake3::Hasher::new();
        hasher.update_reader(&mut reader)?;
        ensure!(
            hasher.finalize().as_bytes() == &expected,
            "index cache checksum mismatch"
        );
        reader.seek(SeekFrom::Start(40))?;
        let (metadata, segments, compact, stats): Payload = bincode::serde::decode_from_std_read(
            &mut reader,
            bincode::config::standard().with_limit::<17_179_869_184>(),
        )?;
        ensure!(reader.read(&mut [0])? == 0, "trailing index cache data");
        ensure!(metadata.schema == SCHEMA, "unsupported index schema");
        ensure!(
            metadata.k == sketcher.k && metadata.fraction == sketcher.h_frac && metadata.max_matches == max_matches,
            "index sketch parameters differ; use a new cache path"
        );
        ensure!(
            reference_stamp(reference)? == (metadata.reference_bytes, metadata.reference_modified),
            "reference metadata changed; use a new cache path"
        );
        if verify_reference {
            ensure!(
                reference_hash(reference)? == metadata.reference_hash,
                "reference checksum mismatch"
            );
        }
        for (segment_id, segment) in segments.iter().enumerate() {
            ensure!(
                segment.id as usize == segment_id && segment.sz >= 0,
                "invalid cached segment"
            );
            ensure!(
                segment.kmers.windows(2).all(|pair| pair[0].r <= pair[1].r),
                "unsorted cached sketch"
            );
            ensure!(
                segment.kmers.iter().all(|kmer| kmer.r >= 0 && kmer.r < segment.sz),
                "invalid cached k-mer position"
            );
        }
        for (&hash, entry) in &compact.entries {
            let hits = match entry {
                Posting::Single(hit) => std::slice::from_ref(hit),
                Posting::Many { start, len } => {
                    ensure!(
                        *len > 1 && *start <= compact.postings.len() && *len <= compact.postings.len() - *start,
                        "invalid cached posting range"
                    );
                    &compact.postings[*start..*start + *len]
                }
            };
            ensure!(
                hits.windows(2)
                    .all(|pair| (pair[0].segm_id, pair[0].r) <= (pair[1].segm_id, pair[1].r)),
                "unsorted cached postings"
            );
            for hit in hits {
                let segment = segments
                    .get(hit.segm_id as usize)
                    .context("invalid cached hit segment")?;
                let kmer = segment
                    .kmers
                    .get(hit.tpos as usize)
                    .context("invalid cached sketch offset")?;
                ensure!(
                    hit.r == kmer.r && hash == kmer.h && hit.strand == kmer.strand,
                    "invalid cached hit"
                );
            }
        }
        for (name, value) in stats {
            counters.inc(&name, value);
        }
        Ok(Self {
            segments,
            compact: Some(compact),
            ..Self::default()
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{Hit, Kmer};

    #[test]
    fn cache_roundtrip_rejects_corruption_and_wrong_parameters() {
        let directory = tempfile::tempdir().unwrap();
        let reference = directory.path().join("ref.fa");
        std::fs::write(&reference, b">ref\nACGTACGT\n").unwrap();
        let cache = directory.path().join("ref.idx");
        let sketcher = FracMinHash::new(4, 1.0);
        let mut index = SketchIndex::new();
        let kmer = Kmer::new(3, 8, false);
        index.segments.push(RefSegment::new(vec![kmer], "ref".into(), 8, 0));
        index.shards[0].h2single.insert(8, Hit::new(&kmer, 0, 0));
        index
            .save_cached(&cache, &reference, &sketcher, None, &Counters::new())
            .unwrap();
        let restored =
            SketchIndex::load_cached(&cache, &reference, &sketcher, None, true, &mut Counters::new()).unwrap();
        assert_eq!(restored.single_hit(8), index.single_hit(8));
        assert_eq!(restored.count(8), 1);
        assert!(
            SketchIndex::load_cached(
                &cache,
                &reference,
                &FracMinHash::new(5, 1.0),
                None,
                false,
                &mut Counters::new()
            )
            .is_err()
        );
        let mut bytes = std::fs::read(&cache).unwrap();
        *bytes.last_mut().unwrap() ^= 1;
        std::fs::write(&cache, bytes).unwrap();
        assert!(SketchIndex::load_cached(&cache, &reference, &sketcher, None, false, &mut Counters::new()).is_err());
    }
}
