//! NUMA topology detection and worker/index placement.
//!
//! Diagnosed in Q4 (see `QUESTIONS.md`, `RESULTS.md` §3/§11): every mapping
//! worker reads the *same* shared [`crate::index::SketchIndex`], so on a
//! multi-socket host most of them are doing every lookup across the
//! cross-socket interconnect. Per-read CPU cost inflates continuously with
//! thread count as a result — mildly within one socket, sharply once a run
//! needs a second one.
//!
//! The fix is one full copy of the index per NUMA node, each built by a
//! thread pinned to that node so its allocations land there under Linux's
//! default (local-allocation) memory policy, with every worker thread
//! pinned to the same node as the copy it reads.
//!
//! On a single-node host — the common case, and the only one `-@` was
//! validated on before this — [`detect_topology`] returns exactly one node,
//! and every caller in this crate treats `len() <= 1` (also `-@1`, via
//! [`plan_workers`]) as "skip replication, unchanged behavior": the exact
//! same code path as before this module existed. That is by construction,
//! not a special case bolted on afterward, specifically so this cannot
//! regress hardware it was never measured on (see `RESULTS.md` §11's
//! now-resolved "needs a single- or 2-socket host to confirm" caveat).

use std::fs;

/// One NUMA node's id and the CPU ids local to it, ascending.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NumaNode {
    pub id: usize,
    pub cpus: Vec<usize>,
}

const SYSFS_NODE_DIR: &str = "/sys/devices/system/node";

/// Parses a Linux sysfs-style id list (`"0-3,8,10-11"`) into individual ids.
///
/// Used for both `.../node/online` (node ids) and `.../nodeN/cpulist` (cpu
/// ids) — same format, same parser. Unrecognised tokens are skipped rather
/// than failing the whole parse: a partial, plausible topology is more
/// useful than none, and [`detect_topology`]'s own sanity checks catch the
/// case where skipping left nothing usable.
fn parse_range_list(s: &str) -> Vec<usize> {
    let mut out = Vec::new();
    for part in s.trim().split(',') {
        let part = part.trim();
        if part.is_empty() {
            continue;
        }
        match part.split_once('-') {
            Some((lo, hi)) => {
                if let (Ok(lo), Ok(hi)) = (lo.trim().parse::<usize>(), hi.trim().parse::<usize>())
                    && lo <= hi
                {
                    out.extend(lo..=hi);
                }
            }
            None => {
                if let Ok(v) = part.parse::<usize>() {
                    out.push(v);
                }
            }
        }
    }
    out
}

/// Reads a real topology from sysfs. `None` on anything unreadable,
/// unparseable, or structurally implausible (a node with no CPUs) — never
/// partial/wrong data, only "give up, let the caller fall back".
fn read_topology(base: &str) -> Option<Vec<NumaNode>> {
    let online = fs::read_to_string(format!("{base}/online")).ok()?;
    let node_ids = parse_range_list(&online);
    if node_ids.is_empty() {
        return None;
    }
    let mut nodes = Vec::with_capacity(node_ids.len());
    for id in node_ids {
        let cpulist = fs::read_to_string(format!("{base}/node{id}/cpulist")).ok()?;
        let cpus = parse_range_list(&cpulist);
        if cpus.is_empty() {
            return None;
        }
        nodes.push(NumaNode { id, cpus });
    }
    Some(nodes)
}

/// The machine's real NUMA topology, or a single fallback node if it can't
/// be determined (non-Linux, sandboxed, single-node hardware, or anything
/// else that makes `/sys/devices/system/node` absent or unreadable). The
/// fallback's `cpus` is deliberately left empty: nothing should ever
/// address it, since every caller checks `len() <= 1` first.
pub fn detect_topology() -> Vec<NumaNode> {
    read_topology(SYSFS_NODE_DIR).unwrap_or_else(|| {
        vec![NumaNode {
            id: 0,
            cpus: Vec::new(),
        }]
    })
}

