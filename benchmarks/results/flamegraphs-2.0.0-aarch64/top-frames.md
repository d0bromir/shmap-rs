### B01

| self % | function |
|---:|---|
|  23.4 | `<shmap::sketch::FracMinHash>::sketch_slice_into` |
|  21.2 | `<shmap::shmap::SHMapper<false, false, false>>::map_reads::{closure#0}::{closure#0}` |
|  20.9 | `<shmap::shmap::SHMapper<false, false, false>>::match_rest` |
|   6.2 | `<shmap::index::SketchIndex>::insert_hit` |
|   4.8 | `[kernel]` |
|   1.8 | `__memcpy_generic` |
|   1.8 | `<needletail::parser::record::SequenceRecord>::seq` |
|   1.7 | `<needletail::parser::fasta::Reader<core::io::util::Chain<core::io::cursor::Cursor<[u8: 2]>, std::fs::File>>...` |
|   1.2 | `__aarch64_cas8_acq_rel` |
|   1.2 | `core::slice::sort::stable::quicksort::quicksort::<shmap::types::Kmer, <[shmap::types::Kmer]>::sort_by<<shma...` |
|   1.1 | `_mi_page_malloc_zero` |
|   0.9 | `<core::hash::sip::Hasher<core::hash::sip::Sip13Rounds> as core::hash::Hasher>::write` |

### B02

| self % | function |
|---:|---|
|  27.2 | `<shmap::sketch::FracMinHash>::sketch_slice_into` |
|  23.4 | `<shmap::shmap::SHMapper<false, false, false>>::map_reads::{closure#0}::{closure#0}` |
|  13.3 | `<shmap::shmap::SHMapper<false, false, false>>::match_rest` |
|   7.4 | `<shmap::index::SketchIndex>::insert_hit` |
|   4.8 | `[kernel]` |
|   2.6 | `__memcpy_generic` |
|   2.2 | `<needletail::parser::record::SequenceRecord>::seq` |
|   2.0 | `<needletail::parser::fasta::Reader<core::io::util::Chain<core::io::cursor::Cursor<[u8: 2]>, std::fs::File>>...` |
|   1.3 | `core::slice::sort::stable::quicksort::quicksort::<shmap::types::Kmer, <[shmap::types::Kmer]>::sort_by<<shma...` |
|   1.2 | `<hashbrown::raw::RawTable<(u64, shmap::types::Seed)>>::reserve_rehash::<hashbrown::map::make_hasher<u64, sh...` |
|   1.2 | `__aarch64_cas8_acq_rel` |
|   1.1 | `_mi_page_malloc_zero` |

### B03

| self % | function |
|---:|---|
|  23.2 | `<shmap::shmap::SHMapper<false, false, false>>::map_reads::{closure#0}::{closure#0}` |
|  20.9 | `<shmap::sketch::FracMinHash>::sketch_slice_into` |
|  19.1 | `<shmap::shmap::SHMapper<false, false, false>>::match_rest` |
|   5.3 | `<shmap::index::SketchIndex>::insert_hit` |
|   4.5 | `[kernel]` |
|   1.9 | `__memcpy_generic` |
|   1.6 | `<needletail::parser::record::SequenceRecord>::seq` |
|   1.5 | `<needletail::parser::fasta::Reader<core::io::util::Chain<core::io::cursor::Cursor<[u8: 2]>, std::fs::File>>...` |
|   1.5 | `_mi_page_malloc_zero` |
|   1.5 | `__aarch64_cas8_acq_rel` |
|   1.2 | `core::slice::sort::unstable::quicksort::quicksort::<u32, <u32 as core::cmp::PartialOrd>::lt>` |
|   1.1 | `core::slice::sort::unstable::quicksort::quicksort::<u64, <u64 as core::cmp::PartialOrd>::lt>` |

### B04

