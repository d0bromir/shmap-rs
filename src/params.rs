//! CLI parameters.
//!
//! Port of `params_t` from `shmap/src/io.h`, using `clap` instead of
//! `cmd_line_parser`.

use std::io::Write;

use anyhow::{bail, Result};
use clap::Parser;

use crate::types::Metric;

/// Fast and accurate sketch-based read mapper.
#[derive(Parser, Debug, Clone)]
#[command(name = "shmap", version, about = "Map-SHmap: fast and accurate sketch-based read mapper")]
pub struct Params {
    /// Reads file (FASTA format)
    #[arg(short = 'p', long = "pattern")]
    pub p_file: String,

    /// Reference file (FASTA format)
    #[arg(short = 's', long = "text")]
    pub t_file: String,

    /// K-mer length to be used for sketches (positive integer)
    #[arg(short = 'k', long = "ksize", default_value_t = 15)]
    pub k: i32,

    /// FracMinHash ratio in (0,1]
    #[arg(short = 'r', long = "hashratio", default_value_t = 0.05)]
    pub h_frac: f64,

    /// Maximum seeds in a sketch (positive integer).
    ///
    /// Accepted for CLI compatibility, but not read anywhere by the
    /// algorithm — confirmed dead upstream too (only parsed/printed,
    /// never branched on), so kept as an accepted-but-inert flag rather
    /// than dropped outright the way the confirmed-unused `-a`/`sam` flag
    /// was.
    #[arg(short = 'S', long = "max_seeds")]
    pub max_seeds: Option<i32>,

    /// Maximum seed matches in a sketch (positive integer)
    #[arg(short = 'M', long = "max_matches")]
    pub max_matches: Option<i32>,

    /// Homology threshold in [0,1]
    #[arg(short = 't', long = "threshold", default_value_t = 0.9)]
    pub theta: f64,

    /// Minimum difference between best and second best mapping
    #[arg(short = 'd', long = "min_diff", default_value_t = 0.02)]
    pub min_diff: f64,

    /// Maximum overlap between best and second best mapping (0,1]
    #[arg(short = 'o', long = "max_overlap", default_value_t = 0.5)]
    pub max_overlap: f64,

    /// Optimization metric: bucket_SH, bucket_LCS, Containment, Jaccard
    #[arg(short = 'm', long = "metric", value_enum, default_value_t = Metric::Containment)]
    pub metric: Metric,

    /// Output file with parameters (tsv)
    #[arg(short = 'z', long = "params", default_value = "")]
    pub params_file: String,

    /// Verbosity level: 0 for none, 1 for some, 2 for all
    #[arg(short = 'v', long = "verbose", default_value_t = 0)]
    pub verbose: i32,

    /// Normalize scores by length.
    ///
    /// Accepted for CLI compatibility, but not read anywhere by the
    /// algorithm — confirmed dead upstream too, same as `max_seeds` above.
    #[arg(short = 'n', long = "normalize")]
    pub normalize: bool,

    /// Disables bucket pruning
    #[arg(short = 'P', long = "no_bucket_pruning")]
    pub no_bucket_pruning: bool,

    /// Disregards seed heuristic and runs one sweep on all matches
    #[arg(short = 'B', long = "one_sweep")]
    pub one_sweep: bool,

    /// Use absolute positions instead of kmer positions
    #[arg(short = 'F', long = "abs_pos")]
    pub abs_pos: bool,
}

impl Params {
    /// Range/sign checks equivalent to `params_t::prsArgs`'s validation
    /// (clap handles the parsing/required-ness itself).
    pub fn validate(&self) -> Result<()> {
        if self.k <= 0 {
            bail!("K-mer length (-k) should be positive. You provided {}.", self.k);
        }
        if self.h_frac <= 0.0 || self.h_frac > 1.0 {
            bail!("Given hash ratio (-r) should be between 0 and 1. You provided {}.", self.h_frac);
        }
        if let Some(s) = self.max_seeds {
            if s <= 0 {
                bail!("The number of seeds (-S) should be positive. You provided {s}.");
            }
        }
        if let Some(m) = self.max_matches {
            if m <= 0 {
                bail!("The number of seed matches (-M) should be positive. You provided {m}.");
            }
        }
        if self.theta < 0.0 || self.theta > 1.0 {
            bail!("The threshold (-t) should be between 0 and 1. You provided {}.", self.theta);
        }
        if self.min_diff < 0.0 {
            bail!("The minimum difference (-d) should be >= 0. You provided {}.", self.min_diff);
        }
        if self.max_overlap < 0.0 || self.max_overlap > 1.0 {
            bail!(
                "The maximum overlap (-o) should be between 0 and 1. You provided {}.",
                self.max_overlap
            );
        }
        if !(0..=2).contains(&self.verbose) {
            bail!("--verbose (-v) should be 0, 1, or 2. You provided {}.", self.verbose);
        }
        Ok(())
    }

