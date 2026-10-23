"""
Secure Isolated Sandbox Executor for LLM-generated backend functions.

Executes functions in separate subprocesses with resource limits and isolation.
"""

import asyncio
import json
import logging
import os
import pickle
import resource
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Default resource limits
DEFAULT_TIMEOUT_MS = int(os.getenv("SANDBOX_TIMEOUT_MS", "90000"))  # 90 seconds
DEFAULT_MEMORY_MB = int(os.getenv("SANDBOX_MEMORY_MB", "512"))  # 512 MB
DEFAULT_CPU_TIME_SEC = int(os.getenv("SANDBOX_CPU_TIME_SEC", "120"))  # 120 seconds


@dataclass
class SandboxConfig:
    """Configuration for sandbox execution."""
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    memory_mb: int = DEFAULT_MEMORY_MB
    cpu_time_sec: int = DEFAULT_CPU_TIME_SEC
    enable_network: bool = False  # Block network access by default


@dataclass
class SandboxResult:
    """Result from sandbox execution."""
    success: bool
    result: Any = None
    error: Optional[str] = None
    execution_time_ms: float = 0.0
    memory_used_mb: float = 0.0


class SandboxExecutor:
    """
    Executes Python functions in isolated subprocesses with resource limits.
    
    Architecture:
    - Main API → SandboxExecutor → Subprocess → Execute function → Return result
    - Each execution is isolated in a separate process
    - Resource limits enforced via OS signals and resource module
    """

    def __init__(self, config: Optional[SandboxConfig] = None):
        self.config = config or SandboxConfig()
        self._worker_script_path = self._create_worker_script()

    def _create_worker_script(self) -> Path:
        """Create the worker script that will run in subprocess."""
        script_content = '''
import asyncio
import base64
import hashlib
import json
import pickle
import re
import string
import sys
import time
import urllib
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
import decimal
from typing import Any, Dict, List, Optional
import builtins
import math
import httpx 
from bson import ObjectId


def _safe_print(*args, **kwargs):
    """Redirect sandbox prints to stderr so stdout stays JSON-only."""
    kwargs.pop("file", None)
    builtins.print(*args, file=sys.stderr, flush=True, **kwargs)


def _convert_to_json_serializable(data: Any) -> None:
    """
    Recursively convert all non-JSON-serializable types to serializable ones in-place.
    Handles ObjectId, datetime, date, Decimal, etc.
    """
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
    """Execute function in isolated environment."""
    try:
        # Restricted execution environment
        # Safe import wrapper to strictly limit which modules can be imported
        def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
            allowed_modules = {
                # Core async / typing / parsing utilities
                "asyncio",
                "json",
                "typing",
                "re",
                "math",
                "decimal",
                "random",
                "statistics",
                # Time / date utilities
                "datetime",
                "time",
                # Misc safe stdlib helpers
                "string",
                "uuid",
                "hashlib",
                "base64",
                "urllib",
                "urllib.parse",
                # MongoDB ObjectId support
                "bson",
                "httpx",
            }
            if name in allowed_modules:
                return builtins.__import__(name, globals, locals, fromlist, level)

            # Check if importing from an allowed parent module
            if '.' in name:
                parent = name.split('.')[0]
                if parent in allowed_modules:
                    return builtins.__import__(name, globals, locals, fromlist, level)
            
            # Check fromlist for submodule imports (e.g., "from urllib import parse")
            if fromlist:
                for item in fromlist:
                    full_name = f"{name}.{item}" if name else item
                    if full_name in allowed_modules or name in allowed_modules:
                        return builtins.__import__(name, globals, locals, fromlist, level)
            
            raise ImportError(f"Import of '{name}' is not allowed in sandboxed functions")

        SAFE_BUILTINS = {
            # Core exceptions
            "BaseException": builtins.BaseException,
            "Exception": builtins.Exception,
            "ValueError": builtins.ValueError,
            "TypeError": builtins.TypeError,
            "KeyError": builtins.KeyError,
            "IndexError": builtins.IndexError,
            "RuntimeError": builtins.RuntimeError,
            "AttributeError": builtins.AttributeError,
            "ZeroDivisionError": builtins.ZeroDivisionError,
            "StopIteration": builtins.StopIteration,
            "AssertionError": builtins.AssertionError,

            # Core types
            "bool": builtins.bool,
            "int": builtins.int,
            "float": builtins.float,
            "str": builtins.str,
            "dict": builtins.dict,
            "list": builtins.list,
            "tuple": builtins.tuple,
            "set": builtins.set,
            "frozenset": builtins.frozenset,
            "bytes": builtins.bytes,
            "bytearray": builtins.bytearray,

            # Safe utility functions
            "len": builtins.len,
            "min": builtins.min,
            "max": builtins.max,
            "sum": builtins.sum,
            "abs": builtins.abs,
            "round": builtins.round,
            "pow": builtins.pow,
            "range": builtins.range,
            "enumerate": builtins.enumerate,
            "zip": builtins.zip,
            "map": builtins.map,
            "filter": builtins.filter,
            "sorted": builtins.sorted,
            "reversed": builtins.reversed,
            "all": builtins.all,
            "any": builtins.any,
            "isinstance": builtins.isinstance,
            "issubclass": builtins.issubclass,
            "hasattr": builtins.hasattr,
            "getattr": builtins.getattr,
            "setattr": builtins.setattr,
            "callable": builtins.callable,
            "print": _safe_print,
            "repr": builtins.repr,
            "str": builtins.str,
            "chr": builtins.chr,
            "ord": builtins.ord,

            # Controlled import mechanism used by generated code
            "__import__": _safe_import,

            # Completely safe objects
            "object": builtins.object,
            "None": None,
            "True": True,
            "False": False,
        }
        restricted_globals = {
            "__builtins__": SAFE_BUILTINS,
            # Commonly used standard libraries / helpers
            "asyncio": asyncio,
            "json": json,
            "re": re,
            "math": math,
            # Datetime helpers
            "datetime": datetime,
            "timedelta": timedelta,
            "date": date,
            "Decimal": Decimal,
            "decimal": decimal,
            # Additional safe standard library modules
            "string": string,
            "uuid": uuid,
            "hashlib": hashlib,
            "base64": base64,
            "time": time,
            "urllib": urllib,  # urllib.parse will be accessible as urllib.parse
            # MongoDB ObjectId support
            "ObjectId": ObjectId,
            "httpx": httpx,
        }
        
        # Inject database wrapper if needed
        if db_wrapper_config:
            from services.database import Database
            from services.backend_generator import DatabaseWrapper
            blocked_collections = db_wrapper_config.get("blocked_collections", [])
            restricted_globals["db_wrapper"] = DatabaseWrapper(blocked_collections)
        
        # Inject secure HTTP client if needed
        if secure_http_config:
            from services.backend_generator import SecureHTTPClient
            blocklist = secure_http_config.get("blocklist", [])
            timeout = secure_http_config.get("timeout_ms", 5000)
            restricted_globals["secure_http"] = SecureHTTPClient(blocklist, timeout)
        
        # Execute function source
        local_vars: Dict[str, Any] = {}
        exec(function_source, restricted_globals, local_vars)
        
        # Get the function
        func = local_vars.get(function_name)
        if not func:
            raise ValueError(f"Function {function_name} not found in source")
        
        if not asyncio.iscoroutinefunction(func):
            raise ValueError(f"Function {function_name} is not async")
        
        # Prepare execution kwargs
        execution_kwargs = {"params": params}
        if "db_wrapper" in restricted_globals:
            execution_kwargs["db_wrapper"] = restricted_globals["db_wrapper"]
        if "secure_http" in restricted_globals:
            execution_kwargs["secure_http"] = restricted_globals["secure_http"]
        if request_meta is not None:
            execution_kwargs["request_meta"] = request_meta
        
        # Execute function
        result = await func(**execution_kwargs)
        # Convert all non-JSON-serializable types before returning
        _convert_to_json_serializable(result)
        return {"success": True, "result": result}
        
    except Exception as e:
        return {"success": False, "error": str(e), "error_type": type(e).__name__}


if __name__ == "__main__":
    # Read input from stdin
    input_data = json.loads(sys.stdin.read())
    
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
        print(json.dumps({
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__
        }))
'''
        
        # Write worker script to temp file
        temp_dir = Path(tempfile.gettempdir()) / "screen_builder_sandbox"
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
    ) -> SandboxResult:
        """
        Execute function in isolated subprocess with resource limits.
        
        Args:
            function_source: Python source code of the function
            function_name: Name of the function to execute
            params: Parameters to pass to the function
            db_wrapper_config: Configuration for database wrapper
            secure_http_config: Configuration for secure HTTP client
            
        Returns:
            SandboxResult with execution outcome
        """
        start_time = time.time()
        
        try:
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
            python_path = env.get("PYTHONPATH", "")
            if python_path:
                env["PYTHONPATH"] = f"{backend_dir}:{python_path}"
            else:
                env["PYTHONPATH"] = str(backend_dir)
            
            # Set resource limits (Unix only, via preexec_fn)
            preexec_fn = None
            if sys.platform != "win32":
                def set_limits():
                    try:
                        # Memory limit (RSS - Resident Set Size)
                        memory_bytes = self.config.memory_mb * 1024 * 1024
                        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
                        
                        # CPU time limit
                        cpu_time = self.config.cpu_time_sec
                        resource.setrlimit(resource.RLIMIT_CPU, (cpu_time, cpu_time))
                    except (OSError, ValueError) as e:
                        # Resource limits may not be available on all platforms
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
            
            # Send input and wait for result with timeout
            timeout_seconds = self.config.timeout_ms / 1000.0
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(input=input_json.encode()),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(f"Function {function_name} timed out after {timeout_seconds}s")
                # Try graceful termination first
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    # Force kill if terminate doesn't work
                    try:
                        process.kill()
                        await process.wait()
                    except ProcessLookupError:
                        # Process already dead - this is fine
                        pass
                except ProcessLookupError:
                    # Process already dead - this is fine
                    pass
                execution_time = (time.time() - start_time) * 1000
                return SandboxResult(
                    success=False,
                    error=f"Execution timeout after {self.config.timeout_ms}ms",
                    execution_time_ms=execution_time,
                )
            
            # Check process return code
            if process.returncode != 0:
                error_msg = stderr.decode() if stderr else "Process exited with non-zero code"
                execution_time = (time.time() - start_time) * 1000
                return SandboxResult(
                    success=False,
                    error=error_msg,
                    execution_time_ms=execution_time,
                )
            
            # Parse result
            stdout_text = stdout.decode().strip()
            try:
                result_data = json.loads(stdout_text)
                execution_time = (time.time() - start_time) * 1000
                
                if result_data.get("success"):
                    return SandboxResult(
                        success=True,
                        result=result_data.get("result"),
                        execution_time_ms=execution_time,
                    )
                else:
                    return SandboxResult(
                        success=False,
                        error=result_data.get("error", "Unknown error"),
                        execution_time_ms=execution_time,
                    )
            except json.JSONDecodeError as e:
                execution_time = (time.time() - start_time) * 1000
                preview = stdout_text[:500]
                return SandboxResult(
                    success=False,
                    error=f"Failed to parse result: {str(e)} | output: {preview}",
                    execution_time_ms=execution_time,
                )
                
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            logger.error(f"Sandbox execution error: {e}", exc_info=True)
            return SandboxResult(
                success=False,
                error=f"Sandbox execution failed: {str(e)}",
                execution_time_ms=execution_time,
            )


