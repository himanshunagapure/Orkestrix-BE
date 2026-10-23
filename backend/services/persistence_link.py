"""Typed writer→reader Mongo lineage (Issue 4).

Stage 1.7 / 4c already record collections, fields, writers, readers, and
filter_keys — but those are independent LLM extracts. The LinkedIn failure was:

  tool wrote Mongo field ``jd_id``
  tool returned graph output ``candidates_batch_id = jd_id``
  Screen 3 queried Mongo field ``candidates_batch_id``  → 0 rows

This module compiles a single ``PersistenceLink`` from the graph binding +
registry so both generators persist and query the SAME physical key.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Literal, Optional, Sequence

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

PersistenceRole = Literal["writer", "reader"]


class PersistenceEndpoint(BaseModel):
    nodeId: str
    # Physical Mongo field used as the join key.
    key: str
    # Graph-side value: writer declared output or reader binding expression.
    valueFrom: str = ""


class PersistenceLink(BaseModel):
    collection: str
    writer: PersistenceEndpoint
    reader: PersistenceEndpoint
    # Optional identity field used for upserts (not the batch join key).
    identityKey: str = ""
    cardinality: str = "one_to_many"  # one writer batch → many rows


def links_to_dicts(links: Sequence[PersistenceLink]) -> List[Dict[str, Any]]:
    return [link.model_dump() for link in links]


def _persistence_link_dedupe_key(link: PersistenceLink) -> tuple[str, str, str, str, str]:
    """Identity for Issue 13: same collection + writer/reader endpoints = one link."""
    return (
        (link.collection or "").strip(),
        (link.writer.nodeId or "").strip(),
        (link.writer.key or "").strip(),
        (link.reader.nodeId or "").strip(),
        (link.reader.key or "").strip(),
    )


def normalize_persistence_link_cardinality(link: PersistenceLink) -> PersistenceLink:
    """Same-node writer+reader is a self-join, not one_to_many."""
    if (link.writer.nodeId or "").strip() and link.writer.nodeId == link.reader.nodeId:
        if (link.cardinality or "").strip() != "self":
            return link.model_copy(update={"cardinality": "self"})
    return link


def dedupe_persistence_links(
    links: Sequence[PersistenceLink],
) -> List[PersistenceLink]:
    """Keep first occurrence of each (collection, writer, reader, keys) tuple.

    Also normalizes cardinality to ``self`` when writer.nodeId == reader.nodeId
    (Issue 13 — Rec3 duplicated self-join 2–4× into databaseLogic).
    """
    out: List[PersistenceLink] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for link in links or []:
        if not isinstance(link, PersistenceLink):
            continue
        normalized = normalize_persistence_link_cardinality(link)
        key = _persistence_link_dedupe_key(normalized)
        if not key[0] or not (key[2] or key[4]):
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
    return out


def links_from_dicts(raw: Any) -> List[PersistenceLink]:
    if not raw:
        return []
    out: List[PersistenceLink] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            if isinstance(item, PersistenceLink):
                out.append(item)
            elif isinstance(item, dict):
                out.append(PersistenceLink(**item))
        except Exception:
            continue
    return dedupe_persistence_links(out)


def node_persistence_links(node: Any) -> List[PersistenceLink]:
    cfg = getattr(node, "config", None)
    if cfg is None:
        return []
    if isinstance(cfg, dict):
        return links_from_dicts(cfg.get("persistenceLinks"))
    return links_from_dicts(getattr(cfg, "persistenceLinks", None))


def resolve_persistence_links(
    *,
    node: Any = None,
    data_source: Optional[Dict[str, Any]] = None,
    texts: Sequence[str] = (),
) -> List[PersistenceLink]:
    """Prefer typed node/IR links; fall back to PINNED PERSISTENCE LINK text."""
    links = node_persistence_links(node) if node is not None else []
    blob_parts: List[str] = [str(t) for t in texts if t]
    if isinstance(data_source, dict):
        if not links:
            links = links_from_dicts(data_source.get("persistenceLinks"))
        blob_parts.extend(
            [
                data_source.get("description") or "",
                data_source.get("databaseLogic") or "",
            ]
        )
    if links:
        return links
    return extract_persistence_links_from_text(*blob_parts)


def _ds_mentions_collection(ds: Dict[str, Any], collection: str) -> bool:
    if not collection:
        return False
    op = ds.get("operation") if isinstance(ds.get("operation"), dict) else {}
    chunks: List[str] = []
    for key in ("collection", "collectionName"):
        if op.get(key):
            chunks.append(str(op[key]))
        if ds.get(key):
            chunks.append(str(ds[key]))
    for key in ("readCollections", "writeCollections"):
        val = op.get(key) or ds.get(key) or []
        if isinstance(val, list):
            chunks.extend(str(x) for x in val if x)
        elif val:
            chunks.append(str(val))
    chunks.append(str(ds.get("databaseLogic") or ""))
    chunks.append(str(ds.get("description") or ""))
    return collection.lower() in " ".join(chunks).lower()


_PINNED_BLOCK_RE = re.compile(
    r"PINNED PERSISTENCE LINK.*?(?=\nPINNED |\nEXTERNAL |\Z)",
    re.IGNORECASE | re.DOTALL,
)


def strip_pinned_persistence_blocks(text: str) -> str:
    """Remove existing PINNED PERSISTENCE LINK blocks so re-stamp does not stack."""
    if not (text or "").strip():
        return ""
    cleaned = _PINNED_BLOCK_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def stamp_persistence_links_on_datasources(
    data_sources: Any,
    links: Sequence[PersistenceLink],
) -> None:
    """Copy compiled links onto matching IR dataSources so backend_generator sees them.

    If at least one dataSource names a linked collection, unrelated dataSources
    are left alone. If none name a collection (thin IR), stamp every dataSource
    so the pin is not dropped.

    Always replaces prior PINNED blocks (Issue 13) so repeated stamp/extract
    cannot stack 2–4× identical specs into databaseLogic.
    """
    usable = dedupe_persistence_links(
        [lk for lk in (links or []) if lk.collection and lk.writer.key]
    )
    if not usable or not isinstance(data_sources, list):
        return
    matches: List[List[PersistenceLink]] = []
    any_match = False
    for ds in data_sources:
        if not isinstance(ds, dict):
            matches.append([])
            continue
        matched = [lk for lk in usable if _ds_mentions_collection(ds, lk.collection)]
        if matched:
            any_match = True
        matches.append(matched)

    for ds, matched in zip(data_sources, matches):
        if not isinstance(ds, dict):
            continue
        to_stamp = dedupe_persistence_links(matched if any_match else usable)
        if not to_stamp:
            continue
        dumped = links_to_dicts(to_stamp)
        pin = format_pinned_persistence_spec(to_stamp)
        ds["persistenceLinks"] = dumped
        logic = strip_pinned_persistence_blocks(ds.get("databaseLogic") or "")
        if not pin:
            if logic != (ds.get("databaseLogic") or "").strip():
                ds["databaseLogic"] = logic
            continue
        ds["databaseLogic"] = f"{logic}\n\n{pin}".strip() if logic else pin


def _node_id(node: Dict[str, Any]) -> str:
    return str(node.get("nodeId") or "").strip()


def _node_config(node: Dict[str, Any]) -> Dict[str, Any]:
    cfg = node.get("config")
    return cfg if isinstance(cfg, dict) else {}


def _declared_output_names(node: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for o in _node_config(node).get("declaredOutputs") or []:
        if isinstance(o, dict) and o.get("name"):
            names.append(str(o["name"]))
        elif hasattr(o, "name") and o.name:
            names.append(str(o.name))
    return names


def _unwrap_binding(bound_to: str) -> str:
    """Stage 4 stores bindings as ``{{node.param}}``; schemas use ``node.param``."""
    s = (bound_to or "").strip()
    if s.startswith("{{") and s.endswith("}}"):
        s = s[2:-2].strip()
    return s


def _input_bindings(node: Dict[str, Any]) -> Dict[str, str]:
    raw = _node_config(node).get("inputBindings") or {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(v, str) and v.strip():
            out[str(k)] = v.strip()
        elif isinstance(v, dict) and v.get("boundTo"):
            out[str(k)] = str(v["boundTo"]).strip()
    return out


def _parse_binding(bound_to: str) -> Optional[tuple[str, str]]:
    inner = _unwrap_binding(bound_to)
    if not inner or inner.startswith("global.") or inner.startswith("pick:"):
        return None
    if "." not in inner:
        return None
    left, right = inner.split(".", 1)
    left, right = left.strip(), right.strip()
    if not left or not right:
        return None
    return left, right


def _collection_field_names(info: Dict[str, Any]) -> List[str]:
    fields = info.get("fields") or {}
    if isinstance(fields, dict):
        return [str(k) for k in fields.keys() if k]
    return []


def _choose_correlation_key(
    *,
    source_param: str,
    reader_param: str,
    field_names: Sequence[str],
    filter_keys: Sequence[str],
) -> str:
    """
    Physical Mongo join key.

    Prefer the graph-declared output/input name so writer and reader agree.
    If that name already exists as a collection field or filter_key, use it.
    Otherwise still use the graph name — the writer MUST persist it (the
    LinkedIn bug was writing ``jd_id`` while the graph/read used ``candidates_batch_id``).
    """
    fields_l = {n.lower(): n for n in field_names}
    filters_l = {n.lower(): n for n in filter_keys}
    for candidate in (source_param, reader_param):
        key = (candidate or "").strip()
        if not key:
            continue
        low = key.lower()
        if low in fields_l:
            return fields_l[low]
        if low in filters_l:
            return filters_l[low]
        return key
    if filter_keys:
        return str(filter_keys[0])
    if field_names:
        return str(field_names[0])
    return source_param or reader_param


def _binding_join_score(
    *,
    source_param: str,
    reader_param: str,
    field_names: Sequence[str],
    filter_keys: Sequence[str],
) -> int:
    """Higher = more likely a Mongo join/correlation key (vs counts/labels)."""
    fields_l = {n.lower() for n in field_names}
    filters_l = {n.lower() for n in filter_keys}
    score = 0
    for name in (source_param, reader_param):
        low = (name or "").strip().lower()
        if not low:
            continue
        if low in (
            "count",
            "fetched_count",
            "total",
            "status",
            "ok",
            "error",
            "message",
            "name",
            "title",
        ):
            score -= 50
            continue
        if low in filters_l or low in fields_l:
            score += 100
        if any(tok in low for tok in ("batch", "correlation", "filter")):
            score += 40
        if low.endswith("_id") or low.endswith("id") or low.endswith("_key"):
            score += 30
        if "uuid" in low or "token" in low:
            score += 20
    if (source_param or "").lower() == (reader_param or "").lower():
        score += 10
    return score


def _self_join_correlation_key(
    node: Optional[Dict[str, Any]],
    field_names: Sequence[str],
    filter_keys: Sequence[str],
) -> str:
    """Join key for a node that both writes and reads the same collection.

    Prefer identity-like names (filter_keys, ``*_id``) over generic collection
    fields. ``job_title`` living in ``fields`` must not beat declared ``jd_id``.
    """
    outs = _declared_output_names(node) if node else []
    outs_by_low = {n.lower(): n for n in outs}

    for fk in filter_keys:
        hit = outs_by_low.get(str(fk).lower())
        if hit:
            return hit
    for name in outs:
        low = name.lower()
        if low.endswith("_id") or low == "id":
            return name
    if filter_keys:
        return str(filter_keys[0])
    for n in field_names:
        low = str(n).lower()
        if low.endswith("_id") or low == "id":
            return str(n)
    ranked: List[tuple[int, str]] = []
    for name in outs:
        score = _binding_join_score(
            source_param=name,
            reader_param=name,
            field_names=field_names,
            filter_keys=filter_keys,
        )
        ranked.append((score, name))
    ranked.sort(key=lambda x: x[0], reverse=True)
    if ranked and ranked[0][0] > 0:
        return ranked[0][1]
    if field_names:
        return str(field_names[0])
    if outs:
        return outs[0]
    return ""


def compile_persistence_links(
    graph_nodes: Sequence[Dict[str, Any]],
    registry: Optional[Dict[str, Any]],
) -> List[PersistenceLink]:
    """
    Deterministic compiler: one PersistenceLink per (collection, writer, reader)
    that is connected by a graph inputBinding from a registered writer.
    """
    cols = ((registry or {}).get("collections") or {}) if isinstance(registry, dict) else {}
    if not cols:
        return []

    by_id: Dict[str, Dict[str, Any]] = {}
    for node in graph_nodes or []:
        nid = _node_id(node)
        if nid:
            by_id[nid] = node

    links: List[PersistenceLink] = []
    seen: set[tuple[str, str, str]] = set()

    for coll_name, info in cols.items():
        if not isinstance(coll_name, str) or not isinstance(info, dict):
            continue
        writers = [str(w) for w in (info.get("writers") or []) if w]
        readers = [str(r) for r in (info.get("readers") or []) if r]
        if not writers or not readers:
            continue
        field_names = _collection_field_names(info)
        filter_keys = [str(k) for k in (info.get("filter_keys") or []) if k]
        identity_candidates = [
            fk for fk in filter_keys
            if any(tok in fk.lower() for tok in ("identifier", "email", "public", "external_id"))
        ]
        identity_key = identity_candidates[0] if identity_candidates else ""

        writer_set = set(writers)
        for reader_id in readers:
            reader_node = by_id.get(reader_id)
            if reader_node is None:
                continue
            bindings = _input_bindings(reader_node)
            candidates: List[tuple[int, str, str, str, str]] = []
            for reader_param, bound_to in bindings.items():
                parsed = _parse_binding(bound_to)
                if not parsed:
                    continue
                writer_id, source_param = parsed
                if writer_id not in writer_set:
                    continue
                key = _choose_correlation_key(
                    source_param=source_param,
                    reader_param=reader_param,
                    field_names=field_names,
                    filter_keys=filter_keys,
                )
                score = _binding_join_score(
                    source_param=source_param,
                    reader_param=reader_param,
                    field_names=field_names,
                    filter_keys=filter_keys,
                )
                candidates.append((score, writer_id, reader_param, bound_to, key))

            if candidates:
                # One join key per (collection, writer, reader) — pick the best
                # binding (batch/id), not every output like fetched_count.
                by_writer: Dict[str, tuple[int, str, str, str, str]] = {}
                for cand in candidates:
                    wid = cand[1]
                    prev = by_writer.get(wid)
                    if prev is None or cand[0] > prev[0]:
                        by_writer[wid] = cand
                for score, writer_id, _reader_param, bound_to, key in by_writer.values():
                    dedupe = (coll_name, writer_id, reader_id)
                    if dedupe in seen:
                        continue
                    seen.add(dedupe)
                    parsed = _parse_binding(bound_to)
                    source_param = parsed[1] if parsed else key
                    ik = identity_key if identity_key.lower() != key.lower() else ""
                    links.append(
                        PersistenceLink(
                            collection=coll_name,
                            writer=PersistenceEndpoint(
                                nodeId=writer_id,
                                key=key,
                                valueFrom=source_param,
                            ),
                            reader=PersistenceEndpoint(
                                nodeId=reader_id,
                                key=key,
                                valueFrom=bound_to,
                            ),
                            identityKey=ik,
                        )
                    )
                continue

            # No graph binding from a writer — still emit a link so generators
            # share filter_keys when 4c recorded both sides.
            shared = [k for k in filter_keys if k in field_names] or list(filter_keys[:1])
            if not shared:
                continue
            key = shared[0]
            for writer_id in writers:
                if writer_id == reader_id:
                    continue
                writer_node = by_id.get(writer_id)
                value_from = key
                if writer_node:
                    outs = _declared_output_names(writer_node)
                    for o in outs:
                        if o.lower() == key.lower():
                            value_from = o
                            break
                dedupe = (coll_name, writer_id, reader_id)
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                ik = identity_key if identity_key.lower() != key.lower() else ""
                links.append(
                    PersistenceLink(
                        collection=coll_name,
                        writer=PersistenceEndpoint(
                            nodeId=writer_id,
                            key=key,
                            valueFrom=value_from,
                        ),
                        reader=PersistenceEndpoint(
                            nodeId=reader_id,
                            key=key,
                            valueFrom=f"{writer_id}.{value_from}",
                        ),
                        identityKey=ik,
                    )
                )

        # Self-join: same node is writer AND reader (list-existing + save on one
        # screen). There is no graph binding from the node to itself, so the
        # loops above never emit a link — Stage 4c then warns "join key unresolved".
        for nid in writers:
            if nid not in readers:
                continue
            dedupe = (coll_name, nid, nid)
            if dedupe in seen:
                continue
            node = by_id.get(nid)
            key = _self_join_correlation_key(node, field_names, filter_keys)
            if not (key or "").strip():
                continue
            seen.add(dedupe)
            ik = identity_key if identity_key.lower() != key.lower() else ""
            links.append(
                PersistenceLink(
                    collection=coll_name,
                    writer=PersistenceEndpoint(
                        nodeId=nid,
                        key=key,
                        valueFrom=key,
                    ),
                    reader=PersistenceEndpoint(
                        nodeId=nid,
                        key=key,
                        valueFrom=key,
                    ),
                    identityKey=ik,
                    cardinality="self",
                )
            )
    return dedupe_persistence_links(links)


def validate_persistence_lineage(
    links: Sequence[PersistenceLink],
    registry: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Return human-readable errors. Empty list = lineage is consistent."""
    errors: List[str] = []
    cols = ((registry or {}).get("collections") or {}) if isinstance(registry, dict) else {}

    for coll_name, info in cols.items():
        if not isinstance(info, dict):
            continue
        writers = [str(w) for w in (info.get("writers") or []) if w]
        readers = [str(r) for r in (info.get("readers") or []) if r]
        if readers and not writers:
            errors.append(
                f"collection '{coll_name}' has readers {readers} but no writers"
            )
        if writers and readers:
            coll_links = [lk for lk in links if lk.collection == coll_name]
            if not coll_links:
                errors.append(
                    f"collection '{coll_name}' has writers {writers} and readers {readers} "
                    "but no compiled persistenceLink (join key unresolved)"
                )

    for link in links:
        if not (link.writer.key or "").strip() or not (link.reader.key or "").strip():
            errors.append(
                f"{link.collection}: empty join key writer={link.writer.nodeId} "
                f"reader={link.reader.nodeId}"
            )
            continue
        if link.writer.key != link.reader.key:
            errors.append(
                f"{link.collection}: writer key '{link.writer.key}' != "
                f"reader key '{link.reader.key}'"
            )
    return errors


