# TUI synchronization review

`tui_settle_inventory.csv` lists each remaining `await settle_tui` in the TUI
module, its enclosing test, line, and next source line. Each call assumes that
draining the message queue and waiting for current screen-owned workers is
sufficient for its next operation. It does not wait for descendant workers,
future timer events, animations, or arbitrary background activity. The inventory
is a search aid, not proof that those assumptions hold at every call site; a
complete individual audit remains outstanding.

The two related execution-detail tests that open diagnostics now wait for log
content and descendant workers before inspecting layout or scroll state. The
role-local preset tests still assume that queued selection and input messages
are drained by `settle_tui`; the runtime-sources regression separately checks
the queued older/newer selection sequence and the immediate source values.
The stop-control test reconciles against a matching persisted status. The
batch-order regression observes the execution worker while completion starts
the refresh, then joins both workers. These new regressions have not been run
in this workspace. The original Windows cancellation and one-row overflow
traces were not supplied, so their exact historical causes remain unproven.

## Windows repetitions

Required evidence is **not available**: no Python interpreter was found on PATH
in this workspace and no repetitions were run. The 50 consecutive runs for
each Python version, including CPU load, remain unmet. The repository's
implementer instructions also reserve the full suite for the orchestrator.

On a provisioned Windows runner, install the pinned development dependencies
as in `.github/workflows/ci.yml`. For each of Python 3.11 and 3.12, activate the
corresponding environment and run the following PowerShell. It fails fast,
records each result, and runs 25 iterations idle and 25 with one CPU-bound
background process. Each iteration runs the entire TUI module, which includes
all four reported tests. Run the two versions separately; retain both logs.

```powershell
$version = python -c 'import sys; print("%s.%s" % sys.version_info[:2])'
if ($version -notin @('3.11', '3.12')) { throw "Unexpected Python: $version" }
$log = "tui-windows-$version.log"
"Python $version; 25 idle and 25 loaded iterations" | Set-Content $log
$load = $null
try {
    for ($i = 1; $i -le 50; $i++) {
        if ($i -eq 26) {
            $load = Start-Process -FilePath (Get-Command python).Source `
                -ArgumentList '-c', '"while True: pass"' -WindowStyle Hidden -PassThru
        }
        $mode = if ($i -le 25) { 'idle' } else { 'one CPU-bound process' }
        "Iteration $i ($mode)" | Tee-Object -FilePath $log -Append
        python -m pytest tests/unit/test_tui.py 2>&1 | Tee-Object -FilePath $log -Append
        if ($LASTEXITCODE -ne 0) { throw "Iteration $i failed: exit $LASTEXITCODE" }
    }
} finally {
    if ($null -ne $load -and -not $load.HasExited) { Stop-Process -Id $load.Id }
}
```

The task-load worker group prevents a new task refresh from superseding the
batch worker, but the historical `WorkerCancelled` cannot be attributed to
that collision without the original trace or a successful controlled run.
