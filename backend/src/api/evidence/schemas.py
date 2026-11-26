"""
Evidence Agreement Meter Pydantic schemas
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.services.evidence.schemas import (  # noqa: F401
    ConsensusLevel,
    EvidenceMeter,
    PublicationRetractionCheck,
    Stance,
)


class PublicationRetractionStatus(str, Enum):
    """Publication-level retraction status for an evidence source."""

    UNKNOWN = "unknown"


class StanceClassification(BaseModel):
    """Individual source stance classification"""

    model_config = ConfigDict(from_attributes=True)

    id: Optional[UUID] = None
    claim_hash: str = Field(..., description="SHA256 hash of normalized claim")
    source_id: UUID = Field(..., description="UUID of the source document")
    stance: Stance = Field(
        ..., description="Classification of source's stance on claim"
    )
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score (0-1)")
    justification_excerpt: Optional[str] = Field(
        None, description="Text excerpt justifying classification"
    )
    model_version: str = Field(..., description="Model version used for classification")
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class StanceBreakdownItem(BaseModel):
    """Stance classification with source metadata for breakdown view"""

    model_config = ConfigDict(from_attributes=True)

    source_id: UUID
    title: str
    stance: Stance
    confidence: float = Field(..., ge=0.0, le=1.0)
    justification_excerpt: Optional[str] = None
    is_retracted: bool = False
    publication_retraction_status: PublicationRetractionStatus = (
        PublicationRetractionStatus.UNKNOWN
    )


class EvidenceBreakdown(BaseModel):
    """Detailed breakdown of source stances"""

    model_config = ConfigDict(from_attributes=True)

    claim: str = Field(..., description="Original claim text")
    claim_hash: str = Field(..., description="SHA256 hash of normalized claim")
    sources: List[StanceBreakdownItem] = Field(
        ..., description="List of source classifications"
    )


# Request/Response schemas
class EvidenceMeterRequest(BaseModel):
    """Request parameters for evidence meter endpoint"""

    claim: str = Field(..., description="Claim to evaluate")
    source_ids: Optional[List[UUID]] = Field(
        None, description="Optional list of specific source IDs"
    )
    query_id: Optional[str] = Field(None, description="Optional query ID for context")


class EvidenceBreakdownRequest(BaseModel):
    """Request parameters for evidence breakdown endpoint"""

    claim_hash: str = Field(..., description="SHA256 hash of claim")
    stance_filter: Optional[Stance] = Field(
        None, description="Filter by specific stance"
    )


class ClassificationCacheInfo(BaseModel):
    """Cache information for stance classifications"""

    cache_key: str
    cached: bool
    cache_ttl: Optional[int] = None
    model_version: str
