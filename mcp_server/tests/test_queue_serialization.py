#!/usr/bin/env python3
"""Unit tests: queued episodes of different groups never overlap.

Graphiti.add_episode switches the shared client driver to the episode's group graph
(FalkorDB), so two groups processed at the same time write into each other's graph.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from services.queue_service import QueueService  # noqa: E402


async def _drain(service: QueueService, groups: list[str]) -> None:
    for gid in groups:
        await asyncio.wait_for(service._episode_queues[gid].join(), timeout=5)


async def test_episodes_of_different_groups_do_not_overlap():
    service = QueueService()
    events: list[tuple[str, str]] = []
    active = 0
    max_active = 0

    def job(name: str):
        async def run():
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            events.append(('start', name))
            await asyncio.sleep(0.02)
            events.append(('end', name))
            active -= 1

        return run

    for i in range(3):
        await service.add_episode_task('boostmedia', job(f'boostmedia-{i}'))
        await service.add_episode_task('gunmade', job(f'gunmade-{i}'))
    await _drain(service, ['boostmedia', 'gunmade'])

    assert max_active == 1
    for i in range(0, len(events), 2):
        assert events[i][0] == 'start' and events[i + 1] == ('end', events[i][1])
    for gid in ('boostmedia', 'gunmade'):
        order = [n for kind, n in events if kind == 'start' and n.startswith(gid)]
        assert order == [f'{gid}-0', f'{gid}-1', f'{gid}-2']


async def test_failed_episode_releases_the_lock():
    service = QueueService()
    done: list[str] = []

    async def boom():
        raise RuntimeError('fictional extraction failure')

    async def ok():
        done.append('gunmade')

    await service.add_episode_task('boostmedia', boom)
    await service.add_episode_task('gunmade', ok)
    await _drain(service, ['boostmedia', 'gunmade'])

    assert done == ['gunmade']
    assert not service._process_lock.locked()