    fn fields(&self) -> Vec<(&'static str, String)> {
        let opt_i32 = |o: Option<i32>| o.map_or_else(|| "-1".to_string(), |v| v.to_string());
        vec![
            ("pFile", self.p_file.clone()),
            ("tFile", self.t_file.clone()),
            ("k", self.k.to_string()),
            ("hFrac", self.h_frac.to_string()),
            ("max_seeds", opt_i32(self.max_seeds)),
            ("max_matches", opt_i32(self.max_matches)),
            ("tThres", self.theta.to_string()),
            ("min_diff", self.min_diff.to_string()),
            ("max_overlap", self.max_overlap.to_string()),
            ("paramsFile", self.params_file.clone()),
            ("metric", self.metric.to_string()),
            ("normalize", (self.normalize as i32).to_string()),
            ("verbose", self.verbose.to_string()),
            ("no-bucket-pruning", (self.no_bucket_pruning as i32).to_string()),
            ("one-sweep", (self.one_sweep as i32).to_string()),
            ("abs-pos", (self.abs_pos as i32).to_string()),
        ]
    }

    /// Prints parameters either as a human-readable table or as TSV
    /// (header line, then a values line — matching the C++ exactly, which
    /// emits no trailing newline after the values line either).
    pub fn print(&self, out: &mut impl Write, human: bool) -> std::io::Result<()> {
        let fields = self.fields();
        if human {
            writeln!(out, "Parameters:")?;
            for (name, value) in &fields {
                writeln!(out, "{name:>20}: {value}")?;
            }
        } else {
            for (name, _) in &fields {
                write!(out, "{name}\t")?;
            }
            writeln!(out)?;
            for (_, value) in &fields {
                write!(out, "{value}\t")?;
            }
        }
        Ok(())
    }

    /// A short display of key parameters.
    pub fn print_display(&self, out: &mut impl Write) -> std::io::Result<()> {
        writeln!(out, "Params:")?;
        writeln!(out, " | reference:             {}", self.t_file)?;
        writeln!(out, " | reads:                 {}", self.p_file)?;
        writeln!(out, " | metric:                {}", self.metric)?;
        writeln!(out, " | k:                     {}", self.k)?;
        writeln!(out, " | hFrac:                 {}", self.h_frac)?;
        writeln!(out, " | verbose:               {}", self.verbose)?;
        writeln!(out, " | tThres:                {}", self.theta)?;
        writeln!(out, " | min_diff:              {}", self.min_diff)?;
        writeln!(out, " | max_overlap:           {}", self.max_overlap)?;
        writeln!(out, " | no-bucket-pruning:     {}", self.no_bucket_pruning as i32)?;
        writeln!(out, " | abs-pos:               {}", self.abs_pos as i32)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::CommandFactory;

    #[test]
    fn cli_definition_is_valid() {
        Params::command().debug_assert();
    }

    #[test]
    fn validate_rejects_out_of_range_k() {
        let mut p = Params::try_parse_from(["shmap", "-p", "r.fa", "-s", "t.fa"]).unwrap();
        p.k = 0;
        assert!(p.validate().is_err());
    }

    #[test]
    fn validate_accepts_defaults() {
        let p = Params::try_parse_from(["shmap", "-p", "r.fa", "-s", "t.fa"]).unwrap();
        assert!(p.validate().is_ok());
        assert_eq!(p.k, 15);
        assert_eq!(p.metric, Metric::Containment);
        assert_eq!(p.max_seeds, None);
    }

    #[test]
    fn metric_flag_accepts_original_cli_strings() {
        let p = Params::try_parse_from(["shmap", "-p", "r.fa", "-s", "t.fa", "-m", "bucket_SH"]).unwrap();
        assert_eq!(p.metric, Metric::BucketSh);
    }
}
