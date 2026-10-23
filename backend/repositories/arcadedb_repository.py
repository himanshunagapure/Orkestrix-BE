import logging
from typing import Any, Dict, List, Optional

from services.database import Database

logger = logging.getLogger(__name__)


class EnterpriseGraphRepository:
    """
    Repository for the Enterprise Graph, stored in ArcadeDB.

    Schema:
      (:Tenant {subscriber_id, org_id})
        -[:OWNS]-> (:Collection {name})
          -[:HAS_FIELD]-> (:Field {name, collection, type, aliases[]})

    Every read/write is strictly scoped by (subscriber_id, org_id) via the
    Tenant node. This is the tenant-isolation boundary: it must not be
    possible for one subscriber's prompts to see another subscriber's
    collection/field names, because that would leak one tenant's business
    schema (and by extension hints about their data model) into another
    tenant's LLM context.

    All queries route through Database.run_arcadedb_command() over ArcadeDB's
    HTTP REST API (POST /api/v1/command/{database}, language="cypher"). This
    ArcadeDB instance only exposes its HTTP port (2480) and its native binary
    protocol port (2424) — the Postgres-wire-protocol plugin is not enabled
    on this server, so a psycopg-based pool connection is not usable here.

    Every value (subscriber_id, org_id, collection/field names, aliases) is
    passed as a genuine Cypher bind parameter ($subscriber_id, $org_id, ...)
    via the HTTP API's `params` field, rather than being escaped and inlined
    into the query string. This avoids Cypher-injection risk entirely,
    the same way parameterized SQL avoids SQL injection.
    """

    async def _run_cypher(
        self, query: str, params: Optional[Dict[str, Any]] = None
    ) -> List[tuple]:
        db_name = Database._resolve_arcadedb_http_settings()["dbname"]
        logger.info("[EnterpriseGraphRepository] using shared metadata ArcadeDB db='%s'", db_name)
        try:
            rows = await Database.run_arcadedb_command(
                query, params=params, language="cypher"
            )
        except Exception as e:
            logger.error(
                "[EnterpriseGraphRepository] cypher failed: db='%s' query=%r params=%r error=%s",
                db_name, query, params, str(e), exc_info=True,
            )
            raise
        if not rows:
            return []
        cleaned: List[tuple] = []
        for row in rows:
            if isinstance(row, dict):
                values = [v for k, v in row.items() if not str(k).startswith("@")]
                cleaned.append(tuple(values))
            else:
                cleaned.append(tuple(row))
        return cleaned

    @staticmethod
    def _require_tenant(subscriber_id: Optional[str], org_id: Optional[str]) -> None:
        """
        Fail loudly rather than silently querying/writing ungoverned data.
        A missing subscriber_id/org_id here is a caller bug, not something
        to default around -- defaulting would risk cross-tenant leakage.
        """
        if not subscriber_id or not org_id:
            raise ValueError(
                "EnterpriseGraphRepository requires both subscriber_id and org_id "
                f"(got subscriber_id={subscriber_id!r}, org_id={org_id!r})"
            )

    async def _ensure_tenant(self, subscriber_id: str, org_id: str) -> None:
        await self._run_cypher(
            "MERGE (t:Tenant {subscriber_id: $subscriber_id, org_id: $org_id}) RETURN t",
            params={"subscriber_id": subscriber_id, "org_id": org_id},
        )

    async def list_all_collections(self, subscriber_id: str, org_id: str) -> List[str]:
        """
        Return every Collection name owned by this tenant.

        Used by the Stage 2a schema builder to keyword-match node name/description
        text against real collections, since nodes don't carry an explicit
        collection reference (see Technical Design 5.4).
        """
        self._require_tenant(subscriber_id, org_id)

        rows = await self._run_cypher(
            """
            MATCH (t:Tenant {subscriber_id: $subscriber_id, org_id: $org_id})-[:OWNS]->(c:Collection)
            RETURN c.name
            """,
            params={"subscriber_id": subscriber_id, "org_id": org_id},
        )
        names = [r[0] for r in rows if r and r[0]]
        logger.info(
            "[EnterpriseGraphRepository] list_all_collections: subscriber_id=%s found=%d",
            subscriber_id, len(names),
        )
        return names

    async def upsert_schema(
        self,
        subscriber_id: str,
        org_id: str,
        collection_name: str,
        fields: List[Dict[str, Any]],
        target_store: str = "mongodb"
    ) -> None:
        """
        MERGE a Collection node (owned by this tenant) and its Field nodes into
        the Enterprise Graph.

        fields: [{"name": "invoice_number", "type": "string", "aliases": ["inv_no"]}, ...]

        Idempotent - safe to call repeatedly (e.g. from Stage 4c write-back).
        """
        self._require_tenant(subscriber_id, org_id)
        if not fields:
            return

        await self._ensure_tenant(subscriber_id, org_id)

        await self._run_cypher(
            """
            MATCH (t:Tenant {subscriber_id: $subscriber_id, org_id: $org_id})
            MERGE (c:Collection {name: $collection_name, subscriber_id: $subscriber_id, org_id: $org_id})
            SET c.target_store = $target_store
            MERGE (t)-[:OWNS]->(c)
            RETURN c
            """,
            params={
                "subscriber_id": subscriber_id,
                "org_id": org_id,
                "collection_name": collection_name,
                "target_store": target_store,
            },
        )

        for field in fields:
            await self._run_cypher(
                """
                MATCH (t:Tenant {subscriber_id: $subscriber_id, org_id: $org_id})-[:OWNS]->
                      (c:Collection {name: $collection_name})
                MERGE (f:Field {name: $field_name, collection: $collection_name,
                                  subscriber_id: $subscriber_id, org_id: $org_id})
                SET f.type = $field_type,
                    f.aliases = $aliases
                MERGE (c)-[:HAS_FIELD]->(f)
                RETURN f
                """,
                params={
                    "subscriber_id": subscriber_id,
                    "org_id": org_id,
                    "collection_name": collection_name,
                    "field_name": field["name"],
                    "field_type": field.get("type", "string"),
                    "aliases": field.get("aliases", []),
                },
            )

        logger.info(
            "[EnterpriseGraphRepository] upsert_schema: subscriber_id=%s collection=%s fields=%d",
            subscriber_id, collection_name, len(fields),
        )

    async def get_target_stores_for_collections(
        self, subscriber_id: str, org_id: str, collection_names: List[str]
    ) -> Dict[str, str]:
        """
        Batch-fetch the authoritative target_store ("mongodb"/"arcadedb") per collection.

        Used by backend_generator.generate_backend() to override any target_store
        an LLM guesses at code-generation time with the decision already made
        (and persisted here) by the /knowledge-sources classifier.
        """
        self._require_tenant(subscriber_id, org_id)
        if not collection_names:
            return {}

        rows = await self._run_cypher(
            """
            MATCH (t:Tenant {subscriber_id: $subscriber_id, org_id: $org_id})-[:OWNS]->(c:Collection)
            WHERE c.name IN $collection_names
            RETURN c.name, c.target_store
            """,
            params={
                "subscriber_id": subscriber_id,
                "org_id": org_id,
                "collection_names": collection_names,
            },
        )

        stores = {r[0]: (r[1] or "mongodb") for r in rows if r and r[0]}
        logger.info(
            "[EnterpriseGraphRepository] get_target_stores_for_collections: subscriber_id=%s "
            "requested=%d matched=%d",
            subscriber_id, len(collection_names), len(stores),
        )
        return stores

    async def get_schema_fields(
        self, subscriber_id: str, org_id: str, collection_name: str
    ) -> List[Dict[str, Any]]:
        """
        Return canonical field definitions for a collection owned by this tenant.

        Used by:
          - Stage 2a node schema builder (5.4): inject available DB fields into prompt
          - Gap resolution (6.1): inject "Use exact fields: [...]" into codegen prompt
        """
        self._require_tenant(subscriber_id, org_id)

        rows = await self._run_cypher(
            """
            MATCH (t:Tenant {subscriber_id: $subscriber_id, org_id: $org_id})-[:OWNS]->
                  (c:Collection {name: $collection_name})-[:HAS_FIELD]->(f:Field)
            RETURN f.name, f.type, f.aliases
            """,
            params={
                "subscriber_id": subscriber_id,
                "org_id": org_id,
                "collection_name": collection_name,
            },
        )

        fields = [
            {"name": r[0], "type": r[1], "aliases": r[2] or []}
            for r in rows
        ]
        logger.info(
            "[EnterpriseGraphRepository] get_schema_fields: subscriber_id=%s collection=%s found=%d",
            subscriber_id, collection_name, len(fields),
        )
        return fields

    async def get_schemas_for_collections(
        self, subscriber_id: str, org_id: str, collection_names: List[str]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Batch variant of get_schema_fields: fetch fields for several matched
        collections in one pass, so Stage 2a doesn't issue one query per match.
        Returns {"invoices": [{"name": "invoice_number", "type": "string", "aliases": [...]}]}.
        """
        self._require_tenant(subscriber_id, org_id)
        if not collection_names:
            return {}

        rows = await self._run_cypher(
            """
            MATCH (t:Tenant {subscriber_id: $subscriber_id, org_id: $org_id})-[:OWNS]->
                  (c:Collection)-[:HAS_FIELD]->(f:Field)
            WHERE c.name IN $collection_names
            RETURN c.name, f.name, f.type, f.aliases
            """,
            params={
                "subscriber_id": subscriber_id,
                "org_id": org_id,
                "collection_names": collection_names,
            },
        )

        schemas: Dict[str, List[Dict[str, Any]]] = {}
        for coll_name, field_name, field_type, aliases in rows:
            schemas.setdefault(coll_name, []).append(
                {"name": field_name, "type": field_type, "aliases": aliases or []}
            )

        logger.info(
            "[EnterpriseGraphRepository] get_schemas_for_collections: subscriber_id=%s "
            "requested=%d matched=%d",
            subscriber_id, len(collection_names), len(schemas),
        )
        return schemas
    
    # arcadedb_repository.py 
    async def upsert_relationship(
        self, subscriber_id: str, org_id: str, from_coll: str, from_field: str, to_coll: str, to_field: str
    ) -> None:
        self._require_tenant(subscriber_id, org_id)
        await self._run_cypher(
            """
            MATCH (t:Tenant {subscriber_id: $subscriber_id, org_id: $org_id})-[:OWNS]->(c1:Collection {name: $from_coll})-[:HAS_FIELD]->(f1:Field {name: $from_field})
            MATCH (t)-[:OWNS]->(c2:Collection {name: $to_coll})-[:HAS_FIELD]->(f2:Field {name: $to_field})
            MERGE (f1)-[:REFERENCES]->(f2)
            """,
            params={
                "subscriber_id": subscriber_id,
                "org_id": org_id,
                "from_coll": from_coll,
                "from_field": from_field,
                "to_coll": to_coll,
                "to_field": to_field,
            },
        )

    async def find_canonical_alias(
        self, subscriber_id: str, org_id: str, missing_field: str
    ) -> Optional[str]:
        """
        Given a runtime field name that failed (KeyError / FieldNotFound), look up
        the canonical field name it's an alias of, scoped to this tenant only.

        Used by executor.py (8.1) schema-drift auto-recovery: on failure, resolve
        the field, mutate params, retry the tool call once.

        Returns None if no canonical mapping is found, or if the field is already
        canonical (caller should not retry in either case).
        """
        self._require_tenant(subscriber_id, org_id)

        rows = await self._run_cypher(
            """
            MATCH (t:Tenant {subscriber_id: $subscriber_id, org_id: $org_id})-[:OWNS]->
                  (:Collection)-[:HAS_FIELD]->(f:Field)
            WHERE f.name = $missing_field OR $missing_field IN f.aliases
            RETURN f.name
            LIMIT 1
            """,
            params={"subscriber_id": subscriber_id, "org_id": org_id, "missing_field": missing_field},
        )

        if not rows:
            logger.info(
                "[EnterpriseGraphRepository] find_canonical_alias: subscriber_id=%s no match for %r",
                subscriber_id, missing_field,
            )
            return None

        canonical = rows[0][0]
        if canonical == missing_field:
            return None

        logger.info(
            "[EnterpriseGraphRepository] find_canonical_alias: subscriber_id=%s %s -> %s",
            subscriber_id, missing_field, canonical,
        )
        return canonical