def format_pinned_persistence_spec(
    links: Sequence[PersistenceLink],
    *,
    node_id: str = "",
) -> str:
    usable = dedupe_persistence_links(
        [lk for lk in links if lk.collection and lk.writer.key]
    )
    if not usable:
        return ""
    lines = [
        "PINNED PERSISTENCE LINK (MUST implement exactly — writer and reader "
        "MUST use the SAME Mongo field name as correlationKey/filterKey):"
    ]
    for i, link in enumerate(usable, 1):
        is_writer = bool(node_id) and node_id == link.writer.nodeId
        is_reader = bool(node_id) and node_id == link.reader.nodeId
        if is_writer and is_reader:
            role = "writer+reader"
        elif is_reader:
            role = "reader"
        elif is_writer:
            role = "writer"
        else:
            role = "writer"
        lines.append(f"  Link {i} ({role}):")
        lines.append(f"    collection: {link.collection}")
        lines.append(f"    writer.nodeId: {link.writer.nodeId}")
        lines.append(f"    writer.correlationKey: {link.writer.key}")
        if link.writer.valueFrom:
            lines.append(f"    writer.valueFrom: {link.writer.valueFrom}")
        lines.append(f"    reader.nodeId: {link.reader.nodeId}")
        lines.append(f"    reader.filterKey: {link.reader.key}")
        if link.reader.valueFrom:
            lines.append(f"    reader.valueSource: {link.reader.valueFrom}")
        if link.identityKey:
            lines.append(f"    identityKey: {link.identityKey}")
        if role in ("writer", "writer+reader"):
            lines.append(
                f"    RULE: every document written to '{link.collection}' MUST include "
                f"field '{link.writer.key}' (do NOT persist a different batch key instead)."
            )
        if role in ("reader", "writer+reader"):
            if link.writer.nodeId == link.reader.nodeId:
                lines.append(
                    f"    RULE: when fetching a single '{link.collection}' row, filter "
                    f"{{'{link.reader.key}': <id>}}. Listing the collection without that "
                    "filter is allowed."
                )
            else:
                lines.append(
                    f"    RULE: query '{link.collection}' with filter "
                    f"{{'{link.reader.key}': <bound value>}}. Do NOT query a different field."
                )
    return "\n".join(lines)