/// How many of `n_threads` workers to place on each node, packing densely
/// into as *few* nodes as fit rather than spreading evenly across every
/// one — each node used costs a full index clone, so touching a node
/// `-@` didn't need to is pure waste, not a hedge. A first version of this
/// spread threads evenly across every available node regardless of size;
/// measured live (a real benchmark run, not a synthetic one), that made
/// `-@16` on this 4x16-core host build four replicas for a workload that
/// fits entirely on *one* socket, and made `-@64` slower than `-@32` —
/// the clone cost was outrunning the locality win it was supposed to pay
/// for. Packing means `-@16` here now touches exactly one node (one
/// clone, matching the confinement Q4's own `numactl
/// --cpunodebind=0 --membind=0 -@16` experiment already proved out),
/// and only `-@` values that genuinely need a second socket pay for one.
///
/// Returns an empty plan (meaning: don't replicate, use the single shared
/// index exactly as before) only when there is nothing to gain at all: a
/// single NUMA node, or a single worker thread.
pub fn plan_workers(n_threads: usize, nodes: &[NumaNode]) -> Vec<(&NumaNode, usize)> {
    if nodes.len() <= 1 || n_threads <= 1 {
        return Vec::new();
    }
    let mut plan: Vec<(&NumaNode, usize)> = Vec::new();
    let mut remaining = n_threads;
    for node in nodes {
        if remaining == 0 {
            break;
        }
        let take = remaining.min(node.cpus.len());
        plan.push((node, take));
        remaining -= take;
    }
    // More threads than total cores across every node `detect_topology`
    // found (unusual, but `-@` isn't otherwise capped at the core count):
    // spread the leftover round-robin across the nodes already in the
    // plan rather than pretending more nodes exist.
    let mut i = 0;
    while remaining > 0 {
        let n = plan.len();
        plan[i % n].1 += 1;
        remaining -= 1;
        i += 1;
    }
    plan
}

/// Which replica (index into a `plan_workers` result, in the same order),
/// which NUMA node id (for [`bind_current_thread_memory`]) and which CPU
/// (for [`pin_current_thread`]) one worker thread should use.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WorkerSlot {
    pub replica: usize,
    pub node_id: usize,
    pub cpu: usize,
}

/// Expands a `plan_workers` result into one slot per worker, in worker
/// order (slot `i` is worker `i`'s placement). Cores are assigned
/// round-robin within a node, wrapping if a node is asked to host more
/// workers than it has cores — oversubscribed, like running without any
/// placement at all, never a panic.
pub fn worker_slots(plan: &[(&NumaNode, usize)]) -> Vec<WorkerSlot> {
    let mut out = Vec::new();
    for (replica, (node, count)) in plan.iter().enumerate() {
        for i in 0..*count {
            out.push(WorkerSlot {
                replica,
                node_id: node.id,
                cpu: node.cpus[i % node.cpus.len()],
            });
        }
    }
    out
}

/// Best-effort: pins the calling thread to one CPU. Failure (unsupported
/// platform, a cpu id the OS rejects) just means that thread keeps today's
/// unpinned behavior — never fatal, since correctness never depends on
/// placement, only speed.
pub fn pin_current_thread(cpu: usize) {
    core_affinity::set_for_current(core_affinity::CoreId { id: cpu });
}

