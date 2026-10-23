from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Union
from bson import ObjectId
from services.database import Database
import ast
import re
import uuid
from datetime import datetime
from services.gemini_service import async_generate, GeminiGenerateConfig
from services.llm_token_tracker import record_llm_call
from services.llm_utils import extract_token_usage_from_response
from services.workflow_budget_service import BillingError

_ASSIGNMENT_RULE_MODEL = "gemini-3.5-flash-lite"

# Combined Router
router = APIRouter(prefix="/aiqod-agent/agent")

# ==========================================
# 1. USER ENDPOINT LOGIC & MODELS (Unchanged)
# ==========================================

class PersonalInfo(BaseModel):
    name: Optional[str] = None
    mobile: Optional[str] = None

class UserResponse(BaseModel):
    id: str = Field(..., alias="_id")
    supervisor: Optional[str] = None
    userType: List[str]
    personalInfo: Optional[PersonalInfo] = None

    class Config:
        populate_by_name = True

class UserAPIResponse(BaseModel):
    message: str
    status: int
    data: List[UserResponse]

@router.get("/list/user", response_model=UserAPIResponse, tags=["Assignment"])
async def get_user(
    subscriber_id: str = Header(..., alias="subscriberId")
):
    if not ObjectId.is_valid(subscriber_id):
        return {"message": "Invalid subscriberId format", "status": 1, "data": []}

    query = {
        "isDeleted": False,
        "userType": {"$in": ["employee", "admin-sme"]},
        "subscriberId": ObjectId(subscriber_id)
    }

    projection = {
        "_id": 1, "supervisor": 1, "userType": 1, 
        "personalInfo.name": 1, "personalInfo.mobile": 1
    }

    try:
        collection = Database.get_collection("users")
        cursor = collection.find(query, projection)
        users = await cursor.to_list(length=None)

        for user in users:
            user["_id"] = str(user["_id"])
            if "supervisor" in user and isinstance(user["supervisor"], ObjectId):
                user["supervisor"] = str(user["supervisor"])

        return {
            "message": "All users" if users else "Users not found",
            "status": 0 if users else 1,
            "data": users
        }
    except Exception as e:
        print(f"Error in getUser: {e}")
        return {"message": "Internal server error", "status": 1, "data": []}


# ==========================================
# 2. ROLE ENDPOINT LOGIC & MODELS (FINAL)
# ==========================================

class RoleModel(BaseModel):
    id: str = Field(..., alias="_id")
    roleName: Optional[str] = None
    roleDesc: Optional[str] = None
    roleType: Optional[str] = None
    level: Optional[int] = None
    childRole: Optional[List[str]] = []

    class Config:
        populate_by_name = True
        extra = 'ignore'

class RoleResponseData(BaseModel):
    roleData: List[RoleModel]
    currRole: Optional[RoleModel] = None

class RoleAPIResponse(BaseModel):
    message: str
    status: int
    data: Optional[RoleResponseData] = None

@router.get("/role", response_model=RoleAPIResponse, tags=["Assignment"])
async def get_all_roles(
    org_id: str = Header(..., alias="selectedorgid"),  
    subscriber_id: str = Header(..., alias="subscriberId"),
    current_role_id: Optional[str] = Header(None, alias="role") 
):
    # 1. Validate IDs immediately
    if not (ObjectId.is_valid(org_id) and ObjectId.is_valid(subscriber_id)):
        return {"message": "Invalid ID format", "status": 1, "data": None}

    try:
        collection = Database.get_collection("roles")

        # 2. Query: Matches Org & Subscriber OR SuperRole
        query = {
            "isDeleted": False,
            "$or": [
                {
                    "$and": [
                        {"orgId": ObjectId(org_id)},
                        {"subscriberId": ObjectId(subscriber_id)}
                    ]
                },
                {"isSuperRole": True}
            ]
        }

        # 3. Projection: Only fetch fields defined in your RoleModel
        projection = {
            "_id": 1, "roleName": 1, "roleDesc": 1, 
            "roleType": 1, "childRole": 1, "level": 1
        }

        cursor = collection.find(query, projection)
        roles_raw = await cursor.to_list(length=None)

        formatted_roles = []
        current_role_obj = None

        for role in roles_raw:
            # Convert ObjectId to string for Pydantic
            role["_id"] = str(role["_id"])
            
            formatted_roles.append(role)

            # Identify the current user's role for the 'currRole' field
            if current_role_id and role["_id"] == current_role_id:
                current_role_obj = role

        if not formatted_roles:
            return {"message": "No roles found", "status": 0, "data": {"roleData": [], "currRole": None}}

        return {
            "message": "Role found successfully",
            "status": 0,
            "data": {
                "roleData": formatted_roles,
                "currRole": current_role_obj
            }
        }

    except Exception as e:
        print(f"Error in get_all_roles: {e}")
        return {"message": "Internal server error", "status": 1, "data": None}

