//! Timing/counting instrumentation and small parsing helpers.
//!
//! Port of `shmap/src/utils.h`.
//!
//! The C++ `ASSERT` macro compiles to a no-op in release (`-DNDEBUG`) builds
//! — which is what the shipped/benchmarked `shmap` binary actually uses —
//! so the pervasive internal invariant checks scattered through the hot
//! path (bucket/refine/seeding code) are ported as `debug_assert!`, matching
//! that same "on in dev, off in release" tradeoff. The handful of checks
//! that are actually exercised by `test_shmap.cpp`'s `CHECK_THROWS` (this
//! module's own `Counters`/`Timer`/`Timers` guards) are ported as
//! unconditional `assert!`/`panic!` instead, since those need to fire
//! regardless of build profile for the ported tests to mean anything.

use std::collections::HashMap;
use std::fmt;
use std::io::Write;
use std::ops::AddAssign;
use std::time::Instant;

use crate::types::RPos;

#[derive(Clone)]
pub struct Timer {
    start_time: Option<Instant>,
    accumulated: f64,
    min: f64,
    max: f64,
    running: bool,
}

impl Default for Timer {
    fn default() -> Self {
        Timer {
            start_time: None,
            accumulated: 0.0,
            min: 1e9,
            max: -1.0,
            running: false,
        }
    }
}

impl Timer {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn start(&mut self) {
        assert!(
            !self.running,
            "Timer cannot be started since it is already running."
        );
        self.start_time = Some(Instant::now());
        self.running = true;
    }

    fn update_range(&mut self, diff: f64) {
        if diff < self.min {
            self.min = diff;
        }
        if diff > self.max {
            self.max = diff;
        }
    }

    pub fn stop(&mut self) {
        assert!(self.running, "Timer cannot be stopped since it is not running.");
        let diff = self.start_time.expect("running timer has a start time").elapsed().as_secs_f64();
        self.accumulated += diff;
        self.running = false;
        self.update_range(diff);
    }

    /// Elapsed seconds. If the timer is currently running, returns the
    /// partial elapsed time without stopping it.
    pub fn secs(&self) -> f64 {
        if self.running {
            return self
                .start_time
                .expect("running timer has a start time")
                .elapsed()
                .as_secs_f64();
        }
        self.accumulated
    }

    /// Ratio of the longest to the shortest recorded interval.
    pub fn range_ratio(&self) -> f64 {
        assert!(!self.running, "Timer is still running.");
        assert!(
            self.max >= 0.0,
            "There is no range ratio since the timer has not been run."
        );
        self.max / self.min
    }
}

impl AddAssign<&Timer> for Timer {
    /// Merges `other`'s *total accumulated time* in as a single new sample
    /// point for `min`/`max` — this mirrors the C++ `Timer::operator+=`
    /// exactly (it is not a "merge two timers' histories" operation). It's
    /// how per-read timers get folded into the run-wide `Handler` timers,
    /// giving `range_ratio()` a min/max over per-read totals.
    fn add_assign(&mut self, other: &Timer) {
        self.accumulated += other.accumulated;
        self.update_range(other.accumulated);
    }
}

#[derive(Clone, Default)]
pub struct Timers {
    timers: HashMap<String, Timer>,
}

impl Timers {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn init(&mut self, names: &[&str]) {
        for name in names {
            self.timers.insert((*name).to_string(), Timer::new());
        }
    }

    pub fn start(&mut self, name: &str) {
        self.timers.entry(name.to_string()).or_default().start();
    }

    /// Silently does nothing if `name` was never started, matching the C++
    /// (`stop` looks the timer up and only calls `.stop()` if found).
    pub fn stop(&mut self, name: &str) {
        if let Some(t) = self.timers.get_mut(name) {
            t.stop();
        }
    }

    /// Returns `0.0` (and logs to stderr) if `name` isn't a registered
    /// timer, rather than propagating an error — matching the *intended*
    /// behavior of the C++'s try/catch here (which in practice never
    /// actually catches anything, since `unordered_map::at`'s
    /// `std::out_of_range` isn't a `std::runtime_error`).
    pub fn secs(&self, name: &str) -> f64 {
        match self.timers.get(name) {
            Some(t) => t.secs(),
            None => {
                eprintln!("Error with timer \"{name}\": not found");
                0.0
            }
        }
    }

    /// Panics if `name` was never registered (matches the C++'s
    /// unconditional throw on a missing name); returns `0.0` if it was
    /// registered but never actually run (matches the range-ratio-specific
    /// error being caught and turned into a graceful `0.0`).
    pub fn range_ratio(&self, name: &str) -> f64 {
        let timer = self
            .timers
            .get(name)
            .unwrap_or_else(|| panic!("Timer {name} not found."));
        if timer.running || timer.max < 0.0 {
            eprintln!("Error with timer \"{name}\": no range ratio available");
            return 0.0;
        }
        timer.range_ratio()
    }

