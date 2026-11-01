import importlib.util
import sys
import json
import os
import re
import textwrap
import logging
from datetime import datetime
from typing import Any, Dict, List, ClassVar, Optional
import traceback
from enum import Enum
from dataclasses import dataclass
import inspect
from langgraph.types import Command
from langchain_core.messages import HumanMessage
from crewai.tools import BaseTool
from app.result_envelope import (
    ToolNodeFailed,
    interpret_tool_result,
    is_tool_error_payload,
    pick_tool_error_assignee,
    run_tool_until_success,
    tool_error_sse_payloads,
)

logger = logging.getLogger("workflow_executor")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_handler)
logger.setLevel(os.getenv("EXECUTOR_LOG_LEVEL", "INFO"))

try:
    from pymongo import MongoClient  # type: ignore
except Exception:  # pragma: no cover
    MongoClient = None  # type: ignore

try:
    from app.observability import (
        get_session,
        maybe_session_for_instance,
        reset_session,
        set_session,
    )
except Exception:  # pragma: no cover
    get_session = None  # type: ignore
    maybe_session_for_instance = None  # type: ignore
    reset_session = None  # type: ignore
    set_session = None  # type: ignore


_LLM_TOKENS_COLLECTION = None  # populated on first successful connect
_LLM_TOKENS_COLLECTION_FAILED = False  # set True after a permanent config error

_CREDENTIALS_COLLECTION = None  # populated on first successful connect
_CREDENTIALS_COLLECTION_FAILED = False  # set True after a permanent config error


def _get_llm_tokens_collection():
    """
    Lazy-load Mongo collection handle for `llm_token_count`.
    Never raises — returns None if Mongo isn't configured/available.
    """
    global _LLM_TOKENS_COLLECTION, _LLM_TOKENS_COLLECTION_FAILED
    if _LLM_TOKENS_COLLECTION is not None:
        return _LLM_TOKENS_COLLECTION
    if _LLM_TOKENS_COLLECTION_FAILED:
        return None

    if MongoClient is None:
        return None

    try:
        # Prefer langraph/config.json; fall back to repo-root config.json
        here = os.path.dirname(__file__)
        langraph_root = os.path.dirname(here)  # .../langraph
        cfg_candidates = [
            os.path.join(langraph_root, "config.json"),
            os.path.join(os.getcwd(), "config.json"),
        ]
        cfg = {}
        for p in cfg_candidates:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    cfg = json.load(f) or {}
                break
            except Exception:
                continue

        mongo_uri = cfg.get("devDbLink") or cfg.get("MONGODB_URI")
        mongo_db = cfg.get("devDb") or cfg.get("MONGODB_DB_NAME")
        if not mongo_uri or not mongo_db:
            print("[LLM token tracker] No Mongo URI/DB in config — token tracking disabled.")
            _LLM_TOKENS_COLLECTION_FAILED = True
            return None

        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
        db = client[mongo_db]
        _LLM_TOKENS_COLLECTION = db["llm_token_count"]
        print(f"[LLM token tracker] Connected to MongoDB db='{mongo_db}' collection='llm_token_count'")
        return _LLM_TOKENS_COLLECTION
    except Exception as e:
        print(f"[LLM token tracker] Failed to connect to MongoDB: {e}")
        _LLM_TOKENS_COLLECTION_FAILED = True
        return None


def _get_credentials_collection():
    """
    Lazy-load Mongo collection handle for `credentials`.
    Uses the same MONGO_URI / MONGO_DB_NAME env vars as the rest of the executor
    service (loaded via load_dotenv() in main.py). Falls back to config.json values.
    Never raises — returns None if Mongo isn't configured/available.
    """
    global _CREDENTIALS_COLLECTION, _CREDENTIALS_COLLECTION_FAILED
    if _CREDENTIALS_COLLECTION is not None:
        return _CREDENTIALS_COLLECTION
    if _CREDENTIALS_COLLECTION_FAILED:
        return None

    if MongoClient is None:
        return None

    try:
        # Prefer env vars (same source as database.py / MongoDBClient used by the executor).
        # Fall back to config.json for deployments that use that file instead.
        mongo_uri = os.getenv("MONGO_URI")
        mongo_db = os.getenv("MONGO_DB_NAME")

        if not mongo_uri or not mongo_db:
            here = os.path.dirname(__file__)
            langraph_root = os.path.dirname(here)
            cfg_candidates = [
                os.path.join(langraph_root, "config.json"),
                os.path.join(os.getcwd(), "config.json"),
            ]
            cfg = {}
            for p in cfg_candidates:
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        cfg = json.load(f) or {}
                    break
                except Exception:
                    continue

            mongo_uri = mongo_uri or cfg.get("devDbLink") or cfg.get("MONGODB_URI")
            mongo_db = mongo_db or cfg.get("devDb") or cfg.get("MONGODB_DB_NAME")

        if not mongo_uri or not mongo_db:
            print("[Credentials] No Mongo URI/DB configured — credential injection disabled.")
            _CREDENTIALS_COLLECTION_FAILED = True
            return None

        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
        db = client[mongo_db]
        _CREDENTIALS_COLLECTION = db["credentials"]
        print(f"[Credentials] Connected to MongoDB db='{mongo_db}' collection='credentials'")
        return _CREDENTIALS_COLLECTION
    except Exception as e:
        print(f"[Credentials] Failed to connect to MongoDB: {e}")
        _CREDENTIALS_COLLECTION_FAILED = True
        return None


def _fetch_credentials_for_project(project_id: str) -> dict:
    """
    Synchronously fetch and decrypt all credentials for a project.
    Uses AES-GCM decryption with CRED_MASTER_KEY from environment.
    Returns a dict of {credential_name: decrypted_value}.
    Never raises — returns {} on any error so tool execution can still proceed.
    """
    if not project_id:
        return {}

    master_key_b64 = os.getenv("CRED_MASTER_KEY")
    if not master_key_b64:
        print("[Credentials] CRED_MASTER_KEY not set — credential injection skipped.")
        return {}

    try:
        import base64 as _b64
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        master_key = _b64.b64decode(master_key_b64)
        aesgcm = AESGCM(master_key)
    except Exception as e:
        print(f"[Credentials] Failed to initialise AES-GCM decryption: {e}")
        return {}

    coll = _get_credentials_collection()
    if coll is None:
        return {}

    try:
        docs = list(coll.find({"project_id": project_id}))
    except Exception as e:
        print(f"[Credentials] Failed to query credentials for project '{project_id}': {e}")
        return {}

    result = {}
    for doc in docs:
        name = doc.get("name")
        if not name:
            continue
        try:
            ciphertext = _b64.b64decode(doc["encrypted_value"])
            iv = _b64.b64decode(doc["iv"])
            tag = _b64.b64decode(doc["tag"])
            full_ciphertext = ciphertext + tag
            plaintext = aesgcm.decrypt(iv, full_ciphertext, None).decode("utf-8")
            result[name] = plaintext
        except Exception as e:
            print(f"[Credentials] Failed to decrypt credential '{name}': {e}")

    print(f"[Credentials] Loaded {len(result)} credential(s) for project '{project_id}': {list(result.keys())}")
    return result