def extract_persistence_links_from_text(*texts: str) -> List[PersistenceLink]:
    """Recover links previously injected into generationDescription / databaseLogic."""
    blob = "\n".join(t for t in texts if t and str(t).strip())
    if "PINNED PERSISTENCE LINK" not in blob.upper() and "correlationKey" not in blob:
        return []
    links: List[PersistenceLink] = []
    # Parse "Link N" stanzas.
    chunks = re.split(r"\n\s*Link \d+", blob)
    for chunk in chunks[1:]:
        fields: Dict[str, str] = {}
        for line in chunk.splitlines():
            if ":" not in line:
                continue
            k, _, v = line.strip().partition(":")
            fields[k.strip().lower()] = v.strip()
        coll = fields.get("collection") or ""
        w_key = fields.get("writer.correlationkey") or fields.get("correlationkey") or ""
        r_key = fields.get("reader.filterkey") or fields.get("filterkey") or w_key
        w_node = fields.get("writer.nodeid") or ""
        r_node = fields.get("reader.nodeid") or ""
        if not coll or not (w_key or r_key):
            continue
        key = w_key or r_key
        links.append(
            PersistenceLink(
                collection=coll,
                writer=PersistenceEndpoint(
                    nodeId=w_node,
                    key=key,
                    valueFrom=fields.get("writer.valuefrom") or "",
                ),
                reader=PersistenceEndpoint(
                    nodeId=r_node,
                    key=r_key or key,
                    valueFrom=fields.get("reader.valuesource") or "",
                ),
                identityKey=fields.get("identitykey") or "",
            )
        )
    return dedupe_persistence_links(links)


