# TUI synchronization review

Verified on Windows with CPython 3.12.10 on 2026-10-26 (America/Sao_Paulo), in
this PR's retained worktree. Review edits are uncommitted. No test was skipped,
retried automatically, weakened, or given a fixed sleep.

## Executed validation

- Before review edits: `python -m pytest tests/unit/test_tui_sync.py tests/unit/test_tui.py -ra`
  -> **271 passed, 1 skipped**, 138.20 seconds.
- After test/code-comment edits: the same command -> **274 passed, 1 skipped**,
  129.87 seconds. Added cases cover a real Textual descendant worker, descendant
  failure, and unexpected descendant cancellation through `wait_for_tui`.
- Clean `origin/develop` baseline at `93354167fc8181b018afb2530b8795d2e144d4f7`:
  `python -m pytest tests/unit/test_tui.py -ra` -> **262 passed, 1 skipped**,
  139.73 seconds. That revision has no `test_tui_sync.py`.
- Both revisions skip the same existing **POSIX file modes required** test.
- `python -m ruff check .` -> **passed**.
- `python -m mypy src/prchef` -> **passed**, 119 source files.

Python 3.11 was not found by `py -0p`, `py -3.11 --version`, or in the checked
standard/uv directories. Python 3.13 does not substitute for 3.11. The
conditional 10-idle + 10-loaded campaign per version was **not executed**
because both required versions are not available. The **50-run criterion is
unmet**. The full repository pytest suite was not run in this review.

## Demonstrated mechanisms

- Removing **only** `group="task-load"` from `_load_tasks`, then running
  `python -m pytest tests/unit/test_tui.py::test_workflow_runner_selects_available_tasks_and_starts_batch_in_list_order -ra`,
  fails with **Task refresh cancelled the batch worker**. The callback observes
  the active batch worker while starting a task refresh. The group was restored.
  This proves a controlled collision, not the cause of every historical trace.
- Removing only the named-preset `event.select.value != event.value` guard
  fails the runtime-sources test because the older preset is applied.
- Removing only the manual-preset guard initially still passed. The regression
  now queues `older`, manual, and `focused` selections without an intervening
  drain and observes default restoration as well as preset application.
  Removing only the manual guard now fails with **Stale manual selection
  restored defaults**. Both guards were restored after the experiments.
  Command: `python -m pytest tests/unit/test_tui.py::test_workflow_runner_shows_runtime_sources_for_each_role_without_changing_payload -ra`.
- The original stop-control fixture stores `APPROVED` while injecting an active
  event. A separate real-TUI probe demonstrated visible stop controls after a
  `RUNNING` event and hidden controls immediately after reconciliation against
  that fixture. This is fixture inconsistency, not demonstrated slow visibility.
  The PR sets matching persisted status and explicitly reconciles.
- The historical one-row diagnostic overflow remains **unproven**. Content and
  geometry now use bounded predicates and descendant-worker waits; passing
  locally does not reproduce that historical Windows failure.

Both bounds in `wait_for_tui` are retained: the deadline passes the remaining
total budget into nested worker waits, while `asyncio.timeout` also interrupts
an unresponsive `Pilot.pause()`. The blocked-pump and unmet-predicate tests
exercise these paths. No helper behavior was changed during this review.

The BOM CSV was replaced by `tui_settle_inventory.md`, a categorized list of
remaining risks and representative tests. A complete individual audit of every
remaining call site is still **outstanding**.

## Full Windows repetitions (not executed)

From this PR worktree, create separate environments with CI's hash-locked
dependencies. Python 3.11 must first be installed. These commands prepare both:

```powershell
$env311 = Join-Path $env:LOCALAPPDATA 'prchef-review\py311'
$env312 = Join-Path $env:LOCALAPPDATA 'prchef-review\py312'
py -3.11 -m venv $env311
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 environment creation failed' }
py -3.12 -m venv $env312
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 environment creation failed' }
foreach ($reviewEnv in @($env311, $env312)) {
    $reviewPython = Join-Path $reviewEnv 'Scripts\python.exe'
    & $reviewPython -m pip install --require-hashes --only-binary=:all: -r constraints-dev.txt
    if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed' }
    & $reviewPython -m pip install --no-deps --no-build-isolation -e '.[dev]'
    if ($LASTEXITCODE -ne 0) { throw 'Project installation failed' }
}
```

Then run this fail-fast loop: **25 idle + 25 loaded = 50 consecutive runs per
version**. One CPU-bound process defines the load; this does not claim to
saturate every CPU core. Set `$perMode = 10` for the reduced **10 + 10** run.

```powershell
$perMode = 25
foreach ($reviewEnv in @($env311, $env312)) {
    $reviewPython = Join-Path $reviewEnv 'Scripts\python.exe'
    $version = & $reviewPython -c 'import sys; print("%s.%s" % sys.version_info[:2])'
    if ($LASTEXITCODE -ne 0 -or $version -notin @('3.11', '3.12')) {
        throw "Unexpected Python: $version"
    }
    $log = Join-Path $env:TEMP "prchef-tui-windows-$version.log"
    "Python $version; $perMode idle + $perMode loaded" | Set-Content $log
    $load = $null
    try {
        for ($i = 1; $i -le (2 * $perMode); $i++) {
            if ($i -eq ($perMode + 1)) {
                $load = Start-Process -FilePath $reviewPython `
                    -ArgumentList '-c', '"while True: pass"' -WindowStyle Hidden -PassThru
            }
            $mode = if ($i -le $perMode) { 'idle' } else { 'one CPU-bound process' }
            if ($null -ne $load -and $load.HasExited) { throw 'Load process exited early' }
            "Iteration $i ($mode)" | Tee-Object -FilePath $log -Append
            & $reviewPython -m pytest tests/unit/test_tui_sync.py tests/unit/test_tui.py -ra 2>&1 |
                Tee-Object -FilePath $log -Append
            if ($LASTEXITCODE -ne 0) { throw "Iteration $i failed: exit $LASTEXITCODE" }
        }
    } finally {
        if ($null -ne $load -and -not $load.HasExited) {
            Stop-Process -Id $load.Id
            $load.WaitForExit()
        }
    }
}
```

Full acceptance remains unmet until both logs contain 50 successful iterations
and the individual settle-site audit is completed.
