//! `print_stats`, `print_time_stats`, `print_warnings`.
//!
//! All read from `handler.counters`/`handler.timers` (the run-wide totals
//! merged in after every read), matching the C++'s `H->C`/`H->T`.

use super::SHMapper;
use crate::handler::Handler;

const ORANGE: &str = "\x1b[38;5;214m";
const RESET: &str = "\x1b[0m";

impl<'idx, const NBP: bool, const OS: bool, const AP: bool> SHMapper<'idx, NBP, OS, AP> {
    pub fn print_stats(&self, handler: &Handler) {
        let c = &handler.counters;
        eprintln!(
            " | Total reads:           {} (~{:.1} nb/read)",
            c.count("reads"),
            c.count("read_len") as f64 / c.count("reads") as f64
        );
        eprintln!(
            " |  | lost on seeding:      {} ({:.1}%)",
            c.count("lost_on_seeding"),
            c.perc("lost_on_seeding", "reads")
        );
        eprintln!(
            " |  | lost on pruning:      {} ({:.1}%)",
            c.count("lost_on_pruning"),
            c.perc("lost_on_pruning", "reads")
        );
        eprintln!(
            " |  | mapped:               {} ({:.1}%)",
            c.count("mapped_reads"),
            c.perc("mapped_reads", "reads")
        );
        eprintln!(" | Kmers:                 {:.1}/read", c.frac("kmers", "reads"));
        eprintln!(
            " |  | sketched:               {:.1} ({:.1}%)",
            c.frac("kmers_sketched", "reads"),
            c.perc("kmers_sketched", "kmers")
        );
        eprintln!(
            " |  | not matched:            {:.1} ({:.1}%)",
            c.frac("kmers_notmatched", "reads"),
            c.perc("kmers_notmatched", "kmers")
        );
        eprintln!(
            " |  | unique:                 {:.1} ({:.1}%)",
            c.frac("kmers_unique", "reads"),
            c.perc("kmers_unique", "kmers")
        );
        eprintln!(
            " |  | seeds:                  {:.1} ({:.1}%)",
            c.frac("kmers_seeds", "reads"),
            c.perc("kmers_seeds", "kmers")
        );
        eprintln!(" | Matches:               {:.1}/read", c.frac("total_matches", "reads"));
        eprintln!(
            " |  | seed matches:           {:.1} ({:.1}%)",
            c.frac("seed_matches", "reads"),
            c.perc("seed_matches", "total_matches")
        );
        eprintln!(
            " |  | in reported mappings:   {:.1} (match inefficiency: {:.1}x)",
            c.frac("matches_in_reported_mappings", "reads"),
            c.frac("total_matches", "matches_in_reported_mappings")
        );
        eprintln!(
            " |  | possible matches:       {:.1} ({:.1}x)",
            c.frac("possible_matches", "reads"),
            c.frac("possible_matches", "total_matches")
        );
        eprintln!(" | Buckets:              ");
        eprintln!(" | | Seeded buckets:          {:.1} /read", c.frac("seeded_buckets", "reads"));
        eprintln!(
            " | Mappings:              {} ({:.1}% of reads)",
            c.count("mappings"),
            c.perc("mappings", "reads")
        );
        eprintln!(" | | Final buckes:            {:.1} /mapping", c.frac("final_buckets", "mappings"));
        eprintln!(
            " | | Average best sim.:       {:.3}",
            c.frac("J_best", "mappings") / 10000.0
        );
        eprintln!(
            " | | mapq=60:                 {} ({:.1}% of mappings)",
            c.count("mapq60"),
            c.perc("mapq60", "mappings")
        );
        eprintln!(
            " | | mapq=0:                 {} ({:.1}% of mappings)",
            c.count("mapq0"),
            c.perc("mapq0", "mappings")
        );
    }

