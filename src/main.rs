//! CLI entry point. Port of `shmap/src/map.cpp`.

use anyhow::Context;
use clap::Parser;

use shmap::handler::Handler;
use shmap::index::SketchIndex;
use shmap::mapper::create_mapper;
use shmap::params::Params;
use shmap::profiling::Profiler;

fn main() -> anyhow::Result<()> {
    let params = Params::parse();
    if let Err(e) = params.validate() {
        eprintln!("ERROR: {e}");
        std::process::exit(1);
    }

    let t_file = params.t_file.clone();
    let p_file = params.p_file.clone();
    let max_matches = params.max_matches;
    let profile_log_path = params.profile_log_path();

    let profiler = Profiler::new(params.profile);
    if params.profile {
        eprintln!("Profiling enabled -> writing report to {profile_log_path}");
    }
    profiler.meta("k", params.k);
    profiler.meta("h_frac", params.h_frac);
    profiler.meta("theta", params.theta);
    profiler.meta("threads_requested", params.threads);
    profiler.meta(
        "available_parallelism",
        std::thread::available_parallelism().map(|n| n.get()).unwrap_or(0),
    );
    profiler.meta("t_file", &t_file);
    profiler.meta("p_file", &p_file);
    profiler.meta("os", std::env::consts::OS);

    let mut handler = Handler::new(params)?;
    let mut tidx = SketchIndex::new();
    profiler.mem_mark("before_index");
    tidx.build_index(
        &t_file,
        &handler.sketcher,
        max_matches,
        &mut handler.counters,
        &mut handler.timers,
        &profiler,
        handler.params.threads,
    )
    .with_context(|| format!("failed to build index from {t_file}"))?;
    profiler.mem_mark("after_index");

    let mut mapper = create_mapper(&tidx, &handler);
    mapper
        .map_reads(&mut handler, &p_file, &profiler)
        .with_context(|| format!("failed while mapping reads from {p_file}"))?;
    profiler.mem_mark("after_mapping");

    profiler
        .finish_and_write(&profile_log_path, &handler.timers, &handler.counters)
        .with_context(|| format!("failed to write profiling report to {profile_log_path}"))?;

    Ok(())
}
