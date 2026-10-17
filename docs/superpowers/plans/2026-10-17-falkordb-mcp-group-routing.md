# Plan: route MCP group-scoped tools to the right FalkorDB graph

Status: implemented locally (2026-10-17), not pushed, not deployed. The adversarial plan review
was not run before implementation. Source: finding F3 of the Hermes GunMade pilot
(`~/hermes-graphiti-memory/docs/pilot-results-2026-10-17.md`).

## Problem

On FalkorDB each `group_id` is its own graph. `Graphiti.add_episode` re-binds the shared driver to
the last-written group (`graphiti_core/graphiti.py:1079-1082`). Core search is routed per group by
`handle_multiple_group_ids` (`graphiti_core/decorators.py`), but several MCP tools pass
`client.driver` directly, so they act on whatever graph was written last:

| Tool | Line | Effect on FalkorDB |
|---|---|---|
| `get_episodes` | `graphiti_mcp_server.py:820` | Lists the wrong graph. Seen live: an episode of `graphiti_isolation_a` stopped appearing after a write to another group. |
| `create_entity_node` | `:1094` | **Writes** the node into the wrong graph (its `group_id` property says one group, the graph is another). |
| `clear_graph(group_ids=…)` | `:1174` | Deletes `group_id IN $ids` in the wrong graph: usually a no-op, so the requested group is not cleared while the tool reports success. |

Neo4j is not affected (one database, `group_id` is a property filter).

## Change

1. Add a helper in `mcp_server/src/graphiti_mcp_server.py`:

   ```python
   def _driver_for_group(client, group_id: str):
       driver = client.driver
       if driver.provider == GraphProvider.FALKORDB:
           return driver.clone(database=group_id)
       return driver
   ```

   `FalkorDriver.clone` reuses the same connection; it does not reassign `client.driver`.

2. `get_episodes`: on FalkorDB, query each effective group with its own driver (concurrently),
   merge, sort by `created_at` descending (then `uuid` descending), and cut to `max_episodes`.
   On Neo4j keep the single query. Sorting by `created_at` instead of `uuid` is a behavior change
   only for multi-group FalkorDB calls, which are broken today.
   Also seen in the pilot: `EpisodicNode.get_by_group_ids` orders by `uuid DESC`, so even a
   single-group call does not return the newest episodes. Decision for review: sort by
   `created_at DESC` for all providers, since the tool documents "most recent episodes". A sort
   after a `LIMIT` by uuid is wrong, so this needs either `Graphiti.retrieve_episodes` (ordered by
   time, already routed per group by `handle_multiple_group_ids`) or a core query change. Prefer
   `retrieve_episodes` if its result set matches; the review should check that.
3. `create_entity_node`: `node.save(_driver_for_group(client, effective_group_id))`.
4. `clear_graph` with group ids: on FalkorDB call `clear_data(_driver_for_group(client, gid), [gid])`
   per group. The no-group-ids path (`clear_data(client.driver)` at `:1345`) is not changed.

No change to `graphiti_core`.

## Out of scope (stated limits)

- UUID-only tools (`get_entity_edge`, `delete_entity_edge`, `delete_entity_node`,
  `delete_episode`, `get_episode_entities`) have no group to route by on FalkorDB. A fix needs a
  `group_id` argument or a lookup across graphs: a separate API decision.
- `summarize_saga`: the saga lookup and core `Graphiti.summarize_saga` both use the shared
  driver. Fixing only the MCP lookup would split the two, so both stay for a separate change.
- The re-binding in `add_episode` itself (root cause) is shared state in core; changing it affects
  all callers and needs its own plan.

## Tests (`mcp_server/tests/test_falkordb_group_routing.py`, unit, no database)

Mock `graphiti_service.get_client()` with a client whose `driver.provider` is FALKORDB and whose
`clone(database=…)` returns per-group fakes:

- `get_episodes(group_ids=['a'])` after the shared driver points at `'b'` → queries the `'a'`
  clone only; returns the `'a'` episodes.
- `get_episodes(group_ids=['a','b'], max_episodes=3)` → both clones queried, merged, newest
  first, 3 results.
- `create_entity_node(group_id='a')` → `save` receives the `'a'` clone.
- `clear_graph(group_ids=['a','b'])` → `clear_data` called once per clone with its own id.
- Neo4j provider → `client.driver` used unchanged, no `clone` call.

Plus an opt-in live check in `test_live_falkordb_int.py`: write to group A, write to group B,
`get_episodes([A])` still returns the A episode.

## Rollout

- Commit on `main` of `bradykirk/graphiti`; CI builds `ghcr.io/bradykirk/graphiti-mcp:proxy-fix`.
- Deploy to Hetzner only with explicit approval. Rollback: redeploy the previous image digest
  (record it before the deploy).
- After the deploy, re-run the pilot crash-recovery duplicate check in `graphiti_isolation_a`.

## As built

- `get_episodes` calls `client.retrieve_episodes(reference_time=now, last_n=max_episodes,
  group_ids=…)`. On FalkorDB it calls it once per group, one after the other, because the FalkorDB
  async driver drops the connection on concurrent queries (AGENTS.md) and
  `handle_multiple_group_ids` would run the groups concurrently. It then sorts by `valid_at`, `created_at`, `uuid` (newest first, naive datetimes read
  as UTC) and cuts to `max_episodes`. Behavior change for all providers: order is by event time,
  not by uuid; episodes with `valid_at` in the future are not listed.
- `create_entity_node` and `clear_graph(group_ids)` use `_driver_for_group`.
- Tests: `mcp_server/tests/test_falkordb_group_routing.py` (8 tests; the FalkorDB tests fail on
  the old code). The live FalkorDB check is not written yet.
