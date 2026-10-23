#!/usr/bin/env python3
"""
Live E2E: estimate → register → Stage 0 → /nl/plan against a running agent_new.py.

Writes a human-readable report (with full request/response per API call) + raw
JSON artifacts under:
  backend/nl_builder/e2e_results/<timestamp>/
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

BASE = "http://127.0.0.1:5010/dev"
OUT_ROOT = Path(__file__).resolve().parent / "e2e_results"
TIMEOUT = 900  # complex KYC graphs can take longer

# Edge-case stress prompt: upload + OCR, retry loop, 3-way risk router,
# dual UI, merge after branches, dual notifications, rejection path.
WORKFLOW_PROMPT = (
    "Build a personal loan KYC verification workflow with these edge cases. "
    "1) Applicant UI form: full name, email, phone, requested loan amount (number), "
    "and government ID document upload (image/PDF). "
    "2) OCR tool extracts name and ID number from the uploaded document. "
    "3) If OCR confidence is below 80 OR extracted name does not match form name, "
    "route back to the applicant form to re-upload (retry loop); otherwise continue. "
    "4) Risk scoring tool computes risk_score (0-100) from loan amount and ID validity. "
    "5) THREE-WAY routing on risk_score: "
    "   - risk_score < 40: auto-approve tool "
    "   - risk_score between 40 and 70 inclusive: additional verification UI where "
    "     an agent enters employment status and monthly income, then a verify tool "
    "     returns verified=true/false "
    "   - risk_score > 70: manual underwriter review UI (approve/reject + comments) "
    "6) If the mid-risk verify tool returns verified=false, go to the same underwriter "
    "   review UI used for high risk. "
    "7) Merge all decision paths and save the final application (status, risk_score, "
    "comments, extracted_id_number) into MongoDB collection loan_kyc_applications. "
    "8) Send BOTH an email and an SMS notification to the applicant with the outcome. "
    "9) Always end after notifications. Do not skip the save step on reject."
)

PROJECT_ID = f"nl_e2e_kyc_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
SELECTED_NAME = "Loan KYC Edge-Case E2E"
SELECTED_TYPE = "automation"
SELECTED_CATEGORY = "finance"
ORG_ID = "507f1f77bcf86cd799439012"
SUBSCRIBER_ID = "507f1f77bcf86cd799439013"
USER_ID = "507f1f77bcf86cd799439014"

HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

# Soft keyword expectations for KYC edge-case workflow
EXPECTED_TOPIC_ALTS = {
    "form/collect": ["form", "applicant", "upload", "loan", "collect", "kyc"],
    "ocr/extract": ["ocr", "extract", "document", "id", "confidence"],
    "retry/loop": ["re-upload", "retry", "match", "confidence", "back"],
    "risk/score": ["risk", "score"],
    "branch/approve": ["approv", "underwriter", "manual", "verify", "auto"],
    "persist/save": ["save", "persist", "store", "mongo", "loan_kyc", "insert"],
    "email/notify": ["email", "smtp", "notif"],
    "sms/notify": ["sms", "text", "phone", "twilio", "message"],
}


def _dump(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def _json_block(obj: Any, *, limit: int = 12000) -> str:
    text = json.dumps(obj, indent=2, default=str)
    if len(text) > limit:
        return text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]"
    return text


def _post(
    path: str,
    body: Dict[str, Any],
    *,
    label: str,
    api_log: List[Dict[str, Any]],
    out_dir: Path,
    file_stem: str,
) -> Dict[str, Any]:
    url = f"{BASE}{path}"
    print(f"\n=== {label} ===")
    print(f"POST {url}")
    t0 = time.time()
    resp = requests.post(url, headers=HEADERS, json=body, timeout=TIMEOUT)
    elapsed = time.time() - t0
    print(f"HTTP {resp.status_code} in {elapsed:.1f}s")
    try:
        data = resp.json()
    except Exception:
        data = {"_raw_text": resp.text[:4000]}

    entry = {
        "label": label,
        "method": "POST",
        "url": url,
        "path": path,
        "elapsedSec": round(elapsed, 2),
        "httpStatus": resp.status_code,
        "requestHeaders": HEADERS,
        "requestBody": body,
        "responseBody": data,
    }
    api_log.append(entry)
    _dump(out_dir / f"{file_stem}_request.json", {"url": url, "headers": HEADERS, "body": body})
    _dump(out_dir / f"{file_stem}_response.json", data)
    _dump(out_dir / f"{file_stem}_exchange.json", entry)

    if resp.status_code >= 400:
        print("ERROR BODY:", json.dumps(data, indent=2, default=str)[:2000])
        resp.raise_for_status()
    return data


def _pick_answer(question: Dict[str, Any]) -> str:
    options = question.get("options") or question.get("choices") or []
    prompt = " ".join(
        str(question.get(k) or "")
        for k in ("prompt", "question", "question_text", "text")
    ).lower()
    flag = str(
        question.get("flagKey") or question.get("flag_key") or question.get("flag_type") or ""
    ).upper()

    texts: List[str] = []
    for opt in options:
        if isinstance(opt, str):
            texts.append(opt)
        elif isinstance(opt, dict):
            texts.append(
                str(
                    opt.get("label")
                    or opt.get("text")
                    or opt.get("value")
                    or opt.get("option")
                    or ""
                )
            )

    def find_substr(*needles: str) -> Optional[str]:
        for t in texts:
            low = t.lower()
            if any(n in low for n in needles):
                return t
        return None

    if "email" in prompt or "EMAIL" in flag or "SMTP" in flag:
        return find_substr("already", "configured", "later", "smtp", "gmail") or (
            texts[0] if texts else "I will configure email credentials later — continue building"
        )
    if "sms" in prompt or "twilio" in prompt or "SMS" in flag or "PHONE" in flag:
        return find_substr("twilio", "already", "configured", "later", "sms") or (
            texts[0] if texts else "Use Twilio SMS credentials; configure later if missing"
        )
    if "ocr" in prompt or "OCR" in flag or "document" in prompt:
        return find_substr("google", "aws", "azure", "tesseract", "built", "generate", "llm") or (
            texts[0] if texts else "Generate an OCR tool that extracts name and ID number"
        )
    if "mongo" in prompt or "database" in prompt or "collection" in prompt or "DB" in flag:
        return find_substr("loan_kyc", "mongo", "existing", "create", "collection") or (
            texts[0] if texts else "Use MongoDB collection loan_kyc_applications"
        )
    if "approv" in prompt or "manager" in prompt or "underwriter" in prompt:
        return find_substr("underwriter", "manager", "approv", "ui", "screen", "yes") or (
            texts[0] if texts else "Underwriter reviews on a UI screen"
        )
    if "risk" in prompt or "score" in prompt:
        return find_substr("0-100", "tool", "generate", "calculate") or (
            texts[0] if texts else "Generate a risk scoring tool returning risk_score 0-100"
        )
    if texts:
        preferred = find_substr(
            "yes", "create", "use", "existing", "mongo", "later", "continue", "already", "generate"
        )
        return preferred or texts[0]
    return "Use reasonable defaults for this KYC edge-case workflow"


def _q_text(q: Dict[str, Any]) -> str:
    return str(
        q.get("prompt")
        or q.get("question")
        or q.get("question_text")
        or q.get("text")
        or ""
    )


def _summarize_questions(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for q in payload.get("questions") or []:
        out.append(
            {
                "id": q.get("id") or q.get("questionId") or q.get("flagKey") or q.get("flag_type"),
                "flagKey": q.get("flagKey") or q.get("flag_key") or q.get("flag_type"),
                "prompt": _q_text(q),
                "options": q.get("options") or q.get("choices") or [],
                "severity": q.get("severity") or q.get("helpText"),
            }
        )
    return out


def _node_desc(node: Dict[str, Any]) -> str:
    top = (node.get("description") or "").strip()
    if top:
        return top
    cfg = node.get("config") or {}
    gd = (cfg.get("generationDescription") or "").strip()
    if gd:
        return gd
    gen = cfg.get("generation") or {}
    if isinstance(gen, dict):
        return (gen.get("description") or "").strip()
    return ""


def _analyze_graph(plan_resp: Dict[str, Any]) -> Dict[str, Any]:
    phase_graphs = plan_resp.get("phaseGraphs") or []
    findings: List[str] = []
    issues: List[str] = []

    if not phase_graphs:
        issues.append("phaseGraphs is empty — /plan/execute would have nothing to run.")
        return {"ok": False, "findings": findings, "issues": issues}

    pg0 = phase_graphs[0]
    graph = pg0.get("graph") or {}
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    types: Dict[str, int] = {}
    for n in nodes:
        types[n.get("type") or "unknown"] = types.get(n.get("type") or "unknown", 0) + 1

    findings.append(f"phase count = {len(phase_graphs)} (expected 1 for NL builder)")
    findings.append(f"nodes={len(nodes)} edges={len(edges)} types={types}")
    findings.append(f"entry={graph.get('entryNodeId')} end={graph.get('endNodeId')}")
    findings.append(
        f"analysis_source={plan_resp.get('analysis_source')} mode={plan_resp.get('mode')}"
    )
    findings.append(f"estimatedCost={plan_resp.get('estimatedCost')}")

    has_ui = any(n.get("type") == "ui_screen" for n in nodes)
    has_tool = any(n.get("type") == "tool" for n in nodes)
    has_end = any(n.get("type") == "end" for n in nodes)
    has_router = any(n.get("type") == "router" for n in nodes)

    if not has_ui:
        issues.append("Missing ui_screen node.")
    else:
        findings.append("Has ui_screen ✓")
    if not has_tool:
        issues.append("Missing tool node(s).")
    else:
        findings.append("Has tool node(s) ✓")
    if not has_end:
        issues.append("Missing end node.")
    else:
        findings.append("Has end node ✓")
    if has_router:
        findings.append("Has router ✓")
    else:
        findings.append("No router — branching may have been linearized.")

    gen_refs = []
    for n in nodes:
        cref = (n.get("capabilityRef") or {}).get("capabilityId") or ""
        if str(cref).startswith("GENERATE:"):
            gen_refs.append(f"{n.get('nodeId')}:{cref}")
    if gen_refs:
        findings.append(
            f"GENERATE capability refs present ({len(gen_refs)}) — correct for pre-execute plan"
        )
    else:
        ui_tool = [n for n in nodes if n.get("type") in ("ui_screen", "tool")]
        if ui_tool:
            issues.append("ui/tool nodes lack GENERATE:* capabilityRef.")

    empty_desc = [
        n.get("nodeId")
        for n in nodes
        if n.get("type") in ("ui_screen", "tool") and not _node_desc(n)
    ]
    if empty_desc:
        issues.append(f"Empty generation descriptions on: {empty_desc}")
    else:
        findings.append("All ui/tool nodes have generation descriptions ✓")

    underscore = [k for k in graph.keys() if str(k).startswith("_")]
    if underscore:
        issues.append(f"Internal _-keys left on graph: {underscore}")
    else:
        findings.append("No internal _-keys on public graph ✓")

    node_ids = {n.get("nodeId") for n in nodes}
    bad_edges = []
    for e in edges:
        fr = e.get("fromNode") or e.get("from")
        to = e.get("to") or e.get("toNode")
        if fr not in node_ids or to not in node_ids:
            bad_edges.append(e)
    if bad_edges:
        issues.append(f"{len(bad_edges)} edge(s) reference missing nodes.")
    else:
        findings.append("All edges reference known nodes ✓")

    entry = graph.get("entryNodeId")
    if entry and entry not in node_ids:
        issues.append(f"entryNodeId '{entry}' not in nodes.")

    blob = " ".join(_node_desc(n) + " " + (n.get("name") or "") for n in nodes).lower()
    for label, alts in EXPECTED_TOPIC_ALTS.items():
        if any(a in blob for a in alts):
            findings.append(f"Topology text covers {label} ✓")
        else:
            issues.append(f"Topology text does not clearly cover {label}.")

    preview = plan_resp.get("plan") or {}
    if not preview.get("summary"):
        issues.append("plan.summary is empty.")
    else:
        findings.append(f"Preview summary present ({len(preview.get('summary') or '')} chars) ✓")

    skeleton = plan_resp.get("skeletonPlan")
    if not skeleton or not skeleton.get("nodes"):
        issues.append("skeletonPlan missing or empty.")
    else:
        findings.append(f"skeletonPlan present with {len(skeleton.get('nodes') or [])} nodes ✓")

    # ---- Edge-case structural checks ----
    ui_count = sum(1 for n in nodes if n.get("type") == "ui_screen")
    router_nodes = [n for n in nodes if n.get("type") == "router"]
    if ui_count >= 2:
        findings.append(f"Multiple UI screens ({ui_count}) ✓")
    else:
        issues.append(f"Expected ≥2 ui_screen nodes for KYC edge case, got {ui_count}.")

    # Loop / back-edge: some edge returns to an earlier UI/entry-ish node
    entry = graph.get("entryNodeId")
    back_edges = []
    for e in edges:
        fr = e.get("fromNode") or e.get("from")
        to = e.get("to") or e.get("toNode")
        if to == entry or (
            to
            and any(
                n.get("nodeId") == to and n.get("type") == "ui_screen" and fr != to
                for n in nodes
            )
            and any(
                (ee.get("fromNode") or ee.get("from")) == to
                and ((ee.get("to") or ee.get("toNode")) == fr or True)
                for ee in edges
            )
        ):
            # simpler: edge whose target appears before source in a topo sense OR target is entry
            if to == entry:
                back_edges.append(e)
    # Also detect any edge targeting a node that can reach the source (cycle)
    from collections import defaultdict, deque

    adj = defaultdict(list)
    for e in edges:
        adj[e.get("fromNode") or e.get("from")].append(e.get("to") or e.get("toNode"))

    def can_reach(src: str, dst: str) -> bool:
        seen = set()
        q = deque([src])
        while q:
            u = q.popleft()
            if u == dst:
                return True
            if u in seen:
                continue
            seen.add(u)
            for v in adj.get(u) or []:
                if v not in seen:
                    q.append(v)
        return False

    cycle_edges = []
    for e in edges:
        fr = e.get("fromNode") or e.get("from")
        to = e.get("to") or e.get("toNode")
        if fr and to and can_reach(to, fr):
            cycle_edges.append(f"{fr}->{to}")
    if cycle_edges or back_edges:
        findings.append(f"Retry/loop edge present ({cycle_edges or back_edges}) ✓")
    else:
        issues.append("No retry/loop back-edge detected (OCR fail should return to form).")

    # Multi-way router: any router with ≥3 outgoing edges
    out_count: Dict[str, int] = defaultdict(int)
    for e in edges:
        fr = e.get("fromNode") or e.get("from")
        if fr:
            out_count[fr] += 1
    multi = [rid for rid, c in out_count.items() if c >= 3 and any(r.get("nodeId") == rid for r in router_nodes)]
    if multi:
        findings.append(f"Multi-way router (≥3 outs) on {multi} ✓")
    else:
        # soft: risk router might be modeled as nested 2-way routers
        if len(router_nodes) >= 2:
            findings.append(
                f"No single 3-way router, but {len(router_nodes)} routers (nested branching OK)"
            )
        else:
            issues.append("Expected 3-way risk routing (one ≥3-out router or ≥2 routers).")

    # pick: merge bindings after branches
    pick_bindings = []
    for n in nodes:
        for k, v in ((n.get("config") or {}).get("inputBindings") or {}).items():
            if isinstance(v, str) and "pick:" in v:
                pick_bindings.append(f"{n.get('nodeId')}.{k}={v}")
    if pick_bindings:
        findings.append(f"Merge pick: bindings present ({len(pick_bindings)}) ✓")
    else:
        findings.append("No pick: merge bindings (may use single-path status field).")

    # Dual notifications: email + sms in names/descriptions
    blob_l = blob  # already lowercased above
    has_email = any(a in blob_l for a in ("email", "smtp"))
    has_sms = any(a in blob_l for a in ("sms", "twilio", "text message"))
    if has_email and has_sms:
        findings.append("Both email and SMS notification signals ✓")
    elif has_email:
        issues.append("Email present but SMS notification not clearly present.")
    else:
        issues.append("Missing email and/or SMS notification nodes.")

    return {
        "ok": len(issues) == 0,
        "findings": findings,
        "issues": issues,
        "edge_case_checks": {
            "ui_count": ui_count,
            "router_count": len(router_nodes),
            "multi_way_routers": multi,
            "cycle_edges": cycle_edges,
            "pick_bindings": pick_bindings,
            "has_email": has_email,
            "has_sms": has_sms,
        },
        "node_summaries": [
            {
                "nodeId": n.get("nodeId"),
                "name": n.get("name"),
                "type": n.get("type"),
                "capabilityRef": (n.get("capabilityRef") or {}).get("capabilityId"),
                "description": _node_desc(n)[:400],
                "hasBindings": bool((n.get("config") or {}).get("inputBindings")),
                "declaredOutputs": len((n.get("config") or {}).get("declaredOutputs") or []),
            }
            for n in nodes
        ],
        "edges": [
            {
                "from": e.get("fromNode") or e.get("from"),
                "to": e.get("to") or e.get("toNode"),
                "kind": e.get("kind"),
                "condition": e.get("condition"),
            }
            for e in edges
        ],
    }


def _append_exchange_section(lines: List[str], entry: Dict[str, Any]) -> None:
    lines.append(f"### {entry['label']}")
    lines.append("")
    lines.append(f"- **Method/URL:** `{entry['method']} {entry['url']}`")
    lines.append(f"- **HTTP status:** `{entry['httpStatus']}`")
    lines.append(f"- **Elapsed:** `{entry['elapsedSec']}s`")
    lines.append("")
    lines.append("#### Request")
    lines.append("```json")
    lines.append(_json_block(entry["requestBody"], limit=8000))
    lines.append("```")
    lines.append("")
    lines.append("#### Response")
    lines.append("```json")
    # Keep nl/plan response readable: prefer compact view if huge
    resp = entry["responseBody"]
    if entry.get("path", "").endswith("/nl/plan") and isinstance(resp, dict):
        compact_resp = {
            "sessionId": resp.get("sessionId"),
            "status": resp.get("status"),
            "workflowId": resp.get("workflowId"),
            "workflowName": resp.get("workflowName"),
            "analysis_source": resp.get("analysis_source"),
            "mode": resp.get("mode"),
            "estimatedCost": resp.get("estimatedCost"),
            "sufficientBalance": resp.get("sufficientBalance"),
            "warnings": resp.get("warnings"),
            "plan": resp.get("plan"),
            "steps": resp.get("steps"),
            "skeletonPlan": resp.get("skeletonPlan"),
            "phaseGraphs_count": len(resp.get("phaseGraphs") or []),
            "phaseGraphs": resp.get("phaseGraphs"),
        }
        lines.append(_json_block(compact_resp, limit=20000))
        lines.append("```")
        lines.append("")
        lines.append(
            "_Full raw response also saved in `04_nl_plan_response.json` / `04_nl_plan_exchange.json`._"
        )
    else:
        lines.append(_json_block(resp, limit=12000))
        lines.append("```")
    lines.append("")


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = OUT_ROOT / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    api_log: List[Dict[str, Any]] = []

    print(f"Output dir: {out_dir}")
    print(f"Project: {PROJECT_ID}")
    print(f"Prompt: {WORKFLOW_PROMPT}")

    def post(path: str, body: Dict[str, Any], *, label: str, file_stem: str) -> Dict[str, Any]:
        return _post(path, body, label=label, api_log=api_log, out_dir=out_dir, file_stem=file_stem)

    # 0a) Cost estimate
    estimate_body = {
        "projectId": PROJECT_ID,
        "orgId": ORG_ID,
        "subscriberId": SUBSCRIBER_ID,
        "userId": USER_ID,
        "prompt": WORKFLOW_PROMPT,
        "selectedName": SELECTED_NAME,
    }
    estimate = post(
        "/aiqod-agent/agent/agentic/plan/estimate",
        estimate_body,
        label="1. plan/estimate",
        file_stem="00_plan_estimate",
    )
    predicted_cost = float(estimate.get("predictedCost") or 1.0)

    # 0b) Register
    register_body = {
        "type": "workflow",
        "projectId": PROJECT_ID,
        "subscriberId": SUBSCRIBER_ID,
        "orgId": ORG_ID,
        "userId": USER_ID,
        "predictedCost": predicted_cost,
        "workflowName": SELECTED_NAME,
        "selectedName": SELECTED_NAME,
        "selectedType": SELECTED_TYPE,
        "selectedCategory": SELECTED_CATEGORY,
        "reserveSubscriberCredits": False,
    }
    post(
        "/aiqod-agent/agent/agentic/workflow/register",
        register_body,
        label="2. workflow/register",
        file_stem="00_workflow_register",
    )

    # 1) Stage 0 start
    start_body = {
        "projectId": PROJECT_ID,
        "orgId": ORG_ID,
        "subscriberId": SUBSCRIBER_ID,
        "userId": USER_ID,
        "prompt": WORKFLOW_PROMPT,
        "selectedName": SELECTED_NAME,
        "selectedType": SELECTED_TYPE,
        "selectedCategory": SELECTED_CATEGORY,
    }
    start = post(
        "/aiqod-agent/agent/stage0/start",
        start_body,
        label="3. stage0/start",
        file_stem="01_stage0_start",
    )

    session_id = start.get("sessionId")
    if not session_id:
        raise SystemExit("No sessionId from stage0/start")

    # 2) Clarification turns
    current = start
    turn = 0
    answers_log: List[Dict[str, Any]] = []
    while current.get("status") == "needs_input" and turn < 12:
        turn += 1
        questions = current.get("questions") or []
        if not questions:
            answer = "Proceed with defaults"
            body = {"sessionId": session_id, "answer": answer, "skipRemaining": True}
        else:
            q = questions[0]
            answer = _pick_answer(q)
            body = {"sessionId": session_id, "answer": answer, "skipRemaining": False}

        print(f"\n--- Clarification turn {turn} ---")
        print("Q:", _q_text(questions[0]) if questions else "(none)")
        print("A:", answer)

        current = post(
            "/aiqod-agent/agent/stage0/respond",
            body,
            label=f"4.{turn} stage0/respond turn {turn}",
            file_stem=f"02_stage0_respond_turn{turn:02d}",
        )
        answers_log.append(
            {
                "turn": turn,
                "question": _summarize_questions({"questions": questions[:1]}) if questions else [],
                "answer": answer,
                "requestBody": body,
                "status_after": current.get("status"),
                "next_questions": _summarize_questions(current),
            }
        )

    if current.get("status") != "complete":
        print("WARNING: Stage 0 did not complete; forcing skipRemaining once.")
        body = {"sessionId": session_id, "answer": "Use defaults", "skipRemaining": True}
        current = post(
            "/aiqod-agent/agent/stage0/respond",
            body,
            label="4.x stage0/respond skipRemaining",
            file_stem="02_stage0_respond_force_complete",
        )

    _dump(out_dir / "03_stage0_final.json", current)
    req_summary = current.get("requirementSummary")

    # 3) NL plan
    plan_body = {
        "sessionId": session_id,
        "projectId": PROJECT_ID,
        "orgId": ORG_ID,
        "subscriberId": SUBSCRIBER_ID,
        "userId": USER_ID,
        "selectedName": SELECTED_NAME,
        "selectedType": SELECTED_TYPE,
        "selectedCategory": SELECTED_CATEGORY,
    }
    plan = post(
        "/aiqod-agent/agent/agentic/nl/plan",
        plan_body,
        label="5. nl/plan",
        file_stem="04_nl_plan",
    )

    analysis = _analyze_graph(plan)
    _dump(out_dir / "05_graph_analysis.json", analysis)
    _dump(out_dir / "00_api_log.json", api_log)

    compact = {
        "sessionId": session_id,
        "projectId": PROJECT_ID,
        "workflowPrompt": WORKFLOW_PROMPT,
        "stage0Status": current.get("status"),
        "stage0Answers": answers_log,
        "requirementSummary": req_summary,
        "planStatus": plan.get("status"),
        "workflowId": plan.get("workflowId"),
        "workflowName": plan.get("workflowName"),
        "estimatedCost": plan.get("estimatedCost"),
        "sufficientBalance": plan.get("sufficientBalance"),
        "analysis_source": plan.get("analysis_source"),
        "warnings": plan.get("warnings"),
        "preview": plan.get("plan"),
        "steps": plan.get("steps"),
        "skeletonPlan": plan.get("skeletonPlan"),
        "graph": ((plan.get("phaseGraphs") or [{}])[0].get("graph")),
        "analysis": analysis,
    }
    _dump(out_dir / "06_compact_result.json", compact)

    # ---- REPORT.md ----
    lines: List[str] = [
        f"# NL Builder E2E Run — {stamp}",
        "",
        f"- Base URL: `{BASE}`",
        f"- Project ID: `{PROJECT_ID}`",
        f"- Session ID: `{session_id}`",
        f"- Workflow: **{SELECTED_NAME}**",
        "",
        "## User prompt",
        "",
        WORKFLOW_PROMPT,
        "",
        "## API call log (request + response)",
        "",
        "Every call below shows the exact JSON body sent and the JSON response received.",
        "",
    ]
    for entry in api_log:
        _append_exchange_section(lines, entry)

    lines.extend(
        [
            "## Stage 0 clarification summary",
            "",
            f"Final status: **{current.get('status')}**",
            "",
        ]
    )
    for item in answers_log:
        qlist = item.get("question") or []
        qtext = qlist[0].get("prompt") if qlist else "(no question text)"
        lines.append(f"### Turn {item['turn']}")
        lines.append(f"- **Q:** {qtext}")
        lines.append(f"- **A:** {item['answer']}")
        lines.append(f"- **Status after:** `{item['status_after']}`")
        lines.append("")

    if req_summary:
        lines.append("### Requirement summary (handoff)")
        lines.append("```json")
        lines.append(_json_block(req_summary, limit=8000))
        lines.append("```")
        lines.append("")

    preview = plan.get("plan") or {}
    lines.extend(
        [
            "## Generated graph overview",
            "",
            f"- status: `{plan.get('status')}`",
            f"- analysis_source: `{plan.get('analysis_source')}`",
            f"- estimatedCost: `{plan.get('estimatedCost')}`",
            f"- nodeCount: `{preview.get('nodeCount')}`",
            f"- edgeCount: `{preview.get('edgeCount')}`",
            f"- ui/tool/router: `{preview.get('uiScreenCount')}` / `{preview.get('toolCount')}` / `{preview.get('routerCount')}`",
            "",
            "### Preview summary",
            "",
            preview.get("summary") or "(empty)",
            "",
            "### Graph nodes",
            "",
        ]
    )
    for ns in analysis.get("node_summaries") or []:
        lines.append(
            f"- `{ns['nodeId']}` (**{ns['type']}**) — {ns['name']}  \n"
            f"  ref=`{ns.get('capabilityRef')}` outputs={ns.get('declaredOutputs')}  \n"
            f"  {ns.get('description')}"
        )
    lines.append("")
    lines.append("### Edges")
    lines.append("")
    for e in analysis.get("edges") or []:
        cond = f" if `{e.get('condition')}`" if e.get("condition") else ""
        lines.append(f"- `{e.get('from')}` → `{e.get('to')}` ({e.get('kind')}){cond}")

    lines.extend(["", "## Correctness check", ""])
    lines.append(f"**Overall: {'PASS' if analysis.get('ok') else 'ISSUES FOUND'}**")
    lines.append("")
    if analysis.get("edge_case_checks"):
        lines.append("### Edge-case checks")
        lines.append("```json")
        lines.append(_json_block(analysis.get("edge_case_checks"), limit=4000))
        lines.append("```")
        lines.append("")
    lines.append("### Findings")
    for f in analysis.get("findings") or []:
        lines.append(f"- {f}")
    lines.append("")
    lines.append("### Issues")
    if analysis.get("issues"):
        for i in analysis["issues"]:
            lines.append(f"- {i}")
    else:
        lines.append("- None")

    lines.extend(
        [
            "",
            "## Artifacts",
            "",
            "- `00_api_log.json` — all exchanges",
            "- `*_request.json` / `*_response.json` / `*_exchange.json` — per call",
            "- `03_stage0_final.json`",
            "- `05_graph_analysis.json`",
            "- `06_compact_result.json`",
            "- `REPORT.md` (this file)",
            "",
        ]
    )

    report_path = out_dir / "REPORT.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")

    print("\n========== REPORT (head) ==========")
    print(report_path.read_text(encoding="utf-8")[:5000])
    print(f"\nFull report: {report_path}")
    print(f"PASS={analysis.get('ok')}")
    return 0 if analysis.get("ok") else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.HTTPError as exc:
        print("HTTPError:", exc)
        sys.exit(1)
    except Exception as exc:
        print("FAILED:", type(exc).__name__, exc)
        sys.exit(1)