# 3. RULE-BASED (PROMPT-BASED) ASSIGNMENT — GENERATION

REQUIRED_FN_NAME = "resolve_assignee"
REQUIRED_FN_ARGS = {"global_inputs", "node_outputs", "chain_position"}
FORBIDDEN_NAMES = {
    "__import__", "eval", "exec", "compile", "open", "input", "os", "sys",
    "subprocess", "socket", "importlib", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "breakpoint", "exit", "quit",
}


class GenerateRuleRequest(BaseModel):
    prompt: str = Field(..., description="Natural language assignment rule, e.g. 'Assign to the roles defined in Screen 1'")
    nodeId: str
    workflowId: Optional[str] = None
    projectId: Optional[str] = None
    userId: Optional[str] = None
    apiKey: Optional[str] = Field(default=None, description="Optional per-subscriber Gemini key; falls back to default credentials if omitted")
    availableNodeOutputs: Optional[Dict[str, List[str]]] = Field(
        default=None,
        description="Map of {nodeId: [outputKey, ...]} for nodes that run before this one, "
                    "so the LLM knows what fields it can actually read from node_outputs.",
    )
    availableGlobalInputs: Optional[List[str]] = Field(
        default=None,
        description="Known global_inputs field names for this workflow (e.g. from "
                    "requiredGlobalInputs), so the LLM doesn't invent/typo key names.",
    )


class GenerateRuleResponse(BaseModel):
    message: str
    status: int
    assignmentRef: Optional[str] = None
    function_code: Optional[str] = None


def _validate_resolve_assignee_code(code: str) -> None:
    """
    Statically validates LLM-generated assignment-rule code before it is ever
    persisted or exec()'d. Raises ValueError with a human-readable reason on
    any violation. This is a hard safety gate — assignment rules run via
    exec() at execution time (executor.py), so nothing beyond a pure,
    side-effect-free function is allowed through.
    """
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        raise ValueError(f"Generated code is not valid Python: {e}")

    top_level_funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    if len(top_level_funcs) != 1 or top_level_funcs[0].name != REQUIRED_FN_NAME:
        raise ValueError(
            f"Generated code must contain exactly one top-level function named "
            f"'{REQUIRED_FN_NAME}'."
        )

    fn = top_level_funcs[0]
    arg_names = {a.arg for a in fn.args.args}
    if arg_names != REQUIRED_FN_ARGS:
        raise ValueError(
            f"'{REQUIRED_FN_NAME}' must take exactly these arguments: "
            f"{sorted(REQUIRED_FN_ARGS)}. Found: {sorted(arg_names)}."
        )

    # Disallow any other top-level statements (imports, class defs, etc.)
    for node in tree.body:
        if node is not fn:
            raise ValueError(
                "Generated code must contain nothing besides the "
                f"'{REQUIRED_FN_NAME}' function definition."
            )

    # Walk the function body and reject dangerous constructs.
    for node in ast.walk(fn):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError("Imports are not allowed inside assignment rule functions.")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise ValueError(f"Use of '{node.id}' is not allowed inside assignment rule functions.")
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            raise ValueError(f"Use of '.{node.attr}' is not allowed inside assignment rule functions.")


