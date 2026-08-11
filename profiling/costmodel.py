#!/usr/bin/env python3
"""A per-operation cost model of the mapper, fitted to a designed experiment.

Eyeballing medians could not settle the theta-ladder questions: a2's ad-hoc
probes carry enough drift that a 5% difference and a real effect look the same,
and one conclusion (a "regression" on B05) turned out to be drift in stages the
change never enters. This replaces the eyeballing with a model.

The model
---------
Per read, the three stages either half of the change can touch are a sum of
counted operations, each at a fixed cost:

    T  =  c_h*H  +  c_b*B  +  c_e*E  +  c_s*(F*m)

    H   `total_matches`     hits streamed by match_seeds, one visit each
    B   `seeded_buckets`    a bucket merged out of the accumulator, sorted, and
                            given its first hseed test
    E   `pruning_extends`   matches_in_bucket calls: a binary search plus a walk
    F   `refined_buckets`   refinements. Under Containment/Jaccard each scans
                            the bucket's span, 2*halflen = 2m positions, so the
                            work is F*m -- under bucket_SH the mapping is built
                            from the accumulator in O(1) and folds into c_b.

Every regressor is a counter the binary already reports, so the model is fitted
to the same runs it is judging -- no separate instrumentation, and no free
parameters beyond the four costs.

Four and not six: an intercept and a separate O(1)-refinement term were both
fitted and both dropped. Neither was distinguishable from zero and neither
improved the fit (R^2 0.99338 against 0.99364), because both are collinear with
the per-bucket term. Dropping the per-bucket term instead costs real accuracy
(0.9868), and dropping the extend term costs more (0.9615) -- so all four that
remain are earning their place.

Drift is modelled, not averaged away: each (benchmark, repeat) block of runs is
contiguous in time and gets one multiplicative factor, estimated jointly with
the costs by alternating least squares. What is left after that is the honest
measurement noise, and it is what every claim below is tested against.

The experiment is `costmodel_run.sh`; RESULTS.md section 5d is this script's output,
read back into prose.

usage: costmodel.py <dir>      # dir holds design.tsv and run<N>.json
"""
import json
import math
import os
import sys
from collections import defaultdict

# Two-sided 95% critical values of Student's t, by degrees of freedom. Hardcoded
# because the host has no scipy -- the same reason charts.py hand-writes SVG.
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
       8: 2.306, 9: 2.262, 10: 2.228, 12: 2.179, 15: 2.131, 20: 2.086,
       25: 2.060, 30: 2.042, 40: 2.021, 60: 2.000, 120: 1.980}


def t95(df):
    if df <= 0:
        return float("inf")
    keys = sorted(T95)
    return T95[min((k for k in keys if k >= df), default=keys[-1])]


