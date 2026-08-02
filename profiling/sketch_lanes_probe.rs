//! Throughput of the rolling hash at L independent lanes, before committing to
//! a rewrite. Scratch probe for Q1 — not part of the suite.
//!
//! The rolling update is a latency chain: each window's hash depends on the
//! previous one, so the loop runs at the latency of rotate->xor->xor rather
//! than at the throughput the ports could sustain. Splitting the sequence into
//! L blocks gives L independent chains to interleave, which should convert the
//! loop from latency-bound to throughput-bound without any intrinsics.
//!
//! Measures hashing only: no emission, no buffers. That is the upper bound on
//! what the real change could recover.

const A: u64 = 0x3c8b_fbb3_95c6_0474;
const C: u64 = 0x3193_c185_62a0_2b4c;
const G: u64 = 0x2032_3ed0_8257_2324;
const TN: u64 = 0x2955_49f5_4be2_4456;

fn luts(k: u32) -> ([u64; 256], [u64; 256], [u64; 256], [u64; 256], [u64; 256]) {
    let mut fw = [0u64; 256];
    let mut rc = [0u64; 256];
    for &(l, u, v) in &[(b'a', b'A', A), (b'c', b'C', C), (b'g', b'G', G), (b't', b'T', TN)] {
        fw[l as usize] = v;
        fw[u as usize] = v;
    }
    for &(l, u, comp) in &[
        (b'a', b'A', b'T'),
        (b'c', b'C', b'G'),
        (b'g', b'G', b'C'),
        (b't', b'T', b'A'),
    ] {
        rc[l as usize] = fw[comp as usize];
        rc[u as usize] = fw[comp as usize];
    }
    let mut fw_k = [0u64; 256];
    let mut rc_r1 = [0u64; 256];
    let mut rc_k1 = [0u64; 256];
    for c in 0..256 {
        fw_k[c] = fw[c].rotate_left(k);
        rc_r1[c] = rc[c].rotate_right(1);
        rc_k1[c] = rc[c].rotate_left(k - 1);
    }
    (fw, rc, fw_k, rc_r1, rc_k1)
}

fn init_window(s: &[u8], at: usize, k: usize, fw: &[u64; 256], rc: &[u64; 256]) -> (u64, u64) {
    let mut h_fw = 0u64;
    let mut h_rc = 0u64;
    for (i, &c) in s[at..at + k].iter().enumerate() {
        h_fw ^= fw[c as usize].rotate_left((k - i - 1) as u32);
        h_rc ^= rc[c as usize].rotate_left(i as u32);
    }
    (h_fw, h_rc)
}

/// L independent lanes over contiguous blocks. Returns a checksum so nothing
/// can be optimised away, and the count passing a threshold so the selection
/// branch is exercised the way the real loop exercises it.
fn hash_lanes<const L: usize>(s: &[u8], k: usize, thres: u64) -> (u64, usize) {
    let (fw, rc, fw_k, rc_r1, rc_k1) = luts(k as u32);
    let windows = s.len() - k + 1;
    let per = windows / L;

    let mut h_fw = [0u64; L];
    let mut h_rc = [0u64; L];
    let mut pos = [0usize; L];
    for j in 0..L {
        let start = j * per;
        let (f, r) = init_window(s, start, k, &fw, &rc);
        h_fw[j] = f;
        h_rc[j] = r;
        pos[j] = start;
    }

    let mut checksum = 0u64;
    let mut selected = 0usize;
    // Every lane runs the same number of steps; the tail after L*per windows is
    // handled separately in a real implementation and is irrelevant to speed.
    for step in 1..per {
        for j in 0..L {
            let out_c = s[pos[j] + step - 1] as usize;
            let in_c = s[pos[j] + step - 1 + k] as usize;
            h_fw[j] = h_fw[j].rotate_left(1) ^ fw_k[out_c] ^ fw[in_c];
            h_rc[j] = h_rc[j].rotate_right(1) ^ rc_r1[out_c] ^ rc_k1[in_c];
            let h = h_fw[j] ^ h_rc[j];
            checksum = checksum.wrapping_add(h);
            if h <= thres {
                selected += 1;
            }
        }
    }
    (checksum, selected)
}

fn main() {
    let len = 50_000_000usize;
    let bases = b"ACGT";
    let mut state = 0x1234_5678_9abc_def0u64;
    let seq: Vec<u8> = (0..len)
        .map(|_| {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            bases[(state % 4) as usize]
        })
        .collect();

    let k = 25usize;
    let thres = (0.01f64 * u64::MAX as f64) as u64;
    let mb = len as f64 / 1e6;

    macro_rules! bench {
        ($l:expr) => {{
            let _ = hash_lanes::<$l>(&seq[..1_000_000], k, thres);
            let t = std::time::Instant::now();
            let (cs, n) = hash_lanes::<$l>(&seq, k, thres);
            let s = t.elapsed().as_secs_f64();
            println!(
                "  L={:<2} {:>7.3} s  {:>7.1} Mbase/s   selected={} checksum={:x}",
                $l,
                s,
                mb / s,
                n,
                cs
            );
        }};
    }

    println!("\nrolling hash throughput, k={k}, {mb:.0} Mbase, hashing only:");
    bench!(1);
    bench!(2);
    bench!(3);
    bench!(4);
    bench!(6);
    bench!(8);
    bench!(12);
    bench!(16);
    println!();
}
