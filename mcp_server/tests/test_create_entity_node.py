#!/usr/bin/env python3
"""Unit tests for the create_entity_node MCP tool.

These tests mock the Graphiti client, so they need no live database.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

# Add the src directory to the path (mirrors the other unit tests)
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

import graphiti_mcp_server  # noqa: E402


@pytest.fixture
def mock_service():
    """Install a mock graphiti_service and restore the original afterwards."""
    driver = Mock()
    embedder = Mock()
    client = Mock()
    client.driver = driver
    client.embedder = embedder

    service = Mock()
    service.get_client = AsyncMock(return_value=client)

    original = graphiti_mcp_server.graphiti_service
    graphiti_mcp_server.graphiti_service = service
    try:
        yield client
    finally:
        graphiti_mcp_server.graphiti_service = original


def _patch_node_io():
    """Patch EntityNode.save/generate_name_embedding so no real I/O happens."""
    return (
        patch.object(graphiti_mcp_server.EntityNode, 'generate_name_embedding', AsyncMock()),
        patch.object(graphiti_mcp_server.EntityNode, 'save', AsyncMock()),
    )


class TestCreateEntityNode:
    @pytest.mark.asyncio
    async def test_uninitialized_service_returns_error(self):
        original = graphiti_mcp_server.graphiti_service
        graphiti_mcp_server.graphiti_service = None
        try:
            result = await graphiti_mcp_server.create_entity_node('Brady Kirkpatrick')
        finally:
            graphiti_mcp_server.graphiti_service = original

        assert 'not initialized' in result['error']

    @pytest.mark.asyncio
    async def test_creates_node_with_defaults(self, mock_service):
        gen_embedding, save = _patch_node_io()
        with gen_embedding as mock_gen_embedding, save as mock_save:
            result = await graphiti_mcp_server.create_entity_node(
                'Brady Kirkpatrick', group_id='default'
            )

        assert 'error' not in result
        node = result['node']
        assert node['name'] == 'Brady Kirkpatrick'
        assert node['group_id'] == 'default'
        assert node['labels'] == []
        mock_gen_embedding.assert_awaited_once_with(mock_service.embedder)
        mock_save.assert_awaited_once_with(mock_service.driver)

    @pytest.mark.asyncio
    async def test_creates_node_with_entity_type_summary_and_attributes(self, mock_service):
        gen_embedding, save = _patch_node_io()
        with gen_embedding, save:
            result = await graphiti_mcp_server.create_entity_node(
                'Graphiti',
                group_id='default',
                summary='A temporal knowledge graph framework.',
                entity_type='Procedure',
                attributes={'language': 'Python'},
            )

        assert 'error' not in result
        node = result['node']
        assert node['labels'] == ['Procedure']
        assert node['summary'] == 'A temporal knowledge graph framework.'
        assert node['attributes'] == {'language': 'Python'}

    @pytest.mark.asyncio
    async def test_missing_group_id_without_default_returns_error(self, mock_service):
        mock_config = Mock()
        mock_config.graphiti.group_id = None

        _unset = object()
        original_config = getattr(graphiti_mcp_server, 'config', _unset)
        graphiti_mcp_server.config = mock_config
        try:
            result = await graphiti_mcp_server.create_entity_node('Brady Kirkpatrick')
        finally:
            if original_config is _unset:
                del graphiti_mcp_server.config
            else:
                graphiti_mcp_server.config = original_config

        assert 'group_id' in result['error']