def infer_persistence_role(
    *,
    node_id: str = "",
    data_source: Optional[Dict[str, Any]] = None,
    links: Sequence[PersistenceLink] = (),
) -> str:
    """``writer`` strips return-dicts; ``reader`` keeps them (query may live in return)."""
    if node_id:
        is_writer = any(node_id == link.writer.nodeId for link in links or [])
        is_reader = any(node_id == link.reader.nodeId for link in links or [])
        if is_writer and is_reader:
            # Dual-role (self-join screen): dataSource action decides. Default
            # writer so the save path must persist the join key.
            ds_only = infer_persistence_role(
                node_id="", data_source=data_source, links=()
            )
            return ds_only
        if is_reader:
            return "reader"
        if is_writer:
            return "writer"
    op = (data_source or {}).get("operation") if isinstance(data_source, dict) else {}
    if not isinstance(op, dict):
        op = {}
    action = str(op.get("action") or "").lower()
    if action in ("find", "find_one", "read", "aggregate") or op.get("readCollections"):
        return "reader"
    if action in ("insert", "update", "delete", "upsert") or op.get("writeCollections"):
        return "writer"
    logic = " ".join(
        [
            str((data_source or {}).get("databaseLogic") or ""),
            str((data_source or {}).get("description") or ""),
        ]
    ).lower()
    if any(tok in logic for tok in ("find(", "find_one", "query ", "filter")):
        return "reader"
    return "writer"