def _extract_gemini_usage_from_message(msg: Any) -> dict:
    """
    Extract {prompt_tokens, completion_tokens, total_tokens} from a LangChain AIMessage
    that carries Gemini usage metadata. Returns zeros if not present.

    LangChain AIMessage stores usage as a plain dict:
        usage_metadata = {'input_tokens': N, 'output_tokens': N, 'total_tokens': N, ...}
    """
    try:
        usage = getattr(msg, "usage_metadata", None)
        if not usage:
            return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        # LangChain dict format (most common — all modern LangChain versions)
        if isinstance(usage, dict):
            prompt_tokens = int(usage.get("input_tokens", 0) or 0)
            completion_tokens = int(usage.get("output_tokens", 0) or 0)
            total_tokens = int(usage.get("total_tokens", 0) or 0) or (prompt_tokens + completion_tokens)
            return {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            }

        # Google genai SDK object fallback (.prompt_token_count etc.)
        prompt_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        completion_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        total_tokens = int(getattr(usage, "total_token_count", 0) or 0) or (prompt_tokens + completion_tokens)
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }
    except Exception:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _collect_llm_messages_from_outputs(outputs: dict) -> list:
    """
    Return every LangChain message object that carries usage_metadata found anywhere
    in a stream-event outputs dict.

    DYNAMIC subgraph events arrive as:
        {"llm_node": {"messages": [<AIMessage>, ...], "iteration_count": N}}

    Non-subgraph events may arrive as:
        {"messages": [<AIMessage>, ...]}

    This function handles both shapes so token tracking always fires.
    """
    collected: list = []
    if not isinstance(outputs, dict):
        return collected

    def _extract_msgs(val: Any) -> None:
        if isinstance(val, list):
            for item in val:
                # Only collect real message objects (have usage_metadata or content attr),
                # not plain strings that were serialised by json.dumps(..., default=str).
                if hasattr(item, "usage_metadata") or hasattr(item, "content"):
                    collected.append(item)
        elif isinstance(val, dict):
            if "messages" in val:
                _extract_msgs(val["messages"])

    # Top-level messages key
    if "messages" in outputs:
        _extract_msgs(outputs["messages"])
    # Nested under node-name keys (subgraph events)
    for val in outputs.values():
        if isinstance(val, dict) and "messages" in val:
            _extract_msgs(val["messages"])

    return collected


def _save_llm_token_count(
    *,
    execution_id: str,
    workflow_id: str,
    thread_id: str,
    node_name: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    status: str,
    metadata: Optional[dict] = None,
) -> None:
    """
    Insert one document into Mongo `llm_token_count`.
    Best-effort only; never raises.
    """
    try:
        col = _get_llm_tokens_collection()
        if col is None:
            print(f"[LLM token tracker] Skipping insert — no collection handle (execution_id={execution_id})")
            return
        doc = {
            "process": "ai-screen-builder",
            "operation": "workflow-execution",
            "sub_operation": "dynamic-llm-node",
            "execution_id": execution_id,
            "workflow_id": workflow_id,
            "thread_id": thread_id,
            "node_name": node_name,
            "model": model,
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "total_tokens": int(total_tokens or 0),
            "status": status,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "metadata": metadata or {},
        }
        col.insert_one(doc)
        print(f"[LLM token tracker] Inserted into llm_token_count: execution_id={execution_id} total_tokens={total_tokens}")
    except Exception as e:
        print(f"[LLM token tracker] insert_one failed: {e}")

