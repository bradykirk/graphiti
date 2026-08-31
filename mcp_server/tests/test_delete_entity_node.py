#!/usr/bin/env python3
"""Unit tests for the delete_entity_node MCP tool.

These tests mock the Graphiti client, so they need no live database.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

# Add the src directory to the path (mirrors the other unit tests)
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

import graphiti_mcp_server  # noqa: E402

UUID = '9586cab8-1401-4f0a-9c4d-29a5782c9d3c'


def _entity_node(name: str = 'Phase 4a', group_id: str = 'default'):
    """A stand-in EntityNode with the attributes the tool reads."""
    node = Mock()
    node.name = name
    node.group_id = group_id
    node.delete = AsyncMock()
    return node


@pytest.fixture
def mock_service():
    """Install a mock graphiti_service and restore the original afterwards."""
    driver = Mock()
    client = Mock()
    client.driver = driver

    service = Mock()
    service.get_client = AsyncMock(return_value=client)

    original = graphiti_mcp_server.graphiti_service
    graphiti_mcp_server.graphiti_service = service
    try:
        yield driver
    finally:
        graphiti_mcp_server.graphiti_service = original


class TestDeleteEntityNode:
    @pytest.mark.asyncio
    async def test_uninitialized_service_returns_error(self):
        original = graphiti_mcp_server.graphiti_service
        graphiti_mcp_server.graphiti_service = None
        try:
            result = await graphiti_mcp_server.delete_entity_node(UUID, confirm=True)
        finally:
            graphiti_mcp_server.graphiti_service = original

        assert 'not initialized' in result['error']

    @pytest.mark.asyncio
    async def test_deletes_the_node_when_confirmed(self, mock_service):
        driver = mock_service
        node = _entity_node()

        with patch.object(
            graphiti_mcp_server.EntityNode, 'get_by_uuid', AsyncMock(return_value=node)
        ) as get_by_uuid:
            result = await graphiti_mcp_server.delete_entity_node(UUID, confirm=True)

        get_by_uuid.assert_awaited_once_with(driver, UUID)
        node.delete.assert_awaited_once_with(driver)
        assert 'error' not in result
        assert UUID in result['message']

    @pytest.mark.asyncio
    async def test_dry_run_is_the_default_and_deletes_nothing(self, mock_service):
        node = _entity_node()
        edges = [Mock(), Mock(), Mock()]

        with (
            patch.object(
                graphiti_mcp_server.EntityNode, 'get_by_uuid', AsyncMock(return_value=node)
            ),
            patch.object(
                graphiti_mcp_server.EntityEdge,
                'get_by_node_uuid',
                AsyncMock(return_value=edges),
            ),
        ):
            result = await graphiti_mcp_server.delete_entity_node(UUID)

        node.delete.assert_not_awaited()
        assert 'not confirmed' in result['error']
        # The dry run reports what would be lost.
        assert 'Phase 4a' in result['error']
        assert '3 attached fact' in result['error']

    @pytest.mark.asyncio
    async def test_explicit_confirm_false_deletes_nothing(self, mock_service):
        node = _entity_node()

        with (
            patch.object(
                graphiti_mcp_server.EntityNode, 'get_by_uuid', AsyncMock(return_value=node)
            ),
            patch.object(
                graphiti_mcp_server.EntityEdge, 'get_by_node_uuid', AsyncMock(return_value=[])
            ),
        ):
            result = await graphiti_mcp_server.delete_entity_node(UUID, confirm=False)

        node.delete.assert_not_awaited()
        assert 'not confirmed' in result['error']

    @pytest.mark.asyncio
    async def test_missing_node_returns_error_without_deleting(self, mock_service):
        with patch.object(
            graphiti_mcp_server.EntityNode,
            'get_by_uuid',
            AsyncMock(side_effect=ValueError('node not found')),
        ):
            result = await graphiti_mcp_server.delete_entity_node(UUID, confirm=True)

        assert 'node not found' in result['error']
