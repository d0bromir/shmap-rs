# docs/

Project documentation that is not a measurement. (Numbers live in
[`../RESULTS.md`](../RESULTS.md); what changed against the C++ lives in
[`../PORT_CHANGES.md`](../PORT_CHANGES.md).)

| I want to… | read |
|---|---|
| understand the algorithm, end to end | [`algorithm/`](algorithm/) — LaTeX book ([PDF](algorithm/shmap_algorithm.pdf)), one section per pipeline stage |
| know where the project is heading, and what was retired | [`ROADMAP.md`](ROADMAP.md) — accuracy-first direction, optimization roadmap, retired adaptive prototype |
| know what is slow and what to change for a speedup | [`SPEEDUP_PROPOSALS.md`](SPEEDUP_PROPOSALS.md) — measured hot spots and ranked, exactness-argued proposals |
| see what Pesho asked and what became of each request | [`QUESTIONS.md`](QUESTIONS.md) — index Q1–Q16 and open questions P1–P3 |
| know what a version number means, or what a PR must satisfy | [`VERSIONING.md`](VERSIONING.md) — software / suite / dataset / result-set versions, the PR rule |
| read what a release contained | [`releases/`](releases/) |

Process docs sit at the repo root by GitHub convention: [`../CONTRIBUTING.md`](../CONTRIBUTING.md)
(how a PR is checked) and [`../SECURITY.md`](../SECURITY.md) (why benchmarks run on a private host).

## Suggested reading order

1. [`../README.md`](../README.md) — what it is, how to run it.
2. [`../PORT_CHANGES.md`](../PORT_CHANGES.md) — why it is faster and lighter (table first, detail after).
3. [`../RESULTS.md`](../RESULTS.md) — the measurements, §1 first.
4. [`ROADMAP.md`](ROADMAP.md) — what is next.
