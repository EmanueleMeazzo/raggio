# ADR 0018 — Safe collection name reuse and selective embedding retries

Status: accepted · Date: 2026-11-08

## Issue verification

Reviewed all four open GitHub issues against main `9fcbbdd` before changing runtime
code. This resolves two remaining items in ADR 0001's concurrency Deferred row.

| Issue | Current verdict | Evidence |
|---|---|---|
| [#1](https://github.com/EmanueleMeazzo/raggio/issues/1): same-name create loses its directory | Valid; fixed here | Parking the delete's `rmtree`, recreating the name, then releasing cleanup reproduced a 201 create followed by a 500 search, with both resident and unloaded collections. |
| [#2](https://github.com/EmanueleMeazzo/raggio/issues/2): subprocess close-race coverage | Already addressed | `test_close_waits_for_a_read_parked_in_prepare` and `test_close_past_its_deadline_leaves_the_connection_to_its_reader` run the real collection setup in child processes and assert process survival and eventual connection closure. Both passed locally. |
| [#4](https://github.com/EmanueleMeazzo/raggio/issues/4): embedding retries permanent failures | Valid; fixed here | Deterministic HTTPX transports reproduced three attempts and sleeps of 1, 2, and 4 seconds for permanent HTTP errors, closed clients, and malformed responses. |
| [#5](https://github.com/EmanueleMeazzo/raggio/issues/5): collection requests racing DELETE return 500 | Already addressed | `test_http_requests_racing_a_close_answer_503_or_404` covers reads and writes after eviction, deletion, and eviction followed by deletion. The tests passed, including the real SQLite guard and the app's typed-error handlers. |

Main's [GitHub Actions run](https://github.com/EmanueleMeazzo/raggio/actions/runs/42431729211)
passed every job, including the native and free-threaded suites on Linux arm64.
Those jobs execute the subprocess close-race tests, so #2 already has the requested
platform coverage. This run validates the previous main revision, not these new fixes.

## Collection directory lifecycle

`create_collection` now joins `touch` and `delete_collection` under `_load_lock`
for directory and catalog changes. Dimension probing stays outside the lock; the
name is checked again after acquiring it, before touching any directory.

After `stop` finishes, deletion commits removal of the catalog row and renames
`collections/<name>` to `collections/<name>.deleted-<uuid>` before releasing the
lock. Physical removal runs in a worker thread against that unique retired path.
A concurrent create can safely publish a fresh directory at the live name. A
cancelled delete can leave cleanup running without endangering its replacement.

Create uses the same retirement step for an unregistered leftover directory,
rather than letting a cancelled leftover cleanup keep removing the live path.
Startup sweeps retired directories left by cancellation, a crash, or failed
physical removal. Dots are excluded from API collection names, so the retirement
pattern cannot match a valid collection name.

The catalog deletion commits before the rename: a crash between them leaves an
unregistered directory that the next create retires. It cannot leave a catalog
row pointing to a directory that startup will remove. On Windows the rename still
requires closed SQLite handles. If `stop` hands a connection to a reader after its
existing close deadline, or a close step fails, rename can fail until that handle
closes. Deletion logs a failed rename and leaves the unregistered directory rather
than returning a new 500 or scheduling cleanup against its live name. A subsequent
create must successfully retire that leftover before publishing fresh data; it
can still fail while the handle remains open. This preserves the existing
best-effort physical cleanup policy and does not bypass the SQLite guard. A retired
directory can be swept at boot.

`stop` remains under `_load_lock`, as in ADR 0001. A delete can still delay a load
of another nonresident collection. Resident requests keep their existing fast
path. No storage format, search ranking, or indexing configuration changes.

## Embedding retry policy

The maximum remains three total attempts. Only `httpx.TransportError`, HTTP 429,
and HTTP 500–599 can retry. Other statuses, a closed client, malformed responses,
and ordinary programming errors propagate immediately. A transport failure from
a client closed during the request also propagates immediately.

Backoff is 1 second before the second attempt and 2 seconds before the third;
there is no sleep after exhaustion. The final transient-failure `RuntimeError`
retains the original HTTPX exception as its cause. Input payloads and sorting
embeddings by their returned `index` are unchanged.

This is a bounded correctness fix, not a throughput optimization. Backoff tests
record requested delays without waiting for wall-clock sleeps; they establish
the retry budget rather than claim deployment latency measurements. HTTPX's
`HTTPStatusError` classification was checked against its 0.28.1 documentation
through Context7 and the installed client was checked for its closed-client error.

## Validation

Before the fixes, the new regression tests produced **15 failures and 8 passes**.
The directory cases failed with the reported HTTP 500, and the retry cases
observed the unwanted attempts/backoffs. The existing #2/#5 verification selection
passed **13 tests** before any runtime edit.

New coverage also exercises a cancelled delete's orphaned cleanup, stale-directory
sweeping, a blocked Windows-style rename, a create delayed by dimension probing,
transient request recovery, payload/result ordering, retry exhaustion,
a transport interrupted by client closure, and an HTTP search using the real
`Embedder` and HTTPX closed-client check. The latter must answer 404 without any
embedding retry backoff.

The full Python suite passed on Windows, CPython 3.12.8 / SQLite 3.47.1:
**806 passed, 74 skipped** in 131.77 seconds. The skips include the optional native
extension and POSIX-only checks. Git Bash's benchmark harness selection separately
passed **47 tests**. `uv lock --check` and `git diff --check` passed.

After building `raggio-native` with Rust 1.99.1 and requiring it at test time, the
full suite passed **874 tests, with 6 skips**, in 143.82 seconds. The blocked-rename
regression first reproduced a 500 in the initial fix. After preserving best-effort
deletion, the final affected-path selection (`test_embeddings`, `test_concurrency`,
`test_api`, `test_cold_start`, `test_fts_keys`) passed **251 tests, with 1 skip**, in
64.88 seconds, with the native extension required. The final fallback was covered
by this selection after the full suite had started.

Validation commands:

```powershell
uv sync --frozen --group bench
uv run --no-sync pytest -q
uv lock --check
git diff --check
```

For native validation, use `uv sync --frozen --group bench --extra native`, set
`REQUIRE_NATIVE=1`, and run the same test command with `--no-sync`.

On this Windows workstation, select Git Bash ahead of the WindowsApps WSL launcher
in the test process's `PATH` when running the shell-based benchmark harness tests.

## Delivery on current main

Before publication, main advanced to `f5fe43e` (raggio 0.4.0, turbovec 1.1.2).
The fixes were rebased onto that revision and this review was renumbered from
0017 to 0018 to preserve the upgrade's ADR. The upgrade's dependency lock, format
compatibility tests, and documentation remain intact.

Current main's [CI run](https://github.com/EmanueleMeazzo/raggio/actions/runs/42486343519)
passed all five jobs, including native and free-threaded suites on Linux arm64.
The rebased native-enabled full suite passed **879 tests, with 6 skips**, in
142.31 seconds on Windows / CPython 3.12.8 / SQLite 3.47.1. The lockfile and diff
checks passed again before publication.