class ParameterType(Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DICT = "dict"
    LIST = "list"
    ANY = 'any'

@dataclass
class ToolParameter:
    name: str
    param_type: ParameterType
    description: str = ""
    required: bool = True
    default_value: Any = None
    inputVarDesc: str = ""

@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: List[ToolParameter]
    keywords: List[str] = None
    function_code: str = ""
    imports: List[str] = None


def _ensure_tool_packages(import_statements: List[str], extra_source: str = "") -> dict:
    """
    Auto-install missing third-party packages for tools (parity with UI sandbox functions).

    Prefers backend services.package_installer when available; falls back to a minimal
    pip install of top-level module names extracted from import statements.
    """
    try:
        from services.package_installer import get_package_installer

        return get_package_installer().ensure_packages_for_imports(
            list(import_statements or []),
            extra_source=extra_source or "",
        )
    except Exception:
        pass

    # Fallback when backend package_installer is not on PYTHONPATH
    import subprocess
    import importlib.util

    installed: List[str] = []
    failed: List[str] = []
    modules: set = set()
    for stmt in import_statements or []:
        text = str(stmt or "").strip()
        if not text:
            continue
        if text.startswith("from "):
            # from reportlab.lib.pagesizes import letter
            parts = text.split()
            if len(parts) >= 2:
                modules.add(parts[1].split(".")[0])
        elif text.startswith("import "):
            # import reportlab / import a, b
            rest = text[len("import "):].strip()
            for piece in rest.split(","):
                piece = piece.strip().split(" as ")[0].strip()
                if piece:
                    modules.add(piece.split(".")[0])

    stdlib = set(getattr(sys, "stdlib_module_names", set())) | set(sys.builtin_module_names)
    skip = stdlib | {"bson", "pydantic", "motor", "pymongo", "crewai", "langchain_core"}
    for mod in sorted(modules):
        if not mod or mod in skip:
            continue
        try:
            if importlib.util.find_spec(mod) is not None:
                continue
        except Exception:
            pass
        try:
            print(f"[ToolRegistry] auto-installing package: {mod}")
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet", mod],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                installed.append(mod)
                try:
                    import importlib

                    importlib.invalidate_caches()
                except Exception:
                    pass
            else:
                failed.append(mod)
                print(f"[ToolRegistry] auto-install failed for {mod}: {result.stderr or result.stdout}")
        except Exception as exc:
            failed.append(mod)
            print(f"[ToolRegistry] auto-install error for {mod}: {exc}")

    return {"installed": installed, "failed": failed, "already_installed": not installed and not failed}


class LocalToolRegistry:
    def __init__(self, json_dir: str = "tools"):
        self.json_dir = json_dir
        self.available_tools: Dict[str, BaseTool] = {}
        self.tool_meta: Dict[str, dict] = {}
        self._load_tools()
    
    def _load_single_tool(self, file_path: str):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                json_data = json.load(f)
            
            parameters = []
            try:
                from services.tool_param_utils import coerce_required_flag
            except ImportError:
                coerce_required_flag = lambda v, default=True: bool(v) if v is not None else default  # noqa: E731
            for param_data in json_data.get('parameters', []):
                parameters.append(ToolParameter(
                    name=param_data['name'],
                    param_type=ParameterType.ANY,
                    description=param_data.get('description', ''),
                    required=coerce_required_flag(param_data.get('required'), default=True),
                    default_value=param_data.get('default_value')
                ))
            
            tool_def = ToolDefinition(
                name=json_data['name'],
                description=json_data['description'],
                parameters=parameters,
                keywords=json_data.get('keywords', []),
                function_code=json_data['function_code'],
                imports=json_data.get('imports', [])
            )
            
            tool_class = self.create_dynamic_tool_class(tool_def)
            tool_instance = tool_class()
            tool_key = tool_def.name.replace(' ', '')
            self.available_tools[tool_key] = tool_instance
            
            # Record the file path and modification timestamp
            self.tool_meta[tool_key] = {
                "file_path": file_path,
                "mtime": os.path.getmtime(file_path)
            }
        except Exception as e:
            print(f"[ToolRegistry] Failed to load {file_path}: {e}")

    def _load_tools(self):
        """Loads all JSON tools from the local directory into memory."""
        if not os.path.exists(self.json_dir):
            os.makedirs(self.json_dir, exist_ok=True)
            return
            
        for filename in os.listdir(self.json_dir):
            if filename.endswith('.json'):
                file_path = os.path.join(self.json_dir, filename)
                self._load_single_tool(file_path)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        json_data = json.load(f)
                    
                    parameters = []
                    try:
                        from services.tool_param_utils import coerce_required_flag
                    except ImportError:
                        coerce_required_flag = lambda v, default=True: bool(v) if v is not None else default  # noqa: E731
                    for param_data in json_data.get('parameters', []):
                        parameters.append(ToolParameter(
                            name=param_data['name'],
                            param_type=ParameterType.ANY,
                            description=param_data.get('description', ''),
                            required=coerce_required_flag(param_data.get('required'), default=True),
                            default_value=param_data.get('default_value')
                        ))
                    
                    tool_def = ToolDefinition(
                        name=json_data['name'],
                        description=json_data['description'],
                        parameters=parameters,
                        keywords=json_data.get('keywords', []),
                        function_code=json_data['function_code'],
                        imports=json_data.get('imports', [])
                    )
                    
                    tool_class = self.create_dynamic_tool_class(tool_def)
                    tool_instance = tool_class()
                    tool_key = tool_def.name.replace(' ', '')
                    self.available_tools[tool_key] = tool_instance
                except Exception as e:
                    print(f"[ToolRegistry] Failed to load {filename}: {e}")

    def create_dynamic_tool_class(self, tool_def: ToolDefinition) -> type:
        """Compiles the JSON function code into a CrewAI BaseTool class."""
        exec_globals = {
            'BaseTool': BaseTool,
            'ClassVar': ClassVar,
            'List': List,
            'Any': Any,
            '__builtins__': __builtins__
        }
        
        if tool_def.imports:
            _ensure_tool_packages(
                list(tool_def.imports or []),
                extra_source=getattr(tool_def, "function_code", "") or "",
            )
            for import_stmt in tool_def.imports:
                try:
                    exec(import_stmt, exec_globals)
                except ImportError as e:
                    _ensure_tool_packages([import_stmt])
                    try:
                        exec(import_stmt, exec_globals)
                    except ImportError as retry_e:
                        print(f"Warning: Could not import {import_stmt} after auto-install: {retry_e}")

        class_name = re.sub(r"\W|^(?=\d)", "_", tool_def.name)
        safe_name = json.dumps(tool_def.name)
        safe_desc = json.dumps(tool_def.description)
        
        class_code = f"""
class {class_name}(BaseTool):
    name: str = {safe_name}
    description: str = {safe_desc}
"""
        if tool_def.keywords:
            class_code += f"    keywords: ClassVar[List[str]] = {tool_def.keywords}\n"
        
        class_code += "\n    def _run(self"
        try:
            from services.tool_param_utils import format_tool_run_params

            class_code += format_tool_run_params(tool_def.parameters)
        except ImportError:
            # Python forbids a required arg after an optional one. Keep required
            # (no-default) params first when the helper module is unavailable.
            leading = []
            trailing = []
            for param in tool_def.parameters:
                if (not param.required) or (param.default_value is not None):
                    trailing.append(param)
                else:
                    leading.append(param)
            for param in leading + trailing:
                if not param.required:
                    class_code += f", {param.name}: Any = {repr(param.default_value)}"
                elif param.default_value is not None:
                    class_code += f", {param.name}: Any = {repr(param.default_value)}"
                else:
                    class_code += f", {param.name}: Any"
        class_code += ", request_meta: Any = None) -> Any:\n"
        class_code += "        if request_meta is None:\n"
        class_code += "            request_meta = {}\n"

        clean_code = tool_def.function_code.replace('\r\n', '\n').replace('\t', '    ')
        
        # Fix 1: Strip out rogue base-level indentation
        clean_code = textwrap.dedent(clean_code).strip()
        
        raw_lines = clean_code.split('\n')
        healed_lines = []
        
        # ASCII constants for absolute safety against copy-paste corruption
        DOUBLE_QUOTE = chr(34)
        SINGLE_QUOTE = chr(39)
        
        # Fix 2: Heal unterminated strings by merging dangling quotes to the previous line
        for line in raw_lines:
            stripped = line.strip()
            
            # Check if line is just a dangling quote or quote+comma
            is_dangling = stripped in (
                DOUBLE_QUOTE, 
                SINGLE_QUOTE, 
                DOUBLE_QUOTE + ',', 
                SINGLE_QUOTE + ','
            )
            
            if is_dangling and healed_lines:
                healed_lines[-1] += stripped
            else:
                healed_lines.append(line)

        # Fix 3: Auto-level rogue indentation (The AI hallucination fixer)
        for line in healed_lines:
            class_code += f"        {line}\n"
        
        try:
            class_code = textwrap.dedent(class_code)
            exec(class_code, exec_globals)
            return exec_globals[class_name]
        except Exception as e:
            print(f"\n[DEBUG] FAILED CODE FOR {tool_def.name}:\n{'-'*40}\n{class_code}\n{'-'*40}\n")
            raise Exception(f"Failed to create dynamic tool class: {e}")
            
    def execute_tool(self, tool_name: str, params: dict, request_meta: dict = None) -> Any:
        """
        Safely executes the tool, handling shape-shifting (wrapping/unwrapping) 
        and stripping rogue parameters to prevent TypeErrors.
        """
        tool_key = tool_name.replace(' ', '')
        meta = self.tool_meta.get(tool_key)
        if meta:
            try:
                current_mtime = os.path.getmtime(meta["file_path"])
                if current_mtime > meta["mtime"]:
                    print(f"[ToolRegistry] File changed, hot-reloading tool: {tool_name}")
                    self._load_single_tool(meta["file_path"])
            except OSError:
                pass

        tool = self.available_tools.get(tool_key)

        if not tool:
            # Attempt hot-reload if the tool was just generated
            self._load_tools()
            tool = self.available_tools.get(tool_key)
            if not tool:
                return {"error": f"Tool '{tool_name}' not found in registry."}

        effective_meta = request_meta if request_meta is not None else {}
        try:
            run_method = getattr(tool.__class__, "_run", getattr(tool.__class__, "__call__", None))
            sig = inspect.signature(run_method)
            
            expected_params = {
                name for name, p in sig.parameters.items() 
                if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            }
            expected_params -= {"self", "request_meta", "args", "kwargs"} # Remove internal kwargs
            
            final_params = {}
            
            # Scenario 1: Auto-Wrap (Tool wants 1 param, LLM sent multiple flat vars)
            if len(expected_params) == 1:
                target_param = list(expected_params)[0]
                if target_param not in params:
                    # Scoop everything into the single parameter the tool asked for
                    final_params[target_param] = params
                else:
                    # LLM formatted it correctly
                    final_params = {target_param: params[target_param]}
            
            # Scenario 2: Auto-Unwrap (Tool wants multiple params, LLM sent a single nested dict)
            elif len(params) == 1 and isinstance(list(params.values())[0], dict):
                inner_dict = list(params.values())[0]
                # Extract only the keys the tool explicitly expects
                for k in expected_params:
                    if k in inner_dict:
                        final_params[k] = inner_dict[k]
            
            else:
                for k in expected_params:
                    if k in params:
                        final_params[k] = params[k]

            # Fill optional parameters from signature defaults when graph did not bind them.
            for name, p in sig.parameters.items():
                if name in ("self", "request_meta", "args", "kwargs"):
                    continue
                if name not in final_params and p.default is not inspect.Parameter.empty:
                    final_params[name] = p.default
                        
        except Exception as e:
            print(f"[Omni-Handler] Introspection failed for {tool_name}, using raw params: {e}")
            final_params = params.copy()
            
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if hasattr(tool, "_run"):
                    raw = tool._run(**final_params, request_meta=effective_meta)
                else:
                    raw = tool(**final_params)
                    
                if not isinstance(raw, dict):
                    return {"result": raw}
                return raw
                
            except TypeError as e:
                error_msg = str(e)
                
                if "unexpected keyword argument 'request_meta'" in error_msg and "request_meta" not in final_params:
                    try:
                        if hasattr(tool, "_run"):
                            raw = tool._run(**final_params)
                        else:
                            raw = tool(**final_params)
                        if not isinstance(raw, dict):
                            return {"result": raw}
                        return raw
                    except TypeError as e2:
                        error_msg = str(e2) 
                import re
                match = re.search(r"unexpected keyword argument '([^']+)'", error_msg)
                if match:
                    rogue_key = match.group(1)
                    if rogue_key in final_params:
                        print(f"[Omni-Handler] Auto-healing execution. Stripping hallucinated parameter '{rogue_key}'...")
                        final_params.pop(rogue_key)
                        continue

                error_trace = traceback.format_exc()
                print(f"[Tool Execution Error] {tool_name}:\n{error_trace}")
                return {"error": error_msg, "traceback": error_trace}
                
            except Exception as e:
                error_trace = traceback.format_exc()
                print(f"[Tool Execution Error] {tool_name}:\n{error_trace}")
                return {"error": str(e), "traceback": error_trace}
                
        return {"error": f"Failed to execute {tool_name} after {max_retries} automatic parameter-stripping retries."}

class WorkflowExecutionController:
    def __init__(self, db_client, checkpointer, valkey_client):
        self.db = db_client
        self.checkpointer = checkpointer
        self.valkey = valkey_client
        env_tools_dir = os.getenv("TOOLS_DIR_PATH")
        if env_tools_dir:
            tools_dir = env_tools_dir
        else:
            base_dir = os.path.dirname(os.path.dirname(__file__))
            tools_dir = os.path.join(base_dir, "tools")
            
        self.tool_registry = LocalToolRegistry(json_dir=tools_dir)

    def _load_graph(self, workflow_id: str, subscriber_id: Optional[str] = None):
        config_doc = self.db.get_workflow_config(workflow_id)
        agentic_process = config_doc.get("agentic_process", {})
        file_path = agentic_process.get("combined_file_path")

        # Fallback: if combined_file_path is missing (e.g., after restore), check current Process_Configs
        if not file_path:
            current_config = self.db.process_configs.find_one({
                "agentic_process.workflow_id": workflow_id,
                "subscriberId": config_doc.get("subscriberId")
            })
            if current_config:
                current_ap = current_config.get("agentic_process", {})
                file_path = current_ap.get("combined_file_path")
                if file_path:
                    print(f"[_load_graph] Using fallback combined_file_path from Process_Configs: {file_path}")

        if not file_path or not os.path.exists(file_path):
            raise FileNotFoundError(f"Graph definition not found or path missing: {file_path}")

        project_id = (
            agentic_process.get("projectId")
            or config_doc.get("projectId")
            or config_doc.get("project_id")
            or ""
        )
        credentials_dict = {}
        if project_id:
            try:
                credentials_dict = _fetch_credentials_for_project(project_id)
            except Exception as _cred_err:
                print(f"[Credentials] Non-fatal error fetching credentials for '{project_id}': {_cred_err}")

        # Build the request_meta that will be forwarded to every tool invocation.
        # This mirrors what the /dynamic/execute endpoint does for UI-screen functions.
        tool_request_meta: dict = {
            "project_id": project_id,
            "credentials": credentials_dict,
            "subscriber_id": subscriber_id,
        }

        # Create a closure that captures tool_request_meta so every tool call within
        # this graph execution automatically receives the project's credentials.
        registry_ref = self.tool_registry

        def execute_dynamic_tool_with_credentials(tool_name: str, params: dict) -> Any:
            # Issue 5: interpret error dicts inside the graph task so interrupt()
            # pauses even already-generated graphs that store the raw return value.
            def _call(_name: str, _params: dict) -> Any:
                session = get_session() if get_session else None
                if session is not None and session.enabled:
                    with session.tool_execution(_name):
                        return registry_ref.execute_tool(
                            _name, _params, request_meta=tool_request_meta
                        )
                return registry_ref.execute_tool(
                    _name, _params, request_meta=tool_request_meta
                )

            return run_tool_until_success(_call, tool_name, params)

        import uuid
        unique_module_name = f"graph_{workflow_id}_{uuid.uuid4().hex[:6]}"

        spec = importlib.util.spec_from_file_location(unique_module_name, file_path)
        if not spec or not spec.loader:
            raise FileNotFoundError(f"Graph definition failed to load: {file_path}")

        module = importlib.util.module_from_spec(spec)
        module.execute_dynamic_tool = execute_dynamic_tool_with_credentials
        module.interpret_tool_result = interpret_tool_result

        try:
            spec.loader.exec_module(module)
        except Exception as e:
            print(f"[Graph Compilation Error] Failed to execute {file_path}: {e}")
            raise

        compiled = module.builder.compile(checkpointer=self.checkpointer)
        compiled._graph_type = getattr(module, "GRAPH_TYPE", "FREEZE")
        return compiled

    def _publish_tool_error_pause(
        self,
        execution_id: str,
        instance,
        agentic_state: dict,
        history: list,
        stream_channel: str,
        paused_node_id: str,
        ui_payload: dict,
    ) -> None:
        """Pause on a tool failure without treating it as a UI screen or success data."""
        built = tool_error_sse_payloads(
            execution_id, instance.workflow_id, paused_node_id, ui_payload
        )
        pause_payload = built["pause_payload"]
        assigned_role = pick_tool_error_assignee(
            history,
            getattr(instance, "assigned_to", None),
            built["assigned_role"],
        )
        pause_payload["assigned_to"] = assigned_role
        built["tool_error_event"]["assigned_to"] = assigned_role
        built["ui_request_event"]["assigned_to"] = assigned_role

        print(
            f"[{execution_id}] Tool error pause at {paused_node_id}: "
            f"{pause_payload.get('error')!r} code={pause_payload.get('code')!r}"
        )

        self.db.update_node_assignment(execution_id, assigned_role, paused_node_id, pause_payload)

        self.valkey.publish(
            stream_channel,
            json.dumps({"type": "tool_error", "data": built["tool_error_event"]}, default=str),
        )
        # Frontends that only subscribe to ui_request can branch on data.type.
        self.valkey.publish(
            stream_channel,
            json.dumps({"type": "ui_request", "data": built["ui_request_event"]}, default=str),
        )

        history.append({
            "step_id": f"step_{len(history) + 1}",
            "node_name": paused_node_id,
            "status": "failed",
            "assigned_to": assigned_role,
            "inputs": pause_payload,
            "outputs": built["history_outputs"],
            "completed_at": datetime.utcnow().isoformat() + "Z",
        })
        sanitized_state = json.loads(json.dumps(agentic_state, default=str))
        self.db.update_state_snapshot(execution_id, sanitized_state)

    def start_workflow(self, execution_id: str, workflow_id: str, initial_state: dict):
        instance = self.db.get_instance(execution_id)
        graph = self._load_graph(workflow_id, subscriber_id=instance.subscriber_id)
        config = {"configurable": {"thread_id": instance.thread_id}}

        try:
            existing = graph.get_state(config)
            if existing and existing.next:
                print(
                    f"[{execution_id}] Graph is already interrupted at {existing.next}. "
                    f"Skipping start — waiting for human resume."
                )
                self.db.update_status(execution_id, "Pending Review")
                return
        except Exception:
            pass 

        print(f"[{execution_id}] Starting workflow execution stream...")
        self._run_and_log_stream(execution_id, instance, graph, initial_state, config)

    def resume_workflow(self, execution_id: str, resume_payload: dict, userid: Optional[str] = None, roleid: Optional[str] = None):
        instance = self.db.get_instance(execution_id)
        graph = self._load_graph(instance.workflow_id, subscriber_id=instance.subscriber_id)
        config = {"configurable": {"thread_id": instance.thread_id}}
        
        print(f"[{execution_id}] Resuming workflow execution stream...")
        safe_payload = resume_payload if resume_payload is not None else {}
        # Terminal UI screens (declaredOutputs=[]) may call complete({}). LangGraph still
        # needs a truthy resume value to consume interrupt() and advance to END.
        if not safe_payload:
            safe_payload = {"__ui_ack__": True}
        current_node = instance.current_node
        
        agentic_state = instance.state
        history = agentic_state.setdefault("execution_history", [])
        timestamp = datetime.utcnow().isoformat() + "Z"
        stream_channel = f"execution_stream:{execution_id}"
        pending_pause = instance.pending_ui_request or {}
        tool_error_resume = is_tool_error_payload(pending_pause)
        existing_step = next(
            (step for step in reversed(history) 
             if step.get("node_name") == current_node and step.get("status") == "paused"),
            None,
        )
        if not existing_step and tool_error_resume:
            existing_step = next(
                (step for step in reversed(history)
                 if step.get("node_name") == current_node and step.get("status") == "failed"),
                None,
            )

        # Tool-error pauses must not treat the retry/abort payload as the tool's
        # declared outputs — that would put {action: retry} into node_outputs.
        if existing_step and not tool_error_resume:
            existing_step["status"] = "success"
            existing_step["outputs"] = safe_payload
            existing_step["completed_at"] = timestamp
            if userid or roleid:
                existing_step["executed_by"] = {"userId": userid, "roleId": roleid}
                
            self.valkey.publish(stream_channel, json.dumps({"type": "log", "data": existing_step}, default=str))
            self.db.update_state_snapshot(execution_id, json.loads(json.dumps(agentic_state, default=str)))
        
        if current_node and not tool_error_resume:
            node_config = self.db.get_node_config(instance.workflow_id, current_node)
            assignment_cfg = (node_config or {}).get("assignment", {}) or {}
            assignment_ref = assignment_cfg.get("assignmentRef")
            
            if assignment_ref:
                try:
                    rule_doc = self.db.get_assignment_rule(assignment_ref)
                    if rule_doc and rule_doc.get("function_code"):
                        exec_globals: dict = {}
                        exec(rule_doc["function_code"], exec_globals)
                        resolve_fn = exec_globals.get("resolve_assignee")
                        
                        if callable(resolve_fn):
                            last_assigned_role = existing_step.get("assigned_to") if existing_step else None

                            def is_duplicate(role1, role2):
                                if not isinstance(role1, dict) or not isinstance(role2, dict):
                                    return role1 == role2
                                u1 = sorted([str(u).strip() for u in role1.get("users", [])])
                                u2 = sorted([str(u).strip() for u in role2.get("users", [])])
                                r1 = sorted([str(r).strip() for r in role1.get("roles", [])])
                                r2 = sorted([str(r).strip() for r in role2.get("roles", [])])
                                return u1 == u2 and r1 == r2

                            current_node_successes = sum(
                                1 for step in history 
                                if step.get("node_name") == current_node and step.get("status") == "success"
                            )
                            
                            rule_result = resolve_fn(
                                global_inputs=agentic_state.get("global_inputs", {}),
                                node_outputs=agentic_state.get("node_outputs", {}),
                                chain_position=current_node_successes,
                            )
                            
                            if isinstance(rule_result, dict) and rule_result.get("id"):
                                if rule_result.get("type") == "user":
                                    new_assigned_role = {"users": [rule_result["id"]], "roles": []}
                                else:
                                    new_assigned_role = {"users": [], "roles": [rule_result["id"]]}
                                    
                                if is_duplicate(new_assigned_role, last_assigned_role):
                                    print(f"[{execution_id}] Duplicate/Static assignee detected {new_assigned_role}. Let LangGraph advance.")
                                else:
                                    print(f"[{execution_id}] Intercepted loop for node {current_node}. Next assignee: {new_assigned_role}")
                                    
                                    agentic_state.setdefault("node_outputs", {}).setdefault(current_node, {}).update(safe_payload)

                                    ui_payload = instance.pending_ui_request or {}
                                    if not ui_payload and existing_step:
                                        ui_payload = existing_step.get("inputs", {})
                                        
                                    ui_payload["assigned_to"] = new_assigned_role
                                    
                                    new_step = {
                                        "step_id": f"step_{len(history) + 1}",
                                        "node_name": current_node,
                                        "status": "paused",
                                        "assigned_to": new_assigned_role,
                                        "inputs": ui_payload,
                                        "outputs": None,
                                        "completed_at": datetime.utcnow().isoformat() + "Z"
                                    }
                                    history.append(new_step)
                                    
                                    self.valkey.publish(stream_channel, json.dumps({"type": "log", "data": new_step}, default=str))
                                    self.valkey.publish(stream_channel, json.dumps({"type": "ui_request", "data": ui_payload}, default=str))
                                    
                                    self.db.update_state_snapshot(execution_id, json.loads(json.dumps(agentic_state, default=str)))
                                    self.db.update_node_assignment(execution_id, new_assigned_role, current_node, ui_payload)
                                    self.db.update_status(execution_id, "Pending Review")
                                    if maybe_session_for_instance:
                                        try:
                                            obs_session = maybe_session_for_instance(
                                                instance, getattr(graph, "_graph_type", "FREEZE")
                                            )
                                            if obs_session is not None:
                                                screen_id = ui_payload.get("screen_id", current_node)
                                                ui_span_id = obs_session.record_ui_pause(
                                                    node_name=current_node,
                                                    screen_id=screen_id,
                                                )
                                                if isinstance(ui_payload.get("ui_data"), dict) and ui_span_id:
                                                    ui_payload["ui_data"]["_obs_ui_span_id"] = ui_span_id
                                                    ui_payload["ui_data"]["execution_id"] = execution_id
                                                    ui_payload["ui_data"]["workflow_id"] = instance.workflow_id
                                                    ui_payload["ui_data"]["screen_id"] = screen_id
                                        except Exception:
                                            pass
                                    return # Stop here, do not advance LangGraph
                            else:
                                print(f"[{execution_id}] Assignment rule returned None. Let LangGraph advance.")
                                # DO NOT mutate history. Fall through!
                                
                except Exception as e:
                    print(f"[{execution_id}] Interception loop failed: {e}")

        self.db.clear_node_assignment(execution_id)
        if maybe_session_for_instance and set_session:
            obs_session = maybe_session_for_instance(instance, getattr(graph, "_graph_type", "FREEZE"))
            if obs_session is not None:
                try:
                    ui_span_id = None
                    pending = instance.pending_ui_request or {}
                    ui_data = pending.get("ui_data") if isinstance(pending, dict) else None
                    if isinstance(ui_data, dict):
                        ui_span_id = ui_data.get("_obs_ui_span_id")
                    if not ui_span_id and existing_step:
                        inputs = existing_step.get("inputs") or {}
                        nested = inputs.get("ui_data") if isinstance(inputs, dict) else None
                        if isinstance(nested, dict):
                            ui_span_id = nested.get("_obs_ui_span_id")
                    obs_session.record_human_resume(
                        node_name=current_node,
                        payload=safe_payload,
                        userid=userid,
                        roleid=roleid,
                        ui_span_id=ui_span_id,
                    )
                except Exception:
                    pass
        self._run_and_log_stream(execution_id, instance, graph, Command(resume=safe_payload), config, userid=userid, roleid=roleid)

    def _run_and_log_stream(self, execution_id: str, instance, graph, input_data, config, userid: Optional[str] = None, roleid: Optional[str] = None):
        agentic_state = instance.state if instance.state else {"global_inputs": {}, "node_outputs": {}, "execution_history": []}
        history = agentic_state.setdefault("execution_history", [])

        stream_channel = f"execution_stream:{execution_id}"

        graph_type = getattr(graph, "_graph_type", "FREEZE")
        try:
            print(f"[{execution_id}] Graph type detected: {graph_type}")
        except Exception:
            pass

        if not isinstance(input_data, Command):
            agentic_state.setdefault("global_inputs", {}).update(input_data)
            agentic_state.setdefault("node_outputs", {})
            full_global_inputs = {
                **agentic_state["global_inputs"],
                "execution_id": execution_id,
                "workflow_id": instance.workflow_id,
                "thread_id": instance.thread_id,
                "user_id": instance.created_by,
                "subscriber_id": instance.subscriber_id,
                "org_id": instance.org_id,
                "jwt_token": instance.jwt_token,
            }
            agentic_state["global_inputs"] = full_global_inputs
            sanitized_state = json.loads(json.dumps(agentic_state, default=str))
            self.db.update_state_snapshot(execution_id, sanitized_state)

            if graph_type in ("DYNAMIC", "MIXED"):
                safe_global_inputs = json.loads(json.dumps(full_global_inputs, default=str))
                if graph_type == "MIXED":
                    kickoff_text = (
                        f"Workflow execution started. execution_id={execution_id}. "
                        "Await phase instructions."
                    )
                else:
                    kickoff_text = (
                        "Start executing the workflow now. "
                        f"Workflow context: {json.dumps(safe_global_inputs, default=str)}"
                    )
                graph_input = {
                    "global_inputs": safe_global_inputs,
                    "node_outputs": {},
                    "iteration_count": 0,
                    "messages": [HumanMessage(content=kickoff_text)],
                }
            else:
                graph_input = {"global_inputs": full_global_inputs}
        else:
            full_global_inputs = {
                **agentic_state.get("global_inputs", {}),
                "execution_id": execution_id,
                "workflow_id": instance.workflow_id,
                "thread_id": instance.thread_id,
                "user_id": instance.created_by,
                "subscriber_id": instance.subscriber_id,
                "org_id": instance.org_id,
                "jwt_token": instance.jwt_token,
            }
            agentic_state["global_inputs"] = full_global_inputs
            graph_input = input_data

        obs_session = None
        obs_token = None
        stream_config = dict(config) if isinstance(config, dict) else config
        if maybe_session_for_instance and set_session:
            obs_session = maybe_session_for_instance(instance, graph_type)
            if obs_session is not None:
                try:
                    obs_token = set_session(obs_session)
                    obs_session.start_trace()
                    handler = obs_session.make_graph_callback()
                    if isinstance(stream_config, dict):
                        stream_config = {
                            **stream_config,
                            "callbacks": list(stream_config.get("callbacks") or []) + [handler],
                        }
                except Exception as _obs_err:
                    print(f"[{execution_id}] Observability init skipped: {_obs_err}")
                    obs_session = None

        try:
            for event in graph.stream(graph_input, config=stream_config, stream_mode="updates", subgraphs=True):
                if isinstance(event, dict):
                    items_iter = event.items()
                elif isinstance(event, tuple) and len(event) == 2:
                    # Extract the actual payload from the (namespace, payload) tuple
                    namespace, payload = event
                    if isinstance(payload, dict):
                        items_iter = payload.items()
                    else:
                        items_iter = [event]
                else:
                    print(f"[{execution_id}] Unexpected stream event shape: {type(event)} -> {event}")
                    continue

                for node_path, outputs in items_iter:
                    # node_path is a tuple (namespace, node_name) for subgraph events,
                    # or a plain string for top-level nodes.
                    if isinstance(node_path, tuple):
                        node_name = "/".join(str(p) for p in node_path)
                    else:
                        node_name = node_path
                    if not node_name or str(node_name).startswith("__"):
                        continue

                    timestamp = datetime.utcnow().isoformat() + "Z"
                    print(f"[{execution_id}] --- Completed node: {node_name} ---")
                    try:
                        if isinstance(outputs, dict):
                            print(f"[{execution_id}] Node outputs keys: {list(outputs.keys())[:30]}")
                    except Exception:
                        pass

                    clean_outputs = outputs

                    has_messages = (
                        graph_type in ("DYNAMIC", "MIXED")
                        and isinstance(outputs, dict)
                        and (
                            "messages" in outputs
                            or any(
                                isinstance(v, dict) and "messages" in v
                                for v in outputs.values()
                            )
                        )
                    )
                    if has_messages:
                        all_msgs = _collect_llm_messages_from_outputs(outputs)

                        msg_with_usage = None
                        for m in reversed(all_msgs):
                            if getattr(m, "usage_metadata", None):
                                msg_with_usage = m
                                break
                        if msg_with_usage is not None:
                            try:
                                usage = _extract_gemini_usage_from_message(msg_with_usage)
                                total_tokens = int(usage.get("total_tokens", 0) or 0)
                                if total_tokens > 0:
                                    gi = agentic_state.get("global_inputs", {}) or {}
                                    _save_llm_token_count(
                                        execution_id=execution_id,
                                        workflow_id=str(
                                            gi.get("workflow_id") or gi.get("workflowId") or instance.workflow_id
                                        ),
                                        thread_id=str(
                                            gi.get("thread_id") or gi.get("threadId") or instance.thread_id
                                        ),
                                        node_name=str(node_name),
                                        model=str(
                                            getattr(msg_with_usage, "response_metadata", {}).get("model_name")
                                            or "gemini-3.5-flash-lite"
                                        ),
                                        prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                                        completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                                        total_tokens=total_tokens,
                                        status="success",
                                        metadata={"graph_type": graph_type},
                                    )
                                    if obs_session is not None:
                                        try:
                                            obs_session.enrich_last_llm_graph(
                                                node_name=str(node_name),
                                                model=str(
                                                    getattr(msg_with_usage, "response_metadata", {}).get("model_name")
                                                    or "gemini-3.5-flash-lite"
                                                ),
                                                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                                                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                                                total_tokens=total_tokens,
                                            )
                                        except Exception:
                                            pass
                                    print(
                                        f"[{execution_id}] LLM token count saved: "
                                        f"node={node_name} "
                                        f"in={usage.get('prompt_tokens', 0)} "
                                        f"out={usage.get('completion_tokens', 0)} "
                                        f"total={total_tokens}"
                                    )
                            except Exception as _te:
                                print(f"[{execution_id}] Token tracking error (non-fatal): {_te}")

                        last_content = ""
                        if all_msgs:
                            last_msg = all_msgs[-1]
                            last_content = (
                                last_msg.content
                                if hasattr(last_msg, "content")
                                else str(last_msg)
                            )
                        clean_outputs = {"messages_summary": last_content}
                        if isinstance(outputs.get("node_outputs"), dict):
                            clean_outputs.update(outputs["node_outputs"])
                        for v in outputs.values():
                            if isinstance(v, dict) and "node_outputs" in v and isinstance(v["node_outputs"], dict):
                                clean_outputs.update(v["node_outputs"])

                    elif isinstance(outputs, dict) and "node_outputs" in outputs:
                        clean_outputs = outputs["node_outputs"]
                    elif isinstance(outputs, dict):
                        for _v in outputs.values():
                            if isinstance(_v, dict) and "node_outputs" in _v and isinstance(_v["node_outputs"], dict):
                                clean_outputs = _v["node_outputs"]
                                break

                    if isinstance(clean_outputs, dict) and "__interrupt__" in clean_outputs:
                        continue
                    existing_step = next(
                        (step for step in reversed(history)
                         if step.get("node_name") == node_name
                         and step.get("status") in ("paused", "success")),
                        None,
                    )

                    if existing_step:
                        existing_step["status"] = "success"
                        existing_step["outputs"] = clean_outputs
                        existing_step["completed_at"] = timestamp
                        if userid or roleid:
                            existing_step["executed_by"] = {"userId": userid, "roleId": roleid}
                        log_entry = existing_step
                    else:
                        captured_inputs = agentic_state.get("global_inputs", {}).copy()
                        captured_inputs.update(agentic_state.get("node_outputs", {}))

                        log_entry = {
                            "step_id": f"step_{len(history) + 1}",
                            "node_name": node_name,
                            "status": "success",
                            "inputs": captured_inputs,
                            "outputs": clean_outputs,
                            "completed_at": timestamp,
                        }
                        history.append(log_entry)
                    
                    self.valkey.publish(
                        stream_channel,
                        json.dumps(
                            {
                                "type": "log",
                                "data": log_entry,
                            },
                            default=str,
                        ),
                    )
                    
                    if isinstance(clean_outputs, dict):
                        serialisable = {k: v for k, v in clean_outputs.items() if k != "messages"}
                        agentic_state.setdefault("node_outputs", {}).update(serialisable)
                        try:
                            print(f"[{execution_id}] node_outputs updated keys: {list(serialisable.keys())[:30]}")
                        except Exception:
                            pass
                    
                    sanitized_state = json.loads(json.dumps(agentic_state, default=str))
                    self.db.update_state_snapshot(execution_id, sanitized_state)

            state = graph.get_state(config)
            if state.next:
                self.db.update_status(execution_id, "Pending Review")
                interrupts = state.tasks[0].interrupts if state.tasks else []
                if interrupts:
                    ui_payload = interrupts[0].value
                    if not isinstance(ui_payload, dict):
                        ui_payload = {"value": ui_payload}
                    paused_node_id = state.next[0]
                    if is_tool_error_payload(ui_payload):
                        self._publish_tool_error_pause(
                            execution_id,
                            instance,
                            agentic_state,
                            history,
                            stream_channel,
                            paused_node_id,
                            ui_payload,
                        )
                        return

                    screen_id = ui_payload.get("screen_id", "unknown_screen")

                    assigned_role = None
                    logger.info(f"[{execution_id}] Assignment resolution starting for paused node '{paused_node_id}'")
                    try:
                        node_config = self.db.get_node_config(instance.workflow_id, paused_node_id)
                        logger.info(f"[{execution_id}] node_config fetched for '{paused_node_id}': {node_config!r}")

                        assignment_cfg = (node_config or {}).get("assignment", {}) or {}
                        assignment_ref = assignment_cfg.get("assignmentRef")
                        logger.info(
                            f"[{execution_id}] assignment_cfg={assignment_cfg!r} "
                            f"assignment_ref={assignment_ref!r}"
                        )

                        if assignment_ref:
                            try:
                                rule_doc = self.db.get_assignment_rule(assignment_ref)
                                logger.info(
                                    f"[{execution_id}] assignment_registry lookup for ref "
                                    f"'{assignment_ref}' -> found={bool(rule_doc)}"
                                )
                                if rule_doc and rule_doc.get("function_code"):
                                    
                                    exec_globals: dict = {}
                                    exec(rule_doc["function_code"], exec_globals)
                                    resolve_fn = exec_globals.get("resolve_assignee")
                                    logger.info(
                                        f"[{execution_id}] resolve_assignee loaded from rule "
                                        f"'{assignment_ref}': callable={callable(resolve_fn)}"
                                    )

                                    if callable(resolve_fn):
                                        history = agentic_state.get("execution_history", [])
                                        completed_count = sum(
                                            1 for step in history 
                                            if step.get("node_name") == paused_node_id and step.get("status") == "success"
                                        )
                                        
                                        rule_result = resolve_fn(
                                            global_inputs=agentic_state.get("global_inputs", {}),
                                            node_outputs=agentic_state.get("node_outputs", {}),
                                            chain_position=completed_count,
                                        )
                                        
                                        if isinstance(rule_result, dict) and rule_result.get("id"):
                                            if rule_result.get("type") == "user":
                                                assigned_role = {"users": [rule_result["id"]], "roles": []}
                                            elif rule_result.get("type") == "role":
                                                assigned_role = {"users": [], "roles": [rule_result["id"]]}
                                            else:
                                                logger.warning(f"[{execution_id}] assignment rule {assignment_ref} returned unknown type: {rule_result.get('type')!r}")
                                            logger.info(f"[{execution_id}] resolved assigned_role={assigned_role!r}")
                                        elif rule_result is not None:
                                            logger.warning(f"[{execution_id}] assignment rule {assignment_ref} returned unexpected shape: {rule_result!r}")
                                        else:
                                            logger.warning(f"[{execution_id}] assignment rule {assignment_ref} returned None -> falling back to static")
                                    else:
                                        logger.warning(f"[{execution_id}] assignment rule {assignment_ref} did not define resolve_assignee()")
                                else:
                                    logger.warning(f"[{execution_id}] assignment rule {assignment_ref} not found in assignment_registry")
                            except Exception as rule_exc:
                                logger.error(
                                    f"[{execution_id}] assignment rule '{assignment_ref}' execution failed: {rule_exc}\n"
                                    f"{traceback.format_exc()}"
                                )
                        else:
                            logger.info(f"[{execution_id}] no assignmentRef configured on node '{paused_node_id}' — skipping rule-based resolution")

                        if not assigned_role:
                            static_users = assignment_cfg.get("users", [])
                            static_roles = assignment_cfg.get("roles", [])
                            if static_users or static_roles:
                                assigned_role = {"users": static_users, "roles": static_roles}
                                logger.info(f"[{execution_id}] falling back to static assignment: {assigned_role!r}")
                            else:
                                assigned_role = ui_payload.get("assigned_to") or "human_user"
                                logger.info(f"[{execution_id}] no rule/static assignment available — defaulting assigned_role={assigned_role!r}")
                    except Exception as lookup_exc:
                        logger.error(f"[{execution_id}] node config lookup failed for '{paused_node_id}': {lookup_exc}\n{traceback.format_exc()}")
                        assigned_role = ui_payload.get("assigned_to")

                    print(f"[{execution_id}] Paused. Waiting for UI: {screen_id}. Assigned: {assigned_role}")

                    ui_span_id = None
                    if obs_session is not None:
                        try:
                            ui_span_id = obs_session.record_ui_pause(
                                node_name=paused_node_id,
                                screen_id=screen_id,
                            )
                        except Exception:
                            pass

                    # 3. Structure `ui_data` correctly and omit thread_id and node_name
                    ui_data = ui_payload.get("ui_data", {})
                    if not isinstance(ui_data, dict):
                        ui_data = {}

                    # Merge node_outputs FIRST so list/complex values from upstream
                    # nodes (e.g. mcq_questions from generate_mcq) are available to the
                    # paused screen. Also include previous outputs from this screen
                    # (if revisited) so form data persists.
                    # node_outputs may be flat field keys or nested {nodeId: {fields}}.
                    node_outputs = agentic_state.get("node_outputs") or {}
                    for nk, nv in node_outputs.items():
                        if isinstance(nv, dict):
                            for ok, ov in nv.items():
                                if ok not in ui_data:
                                    ui_data[ok] = ov
                        elif nk not in ui_data:
                            ui_data[nk] = nv

                    for gk, gv in full_global_inputs.items():
                        if gk not in ("thread_id", "node_name") and gk not in ui_data:
                            ui_data[gk] = gv

                    ui_data["execution_id"] = execution_id
                    ui_data["workflow_id"] = instance.workflow_id
                    ui_data["screen_id"] = screen_id
                    if ui_span_id:
                        ui_data["_obs_ui_span_id"] = ui_span_id
                            
                    ui_payload["ui_data"] = ui_data

                    ui_payload["execution_id"] = execution_id
                    ui_payload["screen_id"] = screen_id
                    ui_payload["assigned_to"] = assigned_role
                    
                    # Ensure node_name and thread_id are removed from root scope
                    ui_payload.pop("node_name", None)
                    ui_payload.pop("thread_id", None)

                    self.db.update_node_assignment(execution_id, assigned_role, paused_node_id, ui_payload)
                    
                    self.valkey.publish(stream_channel, json.dumps({
                        "type": "ui_request",
                        "data": ui_payload
                    }))
                    
                    history.append({
                        "step_id": f"step_{len(history) + 1}",
                        "node_name": paused_node_id,
                        "status": "paused",
                        "assigned_to": assigned_role, 
                        "inputs": ui_payload,
                        "outputs": None,
                        "completed_at": datetime.utcnow().isoformat() + "Z"
                    })
                    
                    sanitized_state = json.loads(json.dumps(agentic_state, default=str))
                    self.db.update_state_snapshot(execution_id, sanitized_state)
            else:
                self.db.update_status(execution_id, "completed")
                self.db.clear_node_assignment(execution_id)
                print(f"[{execution_id}] Workflow completed successfully.")
                if obs_session is not None:
                    try:
                        obs_session.finish_trace("completed")
                    except Exception:
                        pass
                self.valkey.publish(
                    stream_channel,
                    json.dumps(
                        {
                            "type": "completed",
                            "data": {"status": "success"},
                        },
                        default=str,
                    ),
                )

        except Exception as e:
            if isinstance(e, ToolNodeFailed):
                print(f"[{execution_id}] Operator aborted tool after error: {e}")
            print(f"[{execution_id}] Error executing graph:\n{traceback.format_exc()}") 
            self.db.update_status(execution_id, "failed")
            if obs_session is not None:
                try:
                    obs_session.record_node_failure("unknown_node", str(e))
                    obs_session.finish_trace("failed")
                except Exception:
                    pass
            
            self.valkey.publish(
                stream_channel,
                json.dumps(
                    {
                        "type": "failed",
                        "data": {"error": str(e)},
                    },
                    default=str,
                ),
            )
            
            failed_node = "unknown_node"
            try:
                current_state = graph.get_state(config)
                if current_state and current_state.next:
                    failed_node = current_state.next[0]
            except Exception:
                pass

            history.append({
                "step_id": f"step_{len(history) + 1}",
                "node_name": failed_node,
                "status": "failed",
                "assigned_to": None,
                "inputs": None,
                "outputs": {"error": str(e)},
                "completed_at": datetime.utcnow().isoformat() + "Z"
            })
            
            agentic_state["execution_history"] = history
            sanitized_state = json.loads(json.dumps(agentic_state, default=str))
            self.db.update_state_snapshot(execution_id, sanitized_state)
        finally:
            if obs_token is not None and reset_session:
                try:
                    reset_session(obs_token)
                except Exception:
                    pass