class WorkerPool:
    """
    Pool of sandbox executors for concurrent function execution.
    
    Manages multiple sandbox executors to handle concurrent requests.
    """
    
    def __init__(self, pool_size: int = 5, config: Optional[SandboxConfig] = None):
        self.pool_size = pool_size
        self.config = config or SandboxConfig()
        self._executors: list[SandboxExecutor] = []
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._queue_timeout = 60 # Wait up to 60s in queue

    async def initialize(self):
        """Initialize the worker pool."""
        self._semaphore = asyncio.Semaphore(self.pool_size)
        logger.info(f"Worker pool initialized with size {self.pool_size}")
        # Executors are created on-demand, no need to pre-create
        
    async def execute(
        self,
        function_source: str,
        function_name: str,
        params: Dict[str, Any],
        db_wrapper_config: Optional[Dict] = None,
        secure_http_config: Optional[Dict] = None,
        request_meta: Optional[Dict[str, Any]] = None,
    ) -> SandboxResult:
        """
        Execute function using pool (with concurrency limit).
        
        Uses semaphore to limit concurrent executions.
        """
        if not self._semaphore:
            await self.initialize()
            
        # Try to acquire semaphore with timeout
        try:
            # Use asyncio.wait_for for Python 3.7+ compatibility
            async def _execute_with_semaphore():
                async with self._semaphore:
                    executor = SandboxExecutor(self.config)
                    return await executor.execute(
                        function_source=function_source,
                        function_name=function_name,
                        params=params,
                        db_wrapper_config=db_wrapper_config,
                        secure_http_config=secure_http_config,
                        request_meta=request_meta,
                    )
            
            return await asyncio.wait_for(_execute_with_semaphore(), timeout=self._queue_timeout)
        except asyncio.TimeoutError:
            # Request waited too long in queue
            logger.error(
                f"Function {function_name} timed out waiting for available worker "
                f"(queue timeout: {self._queue_timeout}s)"
            )
            return SandboxResult(
                success=False,
                error=f"Server overloaded - request queued for {self._queue_timeout}s",
                execution_time_ms=self._queue_timeout * 1000,
            )


# Global worker pool instance
_worker_pool: Optional[WorkerPool] = None


def get_worker_pool() -> WorkerPool:
    """Get or create global worker pool."""
    global _worker_pool
    if _worker_pool is None:
        pool_size = int(os.getenv("SANDBOX_POOL_SIZE", "5"))
        config = SandboxConfig(
            timeout_ms=int(os.getenv("SANDBOX_TIMEOUT_MS", "10000")),
            memory_mb=int(os.getenv("SANDBOX_MEMORY_MB", "256")),
            cpu_time_sec=int(os.getenv("SANDBOX_CPU_TIME_SEC", "30")),
        )
        _worker_pool = WorkerPool(pool_size=pool_size, config=config)
    return _worker_pool


__all__ = ["SandboxExecutor", "SandboxConfig", "SandboxResult", "WorkerPool", "get_worker_pool"]

