//! The `SHMapper<NBP, OS, AP>` core mapping algorithm.
//!
//! Port of `shmap/src/shmap.h`, split by concern across this directory's
//! submodules (the original file's own method ordering already groups by
//! the same boundaries).

mod pruning;
mod scoring;
mod seeding;
mod stats;
