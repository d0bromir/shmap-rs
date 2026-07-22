//! `Mapper` trait and the const-generic dispatch that selects one of the
//! 8 `SHMapper<NBP, OS, AP>` combinations at runtime.
//!
//! Port of `shmap/src/mapper.h` + `shmap/src/mapper.cpp`. Upstream's
//! `MapperFactory::createMapper` only ever instantiates one of these 8
//! combinations (`case 0`; the other 7 `case` arms are commented out) —
//! this port wires up all 8 as real, live combinations instead, per the
//! full-parity decision made when this port was planned.

use crate::handler::Handler;
use crate::index::SketchIndex;
use crate::profiling::Profiler;
use crate::shmap::SHMapper;

pub trait Mapper {
    fn map_reads(&mut self, handler: &mut Handler, p_file: &str, profiler: &Profiler) -> anyhow::Result<()>;
}

impl<'idx, const NBP: bool, const OS: bool, const AP: bool> Mapper for SHMapper<'idx, NBP, OS, AP> {
    fn map_reads(&mut self, handler: &mut Handler, p_file: &str, profiler: &Profiler) -> anyhow::Result<()> {
        SHMapper::map_reads(self, handler, p_file, profiler)
    }
}

pub fn create_mapper<'idx>(tidx: &'idx SketchIndex, handler: &Handler) -> Box<dyn Mapper + 'idx> {
    let nbp = handler.params.no_bucket_pruning;
    let os = handler.params.one_sweep;
    let ap = handler.params.abs_pos;

    match (nbp, os, ap) {
        (false, false, false) => Box::new(SHMapper::<false, false, false>::new(tidx)),
        (true, false, false) => Box::new(SHMapper::<true, false, false>::new(tidx)),
        (false, true, false) => Box::new(SHMapper::<false, true, false>::new(tidx)),
        (false, false, true) => Box::new(SHMapper::<false, false, true>::new(tidx)),
        (true, true, false) => Box::new(SHMapper::<true, true, false>::new(tidx)),
        (true, false, true) => Box::new(SHMapper::<true, false, true>::new(tidx)),
        (false, true, true) => Box::new(SHMapper::<false, true, true>::new(tidx)),
        (true, true, true) => Box::new(SHMapper::<true, true, true>::new(tidx)),
    }
}
