# Plan: process queued episodes one at a time across groups (FalkorDB)

Status: implemented locally (2026-10-17); adversarial plan review not run before implementation.

## Problem (confirmed live 2026-10-17)

`Graphiti.add_episode` switches the shared client driver to the episode's group graph
(`graphiti_core/graphiti.py:1079-1082`: `self.driver = self.driver.clone(database=group_id)`,
`self.clients.driver = self.driver`) and every later step of that episode uses the shared driver.

`QueueService` runs one worker per group, so episodes of two groups run concurrently. At
22:10:30 UTC a `boostmedia` and a `gunmade` episode started in the same second. The gunmade
worker switched the shared driver while the boostmedia episode was in progress, so the boostmedia
episode was written to the `gunmade` graph with `group_id=boostmedia`. Both workers logged
"Successfully processed". The episode is invisible: a `boostmedia` read queries the boostmedia
graph, and a `gunmade` read filters `group_id=gunmade`. A resend at 22:27 (no concurrency)
worked.

Hermes now writes several groups (keyword routing, several profiles), so this loses memory
silently.

## Change

`mcp_server/src/services/queue_service.py`: one `asyncio.Lock` shared by all group workers;
each worker holds it while `process_func()` runs. Per-group queues, ordering and the error
handling stay. Episodes of different groups no longer overlap, so the shared driver is never
switched during an episode.

Cost: no parallelism across groups. Single user; episodes take 5–40 s; acceptable.

## Out of scope (noted)

- The root fix is in `graphiti_core`: `add_episode` should use a call-local driver instead of
  mutating `self.driver`. A larger upstream change.
- `add_triplet` (MCP tool) uses `self.driver`, which is whatever graph the last episode used; it
  is not queued, so it can also hit the wrong graph. Separate fix.
- Reads that run during an episode: the multi-group decorator clones per call; a single-group
  read compares against the current shared driver and can race. Low impact (transient wrong or
  empty result).
- The orphan data from the lost episode (nodes/edges with `group_id=boostmedia` inside the
  `gunmade` graph) stays; it is invisible to every scoped read.

## Tests

`mcp_server/tests/test_queue_serialization.py`: two groups, process functions that record
start/end and await; assert no overlap across groups, per-group order kept, a failing episode
releases the lock and the next one runs.
