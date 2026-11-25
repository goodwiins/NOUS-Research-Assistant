"""Evidence Agreement Meter types shared by the API router and the service layer.

Moved verbatim from ``src.api.evidence.schemas`` (GOO-503): the consensus
calculator (``consensus_calculator``) builds ``EvidenceMeter`` results from
``Stance`` counts, so these types must live where the service layer can import
them without inverting the dependency direction (services must never import
``src.api`` — enforced by ``tests/unit/test_import_direction.py``).
``src/api/evidence/schemas.py`` re-exports every name here, so
``from src.api.evidence.schemas import EvidenceMeter`` keeps resolving for
existing callers.

API-only schemas (stance classifications, breakdowns, request bodies) stay in
``src/api/evidence/schemas.py`` — nothing below the API layer needs them.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Stance(str, Enum):
    """Stance classification options"""

    SUPPORTING = "supporting"
    OPPOSING = "opposing"
    NEUTRAL = "neutral"
    NOT_ADDRESSED = "not_addressed"


class ConsensusLevel(str, Enum):
    """Consensus level categories"""

    STRONG_AGREEMENT = "strong_agreement"  # >80% agreement
    MODERATE_AGREEMENT = "moderate_agreement"  # 60-80% agreement
    MIXED = "mixed"  # 40-60% agreement
    LOW_AGREEMENT = "low_agreement"  # 20-40% agreement
    INSUFFICIENT_DATA = "insufficient_data"  # <3 sources


class PublicationRetractionCheck(str, Enum):
    """Publication-level retraction check state."""

    NOT_PERFORMED = "not_performed"


class EvidenceMeter(BaseModel):
    """Evidence agreement meter summary"""

    model_config = ConfigDict(from_attributes=True)

    claim: str = Field(..., description="Original claim text")
    claim_hash: str = Field(..., description="SHA256 hash of normalized claim")
    total_sources: int = Field(
        ..., ge=0, description="Total number of evaluated sources"
    )
    supporting: int = Field(
        ..., ge=0, description="Number of sources supporting the claim"
    )
    opposing: int = Field(..., ge=0, description="Number of sources opposing the claim")
    neutral: int = Field(..., ge=0, description="Number of neutral sources")
    not_addressed: int = Field(
        ..., ge=0, description="Number of sources not addressing the claim"
    )
    consensus_level: ConsensusLevel = Field(..., description="Overall consensus level")
    average_confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Average classification confidence"
    )
    retracted_sources: int = Field(
        ...,
        ge=0,
        description="Number of workspace-withdrawn sources excluded from classification",
    )
    publication_retraction_check: PublicationRetractionCheck = (
        PublicationRetractionCheck.NOT_PERFORMED
    )
    failed_sources: int = Field(
        0,
        ge=0,
        description=(
            "Number of sources whose stance could not be classified (provider "
            "error, timeout or ungrounded answer). A meter with failed sources "
            "is partial and is never cached."
        ),
    )
    cached: bool = Field(..., description="Whether result was served from cache")
    reproducibility_hash: str = Field(
        ..., description="Hash for reproducibility tracking"
    )
