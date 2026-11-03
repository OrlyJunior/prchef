"""Bounded application-instance liveness on one machine and one database.

Heartbeat timestamps are UTC wall-clock values; scheduling uses a monotonic wait.
An unended owner is live through the threshold, including future timestamps.
Backward clock jumps delay recovery and forward jumps can expire an owner early.
A recovered owner is fenced from execution writes and stops at the next safe boundary.
External calls already in flight cannot be undone; checks and calls are not atomic.
"""

HEARTBEAT_INTERVAL_SECONDS = 10
OWNER_STALE_AFTER_SECONDS = 300
