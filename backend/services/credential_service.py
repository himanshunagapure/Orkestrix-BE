"""
CredentialService: store and retrieve per-project credentials encrypted in MongoDB.
Secrets are encrypted with AES-GCM using CRED_MASTER_KEY from .env.
"""

import base64
import logging
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv

from services.database import Database
from bson import ObjectId

logger = logging.getLogger(__name__)

CREDENTIALS_COLLECTION = "credentials"
DEFAULT_ENVIRONMENT = "default"


class CredentialService:
    """Store and retrieve project credentials (encrypted in MongoDB)."""

    def __init__(self) -> None:
        master_key_b64 = os.getenv("CRED_MASTER_KEY")
        if not master_key_b64:
            raise ValueError("CRED_MASTER_KEY not set in .env")
        try:
            self._master_key = base64.b64decode(master_key_b64)
        except Exception as e:
            raise ValueError(f"CRED_MASTER_KEY must be base64-encoded: {e}") from e
        if len(self._master_key) != 32:
            raise ValueError("CRED_MASTER_KEY must decode to 32 bytes (AES-256)")
        self._aesgcm = AESGCM(self._master_key)

    @classmethod
    def optional(cls) -> Optional["CredentialService"]:
        """
        Return a CredentialService instance if CRED_MASTER_KEY is set, else None.

        This method is safe to call at import time from routers. If the environment
        variable is not yet loaded, it will attempt to load a .env file from the
        backend directory before giving up.
        """
        # First check if the key is already present in environment
        if not os.getenv("CRED_MASTER_KEY"):
            # Try loading .env from the backend directory (parent of services/)
            backend_dir = Path(__file__).resolve().parent.parent
            env_path = backend_dir / ".env"
            if env_path.exists():
                load_dotenv(dotenv_path=env_path, override=False)

        if not os.getenv("CRED_MASTER_KEY"):
            return None

        try:
            return cls()
        except Exception as e:
            logger.warning("CredentialService initialization failed: %s", e)
            return None

    def _get_coll(self):
        return Database.get_collection(CREDENTIALS_COLLECTION)

    def _encrypt(self, plaintext: str) -> Dict[str, str]:
        plaintext_bytes = plaintext.encode("utf-8")
        iv = secrets.token_bytes(12)
        ciphertext_with_tag = self._aesgcm.encrypt(iv, plaintext_bytes, None)
        tag = ciphertext_with_tag[-16:]
        ciphertext_only = ciphertext_with_tag[:-16]
        return {
            "encrypted_value": base64.b64encode(ciphertext_only).decode("utf-8"),
            "iv": base64.b64encode(iv).decode("utf-8"),
            "tag": base64.b64encode(tag).decode("utf-8"),
        }

    def _decrypt(self, encrypted_data: Dict[str, str]) -> str:
        ciphertext = base64.b64decode(encrypted_data["encrypted_value"])
        iv = base64.b64decode(encrypted_data["iv"])
        tag = base64.b64decode(encrypted_data["tag"])
        full_ciphertext = ciphertext + tag
        plaintext_bytes = self._aesgcm.decrypt(iv, full_ciphertext, None)
        return plaintext_bytes.decode("utf-8")

    async def create_indexes(self) -> None:
        """Create unique and query indexes on credentials collection."""
        coll = self._get_coll()
        await coll.create_index(
            [("project_id", 1), ("name", 1), ("environment", 1)],
            unique=True,
        )
        await coll.create_index([("project_id", 1), ("environment", 1)])
        logger.info("CredentialService indexes created")

    async def store_credential(
        self,
        project_id: str,
        name: str,
        value: Optional[str],
        type: str,
        service: str,
        scope: str = "backend_only",
        environment: Optional[str] = None,
        user_id: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Store a credential (encrypted). Upserts by (project_id, name, environment)."""
        env = environment or DEFAULT_ENVIRONMENT
        now = datetime.utcnow()

        # Normalize ID fields to ObjectId when possible
        def _to_object_id(value: Optional[str]) -> Optional[ObjectId]:
            if not value:
                return None
            if isinstance(value, ObjectId):
                return value
            try:
                return ObjectId(value)
            except Exception:
                return None

        user_oid = _to_object_id(user_id)
        org_oid = _to_object_id(org_id)
        subscriber_oid = _to_object_id(subscriber_id)
        doc = {
            "project_id": project_id,
            "name": name,
            "type": type,
            "service": service,
            "scope": scope,
            "environment": env,
            "key_version": 1,
            "createdAt": now,
            "updatedAt": now,
            "userId": user_oid,
            "orgId": org_oid,
            "subscriberId": subscriber_oid
        }

        # FIX: Only encrypt if a value is actually provided
        if value is not None:
            encrypted = self._encrypt(value)
            doc.update({
                "encrypted_value": encrypted["encrypted_value"],
                "iv": encrypted["iv"],
                "tag": encrypted["tag"],
                "key_version": 1,
            })
        coll = self._get_coll()
        await coll.update_one(
            {"project_id": project_id, "name": name, "environment": env},
            {"$set": doc},
            upsert=True,
        )
        return {
            "project_id": project_id,
            "name": name,
            "type": type,
            "service": service,
            "scope": scope,
            "environment": env,
            "updatedAt": now,
        }

    async def get_credential_value(
        self,
        project_id: str,
        name: str,
        environment: Optional[str] = None,
    ) -> Optional[str]:
        """Retrieve and decrypt a credential value. Returns None if not found or decrypt fails."""
        env = environment or DEFAULT_ENVIRONMENT
        coll = self._get_coll()
        doc = await coll.find_one(
            {"project_id": project_id, "name": name, "environment": env}
        )
        if not doc:
            return None
        # Credentials stored with value=None have no encryption fields.
        if "encrypted_value" not in doc or "iv" not in doc or "tag" not in doc:
            logger.warning(
                "Credential %s/%s exists but has no encrypted value (stored without a value).",
                project_id,
                name,
            )
            return None
        try:
            plaintext = self._decrypt(
                {
                    "encrypted_value": doc["encrypted_value"],
                    "iv": doc["iv"],
                    "tag": doc["tag"],
                }
            )
            await coll.update_one(
                {"_id": doc["_id"]},
                {"$set": {"last_used_at": datetime.utcnow()}},
            )
            return plaintext
        except Exception as e:
            logger.error(
                "Failed to decrypt credential %s/%s: %s",
                project_id,
                name,
                e,
                exc_info=True,
            )
            return None

    async def list_credentials(
        self,
        project_id: str,
        environment: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        List credentials for a project.
        If environment is None, returns ALL credentials for the project.
        org_id and subscriber_id are optional; when provided they further narrow
        results to credentials that were stored with matching org/subscriber context.
        """
        coll = self._get_coll()

        query: Dict[str, Any] = {"project_id": project_id}
        if environment:
            query["environment"] = environment
        # Narrow by org/subscriber when provided so multi-tenant projects can
        # isolate their credentials without ambiguity.
        if org_id:
            try:
                query["orgId"] = ObjectId(org_id) if isinstance(org_id, str) else org_id
            except Exception:
                query["orgId"] = org_id
        if subscriber_id:
            try:
                query["subscriberId"] = ObjectId(subscriber_id) if isinstance(subscriber_id, str) else subscriber_id
            except Exception:
                query["subscriberId"] = subscriber_id

        cursor = coll.find(
            query,
            projection={"encrypted_value": 0, "iv": 0, "tag": 0} 
        )
        out: List[Dict[str, Any]] = []
        async for doc in cursor:
            created_at = doc.get("createdAt")
            updated_at = doc.get("updatedAt")
            out.append(
                {
                    "name": doc["name"],
                    "type": doc["type"],
                    "service": doc["service"],
                    "scope": doc.get("scope", "backend_only"),
                    "environment": doc.get("environment", DEFAULT_ENVIRONMENT),
                    "createdAt": created_at.isoformat() if created_at else None,
                    "updatedAt": updated_at.isoformat() if updated_at else None,
                }
            )
        return out

    async def delete_credential(
        self,
        project_id: str,
        name: str,
        environment: Optional[str] = None,
    ) -> bool:
        """Delete a credential. Returns True if a document was deleted."""
        env = environment or DEFAULT_ENVIRONMENT
        coll = self._get_coll()
        result = await coll.delete_one(
            {"project_id": project_id, "name": name, "environment": env}
        )
        return result.deleted_count > 0
