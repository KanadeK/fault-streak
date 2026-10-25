# Validation

Local validation on 2026-10-25: Windows, CPython 3.13.16.

- 21 unittest tests passed. Actual runtime integration captures a controlled waiting thread twice and verifies the resulting thread ID appears in a streak. A real recursive stack tests conservative frame-cap exclusion. Threads are released and joined in cleanup.
- Synthetic tests cover reordered threads, full-stack equality, changed callers, maximal consecutive runs, missing observations, normalized IDs, truncation, unknown frames, unsupported/mixed inputs, duplicate current-thread headers, budgets and CLI statuses.
- Statement coverage: 99% of core.py and 98% of the package in the measured test process. Subprocess CLI coverage is not added to those totals.
- Ruff passed (E4/E7/E9/F/B). The documented demo was actually run and its two real captures analyzed successfully.
- `scripts/smoke_wheel.py` checks a freshly built wheel in a separate virtual environment, verifies import origin and the documented synthetic workflow, then uninstalls it.
- Review caught two evidence hazards before release: a deep CPython stack can be silently capped, and concatenated snapshots can have distinct thread sets but repeated current-thread headers. Both now receive conservative handling/tests. No unsupported native-stack format is silently accepted.

CI runs on Windows/Linux with Python 3.10/3.13; inspect the actual run for remote results. No runtime third-party dependencies or copied implementation code are included. Exact displayed stack equality is not a deadlock or progress diagnosis. Thread reuse, between-sample work and undetectable capture omissions remain explicit limitations.
