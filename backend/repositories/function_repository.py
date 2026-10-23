from typing import Dict, Any, Optional, List
from datetime import datetime
from bson import ObjectId
from services.database import Database


class FunctionRepository:
    """Repository for persisting and retrieving backend function artifacts."""

    def __init__(self):
        self.collection_name = "backend_functions"
        self.collection = None

    def get_collection(self):
        """Get or initialize collection"""
        if self.collection is None:
            self.collection = Database.get_collection(self.collection_name)
        return self.collection

    async def save_function(
        self,
        project_id: str,
        function_id: str,
        function_type: str,
        source_code: str,
        input_model_name: str,
        response_model_name: str,
        models_source: str,
        description: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        *,
        create_new_version: bool = True,
        version: Optional[int] = None,
        version_label: Optional[str] = None,
        screen_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Save a backend function artifact.
        
        - When create_new_version=True (default), this will INSERT a new document
          in the backend_functions collection with an incremented integer version:
              version: 1, 2, 3, ...
              version_label: \"v1\", \"v2\", ...
        - When create_new_version=False, the latest document for this
          (project_id, function_id) pair is updated in-place without bumping
          version. This is used mainly for reload/maintenance flows.
        """
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

        now = datetime.utcnow()

        # Helper: get latest version doc for this function (for rollback we keep multiple versions)
        async def _get_latest():
            return await collection.find_one(
                {"project_id": project_id, "function_id": function_id},
                sort=[("version", -1), ("created_at", -1)],
            )

        # Determine version number and label
        if create_new_version:
            if version is not None:
                try:
                    version_number = int(version)
                except (TypeError, ValueError):
                    version_number = 1
            else:
                latest = await _get_latest()
                if latest and isinstance(latest.get("version"), int):
                    version_number = latest["version"] + 1
                else:
                    raw_version = latest.get("version") if latest else None
                    try:
                        version_number = int(raw_version) + 1  # type: ignore[arg-type]
                    except Exception:
                        version_number = 1

            version_label_final = version_label or f"v{version_number}"

            # Insert new versioned document (unique on project_id + function_id + version for rollback)
            record = {
                "project_id": project_id,
                "function_id": function_id,
                "type": function_type,
                "source_code": source_code,
                "input_model_name": input_model_name,
                "response_model_name": response_model_name,
                "models_source": models_source,
                "description": description,
                "metadata": metadata or {},
                "orgId": org_id_obj,
                "subscriberId": subscriber_id_obj,
                "userId": user_id_obj,
                "created_at": now,
                "updated_at": now,
                "version": version_number,
                "version_label": version_label_final,
            }
            if screen_version is not None:
                record["screen_version"] = screen_version
            await collection.insert_one(record)
        else:
            # Update latest record in-place without bumping version
            latest = await _get_latest()
            if not latest:
                # No existing doc: insert one (upsert semantics for this path)
                version_number = 1
                version_label_final = "v1"
                record = {
                    "project_id": project_id,
                    "function_id": function_id,
                    "type": function_type,
                    "source_code": source_code,
                    "input_model_name": input_model_name,
                    "response_model_name": response_model_name,
                    "models_source": models_source,
                    "description": description,
                    "metadata": metadata or {},
                    "orgId": org_id_obj,
                    "subscriberId": subscriber_id_obj,
                    "userId": user_id_obj,
                    "created_at": now,
                    "updated_at": now,
                    "version": version_number,
                    "version_label": version_label_final,
                }
                await collection.insert_one(record)
            else:
                if isinstance(latest.get("version"), int):
                    version_number = latest["version"]
                    version_label_final = latest.get("version_label") or f"v{version_number}"
                else:
                    version_number = 1
                    version_label_final = "v1"
                update_fields = {
                    "type": function_type,
                    "source_code": source_code,
                    "input_model_name": input_model_name,
                    "response_model_name": response_model_name,
                    "models_source": models_source,
                    "description": description,
                    "metadata": metadata or {},
                    "orgId": org_id_obj,
                    "subscriberId": subscriber_id_obj,
                    "userId": user_id_obj,
                    "updated_at": now,
                    "version": version_number,
                    "version_label": version_label_final,
                }
                await collection.update_one(
                    {"_id": latest["_id"]},
                    {"$set": update_fields},
                )

        return {
            "function_id": function_id,
            "version": version_number,
            "version_label": version_label_final,
        }

    def _not_deleted_filter(self) -> Dict[str, Any]:
        """Filter to exclude soft-deleted function docs (for rollback)."""
        return {"deleted": {"$ne": True}}

    async def get_function(
        self, project_id: str, function_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get the latest non-deleted version of a function by project_id and function_id."""
        collection = self.get_collection()
        try:
            record = await collection.find_one(
                {"project_id": project_id, "function_id": function_id, **self._not_deleted_filter()},
                sort=[("version", -1), ("created_at", -1)],
            )
            if record:
                record["_id"] = str(record["_id"])
            return record
        except Exception as e:
            print(f"Error retrieving function: {str(e)}")
            return None

    async def mark_functions_deleted_by_screen_versions(
        self, project_id: str, screen_versions: List[str]
    ) -> int:
        """Soft-delete backend_functions docs that have screen_version in the given list (rollback)."""
        if not screen_versions:
            return 0
        collection = self.get_collection()
        try:
            result = await collection.update_many(
                {"project_id": project_id, "screen_version": {"$in": screen_versions}},
                {"$set": {"deleted": True, "deleted_at": datetime.utcnow()}},
            )
            return result.modified_count
        except Exception as e:
            print(f"Error marking functions deleted: {e}")
            return 0

    async def delete_functions_by_screen_versions(
        self, project_id: str, screen_versions: List[str]
    ) -> int:
        """Hard-delete backend_functions docs with screen_version in list. Returns count deleted."""
        if not screen_versions:
            return 0
        collection = self.get_collection()
        try:
            result = await collection.delete_many(
                {"project_id": project_id, "screen_version": {"$in": screen_versions}}
            )
            return result.deleted_count
        except Exception as e:
            print(f"Error deleting functions by screen versions: {e}")
            return 0

    async def get_project_functions(self, project_id: str) -> List[Dict[str, Any]]:
        """Get all functions for a project (latest non-deleted per function_id)."""
        collection = self.get_collection()
        try:
            cursor = collection.find({"project_id": project_id, **self._not_deleted_filter()})
            records = await cursor.to_list(length=None)
            for record in records:
                record["_id"] = str(record["_id"])
            return records
        except Exception as e:
            print(f"Error retrieving project functions: {str(e)}")
            return []

    async def find_function_by_id(self, function_id: str) -> Optional[Dict[str, Any]]:
        """Find the latest non-deleted function by function_id across all projects."""
        collection = self.get_collection()
        try:
            record = await collection.find_one(
                {"function_id": function_id, **self._not_deleted_filter()},
                sort=[("version", -1), ("created_at", -1)],
            )
            if record:
                record["_id"] = str(record["_id"])
            return record
        except Exception as e:
            print(f"Error searching for function by id: {str(e)}")
            return None

    async def delete_function(self, project_id: str, function_id: str) -> bool:
        """Delete a function artifact."""
        collection = self.get_collection()
        try:
            result = await collection.delete_one(
                {"project_id": project_id, "function_id": function_id}
            )
            return result.deleted_count > 0
        except Exception as e:
            print(f"Error deleting function: {str(e)}")
            return False

    async def delete_project_functions(self, project_id: str) -> int:
        """Delete all functions for a project."""
        collection = self.get_collection()
        try:
            result = await collection.delete_many({"project_id": project_id})
            return result.deleted_count
        except Exception as e:
            print(f"Error deleting project functions: {str(e)}")
            return 0

    async def save_error_logs(
            self,
            project_id: str,
            function_id: str,
            error_logs: Dict[str, Any],
        ) -> bool:
        """
        Save error logs for a function.
        Updates: Keeps only the latest 20 errors (newest first).
        """
        collection = self.get_collection()
        
        # --- LOG 1: Verify Input ---
        print(f"[DEBUG] Attempting to save logs for: Project={project_id}, Function={function_id}")
        
        try:
            # Ensure timestamp is set
            if "timestamp" not in error_logs:
                error_logs["timestamp"] = datetime.utcnow()
            
            # Update the function record with error logs
            result = await collection.update_one(
                {"project_id": project_id, "function_id": function_id},
                {
                    # UPDATED SECTION: Use $slice to limit to 3
                    "$push": {
                        "error_logs": {
                            "$each": [error_logs], 
                            "$position": 0,        
                            "$slice": 20            
                        }
                    },
                    "$set": {
                        "last_error": error_logs,  
                        "updated_at": datetime.utcnow(),
                    }
                },
                # SUGGESTION: You might want to temporarily enable upsert=True to force a write
                # upsert=True 
            )

            # --- LOG 2: Check Database Response ---
            print(f"[DEBUG] MongoDB Result: Matched={result.matched_count}, Modified={result.modified_count}")
            
            if result.matched_count == 0:
                 print(f"[ERROR] No document found for project_id={project_id} and function_id={function_id}")

            return result.modified_count > 0 or result.matched_count > 0
            
        except Exception as e:
            # --- LOG 3: Catch Hidden Errors (BSON, Connection, etc.) ---
            import traceback
            print(f"[CRITICAL ERROR] Failed to save error logs for function {function_id}")
            print(f"Exception: {str(e)}")
            traceback.print_exc() # Prints the full stack trace
            return False

    async def mark_last_error_llm_outcome(
        self,
        project_id: str,
        function_id: str,
        *,
        attempted: bool,
        successful: bool,
    ) -> bool:
        """Update the newest error_logs entry after an LLM auto-fix attempt."""
        collection = self.get_collection()
        now = datetime.utcnow()
        set_fields: Dict[str, Any] = {
            "last_error.llm_fix_attempted": attempted,
            "last_error.llm_fix_successful": successful,
            "error_logs.0.llm_fix_attempted": attempted,
            "error_logs.0.llm_fix_successful": successful,
            "updated_at": now,
        }
        if successful:
            set_fields["last_error.llm_fix_applied"] = True
            set_fields["last_error.resolved_at"] = now
            set_fields["error_logs.0.llm_fix_applied"] = True
            set_fields["error_logs.0.resolved_at"] = now
        try:
            result = await collection.update_one(
                {"project_id": project_id, "function_id": function_id},
                {"$set": set_fields},
            )
            return result.matched_count > 0
        except Exception as e:
            print(f"Error updating LLM outcome for function {function_id}: {str(e)}")
            return False

    async def clear_error_logs(
        self,
        project_id: str,
        function_id: str,
    ) -> bool:
        """
        Clear error logs for a function (e.g., after successful fix).
        
        Args:
            project_id: Project identifier
            function_id: Function identifier
        
        Returns:
            True if error logs were cleared successfully
        """
        collection = self.get_collection()
        try:
            result = await collection.update_one(
                {"project_id": project_id, "function_id": function_id},
                {
                    "$unset": {"error_logs": "", "last_error": ""},
                    "$set": {"updated_at": datetime.utcnow()}
                },
            )
            return result.modified_count > 0 or result.matched_count > 0
        except Exception as e:
            print(f"Error clearing error logs for function {function_id}: {str(e)}")
            return False

    async def create_indexes(self):
        """Create database indexes. Multiple versioned docs per (project_id, function_id) for rollback."""
        collection = self.get_collection()
        try:
            # Drop old unique index that allowed only one doc per (project_id, function_id)
            try:
                # Drop ALL legacy indexes that enforced uniqueness on only (project_id, function_id),
                # which blocks inserting multiple versions of the same function.
                legacy_indexes = [
                    "project_id_1_function_id_1",          # old default-named unique index
                    "project_id_function_id_unique",        # any alternate name that may exist
                ]
                existing = await collection.index_information()
                for name in legacy_indexes:
                    if name in existing:
                        await collection.drop_index(name)
                        print(f"Dropped legacy index: {name}")
            except Exception:
                pass  # Index may not exist or already dropped
                
            # Unique per (project_id, function_id, version) so we can store v1, v2, v3...
            await collection.create_index(
                [("project_id", 1), ("function_id", 1), ("version", 1)],
                unique=True,
                name="project_id_1_function_id_1_version_1_unique",
            )
            # Fast descending lookup for "get latest version" queries
            await collection.create_index(
                [("project_id", 1), ("function_id", 1), ("version", -1)],
                background=True,
                name="project_id_1_function_id_1_version_-1",
            )
            await collection.create_index("project_id", background=True)
            await collection.create_index("function_id", background=True)
            await collection.create_index("created_at", background=True)
            print("Function repository indexes created successfully")
        except Exception as e:
            print(f"Error creating function indexes: {str(e)}")

