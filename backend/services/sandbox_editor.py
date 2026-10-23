import os
import shutil
import uuid
import asyncio
import logging
import json
import re
import time
import functools
from pathlib import Path
from typing import Dict, List, Any, Optional

# Configure logging
logger = logging.getLogger(__name__)

async def run_sync(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    pfunc = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(None, pfunc)

class SandboxEditor:
    def __init__(self, base_temp_dir: str):
        """
        Initializes the Sandbox Service.
        :param base_temp_dir: The root directory where temporary sessions will be created.
        """
        self.base_temp_dir = Path(base_temp_dir).resolve() 
        self.base_temp_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Sandbox initialized at: {self.base_temp_dir}")

    def _get_session_path(self, session_id: str) -> Path:
        """Securely resolve the session path."""
        session_path = (self.base_temp_dir / session_id).resolve()
        if not str(session_path).startswith(str(self.base_temp_dir)):
            raise ValueError("Invalid session ID or path traversal attempt")
        return session_path

    def _get_safe_file_path(self, session_id: str, relative_path: str) -> Path:
        """Resolves a file path securely."""
        session_root = self._get_session_path(session_id)
        target_path = (session_root / relative_path).resolve()
        if not str(target_path).startswith(str(session_root)):
            raise PermissionError(f"Access Denied: Path traversal detected for {relative_path}")
        return target_path

    async def init_session(self, project_path: str, selector: str = "app-root") -> Dict[str, Any]:
        """
        Creates a temporary session copy of the project.
        """
        session_id = str(uuid.uuid4())
        session_dir = self._get_session_path(session_id)
        
        try:
            if os.path.exists(project_path):
                await run_sync(
                    shutil.copytree,
                    project_path,
                    session_dir,
                    symlinks=True,
                    ignore=shutil.ignore_patterns("node_modules", ".git", "dist", ".angular")
                )
            else:
                raise FileNotFoundError(f"Source project not found: {project_path}")

            # Generate initial file tree (async wrapper)
            file_tree = await self.generate_file_tree_async(session_dir)

            return {
                "session_id": session_id,
                "root_path": str(session_dir),
                "file_tree": file_tree,
                "selector": selector
            }
        except Exception as e:
            logger.error(f"Failed to init session: {e}")
            # Clean up partial creation
            if session_dir.exists():
                await run_sync(shutil.rmtree, session_dir, ignore_errors=True)
            raise

    def save_backend_draft(self, session_id: str, function_id: str, source_code: str, models_source: str, is_live_equivalent: bool = False) -> None:
        """
        Saves a draft version of a backend function to the session.
        [UPDATED] Supports Smart Dirty Flag: If content matches live, we clear the dirty flag.
        """
        session_path = self._get_session_path(session_id)
        draft_dir = session_path / ".backend_drafts"
        draft_dir.mkdir(exist_ok=True)
        dirty_flag = session_path / ".backend_dirty"

        # 1. Save the draft content
        draft_file = draft_dir / f"{function_id}.json"
        data = {
            "source_code": source_code,
            "models_source": models_source,
            "updated_at": str(time.time())
        }
        draft_file.write_text(json.dumps(data, indent=2), encoding='utf-8')

        # 2. Manage Dirty Flag
        if is_live_equivalent:
            if dirty_flag.exists():
                try:
                    dirty_flag.unlink()
                    logger.info(f"[{session_id}] Code reverted to Live state. Dirty flag CLEARED.")
                except Exception as e:
                    logger.warning(f"Failed to clear dirty flag: {e}")
        else:
            if not dirty_flag.exists():
                try:
                    dirty_flag.touch()
                    logger.info(f"[{session_id}] Code changed. Session marked as DIRTY.")
                except Exception as e:
                    logger.warning(f"Failed to set dirty flag: {e}")

    def is_backend_dirty(self, session_id: str) -> bool:
        session_path = self._get_session_path(session_id)
        return (session_path / ".backend_dirty").exists()

    def has_build_artifacts(self, session_id: str) -> bool:
        session_path = self._get_session_path(session_id)
        return (session_path / "dist").exists() and any((session_path / "dist").iterdir())


    async def run_build(self, session_id: str, progress_callback=None, 
                        api_external_url: str = "",
                        template_search_url: str = "", 
                        clean_build: bool = False) -> Dict[str, str]:
        
        session_path = self._get_session_path(session_id)
        
        # 1. Install deps if missing
        if not (session_path / "node_modules").exists():
            if progress_callback: await progress_callback("log", "Installing dependencies...")
            await (await asyncio.create_subprocess_exec("npm", "install", cwd=str(session_path))).wait()

        target_file_path = None
        original_content = None 
        dirty_flag = session_path / ".backend_dirty"
        
        # Normalize URLs for search
        live_url = api_external_url.rstrip('/')
        search_pattern = template_search_url if template_search_url else live_url

        # Robust Regex to catch ANY stuck proxy URL from previous crashes
        STALE_PROXY_REGEX = r"https?://[^\"'\s]+/aiqod-agent/agent/editor/proxy/[^/]+/execute"

        try:
            # 2. LOCATE & SANITIZE THE FILE
            src_path = session_path / "src"
            if src_path.exists():
                for path in src_path.rglob("*"):
                    if path.is_file() and path.suffix in ['.ts', '.js', '.environment']:
                        try:
                            content = path.read_text(encoding='utf-8')
                            
                            # A. Always Clean First: Remove any stale proxy URLs found
                            if re.search(STALE_PROXY_REGEX, content):
                                content = re.sub(STALE_PROXY_REGEX, live_url, content)
                                path.write_text(content, encoding='utf-8') # Fix on disk immediately
                                if progress_callback: await progress_callback("log", "🧹 Sanatized stale proxy URL.")
                            
                            # B. Identify Target File
                            if search_pattern in content:
                                target_file_path = path
                                original_content = content
                                break
                        except:
                            continue

            # 3. INJECT PROXY (Only if explicitly needed)
            if not clean_build and dirty_flag.exists() and target_file_path and original_content:
                if progress_callback: await progress_callback("log", "Backend changes detected. Injecting Session Proxy...")
                
                proxy_url = f"{live_url}/aiqod-agent/agent/editor/proxy/{session_id}/execute"
                new_content = original_content.replace(search_pattern, proxy_url)
                target_file_path.write_text(new_content, encoding='utf-8')

            elif clean_build:
                if progress_callback: await progress_callback("log", "Clean Build Requested. Enforcing Live URL.")
            else:
                if progress_callback: await progress_callback("log", "No backend changes. Using Live URL.")

            # 4. Run Build
            if progress_callback: await progress_callback("log", "Starting Angular build...")
            
            process = await asyncio.create_subprocess_exec(
                "npm", "run", "build", "--", "--base-href", "./", "--configuration", "production", "--output-hashing=none",
                cwd=str(session_path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )

            async def stream_log(stream, kind):
                while line := await stream.readline():
                    if progress_callback: await progress_callback(kind, line.decode().strip())

            await asyncio.gather(stream_log(process.stdout, "STDOUT"), stream_log(process.stderr, "STDERR"))
            
            if await process.wait() == 0:
                dist = list((session_path / "dist").rglob("index.html"))
                return {"status": "success", "preview_path": str(dist[0].relative_to(self.base_temp_dir))} if dist else {"status": "error", "message": "No index.html found"}
            return {"status": "error", "message": "Build failed"}

        finally:
            # [CRITICAL] RESTORE CLEAN CONTENT
            if target_file_path and original_content is not None:
                try:
                    target_file_path.write_text(original_content, encoding='utf-8')
                    if progress_callback: await progress_callback("log", "Restored original source file configuration.")
                except Exception as e:
                    logger.error(f"Failed to restore file {target_file_path}: {e}")

    # --- Session Cleanup Logic (NEW) ---

    def wipe_all_sessions(self):
        """Forcefully removes ALL sessions (Used on server startup)."""
        if self.base_temp_dir.exists():
            logger.warning(f"🧹 Startup Scavenger: Wiping all sessions in {self.base_temp_dir}")
            try:
                shutil.rmtree(self.base_temp_dir)
                self.base_temp_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.error(f"Failed to wipe sessions: {e}")

    def prune_stale_sessions(self, max_age_seconds: int = 3600):
        """Removes sessions not modified in the last hour."""
        if not self.base_temp_dir.exists(): return
        
        now = time.time()
        for item in self.base_temp_dir.iterdir():
            if item.is_dir():
                try:
                    # Check directory modification time
                    if now - item.stat().st_mtime > max_age_seconds:
                        logger.info(f"💀 Reaper: Removing stale session {item.name}")
                        shutil.rmtree(item)
                except Exception as e:
                    logger.error(f"Error pruning session {item.name}: {e}")

    def get_session_file_tree(self, session_id: str) -> List[Dict]:
        """Returns the file tree for an existing session."""
        session_path = self._get_session_path(session_id)
        if not session_path.exists():
            raise FileNotFoundError("Session does not exist")
        return self.generate_file_tree(session_path)

    def get_backend_draft(self, session_id: str, function_id: str) -> Optional[Dict]:
        """Retrieves a draft backend function if it exists."""
        session_path = self._get_session_path(session_id)
        draft_file = session_path / ".backend_drafts" / f"{function_id}.json"
        
        if draft_file.exists():
            try:
                return json.loads(draft_file.read_text(encoding='utf-8'))
            except Exception:
                return None
        return None

    def list_backend_drafts(self, session_id: str) -> List[str]:
        """Lists all function IDs that have drafts in this session."""
        session_path = self._get_session_path(session_id)
        draft_dir = session_path / ".backend_drafts"
        if not draft_dir.exists():
            return []
        
        return [f.stem for f in draft_dir.glob("*.json")]
    
    def clear_dirty_flag(self, session_id: str) -> None:
        session_path = self._get_session_path(session_id)
        dirty_flag = session_path / ".backend_dirty"
        if dirty_flag.exists():
            try:
                dirty_flag.unlink() # Delete the file
                logger.info(f"[{session_id}] Dirty flag cleared. Session is now CLEAN.")
            except Exception as e:
                logger.warning(f"[{session_id}] Failed to clear dirty flag: {e}")

    def read_file(self, session_id: str, file_path: str) -> str:
        """Reads a text file from the sandbox."""
        target_path = self._get_safe_file_path(session_id, file_path)
        if not target_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        return target_path.read_text(encoding='utf-8')

    def write_file(self, session_id: str, file_path: str, content: str) -> None:
        """Overwrites a file in the sandbox."""
        target_path = self._get_safe_file_path(session_id, file_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding='utf-8')

    def publish_changes(self, session_id: str, target_path_str: str) -> None:
        """Publishes the BUILT ARTIFACTS (dist) to the target directory."""
        session_path = self._get_session_path(session_id)
        target_path = Path(target_path_str)
        
        dist_path = session_path / "dist"
        index_html_files = list(dist_path.rglob("index.html"))
        
        if not index_html_files:
             raise FileNotFoundError("No build artifacts found.")
        
        build_output_root = index_html_files[0].parent

        if target_path.exists() or target_path.is_symlink():
            if target_path.is_symlink():
                target_path.unlink()
            elif target_path.is_dir():
                shutil.rmtree(target_path)
            else:
                target_path.unlink()

        shutil.copytree(build_output_root, target_path)

    def save_source_back(self, session_id: str, original_source_path: str) -> None:
        """Syncs modified source code BACK to the original workspace."""
        session_path = self._get_session_path(session_id)
        target_path = Path(original_source_path)

        if not target_path.exists():
             raise FileNotFoundError(f"Original source path not found: {original_source_path}")

        # Note: We EXCLUDE .backend_drafts from syncing back to source
        ignore_patterns = shutil.ignore_patterns(
            'node_modules', 'dist', '.git', '.angular', 'tmp', '.editorconfig', '.backend_drafts'
        )

        shutil.copytree(
            session_path, 
            target_path, 
            ignore=ignore_patterns, 
            dirs_exist_ok=True
        )

    def cleanup_session(self, session_id: str) -> None:
        """Deletes the temporary session directory."""
        session_path = self._get_session_path(session_id)
        if session_path.exists():
            shutil.rmtree(session_path)

    def generate_file_tree(self, path: Path, relative_root: str = "") -> List[Dict]:
        """Recursive helper to generate JSON file tree."""
        tree = []
        try:
            items = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            for item in items:
                # Hide internal folders
                if item.name in ['node_modules', '.git', 'dist', '.angular', '__pycache__', '.editorconfig', '.gitignore', '.backend_drafts']:
                    continue
                item_rel_path = f"{relative_root}/{item.name}" if relative_root else item.name
                node = {
                    "name": item.name,
                    "path": item_rel_path, 
                    "type": "dir" if item.is_dir() else "file"
                }
                if item.is_dir():
                    node["children"] = self.generate_file_tree(item, item_rel_path)
                tree.append(node)
        except PermissionError:
            pass 
        return tree

    def save_last_error(self, session_id: str, error_data: Dict[str, Any]) -> None:
        """Saves the last error details to a JSON file in the session."""
        try:
            file_path = self._get_safe_file_path(session_id, "last_error.json")
            # Ensure the directory exists
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(error_data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save last error: {e}")

    def get_last_error(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves the last error details if they exist."""
        try:
            file_path = self._get_safe_file_path(session_id, "last_error.json")
            if not file_path.exists():
                return None
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to get last error: {e}")
            return None
            
    async def generate_file_tree_async(self, path: Path, rel_path: str = ""):
        """Async wrapper for generating file tree"""
        return await run_sync(self.generate_file_tree, path, rel_path)

    async def save_last_error_async(self, session_id: str, error_data: Dict[str, Any]):
        """Async wrapper for saving error logs"""
        return await run_sync(self.save_last_error, session_id, error_data)

    async def get_last_error_async(self, session_id: str):
        """Async wrapper for reading error logs"""
        return await run_sync(self.get_last_error, session_id)
        
    async def read_file_async(self, session_id: str, file_path: str):
        """Async wrapper for reading a file"""
        # Assuming you have a read_file method, if not, skip this
        if hasattr(self, 'read_file'):
             return await run_sync(self.read_file, session_id, file_path)