"""Bounded application-instance liveness on one machine and one database.

Heartbeat timestamps are UTC wall-clock values; scheduling uses a monotonic wait.
An unended owner is live through the threshold, including future timestamps.
Backward clock jumps delay recovery and forward jumps can expire an owner early.
A stalled live process can be recovered and later overlap its replacement.
"""

HEARTBEAT_INTERVAL_SECONDS = 10
OWNER_STALE_AFTER_SECONDS = 300
