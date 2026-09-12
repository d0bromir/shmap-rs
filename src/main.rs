//! CLI entry point. Port of `shmap/src/map.cpp`.

use anyhow::Context;
use clap::Parser;
use mimalloc::MiMalloc;

use shmap::handler::Handler;
use shmap::index::SketchIndex;
use shmap::mapper::create_mapper;
use shmap::params::Params;
use shmap::profiling::Profiler;

#[global_allocator]
static GLOBAL: MiMalloc = MiMalloc;

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
    profiler.meta("reader_threads", params.reader_threads);
    profiler.meta(
        "available_parallelism",
        std::thread::available_parallelism().map(|n| n.get()).unwrap_or(0),
    );
    profiler.meta("t_file", &t_file);
    profiler.meta("p_file", &p_file);
    profiler.meta("os", std::env::consts::OS);
    profiler.meta("adaptive", params.adaptive);
    profiler.meta("adaptive_dense", params.adaptive_dense);
    profiler.meta("compact_index", params.compact_index || params.index_cache.is_some());

    let mut handler = Handler::new(params)?;
    let mut tidx = SketchIndex::new();
    profiler.mem_mark("before_index");
    if let Some(path) = &handler.params.index_cache
        && path.try_exists()?
    {
        handler.timers.start("indexing");
        handler.timers.start("index_load");
        tidx = SketchIndex::load_cached(
            path,
            std::path::Path::new(&t_file),
            &handler.sketcher,
            max_matches,
            handler.params.verify_index_reference,
            &mut handler.counters,
        )
        .with_context(|| format!("failed to load index {}", path.display()))?;
        handler.timers.stop("index_load");
        handler.timers.stop("indexing");
    } else {
        let before = std::fs::metadata(&t_file)?;
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
        if let Some(path) = &handler.params.index_cache {
            let after = std::fs::metadata(&t_file)?;
            anyhow::ensure!(
                before.len() == after.len() && before.modified()? == after.modified()?,
                "reference changed during indexing"
            );
            handler.timers.start("index_save");
            tidx.save_cached(
                path,
                std::path::Path::new(&t_file),
                &handler.sketcher,
                max_matches,
                &handler.counters,
            )?;
            handler.timers.stop("index_save");
        }
    }
    if handler.params.compact_index {
        handler.timers.start("index_compact");
        tidx.compact();
        handler.timers.stop("index_compact");
    }
    if handler.params.adaptive_dense {
        handler.timers.start("repeat_indexing");
        tidx.build_repeat_index(&t_file, handler.params.k, &mut handler.counters, &mut handler.timers)?;
        handler.timers.stop("repeat_indexing");
    }
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
