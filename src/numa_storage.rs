//! Raw, `mmap`-backed storage for NUMA index replicas.
//!
//! Diagnosed across the four prior attempts recorded in
//! [`crate::index::SketchIndex::clone_pinned`]'s doc comment: `set_mempolicy`
//! and even `mbind`/`MPOL_MF_MOVE` measured live as not enough, because
//! mimalloc (this crate's global allocator, see `main.rs`) eagerly commits
//! and reuses large arena pages that an earlier, unrelated, unpinned thread
//! (typically one of `build_index`'s own indexing workers) already touched
//! — so a "fresh" `Vec`/`HashMap` allocation from a pinned+bound thread can
//! still land on pages that were never actually fresh.
//!
//! The only way to be sure a page's first touch is real is to never go
//! through mimalloc for it at all: [`NumaBuffer`] gets its memory straight
//! from the kernel via `mmap(2)`, which Linux always demand-pages (no
//! eager commit, no arena reuse — every page is genuinely untouched until
//! this buffer's own constructor writes it). Combined with
//! [`crate::numa::bind_current_thread_memory`] (still needed: Linux's
//! automatic NUMA balancing can migrate *any* anonymous memory later,
//! regardless of how it was allocated, unless it carries an explicit
//! policy), this is the version that actually keeps a replica's data on
//! the node it was built for.
//!
//! [`RawHashTable`] exists because that fix doesn't reach `HashMap`:
//! there is no safe way to hand a `std`/`hashbrown` table a caller-owned
//! backing buffer, and reading its private internals to `mbind` them
//! after the fact isn't a supported or stable thing to depend on. It's a
//! plain open-addressing table over [`NumaBuffer`]s instead — built once
//! from an already-finalised source and never mutated afterward, so there
//! is no need for tombstones, resizing, or any of the complexity a
//! general-purpose hash table carries for a mutable workload.

use std::mem::size_of;
use std::os::raw::c_void;
use std::ptr::NonNull;

use crate::types::Hash;

/// A fixed-size, `mmap`-backed buffer of `T`, read as a slice via `Deref`.
///
/// `T` must be `Copy` (this type never runs a destructor on its elements —
/// see [`Self::zeroed`]'s safety note) and must be valid with every byte
/// zero, since [`Self::zeroed`] never explicitly initialises anything.
/// Every use in this crate is a plain-old-data type (an unsigned integer,
/// a tuple of them, or a struct of only such fields), so this holds.
pub struct NumaBuffer<T> {
    ptr: NonNull<T>,
    len: usize,
}

// SAFETY: a `NumaBuffer<T>` owns its `mmap`-backed memory exclusively (no
// other handle to it exists), so it can be sent across threads exactly
// like a `Vec<T>` can, under the same `T: Send`/`T: Sync` conditions.
unsafe impl<T: Send> Send for NumaBuffer<T> {}
unsafe impl<T: Sync> Sync for NumaBuffer<T> {}

/// `MAP_POPULATE` (pre-faulting every page during the `mmap` call itself,
/// rather than one minor fault per page on first write) was tried and
/// measured live to be a net loss, not a win: `RawHashTable`'s value
/// buffer is deliberately over-provisioned (~50% load factor for short
/// probe chains — see `RawHashTable::build`), so roughly half of it is
/// never actually read or written. Left lazy, those pages simply never
/// get faulted at all (the kernel backs unwritten anonymous memory with a
/// shared zero page essentially for free); `MAP_POPULATE` forced them
/// all to be materialised up front regardless, which cost more than it
/// saved on the pages that *were* used.
fn mmap_anon(bytes: usize) -> NonNull<u8> {
    // SAFETY: a fixed, valid set of arguments requesting a fresh
    // private/anonymous mapping — no file descriptor, no caller-supplied
    // pointer, nothing that depends on this call's arguments being
    // checked by the caller beyond `bytes > 0` (guaranteed by every
    // constructor below). `mmap` failing (returning `MAP_FAILED`) is
    // handled explicitly, not assumed away.
    let ptr = unsafe {
        libc::mmap(
            std::ptr::null_mut(),
            bytes,
            libc::PROT_READ | libc::PROT_WRITE,
            libc::MAP_PRIVATE | libc::MAP_ANONYMOUS,
            -1,
            0,
        )
    };
    assert!(
        ptr != libc::MAP_FAILED,
        "mmap({bytes} bytes) failed: {}",
        std::io::Error::last_os_error()
    );
    // SAFETY: just checked non-null/non-failure above.
    unsafe { NonNull::new_unchecked(ptr as *mut u8) }
}