| self % | function |
|---:|---|
|  29.1 | `<shmap::shmap::SHMapper<false, false, false>>::map_reads::{closure#0}::{closure#0}` |
|  24.2 | `<shmap::shmap::SHMapper<false, false, false>>::match_rest` |
|  14.2 | `<shmap::sketch::FracMinHash>::sketch_slice_into` |
|   4.8 | `[kernel]` |
|   1.9 | `_mi_page_malloc_zero` |
|   1.7 | `__aarch64_cas8_acq_rel` |
|   1.6 | `core::slice::sort::unstable::quicksort::quicksort::<u64, <u64 as core::cmp::PartialOrd>::lt>` |
|   1.5 | `core::slice::sort::unstable::quicksort::quicksort::<u32, <u32 as core::cmp::PartialOrd>::lt>` |
|   1.5 | `<core::hash::sip::Hasher<core::hash::sip::Sip13Rounds> as core::hash::Hasher>::write` |
|   1.1 | `core::slice::sort::stable::quicksort::quicksort::<shmap::types::Kmer, <[shmap::types::Kmer]>::sort_by<<shma...` |
|   1.1 | `<hashbrown::raw::RawTable<(u64, shmap::types::Seed)>>::reserve_rehash::<hashbrown::map::make_hasher<u64, sh...` |
|   1.1 | `<shmap::shmap::SHMapper<false, false, false>>::map_reads` |

### B05

| self % | function |
|---:|---|
|  33.1 | `<shmap::sketch::FracMinHash>::sketch_slice_into` |
|  13.6 | `<shmap::shmap::SHMapper<false, false, false>>::map_reads::{closure#0}::{closure#0}` |
|  11.1 | `[kernel]` |
|   9.6 | `<shmap::index::SketchIndex>::insert_hit` |
|   5.8 | `<shmap::shmap::SHMapper<false, false, false>>::match_rest` |
|   3.8 | `__memcpy_generic` |
|   2.8 | `<needletail::parser::record::SequenceRecord>::seq` |
|   2.6 | `<needletail::parser::fasta::Reader<core::io::util::Chain<core::io::cursor::Cursor<[u8: 2]>, std::fs::File>>...` |
|   1.4 | `__aarch64_cas8_acq_rel` |
|   1.2 | `core::slice::sort::stable::quicksort::quicksort::<shmap::types::Kmer, <[shmap::types::Kmer]>::sort_by<<shma...` |
|   1.1 | `<shmap::index::SketchIndex>::finalize_shard` |
|   1.0 | `<hashbrown::raw::RawTable<(u64, shmap::types::Seed)>>::reserve_rehash::<hashbrown::map::make_hasher<u64, sh...` |

### B06

| self % | function |
|---:|---|
|  27.4 | `<shmap::shmap::SHMapper<false, false, false>>::map_reads::{closure#0}::{closure#0}` |
|  27.2 | `<shmap::shmap::SHMapper<false, false, false>>::match_rest` |
|   8.5 | `core::slice::sort::unstable::quicksort::quicksort::<(i32, i32, i32), <[(i32, i32, i32)]>::sort_unstable_by_...` |
|   6.2 | `core::slice::sort::unstable::quicksort::quicksort::<u64, <u64 as core::cmp::PartialOrd>::lt>` |
|   4.3 | `<shmap::buckets::Buckets<false>>::merge_entries` |
|   4.3 | `core::slice::sort::unstable::quicksort::quicksort::<u32, <u32 as core::cmp::PartialOrd>::lt>` |
|   2.9 | `<shmap::buckets::Buckets<false>>::get_sorted_buckets` |
|   2.4 | `_mi_page_malloc_zero` |
|   1.4 | `<shmap::shmap::SHMapper<false, false, false>>::map_reads` |
|   1.2 | `core::slice::sort::shared::smallsort::sort8_stable::<(i32, i32, i32), <[(i32, i32, i32)]>::sort_unstable_by...` |
|   1.1 | `[kernel]` |
|   1.0 | `<core::hash::sip::Hasher<core::hash::sip::Sip13Rounds> as core::hash::Hasher>::write` |

### B07

