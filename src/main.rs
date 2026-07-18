//! CLI entry point. Port of `shmap/src/map.cpp`.

use anyhow::Context;
use clap::Parser;

use shmap::handler::Handler;
use shmap::index::SketchIndex;
use shmap::mapper::create_mapper;
use shmap::params::Params;

fn main() -> anyhow::Result<()> {
    let params = Params::parse();
    if let Err(e) = params.validate() {
        eprintln!("ERROR: {e}");
        std::process::exit(1);
    }

    let t_file = params.t_file.clone();
    let p_file = params.p_file.clone();
    let max_matches = params.max_matches;

    let mut handler = Handler::new(params)?;
    let mut tidx = SketchIndex::new();
    tidx.build_index(
        &t_file,
        &handler.sketcher,
        max_matches,
        &mut handler.counters,
        &mut handler.timers,
    )
    .with_context(|| format!("failed to build index from {t_file}"))?;

    let mut mapper = create_mapper(&tidx, &handler);
    mapper
        .map_reads(&mut handler, &p_file)
        .with_context(|| format!("failed while mapping reads from {p_file}"))?;

    Ok(())
}
