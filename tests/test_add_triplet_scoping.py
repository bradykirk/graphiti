"""
Copyright 2024, Zep Software, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest

from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.driver.driver import GraphDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.errors import EdgeNotFoundError
from graphiti_core.graphiti import Graphiti
from graphiti_core.llm_client import LLMClient
from graphiti_core.nodes import EntityNode
from graphiti_core.search.search_filters import SearchFilters

pytest_plugins = ('pytest_asyncio', 'tests.helpers_test')

GROUP_ID = 'scoping_test_group'


def _make_graphiti(mock_embedder, mock_llm_client, mock_cross_encoder_client) -> Graphiti:
    """A Graphiti instance with a fake driver; every driver-touching call is mocked
    per-test, so no network or live database is needed."""
    return Graphiti(
        graph_driver=Mock(spec=GraphDriver),
        llm_client=mock_llm_client,
        embedder=mock_embedder,
        cross_encoder=mock_cross_encoder_client,
    )


@pytest.fixture
def mock_llm_client():
    mock_llm = Mock(spec=LLMClient)
    mock_llm.config = Mock()
    mock_llm.set_tracer = Mock()
    return mock_llm


@pytest.fixture
def mock_cross_encoder_client():
    return Mock(spec=CrossEncoderClient)


@pytest.mark.asyncio
async def test_existing_edges_candidates_are_scoped_to_the_endpoint_nodes(
    mock_embedder, mock_llm_client, mock_cross_encoder_client
):
    """add_triplet's contradiction-candidate search must be limited to edges already
    touching the source/target nodes, not the whole group.

    Before the fix, `existing_edges` used `SearchFilters()` with no restriction, so a
    new triplet write could put every semantically-similar fact in the group up for
    invalidation, including facts about entities the write never mentioned.
    """
    graphiti = _make_graphiti(mock_embedder, mock_llm_client, mock_cross_encoder_client)

    now = datetime.now()
    source = EntityNode(uuid='source-uuid', name='Alice', group_id=GROUP_ID, created_at=now)
    target = EntityNode(uuid='target-uuid', name='Acme', group_id=GROUP_ID, created_at=now)
    edge = EntityEdge(
        name='WORKS_FOR',
        fact='Alice works for Acme',
        group_id=GROUP_ID,
        source_node_uuid=source.uuid,
        target_node_uuid=target.uuid,
        created_at=now,
    )

    neighbor_of_source = EntityEdge(
        uuid='neighbor-edge-1',
        name='LIVES_IN',
        fact='Alice lives in Seattle',
        group_id=GROUP_ID,
        source_node_uuid=source.uuid,
        target_node_uuid='seattle-uuid',
        created_at=now,
    )
    neighbor_of_target = EntityEdge(
        uuid='neighbor-edge-2',
        name='HEADQUARTERED_IN',
        fact='Acme is headquartered in Denver',
        group_id=GROUP_ID,
        source_node_uuid=target.uuid,
        target_node_uuid='denver-uuid',
        created_at=now,
    )

    async def fake_get_by_node_uuid(driver, node_uuid):
        if node_uuid == source.uuid:
            return [neighbor_of_source]
        if node_uuid == target.uuid:
            return [neighbor_of_target]
        return []

    with (
        patch.object(
            EntityNode,
            'get_by_uuid',
            AsyncMock(
                side_effect=lambda _driver, uuid: {
                    source.uuid: source,
                    target.uuid: target,
                }[uuid]
            ),
        ),
        patch.object(EntityNode, 'generate_name_embedding', AsyncMock()),
        patch.object(EntityEdge, 'generate_embedding', AsyncMock()),
        patch.object(
            EntityEdge, 'get_by_uuid', AsyncMock(side_effect=EdgeNotFoundError(edge.uuid))
        ),
        patch.object(EntityEdge, 'get_between_nodes', AsyncMock(return_value=[])),
        patch.object(EntityEdge, 'get_by_node_uuid', AsyncMock(side_effect=fake_get_by_node_uuid)),
        patch(
            'graphiti_core.graphiti.search', AsyncMock(return_value=Mock(edges=[]))
        ) as mock_search,
        patch(
            'graphiti_core.graphiti.resolve_extracted_edge',
            AsyncMock(return_value=(edge, [], None)),
        ),
        patch('graphiti_core.graphiti.create_entity_edge_embeddings', AsyncMock()),
        patch('graphiti_core.graphiti.create_entity_node_embeddings', AsyncMock()),
        patch('graphiti_core.graphiti.add_nodes_and_edges_bulk', AsyncMock()),
    ):
        await graphiti.add_triplet(source, edge, target)

    # Two search calls happen: related_edges (already scoped via get_between_nodes),
    # then existing_edges (the one this fix scopes).
    assert mock_search.await_count == 2
    existing_edges_call = mock_search.await_args_list[1]
    existing_edges_filter: SearchFilters = existing_edges_call.kwargs['search_filter']

    assert existing_edges_filter.edge_uuids is not None, (
        'existing_edges search must be scoped with an edge_uuids filter, not left '
        'unrestricted across the whole group'
    )
    assert set(existing_edges_filter.edge_uuids) == {'neighbor-edge-1', 'neighbor-edge-2'}
