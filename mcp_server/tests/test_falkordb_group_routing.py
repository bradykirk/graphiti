#!/usr/bin/env python3
"""Unit tests: MCP group-scoped tools use the graph of the requested group on FalkorDB.

On FalkorDB each group_id is its own graph and client.driver is re-bound to the
last group written. These tests mock the client, so they need no database.
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from graphiti_core.driver.driver import GraphProvider  # noqa: E402

import graphiti_mcp_server  # noqa: E402

T0 = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def _episode(uuid: str, group_id: str, minutes: int, naive: bool = False):
    ts = T0 + timedelta(minutes=minutes)
    if naive:
        ts = ts.replace(tzinfo=None)
    ep = Mock()
    ep.uuid = uuid
    ep.name = f'ep-{uuid}'
    ep.content = f'content {uuid}'
    ep.valid_at = ts
    ep.created_at = ts
    ep.source = Mock(value='text')
    ep.source_description = 'test'
    ep.group_id = group_id
    return ep


def _client(provider: GraphProvider, bound_to: str = 'other_group'):
    driver = Mock()
    driver.provider = provider
    driver._database = bound_to
    clones: dict[str, Mock] = {}

    def clone(database):
        clones.setdefault(database, Mock(name=f'driver-{database}', provider=provider))
        return clones[database]

    driver.clone = Mock(side_effect=clone)
    client = Mock()
    client.driver = driver
    client.embedder = Mock()
    client.clones = clones
    return client


@pytest.fixture
def install():
    original = graphiti_mcp_server.graphiti_service

    def _install(client):
        service = Mock()
        service.get_client = AsyncMock(return_value=client)
        graphiti_mcp_server.graphiti_service = service
        return client

    try:
        yield _install
    finally:
        graphiti_mcp_server.graphiti_service = original


class TestGetEpisodes:
    @pytest.mark.asyncio
    async def test_uses_retrieve_episodes_with_requested_groups(self, install):
        client = install(_client(GraphProvider.FALKORDB))
        client.retrieve_episodes = AsyncMock(return_value=[_episode('a1', 'group_a', 1)])

        result = await graphiti_mcp_server.get_episodes(group_ids=['group_a'], max_episodes=5)

        kwargs = client.retrieve_episodes.await_args.kwargs
        assert kwargs['group_ids'] == ['group_a']
        assert kwargs['last_n'] == 5
        assert [e['uuid'] for e in result['episodes']] == ['a1']

    @pytest.mark.asyncio
    async def test_falkordb_queries_groups_one_at_a_time_and_merges(self, install):
        client = install(_client(GraphProvider.FALKORDB))
        per_group = {
            'group_a': [_episode('a1', 'group_a', 1), _episode('a3', 'group_a', 3)],
            'group_b': [_episode('b2', 'group_b', 2, naive=True), _episode('b4', 'group_b', 4)],
        }
        active = 0
        max_active = 0

        async def retrieve(*, reference_time, last_n, group_ids):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            assert len(group_ids) == 1
            return per_group[group_ids[0]]

        client.retrieve_episodes = AsyncMock(side_effect=retrieve)

        result = await graphiti_mcp_server.get_episodes(
            group_ids=['group_a', 'group_b'], max_episodes=3
        )

        assert [c.kwargs['group_ids'] for c in client.retrieve_episodes.await_args_list] == [
            ['group_a'],
            ['group_b'],
        ]
        assert max_active == 1
        assert [e['uuid'] for e in result['episodes']] == ['b4', 'a3', 'b2']

    @pytest.mark.asyncio
    async def test_neo4j_queries_all_groups_in_one_call(self, install):
        client = install(_client(GraphProvider.NEO4J))
        client.retrieve_episodes = AsyncMock(return_value=[_episode('a1', 'group_a', 1)])

        await graphiti_mcp_server.get_episodes(group_ids=['group_a', 'group_b'])

        client.retrieve_episodes.assert_awaited_once()
        assert client.retrieve_episodes.await_args.kwargs['group_ids'] == ['group_a', 'group_b']

    @pytest.mark.asyncio
    async def test_does_not_query_shared_driver_directly(self, install):
        client = install(_client(GraphProvider.FALKORDB))
        client.retrieve_episodes = AsyncMock(return_value=[])
        with patch('graphiti_core.nodes.EpisodicNode.get_by_group_ids', new=AsyncMock()) as direct:
            await graphiti_mcp_server.get_episodes(group_ids=['group_a'])
        direct.assert_not_awaited()


class TestCreateEntityNode:
    @pytest.mark.asyncio
    async def test_falkordb_saves_into_group_graph(self, install):
        client = install(_client(GraphProvider.FALKORDB))
        with (
            patch.object(
                graphiti_mcp_server.EntityNode, 'generate_name_embedding', new=AsyncMock()
            ),
            patch.object(graphiti_mcp_server.EntityNode, 'save', new=AsyncMock()) as save,
        ):
            await graphiti_mcp_server.create_entity_node(name='Acme', group_id='group_a')

        assert save.await_args.args[0] is client.clones['group_a']

    @pytest.mark.asyncio
    async def test_neo4j_uses_shared_driver(self, install):
        client = install(_client(GraphProvider.NEO4J))
        with (
            patch.object(
                graphiti_mcp_server.EntityNode, 'generate_name_embedding', new=AsyncMock()
            ),
            patch.object(graphiti_mcp_server.EntityNode, 'save', new=AsyncMock()) as save,
        ):
            await graphiti_mcp_server.create_entity_node(name='Acme', group_id='group_a')

        assert save.await_args.args[0] is client.driver
        client.driver.clone.assert_not_called()


class TestClearGraph:
    @pytest.mark.asyncio
    async def test_falkordb_clears_each_group_in_its_own_graph(self, install):
        client = install(_client(GraphProvider.FALKORDB))
        with patch.object(graphiti_mcp_server, 'clear_data', new=AsyncMock()) as clear:
            await graphiti_mcp_server.clear_graph(group_ids=['group_a', 'group_b'])

        calls = [(c.args[0], c.kwargs['group_ids']) for c in clear.await_args_list]
        assert calls == [
            (client.clones['group_a'], ['group_a']),
            (client.clones['group_b'], ['group_b']),
        ]

    @pytest.mark.asyncio
    async def test_neo4j_clears_all_groups_with_one_call(self, install):
        client = install(_client(GraphProvider.NEO4J))
        with patch.object(graphiti_mcp_server, 'clear_data', new=AsyncMock()) as clear:
            await graphiti_mcp_server.clear_graph(group_ids=['group_a', 'group_b'])

        clear.assert_awaited_once_with(client.driver, group_ids=['group_a', 'group_b'])
