"""Small public feature probe for frontends deployed independently of the API."""

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

router = APIRouter(tags=["capabilities"])


class BackendCapabilities(BaseModel):
    draft_claims: bool = Field(serialization_alias="draftClaims")
    draft_release: bool = Field(serialization_alias="draftRelease")


@router.get("/api/v1/backend-capabilities", response_model=BackendCapabilities)
async def backend_capabilities(
    request: Request, response: Response
) -> BackendCapabilities:
    """Expose only whether the deployed API supports the draft panels."""
    response.headers["Cache-Control"] = "no-store"
    paths = request.app.openapi()["paths"]

    def has_operation(path: str, method: str) -> bool:
        return method in paths.get(path, {})

    project = "/api/v1/projects/{project_id}"
    draft = f"{project}/drafts/{{draft_id}}/versions/{{version}}"
    return BackendCapabilities(
        draft_claims=has_operation(f"{project}/claims", "get")
        and has_operation(f"{project}/claims/export", "get"),
        draft_release=has_operation(f"{draft}/release", "get")
        and has_operation(f"{draft}/promote", "post")
        and has_operation("/api/v1/research-engine/projects/{project_id}/roles", "get"),
    )
