#!/usr/bin/env python3
"""Unit tests for the add_triplet MCP tool's valid_at / invalid_at passthrough.

These tests mock the Graphiti client, so they need no live database.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

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
    client.add_triplet = AsyncMock(return_value=Mock(nodes=[], edges=[]))

    service = Mock()
    service.get_client = AsyncMock(return_value=client)

    original = graphiti_mcp_server.graphiti_service
    graphiti_mcp_server.graphiti_service = service
    try:
        yield client
    finally:
        graphiti_mcp_server.graphiti_service = original


class TestAddTripletTemporalPassthrough:
    @pytest.mark.asyncio
    async def test_passes_valid_at_and_invalid_at_through_to_edge(self, mock_service):
        result = await graphiti_mcp_server.add_triplet(
            'Alice',
            'WORKS_FOR',
            'Alice worked for Acme from Jan to March',
            'Acme',
            group_id='default',
            valid_at='2026-01-01T00:00:00Z',
            invalid_at='2026-03-01T00:00:00Z',
        )

        assert 'error' not in result
        edge = mock_service.add_triplet.await_args.args[1]
        assert edge.valid_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert edge.invalid_at == datetime(2026, 3, 1, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_omitted_temporal_fields_default_to_none(self, mock_service):
        result = await graphiti_mcp_server.add_triplet(
            'Alice', 'KNOWS', 'Alice knows Bob', 'Bob', group_id='default'
        )

        assert 'error' not in result
        edge = mock_service.add_triplet.await_args.args[1]
        assert edge.valid_at is None
        assert edge.invalid_at is None

    @pytest.mark.asyncio
    async def test_unparseable_valid_at_returns_error(self, mock_service):
        result = await graphiti_mcp_server.add_triplet(
            'Alice',
            'KNOWS',
            'Alice knows Bob',
            'Bob',
            group_id='default',
            valid_at='not-a-date',
        )

        assert 'error' in result
        mock_service.add_triplet.assert_not_awaited()
