# PR description: Speed up loading the Start workflow task list

Start workflow repeated GitHub provider construction and App authentication when
loading the label catalog. Its execution-status read also hydrated every historical
execution for the displayed tasks, including a lazy human-interactions SELECT for
each execution that was subsequently discarded.

The GitHub launcher now shares one provider within each discovery worker and reads
the newest execution summaries for a display page in one SQL query. The provider is
discarded on success or failure; overlapping workers have separate contexts. Full
execution hydration and admission reads keep their existing behavior. Inline hints
identify execution-history, label-catalog and page-availability waits.

## Real-source measurements

Measured 2026-11-05 on Windows, Python 3.12.10, SQLAlchemy 2.0.52, gh 2.98.0,
against `OrlyJunior/prchef`: **63 open issues**, default **25-row display page**,
**14 complete label choices**, and the existing local SQLite execution history.
The database contained 192 GitHub executions across 138 task identities; probes
used a read-only database connection and the configured repository identity aliases.
The configured GitHub App supplied authentication. No artificial network delay,
fake task source, cached task catalog or headless rendering was used for the
interactive measurements.

Instrumentation used `perf_counter` around provider construction, configuration
resolution, subprocesses, controller reads and screen setup. HTTP requests were
counted/timed at `httpx.Client.send` through response-body receipt; SQL statements
were counted using SQLAlchemy's `before_cursor_execute` event. The usability endpoint
was the first completed Textual refresh with the page rows mounted and execution
availability applied. The response boundary was receipt of the final issue-page
body, before normalization. Labels and page status are included in that interval.

The baseline exercised the original independent issue/label providers and full
execution-status read. Loading-phase text and timing probes were already present;
the baseline status seam was explicitly routed to the unchanged full-execution
method. Samples are individual live runs, not a statistical latency guarantee.

| Measured phase | Before | After |
| --- | ---: | ---: |
| Aggregate configuration loads during screen setup/loading | 159 calls / 3.016 s | 158 calls / 2.945 s |
| Repository-options setup, before requesting tasks | 1.284 s | 1.226 s |
| First provider construction | 1.563 s | 1.588 s |
| Issue App-token HTTP request | 0.347 s | 0.312 s |
| Issue-page HTTP request | 0.795 s | 0.848 s |
| History-presence read, including identity resolution | 0.023 s | 0.056 s |
| Second provider construction for labels | 0.334 s | eliminated |
| Second App-token HTTP request for labels | 0.393 s | eliminated |
| Label HTTP request | 0.490 s | 0.397 s |
| Complete label phase, including construction/token | 1.241 s | 0.397 s |
| Page status, including identity resolution/hydration | 0.052 s | 0.044 s |
| **Final issue response to usable displayed rows** | **1.368 s** | **0.564 s** |
| Task-load request to usable displayed rows | 4.136 s | 3.382 s |
| Repository-options entry to usable displayed rows | 5.419 s | 4.608 s |

Phase timings overlap: main-thread configuration work continues while the worker
constructs its first provider. Do not add the rows as independent CPU costs.
Screen setup and repeated configuration resolution account for about three seconds
of aggregate work, primarily before the issue response. Configuration is repeatedly
resolved for role controls/defaults during screen initialization; this remains an
opportunity for a separate change. Removing the second construction/token locally
meets the measured response-to-usability target without changing shared
configuration or subprocess behavior. The label phase is the largest
measured contributor after issue receipt; the N+1 read is real but accounts for
milliseconds on this local database, not the entire reported delay.

| Actual requests/SELECTs for the interactive load | Before | After |
| --- | ---: | ---: |
| Issue-page requests | 1 | 1 |
| Issue-detail/comment requests | 0 | 0 |
| Label-catalog requests | 1 | 1 |
| App-token requests | 2 | 1 |
| Git remote-resolution subprocesses | 2 | 1 |
| History-presence SELECTs | 1 | 1 |
| Page-status SELECTs | 5 (including 4 relationship reads) | 1 |