def function_code_matches_persistence_fixture(
    function_code: str,
    links: Sequence[PersistenceLink],
    *,
    node_id: str = "",
    role: str = "",
    data_source: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Rejection reason when generated code ignores the pinned Mongo join key."""
    usable = [lk for lk in links if lk.collection and lk.writer.key]
    if not usable or not (function_code or "").strip():
        return None

    def _quoted_field(blob: str, field: str) -> bool:
        if not field:
            return True
        return bool(
            re.search(rf"""['"]{re.escape(field)}['"]\s*:""", blob, re.IGNORECASE)
        )

    def _without_returns(blob: str) -> str:
        parts = re.split(r"\breturn\b", blob)
        if len(parts) <= 1:
            return blob
        return "\n".join(parts[:-1])

    inferred = (role or "").strip().lower() or infer_persistence_role(
        node_id=node_id, data_source=data_source, links=usable
    )
    for link in usable:
        this_role = inferred
        if node_id and node_id == link.reader.nodeId and node_id == link.writer.nodeId:
            this_role = inferred or infer_persistence_role(
                node_id="", data_source=data_source, links=()
            )
        elif node_id and node_id == link.reader.nodeId:
            this_role = "reader"
        elif node_id and node_id == link.writer.nodeId:
            this_role = "writer"
        key = link.writer.key if this_role == "writer" else link.reader.key
        haystack = function_code if this_role == "reader" else _without_returns(function_code)
        if _quoted_field(haystack, key):
            continue
        if this_role == "reader" and link.writer.nodeId == link.reader.nodeId:
            # Self-join reader is typically "list all" — do not require a filter
            # on the join key. Writer half still must persist it.
            continue
        if this_role == "writer":
            return (
                f"function_code does not persist pinned correlationKey '{link.writer.key}' "
                f"on collection '{link.collection}'. Write that field on every document; "
                "do not substitute a different batch key."
            )
        return (
            f"function_code does not query pinned filterKey '{link.reader.key}' "
            f"on collection '{link.collection}'. Filter on that field; "
            "do not query a different batch key."
        )
    return None


