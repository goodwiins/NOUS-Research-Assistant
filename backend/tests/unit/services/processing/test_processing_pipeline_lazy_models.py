"""GOO-642 / IS2B-1: building a ProcessingPipeline must not load models.

``get_processing_service`` builds a pipeline for every process/status/retry
request. The constructor used to build EntityExtractionService (spaCy load)
and AudioProcessingService (Whisper load) eagerly, on the event loop, even
though those routes never touch an extractor.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.services.processing import processing_service as mod


@pytest.mark.unit
def test_constructor_builds_no_extractors() -> None:
    with (
        patch.object(mod, "EntityExtractionService") as entity_cls,
        patch.object(mod, "AudioProcessingService") as audio_cls,
        patch.object(mod, "ImageProcessingService") as image_cls,
        patch.object(mod, "VideoProcessingService") as video_cls,
        patch("src.services.documents.file_service.FileService"),
    ):
        mod.get_processing_service(db=MagicMock())

    entity_cls.assert_not_called()
    audio_cls.assert_not_called()
    image_cls.assert_not_called()
    video_cls.assert_not_called()


@pytest.mark.unit
def test_extractor_is_built_once_on_first_use() -> None:
    with (
        patch.object(mod, "EntityExtractionService") as entity_cls,
        patch.object(mod, "AudioProcessingService") as audio_cls,
        patch("src.services.documents.file_service.FileService"),
    ):
        pipeline = mod.ProcessingPipeline(MagicMock())
        first = pipeline.entity_extractor
        second = pipeline.entity_extractor
        audio = pipeline.audio_processor

    assert first is second is entity_cls.return_value
    entity_cls.assert_called_once_with()
    assert audio is audio_cls.return_value


@pytest.mark.unit
def test_extractor_can_be_replaced_for_tests() -> None:
    with patch("src.services.documents.file_service.FileService"):
        pipeline = mod.ProcessingPipeline(MagicMock())
    stub = MagicMock()
    pipeline.entity_extractor = stub
    assert pipeline.entity_extractor is stub
