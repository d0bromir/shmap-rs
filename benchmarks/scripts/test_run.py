#!/usr/bin/env python3
"""Self-test for run.py's one_read_fasta.

Pesho's indexing/mapping split for a tool with no native phase report (the
C++ reference; see `measure`'s doc comment) depends entirely on this
function slicing out exactly the reads file's first record — get that wrong
(truncate mid-sequence, grab zero records, grab two) and the "index-only" run
silently measures the wrong thing, and every index_s/map_s the C++ reports
is wrong with it.

  python3 benchmarks/scripts/test_run.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import one_read_fasta  # noqa: E402

FAIL: list[str] = []


def check(name: str, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name:46} got {got!r}")
    if not ok:
        FAIL.append(f"{name}: got {got!r}, want {want!r}")


def check_repeats() -> list[str]:
    """Repeats are per host, and only the subject's count moves.

    The failure this guards against is a host silently measuring once when it
    is configured for three, or -- worse -- repeats leaking into the reference
    implementation, whose count is fixed in suite.toml and whose median of
    three is what every published speedup divides by.
    """
    from run import host_config, load_registry, load_suite, plan, subject_repeats
    fail = []
    suite, reg = load_suite(), load_registry()

    # Subject rows only: the drift probe contributes reference jobs that do not
    # scale with this count, and counting them would make the identity false
    # for a reason that has nothing to do with repeats.
    #
    # Benchmarks that pin their own `repeats` are excluded for the same kind of
    # reason: they deliberately ignore the host's count, so including them
    # would make this fail for the override working rather than breaking.
    pinned = {b["id"] for b in suite["benchmark"] if "repeats" in b}

    def n_subject(n):
        return sum(1 for j in plan(suite, reg, ["shmap-rs"], n)
                   if j["impl"] == "shmap-rs" and j["benchmark"] not in pinned)

    one, three = n_subject(1), n_subject(3)
    if three != one * 3:
        fail.append(f"repeats=3 planned {three} jobs, expected {one * 3}")
    print(f"  [{'ok  ' if three == one * 3 else 'FAIL'}] repeats multiply the subject matrix"
          f"{'':14} {one} -> {three}")

    if pinned:
        def n_pinned(n):
            return sum(1 for j in plan(suite, reg, ["shmap-rs"], n)
                       if j["impl"] == "shmap-rs" and j["benchmark"] in pinned)
        p1, p3 = n_pinned(1), n_pinned(3)
        if p1 != p3:
            fail.append(f"a benchmark pinning repeats still moved with the host's: {p1} -> {p3}")
        print(f"  [{'ok  ' if p1 == p3 else 'FAIL'}] but not one that pins its own"
              f"{'':21} {sorted(pinned)} {p1} -> {p3}")

    both = plan(suite, reg, ["shmap-rs", "cpp-shmap"], 3)
    ref = {j["repeats"] for j in both if j["impl"] == "cpp-shmap"}
    want = {suite["run"]["reference_impl"]["repeats"]}
    if ref != want:
        fail.append(f"reference repeats {ref}, expected {want} — suite.toml owns that number")
    print(f"  [{'ok  ' if ref == want else 'FAIL'}] the reference keeps suite.toml's own count"
          f"{'':11} {sorted(ref)}")

    # An unknown host must fall back to the suite default, not to a guess.
    n = subject_repeats(suite, None)
    known = host_config().get("repeats")
    expect = known if isinstance(known, int) else int(suite["run"]["repeats"])
    if n != expect:
        fail.append(f"subject_repeats returned {n}, expected {expect}")
    print(f"  [{'ok  ' if n == expect else 'FAIL'}] this host resolves its own repeat count"
          f"{'':13} {n}")

    if subject_repeats(suite, 1) != 1:
        fail.append("--repeats override ignored")
    print(f"  [{'ok  ' if subject_repeats(suite, 1) == 1 else 'FAIL'}] "
          f"--repeats overrides the host{'':22} 1")
    return fail


def check_drift_probe() -> list[str]:
    """The probe runs the reference unasked, and only where configured.

    Two failures to guard against. One: the probe silently not running, which
    looks exactly like normal operation and leaves compare.py unable to
    normalise drift -- the state this was written to fix. Two: the probe firing
    when the caller asked for the reference anyway, which would measure it
    twice.
    """
    from run import drift_probe_benchmarks, load_registry, load_suite, plan
    fail = []
    suite, reg = load_suite(), load_registry()
    want = drift_probe_benchmarks(suite)

    jobs = plan(suite, reg, ["shmap-rs"], 1)
    probed = {j["benchmark"] for j in jobs if j.get("drift_probe")}
    if probed != want:
        fail.append(f"probe covered {sorted(probed)}, configured for {sorted(want)}")
    print(f"  [{'ok  ' if probed == want else 'FAIL'}] the reference runs unasked on the "
          f"probe set{'':6} {sorted(probed)}")

    keys = {(j["benchmark"], j["metric"]) for j in jobs if j.get("drift_probe")}
    floor = suite["thresholds"].get("drift_min_samples", 6)
    ok = len(keys) >= floor
    if not ok:
        fail.append(f"probe yields {len(keys)} keys, below drift_min_samples={floor}; "
                    f"compare.py would decline to correct")
    print(f"  [{'ok  ' if ok else 'FAIL'}] it yields at least drift_min_samples keys"
          f"{'':7} {len(keys)} >= {floor}")

    both = plan(suite, reg, ["shmap-rs", "cpp-shmap"], 1)
    dup = sum(1 for j in both if j.get("drift_probe"))
    if dup:
        fail.append(f"{dup} probe jobs added when the reference was asked for explicitly")
    print(f"  [{'ok  ' if not dup else 'FAIL'}] no probe when the reference was asked for"
          f"{'':6} {dup}")
    return fail


def check_carry_forward() -> list[str]:
    """Reference rows are carried per key, not all-or-nothing.

    The drift probe means an ordinary run now has *some* reference rows. An
    all-or-nothing rule would read those as "this run measured its reference"
    and drop every benchmark the probe does not cover -- silently, because a
    table with fewer rows still renders.
    """
    import shutil
    import tempfile
    from promote import carry_reference_rows
    from run import load_suite
    fail = []
    suite = load_suite()
    impl_ref = "cpp-shmap"

    head = ("benchmark\timpl\tmetric\tthreads\trepeat\treference_id\treads_id\tparams_id\t"
            "rc\twall_s\tindex_s\tmap_s\tpeak_rss_kb\tmapped\tmapq60\tcmd\n")

    def row(b, impl, m):
        return (f"{b}\t{impl}\t{m}\t1\t0\tREF\tRD\tpaper\t0\t10.0\t1.0\t9.0\t"
                f"1000\t10\t9\tcmd\n")

    benches = ["B01", "B02", "B05"]
    metrics = ["Containment", "Jaccard"]
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        dst = t / "cur"
        dst.mkdir()
        (dst / "results.tsv").write_text(
            head + "".join(row(b, "shmap-rs", m) for b in benches for m in metrics)
            + "".join(row(b, impl_ref, m) for b in benches for m in metrics))
        (dst / "manifest.json").write_text('{"commit": "b" * 3, "finished": "2026-01-01T00:00:00"}'
                                           .replace('"b" * 3', '"bbb"'))

        # A probe run: reference rows for B05 only.
        src = t / "probe"
        src.mkdir()
        (src / "results.tsv").write_text(
            head + "".join(row(b, "shmap-rs", m) for b in benches for m in metrics)
            + "".join(row("B05", impl_ref, m) for m in metrics))
        shutil.copy(dst / "manifest.json", src / "manifest.json")

        c = carry_reference_rows(suite, src, dst)
        got = sorted({l.split("\t")[0] for l in c["_rows"]}) if c else []
        want = ["B01", "B02"]
        if got != want:
            fail.append(f"carried {got}, expected {want}")
        print(f"  [{'ok  ' if got == want else 'FAIL'}] a probe run carries only the "
              f"uncovered keys{'':2} {got}")
        n = c.get("measured_in_run") if c else None
        if n != len(metrics):
            fail.append(f"measured_in_run {n}, expected {len(metrics)}")
        print(f"  [{'ok  ' if n == len(metrics) else 'FAIL'}] and records how many it "
              f"measured itself{'':4} {n}")

        # A run that measured every reference key carries nothing.
        full = t / "full"
        full.mkdir()
        shutil.copy(dst / "results.tsv", full / "results.tsv")
        shutil.copy(dst / "manifest.json", full / "manifest.json")
        none = carry_reference_rows(suite, full, dst)
        if none is not None:
            fail.append(f"a full reference run carried {none.get('rows')} rows")
        print(f"  [{'ok  ' if none is None else 'FAIL'}] a full reference run carries "
              f"nothing{'':8} {none}")
    return fail


def check_manifest() -> list[str]:
    """The manifest builds, and carries what its readers need.

    Guards the failure mode that cost two release runs: a manifest that can
    only be constructed by finishing a benchmark, so a mistake in it surfaces
    hours after the measurements are done and cannot be fixed without redoing
    them.
    """
    import time
    from run import build_manifest, load_registry, load_suite, plan
    fail = []
    suite, reg = load_suite(), load_registry()
    jobs = plan(suite, reg, ["shmap-rs"], 3)

    m = build_manifest(suite=suite, reg=reg, jobs=jobs, rows=[{} for _ in jobs],
                       failed=0, commit="a" * 40, authorized_by="test",
                       t0=time.time(), binaries={}, per_read_files=[])

    # Every field something downstream reads. compare.py guards on host, arch,
    # suite_version and dataset_version; promote.py routes on arch and refuses
    # on failures; report.py prints commit, finished and binaries.
    need = {"schema", "suite_version", "dataset_version", "commit", "host", "arch",
            "rustc", "started", "finished", "duration_s", "invocations", "failures",
            "datasets", "binaries", "per_read_stats", "repeats", "drift_probe"}
    missing = sorted(need - set(m))
    if missing:
        fail.append(f"manifest is missing {missing}")
    print(f"  [{'ok  ' if not missing else 'FAIL'}] carries every field its readers use"
          f"{'':11} {len(need)} keys")

    if m["repeats"]["subject"] != 3:
        fail.append(f"manifest recorded subject repeats {m['repeats']['subject']}, expected 3")
    print(f"  [{'ok  ' if m['repeats']['subject'] == 3 else 'FAIL'}] records the repeat count "
          f"the run actually planned  {m['repeats']['subject']}")

    want_ref = suite["run"]["reference_impl"]["repeats"]
    if m["repeats"]["reference"] != want_ref:
        fail.append(f"reference repeats {m['repeats']['reference']}, expected {want_ref}")
    print(f"  [{'ok  ' if m['repeats']['reference'] == want_ref else 'FAIL'}] and the "
          f"reference's separately{'':16} {m['repeats']['reference']}")

    from run import drift_probe_benchmarks
    want_probe = sorted(drift_probe_benchmarks(suite))
    if m["drift_probe"] != want_probe:
        fail.append(f"drift_probe {m['drift_probe']}, expected {want_probe}")
    print(f"  [{'ok  ' if m['drift_probe'] == want_probe else 'FAIL'}] names the benchmarks "
          f"the probe covered{'':7} {m['drift_probe']}")

    if m["invocations"] != len(jobs):
        fail.append(f"invocations {m['invocations']}, expected {len(jobs)}")
    print(f"  [{'ok  ' if m['invocations'] == len(jobs) else 'FAIL'}] counts the rows it was "
          f"given{'':19} {m['invocations']}")
    return fail


def check_paf_intersection(tmp: Path) -> list[str]:
    """impl_agreement's record count must not depend on which coreutils is installed.

    This replaced `comm -12 <(cut|sort) <(cut|sort)`, which is exact under GNU
    coreutils and lossy under uutils: on the real check galaxy dropped 0.15% of
    genuine matches, silently, in both C and UTF-8 locales.
    """
    from run import paf_intersection
    fail = []

    def paf(name: str, rows: list[str]) -> Path:
        p = tmp / name
        p.write_text("".join(rows))
        return p

    def rec(q: str, tags: str = "") -> str:
        return f"{q}\t24000\t0\t23999\t+\tchr1\t1000\t2000\t3000\t200\t240\t60{tags}\n"

    shared = [rec(f"S{i}_1!chr{i % 22 + 1}!{i * 7}!+") for i in range(500)]
    a = paf("a.paf", shared + [rec("only_in_a!chr1!1!+")])
    b = paf("b.paf", [rec("only_in_b!chr2!2!-")] + shared)
    n = paf_intersection(a, b)
    ok = n == 500
    print(f"  [{'ok  ' if ok else 'FAIL'}] punctuation-rich names, 500 shared{'':13} {n}")
    if not ok:
        fail.append(f"paf_intersection: got {n}, want 500")

    # Columns past the 12th are tags -- per-read timings among them, which
    # differ every run. Two records agreeing on placement must still match.
    a2 = paf("a2.paf", [rec("r1!chr1!1!+", "\ttp:A:P\tt:f:0.001")])
    b2 = paf("b2.paf", [rec("r1!chr1!1!+", "\ttp:A:P\tt:f:0.999")])
    n = paf_intersection(a2, b2)
    ok = n == 1
    print(f"  [{'ok  ' if ok else 'FAIL'}] tags beyond column 12 ignored{'':17} {n}")
    if not ok:
        fail.append(f"paf_intersection ignoring tags: got {n}, want 1")

    # A multiset, matching comm -12: two copies on one side and three on the
    # other share two, not three and not one.
    a3 = paf("a3.paf", [rec("r!chr1!1!+")] * 2)
    b3 = paf("b3.paf", [rec("r!chr1!1!+")] * 3)
    n = paf_intersection(a3, b3)
    ok = n == 2
    print(f"  [{'ok  ' if ok else 'FAIL'}] duplicate records count as a multiset{'':8} {n}")
    if not ok:
        fail.append(f"paf_intersection multiset: got {n}, want 2")

    n = paf_intersection(paf("e1.paf", []), paf("e2.paf", [rec("r!chr1!1!+")]))
    ok = n == 0
    print(f"  [{'ok  ' if ok else 'FAIL'}] empty input{'':34} {n}")
    if not ok:
        fail.append(f"paf_intersection empty: got {n}, want 0")
    return fail


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)

        print("single-record file:")
        src = tmp / "one.fa"
        src.write_text(">r1\nACGT\nACGT\n")
        out = one_read_fasta(str(src), tmp / "cache1")
        check("record count", out.read_text().count(">"), 1)
        check("content unchanged", out.read_text(), ">r1\nACGT\nACGT\n")

        print("\nmulti-record file: only the first is kept")
        src = tmp / "multi.fa"
        src.write_text(">r1\nAAAA\nAAAA\n>r2\nCCCC\n>r3\nGGGG\n")
        out = one_read_fasta(str(src), tmp / "cache2")
        check("record count", out.read_text().count(">"), 1)
        check("first record's header kept", out.read_text().splitlines()[0], ">r1")
        check("second record's header dropped", "r2" in out.read_text(), False)
        check("first record's sequence not truncated", out.read_text(), ">r1\nAAAA\nAAAA\n")

        print("\nwrapped (multi-line) sequence within one record: no lines dropped")
        src = tmp / "wrapped.fa"
        src.write_text(">r1\nAAAA\nCCCC\nGGGG\nTTTT\n>r2\nNNNN\n")
        out = one_read_fasta(str(src), tmp / "cache3")
        check("all four sequence lines kept", out.read_text(), ">r1\nAAAA\nCCCC\nGGGG\nTTTT\n")

        print("\ncached: a second call for the same reads file returns the same content"
              " without re-deriving it from a now-different source")
        src = tmp / "cache_check.fa"
        src.write_text(">r1\nAAAA\n")
        cache_dir = tmp / "cache4"
        out1 = one_read_fasta(str(src), cache_dir)
        src.write_text(">different\nZZZZ\n")  # source changes; cache must not
        out2 = one_read_fasta(str(src), cache_dir)
        check("same cached path returned", out2, out1)
        check("cached content unaffected by the source changing after", out2.read_text(), ">r1\nAAAA\n")

        print("\nrepeat plumbing:")
        FAIL.extend(check_repeats())

        print("\ndrift probe:")
        FAIL.extend(check_drift_probe())

        print("\nreference carry-forward:")
        FAIL.extend(check_carry_forward())

        print("\nmanifest:")
        FAIL.extend(check_manifest())

        print("\nimpl_agreement record count:")
        FAIL.extend(check_paf_intersection(tmp))

        print()
        if FAIL:
            for f in FAIL:
                print(f"  {f}")
            print(f"{len(FAIL)} failure(s)")
            return 1
        print("OK — one record, per-host repeats, a live drift probe, carry-forward, manifest")
        return 0


if __name__ == "__main__":
    sys.exit(main())
