# Execution ownership and startup recovery

Scope: one machine and one database, normally local SQLite. Each execution-producing
application instance registers a random UUID, never a PID, in `execution_owners`.
Registration commits its initial UTC heartbeat before admitting work. A dedicated
thread persists a fresh UTC heartbeat every 10 seconds using a short, independent
repository session. Scheduling uses an interruptible monotonic wait, independent of
the UI, dispatcher, provider calls and stage progress. All services and batches in
the instance share that owner. Read-only commands run recovery without registering.
Registration failure prevents admission; refresh failures are logged and retried at
the next interval. Database read errors propagate; they never imply owner death.

An unended owner is live when `now - last_heartbeat_at <= 300 seconds`. Exactly 300
seconds and future heartbeats are live; only ages strictly greater than 300 seconds
are stale. Recovery considers PENDING, WAITING and RUNNING rows. Live-owned rows and
their steps, snapshots, timestamps and checkpoints are left untouched and excluded
from startup resume choices. The TUI shows a notice even if no rows were interrupted.
Ownerless, missing-owner, ended-owner and stale-owner active rows become INTERRUPTED
using existing checkpoint inference and running-step interruption. Terminal rows
are never candidates. Recovery remains an explicit startup action, not a worker.

Fresh insertion, interrupted admission, human response and continuation assign owner
and PENDING in the same transaction. Interrupted admission retains its conditional
status check; continuation cannot steal active work. On SQLite, recovery obtains
`BEGIN IMMEDIATE` before reading any candidates or heartbeat records and takes its
comparison time after obtaining the reservation. Admission and heartbeat writes
serialize against it. Completion committed before the candidate read stays terminal;
a heartbeat committed before the decision protects its executions. PostgreSQL uses
READ COMMITTED and the same transaction advisory lock for ownership, admission,
recovery and completion. Other dialects fail explicitly without a locking strategy.
No lock spans UI, provider, network or subprocess work.

Shutdown first stops local admission, blocks worker writes and interrupts local
commands, then stops/joins the heartbeat thread. One short transaction interrupts only
that owner's active work and marks the owner ended. Repeated shutdown is safe, and
heartbeat updates condition on an unended owner so late calls cannot revive it.
There is no unscoped shutdown recovery. Quit confirmation counts only locally owned
active work. The CLI `run` and `respond` entry points use the same lifecycle, keeping
logs and startup notices on stderr and final JSON on stdout.

Bounded liveness does not prove physical process death or workflow progress. An owner
suspended, stalled or unable to write heartbeats for more than 300 seconds can be
recovered while physically alive, and may overlap recovery if it later returns.
A recent crash remains protected until heartbeat expiry; a second instance opened
before expiry needs reopening after expiry to offer it for resume. Backward clock
jumps conservatively delay recovery; large forward jumps can expire owners early.
Retained-worktree guards, including macOS `lsof` and Windows/Linux process checks,
remain mandatory at existing resume sites because subprocesses can outlive the TUI.

Close old instances before upgrading. Legacy NULL-owner rows retain orphan recovery,
and simultaneous old/new versions are unsupported. The live-owner guarantee covers
only ownership-recording versions. Multi-machine databases, remote cancellation,
cross-process provider capacity and a worker that outlives the TUI are outside scope.
Local Qdrant still supports only one process.