impl<T: Copy> NumaBuffer<T> {
    /// `len` elements of zeroed memory, freshly `mmap`'d (see the module
    /// doc comment for why that matters), with the calling thread's
    /// allocations bound to `node` before any of it is touched.
    ///
    /// Safe only because every `T` this crate instantiates this with has
    /// an all-zero bit pattern as a valid value (an integer, a tuple of
    /// them, or [`crate::types::Hit`], all plain fields with no niches) —
    /// this never writes anything, so a `T` with an invalid all-zero
    /// representation would make every unwritten element instant UB.
    pub fn zeroed(len: usize, node: usize) -> Self {
        // Binding *before* mmap, not after: MAP_POPULATE (see
        // `mmap_anon`) faults every page in synchronously as part of the
        // mmap(2) call itself, so the policy has to already be in effect
        // when that call happens, not merely by the time this buffer's
        // own writes happen later.
        crate::numa::bind_current_thread_memory(node);
        let bytes = len.max(1) * size_of::<T>();
        let ptr = mmap_anon(bytes);
        NumaBuffer { ptr: ptr.cast(), len }
    }

    /// `len` elements, each set to `value`, freshly `mmap`'d and bound to
    /// `node` before the fill writes happen.
    pub fn filled(len: usize, value: T, node: usize) -> Self {
        let mut buf = Self::zeroed(len, node);
        buf.as_mut_slice().fill(value);
        buf
    }

    /// A copy of `src`, freshly `mmap`'d and bound to `node` before the
    /// copy happens.
    pub fn from_slice(src: &[T], node: usize) -> Self {
        let mut buf = Self::zeroed(src.len(), node);
        buf.as_mut_slice().copy_from_slice(src);
        buf
    }

    fn as_mut_slice(&mut self) -> &mut [T] {
        // SAFETY: `self.ptr` was `mmap`'d for exactly `self.len` elements
        // of `T` and is exclusively owned by `self` (no aliasing handle
        // exists, since this takes `&mut self`); every element is a valid
        // `T` per this impl block's safety note above.
        unsafe { std::slice::from_raw_parts_mut(self.ptr.as_ptr(), self.len) }
    }
}

impl<T> std::ops::Deref for NumaBuffer<T> {
    type Target = [T];
    fn deref(&self) -> &[T] {
        // SAFETY: same as `as_mut_slice`, minus exclusivity — a shared
        // slice only needs the memory to be valid and not concurrently
        // mutated, which holds since nothing mutates a `NumaBuffer` after
        // construction (no `DerefMut` is exposed).
        unsafe { std::slice::from_raw_parts(self.ptr.as_ptr(), self.len) }
    }
}

impl<T> Drop for NumaBuffer<T> {
    fn drop(&mut self) {
        let bytes = self.len.max(1) * size_of::<T>();
        // SAFETY: `self.ptr` was obtained from `mmap` with exactly this
        // many bytes (see every constructor above) and is not used again
        // after this call, since `self` is being dropped.
        unsafe {
            libc::munmap(self.ptr.as_ptr() as *mut c_void, bytes);
        }
    }
}

/// Sentinel marking an empty slot in [`RawHashTable`]'s key array.
/// `FracMinHash` only keeps a k-mer whose hash is under `h_frac *
/// u64::MAX` (see `sketch.rs`), and every real `-r` used in practice is
/// far below `1.0`, so `u64::MAX` itself is never a real k-mer hash here.
/// `debug_assert_ne!` in [`RawHashTable::build`] catches it directly if
/// that ever stops holding, rather than this silently misreporting a
/// lookup as absent.
const EMPTY: Hash = Hash::MAX;