    /// Returns `0.0` if either name is missing, rather than asserting —
    /// matches the shipped (assertions-disabled) release build's actual
    /// behavior, which is the only behavior ever exercised in practice.
    pub fn perc(&self, name: &str, total: &str) -> f64 {
        match (self.timers.get(name), self.timers.get(total)) {
            (Some(a), Some(b)) => a.secs() / b.secs() * 100.0,
            _ => 0.0,
        }
    }

    pub fn clear(&mut self) {
        self.timers.clear();
    }

    /// Every registered timer's name and accumulated seconds (a running
    /// timer's partial elapsed time is included, matching [`Timer::secs`]).
    /// Used by [`crate::profiling`] to serialize a whole `Timers` set
    /// without needing to know the names in advance.
    pub fn iter_secs(&self) -> impl Iterator<Item = (&str, f64)> + '_ {
        self.timers.iter().map(|(name, t)| (name.as_str(), t.secs()))
    }
}

impl AddAssign<&Timers> for Timers {
    fn add_assign(&mut self, other: &Timers) {
        for (name, timer) in other.timers.iter() {
            *self.timers.entry(name.clone()).or_default() += timer;
        }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord)]
pub struct Counter(i64);

impl Counter {
    pub fn new(value: i64) -> Self {
        Counter(value)
    }

    pub fn inc(&mut self, value: i64) {
        self.0 += value;
    }

    pub fn count(&self) -> i64 {
        self.0
    }
}

impl AddAssign<i64> for Counter {
    fn add_assign(&mut self, value: i64) {
        self.0 += value;
    }
}

impl AddAssign for Counter {
    fn add_assign(&mut self, other: Counter) {
        self.0 += other.0;
    }
}

#[derive(Clone, Default)]
pub struct Counters {
    counters: HashMap<String, Counter>,
}

impl Counters {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn init(&mut self, names: &[&str]) {
        for name in names {
            self.counters.insert((*name).to_string(), Counter::default());
        }
    }

    pub fn inc(&mut self, name: &str, value: i64) {
        *self.counters.entry(name.to_string()).or_default() += value;
    }

    /// Increments `name` by 1 — the common case of C++'s `inc(name)` with
    /// its default `value = 1` argument.
    pub fn inc1(&mut self, name: &str) {
        self.inc(name, 1);
    }

    /// Panics if `name` isn't a registered counter, matching
    /// `CHECK_THROWS(C.count("c1"))` in the ported test suite.
    pub fn count(&self, name: &str) -> i64 {
        self.counters
            .get(name)
            .unwrap_or_else(|| panic!("Counter \"{name}\" not found."))
            .count()
    }

    pub fn frac(&self, name: &str, total: &str) -> f64 {
        self.count(name) as f64 / self.count(total) as f64
    }

    pub fn perc(&self, name: &str, total: &str) -> f64 {
        100.0 * self.frac(name, total)
    }

    pub fn clear(&mut self) {
        self.counters.clear();
    }

    /// Every registered counter's name and value. Used by
    /// [`crate::profiling`] to serialize a whole `Counters` set without
    /// needing to know the names in advance.
    pub fn iter_counts(&self) -> impl Iterator<Item = (&str, i64)> + '_ {
        self.counters.iter().map(|(name, c)| (name.as_str(), c.count()))
    }

    /// `counters[name] = max(counters[name], value)`, auto-vivifying
    /// `name` to 0 first if it isn't present yet — the one place upstream
    /// uses `Counters::operator[]` for both read and write in the same
    /// expression (`C["max_seed_matches"] = max(C["max_seed_matches"], ...)`
    /// in `match_seeds`).
    pub fn update_max(&mut self, name: &str, value: i64) {
        let entry = self.counters.entry(name.to_string()).or_default();
        if value > entry.count() {
            *entry = Counter::new(value);
        }
    }
}

impl AddAssign<&Counters> for Counters {
    fn add_assign(&mut self, other: &Counters) {
        for (name, counter) in other.counters.iter() {
            *self.counters.entry(name.clone()).or_default() += *counter;
        }
    }
}

pub struct ProgressBar {
    message: String,
}

impl ProgressBar {
    const WIDTH: usize = 60;

    pub fn new(message: impl Into<String>) -> Self {
        ProgressBar {
            message: message.into(),
        }
    }

    /// `progress` in `[0, 1]`. Always writes to stderr, matching every
    /// actual call site upstream (the C++ `ostream&` parameter defaults to
    /// `cerr` and is never overridden).
    pub fn update(&self, progress: f64) {
        let val = (progress * 100.0) as i64;
        let lpad = ((progress * Self::WIDTH as f64) as usize).min(Self::WIDTH);
        let rpad = Self::WIDTH - lpad;
        eprint!(
            "\r{} {val:>3}% [{}{}]",
            self.message,
            "|".repeat(lpad),
            " ".repeat(rpad)
        );
        let _ = std::io::stderr().flush();
    }
}

