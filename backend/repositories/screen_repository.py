from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime
from models.screen_generation import ScreenGenerationRecord
from services.database import Database
from bson import ObjectId
from pymongo import ReturnDocument

class ScreenRepository:
    def __init__(self):
        self.collection_name = "screen_generations"
        self.collection = None

    def get_collection(self):
        """Get or initialize collection"""
        if self.collection is None:
            self.collection = Database.get_collection(self.collection_name)
        return self.collection

    def _not_deleted_filter(self) -> Dict[str, Any]:
        """Filter to exclude soft-deleted version docs (for rollback)."""
        return {"deleted": {"$ne": True}}

    _SCREEN_METADATA_KEYS = frozenset({
        "screen_name",
        "description",
        "tags",
        "screen_type",
        "permissions",
        "theme_preferences",
        "generationDescription",
        "intentDescription",
        "generation",
    })

    async def update_screen_metadata(
        self,
        project_id: str,
        screen_id: str,
        metadata: Dict[str, Any],
        *,
        version: Optional[str] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        Whitelisted $set on one screen_generations row (current pointer version, explicit version, or latest).

        Returns (updated_doc, None) on success, (None, error_code) on failure.
        error_code: not_found | no_allowed_fields
        """
        collection = self.get_collection()
        screens_coll = self._get_screens_collection()
        nd = self._not_deleted_filter()

        set_payload: Dict[str, Any] = {
            k: v for k, v in metadata.items() if k in self._SCREEN_METADATA_KEYS
        }
        if not set_payload:
            return None, "no_allowed_fields"

        set_payload["updated_at"] = datetime.utcnow()

        base = {"project_id": project_id, "screen_id": screen_id, **nd}
        filter_query: Dict[str, Any]
        if version:
            filter_query = {**base, "version": version}
        else:
            pointer = await screens_coll.find_one(
                {"screen_id": screen_id, "project_id": project_id}
            )
            cv = (pointer or {}).get("current_version")
            if cv:
                filter_query = {**base, "version": cv}
            else:
                latest = await collection.find_one(
                    base,
                    sort=[("version", -1), ("created_at", -1)],
                )
                if not latest:
                    return None, "not_found"
                filter_query = {"_id": latest["_id"]}

        updated = await collection.find_one_and_update(
            filter_query,
            {"$set": set_payload},
            return_document=ReturnDocument.AFTER,
        )
        if not updated:
            return None, "not_found"
        return updated, None

    async def create_screen_generation(
        self,
        prompt: str,
        ir_schema: Dict[str, Any],
        screen_spec: Dict[str, Any],
        components: List[Dict[str, Any]],
        screen_id: str,
        project_id: str,
        screen_name: str,
        description: Optional[str] = None,
        screen_type: Optional[str] = None,
        theme_preferences: Optional[Dict[str, str]] = None,
        permissions: Optional[Dict[str, Any]] = None,
        angular_metadata: Optional[Dict[str, Any]] = None,
        backend_metadata: Optional[Dict[str, Any]] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        original_prompt_enhanced: Optional[str] = None,
        v1_created_at: Optional[datetime] = None,
    ) -> str:
        """Create or update a screen generation record based on screen_id and project_id.
        If a record with the same screen_id and project_id exists, it will be updated.
        Otherwise, a new record will be created.
        """
        
        collection = self.get_collection()
        
        # Use prompt as description if no description provided
        description_to_save = description or prompt
        
        # Convert org_id, subscriber_id, user_id to ObjectId if provided
        org_id_obj = None
        subscriber_id_obj = None
        user_id_obj = None
        
        if org_id:
            try:
                org_id_obj = ObjectId(org_id) if isinstance(org_id, str) else org_id
            except Exception as e:
                print(f"Failed to convert org_id to ObjectId: {e}")
        
        if subscriber_id:
            try:
                subscriber_id_obj = ObjectId(subscriber_id) if isinstance(subscriber_id, str) else subscriber_id
            except Exception as e:
                print(f"Failed to convert subscriber_id to ObjectId: {e}")
        
        if user_id:
            try:
                user_id_obj = ObjectId(user_id) if isinstance(user_id, str) else user_id
            except Exception as e:
                print(f"Failed to convert user_id to ObjectId: {e}")
        
        # Check if record exists to preserve created_at and immutable v1 fields
        existing_record = await collection.find_one(
            {
                "screen_id": screen_id,
                "project_id": project_id,
            }
        )

        is_new = existing_record is None

        update_data: Dict[str, Any] = {
            "screen_id": screen_id,
            "project_id": project_id,
            "screen_name": screen_name,
            "description": description_to_save,  # Save description (falls back to prompt)
            "theme_preferences": theme_preferences,
            "ir_schema": ir_schema,
            "permissions": permissions,
            "angular": angular_metadata or {},
            "backend": backend_metadata or {},
            "orgId": org_id_obj,
            "subscriberId": subscriber_id_obj,
            "userId": user_id_obj,
            "updated_at": datetime.utcnow(),
            "version": "1.0",
            "status": "generated",
        }

        # Immutable v1 fields: only set on first creation
        if is_new:
            update_data["prompt"] = prompt  # original v1 prompt
            update_data["original_prompt_enhanced"] = original_prompt_enhanced or ""
            update_data["v1_created_at"] = v1_created_at or datetime.utcnow()
        else:
            # Preserve existing immutable fields if present
            if "prompt" in existing_record:
                update_data.setdefault("prompt", existing_record["prompt"])
            if "original_prompt_enhanced" in existing_record:
                update_data.setdefault("original_prompt_enhanced", existing_record["original_prompt_enhanced"])
            if "v1_created_at" in existing_record:
                update_data.setdefault("v1_created_at", existing_record["v1_created_at"])
        
        if existing_record:
            # Update existing record (preserve created_at)
            result = await collection.update_one(
                {"screen_id": screen_id, "project_id": project_id},
                {"$set": update_data}
            )
            # Return the existing _id
            return str(existing_record["_id"])
        else:
            # Insert new record with created_at
            update_data["created_at"] = datetime.utcnow()
            try:
                result = await collection.insert_one(update_data)
                return str(result.inserted_id)
            except Exception as e:
                # If insert fails due to duplicate key (unique index on screen_id),
                # check if it's the same project_id and update if so
                if "duplicate key" in str(e).lower() or "E11000" in str(e):
                    existing_record = await collection.find_one({"screen_id": screen_id})
                    if existing_record and existing_record.get("project_id") == project_id:
                        # Update the existing record with matching project_id
                        result = await collection.update_one(
                            {"screen_id": screen_id, "project_id": project_id},
                            {"$set": update_data}
                        )
                        return str(existing_record["_id"])
                # Re-raise if it's not a duplicate key error or project_id doesn't match
                raise

    async def get_screen_generation(self, screen_id: str) -> Optional[Dict[str, Any]]:
        """Get latest non-deleted screen generation by screen_id."""
        collection = self.get_collection()
        try:
            record = await collection.find_one(
                {"screen_id": screen_id, **self._not_deleted_filter()},
                sort=[("version", -1), ("created_at", -1)],
            )
            if record:
                record["_id"] = str(record["_id"])
            return record
        except Exception as e:
            print(f"Error retrieving record: {str(e)}")
            return None

    async def update_screen_generation(
        self,
        screen_id: str,
        update_data: Dict[str, Any]
    ) -> bool:
        """Update a screen generation record"""
        collection = self.get_collection()
        
        try:
            update_data["updated_at"] = datetime.utcnow()
            result = await collection.update_one(
                {"screen_id": screen_id},
                {"$set": update_data}
            )
            return result.modified_count > 0
        except Exception as e:
            print(f"Error updating record: {str(e)}")
            return False

    async def delete_screen_generation(self, screen_id: str) -> bool:
        """Delete a screen generation record"""
        collection = self.get_collection()
        
        try:
            result = await collection.delete_one({"screen_id": screen_id})
            return result.deleted_count > 0
        except Exception as e:
            print(f"Error deleting record: {str(e)}")
            return False

    async def list_screen_generations(
        self,
        skip: int = 0,
        limit: int = 10,
        filter_query: Optional[Dict[str, Any]] = None,
        sort_by: str = "created_at",
        sort_order: int = -1
    ) -> List[Dict[str, Any]]:
        """List screen generations with pagination and filtering"""
        collection = self.get_collection()
        
        query = filter_query or {}
        
        cursor = collection.find(query).sort(sort_by, sort_order).skip(skip).limit(limit)
        records = await cursor.to_list(length=limit)
        
        for record in records:
            record["_id"] = str(record["_id"])
        
        return records

    async def count_screen_generations(
        self,
        filter_query: Optional[Dict[str, Any]] = None
    ) -> int:
        """Count screen generations"""
        collection = self.get_collection()
        query = filter_query or {}
        return await collection.count_documents(query)

    async def search_by_prompt(
        self,
        search_term: str,
        skip: int = 0,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Search screen generations by prompt"""
        collection = self.get_collection()
        
        query = {"prompt": {"$regex": search_term, "$options": "i"}}
        cursor = collection.find(query).sort("created_at", -1).skip(skip).limit(limit)
        records = await cursor.to_list(length=limit)
        
        for record in records:
            record["_id"] = str(record["_id"])
        
        return records

    async def get_by_tags(
        self,
        tags: List[str],
        skip: int = 0,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Get screen generations by tags"""
        collection = self.get_collection()
        
        query = {"tags": {"$in": tags}}
        cursor = collection.find(query).sort("created_at", -1).skip(skip).limit(limit)
        records = await cursor.to_list(length=limit)
        
        for record in records:
            record["_id"] = str(record["_id"])
        
        return records

    async def update_status(
        self,
        screen_id: str,
        status: str
    ) -> bool:
        """Update status of a screen generation"""
        return await self.update_screen_generation(
            screen_id,
            {"status": status}
        )


    async def save_screen_build_metadata(
        self,
        screen_id: str,
        project_id: str,
        ir_schema: Dict[str, Any],
        angular_metadata: Optional[Dict[str, Any]] = None,
        backend_metadata: Optional[Dict[str, Any]] = None,
        version: Optional[str] = None,
        screen_name: Optional[str] = None,  
        prompt: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        chat_history_entry: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Save or update screen build metadata (IR schema, Angular, backend)"""
        collection = self.get_collection()
        
        # Convert org_id, subscriber_id, user_id to ObjectId if provided
        org_id_obj = None
        subscriber_id_obj = None
        user_id_obj = None
        
        if org_id:
            try:
                org_id_obj = ObjectId(org_id) if isinstance(org_id, str) else org_id
            except Exception as e:
                print(f"Failed to convert org_id to ObjectId: {e}")
        
        if subscriber_id:
            try:
                subscriber_id_obj = ObjectId(subscriber_id) if isinstance(subscriber_id, str) else subscriber_id
            except Exception as e:
                print(f"Failed to convert subscriber_id to ObjectId: {e}")
        
        if user_id:
            try:
                user_id_obj = ObjectId(user_id) if isinstance(user_id, str) else user_id
            except Exception as e:
                print(f"Failed to convert user_id to ObjectId: {e}")
        
        update_data: Dict[str, Any] = {
            "ir_schema": ir_schema,
            "updated_at": datetime.utcnow(),
        }
        if angular_metadata:
            update_data["angular"] = angular_metadata
        if backend_metadata:
            update_data["backend"] = backend_metadata
        if version:
            update_data["version"] = version
            update_data["build_status"] = "success"
        
        # --- NEW: Save name if provided (prompt is immutable v1 and managed by create_screen_generation) ---
        if screen_name:
            update_data["screen_name"] = screen_name
        # ---------------------------------------------

        # Optional: update chat history for this screen (used by update flows)
        filter_query: Dict[str, Any] = {"screen_id": screen_id, "project_id": project_id}
        if version:
            filter_query["version"] = version

        if chat_history_entry:
            try:
                existing_record = await collection.find_one(filter_query)
            except Exception as e:
                print(f"Failed to load existing record for chat_history: {e}")
                existing_record = None

            existing_chat = (existing_record or {}).get("chat_history") or {}
            max_messages = existing_chat.get("max_messages") or 10
            if not isinstance(max_messages, int) or max_messages <= 0:
                max_messages = 10

            existing_messages = existing_chat.get("messages") or []
            if not isinstance(existing_messages, list):
                existing_messages = []

            all_messages = existing_messages + [chat_history_entry]
            if len(all_messages) > max_messages:
                all_messages = all_messages[-max_messages:]

            update_data["chat_history"] = {
                "max_messages": max_messages,
                "messages": all_messages,
            }

        if org_id_obj is not None:
            update_data["orgId"] = org_id_obj
        if subscriber_id_obj is not None:
            update_data["subscriberId"] = subscriber_id_obj
        if user_id_obj is not None:
            update_data["userId"] = user_id_obj

        try:
            if version:
                result = await collection.update_one(filter_query, {"$set": update_data})
            else:
                # Legacy: update single doc (e.g. no version field yet); prefer latest non-deleted
                latest = await collection.find_one(
                    {"screen_id": screen_id, "project_id": project_id, **self._not_deleted_filter()},
                    sort=[("version", -1), ("created_at", -1)],
                )
                if latest:
                    result = await collection.update_one(
                        {"_id": latest["_id"]},
                        {"$set": update_data},
                    )
                else:
                    result = await collection.update_one(
                        {"screen_id": screen_id, "project_id": project_id},
                        {"$set": update_data},
                        upsert=True,
                    )
            return result.modified_count > 0 or result.upserted_id is not None
        except Exception as e:
            print(f"Error saving build metadata: {str(e)}")
            return False


    def _get_screens_collection(self):
        """Pointer collection for current_version per screen."""
        return Database.get_collection("screens")

    async def get_screen_state(
        self,
        project_id: str,
        screen_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get current screen state: use screens.current_version if set, else latest by version."""
        collection = self.get_collection()
        screens_coll = self._get_screens_collection()

        try:
            record = None
            pointer = await screens_coll.find_one({"screen_id": screen_id, "project_id": project_id})
            if pointer and pointer.get("current_version"):
                record = await collection.find_one({
                    "screen_id": screen_id,
                    "project_id": project_id,
                    "version": pointer["current_version"],
                    **self._not_deleted_filter(),
                })
            if record is None:
                # No pointer or version not found: use latest by version then created_at (exclude deleted)
                record = await collection.find_one(
                    {"screen_id": screen_id, "project_id": project_id, **self._not_deleted_filter()},
                    sort=[("version", -1), ("created_at", -1)],
                )
            if record:
                record["_id"] = str(record["_id"])
                return {
                    "current_ir_schema": record.get("ir_schema"),
                    "angular_workspace_directory": record.get("angular", {}).get("workspace_directory"),
                    "public_url": record.get("angular", {}).get("public_url"),
                    "angular_version": record.get("angular", {}).get("version"),
                    "backend": record.get("backend", {}),
                    "prompt": record.get("prompt"),
                    "screen_name": record.get("screen_name"),
                    "name": record.get("screen_name"),
                    "chat_history": record.get("chat_history"),
                    "version": record.get("version"),
                }
            return None
        except Exception as e:
            print(f"Error retrieving screen state: {str(e)}")
            return None

    async def get_latest_screen_doc(
        self, project_id: str, screen_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get the latest non-deleted version document (full doc) for cloning."""
        collection = self.get_collection()
        try:
            record = await collection.find_one(
                {"screen_id": screen_id, "project_id": project_id, **self._not_deleted_filter()},
                sort=[("version", -1), ("created_at", -1)],
            )
            return record
        except Exception as e:
            print(f"Error getting latest screen doc: {str(e)}")
            return None

    async def count_versions(self, project_id: str, screen_id: str) -> int:
        """Count non-deleted version documents for this screen (for version_label v1, v2, ...)."""
        collection = self.get_collection()
        return await collection.count_documents(
            {"screen_id": screen_id, "project_id": project_id, **self._not_deleted_filter()}
        )

    async def get_versions_newer_than(
        self, project_id: str, screen_id: str, target_version: str
    ) -> List[str]:
        """Return version strings strictly greater than target_version (for rollback cleanup)."""
        collection = self.get_collection()
        cursor = collection.find(
            {
                "screen_id": screen_id,
                "project_id": project_id,
                "version": {"$gt": target_version},
                **self._not_deleted_filter(),
            },
            projection={"version": 1},
        )
        docs = await cursor.to_list(length=None)
        versions = [d["version"] for d in docs if d.get("version")]
        return sorted(versions)

    async def mark_screen_versions_deleted(
        self, project_id: str, screen_id: str, versions: List[str]
    ) -> int:
        """Soft-delete version docs (when server has no delete access). Returns count updated."""
        if not versions:
            return 0
        collection = self.get_collection()
        result = await collection.update_many(
            {"screen_id": screen_id, "project_id": project_id, "version": {"$in": versions}},
            {"$set": {"deleted": True, "deleted_at": datetime.utcnow()}},
        )
        return result.modified_count

    async def delete_screen_versions(
        self, project_id: str, screen_id: str, versions: List[str]
    ) -> int:
        """Hard-delete version docs. Returns count deleted (may be 0 if no delete access)."""
        if not versions:
            return 0
        collection = self.get_collection()
        try:
            result = await collection.delete_many(
                {"screen_id": screen_id, "project_id": project_id, "version": {"$in": versions}}
            )
            return result.deleted_count
        except Exception as e:
            print(f"Error deleting screen versions: {e}")
            return 0

    async def insert_screen_version_doc(
        self,
        project_id: str,
        screen_id: str,
        version: str,
        version_label: str,
        doc: Dict[str, Any],
        *,
        build_status: str = "success",
        is_stable: bool = True,
    ) -> None:
        """Insert a new version document (clone or initial). doc must not contain _id; version/version_label/created_at are set here."""
        collection = self.get_collection()
        now = datetime.utcnow()
        doc = dict(doc)
        doc.pop("_id", None)
        doc["screen_id"] = screen_id
        doc["project_id"] = project_id
        doc["version"] = version
        doc["version_label"] = version_label
        doc["build_status"] = build_status
        doc["is_stable"] = is_stable
        doc["created_at"] = now
        doc["updated_at"] = now
        await collection.insert_one(doc)

    async def delete_screen_version(
        self, project_id: str, screen_id: str, version: str
    ) -> bool:
        """Remove a single version document (e.g. failed update v2)."""
        collection = self.get_collection()
        result = await collection.delete_one(
            {"screen_id": screen_id, "project_id": project_id, "version": version}
        )
        return result.deleted_count > 0

    async def update_screens_pointer(
        self,
        project_id: str,
        screen_id: str,
        current_version: str,
        *,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        subscriber_db_name: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        """Set screens pointer to current_version (and latest_version). Optionally store subscriber_db_name for fallback lookup.

        Note: `status` is tracked on individual documents in screen_generations, not in the screens pointer collection.
        """
        screens_coll = self._get_screens_collection()
        now = datetime.utcnow()
        update: Dict[str, Any] = {
            "screen_id": screen_id,
            "project_id": project_id,
            "current_version": current_version,
            "latest_version": current_version,
            "updatedAt": now,
        }
        if org_id is not None:
            try:
                update["orgId"] = ObjectId(org_id) if isinstance(org_id, str) else org_id
            except Exception:
                pass
        if subscriber_id is not None:
            try:
                update["subscriberId"] = ObjectId(subscriber_id) if isinstance(subscriber_id, str) else subscriber_id
            except Exception:
                pass
        if subscriber_db_name is not None:
            update["subscriberDbName"] = subscriber_db_name
        if user_id is not None:
            try:
                update["userId"] = ObjectId(user_id) if isinstance(user_id, str) else user_id
            except Exception:
                pass
        await screens_coll.update_one(
            {"screen_id": screen_id, "project_id": project_id},
            {
                "$setOnInsert": {"createdAt": now},
                "$set": update,
                # Clean up any legacy status field from previous versions
                "$unset": {"status": ""},
            },
            upsert=True,
        )

    async def create_indexes(self):
        """Create database indexes. Multiple docs per (project_id, screen_id) allowed (versioned)."""
        collection = self.get_collection()
        try:
            # Drop old unique index on screen_id so multiple versioned docs per screen are allowed
            try:
                await collection.drop_index("screen_id_1")
            except Exception:
                pass  # Index may not exist or already dropped
            await collection.create_index("prompt")
            await collection.create_index("created_at", background=True)
            await collection.create_index("status")
            await collection.create_index([("project_id", 1), ("screen_id", 1)])
            await collection.create_index(
                [("project_id", 1), ("screen_id", 1), ("version", 1)],
                background=True,
            )
            print("Database indexes created successfully")
        except Exception as e:
            print(f"Error creating indexes: {str(e)}")

    async def get_screen_versions(self, project_id: str, screen_id: str) -> List[Dict[str, Any]]:
        """Fetch a lightweight list of all successful versions for a screen."""
        collection = self.get_collection()
        cursor = collection.find(
            {
                "project_id": project_id, 
                "screen_id": screen_id, 
                "deleted": {"$ne": True},
                "status": {"$ne": "draft"}
            },
            {
                "version": 1, 
                "version_label": 1, 
                "created_at": 1, 
                "deployed_url": 1, 
                "preview_url": 1
            }
        ).sort([("version", -1), ("created_at", -1)])
        
        versions = await cursor.to_list(length=None)
        for v in versions:
            if '_id' in v:
                v['_id'] = str(v['_id'])
        return versions

    async def get_screen_version_data(self, project_id: str, screen_id: str, version: str) -> Optional[Dict[str, Any]]:
        """Fetch the full document snapshot for a specific version."""
        collection = self.get_collection()
        return await collection.find_one({
            "project_id": project_id,
            "screen_id": screen_id,
            "version": version,
            "deleted": {"$ne": True}
        })