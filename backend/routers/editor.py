import asyncio
import json
import logging
import traceback
import os
import time
import re
import httpx 
import functools
import functools
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from services.database import Database 
# Import services
from services.sandbox_editor import SandboxEditor 
from repositories.screen_repository import ScreenRepository
from services.backend_generator import BackendGenerator
from services.sandbox_function_executor import get_function_worker_pool
from crewai import LLM
import time
from pydantic import BaseModel
# Configure logging
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/aiqod-agent/agent/editor",
    tags=["Editor"]
)

screen_repository = ScreenRepository()
backend_generator = BackendGenerator()

async def run_sync(func, *args, **kwargs):
    """Safely runs blocking I/O in a thread pool without blocking the main loop."""
    loop = asyncio.get_running_loop()
    pfunc = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(None, pfunc)
    
def run_sync_background(func, *args, **kwargs):
    """
    Thread-safe bridge to run a function in the background executor 
    without blocking the main FastAPI loop.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # If we are in a thread with no loop, get the main one
        loop = asyncio.get_event_loop()
        
    pfunc = functools.partial(func, *args, **kwargs)
    return loop.run_in_executor(None, pfunc)

# --- CONFIGURATION LOADER ---
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")
try:
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
    
    SANDBOX_TEMP_DIR = config.get("sandbox_temp_dir", "/tmp/sessions")
    PUBLISH_BASE_DIR = config.get("publish_base_dir", "/data/publicfolder/liberty-fs")
    PREVIEW_BASE_URL = config.get("preview_base_url", "/aiqod-agent/agent/static/sessions")
    API_PREFIX = config.get("api_prefix", "/dev")
    
    # Dynamic URLs for Injection
    API_EXTERNAL_URL = config.get("api_external_url", "https://dev.aiqod.com:843")
    TEMPLATE_API_URL_PATTERN = config.get("template_api_url", "https://dev.aiqod.com:843/aiqod-agent/agent/dynamic/execute")

except Exception as e:
    logger.error(f"Failed to load config.json: {e}. Using defaults.")
    SANDBOX_TEMP_DIR = "/tmp/sessions"
    PUBLISH_BASE_DIR = "/data/publicfolder/liberty-fs"
    PREVIEW_BASE_URL = "/aiqod-agent/agent/static/sessions"
    API_PREFIX = "/dev"
    API_EXTERNAL_URL = "https://dev.aiqod.com:843"
    TEMPLATE_API_URL_PATTERN = "https://dev.aiqod.com:843/aiqod-agent/agent/dynamic/execute"

# Initialize Sandbox Service 
sandbox_service = SandboxEditor(base_temp_dir=SANDBOX_TEMP_DIR)


# --- Job Manager ---
class BuildJobManager:
    def __init__(self):
        self.active_jobs = {}

    def create_job(self, session_id: str) -> asyncio.Queue:
        queue = asyncio.Queue()
        self.active_jobs[session_id] = queue
        return queue

    def get_queue(self, session_id: str) -> Optional[asyncio.Queue]:
        return self.active_jobs.get(session_id)

    def cleanup_job(self, session_id: str):
        if session_id in self.active_jobs:
            del self.active_jobs[session_id]
class LoadVersionRequest(BaseModel):
    session_id: str
    project_id: str
    screen_id: str
    version: str
    
build_manager = BuildJobManager()

class RegenerateRequest(BaseModel):
    session_id: str
    function_id: str
    source_code: str
    models_source: str
    context: Dict[str, Any]

@router.get("/backend/last-error")
async def get_last_error_endpoint(session_id: str = Query(...)):
    """Fetches the last execution error from the session."""
    try:
        error_data = await run_sync(sandbox_service.get_last_error, session_id)
        return {"success": True, "error_data": error_data}
    except Exception as e:
        return {"success": False, "message": str(e)}

@router.post("/backend/regenerate")
async def regenerate_backend_code(request: RegenerateRequest):
    """
    Regenerates backend code by delegating the LLM call to agent_new.py via callback.
    """
    try:
        # 1. Check if callback is initialized (injected by agent_new.py)
        if not code_generation_callback:
            raise HTTPException(
                status_code=503, 
                detail="LLM callback service is not initialized. The agent server might be starting up."
            )

        # Extract context
        error_msg = request.context.get('error_message', 'Unknown Error')
        traceback_str = request.context.get('traceback', '')
        params_val = request.context.get('params', {})
        
        # 2. Construct the Advanced Prompt
        prompt = f"""You are a Python debugging expert. A backend function failed during execution due to a LOGICAL ERROR. Your task is to fix ONLY the specific logical issue, NOT rewrite the entire function.

            Function ID: {request.function_id}
            Error: {error_msg}
            
            VALIDATED INPUT PARAMETERS (used in execution):
            {json.dumps(params_val, indent=2, default=str)}
            
            ⚠️ CRITICAL - PARAMS IS A DICT, NOT A MODEL INSTANCE:
            - Functions receive `params` as a plain Python dict.
            - ✅ CORRECT: params.get("file", {{}}).get("content") or params["file"]["content"]
            - ❌ WRONG: params.file.content
            
            FULL TRACEBACK / ERROR DETAILS:
            {traceback_str}
            
            CURRENT PYDANTIC MODELS:
            ```python
            {request.models_source}
            ```
            
            CURRENT FUNCTION SOURCE CODE:
            ```python
            {request.source_code}
            ```
            
            TASK:
            Analyze the error and fix ONLY the specific logical issue.
            
            COMMON FIXES:
            1. Response Error: If the function returns {{"error": "..."}}, fix the logic that causes it.
            2. Missing Imports: Add imports inside the function.
            3. Database Access: Use `from services.database import Database` then `db = Database.get_collection(...)`.
            4. Threading: Use `await db.collection.find(...)`, never synchronous calls.
            
            Respond in this EXACT XML format (no markdown):
            <response>
            <source_code>
            complete updated function source code here
            </source_code>
            </response>"""

        # 3. Call the Callback (Delegates execution to agent_new.py)
        # This uses the authenticated LLM instance from the main agent file
        response = await code_generation_callback([{"role": "user", "content": prompt}])
        response_text = response.strip()

        # 4. Parse XML Response
        source_match = re.search(r'<source_code>(.*?)</source_code>', response_text, re.DOTALL)
        
        if source_match:
            fixed_code = source_match.group(1).strip()
            # Clean markdown if present
            if fixed_code.startswith("```python"): fixed_code = fixed_code[9:]
            if fixed_code.startswith("```"): fixed_code = fixed_code[3:]
            if fixed_code.endswith("```"): fixed_code = fixed_code[:-3]
            fixed_code = fixed_code.strip()
        else:
            # Fallback: try to just grab code block if XML failed
            if "```python" in response_text:
                fixed_code = response_text.split("```python")[1].split("```")[0].strip()
            else:
                # Last resort: return original to prevent data loss
                fixed_code = request.source_code 

        # 5. Save to Draft
        await run_sync(
            sandbox_service.save_backend_draft,
            request.session_id, 
            request.function_id, 
            fixed_code, 
            request.models_source
        )

        return {"success": True, "fixed_code": fixed_code}

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail={"success": False, "error": str(e)})

# --- Background Cleanup Task ---
async def cleanup_task():
    """Runs every 10 minutes to clean up sessions older than 1 hour."""
    while True:
        try:
            await asyncio.sleep(600) # Check every 10 mins
            logger.info("Running background session pruner...")
            # Prune sessions older than 1 hour (3600 seconds)
            await run_sync(sandbox_service.prune_stale_sessions, max_age_seconds=3600)
        except Exception as e:
            logger.error(f"Cleanup task error: {e}")

@router.on_event("startup")
async def startup_event():
    # 1. Startup Scavenger: Wipe ALL temporary sessions on boot
    await run_sync(sandbox_service.wipe_all_sessions)
    
    # 2. Start the Reaper: Background task for ongoing cleanup
    asyncio.create_task(cleanup_task())

# --- Models ---
class InitRequest(BaseModel):
    screen_id: str

class WriteFileRequest(BaseModel):
    session_id: str
    file_path: str
    content: str

class BuildRequest(BaseModel):
    session_id: str

class PublishRequest(BaseModel):
    session_id: str
    screen_id: str

class BackendDraftRequest(BaseModel):
    session_id: str
    function_id: str
    source_code: str
    models_source: str

class BackendTestRequest(BaseModel):
    session_id: str
    function_id: str
    test_payload: Dict[str, Any]

# --- Endpoints ---

@router.post("/init")
async def init_editor(request: InitRequest):
    try:
        logger.info(f"Fetching screen record for: {request.screen_id}")
        screen_data = await screen_repository.get_screen_generation(request.screen_id)
        
        if not screen_data:
            raise HTTPException(status_code=404, detail=f"Screen {request.screen_id} not found in DB")

        project_path = None
        if 'angular' in screen_data and 'workspace_directory' in screen_data['angular']:
            project_path = screen_data['angular']['workspace_directory']
        
        if not project_path or not os.path.exists(project_path):
             raise HTTPException(status_code=404, detail="Source code not found for this screen.")

        selector = "app-root"
        try:
            selector = screen_data.get('ir_schema', {}).get('screen', {}).get('selector', 'app-root')
        except:
            pass

        session_data = await sandbox_service.init_session(project_path, selector)
        
        return {
            "success": True,
            "session_id": session_data["session_id"],
            "root_path": session_data["root_path"],
            "file_tree": session_data["file_tree"],
            "selector": session_data["selector"]
        }
    except Exception as e:
        logger.error(f"Init failed: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/file")
async def read_file(session_id: str = Query(...), file_path: str = Query(...)):
    try:
        content = await run_sync(sandbox_service.read_file, session_id, file_path)
        return {"content": content} 
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")

@router.post("/file")
async def write_file(request: WriteFileRequest):
    try:
        await run_sync(sandbox_service.write_file, request.session_id, request.file_path, request.content)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/build/stream/{session_id}")
async def stream_build_logs(session_id: str):
    queue = build_manager.get_queue(session_id)
    if not queue:
        raise HTTPException(status_code=404, detail="Build session not active")

    async def event_generator():
        try:
            while True:
                data = await queue.get()
                if data is None:
                    break
                yield f"data: {json.dumps(data)}\n\n"
        finally:
            build_manager.cleanup_job(session_id)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@router.post("/publish")
async def publish_changes(request: PublishRequest):
    try:
        # 1. Fetch Screen Data
        screen_data = await screen_repository.get_screen_generation(request.screen_id)
        if not screen_data:
             raise HTTPException(status_code=404, detail="Screen record not found")

        # 2. Define original_source_path
        original_source_path = None
        if 'angular' in screen_data and 'workspace_directory' in screen_data['angular']:
            original_source_path = screen_data['angular']['workspace_directory']
            
        if not original_source_path:
            raise HTTPException(status_code=404, detail="Original source path not found in DB")

        
        logger.info(f"[{request.session_id}] Publish requested. Forcing CLEAN BUILD to sanitize paths.")
        
        async def null_callback(t, m): pass 
        
        # 'clean_build=True' tells the builder to use the Production URL, not the Session URL.
        build_result = await sandbox_service.run_build(
            request.session_id, 
            progress_callback=null_callback,
            api_external_url=API_EXTERNAL_URL,
            clean_build=True 
        )

        if build_result['status'] != 'success':
            raise HTTPException(status_code=500, detail=f"Publish failed: Build Error - {build_result.get('message')}")


        # 4. Save Source Code back to the original project folder
        await run_sync(sandbox_service.save_source_back, request.session_id, original_source_path)

        # 5. Deploy the 'dist' folder to the public server
        # Extract the correct folder name from the DB metadata
        angular_data = screen_data.get('angular', {})
        public_url = angular_data.get('public_url')
        version = angular_data.get('version')

        # Determine the target folder name matching the live deployment structure
        target_folder_name = None
        if public_url and isinstance(public_url, str) and public_url.strip():
            try:
                from urllib.parse import urlparse
                parsed_url = urlparse(public_url.strip())
                # Extract the last path segment safely, ignoring query params and trailing slashes
                path_segments = [seg for seg in parsed_url.path.split('/') if seg]
                if path_segments:
                    target_folder_name = path_segments[-1]
            except Exception as e:
                logger.warning(f"Failed to parse public_url for publish folder extraction: {e}")
        
        # Fallback to existing functionality if dynamic extraction failed or public_url is absent
        if not target_folder_name:
            if version:
                target_folder_name = f"{request.screen_id}-v{version}"
            else:
                target_folder_name = request.screen_id

        # Use the dynamically resolved target folder name
        deploy_target_path = os.path.join(PUBLISH_BASE_DIR, target_folder_name)
        await run_sync(sandbox_service.publish_changes, request.session_id, deploy_target_path)

        # 6. Clear Dirty Flag
        await run_sync(sandbox_service.clear_dirty_flag, request.session_id)
        
        # 7. Cleanup the temporary session folder
        await run_sync(sandbox_service.cleanup_session, request.session_id)

        return {"success": True, "message": "Published successfully"}

    except Exception as e:
        logger.error(f"Publish failed: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/backend/draft")
async def save_backend_draft(request: BackendDraftRequest):
    try:
        # 1. Async DB Call (Runs on the main loop, non-blocking)
        db = Database()
        live_func = await db.get_collection("functions").find_one({"function_id": request.function_id})
        
        is_live_equivalent = False
        if live_func:
            live_source = (live_func.get("source_code") or "").strip()
            live_models = (live_func.get("models_source") or "").strip()
            
            if live_source == request.source_code.strip() and \
               live_models == request.models_source.strip():
                is_live_equivalent = True

        # 2. Synchronous File I/O (Pushed to background thread via run_sync)
        await run_sync(
            sandbox_service.save_backend_draft,
            request.session_id, 
            request.function_id, 
            request.source_code, 
            request.models_source,
            is_live_equivalent=is_live_equivalent
        )
        return {"success": True}

    except Exception as e:
        logger.error(f"Autosave failed: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/backend/draft")
async def get_backend_draft(session_id: str = Query(...), function_id: str = Query(...)):
    try:
        draft = await run_sync(sandbox_service.get_backend_draft, session_id, function_id)
        if draft:
            return {"success": True, "data": draft}
        return {"success": False, "message": "No draft found"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/file-tree")
async def get_file_tree(session_id: str = Query(...)):
    try:
        tree = await run_sync(sandbox_service.get_session_file_tree, session_id)
        return {"success": True, "file_tree": tree}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/proxy/{session_id}/execute")
@router.post("/proxy/{session_id}/execute/{subpath:path}")
async def execute_proxy_function(session_id: str, request: Request, subpath: str = None):
    try:
        # Extract payload safely
        body_bytes = await request.body()
        try:
            payload = json.loads(body_bytes)
        except:
            payload = {}
            
        function_id = payload.get("function_id")
        
        # Fallback: Extract function_id from URL
        if not function_id and subpath:
            parts = subpath.strip('/').split('/')
            if parts:
                function_id = parts[-1]

        logger.info(f"[{session_id}] Proxy Request: {function_id} | Path: {subpath}")

        worker_pool = get_function_worker_pool()
        draft = await run_sync(sandbox_service.get_backend_draft, session_id, function_id)

        # 1. Try DRAFT (Session)
        if draft:
            logger.info(f"[{session_id}] Executing DRAFT function: {function_id}")
            result = await worker_pool.execute(
                function_source=draft['source_code'],
                function_name=function_id,
                params=payload.get("params", {}),
                db_wrapper_config=None,
                secure_http_config=None,
                request_meta=payload.get("request_meta", {})
            )
            return result.result if result.success else Response(content=str(result), status_code=500)

        # 2. Try LIVE DB (Direct DB Access)
        # We only try this if it looks like a function call (has a function_id)
        if function_id:
            db = Database()
            func_record = await db.get_collection("functions").find_one({"function_id": function_id})
            
            if func_record:
                logger.info(f"[{session_id}] Executing LIVE function source: {function_id}")
                result = await worker_pool.execute(
                    function_source=func_record['source_code'],
                    function_name=function_id,
                    params=payload.get("params", {}),
                    db_wrapper_config=None,
                    secure_http_config=None
                )
                return result.result if result.success else Response(content=str(result), status_code=500)

        # 3. FALLBACK: TRANSPARENT PROXY TO LIVE SERVER
        # If it's not a draft and not in the DB, it's likely a system endpoint (like login_user)
        logger.info(f"[{session_id}] '{function_id}' not in DB. Forwarding to LIVE Path.")
        
        # [FIX] Use TEMPLATE_API_URL_PATTERN to construct the full correct path
        base_execute_url = TEMPLATE_API_URL_PATTERN.rstrip('/')
        target_url = f"{base_execute_url}/{subpath}" if subpath else base_execute_url
        
        logger.info(f"[{session_id}] Forwarding to Target URL: {target_url}")

        async with httpx.AsyncClient(timeout=120.0) as client:
            proxy_res = await client.request(
                method=request.method,
                url=target_url,
                headers={k: v for k, v in request.headers.items() if k.lower() != 'host'}, 
                content=body_bytes, 
                params=request.query_params
            )
            return Response(content=proxy_res.content, status_code=proxy_res.status_code, headers=dict(proxy_res.headers))
    except Exception as e:
        logger.error(f"Proxy execution failed: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/build/start")
async def start_build(request: BuildRequest):
    try:
        queue = build_manager.create_job(request.session_id)

        async def run_build_task():
            async def log_callback(level, message):
                await queue.put({"type": "log", "data": message})

            try:
                # [NOTE] clean_build=False ensures Proxy Injection works for Preview
                result = await sandbox_service.run_build(
                    request.session_id, 
                    progress_callback=log_callback,
                    api_external_url=API_EXTERNAL_URL, # Uses Config URL
                    template_search_url=TEMPLATE_API_URL_PATTERN, # Uses Config Pattern
                    clean_build=False 
                )
                
                if result['status'] == 'success':
                    # Build path under API prefix so static mount is reachable (core app is mounted at API_PREFIX)
                    prefix = (API_PREFIX or "").rstrip("/")
                    base = PREVIEW_BASE_URL.strip("/")
                    base_url = f"{prefix}/{base}" if prefix else base
                    base_url = base_url.rstrip("/")
                    
                    # Send path to index.html so the iframe loads a concrete file (avoids directory-index 404)
                    preview_file_path = result['preview_path']
                    
                    # Result: /dev/static/sessions/.../dist/angular-base-project/index.html
                    full_preview_path = f"{base_url}/{preview_file_path}"

                    await queue.put({
                        "type": "close", 
                        "preview_url": full_preview_path
                    })
                else:
                    await queue.put({"type": "error", "data": result['message']})
            except Exception as e:
                await queue.put({"type": "error", "data": str(e)})
            finally:
                await queue.put(None) 

        asyncio.create_task(run_build_task())
        return {"success": True, "message": "Build started"}
    except Exception as e:
        # [FIXED] Added missing closing parenthesis here
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/versions/{project_id}/{screen_id}")
async def get_editor_versions(project_id: str, screen_id: str):
    """Returns a list of all historical versions for the dropdown."""
    try:
        versions = await screen_repository.get_screen_versions(project_id, screen_id)
        return {"success": True, "versions": versions}
    except Exception as e:
        logger.error(f"Error fetching versions: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/load-version")
async def load_version_to_sandbox(request: LoadVersionRequest):
    """Loads historical code into the sandbox and returns the preview URL."""
    try:
        version_data = await screen_repository.get_screen_version_data(
            request.project_id, request.screen_id, request.version
        )
        if not version_data:
            raise HTTPException(status_code=404, detail="Version not found")

        # 1. Clear existing sandbox files for the current session
        session_path = sandbox_service._get_session_path(request.session_id)
        if session_path.exists():
            import shutil
            shutil.rmtree(session_path)
        session_path.mkdir(parents=True, exist_ok=True)

        # 2. Get the historical files from the saved workspace directory
        angular_meta = version_data.get("angular", {})
        historical_workspace = angular_meta.get("workspacedirectory") or angular_meta.get("workspace_directory")

        if historical_workspace and os.path.exists(historical_workspace):
            import shutil
            shutil.copytree(historical_workspace, session_path, dirs_exist_ok=True)
        else:
            logger.warning(f"Historical workspace not found at {historical_workspace}. Sandbox may be empty.")

        # 3. Write Backend Files (Functions) & Extract for response
        backend_meta = version_data.get("backend") or version_data.get("backend_metadata") or {}
        backend_functions_list = backend_meta.get("functions", [])
        
        db = Database() # Initialize database connection

        for func in backend_functions_list:
            func_name = func.get("function_id") or func.get("id", "unknown_func")
            
            # --- FIX: Query the exact collection 'backend_functions' using BOTH function_id and project_id ---
            live_func = await db.get_collection("backend_functions").find_one({
                "function_id": func_name,
                "project_id": request.project_id
            })
            
            if live_func:
                func["source_code"] = live_func.get("source_code", "")
                func["models_source"] = live_func.get("models_source", "")
            else:
                func["source_code"] = func.get("source_code", "")
                func["models_source"] = func.get("models_source", "")

            file_path = session_path / "backend" / f"{func_name}.py"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as file:
                file.write(func.get("source_code", ""))

        # 4. Generate new file tree
        file_tree = await sandbox_service.generate_file_tree_async(session_path)
        
        # 5. Get the historical preview URL safely
        preview_url = angular_meta.get("public_url") or version_data.get("preview_url") or version_data.get("deployed_url")

        return {
            "success": True,
            "message": f"Loaded version {request.version}",
            "file_tree": file_tree,
            "preview_url": preview_url,
            "backend_functions": backend_functions_list 
        }
    except Exception as e:
        logger.error(f"Error loading version: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/backend/test")
async def manual_test(request: BackendTestRequest):
    """
    Executes code and uses agent_new.py logic to detect logical errors.
    """
    try:
        worker_pool = get_function_worker_pool()
        draft = await run_sync(sandbox_service.get_backend_draft, request.session_id, request.function_id)
        if not draft:
             return {"success": False, "message": "No draft code found. Please save the draft first."}
        
        source_code = draft['source_code']
        result = await worker_pool.execute(
            function_source=source_code,
            function_name=request.function_id,
            params=request.test_payload,
            db_wrapper_config=None,
            secure_http_config=None
        )
        
        # Get result dict
        res_dict = result.result if result.success else result.__dict__
        if not isinstance(res_dict, dict) and hasattr(result, "__dict__"):
             res_dict = result.__dict__

        # --- LOGIC COPIED FROM agent_new.py ---
        # Detect "logical" errors where execution succeeded (200 OK) 
        # but the function returned an error message.
        
        response_error = None
        
        if isinstance(res_dict, dict):
            # Check 1: "error" key with text value
            error_val = res_dict.get("error")
            if error_val and isinstance(error_val, str) and error_val.strip():
                response_error = error_val.strip()
            
            # Check 2: explicit success: False
            elif res_dict.get("success") is False:
                response_error = res_dict.get("message") or "Operation Failed"

        # If a logical error was found
        if response_error:
             # FORCE failure so frontend turns RED
             if isinstance(res_dict, dict):
                 res_dict['success'] = False
                 # Ensure message is set for the UI
                 if 'message' not in res_dict:
                     res_dict['message'] = response_error

             # Capture for "Fix with AI"
             error_data = {
                 "params": request.test_payload,
                 "error_message": f"Function returned error in response: {response_error}",
                 "traceback": result.stderr_output, # Pass logs for context
                 "timestamp": time.time()
             }
             await run_sync(sandbox_service.save_last_error, request.session_id, error_data)
        
        # Handle actual crashes (Exceptions)
        elif not result.success:
             error_data = {
                 "params": request.test_payload,
                 "error_message": result.error or "Execution Error",
                 "traceback": result.traceback,
                 "timestamp": time.time()
             }
             await run_sync(sandbox_service.save_last_error, request.session_id, error_data)

        return res_dict

    except Exception as e:
        # System level crashes
        error_response = {
            "success": False,
            "message": str(e),
            "details": traceback.format_exc(),
            "execution_time": 0
        }
        
        await run_sync(sandbox_service.save_last_error, request.session_id, {
            "params": request.test_payload,
            "error_message": error_response['message'],
            "traceback": error_response['details'],
            "timestamp": time.time()
        })
        return error_response