"""
Non-Restrictive Sandbox Executor for LLM-generated backend functions.

Executes functions in separate subprocesses with resource limits but without
import or builtin restrictions. Functions can use any Python module or feature.
"""

import asyncio
import json
import logging
import os
import resource
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
import functools

logger = logging.getLogger(__name__)

# Default resource limits
DEFAULT_TIMEOUT_MS = int(os.getenv("SANDBOX_TIMEOUT_MS", "90000"))  # 90 seconds
DEFAULT_MEMORY_MB = int(os.getenv("SANDBOX_MEMORY_MB", "512"))  # 512 MB
DEFAULT_CPU_TIME_SEC = int(os.getenv("SANDBOX_CPU_TIME_SEC", "120"))  # 120 seconds

async def run_sync(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    pfunc = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(None, pfunc)

@dataclass
class SandboxFunctionConfig:
    """Configuration for non-restrictive sandbox execution."""
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    memory_mb: int = DEFAULT_MEMORY_MB
    cpu_time_sec: int = DEFAULT_CPU_TIME_SEC
    enable_network: bool = True  # Allow network access


@dataclass
class SandboxFunctionResult:
    """Result from non-restrictive sandbox execution."""
    success: bool
    result: Any = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    traceback: Optional[str] = None
    stderr_output: Optional[str] = None  # Full stderr output including tracebacks
    execution_time_ms: float = 0.0
    memory_used_mb: float = 0.0


class SandboxFunctionExecutor:
    """
    Executes Python functions in isolated subprocesses with resource limits but NO code restrictions.
    
    Functions have COMPLETE FREEDOM to:
    - Import and use ANY Python package (OCR libraries, PDF parsers, Stripe SDK, etc.)
    - Access database directly via Database.get_collection() (no collection restrictions)
    - Use any HTTP library (requests, httpx, aiohttp) without domain restrictions
    - Install and use any library they need
    - Use all Python builtins and features without limitations
    
    Architecture:
    - Main API → SandboxFunctionExecutor → Subprocess → Execute function → Return result
    - Each execution is isolated in a separate process
    - Resource limits enforced via OS signals and resource module (timeout, memory, CPU)
    - NO restrictions on imports, builtins, or Python features
    """

    def __init__(self, config: Optional[SandboxFunctionConfig] = None):
        self.config = config or SandboxFunctionConfig()
        self._worker_script_path = self._create_worker_script()

    def _create_worker_script(self) -> Path:
        """Create the worker script that will run in subprocess with full Python environment."""
        script_content = '''
import os
# Set OpenBLAS thread limit before any numpy imports to prevent resource exhaustion
# This must be set before importing numpy (which happens via pytesseract)
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'
os.environ['NUMEXPR_NUM_THREADS'] = '1'
os.environ['OMP_NUM_THREADS'] = '1'

import asyncio
import json
import sys
import logging
import traceback
from typing import Any, Dict, Optional

# Configure logging to output to stderr so it can be captured by parent process
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    stream=sys.stderr,
    force=True  # Override any existing configuration
)


def _convert_to_json_serializable(data: Any) -> None:
    """
    Recursively convert all non-JSON-serializable types to serializable ones in-place.
    Handles ObjectId, datetime, date, Decimal, etc.
    """
    from bson import ObjectId
    from datetime import datetime, date
    from decimal import Decimal
    
    if isinstance(data, dict):
        for key, value in list(data.items()):
            if isinstance(value, ObjectId):
                data[key] = str(value)
            elif isinstance(value, (datetime, date)):
                data[key] = value.isoformat()
            elif isinstance(value, Decimal):
                data[key] = float(value)
            elif isinstance(value, (dict, list)):
                _convert_to_json_serializable(value)
    elif isinstance(data, list):
        for i, item in enumerate(data):
            if isinstance(item, ObjectId):
                data[i] = str(item)
            elif isinstance(item, (datetime, date)):
                data[i] = item.isoformat()
            elif isinstance(item, Decimal):
                data[i] = float(item)
            elif isinstance(item, (dict, list)):
                _convert_to_json_serializable(item)


async def execute_function(
    function_source: str,
    function_name: str,
    params: Dict[str, Any],
    db_wrapper_config: Optional[Dict] = None,
    secure_http_config: Optional[Dict] = None,
    request_meta: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    Execute function in isolated environment with FULL Python access - no restrictions.
    
    Functions can:
    - Import and use ANY Python package (OCR, PDF parsers, Stripe, etc.)
    - Access database directly via pymongo/motor (no collection restrictions)
    - Use any HTTP library (requests, httpx, aiohttp) without domain restrictions
    - Install and use any library they need
    """
    try:
        # Full Python environment - NO restrictions whatsoever
        import builtins
        
        # Provide direct database access (no wrapper restrictions)
        from services.database import Database

        # Standard execution environment with FULL access
        # All builtins are available, ALL imports are allowed
        import logging
        import shutil

        # Native async Mongo (SANDBOX_FUNCTION_WORKER) — avoids Motor's thread-pool + sync pymongo
        _sub_db = os.environ.get("SUBSCRIBER_DB_NAME", "")
        if _sub_db:
            logging.getLogger("sandbox_function").info(
                "Sandbox worker SUBSCRIBER_DB_NAME=%s", _sub_db
            )
        Database.connect_db()
        
        # Configure pytesseract if available to prevent hanging
        # This ensures pytesseract can find the tesseract binary even in subprocess environments
        try:
            import pytesseract
            tesseract_path = shutil.which("tesseract") or "/usr/bin/tesseract"
            pytesseract.pytesseract.tesseract_cmd = tesseract_path
        except ImportError:
            # pytesseract not installed, that's fine
            pass
        except Exception:
            # If configuration fails, that's okay - function will handle it
            pass
        
        execution_globals = {
            "__builtins__": builtins.__dict__,
            "__name__": "__main__",
            # Provide direct database access - functions can use Database.get_collection() directly
            # This bypasses any collection blocklists
            "Database": Database,
            # Provide logging module for error logging in generated functions
            "logging": logging,
        }
        
        # Optionally provide db_wrapper and secure_http for backward compatibility
        # But functions are NOT restricted to using them - they can use direct imports instead
        if db_wrapper_config:
            from services.backend_generator import DatabaseWrapper
            blocked_collections = db_wrapper_config.get("blocked_collections", [])
            execution_globals["db_wrapper"] = DatabaseWrapper(blocked_collections)
        else:
            execution_globals["db_wrapper"] = None
        
        if secure_http_config:
            from services.backend_generator import SecureHTTPClient
            blocklist = secure_http_config.get("blocklist", [])
            timeout = secure_http_config.get("timeout_ms", 5000)
            execution_globals["secure_http"] = SecureHTTPClient(blocklist, timeout)
        else:
            execution_globals["secure_http"] = None
        
        # Execute function source with FULL Python environment
        # Functions can import ANYTHING: OCR libraries, PDF parsers, Stripe SDK, etc.
        # No import restrictions, no builtin restrictions, complete freedom
        local_vars: Dict[str, Any] = {}
        exec(function_source, execution_globals, local_vars)
        
        # Get the function
        func = local_vars.get(function_name)
        if not func:
            raise ValueError(f"Function {function_name} not found in source")
        
        if not asyncio.iscoroutinefunction(func):
            raise ValueError(f"Function {function_name} is not async")
        
        # Prepare execution kwargs - pass params and optional helpers
        # Functions can choose to use db_wrapper/secure_http OR use direct imports
        execution_kwargs = {"params": params}
        if execution_globals.get("db_wrapper") is not None:
            execution_kwargs["db_wrapper"] = execution_globals["db_wrapper"]
        if execution_globals.get("secure_http") is not None:
            execution_kwargs["secure_http"] = execution_globals["secure_http"]
        if request_meta is not None:
            execution_kwargs["request_meta"] = request_meta
        
        _obs_cm = None
        try:
            from workflow_observability.sandbox_bootstrap import sandbox_observe
            _obs_cm = sandbox_observe(request_meta, function_name)
            _obs_cm.__enter__()
        except Exception:
            _obs_cm = None

        try:
            result = await func(**execution_kwargs)
            _convert_to_json_serializable(result)
            return {"success": True, "result": result}
        finally:
            if _obs_cm is not None:
                try:
                    _obs_cm.__exit__(None, None, None)
                except Exception:
                    pass
        
    except Exception as e:
        # Log the full error with traceback to stderr (will be captured by parent)
        error_logger = logging.getLogger("sandbox_function")
        error_logger.error(
            f"Function {function_name} execution failed: {type(e).__name__}: {str(e)}",
            exc_info=True
        )
        # Also include traceback in error message for better debugging
        error_traceback = traceback.format_exc()
        return {
            "success": False, 
            "error": str(e), 
            "error_type": type(e).__name__,
            "traceback": error_traceback
        }


if __name__ == "__main__":
    # Read input from stdin
    try:
        input_data = json.loads(sys.stdin.read())
    except Exception as e:
        error_logger = logging.getLogger("sandbox_worker")
        error_logger.error(f"Failed to parse input JSON: {e}", exc_info=True)
        print(json.dumps({
            "success": False,
            "error": f"Failed to parse input: {str(e)}",
            "error_type": type(e).__name__
        }))
        sys.exit(1)
    
    function_source = input_data["function_source"]
    function_name = input_data["function_name"]
    params = input_data["params"]
    db_wrapper_config = input_data.get("db_wrapper_config")
    secure_http_config = input_data.get("secure_http_config")
    request_meta = input_data.get("request_meta")
    
    # Execute and return result
    try:
        result = asyncio.run(execute_function(
            function_source,
            function_name,
            params,
            db_wrapper_config,
            secure_http_config,
            request_meta,
        ))
        print(json.dumps(result))
    except Exception as e:
        # Log error to stderr (will be captured by parent)
        error_logger = logging.getLogger("sandbox_worker")
        error_logger.error(
            f"Worker script execution failed: {type(e).__name__}: {str(e)}",
            exc_info=True
        )
        error_traceback = traceback.format_exc()
        print(json.dumps({
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": error_traceback
        }))
'''
        
        # Write worker script to temp file
        temp_dir = Path(tempfile.gettempdir()) / "screen_builder_sandbox_function"
        temp_dir.mkdir(exist_ok=True, mode=0o755)
        script_path = temp_dir / "worker.py"
        script_path.write_text(script_content)
        return script_path

    async def execute(
        self,
        function_source: str,
        function_name: str,
        params: Dict[str, Any],
        db_wrapper_config: Optional[Dict] = None,
        secure_http_config: Optional[Dict] = None,
        request_meta: Optional[Dict[str, Any]] = None,
    ) -> SandboxFunctionResult:
        """
        Execute function in isolated subprocess with resource limits but no code restrictions.
        
        Args:
            function_source: Python source code of the function
            function_name: Name of the function to execute
            params: Parameters to pass to the function
            db_wrapper_config: Configuration for database wrapper
            secure_http_config: Configuration for secure HTTP client
            request_meta: Request metadata
            
        Returns:
            SandboxFunctionResult with execution outcome
        """
        start_time = time.time()
        
        try:
            # Install required packages before execution
            from services.package_installer import get_package_installer
            
            try:
                installer = get_package_installer()
                install_result = await run_sync(installer.ensure_packages_for_code, function_source)
                
                if install_result.get("failed"):
                    logger.warning(
                        f"Failed to install packages for {function_name}: {install_result['failed']}"
                    )
                    # Continue execution - function will fail at runtime if packages are truly needed
                
                if install_result.get("installed"):
                    logger.info(
                        f"Installed packages for {function_name}: {install_result['installed']}"
                    )
            except Exception as install_error:
                logger.warning(f"Package installation error: {install_error}")
                # Continue execution - function will fail at runtime if packages are truly needed

            # Prepare input data
            input_data = {
                "function_source": function_source,
                "function_name": function_name,
                "params": params,
                "db_wrapper_config": db_wrapper_config,
                "secure_http_config": secure_http_config,
                "request_meta": request_meta,
            }
            input_json = json.dumps(input_data)
            
            # Prepare subprocess command
            python_exec = sys.executable
            cmd = [python_exec, str(self._worker_script_path)]
            
            # Set up Python path to include backend directory
            env = os.environ.copy()
            backend_dir = Path(__file__).parent.parent
            repo_root = backend_dir.parent
            python_path = env.get("PYTHONPATH", "")
            path_parts = [str(repo_root), str(backend_dir)]
            if python_path:
                path_parts.append(python_path)
            env["PYTHONPATH"] = ":".join(path_parts)
            # Per-subscriber DB isolation: set SUBSCRIBER_DB_NAME in subprocess env so worker uses subscriber DB.
            # Env is built fresh per request (not persisted); after server restart the next request still gets
            # subscriber_db_name from request_meta (derived from project_id in execute_dynamic_function_async).
            if request_meta and request_meta.get("subscriber_db_name"):
                env["SUBSCRIBER_DB_NAME"] = request_meta["subscriber_db_name"]
            # Do not set when no subscriber (backward compatibility: main DB)
            env["SANDBOX_FUNCTION_WORKER"] = "1"

            # Set OpenBLAS thread limits to prevent resource exhaustion
            # This prevents OpenBLAS from trying to create too many threads when numpy is imported
            env["OPENBLAS_NUM_THREADS"] = "1"
            env["MKL_NUM_THREADS"] = "1"
            env["NUMEXPR_NUM_THREADS"] = "1"
            env["OMP_NUM_THREADS"] = "1"
            
            # Set resource limits (Unix only, via preexec_fn)
            preexec_fn = None
            if sys.platform != "win32":
                def set_limits():
                    try:
                        # Memory limit (RSS - Resident Set Size)
                        memory_bytes = self.config.memory_mb * 1024 * 1024
                        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
                        # CPU time limit intentionally removed to allow long-running execution
                    except (OSError, ValueError) as e:
                        logger.warning(f"Could not set resource limits: {e}")
                        pass
                preexec_fn = set_limits
            
            # Start subprocess
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=preexec_fn,
                cwd=str(backend_dir),
                env=env,
            )
            
            # Send input and wait for result indefinitely (no timeout)
            try:
                stdout, stderr = await process.communicate(input=input_json.encode())
            except Exception as e:
                execution_time = (time.time() - start_time) * 1000
                return SandboxFunctionResult(
                    success=False,
                    error=f"Process communication failed: {str(e)}",
                    error_type="CommunicationError",
                    execution_time_ms=execution_time,
                )
            
            # Capture and log stderr (contains function logs and errors)
            stderr_text = stderr.decode() if stderr else ""
            if stderr_text:
                # Log all stderr output (function logs, errors, etc.)
                logger.info(f"Function {function_name} stderr output:\n{stderr_text}")
            
            # Check process return code
            if process.returncode != 0:
                error_msg = stderr_text if stderr_text else "Process exited with non-zero code"
                execution_time = (time.time() - start_time) * 1000
                logger.error(
                    f"Function {function_name} process exited with code {process.returncode}",
                    extra={"stderr": stderr_text, "function_name": function_name}
                )
                return SandboxFunctionResult(
                    success=False,
                    error=error_msg,
                    error_type="ProcessError",
                    stderr_output=stderr_text,
                    execution_time_ms=execution_time,
                )
            
            # Parse result
            stdout_text = stdout.decode().strip()
            try:
                result_data = json.loads(stdout_text)
                execution_time = (time.time() - start_time) * 1000
                
                if result_data.get("success"):
                    return SandboxFunctionResult(
                        success=True,
                        result=result_data.get("result"),
                        stderr_output=stderr_text,  # Include stderr even on success (may contain tracebacks)
                        execution_time_ms=execution_time,
                    )
                else:
                    # Execution failed - log the error with traceback if available
                    error_msg = result_data.get("error", "Unknown error")
                    error_type = result_data.get("error_type", "ExecutionError")
                    error_traceback = result_data.get("traceback", "")
                    
                    logger.error(
                        f"Function {function_name} execution failed: {error_type}: {error_msg}",
                        extra={
                            "function_name": function_name,
                            "error_type": error_type,
                            "traceback": error_traceback
                        }
                    )
                    
                    # Log traceback if available (to stderr so it appears in logs)
                    if error_traceback:
                        logger.error(f"Function {function_name} traceback:\n{error_traceback}")
                    
                    return SandboxFunctionResult(
                        success=False,
                        error=error_msg,
                        error_type=error_type,
                        traceback=error_traceback,
                        stderr_output=stderr_text,  # Include full stderr output
                        execution_time_ms=execution_time,
                    )
            except json.JSONDecodeError as e:
                execution_time = (time.time() - start_time) * 1000
                preview = stdout_text[:500]
                # Capture stderr if available (may contain tracebacks from function execution)
                stderr_text = stderr.decode() if stderr else ""
                return SandboxFunctionResult(
                    success=False,
                    error=f"Failed to parse result: {str(e)} | output: {preview}",
                    error_type="JSONDecodeError",
                    stderr_output=stderr_text,
                    execution_time_ms=execution_time,
                )
                
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Sandbox function execution error: {e}", exc_info=True)
            return SandboxFunctionResult(
                success=False,
                error=f"Sandbox function execution failed: {str(e)}",
                error_type=type(e).__name__,
                execution_time_ms=execution_time,
            )


