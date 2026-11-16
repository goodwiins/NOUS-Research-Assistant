"""GOO-487: the Kaggle LLM bulk path must not compute embeddings it discards.

The Qdrant vector store was retired, so ``_process_paper_with_llm`` had nowhere
to persist an embedding, yet it still called the Azure embedding API for every
paper (``enable_embeddings`` defaults to True), only to drop the vector.
"""

from unittest.mock import AsyncMock

import pytest

from src.services.ingestion.kaggle_llm_bulk_ingestion import (
    KaggleLLMBulkIngestionService,
)

pytestmark = pytest.mark.unit


async def test_processing_a_paper_does_not_generate_embeddings() -> None:
    service = KaggleLLMBulkIngestionService(
        enable_embeddings=True,
        enable_entity_extraction=False,
        enable_relationship_extraction=False,
    )
    generate = AsyncMock(return_value=[[0.1, 0.2]])
    service._generate_embeddings = generate  # type: ignore[method-assign]
    session = AsyncMock()

    result = await service._process_paper_with_llm(
        session,
        {"id": "2401.00001", "title": "A paper", "abstract": "An abstract."},
    )

    assert result["success"] is True
    generate.assert_not_awaited()
    assert service.stats["embeddings_generated"] == 0