def _build_rule_generation_prompt(
    req: "GenerateRuleRequest",
    users: List[Dict[str, Any]],
    roles: List[Dict[str, Any]],
) -> str:
    outputs_hint = ""
    if req.availableNodeOutputs:
        lines = [f"  - {nid}: {keys}" for nid, keys in req.availableNodeOutputs.items()]
        outputs_hint = "Fields available in node_outputs (populated by prior nodes):\n" + "\n".join(lines)
    else:
        outputs_hint = "No prior node outputs are available to this node."

    if req.availableGlobalInputs:
        inputs_hint = "Field names available in global_inputs (use ONLY these — do not invent or guess key names):\n" + \
            "\n".join(f"  - {k}" for k in req.availableGlobalInputs)
    else:
        inputs_hint = (
            "No global_inputs field list was supplied. Do NOT invent field names — "
            "if the instruction references a value you cannot confirm exists, use "
            "global_inputs.get('<best_guess_key>') defensively and treat a missing "
            "value as inconclusive (fall through to the default branch)."
        )

    users_hint = "\n".join(f"  - id=\"{u['_id']}\" name=\"{u.get('name', '')}\"" for u in users) or "  (none available)"
    roles_hint = "\n".join(f"  - id=\"{r['_id']}\" name=\"{r.get('name', '')}\"" for r in roles) or "  (none available)"

    return f"""You write exactly ONE Python function that resolves who should be
assigned a human-approval task in a workflow engine.

Function signature (must match EXACTLY):
def resolve_assignee(global_inputs, node_outputs, chain_position):
    ...

Rules:
- global_inputs: dict of workflow-level inputs (execution_id, workflow_id, and any caller-supplied fields).
- node_outputs: dict of {{nodeId: {{outputKey: value}}}} for every node that has already run.
- chain_position: int, starts at 0, increments by 1 every time this node is resumed
  (used to walk an ordered list of approvers on sequential/looping approval chains).
- Pure function only: no imports, no I/O, no exec/eval, no network or filesystem access.
- Use only global_inputs, node_outputs, and chain_position — do not reference anything else.
- Return ONLY the function code. No explanation, no markdown fences, no extra text.

RETURN VALUE — CRITICAL, must follow exactly:
- You must return one of:
    {{"type": "user", "id": "<exact id from the Available users list below>"}}
    {{"type": "role", "id": "<exact id from the Available roles list below>"}}
    None   (if no assignee can be resolved yet)
- The "id" value MUST be copied verbatim from the lists below. NEVER invent, guess,
  or make up a name or id (e.g. do not return "admin" or "approver1" as a name —
  look up the actual person/role with that meaning in the lists below and use their id).
- If the instruction refers to a role or person that does not clearly match any
  entry in the lists below, return None rather than guessing.

Available users (assignable individuals):
{users_hint}

Available roles (assignable roles):
{roles_hint}

{inputs_hint}

{outputs_hint}

User's assignment instruction:
\"\"\"{req.prompt}\"\"\"
"""


