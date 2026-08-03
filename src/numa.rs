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

/// How many of `n_threads` workers to place on each node, most-even split,
/// using only as many nodes as there are threads to place — so a small
/// `-@` on a big multi-socket host doesn't pay to build replicas nothing
/// reads. Returns an empty plan (meaning: don't replicate, use the single
/// shared index exactly as before) whenever there's nothing to gain: a
/// single node, or a single worker thread.
pub fn plan_workers(n_threads: usize, nodes: &[NumaNode]) -> Vec<(&NumaNode, usize)> {
    if nodes.len() <= 1 || n_threads <= 1 {
        return Vec::new();
    }
    let n_nodes = nodes.len().min(n_threads);
    let base = n_threads / n_nodes;
    let extra = n_threads % n_nodes;
    nodes
        .iter()
        .take(n_nodes)
        .enumerate()
        .map(|(i, node)| (node, base + usize::from(i < extra)))
        .collect()
}

/// Which replica (index into a `plan_workers` result, in the same order)
/// and which CPU one worker thread should use.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WorkerSlot {
    pub replica: usize,
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
    fn four_nodes_even_split() {
        let n = nodes(&[(0, 0..=15), (1, 16..=31), (2, 32..=47), (3, 48..=63)]);
        let plan = plan_workers(64, &n);
        assert_eq!(plan.len(), 4);
        for (_, count) in &plan {
            assert_eq!(*count, 16);
        }
        assert_eq!(plan.iter().map(|(_, c)| c).sum::<usize>(), 64);
    }

    #[test]
    fn four_nodes_uneven_split_distributes_remainder() {
        let n = nodes(&[(0, 0..=15), (1, 16..=31), (2, 32..=47), (3, 48..=63)]);
        let plan = plan_workers(18, &n);
        assert_eq!(plan.len(), 4);
        assert_eq!(plan.iter().map(|(_, c)| *c).collect::<Vec<_>>(), vec![5, 5, 4, 4]);
        assert_eq!(plan.iter().map(|(_, c)| c).sum::<usize>(), 18);
    }

    #[test]
    fn fewer_threads_than_nodes_only_uses_as_many_nodes_as_needed() {
        let n = nodes(&[(0, 0..=15), (1, 16..=31), (2, 32..=47), (3, 48..=63)]);
        let plan = plan_workers(3, &n);
        assert_eq!(plan.len(), 3);
        for (_, count) in &plan {
            assert_eq!(*count, 1);
        }
    }

    #[test]
    fn worker_slots_cover_every_worker_exactly_once_in_order() {
        let n = nodes(&[(0, 0..=1), (1, 2..=3)]);
        let plan = plan_workers(4, &n);
        let slots = worker_slots(&plan);
        assert_eq!(slots.len(), 4);
        assert_eq!(slots[0], WorkerSlot { replica: 0, cpu: 0 });
        assert_eq!(slots[1], WorkerSlot { replica: 0, cpu: 1 });
        assert_eq!(slots[2], WorkerSlot { replica: 1, cpu: 2 });
        assert_eq!(slots[3], WorkerSlot { replica: 1, cpu: 3 });
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