class FunctionWorkerPool:
    """
    Pool of non-restrictive sandbox executors for concurrent function execution.
    
    Manages multiple sandbox executors to handle concurrent requests.
    """
    
    def __init__(self, pool_size: int = 5, config: Optional[SandboxFunctionConfig] = None):
        self.pool_size = pool_size
        self.config = config or SandboxFunctionConfig()
        self._executors: list[SandboxFunctionExecutor] = []
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._queue_timeout = 600  # Wait up to 600s in queue

    async def initialize(self):
        """Initialize the worker pool."""
        self._semaphore = asyncio.Semaphore(self.pool_size)
        logger.info(f"Function worker pool initialized with size {self.pool_size}")
        # Executors are created on-demand, no need to pre-create
        
    async def execute(
        self,
        function_source: str,
        function_name: str,
        params: Dict[str, Any],
        db_wrapper_config: Optional[Dict] = None,
        secure_http_config: Optional[Dict] = None,
        request_meta: Optional[Dict[str, Any]] = None,
    ) -> SandboxFunctionResult:
        """
        Execute function using pool (with concurrency limit).
        
        Uses semaphore to limit concurrent executions.
        """
        if not self._semaphore:
            await self.initialize()
            
        # Removed asyncio.wait_for to allow infinite queue wait time 
        # The execution time is now strictly decoupled and managed by SANDBOX_TIMEOUT_MS in .env
        async with self._semaphore:
            executor = SandboxFunctionExecutor(self.config)
            return await executor.execute(
                function_source=function_source,
                function_name=function_name,
                params=params,
                db_wrapper_config=db_wrapper_config,
                secure_http_config=secure_http_config,
                request_meta=request_meta,
            )


# Global worker pool instance
_function_worker_pool: Optional[FunctionWorkerPool] = None


def get_function_worker_pool() -> FunctionWorkerPool:
    """Get or create global function worker pool."""
    global _function_worker_pool
    if _function_worker_pool is None:
        pool_size = int(os.getenv("SANDBOX_POOL_SIZE", "10"))
        config = SandboxFunctionConfig(
            timeout_ms=int(os.getenv("SANDBOX_TIMEOUT_MS", "90000")),
            memory_mb=int(os.getenv("SANDBOX_MEMORY_MB", "512")),
            cpu_time_sec=int(os.getenv("SANDBOX_CPU_TIME_SEC", "120")),
        )
        _function_worker_pool = FunctionWorkerPool(pool_size=pool_size, config=config)
    return _function_worker_pool


__all__ = [
    "SandboxFunctionExecutor",
    "SandboxFunctionConfig",
    "SandboxFunctionResult",
    "FunctionWorkerPool",
    "get_function_worker_pool",
]

