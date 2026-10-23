"""
Credentials API: create, list, and delete per-project credentials.
Secrets are stored encrypted via CredentialService; values are never returned in responses.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from services.credential_service import CredentialService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/aiqod-agent/agent/projects",
    tags=["Credentials"],
)

# Optional: instantiate only if CRED_MASTER_KEY is set
_credential_service: Optional[CredentialService] = CredentialService.optional()


def _get_credential_service() -> CredentialService:
    if _credential_service is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "credential_store_unavailable",
                "message": "CRED_MASTER_KEY is not set. Configure credentials storage in .env.",
            },
        )
    return _credential_service


# --- Request/Response models ---


class CredentialCreate(BaseModel):
    """Body for creating or updating a credential (upsert by name)."""
    name: str = Field(..., min_length=1, description="Unique name for this credential (e.g. github_token)")
    type: str = Field(..., description="e.g. bearer_token, api_key, http_header, basic_auth")
    service: str = Field("", description="Service label (e.g. GitHub, Stripe)")
    scope: str = Field("backend_only", description="backend_only or frontend_public")
    value: Optional[str] = Field(None, description="The secret value. Optional if updating metadata only.")
    environment: Optional[str] = Field(None, description="Optional environment; defaults to 'default'")


@router.post("/{project_id}/credentials")
async def create_or_update_credential(
    project_id: str,
    body: CredentialCreate,
    org_id: Optional[str] = Query(None, alias="orgId"),
    subscriber_id: Optional[str] = Query(None, alias="subscriberId"),
    user_id: Optional[str] = Query(None, alias="userId"),
) -> Dict[str, Any]:
    """
    Create or update a credential for the project (upsert by name).
    Value is encrypted at rest; only metadata is returned.
    """
    svc = _get_credential_service()
    meta = await svc.store_credential(
        project_id=project_id,
        name=body.name,
        value=body.value,
        type=body.type,
        service=body.service,
        scope=body.scope,
        environment=body.environment,
        user_id=user_id,
        org_id=org_id,
        subscriber_id=subscriber_id,
    )
    return {"success": True, "credential": meta}


@router.get("/{project_id}/credentials")
async def list_credentials(
    project_id: str,
    environment: Optional[str] = None,
) -> Dict[str, Any]:
    """List credential metadata for the project (no values)."""
    svc = _get_credential_service()
    items = await svc.list_credentials(project_id=project_id, environment=environment)
    return {"success": True, "credentials": items}


@router.delete("/{project_id}/credentials/{name}")
async def delete_credential(
    project_id: str,
    name: str,
    environment: Optional[str] = None,
) -> Dict[str, Any]:
    """Delete a credential by name."""
    svc = _get_credential_service()
    deleted = await svc.delete_credential(
        project_id=project_id,
        name=name,
        environment=environment,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail={"error": "credential_not_found", "message": f"No credential named '{name}' for this project."})
    return {"success": True, "message": f"Credential '{name}' deleted."}
