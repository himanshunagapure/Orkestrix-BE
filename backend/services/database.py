import inspect
import json
import logging
import os
from typing import Any, Dict, List, Optional, Union

import asyncio
import httpx
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import AsyncMongoClient, MongoClient

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
    """Load config.json when env MONGODB_URI/MONGODB_DB_NAME are not set (used by connect_db)."""
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
    native_async_client: Optional[AsyncMongoClient] = None
    sync_client: Optional[MongoClient] = None
    db = None
    sync_db = None
    loop: Optional["asyncio.AbstractEventLoop"] = None
    _db_name: Optional[str] = None
    _sandbox_mode: bool = False

    # ── ArcadeDB (Enterprise Graph / Knowledge Graph) ─────────────────────
    arcadedb_http_client: Optional[httpx.AsyncClient] = None
    _arcadedb_known_databases: set = set()

    @classmethod
    def _resolve_arcadedb_http_settings(cls) -> Dict[str, str]:
        """
        config.json is the primary source (arcadeDbHost / arcadeDbPort /
        arcadeDbName / arcadeDbUser / arcadeDbPassword); ARCADEDB_* env vars
        are an optional override, same precedence as Mongo's devDbLink/devDb.
        """
        cfg = _load_config()
        host = cfg.get("arcadeDbHost", "localhost")
        port = cfg.get("arcadeDbPort", "2480")
        dbname = cfg.get("arcadeDbName", "aiqod")
        user = cfg.get("arcadeDbUser", "root")
        password = cfg.get("arcadeDbPassword", "password")


        return {
            "host": str(host),
            "port": str(port),
            "dbname": str(dbname),
            "user": str(user),
            "password": str(password),
        }

    @classmethod
    async def get_arcadedb_http_client(cls) -> httpx.AsyncClient:
        """Lazily create and return the shared ArcadeDB HTTP client."""
        if cls.arcadedb_http_client is None:
            settings = cls._resolve_arcadedb_http_settings()
            base_url = f"http://{settings['host']}:{settings['port']}"
            cls.arcadedb_http_client = httpx.AsyncClient(
                base_url=base_url,
                auth=(settings["user"], settings["password"]),
                timeout=httpx.Timeout(10.0, connect=3.0),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=10),
            )
            logger.info("Connected to ArcadeDB (HTTP) at %s", base_url)
        return cls.arcadedb_http_client

    @classmethod
    async def close_arcadedb_http_client(cls):
        if cls.arcadedb_http_client is not None:
            await cls.arcadedb_http_client.aclose()
            cls.arcadedb_http_client = None
            logger.info("ArcadeDB HTTP client closed")

    @classmethod
    async def run_arcadedb_command(
        cls,
        command: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        language: str = "cypher",
        database: Optional[str] = None,
        _retry: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Execute a Cypher/SQL command against ArcadeDB over its HTTP REST API.

        database defaults to SUBSCRIBER_DB_NAME (same identity used for the
        subscriber's MongoDB database) so generated code never has to pass
        it explicitly — same routing convention as Database.get_collection.
        """
        settings = cls._resolve_arcadedb_http_settings()
        db_name = database or os.getenv("SUBSCRIBER_DB_NAME") or settings["dbname"]
        client = await cls.get_arcadedb_http_client()
        logger.info("Database.run_arcadedb_command using ArcadeDB db='%s'", db_name)

        payload: Dict[str, Any] = {"language": language, "command": command}
        if params:
            payload["params"] = params

        try:
            resp = await client.post(f"/api/v1/command/{db_name}", json=payload)
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
                "Database.run_arcadedb_command failed: db='%s' status=%s not_found=%s error='%s'",
                db_name, exc.response.status_code, not_found, exc.response.text,
            )
            if _retry and not_found and db_name not in cls._arcadedb_known_databases:
                logger.warning(
                    "Database.run_arcadedb_command: db='%s' not found, creating it now", db_name,
                )
                await cls.create_arcadedb_database(db_name)
                logger.info(
                    "Database.run_arcadedb_command: db='%s' created, retrying original command", db_name,
                )
                return await cls.run_arcadedb_command(
                    command, params, language=language, database=db_name, _retry=False
                )
            raise
        except Exception as exc:
            logger.error(
                "Database.run_arcadedb_command failed: db='%s' unexpected error='%s'",
                db_name, str(exc), exc_info=True,
            )
            raise

        cls._arcadedb_known_databases.add(db_name)
        body = resp.json()
        # ArcadeDB wraps rows under result[].result (record) — normalize to
        # a flat list of dicts either way.
        result = body.get("result", [])
        rows: List[Dict[str, Any]] = []
        for item in result:
            if isinstance(item, dict) and "result" in item and isinstance(item["result"], dict):
                rows.append(item["result"])
            elif isinstance(item, dict):
                rows.append(item)
        return rows

    @classmethod
    async def create_arcadedb_database(cls, database: Optional[str] = None) -> Dict[str, Any]:
        """
        Create a new ArcadeDB database via the HTTP server-command endpoint
        (POST /api/v1/server), e.g. equivalent to:
            curl -u root:<password> -X POST http://<host>:2480/api/v1/server \
                 -d '{"command": "create database <dbname>"}'
        Safe to call if the database already exists (ArcadeDB will error;
        callers should catch and treat as non-fatal if desired).
        """
        settings = cls._resolve_arcadedb_http_settings()
        db_name = database or settings["dbname"]
        client = await cls.get_arcadedb_http_client()
        resp = await client.post(
            "/api/v1/server",
            json={"command": f"create database {db_name}"},
        )
        resp.raise_for_status()
        logger.info("ArcadeDB database '%s' created (or already existed)", db_name)
        return resp.json()



    @classmethod
    def _resolve_connection_settings(cls) -> tuple[str, str]:
        mongodb_uri = os.getenv("MONGODB_URI")
        db_name = os.getenv("MONGODB_DB_NAME")
        if not mongodb_uri or not db_name:
            cfg = _load_config()
            mongodb_uri = mongodb_uri or cfg.get("devDbLink", "mongodb://localhost:27017")
            db_name = db_name or cfg.get("devDb", "saas-uat")
        return mongodb_uri, db_name

    @classmethod
    def connect_db(cls, loop=None):
        """Connect to MongoDB. Uses MONGODB_URI/MONGODB_DB_NAME from env, or config.json (devDbLink/devDb) when env not set."""
        mongodb_uri, db_name = cls._resolve_connection_settings()
        cls._db_name = db_name

        # Sandbox subprocess: use PyMongo native async (no Motor thread pool / sync monitors).
        if os.getenv("SANDBOX_FUNCTION_WORKER") == "1":
            cls._sandbox_mode = True
            cls.loop = loop
            if cls.native_async_client is None:
                cls.native_async_client = AsyncMongoClient(
                    mongodb_uri,
                    serverMonitoringMode="poll",
                    maxPoolSize=10,
                )
            cls.db = cls.native_async_client[db_name]
            subscriber_db = os.getenv("SUBSCRIBER_DB_NAME")
            if subscriber_db:
                logger.info(
                    "Connected to MongoDB (sandbox native async); data DB=%s (main catalog=%s)",
                    subscriber_db,
                    db_name,
                )
            else:
                logger.info(
                    "Connected to MongoDB (sandbox native async); data DB=%s (no SUBSCRIBER_DB_NAME)",
                    db_name,
                )
            return

        cls._sandbox_mode = False
        cls.loop = loop
        if cls.client is None:
            cls.client = AsyncIOMotorClient(mongodb_uri)
        cls.db = cls.client[db_name]
        logger.info("Connected to MongoDB: %s", db_name)

        # Also initialize a sync client for code paths that cannot use `await`
        # (e.g. dynamically generated LangChain BaseTool._run methods).
        if cls.sync_client is None:
            try:
                cls.sync_client = MongoClient(mongodb_uri)
                cls.sync_db = cls.sync_client[db_name]
            except Exception as e:
                logger.warning("Could not initialize sync Mongo client: %s", e)

    @classmethod
    def close_db(cls):
        """Close MongoDB connection"""
        if cls.native_async_client:
            cls.native_async_client.close()
            cls.native_async_client = None
            logger.info("MongoDB native async connection closed")
        if cls.client:
            cls.client.close()
            cls.client = None
            logger.info("MongoDB connection closed")
        if cls.sync_client:
            try:
                cls.sync_client.close()
            except Exception:
                pass
            cls.sync_client = None
        cls.db = None
        cls.sync_db = None

    @classmethod
    def get_collection(cls, collection_name: str):
        """Get a collection from the database.

        - When SUBSCRIBER_DB_NAME is set (sandbox worker only), returns that DB's collection.
        - Otherwise returns the main DB collection.

        Notes:
        - SUBSCRIBER_DB_NAME is never read from a static .env file; it is set by the
          sandbox parent per request when spawning the worker.
        - If .env has no MONGODB_URI/MONGODB_DB_NAME, connect_db() falls back to
          config.json (devDbLink, devDb).
        """
        if cls.db is None and cls.native_async_client is None:
            cls.connect_db(loop=cls.loop)
        subscriber_db = os.getenv("SUBSCRIBER_DB_NAME")
        if cls._sandbox_mode or cls.native_async_client is not None:
            mongo = cls.native_async_client
            if subscriber_db:
                logger.info(
                    "Database.get_collection using subscriber DB '%s', collection '%s'",
                    subscriber_db,
                    collection_name,
                )
                return mongo[subscriber_db][collection_name]
            logger.info(
                "Database.get_collection using main DB, collection '%s'",
                collection_name,
            )
            return mongo[cls._db_name][collection_name]
        if subscriber_db:
            logger.info(
                "Database.get_collection using subscriber DB '%s', collection '%s'",
                subscriber_db,
                collection_name,
            )
            return cls.client[subscriber_db][collection_name]
        logger.info(
            "Database.get_collection using main DB, collection '%s'",
            collection_name,
        )
        return cls.db[collection_name]

    @classmethod
    def _ensure_sync_client(cls) -> None:
        """Initialize the private pymongo client (sandbox connect_db skips it)."""
        if cls.sync_client is not None and cls.sync_db is not None:
            return
        mongodb_uri, db_name = cls._resolve_connection_settings()
        if cls._db_name is None:
            cls._db_name = db_name
        if cls.sync_client is None:
            cls.sync_client = MongoClient(mongodb_uri)
        cls.sync_db = cls.sync_client[cls._db_name or db_name]

    @classmethod
    def _sync_collection(cls, collection_name: str):
        """Private sync handle. Generated code must not call get_sync_collection."""
        cls._ensure_sync_client()
        subscriber_db = os.getenv("SUBSCRIBER_DB_NAME")
        if subscriber_db:
            return cls.sync_client[subscriber_db][collection_name]
        return cls.sync_db[collection_name]

    @classmethod
    def get_sync_collection(cls, collection_name: str):
        """
        Get a *synchronous* pymongo collection.

        Generated sandbox/tool code must NOT call this (Issue 10). Use
        ``await Database.upsert_one`` (async) or ``Database.upsert_one_sync``
        from LangChain ``BaseTool._run``. Kept for legacy tools only.
        """
        logger.warning(
            "Database.get_sync_collection(%r) is deprecated for generated code; "
            "use Database.upsert_one / upsert_one_sync",
            collection_name,
        )
        return cls._sync_collection(collection_name)

    @classmethod
    async def insert_one(cls, collection_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Insert one document into the given collection. Used by generated backend functions (e.g. upload_pdf)."""
        db_name = os.getenv("SUBSCRIBER_DB_NAME") or os.getenv("MONGODB_DB_NAME") or _load_config().get("devDb")
        try:
            coll = cls.get_collection(collection_name)
            result = await coll.insert_one(payload)
            inserted_id = str(result.inserted_id)
            logger.info(
                "Database.insert_one succeeded: db='%s', collection='%s', inserted_id='%s'",
                db_name,
                collection_name,
                inserted_id,
            )
            return {"inserted_id": inserted_id}
        except Exception as e:
            logger.error(
                "Database.insert_one failed: db='%s', collection='%s', error='%s'",
                db_name,
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
        """Update one matching document. Used by generated backend functions.

        This is not upsert. Motor/PyMongo ``update_one`` is called without
        ``upsert=True``, so zero matched = zero write and no error. For
        insert-if-new, use ``insert_one``. Only call ``update_one`` when the
        filter is guaranteed to match an existing document.
        """
        db_name = os.getenv("SUBSCRIBER_DB_NAME") or os.getenv("MONGODB_DB_NAME") or _load_config().get("devDb")
        try:
            coll = cls.get_collection(collection_name)
            update_doc = _normalize_update_doc(updates or {})
            result = await coll.update_one(filters or {}, update_doc)
            logger.info(
                "Database.update_one succeeded: db='%s', collection='%s', matched=%s, modified=%s",
                db_name,
                collection_name,
                result.matched_count,
                result.modified_count,
            )
            return {"matched": result.matched_count, "modified": result.modified_count}
        except Exception as e:
            logger.error(
                "Database.update_one failed: db='%s', collection='%s', error='%s'",
                db_name,
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
        """Insert or update one document (``upsert=True``).

        Pass a plain field dict — this helper wraps ``$set`` the same way as
        ``update_one``. Do not pass ``{"$set": fields}`` unless you intend
        operator form. Async sandbox / UI functions should ``await`` this
        instead of ``Database.get_sync_collection().update_one(..., upsert=True)``.
        """
        db_name = os.getenv("SUBSCRIBER_DB_NAME") or os.getenv("MONGODB_DB_NAME") or _load_config().get("devDb")
        try:
            coll = cls.get_collection(collection_name)
            update_doc = _normalize_update_doc(updates or {})
            result = await coll.update_one(filters or {}, update_doc, upsert=True)
            out = cls._upsert_result(result)
            logger.info(
                "Database.upsert_one succeeded: db='%s', collection='%s', matched=%s, modified=%s, upserted_id=%s",
                db_name,
                collection_name,
                out["matched"],
                out["modified"],
                out["upserted_id"],
            )
            return out
        except Exception as e:
            logger.error(
                "Database.upsert_one failed: db='%s', collection='%s', error='%s'",
                db_name,
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
        """Sync upsert for LangChain ``BaseTool._run`` (no ``await``).

        Same contract as :meth:`upsert_one`. Generated tools must call this
        instead of ``get_sync_collection`` + raw ``update_one(..., upsert=True)``.
        """
        db_name = os.getenv("SUBSCRIBER_DB_NAME") or os.getenv("MONGODB_DB_NAME") or _load_config().get("devDb")
        try:
            coll = cls._sync_collection(collection_name)
            update_doc = _normalize_update_doc(updates or {})
            result = coll.update_one(filters or {}, update_doc, upsert=True)
            out = cls._upsert_result(result)
            logger.info(
                "Database.upsert_one_sync succeeded: db='%s', collection='%s', matched=%s, modified=%s, upserted_id=%s",
                db_name,
                collection_name,
                out["matched"],
                out["modified"],
                out["upserted_id"],
            )
            return out
        except Exception as e:
            logger.error(
                "Database.upsert_one_sync failed: db='%s', collection='%s', error='%s'",
                db_name,
                collection_name,
                str(e),
                exc_info=True,
            )
            raise

    @classmethod
    def insert_one_sync(cls, collection_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Sync insert for LangChain ``BaseTool._run`` (no ``await``)."""
        db_name = os.getenv("SUBSCRIBER_DB_NAME") or os.getenv("MONGODB_DB_NAME") or _load_config().get("devDb")
        try:
            coll = cls._sync_collection(collection_name)
            result = coll.insert_one(payload)
            inserted_id = str(result.inserted_id)
            logger.info(
                "Database.insert_one_sync succeeded: db='%s', collection='%s', inserted_id='%s'",
                db_name,
                collection_name,
                inserted_id,
            )
            return {"inserted_id": inserted_id}
        except Exception as e:
            logger.error(
                "Database.insert_one_sync failed: db='%s', collection='%s', error='%s'",
                db_name,
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
        """Sync update (not upsert) for LangChain ``BaseTool._run``."""
        db_name = os.getenv("SUBSCRIBER_DB_NAME") or os.getenv("MONGODB_DB_NAME") or _load_config().get("devDb")
        try:
            coll = cls._sync_collection(collection_name)
            update_doc = _normalize_update_doc(updates or {})
            result = coll.update_one(filters or {}, update_doc)
            logger.info(
                "Database.update_one_sync succeeded: db='%s', collection='%s', matched=%s, modified=%s",
                db_name,
                collection_name,
                result.matched_count,
                result.modified_count,
            )
            return {"matched": result.matched_count, "modified": result.modified_count}
        except Exception as e:
            logger.error(
                "Database.update_one_sync failed: db='%s', collection='%s', error='%s'",
                db_name,
                collection_name,
                str(e),
                exc_info=True,
            )
            raise

    @classmethod
    def delete_one_sync(cls, collection_name: str, filters: Dict[str, Any]) -> Dict[str, Any]:
        """Sync delete for LangChain ``BaseTool._run`` (no ``await``)."""
        db_name = os.getenv("SUBSCRIBER_DB_NAME") or os.getenv("MONGODB_DB_NAME") or _load_config().get("devDb")
        try:
            coll = cls._sync_collection(collection_name)
            result = coll.delete_one(filters or {})
            logger.info(
                "Database.delete_one_sync succeeded: db='%s', collection='%s', deleted=%s",
                db_name,
                collection_name,
                result.deleted_count,
            )
            return {"deleted": result.deleted_count}
        except Exception as e:
            logger.error(
                "Database.delete_one_sync failed: db='%s', collection='%s', error='%s'",
                db_name,
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
        """Sync find_one for LangChain ``BaseTool._run`` (no ``await``)."""
        db_name = os.getenv("SUBSCRIBER_DB_NAME") or os.getenv("MONGODB_DB_NAME") or _load_config().get("devDb")
        try:
            coll = cls._sync_collection(collection_name)
            if projection is not None:
                doc = coll.find_one(filters or {}, projection)
            else:
                doc = coll.find_one(filters or {})
            logger.info(
                "Database.find_one_sync succeeded: db='%s', collection='%s', found=%s",
                db_name,
                collection_name,
                doc is not None,
            )
            return doc
        except Exception as e:
            logger.error(
                "Database.find_one_sync failed: db='%s', collection='%s', error='%s'",
                db_name,
                collection_name,
                str(e),
                exc_info=True,
            )
            raise

    @classmethod
    async def delete_one(cls, collection_name: str, filters: Dict[str, Any]) -> Dict[str, Any]:
        """Delete one document. Used by generated backend functions."""
        db_name = os.getenv("SUBSCRIBER_DB_NAME") or os.getenv("MONGODB_DB_NAME") or _load_config().get("devDb")
        try:
            coll = cls.get_collection(collection_name)
            result = await coll.delete_one(filters or {})
            logger.info(
                "Database.delete_one succeeded: db='%s', collection='%s', deleted=%s",
                db_name,
                collection_name,
                result.deleted_count,
            )
            return {"deleted": result.deleted_count}
        except Exception as e:
            logger.error(
                "Database.delete_one failed: db='%s', collection='%s', error='%s'",
                db_name,
                collection_name,
                str(e),
                exc_info=True,
            )
            raise

    @classmethod
    async def resolve_cursor(cls, maybe_cursor_or_coro: Any) -> Any:
        """Normalize Motor vs PyMongo-native-async cursor returns.

        - Motor ``collection.aggregate(...)`` returns a cursor immediately.
        - PyMongo ``AsyncCollection.aggregate(...)`` (sandbox worker) returns a
          coroutine that must be awaited to get ``AsyncCommandCursor``.
        - ``collection.find(...)`` returns a cursor immediately in both drivers.
        """
        if inspect.isawaitable(maybe_cursor_or_coro):
            return await maybe_cursor_or_coro
        return maybe_cursor_or_coro

    @classmethod
    async def cursor_to_list(
        cls,
        maybe_cursor_or_coro: Any,
        length: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Materialize a find/aggregate cursor safely under Motor and sandbox async.

        Prefer this over ``await collection.aggregate(pipeline).to_list(...)``,
        which fails in the sandbox because ``aggregate`` is a coroutine there.
        """
        cursor = await cls.resolve_cursor(maybe_cursor_or_coro)
        if hasattr(cursor, "to_list"):
            return await cursor.to_list(length=length)
        docs: List[Dict[str, Any]] = []
        async for doc in cursor:
            docs.append(doc)
            if length is not None and len(docs) >= length:
                break
        return docs

    @classmethod
    async def aggregate_to_list(
        cls,
        collection_name: str,
        pipeline: List[Dict[str, Any]],
        length: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Run an aggregation and return documents (Motor + sandbox safe)."""
        coll = cls.get_collection(collection_name)
        return await cls.cursor_to_list(coll.aggregate(pipeline or []), length=length)

    @classmethod
    async def find_one(
        cls,
        collection_name: str,
        filters: Optional[Dict[str, Any]] = None,
        projection: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Fetch one document (Motor + sandbox native-async).

        Generated backend functions analogize ``Database.insert_one`` into
        ``Database.find_one``. That class method must exist — ``Database.find``
        does not (use ``find_to_list`` for multi-doc reads).
        """
        db_name = os.getenv("SUBSCRIBER_DB_NAME") or os.getenv("MONGODB_DB_NAME") or _load_config().get("devDb")
        try:
            coll = cls.get_collection(collection_name)
            if projection is not None:
                doc = await coll.find_one(filters or {}, projection)
            else:
                doc = await coll.find_one(filters or {})
            logger.info(
                "Database.find_one succeeded: db='%s', collection='%s', found=%s",
                db_name,
                collection_name,
                doc is not None,
            )
            return doc
        except Exception as e:
            logger.error(
                "Database.find_one failed: db='%s', collection='%s', error='%s'",
                db_name,
                collection_name,
                str(e),
                exc_info=True,
            )
            raise

    @classmethod
    async def find_to_list(
        cls,
        collection_name: str,
        filters: Optional[Dict[str, Any]] = None,
        projection: Optional[Dict[str, Any]] = None,
        *,
        length: Optional[int] = None,
        sort: Optional[Union[List, Dict, str]] = None,
        skip: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Run a find and return documents (Motor + sandbox safe)."""
        coll = cls.get_collection(collection_name)
        cursor = coll.find(filters or {}, projection)
        if sort is not None:
            cursor = cursor.sort(sort)
        if skip is not None:
            cursor = cursor.skip(int(skip))
        if limit is not None:
            cursor = cursor.limit(int(limit))
        to_list_len = length if length is not None else limit
        return await cls.cursor_to_list(cursor, length=to_list_len)


# Initialize database connection
db = Database()