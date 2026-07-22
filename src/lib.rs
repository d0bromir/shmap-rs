//! shmap: a FracMinHash-sketch-based long-read mapper.
//!
//! Rust port of https://github.com/pesho-ivanov/shmap (Map-SHmap / sweepmap).

pub mod analyse_simulated;
pub mod buckets;
pub mod handler;
pub mod index;
pub mod io;
pub mod mapper;
pub mod mapping;
pub mod params;
pub mod profiling;
pub mod refine;
pub mod shmap;
pub mod sketch;
pub mod types;
pub mod utils;