def attach_persistence_links_to_graph(
    graph_nodes: List[Dict[str, Any]],
    registry: Optional[Dict[str, Any]],
) -> tuple[List[PersistenceLink], List[str]]:
    """
    Compile links, stamp ``config.persistenceLinks`` on writer/reader nodes,
    and append the pin block to ``generationDescription`` (not intentDescription).
    Returns (links, lineage_errors).
    """
    links = compile_persistence_links(graph_nodes, registry)
    errors = validate_persistence_lineage(links, registry)
    by_node: Dict[str, List[PersistenceLink]] = {}
    for link in links:
        by_node.setdefault(link.writer.nodeId, []).append(link)
        # Issue 13: self-join must not append the same link twice for one node.
        if link.reader.nodeId != link.writer.nodeId:
            by_node.setdefault(link.reader.nodeId, []).append(link)

    for node in graph_nodes or []:
        nid = _node_id(node)
        node_links = dedupe_persistence_links(by_node.get(nid) or [])
        if not node_links:
            continue
        cfg = node.setdefault("config", {})
        if not isinstance(cfg, dict):
            continue
        cfg["persistenceLinks"] = links_to_dicts(node_links)
        pin = format_pinned_persistence_spec(node_links, node_id=nid)
        if not pin:
            continue
        desc = strip_pinned_persistence_blocks(cfg.get("generationDescription") or "")
        cfg["generationDescription"] = f"{desc}\n\n{pin}".strip() if desc else pin
        gen = cfg.get("generation")
        if isinstance(gen, dict):
            gen["description"] = cfg["generationDescription"]
        logger.info(
            "[persistence-link] node %s attached %s link(s) collection(s)=%s",
            nid,
            len(node_links),
            sorted({lk.collection for lk in node_links}),
        )
    return links, errors
