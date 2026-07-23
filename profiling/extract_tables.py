#!/usr/bin/env python3
"""Extracts every shmap-rs `--profile` JSON report in a directory into one
combined Markdown file of tables: run metadata, global timers/counters, the
per-thread breakdown, and named memory marks -- one section per input file.

This is a *complete* dump (every timer, every counter, every thread, every
named memory mark) rather than a curated summary -- see PROFILING.md for the
narrative findings; this script is what regenerates the raw tables backing it
whenever new *.profile.json files are added.

Usage:
    ./extract_tables.py                              # profiling/*.profile.json -> profiling/tables.md
    ./extract_tables.py --input-dir DIR --output OUT.md
    ./extract_tables.py file1.profile.json file2.profile.json --output OUT.md
"""
import argparse
import json
import sys
from pathlib import Path


def load_profiles(paths):
    profiles = []
    for p in paths:
        with open(p) as f:
            data = json.load(f)
        profiles.append((p, data))
    return profiles


def fmt(x, prec=3):
    if isinstance(x, float):
        return f"{x:.{prec}f}"
    return str(x)


def meta_table(data):
    lines = ["| Key | Value |", "|---|---|"]
    lines.append(f"| shmap_version | {data.get('shmap_version', '?')} |")
    lines.append(f"| started_at_unix | {data.get('started_at_unix', '?')} |")
    lines.append(f"| wall_seconds | {fmt(data.get('wall_seconds', 0))} |")
    for k, v in data.get("meta", {}).items():
        lines.append(f"| {k} | {v} |")
    return "\n".join(lines)


def timers_table(timers, wall_seconds):
    rows = sorted(timers.items(), key=lambda kv: -kv[1])
    lines = ["| Stage | Seconds | % of wall |", "|---|---:|---:|"]
    for name, secs in rows:
        pct = 100.0 * secs / wall_seconds if wall_seconds else 0.0
        lines.append(f"| {name} | {fmt(secs)} | {fmt(pct, 1)}% |")
    return "\n".join(lines)


def counters_table(counters):
    rows = sorted(counters.items(), key=lambda kv: kv[0])
    lines = ["| Counter | Value |", "|---|---:|"]
    for name, val in rows:
        lines.append(f"| {name} | {val} |")
    return "\n".join(lines)


def threads_table(threads, wall_seconds):
    lines = [
        "| Thread | Role | Jobs | Busy s (top timer) | % of wall |",
        "|---|---|---:|---:|---:|",
    ]
    for t in threads:
        timers = t.get("timers_secs", {})
        if timers:
            top_name, top_secs = max(timers.items(), key=lambda kv: kv[1])
            busy = f"{fmt(top_secs)} ({top_name})"
            pct = 100.0 * top_secs / wall_seconds if wall_seconds else 0.0
            pct_str = f"{fmt(pct, 1)}%"
        else:
            busy, pct_str = "0", "0.0%"
        lines.append(f"| {t['label']} | {t['role']} | {t['jobs']} | {busy} | {pct_str} |")
    return "\n".join(lines)


def thread_detail_tables(threads):
    """One timers/counters sub-table per thread, for full traceability."""
    parts = []
    for t in threads:
        parts.append(f"<details><summary><code>{t['label']}</code> ({t['role']}, {t['jobs']} jobs) -- full timers/counters</summary>\n")
        parts.append("\n**Timers (s)**\n")
        parts.append(_plain_timers_table(t.get("timers_secs", {})))
        parts.append("\n**Counters**\n")
        parts.append(counters_table(t.get("counters", {})))
        parts.append("\n</details>\n")
    return "\n".join(parts)


def _plain_timers_table(timers):
    rows = sorted(timers.items(), key=lambda kv: -kv[1])
    lines = ["| Stage | Seconds |", "|---|---:|"]
    for name, secs in rows:
        lines.append(f"| {name} | {fmt(secs)} |")
    return "\n".join(lines)


def memory_table(memory):
    marks = [s for s in memory["samples"] if s.get("label")]
    lines = ["| Label | At (s) | RSS (MB) | Peak RSS so far (MB) |", "|---|---:|---:|---:|"]
    for s in marks:
        lines.append(
            f"| {s['label']} | {fmt(s['at_secs'])} | {fmt(s['rss_kb'] / 1024, 1)} | {fmt(s['vmhwm_kb'] / 1024, 1)} |"
        )
    n_periodic = len(memory["samples"]) - len(marks)
    extra = f"\n\n({n_periodic} additional periodic background samples not shown; peak_rss_kb overall = {memory['peak_rss_kb']} KB = {memory['peak_rss_kb'] / 1024 / 1024:.2f} GB)"
    return "\n".join(lines) + extra


def render_profile(path, data):
    name = Path(path).stem.replace(".profile", "")
    out = [f"## {name}\n"]
    out.append("### Run info\n")
    out.append(meta_table(data) + "\n")
    out.append("### Global timers (summed across all threads)\n")
    out.append(timers_table(data["global"]["timers_secs"], data["wall_seconds"]) + "\n")
    out.append("### Global counters\n")
    out.append(counters_table(data["global"]["counters"]) + "\n")
    out.append("### Per-thread breakdown\n")
    out.append(threads_table(data["threads"], data["wall_seconds"]) + "\n")
    out.append("### Per-thread full detail\n")
    out.append(thread_detail_tables(data["threads"]) + "\n")
    out.append("### Memory (named marks)\n")
    out.append(memory_table(data["memory"]) + "\n")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="explicit *.profile.json files (default: --input-dir glob)")
    ap.add_argument("--input-dir", default=str(Path(__file__).resolve().parent),
                    help="directory to glob for *.profile.json (default: this script's directory)")
    ap.add_argument("--pattern", default="*.profile.json", help="glob pattern within --input-dir")
    ap.add_argument("--output", default=None,
                    help="output .md path (default: <input-dir>/tables.md)")
    args = ap.parse_args()

    if args.files:
        paths = [Path(f) for f in args.files]
    else:
        paths = sorted(Path(args.input_dir).glob(args.pattern))

    if not paths:
        sys.exit(f"no profile JSON files found (looked in {args.input_dir!r} for {args.pattern!r})")

    output = Path(args.output) if args.output else Path(args.input_dir) / "tables.md"

    profiles = load_profiles(paths)
    sections = [
        "# shmap-rs profiling data tables\n",
        "Auto-generated by `profiling/extract_tables.py` from every `*.profile.json` report in "
        "this directory -- a complete dump of timers/counters/threads/memory, not a curated "
        "summary (see `PROFILING.md` for that). Re-run the script after adding new profile "
        "JSON files to regenerate.\n",
    ]
    for path, data in profiles:
        sections.append(render_profile(path, data))

    output.write_text("\n".join(sections))
    print(f"wrote {output} ({len(profiles)} profile(s))")


if __name__ == "__main__":
    main()
