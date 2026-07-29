use std::fs::File;
use std::os::unix::fs::FileExt;

pub trait Semantics {
    fn read_exact_at(&self, buf: &mut [u8], off: u64) -> std::io::Result<()>;

    /// True if the file starts with a magic number this reader can't split.
    fn is_compressed(&self) -> bool;
}

#[cfg(unix)]
impl Semantics for File {
    fn read_exact_at(&self, buf: &mut [u8], mut off: u64) -> std::io::Result<()> {
        let mut done = 0;
        while done < buf.len() {
            match self.read_at(&mut buf[done..], off) {
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

    fn is_compressed(&self) -> bool {
        let mut magic = [0u8; 4];
        if self.read_at(&mut magic, 0).is_err() {
            // Too short to hold a magic number, or unreadable: let the serial
            // reader produce the real error.
            true
        } else {
            magic[..2] == [0x1f, 0x8b]                   // gzip
                    || magic[..3] == *b"BZh"             // bzip2
                    || magic == [0xfd, b'7', b'z', b'X'] // xz
                    || magic == [0x28, 0xb5, 0x2f, 0xfd] // zstd
        }
    }
}