    pub fn print_time_stats(&self, handler: &Handler) {
        let c = &handler.counters;
        let t = &handler.timers;
        eprintln!(
            " | Runtime:                {:>5.1} sec, {:.1} reads/sec ({:>5.1}x)",
            t.secs("mapping"),
            c.count("reads") as f64 / t.secs("mapping"),
            t.range_ratio("query_mapping")
        );
        eprintln!(
            " |  | load reads:             {:>5.1} ({:>4.1}%, {:>6.1}x)",
            t.secs("query_reading"),
            t.perc("query_reading", "mapping"),
            t.range_ratio("query_reading")
        );
        eprintln!(
            " |  | sketch reads:           {:>5.1} ({:>4.1}%, {:>6.1}x)",
            t.secs("sketching"),
            t.perc("sketching", "mapping"),
            t.range_ratio("sketching")
        );
        eprintln!(
            " |  | seed:                   {:>5.1} ({:>4.1}%, {:>6.1}x)",
            t.secs("seeding"),
            t.perc("seeding", "mapping"),
            t.range_ratio("seeding")
        );
        eprintln!(
            " |  |  | group_kmers:            {:>5.1} ({:>4.1}%, {:>6.1}x)",
            t.secs("group_kmers"),
            t.perc("group_kmers", "seeding"),
            t.range_ratio("group_kmers")
        );
        eprintln!(
            " |  |  | collect_kmer_info:      {:>5.1} ({:>4.1}%, {:>6.1}x)",
            t.secs("collect_kmer_info"),
            t.perc("collect_kmer_info", "seeding"),
            t.range_ratio("collect_kmer_info")
        );
        eprintln!(
            " |  |  | sort_kmers:             {:>5.1} ({:>4.1}%, {:>6.1}x)",
            t.secs("sort_kmers"),
            t.perc("sort_kmers", "seeding"),
            t.range_ratio("sort_kmers")
        );
        eprintln!(
            " |  | match seeds:            {:>5.1} ({:>4.1}%, {:>6.1}x)",
            t.secs("match_seeds"),
            t.perc("match_seeds", "mapping"),
            t.range_ratio("match_seeds")
        );
        eprintln!(
            " |  | match rest:             {:>5.1} ({:>4.1}%, {:>6.1}x): {:.1}% for second best",
            t.secs("match_rest"),
            t.perc("match_rest", "mapping"),
            t.range_ratio("match_rest"),
            t.perc("match_rest_for_best2", "match_rest")
        );
        eprintln!(
            " |  |  | refine:                 {:>5.1} ({:>4.1}%, {:>6.1}x)",
            t.secs("refine"),
            t.perc("refine", "match_rest"),
            t.range_ratio("refine")
        );
        eprintln!(
            " |  | output:                 {:>5.1} ({:>4.1}%, {:>6.1}x)",
            t.secs("output"),
            t.perc("output", "mapping"),
            t.range_ratio("output")
        );
    }

    pub fn print_warnings(&self, handler: &Handler) {
        let c = &handler.counters;
        let t = &handler.timers;

        if c.frac("mapped_reads", "reads") < 0.95 {
            eprintln!("{ORANGE}Mapped reads = {} < 0.95.{RESET}", c.frac("mapped_reads", "reads"));
        }
        if c.frac("possible_matches", "total_matches") < 10.0 {
            eprintln!(
                "{ORANGE}Possible matches = {}x < 10.0 => seed heuristic not effective.{RESET}",
                c.frac("possible_matches", "total_matches")
            );
        }
        if c.count("mapq60") < c.count("mapq0") {
            eprintln!(
                "{ORANGE}Reads mapped with mapq=60: {} < mapq=0: {}.{RESET}",
                c.count("mapq60"),
                c.count("mapq0")
            );
        }
        if t.perc("match_seeds", "mapping") > 90.0 {
            eprintln!(
                "{ORANGE}Runtime bottleneck: match_seeds takes {}% of the mapping time.{RESET}",
                t.perc("match_seeds", "mapping")
            );
        }
        if t.perc("match_rest", "mapping") > 90.0 {
            eprintln!(
                "{ORANGE}Runtime bottleneck: match_rest takes {}% of the mapping time.{RESET}",
                t.perc("match_rest", "mapping")
            );
        }
        if t.perc("match_rest_for_best2", "match_rest") > 40.0 {
            eprintln!(
                "{ORANGE}Runtime bottleneck: match_rest_for_best2 takes: {}% > 40% of the match_rest time.{RESET}",
                t.perc("match_rest_for_best2", "match_rest")
            );
        }
    }
}