/// Best-effort: binds every allocation the calling thread makes from now
/// on to one NUMA node, via `set_mempolicy(2)`/`MPOL_BIND`.
///
/// [`pin_current_thread`] alone was tried first and measured live (real
/// benchmark runs) to not be enough on this host: `cat
/// /proc/sys/kernel/numa_balancing` is `1` here, and the kernel's automatic
/// balancing migrates pages between nodes based on its own runtime
/// heuristics regardless of which pinned thread first wrote them —
/// confirmed directly via `/proc/<pid>/numa_maps`, sampled mid-run, which
/// showed a clone's pages landing scattered across every node even though
/// the thread that wrote them was pinned to one. `MPOL_BIND` pages are
/// exempt from that migration (the kernel treats an explicit mempolicy as
/// authoritative), which plain affinity-plus-first-touch is not. This is
/// what `numactl --membind=N` itself uses under the hood.
///
/// Not exposed by the `libc` crate as a named wrapper (`SYS_set_mempolicy`
/// isn't one of its constants), so this calls the syscall number directly
/// — 238, x86_64's ABI-fixed value, not expected to change. A no-op on any
/// other architecture: correctness never depends on this succeeding, only
/// speed, and a wrong syscall number on an untested arch is a strictly
/// worse failure mode than "this optimization silently didn't apply
/// there".
#[cfg(target_arch = "x86_64")]
pub fn bind_current_thread_memory(node: usize) {
    const SYS_SET_MEMPOLICY: i64 = 238;
    const MPOL_BIND: i64 = 2;
    let nodemask: u64 = 1u64 << node;
    // SAFETY: `nodemask` is a valid, live, correctly-sized bitmask for
    // `set_mempolicy`'s nodemask argument for the duration of this call
    // (see `set_mempolicy(2)`); `maxnode` (64) matches the bitmask's
    // width in bits. The call has no effect on Rust-level memory safety
    // either way — it changes a kernel-side allocation policy for this
    // thread, not this process's address space — so failure (ignored
    // return value) degrades to "no pinning", never undefined behavior.
    unsafe {
        libc::syscall(SYS_SET_MEMPOLICY, MPOL_BIND, &nodemask as *const u64, 64u64);
    }
}

#[cfg(not(target_arch = "x86_64"))]
pub fn bind_current_thread_memory(_node: usize) {}