The page contained two tasks with earlier executions and 23 available rows. The
new read ranks only matching repository/source identities by descending creation
time and ID, returns one summary per task, and never loads human interactions or
unrelated snapshots. PR presence is retained for fresh-run confirmation.

## Manual two-second check

Opened the actual interactive terminal app through a Windows PTY (80 columns,
25 rows), entered Start workflow, and navigated to the loaded task rows. With
63 issues and a 25-row page, selectable rows were rendered **0.564 s** after the
final issue response, including the label and status waits above. A second fresh
process measured **0.555 s**; it retained all 14 label choices and the same
23 available rows. Focus was observed on `task-checkbox-0`, and pressing Space
selected issue 354 and changed the hint to `Selected 1 of 63`.

Workflow submission was disabled in the selection-confirmation probe; no workflow
was launched. The startup interrupted-execution prompt was left for later in the
baseline and suppressed in subsequent probes, outside the measured Start-screen
interval. This check was interactive and is separate from automated tests.

## gh comparison and history scaling

For a comparable transport check, the existing App token was supplied only in the
probe process's environment to gh; ambient gh authentication was not modified.
Three runs of each command/read gave these seconds (median in bold):

| Read | Runs | Median |
| --- | --- | ---: |
| Raw `gh issue list --state open --limit 100` | 0.604, 1.168, 0.643 | **0.643** |
| Same list with JSON issue fields and comments | 2.068, 1.676, 1.481 | **1.676** |
| Raw `gh api` using the adapter's REST issue-page URL | 0.752, 0.851, 0.704 | **0.752** |
| Actual `GitHubIssueClient.list_issues` through gh | 0.847, 0.800, 0.929 | **0.847** |

The actual gh adapter returned 63 issues with exactly one subprocess/request each
run, without issue-detail or comment reads. The approximately 95 ms median
difference from raw `gh api` does not explain seconds of additional latency.
GraphQL `gh issue list` and the REST adapter have different payloads; the raw REST
comparison isolates that distinction. Neither transport implementation was changed.

A separate real-history read used the 1/25/50 most recently executed task
identities in the persisted legacy repository-reference group, without aliases.
These are historical task identities, not a claim that those issues remain open.
Five reads per method on the same read-only engine gave:

| Requested identities | Old SELECTs | New SELECTs | Old median | New median |
| --- | ---: | ---: | ---: | ---: |
| 1 | 2 | 1 | 0.773 ms | 0.607 ms |
| 25 | 29 | 1 | 6.812 ms | 0.747 ms |
| 50 | 67 | 1 | 14.863 ms | 0.908 ms |

## Validation

- 87 targeted unit/integration regressions passed across task providers, launcher
  status reads, labels, fresh-run confirmation, selection retention, paging,
  partial discovery, stale completions, lookup failures and narrow-terminal
  keyboard navigation.
- A real-SQLAlchemy regression reads through the controller with 1, 9 and 50 task
  identities, three executions per identity, snapshots, and both present/absent
  human interactions. Each nonempty read performs exactly one SELECT. It also
  checks timestamp/ID precedence, aliases, repository/source isolation, absent
  tasks, interrupted-stage information and every execution status. Empty input
  performs no SELECT.
- Provider-context tests cover load-local reuse, reset after exceptions, separate
  overlapping loads for the same repository, and unchanged non-GitHub behavior.
  Additional App transport tests cover 1/50/125 issues, a multi-page label catalog,
  case-distinct and unused labels, and exactly one installation-token request.
  Existing gh/App tests cover duplicates, PR-only pages and partial failures.
- Targeted Ruff checks passed for all changed Python files. Targeted mypy checking
  passed for the five changed source modules.
- Full pytest, full-repository Ruff and normal mypy validation are left to the
  orchestrator, as required by the implementer instructions. No existing test
  assertions were changed. `docs/PROJECT_CONTEXT.md` is unchanged because the
  existing layering and durable invariants are unchanged.