| self % | function |
|---:|---|
|  27.6 | `<shmap::shmap::SHMapper<false, false, false>>::map_reads::{closure#0}::{closure#0}` |
|  27.2 | `<shmap::shmap::SHMapper<false, false, false>>::match_rest` |
|   8.7 | `core::slice::sort::unstable::quicksort::quicksort::<(i32, i32, i32), <[(i32, i32, i32)]>::sort_unstable_by_...` |
|   6.3 | `core::slice::sort::unstable::quicksort::quicksort::<u64, <u64 as core::cmp::PartialOrd>::lt>` |
|   4.4 | `core::slice::sort::unstable::quicksort::quicksort::<u32, <u32 as core::cmp::PartialOrd>::lt>` |
|   4.4 | `<shmap::buckets::Buckets<false>>::merge_entries` |
|   2.9 | `<shmap::buckets::Buckets<false>>::get_sorted_buckets` |
|   2.5 | `_mi_page_malloc_zero` |
|   1.5 | `<shmap::shmap::SHMapper<false, false, false>>::map_reads` |
|   1.3 | `core::slice::sort::shared::smallsort::sort8_stable::<(i32, i32, i32), <[(i32, i32, i32)]>::sort_unstable_by...` |
|   1.0 | `<core::hash::sip::Hasher<core::hash::sip::Sip13Rounds> as core::hash::Hasher>::write` |
|   1.0 | `__memcpy_generic` |

### B08

| self % | function |
|---:|---|
|  27.5 | `<shmap::shmap::SHMapper<false, false, false>>::map_reads::{closure#0}::{closure#0}` |
|  27.2 | `<shmap::shmap::SHMapper<false, false, false>>::match_rest` |
|   8.7 | `core::slice::sort::unstable::quicksort::quicksort::<(i32, i32, i32), <[(i32, i32, i32)]>::sort_unstable_by_...` |
|   6.3 | `core::slice::sort::unstable::quicksort::quicksort::<u64, <u64 as core::cmp::PartialOrd>::lt>` |
|   4.4 | `core::slice::sort::unstable::quicksort::quicksort::<u32, <u32 as core::cmp::PartialOrd>::lt>` |
|   4.3 | `<shmap::buckets::Buckets<false>>::merge_entries` |
|   2.9 | `<shmap::buckets::Buckets<false>>::get_sorted_buckets` |
|   2.5 | `_mi_page_malloc_zero` |
|   1.6 | `<shmap::shmap::SHMapper<false, false, false>>::map_reads` |
|   1.3 | `core::slice::sort::shared::smallsort::sort8_stable::<(i32, i32, i32), <[(i32, i32, i32)]>::sort_unstable_by...` |
|   1.0 | `<core::hash::sip::Hasher<core::hash::sip::Sip13Rounds> as core::hash::Hasher>::write` |
|   1.0 | `__memcpy_generic` |

### B09

| self % | function |
|---:|---|
|  29.3 | `<shmap::shmap::SHMapper<false, false, false>>::map_reads::{closure#0}::{closure#0}` |
|  23.6 | `<shmap::shmap::SHMapper<false, false, false>>::match_rest` |
|   7.1 | `core::slice::sort::unstable::quicksort::quicksort::<u64, <u64 as core::cmp::PartialOrd>::lt>` |
|   7.0 | `core::slice::sort::unstable::quicksort::quicksort::<(i32, i32, i32), <[(i32, i32, i32)]>::sort_unstable_by_...` |
|   5.0 | `core::slice::sort::unstable::quicksort::quicksort::<u32, <u32 as core::cmp::PartialOrd>::lt>` |
|   4.7 | `<shmap::buckets::Buckets<false>>::merge_entries` |
|   3.2 | `<shmap::buckets::Buckets<false>>::get_sorted_buckets` |
|   2.5 | `_mi_page_malloc_zero` |
|   1.8 | `<shmap::shmap::SHMapper<false, false, false>>::map_reads` |
|   1.1 | `<core::hash::sip::Hasher<core::hash::sip::Sip13Rounds> as core::hash::Hasher>::write` |
|   1.1 | `__memcpy_generic` |
|   1.0 | `core::slice::sort::shared::smallsort::sort8_stable::<(i32, i32, i32), <[(i32, i32, i32)]>::sort_unstable_by...` |