def solve(a, b):
    """Gauss-Jordan solve of a*x = b, returning (x, inverse-of-a)."""
    n = len(a)
    m = [list(row) + [1.0 if i == j else 0.0 for j in range(n)] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-300:
            raise ValueError("singular design: a regressor is a combination of the others")
        m[col], m[piv] = m[piv], m[col]
        d = m[col][col]
        m[col] = [v / d for v in m[col]]
        for r in range(n):
            if r != col and m[r][col] != 0.0:
                f = m[r][col]
                m[r] = [v - f * w for v, w in zip(m[r], m[col])]
    return [m[i][2 * n] for i in range(n)], [m[i][n:2 * n] for i in range(n)]


def ols(X, y):
    """Ordinary least squares. Returns (beta, stderr, r2, residual_sd)."""
    n, p = len(X), len(X[0])
    xtx = [[sum(X[i][a] * X[i][b] for i in range(n)) for b in range(p)] for a in range(p)]
    xty = [sum(X[i][a] * y[i] for i in range(n)) for a in range(p)]
    beta, inv = solve(xtx, xty)
    fit = [sum(beta[a] * X[i][a] for a in range(p)) for i in range(n)]
    rss = sum((y[i] - fit[i]) ** 2 for i in range(n))
    ybar = sum(y) / n
    tss = sum((v - ybar) ** 2 for v in y)
    dof = max(n - p, 1)
    s2 = rss / dof
    se = [math.sqrt(max(s2 * inv[a][a], 0.0)) for a in range(p)]
    return beta, se, (1 - rss / tss if tss > 0 else float("nan")), math.sqrt(s2)


# --- the experiment ---------------------------------------------------------

NAMES = ["c_h  per streamed hit", "c_b  per seeded bucket", "c_e  per pruning extend",
         "c_s  per refinement per read k-mer"]
STAGES = ["match_seeds", "bucket_merge", "match_rest"]


def load(d):
    rows = []
    with open(os.path.join(d, "design.tsv")) as f:
        head = f.readline().rstrip("\n").split("\t")
        for line in f:
            r = dict(zip(head, line.rstrip("\n").split("\t")))
            p = os.path.join(d, f"run{r['idx']}.json")
            if not os.path.exists(p):
                continue
            j = json.load(open(p))
            c, t = j["global"]["counters"], j["global"]["timers_secs"]
            n = c["reads"]
            m = c["kmers_sketched"] / n
            refining = r["metric"] in ("Containment", "Jaccard")
            rows.append(dict(
                idx=int(r["idx"]), bench=r["bench"], metric=r["metric"],
                steps=int(r["steps"]), skip=int(r["skip"]), rep=int(r["rep"]),
                reads=n, m=m, refined=c["refined_buckets"] / n,
                extends=c["pruning_extends"] / n,
                # regressors, per read
                w=[c["total_matches"] / n, c["seeded_buckets"] / n, c["pruning_extends"] / n,
                   (c["refined_buckets"] / n * m) if refining else 0.0],
                t=sum(t[s] for s in STAGES) / n,
                q=t["query_mapping"] / n))
    return rows


def live_columns(rows):
    """Regressors that carry information here.

    Two ways a column can fail to. It can be structurally absent — a metric that
    never takes a code path leaves it all-zero, which is not a missing
    measurement. Or it can be constant across every run in the subset, which
    makes it indistinguishable from the intercept. Both are dropped rather than
    fitted to noise.
    """
    p = len(rows[0]["w"])
    keep = []
    for c in range(p):
        vals = [r["w"][c] for r in rows]
        if not any(abs(v) > 0 for v in vals):
            continue
        lo, hi = min(vals), max(vals)
        if c != p - 1 and hi - lo <= 1e-12 * max(abs(hi), 1.0):
            continue  # constant: absorbed by the intercept
        keep.append(c)
    return keep


def fit_block(rows, blocks=8):  # noqa: C901
    """Costs shared across the rows, one multiplicative drift factor per block.

    Alternating least squares: with the drift factors held fixed the costs are a
    plain OLS problem, and with the costs held fixed each block's factor is the
    least-squares scale between its predictions and its measurements. The first
    block is pinned at 1 so the two are identifiable.
    """
    cols = live_columns(rows)
    key = lambda r: (r["rep"], r["bench"])
    if len(rows) <= len(cols) + 1:
        raise ValueError(f"only {len(rows)} runs for {len(cols)} regressors")
    ks = sorted({key(r) for r in rows})
    gamma = {k: 1.0 for k in ks}
    beta = se = None
    r2 = sd = float("nan")
    for _ in range(blocks):
        X = [[r["w"][c] for c in cols] for r in rows]
        y = [r["t"] / gamma[key(r)] for r in rows]
        beta, se, r2, sd = ols(X, y)
        pred = {id(r): sum(b * r["w"][c] for b, c in zip(beta, cols)) for r in rows}
        for k in ks[1:]:
            num = sum(r["t"] * pred[id(r)] for r in rows if key(r) == k)
            den = sum(pred[id(r)] ** 2 for r in rows if key(r) == k)
            if den > 0:
                gamma[k] = num / den
    # Re-expand to the full regressor list so callers need not know about cols.
    full_b = [0.0] * len(rows[0]["w"])
    full_s = [float("nan")] * len(rows[0]["w"])
    for b, s, c in zip(beta, se, cols):
        full_b[c], full_s[c] = b, s
    return full_b, full_s, r2, sd, gamma


def predict(beta, r):
    return sum(b * w for b, w in zip(beta, r["w"]))


def main(d):
    rows = load(d)
    if not rows:
        sys.exit(f"no runs found in {d}")
    print(__doc__.split("usage:")[0].rstrip())
    print(f"\n{len(rows)} runs, "
          f"{len({r['bench'] for r in rows})} benchmarks x {len({r['metric'] for r in rows})} metrics "
          f"x {len({(r['steps'], r['skip']) for r in rows})} configurations x "
          f"{len({r['rep'] for r in rows})} repeats\n")

    print("=" * 78)
    print("1. FITTED PER-OPERATION COSTS, nanoseconds with a 95% CI")
    print("=" * 78)
    beta_all, se_all, r2_all, sd_all, gamma_all = fit_block(rows)
    df_all = len(rows) - len(live_columns(rows)) - len({(r["rep"], r["bench"]) for r in rows}) + 1
    mean_all = sum(r["t"] for r in rows) / len(rows)
    print("  pooled over every run:")
    for i, name in enumerate(NAMES):
        v, e = beta_all[i], se_all[i]
        if v == 0.0 and e != e:
            continue
        hi = t95(df_all) * e
        unit = f"{v * 1e9:8.2f} +- {hi * 1e9:.2f} ns"
        flag = "" if abs(v) > hi else "   (not distinguishable from zero)"
        print(f"    {name:40} {unit}{flag}")
    print(f"    {'R^2':40} {r2_all:8.4f}")
    print(f"    {'residual sd':40} {100 * sd_all / mean_all:8.2f}% of the mean per-read time")
    print()
    print("  and per benchmark, which is the consistency check -- these are machine")
    print("  constants, so they should not move much between workloads:")
    print(f"{'benchmark':10} " + " ".join(f"{n.split()[0]:>13}" for n in NAMES) + f" {'R^2':>7} {'resid':>7}")
    fits = {}
    for b in sorted({r["bench"] for r in rows}):
        sub = [r for r in rows if r["bench"] == b]
        try:
            beta, se, r2, sd, gamma = fit_block(sub)
        except ValueError as e:
            print(f"{b:10} skipped: {e}")
            continue
        fits[b] = (beta, se, sd, gamma, sub)
        df = len(sub) - len(beta)
        cells = []
        for v, e in zip(beta, se):
            if v == 0.0 and e != e:
                cells.append("—")
                continue
            hi = t95(df) * e
            cells.append(f"{v * 1e9:7.1f}+-{hi * 1e9:<5.1f}")
        mean_t = sum(r["t"] for r in sub) / len(sub)
        print(f"{b:10} " + " ".join(f"{c:>13}" for c in cells) + f" {r2:7.4f} {100 * sd / mean_t:6.2f}%")
    print("\n  `resid` is the residual standard deviation as a share of the mean per-read")
    print("  time: the measurement noise that survives drift correction, i.e. the real")
    print("  floor. A dash is a regressor that does not vary within that benchmark and so")
    print("  cannot be estimated from it alone -- on B05 the ladder is inert, so `H` and")
    print("  `B` are the same in every configuration and the remaining two terms absorb")
    print("  its baseline. Read B05's row as a fit, not as a measurement of those costs;")
    print("  the pooled fit above estimates them from the benchmarks that do vary.")

    print("\n" + "=" * 78)
    print("2. WHERE A READ'S TIME ACTUALLY GOES, from the fit x the counters")
    print("=" * 78)
    print("  Shipped configuration, per read, decomposed by the model:")
    print(f"  {'benchmark':10} {'metric':12} " + " ".join(f"{n.split()[0]:>9}" for n in NAMES) + f" {'total':>9}")
    for b in sorted(fits):
        for me in sorted({r["metric"] for r in rows if r["bench"] == b}):
            sub = [r for r in rows if r["bench"] == b and r["metric"] == me
                   and r["steps"] == 1 and r["skip"] == 1]
            if not sub:
                continue
            parts = [sum(beta_all[i] * r["w"][i] for r in sub) / len(sub) for i in range(len(NAMES))]
            tot = sum(parts)
            print(f"  {b:10} {me:12} " + " ".join(f"{100 * p / tot:8.1f}%" for p in parts)
                  + f" {tot * 1e6:8.2f}us")

    print("\n" + "=" * 78)
    print("3. THE EXCHANGE RATES, which are what the design turns on")
    print("=" * 78)
    c_h, c_b, c_e, c_s = beta_all[0], beta_all[1], beta_all[2], beta_all[3]
    print(f"  one pruning extend   = {c_e / c_h:6.1f} streamed hits")
    print(f"  one seeded bucket    = {c_b / c_h:6.1f} streamed hits")
    for b in sorted({r["bench"] for r in rows}):
        m = sum(r["m"] for r in rows if r["bench"] == b) / len([r for r in rows if r["bench"] == b])
        print(f"  one refinement       = {c_s * m / c_e:6.1f} extends = {c_s * m / c_h:6.0f} hits"
              f"   ({b}, m = {m:.0f})")
    print()
    print("  Which is the whole argument, in one line: **skipping a bucket's pruning")
    print("  walk pays only where it removes more extends than a refinement costs**.")
    print("  That number is workload-dependent through `m` alone, and it is why the")
    print("  same rule wins on 12.8 kb HiFi reads and loses on 23.8 kb ONT ones.")

    print("\n" + "=" * 78)
    print("4. DOES THE MODEL PREDICT THE CHANGE? Held-out: fit without the shipped")
    print("   configuration, then predict it")
    print("=" * 78)
    print(f"{'benchmark':10} {'metric':12} {'observed':>10} {'predicted':>10} {'error':>8} {'vs noise':>10}")
    for b in sorted(fits):
        for me in sorted({r["metric"] for r in rows if r["bench"] == b}):
            sub = [r for r in rows if r["bench"] == b]
            shipped = [r for r in sub if r["metric"] == me and r["steps"] == 1 and r["skip"] == 1]
            train = [r for r in sub if not (r["metric"] == me and r["steps"] == 1 and r["skip"] == 1)]
            if not shipped or len(train) < 12:
                continue
            beta, _, _, sd, gamma = fit_block(train)
            obs = sum(r["t"] for r in shipped) / len(shipped)
            pre = sum(predict(beta, r) * gamma.get((r["rep"], r["bench"]), 1.0) for r in shipped) / len(shipped)
            print(f"{b:10} {me:12} {obs * 1e6:9.2f}us {pre * 1e6:9.2f}us "
                  f"{100 * (pre - obs) / obs:7.1f}% {abs(pre - obs) / sd:9.2f}sd")

    print("\n" + "=" * 78)
    print("5. IS EACH HALF OF THE CHANGE REAL? Paired, because the two members of a")
    print("   pair run back to back and share their drift")
    print("=" * 78)
    for label, field, on, off in [("A. the ladder (1 rung vs the single pass), walk skipped", "steps", 1, 0),
                                  ("B. the walk skipped vs walked, at 1 rung", "skip", 1, 0)]:
        print(f"\n  {label}")
        print(f"  {'benchmark':10} {'metric':12} {'speedup':>9} {'95% CI':>17} "
              f"{'significant':>12} {'model predicts':>15}")
        for b in sorted({r["bench"] for r in rows}):
            for me in sorted({r["metric"] for r in rows if r["bench"] == b}):
                pairs = defaultdict(dict)
                for r in rows:
                    if r["bench"] != b or r["metric"] != me:
                        continue
                    if field == "steps" and r["skip"] != 1:
                        continue
                    if field == "skip" and r["steps"] != 1:
                        continue
                    pairs[r["rep"]][r[field]] = r
                ds, mds, bases = [], [], []
                for pr in pairs.values():
                    if on in pr and off in pr:
                        ds.append(pr[off]["t"] - pr[on]["t"])
                        mds.append(predict(beta_all, pr[off]) - predict(beta_all, pr[on]))
                        bases.append(pr[on]["t"])
                if len(ds) < 2:
                    continue
                n = len(ds)
                mu = sum(ds) / n
                sd = math.sqrt(sum((x - mu) ** 2 for x in ds) / (n - 1)) if n > 1 else 0.0
                half = t95(n - 1) * sd / math.sqrt(n)
                base = sum(bases) / n
                mmu = sum(mds) / len(mds)
                print(f"  {b:10} {me:12} {1 + mu / base:8.3f}x "
                      f"[{1 + (mu - half) / base:5.3f},{1 + (mu + half) / base:5.3f}] "
                      f"{('yes' if abs(mu) > half else 'no'):>12} {1 + mmu / base:14.3f}x")

    print("\n" + "=" * 78)
    print("6. WHAT DOES THE MODEL SAY THE RUNG COUNT SHOULD BE?")
    print("   Predicted per-read time at each rung count, from that rung's own counters")
    print("=" * 78)
    print(f"{'benchmark':10} {'metric':12} " + " ".join(f"{'steps=' + str(s):>16}" for s in (0, 1, 2)) + "  best")
    for b in sorted(fits):
        beta = fits[b][0]
        for me in sorted({r["metric"] for r in rows if r["bench"] == b}):
            cells, best, bestv = [], None, None
            for s in (0, 1, 2):
                sub = [r for r in rows if r["bench"] == b and r["metric"] == me
                       and r["steps"] == s and r["skip"] == 1]
                if not sub:
                    cells.append(f"{'-':>16}")
                    continue
                p = sum(predict(beta, r) for r in sub) / len(sub)
                o = sum(r["t"] for r in sub) / len(sub)
                cells.append(f"{p * 1e6:7.2f} ({o * 1e6:5.2f})")
                if bestv is None or p < bestv:
                    bestv, best = p, s
            print(f"{b:10} {me:12} " + " ".join(f"{c:>16}" for c in cells) + f"  steps={best}")
    print("\n  predicted us/read, with the drift-uncorrected observation in brackets.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/q12m")