/// A read-only, open-addressing hash table over [`NumaBuffer`]s: linear
/// probing, no tombstones, built once from a fully-known set of entries
/// and never mutated again. See the module doc comment for why this
/// exists instead of a `HashMap`.
pub struct RawHashTable<V> {
    keys: NumaBuffer<Hash>,
    values: NumaBuffer<V>,
    mask: usize,
}

impl<V: Copy> RawHashTable<V> {
    /// Builds a table holding exactly `entries`, sized for a ~50% load
    /// factor (the usual sweet spot for linear probing: full enough not to
    /// waste memory, sparse enough to keep probe chains short) and bound
    /// to `node`.
    pub fn build(entries: impl ExactSizeIterator<Item = (Hash, V)>, node: usize) -> Self {
        let n = entries.len();
        let capacity = (n.max(1) * 2).next_power_of_two();
        let mask = capacity - 1;
        let mut keys = NumaBuffer::filled(capacity, EMPTY, node);
        let mut values = NumaBuffer::<V>::zeroed(capacity, node);
        for (h, v) in entries {
            debug_assert_ne!(h, EMPTY, "a real hash collided with the empty-slot sentinel");
            let mut idx = (h as usize) & mask;
            while keys.as_mut_slice()[idx] != EMPTY {
                idx = (idx + 1) & mask;
            }
            keys.as_mut_slice()[idx] = h;
            values.as_mut_slice()[idx] = v;
        }
        RawHashTable { keys, values, mask }
    }

    #[inline]
    pub fn get(&self, h: Hash) -> Option<&V> {
        let mut idx = (h as usize) & self.mask;
        loop {
            let k = self.keys[idx];
            if k == h {
                return Some(&self.values[idx]);
            }
            if k == EMPTY {
                return None;
            }
            idx = (idx + 1) & self.mask;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn numa_buffer_from_slice_round_trips() {
        let src = [1u64, 2, 3, 4, 5];
        let buf = NumaBuffer::from_slice(&src, 0);
        assert_eq!(&*buf, &src[..]);
    }

    #[test]
    fn numa_buffer_zeroed_is_all_zero() {
        let buf: NumaBuffer<u64> = NumaBuffer::zeroed(10, 0);
        assert!(buf.iter().all(|&x| x == 0));
    }

    #[test]
    fn numa_buffer_filled_matches_value() {
        let buf = NumaBuffer::filled(7, 42u32, 0);
        assert!(buf.iter().all(|&x| x == 42));
    }

    #[test]
    fn raw_hash_table_finds_every_inserted_key() {
        let entries: Vec<(Hash, u32)> = (0..1000).map(|i| (i as Hash * 7 + 1, i)).collect();
        let table = RawHashTable::build(entries.iter().copied(), 0);
        for &(h, v) in &entries {
            assert_eq!(table.get(h), Some(&v));
        }
    }

    #[test]
    fn raw_hash_table_missing_key_is_none() {
        let entries: Vec<(Hash, u32)> = vec![(10, 1), (20, 2), (30, 3)];
        let table = RawHashTable::build(entries.into_iter(), 0);
        assert_eq!(table.get(999), None);
    }

    #[test]
    fn raw_hash_table_matches_a_real_hashmap_on_random_keys() {
        use rustc_hash::FxHashMap;
        // A simple xorshift so this test has no extra dev-dependency.
        let mut seed: u64 = 0x1234_5678_9abc_def0;
        let mut next = || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        let mut reference = FxHashMap::default();
        for i in 0..5000u32 {
            let h = next() % (1u64 << 56); // stays well under EMPTY
            reference.insert(h, i);
        }
        let table = RawHashTable::build(reference.iter().map(|(&h, &v)| (h, v)), 0);
        for (&h, &v) in &reference {
            assert_eq!(table.get(h), Some(&v));
        }
        // A handful of probe misses should also agree with the reference.
        for probe in [1u64, 2, 999_999_999, u64::MAX / 2] {
            assert_eq!(table.get(probe), reference.get(&probe));
        }
    }

    #[test]
    fn raw_hash_table_handles_empty_input() {
        let table: RawHashTable<u32> = RawHashTable::build(std::iter::empty(), 0);
        assert_eq!(table.get(42), None);
    }
}
