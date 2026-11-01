import json
import logging
import os
import inspect
from typing import Any, Dict, List, Optional

import asyncio
import httpx
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient

load_dotenv()
logger = logging.getLogger(__name__)

_config_cache: Optional[Dict[str, Any]] = None


def _normalize_update_doc(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Accept plain field dicts or operator docs ($set, $unset, ...). Avoid double $set."""
    if not updates:
        return {"$set": {}}
    if (
        len(updates) == 1
        and "$set" in updates
        and isinstance(updates["$set"], dict)
        and len(updates["$set"]) == 1
        and "$set" in updates["$set"]
    ):
        return {"$set": updates["$set"]["$set"]}
    if any(str(key).startswith("$") for key in updates.keys()):
        return updates
    return {"$set": updates}


def _load_config() -> Dict[str, Any]:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    config_path = os.getenv("CONFIG_PATH", "config.json")
    try:
        with open(config_path, "r") as f:
            _config_cache = json.load(f)
        return _config_cache
    except Exception as e:
        logger.warning("Could not load config from %s: %s", config_path, e)
        _config_cache = {}
        return _config_cache


class Database:
    client: Optional[AsyncIOMotorClient] = None
    sync_client: Optional[MongoClient] = None
    db = None
    sync_db = None
    loop: Optional["asyncio.AbstractEventLoop"] = None

    # ── ArcadeDB (Enterprise Graph / Knowledge Graph) — sync HTTP for tools ──
    arcadedb_http_client_sync: Optional[httpx.Client] = None
    _arcadedb_known_databases: set = set()

    @classmethod
    def connect_db(cls, loop=None):
        mongodb_uri = os.getenv("MONGODB_URI")
        db_name = os.getenv("MONGODB_DB_NAME")
        if not mongodb_uri or not db_name:
            cfg = _load_config()
            mongodb_uri = mongodb_uri or cfg.get("devDbLink", "mongodb://localhost:27017")
            db_name = db_name or cfg.get("devDb", "saas-uat")

        cls.loop = loop
        if loop:
            cls.client = AsyncIOMotorClient(mongodb_uri, io_loop=loop)
        else:
            cls.client = AsyncIOMotorClient(mongodb_uri)
        cls.db = cls.client[db_name]
        logger.info("Connected to MongoDB: %s", db_name)

        try:
            cls.sync_client = MongoClient(mongodb_uri)
            cls.sync_db = cls.sync_client[db_name]
        except Exception as e:
            logger.warning("Could not initialize sync Mongo client: %s", e)

    @classmethod
    def close_db(cls):
        if cls.client:
            cls.client.close()
            logger.info("MongoDB connection closed")
        if cls.sync_client:
            try:
                cls.sync_client.close()
            except Exception:
                pass

    @classmethod
    def _get_subscriber_from_call_stack(cls) -> Optional[str]:
        try:
            frame = inspect.currentframe()
            while frame:
                if 'request_meta' in frame.f_locals:
                    meta = frame.f_locals['request_meta'] or {}
                    
                    sub_id = meta.get("subscriber_id")
                    if sub_id:
                        resolved_id = str(sub_id).strip()
                        logger.info("Subscriber ID '%s' found explicitly in request_meta", resolved_id)
                        return resolved_id

                    project_id = meta.get("project_id", "")
                    if not project_id:
                        frame = frame.f_back
                        continue

                    try:
                        if cls.sync_db is not None:
                            doc = cls.sync_db["Process_Configs"].find_one({
                                "$or": [
                                    {"agentic_process.projectId": project_id},
                                    {"agentic_process.workflow_id": project_id}
                                ]
                            })
                            if doc and doc.get("subscriberId"):
                                resolved_id = str(doc["subscriberId"]).strip()
                                logger.info("Subscriber ID '%s' resolved from Process_Configs using project_id '%s'", resolved_id, project_id)
                                return resolved_id
                            else:
                                logger.debug("Subscriber ID not found in Process_Configs for project_id '%s'", project_id)
                    except Exception as e:
                        logger.warning("Failed to query Process_Configs in stack inspector for project_id '%s': %s", project_id, e)

                    if "_sb_" in project_id:
                        parts = project_id.rsplit("_sb_", 1)
                        if len(parts) == 2 and (parts[1] or "").strip():
                            resolved_id = parts[1].strip()
                            logger.info("Subscriber ID '%s' parsed from legacy project_id '%s'", resolved_id, project_id)
                            return resolved_id
                            
                    logger.debug("Could not resolve subscriber ID from request_meta for project_id '%s'", project_id)

                frame = frame.f_back
        except Exception as e:
            logger.error("Error inspecting call stack for subscriber ID: %s", e)
        finally:
            del frame 
        return None

    @classmethod
    def _get_target_db_name(cls) -> str:
        base_db = os.environ.get("SUBSCRIBER_DB_NAME")
        if not base_db:
            base_db = os.getenv("MONGODB_DB_NAME") or _load_config().get("devDb")
            
        subscriber_id = cls._get_subscriber_from_call_stack()
        
        if subscriber_id and base_db:
            target_db = f"{base_db}_sb_{subscriber_id}"
            logger.debug("Database routing resolved to: '%s'", target_db)
            return target_db
        elif subscriber_id:
            logger.debug("Database routing resolved to: '%s'", subscriber_id)
            return subscriber_id
            
        logger.debug("Database routing falling back to base DB: '%s'", base_db)
        return base_db

    @classmethod
    def get_collection(cls, collection_name: str):
        if cls.db is None or cls.client is None:
            cls.connect_db(loop=cls.loop)
            
        target_db = cls._get_target_db_name()
        logger.info("Database.get_collection using DB '%s', collection '%s'", target_db, collection_name)
        return cls.client[target_db][collection_name]

    @classmethod
    def get_sync_collection(cls, collection_name: str):
        if cls.sync_db is None or cls.sync_client is None:
            cls.connect_db(loop=cls.loop)
            
        target_db = cls._get_target_db_name()
        logger.info("Database.get_sync_collection using DB '%s', collection '%s'", target_db, collection_name)
        return cls.sync_client[target_db][collection_name]

    @classmethod
    def _resolve_arcadedb_http_settings(cls) -> Dict[str, str]:
        """config.json is the primary source; ARCADEDB_* env vars are an optional override."""
        cfg = _load_config()
        return {
            "host": str(cfg.get("arcadeDbHost", "localhost")),
            "port": str(cfg.get("arcadeDbPort", "2480")),
            "dbname": str(cfg.get("arcadeDbName", "aiqod")),
            "user": str(cfg.get("arcadeDbUser", "root")),
            "password": str(cfg.get("arcadeDbPassword", "password")),
        }

    @classmethod
    def get_arcadedb_http_client_sync(cls) -> httpx.Client:
        """Lazily create and return the shared sync ArcadeDB HTTP client (tools are sync)."""
        if cls.arcadedb_http_client_sync is None:
            settings = cls._resolve_arcadedb_http_settings()
            base_url = f"http://{settings['host']}:{settings['port']}"
            cls.arcadedb_http_client_sync = httpx.Client(
                base_url=base_url,
                auth=(settings["user"], settings["password"]),
                timeout=httpx.Timeout(10.0, connect=3.0),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=10),
            )
            logger.info("Connected to ArcadeDB (sync HTTP) at %s", base_url)
        return cls.arcadedb_http_client_sync

    @classmethod
    def create_arcadedb_database_sync(cls, database: str) -> None:
        client = cls.get_arcadedb_http_client_sync()
        resp = client.post("/api/v1/server", json={"command": f"create database {database}"})
        resp.raise_for_status()
        logger.info("ArcadeDB database '%s' created", database)

    @classmethod
    def run_arcadedb_command_sync(
        cls,
        command: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        language: str = "cypher",
        database: Optional[str] = None,
        _retry: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Execute a Cypher/SQL command against ArcadeDB over its HTTP REST API — the
        SAME endpoint/payload contract as the async Database.run_arcadedb_command
        (used by backend functions and EnterpriseGraphRepository), just over a sync
        httpx.Client since LangChain BaseTool._run is synchronous.

        database defaults to this file's own subscriber routing (_get_target_db_name),
        same identity as get_sync_collection.
        """
        settings = cls._resolve_arcadedb_http_settings()
        db_name = database or cls._get_target_db_name() or settings["dbname"]
        client = cls.get_arcadedb_http_client_sync()
        logger.info("Database.run_arcadedb_command_sync using ArcadeDB db='%s'", db_name)

        payload: Dict[str, Any] = {"language": language, "command": command}
        if params:
            payload["params"] = params

        try:
            resp = client.post(f"/api/v1/command/{db_name}", json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            resp_text = exc.response.text.lower()
            not_found = (
                "does not exist" in resp_text
                or "is not available" in resp_text
                or "not found" in resp_text
                or "unknown database" in resp_text
                or exc.response.status_code == 404
            )
            logger.error(
                "Database.run_arcadedb_command_sync failed: db='%s' status=%s not_found=%s error='%s'",
                db_name, exc.response.status_code, not_found, exc.response.text,
            )
            if _retry and not_found and db_name not in cls._arcadedb_known_databases:
                logger.warning(
                    "Database.run_arcadedb_command_sync: db='%s' not found, creating it now", db_name,
                )
                cls.create_arcadedb_database_sync(db_name)
                logger.info(
                    "Database.run_arcadedb_command_sync: db='%s' created, retrying original command", db_name,
                )
                return cls.run_arcadedb_command_sync(
                    command, params, language=language, database=db_name, _retry=False
                )
            raise
        except Exception as exc:
            logger.error(
                "Database.run_arcadedb_command_sync failed: db='%s' unexpected error='%s'",
                db_name, str(exc), exc_info=True,
            )
            raise

        cls._arcadedb_known_databases.add(db_name)
        body = resp.json()
        result = body.get("result", [])
        rows: List[Dict[str, Any]] = []
        for item in result:
            if isinstance(item, dict) and "result" in item and isinstance(item["result"], dict):
                rows.append(item["result"])
            elif isinstance(item, dict):
                rows.append(item)
        return rows

    @classmethod
    async def insert_one(cls, collection_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        target_db = cls._get_target_db_name()
        try:
            coll = cls.get_collection(collection_name)
            result = await coll.insert_one(payload)
            inserted_id = str(result.inserted_id)
            logger.info(
                "Database.insert_one succeeded: db='%s', collection='%s', inserted_id='%s'",
                target_db,
                collection_name,
                inserted_id,
            )
            return {"inserted_id": inserted_id}
        except Exception as e:
            logger.error(
                "Database.insert_one failed: db='%s', collection='%s', error='%s'",
                target_db,
                collection_name,
                str(e),
                exc_info=True,
            )
            raise

    @classmethod
    async def update_one(
        cls,
        collection_name: str,
        filters: Dict[str, Any],
        updates: Dict[str, Any],
    ) -> Dict[str, Any]:
        target_db = cls._get_target_db_name()
        try:
            coll = cls.get_collection(collection_name)
            update_doc = _normalize_update_doc(updates or {})
            result = await coll.update_one(filters or {}, update_doc)
            logger.info(
                "Database.update_one succeeded: db='%s', collection='%s', matched=%s, modified=%s",
                target_db,
                collection_name,
                result.matched_count,
                result.modified_count,
            )
            return {"matched": result.matched_count, "modified": result.modified_count}
        except Exception as e:
            logger.error(
                "Database.update_one failed: db='%s', collection='%s', error='%s'",
                target_db,
                collection_name,
                str(e),
                exc_info=True,
            )
            raise

    @classmethod
    def _upsert_result(cls, result: Any) -> Dict[str, Any]:
        upserted = getattr(result, "upserted_id", None)
        return {
            "matched": int(getattr(result, "matched_count", 0) or 0),
            "modified": int(getattr(result, "modified_count", 0) or 0),
            "upserted_id": str(upserted) if upserted else None,
        }

    @classmethod
    async def upsert_one(
        cls,
        collection_name: str,
        filters: Dict[str, Any],
        updates: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Insert or update one document (upsert=True). Plain field dict; helper adds $set."""
        target_db = cls._get_target_db_name()
        try:
            coll = cls.get_collection(collection_name)
            update_doc = _normalize_update_doc(updates or {})
            result = await coll.update_one(filters or {}, update_doc, upsert=True)
            out = cls._upsert_result(result)
            logger.info(
                "Database.upsert_one succeeded: db='%s', collection='%s', matched=%s, modified=%s, upserted_id=%s",
                target_db,
                collection_name,
                out["matched"],
                out["modified"],
                out["upserted_id"],
            )
            return out
        except Exception as e:
            logger.error(
                "Database.upsert_one failed: db='%s', collection='%s', error='%s'",
                target_db,
                collection_name,
                str(e),
                exc_info=True,
            )
            raise

    @classmethod
    def upsert_one_sync(
        cls,
        collection_name: str,
        filters: Dict[str, Any],
        updates: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Sync upsert for LangChain BaseTool._run. Do not use get_sync_collection in generated code."""
        target_db = cls._get_target_db_name()
        try:
            coll = cls.get_sync_collection(collection_name)
            update_doc = _normalize_update_doc(updates or {})
            result = coll.update_one(filters or {}, update_doc, upsert=True)
            out = cls._upsert_result(result)
            logger.info(
                "Database.upsert_one_sync succeeded: db='%s', collection='%s', matched=%s, modified=%s, upserted_id=%s",
                target_db,
                collection_name,
                out["matched"],
                out["modified"],
                out["upserted_id"],
            )
            return out
        except Exception as e:
            logger.error(
                "Database.upsert_one_sync failed: db='%s', collection='%s', error='%s'",
                target_db,
                collection_name,
                str(e),
                exc_info=True,
            )
            raise

    @classmethod
    def insert_one_sync(cls, collection_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Sync insert for LangChain BaseTool._run (no await)."""
        target_db = cls._get_target_db_name()
        try:
            coll = cls.get_sync_collection(collection_name)
            result = coll.insert_one(payload)
            inserted_id = str(result.inserted_id)
            logger.info(
                "Database.insert_one_sync succeeded: db='%s', collection='%s', inserted_id='%s'",
                target_db,
                collection_name,
                inserted_id,
            )
            return {"inserted_id": inserted_id}
        except Exception as e:
            logger.error(
                "Database.insert_one_sync failed: db='%s', collection='%s', error='%s'",
                target_db,
                collection_name,
                str(e),
                exc_info=True,
            )
            raise

    @classmethod
    def update_one_sync(
        cls,
        collection_name: str,
        filters: Dict[str, Any],
        updates: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Sync update (not upsert) for LangChain BaseTool._run."""
        target_db = cls._get_target_db_name()
        try:
            coll = cls.get_sync_collection(collection_name)
            update_doc = _normalize_update_doc(updates or {})
            result = coll.update_one(filters or {}, update_doc)
            logger.info(
                "Database.update_one_sync succeeded: db='%s', collection='%s', matched=%s, modified=%s",
                target_db,
                collection_name,
                result.matched_count,
                result.modified_count,
            )
            return {"matched": result.matched_count, "modified": result.modified_count}
        except Exception as e:
            logger.error(
                "Database.update_one_sync failed: db='%s', collection='%s', error='%s'",
                target_db,
                collection_name,
                str(e),
                exc_info=True,
            )
            raise

    @classmethod
    def delete_one_sync(cls, collection_name: str, filters: Dict[str, Any]) -> Dict[str, Any]:
        """Sync delete for LangChain BaseTool._run (no await)."""
        target_db = cls._get_target_db_name()
        try:
            coll = cls.get_sync_collection(collection_name)
            result = coll.delete_one(filters or {})
            logger.info(
                "Database.delete_one_sync succeeded: db='%s', collection='%s', deleted=%s",
                target_db,
                collection_name,
                result.deleted_count,
            )
            return {"deleted": result.deleted_count}
        except Exception as e:
            logger.error(
                "Database.delete_one_sync failed: db='%s', collection='%s', error='%s'",
                target_db,
                collection_name,
                str(e),
                exc_info=True,
            )
            raise

    @classmethod
    def find_one_sync(
        cls,
        collection_name: str,
        filters: Optional[Dict[str, Any]] = None,
        projection: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Sync find_one for LangChain BaseTool._run (no await)."""
        target_db = cls._get_target_db_name()
        try:
            coll = cls.get_sync_collection(collection_name)
            if projection is not None:
                doc = coll.find_one(filters or {}, projection)
            else:
                doc = coll.find_one(filters or {})
            logger.info(
                "Database.find_one_sync succeeded: db='%s', collection='%s', found=%s",
                target_db,
                collection_name,
                doc is not None,
            )
            return doc
        except Exception as e:
            logger.error(
                "Database.find_one_sync failed: db='%s', collection='%s', error='%s'",
                target_db,
                collection_name,
                str(e),
                exc_info=True,
            )
            raise

    @classmethod
    async def delete_one(cls, collection_name: str, filters: Dict[str, Any]) -> Dict[str, Any]:
        target_db = cls._get_target_db_name()
        try:
            coll = cls.get_collection(collection_name)
            result = await coll.delete_one(filters or {})
            logger.info(
                "Database.delete_one succeeded: db='%s', collection='%s', deleted=%s",
                target_db,
                collection_name,
                result.deleted_count,
            )
            return {"deleted": result.deleted_count}
        except Exception as e:
            logger.error(
                "Database.delete_one failed: db='%s', collection='%s', error='%s'",
                target_db,
                collection_name,
                str(e),
                exc_info=True,
            )
            raise


db = Database()