// A fifth thing was tried and measured here before the current design:
// `mbind(2)`/`MPOL_MF_MOVE` to actively relocate a buffer's pages after
// the fact, for the cases `bind_current_thread_memory` alone wasn't
// enough for (see `crate::index::SketchIndex::clone_pinned`'s doc
// comment for why it wasn't). It measured as a further net regression —
// migrating an already-misplaced buffer costs a real page-by-page copy,
// scaling with exactly how bad the placement was — and is gone from this
// file. [`crate::numa_storage`] is what actually worked: bypass the
// allocator that was causing the misplacement in the first place, rather
// than trying to correct for it afterward.

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_mixed_ranges_and_singletons() {
        assert_eq!(parse_range_list("0-3,8,10-11"), vec![0, 1, 2, 3, 8, 10, 11]);
    }

    #[test]
    fn parses_single_id() {
        assert_eq!(parse_range_list("0"), vec![0]);
    }

    #[test]
    fn parses_single_range() {
        assert_eq!(parse_range_list("0-15"), (0..=15).collect::<Vec<_>>());
    }

    #[test]
    fn skips_garbage_tokens_rather_than_failing() {
        assert_eq!(parse_range_list("0-3,,garbage,8"), vec![0, 1, 2, 3, 8]);
    }

    #[test]
    fn empty_string_parses_empty() {
        assert_eq!(parse_range_list(""), Vec::<usize>::new());
    }

    fn nodes(spec: &[(usize, std::ops::RangeInclusive<usize>)]) -> Vec<NumaNode> {
        spec.iter()
            .map(|(id, range)| NumaNode {
                id: *id,
                cpus: range.clone().collect(),
            })
            .collect()
    }

    #[test]
    fn single_node_never_plans_replication() {
        let n = nodes(&[(0, 0..=63)]);
        assert!(plan_workers(64, &n).is_empty());
        assert!(plan_workers(1, &n).is_empty());
    }

    #[test]
    fn single_worker_never_plans_replication_even_multi_node() {
        let n = nodes(&[(0, 0..=15), (1, 16..=31), (2, 32..=47), (3, 48..=63)]);
        assert!(plan_workers(1, &n).is_empty());
    }

    #[test]
    fn four_nodes_exactly_saturated_uses_all_four() {
        let n = nodes(&[(0, 0..=15), (1, 16..=31), (2, 32..=47), (3, 48..=63)]);
        let plan = plan_workers(64, &n);
        assert_eq!(plan.len(), 4);
        for (_, count) in &plan {
            assert_eq!(*count, 16);
        }
        assert_eq!(plan.iter().map(|(_, c)| c).sum::<usize>(), 64);
    }

    #[test]
    fn packs_densely_leftover_spills_to_the_next_node_only() {
        // 18 threads on 16-core nodes needs a second node for the last 2 -
        // and only the second, not all four, which is the whole point:
        // each node touched costs a full index clone.
        let n = nodes(&[(0, 0..=15), (1, 16..=31), (2, 32..=47), (3, 48..=63)]);
        let plan = plan_workers(18, &n);
        assert_eq!(plan.iter().map(|(_, c)| *c).collect::<Vec<_>>(), vec![16, 2]);
        assert_eq!(plan.iter().map(|(_, c)| c).sum::<usize>(), 18);
    }

    #[test]
    fn fits_on_one_node_uses_only_one_node() {
        // The regression this test guards against was measured live: an
        // earlier version spread even a small `-@` across every available
        // node (min(nodes.len(), n_threads)), so `-@16` on this host built
        // four replicas for a workload that fits on one 16-core socket,
        // and the clone cost made `-@64` slower than `-@32`.
        let n = nodes(&[(0, 0..=15), (1, 16..=31), (2, 32..=47), (3, 48..=63)]);
        let plan = plan_workers(3, &n);
        assert_eq!(plan.len(), 1);
        assert_eq!(plan[0].1, 3);

        let plan16 = plan_workers(16, &n);
        assert_eq!(plan16.len(), 1);
        assert_eq!(plan16[0].1, 16);
    }

    #[test]
    fn oversubscription_spreads_leftover_round_robin_over_nodes_in_use() {
        let n = nodes(&[(0, 0..=1), (1, 2..=3)]); // 2 nodes x 2 cores = 4 total
        let plan = plan_workers(7, &n);
        assert_eq!(plan.iter().map(|(_, c)| *c).collect::<Vec<_>>(), vec![4, 3]);
        assert_eq!(plan.iter().map(|(_, c)| c).sum::<usize>(), 7);
    }

    #[test]
    fn worker_slots_cover_every_worker_exactly_once_in_order() {
        let n = nodes(&[(0, 0..=1), (1, 2..=3)]);
        let plan = plan_workers(4, &n);
        let slots = worker_slots(&plan);
        assert_eq!(slots.len(), 4);
        assert_eq!(
            slots[0],
            WorkerSlot {
                replica: 0,
                node_id: 0,
                cpu: 0
            }
        );
        assert_eq!(
            slots[1],
            WorkerSlot {
                replica: 0,
                node_id: 0,
                cpu: 1
            }
        );
        assert_eq!(
            slots[2],
            WorkerSlot {
                replica: 1,
                node_id: 1,
                cpu: 2
            }
        );
        assert_eq!(
            slots[3],
            WorkerSlot {
                replica: 1,
                node_id: 1,
                cpu: 3
            }
        );
    }

    #[test]
    fn worker_slots_wrap_cores_when_oversubscribed() {
        let n = nodes(&[(0, 0..=1)]);
        // A single-node plan can't happen via `plan_workers`, but
        // `worker_slots` itself must still degrade safely if ever called
        // directly with a node asked to host more workers than it has
        // cores.
        let plan = vec![(&n[0], 5usize)];
        let slots = worker_slots(&plan);
        assert_eq!(slots.iter().map(|s| s.cpu).collect::<Vec<_>>(), vec![0, 1, 0, 1, 0]);
    }

    #[test]
    fn real_topology_detection_never_returns_zero_nodes() {
        // Whatever this host actually is (single-node CI runner or a real
        // multi-socket box), detection must always yield something callers
        // can treat uniformly via `len() <= 1`.
        assert!(!detect_topology().is_empty());
    }
}