/// A read name encoding simulated-read ground truth, e.g.
/// `"S1_21!NC_060948.1!57693539!57715501!+"`.
#[derive(Clone, Debug)]
pub struct ParsedQueryId {
    pub segm_id: String,
    pub start_pos: RPos,
    pub end_pos: RPos,
    pub strand: char,
}

impl ParsedQueryId {
    /// Returns `None` if `query_id` doesn't look like a ground-truth-encoded
    /// name (no `!` delimiter, or not exactly 5 `!`-separated fields) —
    /// these are the two cases the C++ handles gracefully. A malformed
    /// numeric field is a harder error in both: the C++ catches the parse
    /// exception only to immediately rethrow it, so this panics too rather
    /// than silently returning `None`.
    pub fn parse(query_id: &str) -> Option<Self> {
        if !query_id.contains('!') {
            return None;
        }
        let parts: Vec<&str> = query_id.split('!').collect();
        if parts.len() != 5 {
            return None;
        }
        let segm_id = parts[1].to_string();
        let start_pos: RPos = parts[2].parse().unwrap_or_else(|e| {
            panic!(
                "Error parsing query_id with start_pos {} and end_pos {}: {}",
                parts[2], parts[3], e
            )
        });
        let end_pos: RPos = parts[3].parse().unwrap_or_else(|e| {
            panic!(
                "Error parsing query_id with start_pos {} and end_pos {}: {}",
                parts[2], parts[3], e
            )
        });
        let strand = parts[4].chars().next().unwrap_or('?');
        Some(ParsedQueryId {
            segm_id,
            start_pos,
            end_pos,
            strand,
        })
    }
}

impl fmt::Display for ParsedQueryId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{}!{}!{}!{}",
            self.segm_id, self.start_pos, self.end_pos, self.strand
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread::sleep;
    use std::time::Duration;

    fn approx(actual: f64, expected: f64, epsilon: f64) -> bool {
        (actual - expected).abs() <= epsilon
    }

    #[test]
    fn counter_basic() {
        let mut c = Counter::default();
        assert_eq!(c.count(), 0);
        c.inc(5);
        assert_eq!(c.count(), 5);
    }

    #[test]
    fn counters_basic() {
        let mut c = Counters::new();
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| c.count("c1")));
        assert!(result.is_err());

        c.inc1("c1");
        assert_eq!(c.count("c1"), 1);
        c.inc("c2", 2);
        assert_eq!(c.count("c2"), 2);
        assert!(approx(c.frac("c1", "c2"), 0.5, 1e-9));
        assert!(approx(c.perc("c1", "c2"), 50.0, 1e-9));

        assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| c.frac("c1", "c3"))).is_err());
        assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| c.perc("c1", "c3"))).is_err());

        c.inc("c4", 0);
        assert_eq!(c.frac("c1", "c4"), f64::INFINITY);
        assert_eq!(c.perc("c1", "c4"), f64::INFINITY);
    }

    #[test]
    fn timer_basic() {
        let mut t = Timer::new();
        assert!(approx(t.secs(), 0.0, 1e-9));
        assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| t.range_ratio())).is_err());

        t.start();
        sleep(Duration::from_millis(10));
        t.stop();
        assert!(approx(t.secs(), 0.01, 0.02));
        assert!(approx(t.range_ratio(), 1.0, 0.05));

        t.start();
        sleep(Duration::from_millis(20));
        t.stop();
        assert!(approx(t.range_ratio(), 2.0, 0.3));
    }

    #[test]
    fn timers_basic() {
        let mut t = Timers::new();
        assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| t.range_ratio("t1"))).is_err());

        t.start("t1");
        t.start("t2");
        sleep(Duration::from_millis(10));
        t.stop("t1");
        sleep(Duration::from_millis(10));
        t.stop("t2");

        assert!(approx(t.secs("t1"), 0.01, 0.02));
        assert!(approx(t.secs("t2"), 0.02, 0.02));
        assert!(approx(t.perc("t1", "t2"), 50.0, 5.0));
        assert!(approx(t.perc("t2", "t1"), 200.0, 15.0));

        assert!(approx(t.range_ratio("t2"), 1.0, 0.3));
        t.start("t2");
        sleep(Duration::from_millis(10));
        t.stop("t2");
        assert!(approx(t.range_ratio("t2"), 2.0, 0.5));
    }

    #[test]
    fn parsed_query_id_valid() {
        let p = ParsedQueryId::parse("S1_21!NC_060948.1!57693539!57715501!+").unwrap();
        assert_eq!(p.segm_id, "NC_060948.1");
        assert_eq!(p.start_pos, 57693539);
        assert_eq!(p.end_pos, 57715501);
        assert_eq!(p.strand, '+');
    }

    #[test]
    fn parsed_query_id_invalid() {
        assert!(ParsedQueryId::parse("plain_read_name").is_none());
        assert!(ParsedQueryId::parse("a!b!c").is_none());
    }
}
