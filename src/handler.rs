//! Run-wide handler bundling parameters, the sketcher, and aggregate stats.
//!
//! Port of `shmap/src/handler.h`.
//!
//! `printMemoryUsage` (reads `/proc/self/status`) has zero live call sites
//! upstream — every call to it is commented out — and is dropped.

use anyhow::Result;

use crate::params::Params;
use crate::sketch::FracMinHash;
use crate::utils::{Counters, Timers};

pub struct Handler {
    pub counters: Counters,
    pub timers: Timers,
    pub params: Params,
    pub sketcher: FracMinHash,
}

impl Handler {
    pub fn new(params: Params) -> Result<Self> {
        let mut timers = Timers::new();
        let sketcher = FracMinHash::new(params.k, params.h_frac);

        timers.start("total");
        if !params.params_file.is_empty() {
            eprintln!("Writing parameters to {}...", params.params_file);
            let mut fout = std::fs::File::create(&params.params_file)?;
            params.print(&mut fout, false)?;
        } else {
            params.print(&mut std::io::stderr(), true)?;
        }
        params.print_display(&mut std::io::stderr())?;

        Ok(Handler {
            counters: Counters::new(),
            timers,
            params,
            sketcher,
        })
    }

    pub fn print_sketching_stats(&self) {
        eprintln!("Sketching:");
        eprintln!(
            " | Sketched sequences:    {} ({} nb)",
            self.counters.count("sketched_seqs"),
            self.counters.count("sketched_len")
        );
        eprintln!(" | Kmers:                 {}", self.counters.count("sketched_kmers"));
    }
}

impl Drop for Handler {
    fn drop(&mut self) {
        self.timers.stop("total");
        self.print_sketching_stats();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::Parser;

    #[test]
    fn handler_accumulates_sketching_counters() {
        let params = Params::try_parse_from(["shmap", "-p", "r.fa", "-s", "t.fa", "-k", "4", "-r", "1.0"]).unwrap();
        let mut handler = Handler::new(params).unwrap();
        handler.sketcher.sketch(b"ACGTACGT", &mut handler.counters);
        assert_eq!(handler.counters.count("sketched_seqs"), 1);
        assert_eq!(handler.counters.count("sketched_len"), 8);
    }
}