async def _fetch_assignable_users_and_roles(
    subscriber_id: str, org_id: Optional[str]
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Fetches the real, current users and roles for this org/subscriber so the
    rule-generation prompt can ground the LLM on actual IDs instead of it
    inventing role/user names. Mirrors the query logic in get_user/get_all_roles.
    """
    users: List[Dict[str, Any]] = []
    roles: List[Dict[str, Any]] = []

    if ObjectId.is_valid(subscriber_id):
        users_collection = Database.get_collection("users")
        user_query = {
            "isDeleted": False,
            "userType": {"$in": ["employee", "admin-sme"]},
            "subscriberId": ObjectId(subscriber_id),
        }
        cursor = users_collection.find(
            user_query, {"_id": 1, "personalInfo.name": 1}
        )
        async for u in cursor:
            users.append({
                "_id": str(u["_id"]),
                "name": (u.get("personalInfo") or {}).get("name") or "",
            })

    if org_id and ObjectId.is_valid(org_id) and ObjectId.is_valid(subscriber_id):
        roles_collection = Database.get_collection("roles")
        role_query = {
            "isDeleted": False,
            "$or": [
                {"$and": [{"orgId": ObjectId(org_id)}, {"subscriberId": ObjectId(subscriber_id)}]},
                {"isSuperRole": True},
            ],
        }
        cursor = roles_collection.find(role_query, {"_id": 1, "roleName": 1})
        async for r in cursor:
            roles.append({"_id": str(r["_id"]), "name": r.get("roleName") or ""})

    return users, roles


def _extract_returned_ids(code: str) -> List[str]:
    """
    Best-effort extraction of every 'id': "<value>" literal the generated
    function returns, so we can cross-check them against the real user/role
    id lists before trusting the rule (defense-in-depth beyond the prompt
    instructions alone).
    """
    return re.findall(r"[\"']id[\"']\s*:\s*[\"']([^\"']+)[\"']", code)


@router.post("/assignment/generate-rule", response_model=GenerateRuleResponse, tags=["Assignment"])
async def generate_assignment_rule(
    req: GenerateRuleRequest,
    subscriber_id: str = Header(..., alias="subscriberId"),
    org_id: Optional[str] = Header(None, alias="selectedorgid"),
):
    """
    Phase 1 — Rule-based (prompt-based) assignment.
    Generates a resolve_assignee() Python function from a natural-language
    prompt via a single LLM call, validates it statically, and persists it to
    assignment_registry. Returns the function for the frontend modal to
    preview before the user commits it via /assignment/save (executor
    service), which is what actually attaches the assignmentRef to the node.
    """
    try:
        users, roles = await _fetch_assignable_users_and_roles(subscriber_id, org_id)
        valid_ids = {u["_id"] for u in users} | {r["_id"] for r in roles}

        prompt_text = _build_rule_generation_prompt(req, users, roles)

        started_at = datetime.utcnow()
        try:
            response = await async_generate(
                _ASSIGNMENT_RULE_MODEL,
                prompt_text,
                config=GeminiGenerateConfig(
                    temperature=0,
                    response_mime_type="text/plain",
                ),
                api_key=req.apiKey,
            )
            finished_at = datetime.utcnow()
            usage = extract_token_usage_from_response(response)
            record_llm_call(
                project_id=req.projectId, org_id=org_id,
                subscriber_id=subscriber_id, user_id=req.userId,
                operation="assignment", sub_operation="assignment_rule_generation",
                model=_ASSIGNMENT_RULE_MODEL,
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                total_tokens=int(usage.get("total_tokens", 0) or 0),
                status="success", attempt=1, is_retry=False,
                started_at=started_at, finished_at=finished_at,
                metadata={"nodeId": req.nodeId, "workflowId": req.workflowId},
            )
            raw_code = response.text or ""
        except BillingError as be:
            return {
                "message": f"Cannot generate assignment rule: {be}",
                "status": 1,
                "assignmentRef": None,
                "function_code": None,
            }
        except Exception as exc:
            finished_at = datetime.utcnow()
            record_llm_call(
                project_id=req.projectId, org_id=org_id,
                subscriber_id=subscriber_id, user_id=req.userId,
                operation="assignment", sub_operation="assignment_rule_generation",
                model=_ASSIGNMENT_RULE_MODEL,
                prompt_tokens=0, completion_tokens=0, total_tokens=0,
                status="failed", attempt=1, is_retry=False,
                started_at=started_at, finished_at=finished_at,
                metadata={"nodeId": req.nodeId, "error": str(exc)},
            )
            raise

        # Strip markdown fences if the model added them despite instructions.
        cleaned = re.sub(r"^```(?:python)?\s*|\s*```$", "", raw_code.strip(), flags=re.MULTILINE).strip()

        try:
            _validate_resolve_assignee_code(cleaned)
        except ValueError as ve:
            return {
                "message": f"Generated rule failed validation: {ve}",
                "status": 1,
                "assignmentRef": None,
                "function_code": cleaned,
            }

        # Defense-in-depth: reject if the function references any id that
        # doesn't correspond to a real user or role — catches cases where the
        # LLM invents a name/id (e.g. "admin", "approver1") despite instructions.
        referenced_ids = _extract_returned_ids(cleaned)
        unknown_ids = [rid for rid in referenced_ids if rid not in valid_ids]
        if unknown_ids:
            return {
                "message": (
                    "Generated rule references id(s) that don't match any real "
                    f"user or role: {unknown_ids}. Try rephrasing the instruction "
                    "to name the exact person or role more clearly, or add them "
                    "as a user/role first."
                ),
                "status": 1,
                "assignmentRef": None,
                "function_code": cleaned,
            }

        assignment_ref = f"func_{uuid.uuid4().hex[:10]}"
        collection = Database.get_collection("assignment_registry")
        await collection.update_one(
            {"_id": assignment_ref},
            {
                "$set": {
                    "nodeId": req.nodeId,
                    "workflowId": req.workflowId,
                    "projectId": req.projectId,
                    "orgId": ObjectId(org_id) if org_id and ObjectId.is_valid(org_id) else org_id,
                    "subscriberId": ObjectId(subscriber_id) if ObjectId.is_valid(subscriber_id) else subscriber_id,
                    "prompt": req.prompt,
                    "function_code": cleaned,
                    "updated_at": datetime.utcnow(),
                },
                "$setOnInsert": {"created_at": datetime.utcnow()},
            },
            upsert=True,
        )

        return {
            "message": "Rule generated successfully",
            "status": 0,
            "assignmentRef": assignment_ref,
            "function_code": cleaned,
        }
    except Exception as e:
        print(f"Error in generate_assignment_rule: {e}")
        return {
            "message": f"Failed to generate assignment rule: {e}",
            "status": 1,
            "assignmentRef": None,
            "function_code": None,
        }