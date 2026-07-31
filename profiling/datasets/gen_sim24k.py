#!/usr/bin/env python3
"""Simulate a 1x whole-genome read set of 24 kbp reads from T2T-CHM13.

Fills a real gap: every whole-genome dataset in this repo is 12.8 kbp, and the
only 24 kbp WGS set is 2,000 reads, which is ~93% indexing and so measures
nothing about mapping parallelism.

125,000 x 24,000 bp = 3.0 Gbp = 0.962x of hs1. Reads carry HiFi-like
substitution noise so per-read mapping cost is representative; they are
simulated, so this is valid for throughput and scaling, not for mapping
accuracy claims.

Headers follow the repo's ground-truth convention: S<i>_1!<chrom>!<start>!<end>!<strand>
"""
import random
import sys

REF = sys.argv[1]
OUT = sys.argv[2]
N_READS = int(sys.argv[3]) if len(sys.argv) > 3 else 125_000
READ_LEN = int(sys.argv[4]) if len(sys.argv) > 4 else 24_000
ERR = 0.005  # substitution rate, HiFi-ish
random.seed(42)

sys.stderr.write("loading reference...\n")
names, seqs = [], []
cur_name, cur = None, []
with open(REF, "rb") as fh:
    for line in fh:
        if line.startswith(b">"):
            if cur_name is not None:
                names.append(cur_name)
                seqs.append(b"".join(cur))
            cur_name = line[1:].split()[0].decode()
            cur = []
        else:
            cur.append(line.strip())
if cur_name is not None:
    names.append(cur_name)
    seqs.append(b"".join(cur))

usable = [(n, s) for n, s in zip(names, seqs) if len(s) >= READ_LEN * 2]
total = sum(len(s) for _, s in usable)
sys.stderr.write(f"loaded {len(usable)} segments, {total:,} bp\n")

# Sample start positions proportionally to segment length.
weights = [len(s) for _, s in usable]
COMP = bytes.maketrans(b"ACGTNacgtn", b"TGCANtgcan")
ALPHABET = b"ACGT"

written = 0
attempts = 0
buf = []
with open(OUT, "wb") as out:
    while written < N_READS:
        attempts += 1
        if attempts > N_READS * 20:
            sys.exit("too many rejected windows; check the reference")
        name, seq = random.choices(usable, weights=weights, k=1)[0]
        start = random.randrange(0, len(seq) - READ_LEN)
        window = seq[start : start + READ_LEN]
        # Reject windows that are mostly assembly gaps.
        if window.count(b"N") + window.count(b"n") > READ_LEN // 10:
            continue
        r = bytearray(window.upper())
        # Substitution noise.
        for pos in random.sample(range(READ_LEN), int(READ_LEN * ERR)):
            base = r[pos]
            if base == 78:  # leave N alone
                continue
            alt = ALPHABET[random.randrange(4)]
            while alt == base:
                alt = ALPHABET[random.randrange(4)]
            r[pos] = alt
        strand = "+"
        if random.random() < 0.5:
            r = bytearray(bytes(r).translate(COMP)[::-1])
            strand = "-"
        written += 1
        buf.append(b">S%d_1!%s!%d!%d!%s\n%s\n" % (written, name.encode(), start + 1, start + READ_LEN, strand.encode(), bytes(r)))
        if len(buf) >= 2000:
            out.write(b"".join(buf))
            buf.clear()
            if written % 25000 == 0:
                sys.stderr.write(f"  {written:,}/{N_READS:,}\n")
    if buf:
        out.write(b"".join(buf))

sys.stderr.write(f"done: {written:,} reads x {READ_LEN} bp = {written * READ_LEN:,} bp "
                 f"({written * READ_LEN / 3_117_292_070:.4f}x of hs1), {attempts:,} windows tried\n")
