import asyncio
import contextvars
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Callable
import functools
from services.gemini_service import (
    sync_generate,
    GeminiGenerateConfig,
    get_gemini_api_key,
    get_or_create_cache,
)
from services.gemini_service import _is_fallback_eligible_error as _gemini_is_fallback_eligible_error
from services.design_reference import get_angular_stack_guidelines
from services.workflow_budget_service import BillingError

import logging

try:
    from pydantic import RootModel
except ImportError:
    class RootModel(BaseModel):
        __root__: Any

# Configure root logger (you can configure this outside too)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

from services.llm_token_tracker import record_llm_call
from services.llm_utils import extract_token_usage_from_response

# Load config.json at module level
_config = None
def _load_config():
    """Load config.json file."""
    global _config
    if _config is None:
        services_dir = Path(__file__).parent
        backend_dir = services_dir.parent
        config_path = backend_dir / "config.json"
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                _config = json.load(f)
        else:
            logger.warning(f"config.json not found at {config_path}, using empty config")
            _config = {}
    return _config

async def run_sync(func, *args, **kwargs):
    """Runs a blocking function in a separate thread, with current context vars copied so
    record_llm_call (and other context-dependent code) sees user_prompt/enhanced_prompt."""
    ctx = contextvars.copy_context()
    def run_in_context():
        return ctx.run(func, *args, **kwargs)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, run_in_context)
_CONFIG_CACHE: Optional[Dict[str, Any]] = None
        
@dataclass
class GeneratedFile:
    """Data container representing a single generated Angular file."""

    path: str
    content: str


# Written deterministically by the build system; LLM output for these paths is ignored.
PROTECTED_GENERATED_PATHS = frozenset({
    "src/screen-runtime.ts",
    "screen-runtime.ts",
    "src/main.ts",
    "main.ts",
})


_ANGULAR_CACHE_TTL_SECONDS = 3600
# Bump when cached system prompt text changes (invalidates stale Gemini context caches).
_ANGULAR_CODEGEN_CACHE_PREFIX_FIGMA = "angular-codegen-v2-figma"
_ANGULAR_CODEGEN_CACHE_PREFIX_TAILWIND = "angular-codegen-v2-tailwind"


def _append_angular_codegen_mode_block(
    prompt_parts: List[str],
    *,
    is_figma_mode: bool,
) -> None:
    """Append figma vs tailwind styling mode block (shared by cached and uncached paths)."""
    if is_figma_mode:
        prompt_parts.append("""
            ## STRICT FIGMA MODE (CRITICAL OVERRIDE)
            Because this UI is generated directly from a Figma design, you MUST IGNORE the standard responsive/Tailwind generic rules.
            - **NO CONTAINER QUERIES OR VIEWPORT BREAKPOINTS**: Do NOT use `@container`, `sm:`, `md:`, etc.
            - **NO GENERIC SPACING/TYPOGRAPHY**: Do NOT use Tailwind spacing (`p-4`, `gap-6`) or typography (`text-xl`, `font-bold`) if exact CSS values are provided in the IR Schema.
            - **EXACT 1:1 REPLICA**: Write custom CSS in the component's `.css` file or use exact arbitrary Tailwind values (e.g., `w-[320px]`, `text-[14px]`, `text-[#1A2B3C]`) based strictly on the IR Schema's `style` object.
            - Force exact dimensions (Width/Height), absolute/flex positioning, and hex colors exactly as stated in the Schema.
            - Your goal is an EXACT pixel-perfect visual replica.
            """)
    else:
        prompt_parts.append("""
            ## RESPONSIVE & TAILWIND MODE
            Use standard Tailwind utility classes, container queries (@container), and standard spacing/typography to ensure a clean, responsive layout as outlined in the design principles.
            """)


async def _get_angular_codegen_cached_content(
    model: str,
    *,
    system_text: str,
    is_figma_mode: bool,
) -> Optional[str]:
    """
    Explicit context cache for static Angular codegen system prompts.

    Cost optimization only — returns None on failure so callers keep working
    via the existing uncached full prompt path.
    """
    try:
        cache_key_prefix = (
            _ANGULAR_CODEGEN_CACHE_PREFIX_FIGMA
            if is_figma_mode
            else _ANGULAR_CODEGEN_CACHE_PREFIX_TAILWIND
        )
        display_name = (
            "angular-generator-codegen-figma"
            if is_figma_mode
            else "angular-generator-codegen-tailwind"
        )
        cache_key = (
            cache_key_prefix
            + "_"
            + hashlib.sha256(system_text.encode("utf-8")).hexdigest()[:12]
        )
        return await get_or_create_cache(
            model=model,
            system_text=system_text,
            ttl_seconds=_ANGULAR_CACHE_TTL_SECONDS,
            cache_key=cache_key,
            api_key=get_gemini_api_key() or None,
            display_name=display_name,
        )
    except Exception as exc:
        logger.warning(
            "Angular codegen cache unavailable, continuing uncached: %s",
            exc,
        )
        return None


def _angular_sync_llm_call(
    model: str,
    *,
    user_prompt: str,
    full_contents: str,
    cached_content: Optional[str],
    config: GeminiGenerateConfig,
) -> Any:
    """
    Angular LLM call with optional context cache.

    When *cached_content* is set, sends *user_prompt* only (system is cached).
    Otherwise sends *full_contents* (original single-string prompt).
    """
    if cached_content:
        contents = user_prompt
        config = replace(config, cached_content=cached_content)
    else:
        contents = full_contents
    return sync_generate(model, contents, config=config)


def _is_recitation_finish_reason(finish_reason: Any) -> bool:
    """True for Gemini FinishReason.RECITATION (enum, name, or integer 4)."""
    if finish_reason == 4:
        return True
    text = str(finish_reason or "").upper()
    name = str(getattr(finish_reason, "name", "") or "").upper()
    return (
        text in {"4", "RECITATION"}
        or "RECITATION" in text
        or "RECITATION" in name
    )


def _angular_error_is_recitation(exc: Optional[BaseException]) -> bool:
    """True when an _invoke_model abort was caused by RECITATION."""
    err = str(exc or "")
    if "RECITATION" in err.upper():
        return True
    # "Generation aborted. Finish reason: 4"
    err_u = err.upper()
    if "FINISH REASON" not in err_u:
        return False
    tail = err_u.rsplit(":", 1)[-1].strip().rstrip(".")
    token = tail.split()[-1] if tail else ""
    return token == "4" or _is_recitation_finish_reason(token)


def _angular_error_is_cache_model_mismatch(exc: Optional[BaseException]) -> bool:
    """True for Gemini 400: request model ≠ CachedContent model."""
    compact = str(exc or "").lower().replace(" ", "")
    return "cachedcontent" in compact and "hastobethesame" in compact


def _adjust_angular_retry_for_model_cache(
    *,
    last_exception: Optional[BaseException],
    selected_model: str,
    cached_content: Optional[str],
    recitation_fallback_used: bool,
    fix_model_name: str,
    cache_created_for_model: str,
) -> Tuple[str, Optional[str], bool]:
    """Keep CachedContent only when the request model still matches the cache.

    Live Issue 6: RECITATION fallback switched gemini-3.1-pro-preview →
    gemini-2.5-pro but reused the 3.1 cache, so attempts 2–5 all returned
    400 INVALID_ARGUMENT (model and CachedContent must be the same).
    """
    model = selected_model
    cache = cached_content
    fallback_used = recitation_fallback_used

    if _angular_error_is_recitation(last_exception) and not fallback_used:
        model = fix_model_name or model
        fallback_used = True

    cache_mismatch = bool(cache) and bool(model) and model != cache_created_for_model
    if cache and (cache_mismatch or _angular_error_is_cache_model_mismatch(last_exception)):
        cache = None

    return model, cache, fallback_used


class AngularBuildError(RuntimeError):
    """Raised when Angular build keeps failing after automated recovery attempts."""

    def __init__(self, message: str, attempts: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.attempts = attempts or []


class AngularAppGenerator:
    """
    Service that prompts Gemini 2.5 Flash with the Angular-generation instructions,
    parses the returned file bundle, and writes the result into a temporary folder.
    
    Directory Lifecycle:
    --------------------
    1. generated-apps/angular-{project_id}-{screen_id}/
       - SOURCE CODE (KEEP) - Merged base boilerplate + LLM-generated files
       - This is the editable source. For updates, edit files here and rebuild.
       - Contains: package.json, src/, angular.json, etc.
    
    2. isolated-angular-apps/angular-{project_id}-{screen_id}-{timestamp}/
       - BUILD SANDBOX (CLEANUP) - Timestamped copy used for isolated builds
       - Automatically cleaned up after successful builds (keeps last 3 for debugging)
       - Contains: Full project copy + node_modules/ + dist/ after build
    
    3. liberty_fs_uploads/{screen_id}/
       - PRODUCTION ASSETS (KEEP) - Final compiled static files served to users
       - This is what gets deployed to the CDN/server
       - Contains: index.html, main-[hash].js, styles-[hash].css, assets/
       - DO NOT edit directly - it gets overwritten on next build
    
    Update Workflow:
    ----------------
    For user-requested changes (colors, components, etc.):
    1. Edit source code in generated-apps/angular-{project_id}-{screen_id}/
    2. Call generate_app() again OR manually rebuild:
       - Copy to isolated directory
       - Run npm install && npm run build
       - Copy dist/ to liberty_fs_uploads/{screen_id}/
    3. The updated build will be live at the same public_url
    """

    def __init__(self) -> None:
        logger.info("Initializing AngularAppGenerator...")
        
        # Load config.json instead of .env
        config = _load_config()
        
        # Validate API key is available (gemini_service caches it for all calls)
        if not get_gemini_api_key():
            logger.error("Google API key is missing.")
            raise ValueError("Failed to retrieve Gemini API key from Secret Manager")
        
        # self.model = genai.GenerativeModel("gemini-3-pro-preview")
        # # self.model = genai.GenerativeModel("gemini-2.5-pro")
        # self.fix_model = genai.GenerativeModel("gemini-2.5-pro")

        self.model_name = "gemini-3.1-pro-preview"
        self.fix_model_name = "gemini-2.5-pro"
        self.analysis_model_name = "gemini-3.5-flash-lite"

        self.generation_config = {
            'temperature': 0.4,
            'top_p': 0.95,
            'top_k': 40,
        }
        self.fix_generation_config = {
            'temperature': 0.2,
            'top_p': 0.9,
            'top_k': 32,
        }

        # Retry and timeout settings
        self.max_fix_iterations = 5
        self.max_fix_prompt_retries = 5
        self.max_fix_iterations = 5
        self.max_fix_prompt_retries = 5
        self.max_error_files = 10
        self.max_fix_file_chars = 20000
        self.max_error_log_chars = 10000
        self.max_retries = 5  # NEW: Max retries for API calls
        self.default_timeout = 2100  # 35 minutes default
        self.fix_timeout = 1800 # 30 minutes 

        # Context strategy for error fixing
        self.use_full_context_for_fixes = True  # Set to False to use old selective approach
        
        # Resolve important paths
        services_dir = Path(__file__).parent
        backend_dir = services_dir.parent
        project_root = backend_dir.parent.resolve()
        
        self.base_project_path = project_root / "angular-base-project"
        self.generated_apps_root = project_root / "generated-apps"
        self.isolated_apps_root = project_root / "isolated-angular-apps"
        # self.deploy_root = project_root / "liberty_fs_uploads"
        #Read from config.json instead of .env
        liberty_root = config.get("LIBERTY_FS_ROOT")
        if liberty_root:
            self.deploy_root = Path(liberty_root).expanduser()
        else:
            self.deploy_root = project_root / "liberty_fs_uploads"

        self.deploy_base_url = config.get("LIBERTY_FS_BASE_URL", "https://liberty-fs.gibots.com:843")
        self.api_base_url = config.get("ai_agent_base_url") or ""
        self.api_prefix = (config.get("api_prefix") or "").strip("/")
        self.agent_base_path = config.get("ai_agent_base_path", "/aiqod-agent/agent")
        self.file_upload_path = config.get("fileUploadPath", "/data/publicfolder/liberty-fs/uploads")
        self.version_retention_count = config.get("version_retention_count", 5)
        
        logger.info("Loading prompt template...")
        self.prompt_template = self._create_system_prompt()
        
        # Ensure directories exist
        for path in [self.generated_apps_root, self.isolated_apps_root, self.deploy_root]:
            path.mkdir(parents=True, exist_ok=True)
        
        if not self.base_project_path.exists():
            logger.warning(f"Base project not found at {self.base_project_path}")
        
        logger.info("AngularAppGenerator initialized successfully.")

    # Hard wall-clock cap (seconds) for the entire sync generation pipeline
    # (LLM codegen + npm install + ng build + Liberty FS upload).  This acts as
    # a last-resort guard: if any sub-operation hangs despite its own timeout
    # (e.g. a new code path without an explicit cap) the event loop is still
    # unblocked and the HTTP request eventually gets a response.
    # The underlying executor thread continues running until it finishes or the
    # process exits, but the caller receives a RuntimeError rather than waiting
    # indefinitely.
    _GENERATE_APP_TOTAL_TIMEOUT_SECONDS: int = 7200  # 2 hours absolute max

    async def generate_app(
        self,
        *,
        user_prompt: str,
        project_id: str,
        screen_id: str,
        ir_schema: Optional[Dict[str, Any]] = None,
        backend_metadata: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable[[str, str, Any], Any]] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        assets_data: Optional[Dict[str, bytes]] = None,
        is_figma_mode: bool = False,
        version: Optional[str] = None,
        is_update: bool = False,
    ) -> Dict[str, Any]:
        """
        Run the full Angular generation flow in a worker thread.
        Includes progress reporting via callback.
        """
        logger.info(f"Starting NEW Angular app generation for screen_id={screen_id}")

        loop = asyncio.get_running_loop()
        def sync_progress_callback(level: str, message: str, details: Any = None):
            if progress_callback:
                asyncio.run_coroutine_threadsafe(progress_callback(level, message, details), loop)

        logger.info("Scheduling sync Angular generation in thread pool.")

        codegen_cached_content = await _get_angular_codegen_cached_content(
            self.model_name,
            system_text=self._build_angular_codegen_cached_system_text(
                is_figma_mode=is_figma_mode,
            ),
            is_figma_mode=is_figma_mode,
        )

        try:
            return await asyncio.wait_for(
                run_sync(
                    self._generate_app_sync,
                    user_prompt,
                    project_id,
                    screen_id,
                    ir_schema,
                    backend_metadata,
                    sync_progress_callback,
                    org_id,
                    subscriber_id,
                    user_id,
                    assets_data,
                    is_figma_mode,
                    version,
                    is_update,
                    False,
                    codegen_cached_content,
                ),
                timeout=self._GENERATE_APP_TOTAL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            logger.error(
                "generate_app timed out after %ds for screen_id=%s — "
                "the executor thread may still be running in the background",
                self._GENERATE_APP_TOTAL_TIMEOUT_SECONDS,
                screen_id,
            )
            raise RuntimeError(
                f"Angular app generation timed out after "
                f"{self._GENERATE_APP_TOTAL_TIMEOUT_SECONDS}s for screen_id={screen_id}"
            ) from exc

    def _generate_app_sync(
        self,
        user_prompt: str,
        project_id: str,
        screen_id: str,
        ir_schema: Optional[Dict[str, Any]] = None,
        backend_metadata: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable] = None, 
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        assets_data: Optional[Dict[str, bytes]] = None,
        is_figma_mode: bool = False,
        version: Optional[str] = None,
        is_update: bool = False,
        full_regeneration_attempted: bool = False,
        codegen_cached_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            if progress_callback:
                progress_callback("INFO", "Initializing generation parameters...")
            
            # Ensure we have a stable version identifier for this generation
            if not version:
                version = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            
            logger.info("Building final prompt for LLM...")
            if progress_callback:
                progress_callback("INFO", "Generating application structure with AI...")
            
            final_prompt = self._build_prompt(
                user_prompt,
                ir_schema=ir_schema,
                backend_metadata=backend_metadata,
                is_figma_mode=is_figma_mode,
            )
            cached_user_prompt = self._build_angular_codegen_dynamic_prompt_parts(
                user_prompt,
                ir_schema=ir_schema,
                backend_metadata=backend_metadata,
            )

            logger.info("Invoking Gemini model...")
            raw_response, finish_reason, safety_ratings, usage_info = self._invoke_model(
                final_prompt,
                model_name=self.model_name,
                generation_config=self.generation_config,
                project_id=project_id,
                screen_id=screen_id,
                operation="generate-angular-app",
                sub_operation="angular-codegen",
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
                cached_content=codegen_cached_content,
                cached_user_prompt=cached_user_prompt,
            )
            
            logger.info("Parsing generated files from model response...")
            if progress_callback:
                progress_callback("INFO", "Parsing generated code...")
                
            files, metadata = self._parse_generated_files(raw_response)
            logger.info(f"Parsed {len(files)} files.")
            self._log_generated_bundle(files, metadata)
            
            logger.info("Creating workspace and copying base project...")
            if progress_callback:
                progress_callback("INFO", "Setting up project workspace...")
                
            # Each version gets its own workspace directory under generated-apps
            workspace_dir = self._create_workspace(
                project_id=project_id,
                screen_id=screen_id,
                version=version,
            )
            
            logger.info("Copying base project to workspace...")
            self._copy_base_project(workspace_dir)
            
            logger.info("Merging generated files with base project...")
            self._merge_generated_files(workspace_dir, files)
            
            if assets_data:
                logger.info(f"Writing {len(assets_data)} asset files to workspace...")
                assets_dir = os.path.join(workspace_dir, "src", "assets")
                os.makedirs(assets_dir, exist_ok=True)
                for filename, content in assets_data.items():
                    asset_path = os.path.join(assets_dir, filename)
                    with open(asset_path, "wb") as f:
                        f.write(content)
            
            self._post_process_components(workspace_dir)
            
            logger.info("Injecting screen runtime and workflow support...")
            self._inject_screen_runtime(workspace_dir, screen_id, ir_schema)
            
            logger.info("Updating index.html with component selector...")
            self._update_index_html(workspace_dir, ir_schema)
            
            logger.info("Updating project files from metadata...")
            self._apply_metadata(workspace_dir, metadata)
            
            logger.info("Writing environment.ts from backend config...")
            self._write_environment_ts(workspace_dir, project_id=project_id)

            # T8 — FE↔BE contract audit + patch/regen before npm build
            # Runs entirely inside the workspace (no npm install yet), so patches
            # are cheap: just file writes + re-merge.  Isolated dir is created
            # afterwards, so it always gets the corrected workspace state.
            if backend_metadata:
                self._ensure_fe_be_contract_sync(
                    workspace_dir,
                    backend_metadata,
                    ir_schema,
                    user_prompt=user_prompt,
                    project_id=project_id,
                    screen_id=screen_id,
                    version=version,
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                    user_id=user_id,
                    progress_callback=progress_callback,
                )

            logger.info("Preparing isolated build directory...")
            # Each build gets its own isolated folder; name mirrors workspace version.
            isolated_dir = self._prepare_isolated_directory(
                workspace_dir,
                project_id,
                screen_id,
                version=version,
            )
            
            logger.info("Installing dependencies in isolated directory...")
            if progress_callback:
                progress_callback("INFO", "Installing NPM dependencies (this may take a minute)...")
                
            install_log = self._install_dependencies(isolated_dir)
            
            logger.info("Validating generated code for missing dependencies...")
            validation_result = self._validate_generated_code(isolated_dir)
            
            if not validation_result["valid"]:
                logger.warning(f"Validation failed: {validation_result['errors']}")
                if progress_callback:
                    progress_callback("WARNING", "Missing dependencies detected. Auto-fixing...")
                    
                fix_attempted = self._fix_missing_dependencies(isolated_dir, validation_result)
                if fix_attempted:
                    validation_result = self._validate_generated_code(isolated_dir)
            
            logger.info("Building Angular application with automated recovery...")
            
            build_log, dist_dir, recovery_attempts = self._build_with_recovery(
                workspace_dir=workspace_dir,
                isolated_dir=isolated_dir,
                user_prompt=user_prompt,
                project_id=project_id,
                screen_id=screen_id,
                ir_schema=ir_schema, 
                backend_metadata=backend_metadata,
                progress_callback=progress_callback 
            )
            
            logger.info("Uploading build artifacts to Liberty FS directory...")
            if progress_callback:
                progress_callback("INFO", "Deploying to preview server...")
                
            deploy_dir = self._upload_build(dist_dir, screen_id, version=version)
            # Public URL should point directly to the concrete version directory (screen_id-v{version})
            public_url = self._build_public_url(screen_id, version)
            
            self._cleanup_old_versions(project_id, screen_id)
            
            if progress_callback:
                progress_callback("INFO", "Generation complete!")
            
            return {
                "success": True,
                "version": version,
                "workspace_directory": workspace_dir,
                "isolated_directory": isolated_dir,
                "dist_directory": dist_dir,
                "deploy_directory": deploy_dir,
                "public_url": public_url,
                "file_count": len(files),
                "files": [{"path": f.path, "absolute_path": os.path.join(workspace_dir, f.path)} for f in files],
                "model": "gemini-3.5-flash-lite",
                "finish_reason": finish_reason,
                "safety_ratings": safety_ratings,
                "token_usage": usage_info,
                "metadata": metadata,
                "install_log": install_log,
                "build_log": build_log,
                "recovery_attempts": recovery_attempts,
            }
        except AngularBuildError as e:
            if full_regeneration_attempted:
                logger.error(
                    "Angular build failed after automated recovery and one full regeneration.",
                )
                if progress_callback:
                    progress_callback(
                        "ERROR",
                        "Build failed after full regeneration.",
                        {"recovery_attempts": len(getattr(e, "attempts", []) or [])},
                    )
                raise

            # Last-resort fallback: rebuild from scratch exactly once.
            logger.error(
                "Angular build failed after automated recovery attempts. "
                "Attempting full regeneration with a fresh workspace (once).",
            )
            if progress_callback:
                progress_callback(
                    "RETRY",
                    "Automated recovery failed. Regenerating Angular app from scratch...",
                    {"recovery_attempts": len(getattr(e, "attempts", []) or [])},
                )

            return self._generate_app_sync(
                user_prompt,
                project_id,
                screen_id,
                ir_schema,
                backend_metadata,
                progress_callback,
                org_id,
                subscriber_id,
                user_id,
                assets_data,
                is_figma_mode,
                version,
                is_update,
                full_regeneration_attempted=True,
                codegen_cached_content=codegen_cached_content,
            )

    def check_component_changes_require_backend(
        self,
        old_ir_schema: Dict[str, Any],
        new_ir_schema: Dict[str, Any]
    ) -> bool:
        """
        Check if component changes between old and new IR schemas require backend updates.
        
        Component add/remove operations are Category B because they may require:
        - New data models (dataModels)
        - New data sources (dataSources) 
        - New backend API functions
        - Removal of associated backend functions
        
        Args:
            old_ir_schema: Previous IR schema
            new_ir_schema: Updated IR schema
            
        Returns:
            True if component changes require backend updates
        """
        old_components = {comp.get("id"): comp for comp in old_ir_schema.get("components", [])}
        new_components = {comp.get("id"): comp for comp in new_ir_schema.get("components", [])}
        
        old_component_ids = set(old_components.keys())
        new_component_ids = set(new_components.keys())
        
        # Check for added or removed components
        added_components = new_component_ids - old_component_ids
        removed_components = old_component_ids - new_component_ids
        
        if added_components or removed_components:
            logger.info(
                "Component changes detected - added: %s, removed: %s (Category B - requires backend update)",
                list(added_components),
                list(removed_components)
            )
            return True
        
        # Also check if dataModels or dataSources changed (which would indicate backend needs)
        old_data_models = set(old_ir_schema.get("dataModels", {}).keys())
        new_data_models = set(new_ir_schema.get("dataModels", {}).keys())
        
        old_data_sources = {ds.get("id") for ds in old_ir_schema.get("dataSources", [])}
        new_data_sources = {ds.get("id") for ds in new_ir_schema.get("dataSources", [])}
        
        if (old_data_models != new_data_models or 
            old_data_sources != new_data_sources):
            logger.info(
                "Data model or data source changes detected (Category B - requires backend update)"
            )
            return True
        
        return False

    def analyze_update_scope(
        self,
        user_prompt: str,
        ir_schema: Dict[str, Any],
        current_code: Dict[str, str],
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        impact_report: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Analyze which files and components are affected by the update request.
        
        Args:
            user_prompt: User's update request
            ir_schema: Current IR schema
            current_code: Dictionary of current code files
            project_id: Project identifier for tracking
            screen_id: Screen identifier for tracking
            version: Version identifier for tracking
        
        Returns:
            {
                "affected_files": [
                    {"path": "src/app/...", "reason": "...", "change_type": "style|text|layout|component-add|component-remove"}
                ],
                "affected_components": [
                    {"id": "component_id", "reason": "..."}
                ],
                "requires_backend_update": bool  # True if component-add/component-remove detected
            }
        """
        logger.info("Analyzing update scope for selective regeneration...")

        # Build IR summary for context
        ir_summary = json.dumps(
            {
                "screen": ir_schema.get("screen", {}),
                "components": [
                    {"id": comp.get("id"), "type": comp.get("type")}
                    for comp in ir_schema.get("components", [])
                ],
                "theme": ir_schema.get("theme", {}),
                "antiPatterns": ir_schema.get("antiPatterns", []),
            },
            indent=2,
        )

        # Include the current Angular project codebase (truncated per file)
        # so the model has full structural context while deciding scope.
        file_blocks: List[str] = []
        max_files = 80
        max_chars_per_file = 10000

        for idx, (path, content) in enumerate(current_code.items()):
            if idx >= max_files:
                break
            truncated = content[:max_chars_per_file]
            file_blocks.append(f"<file path=\"{path}\">\n{truncated}\n</file>")

        current_code_block = "\n".join(file_blocks)

        # Include impact report context if available
        impact_context = ""
        if impact_report:
            affected_nodes = impact_report.get("all_affected_nodes", [])
            if affected_nodes:
                impact_lines = ["Phase 1 Impact Analysis already identified affected files:"]
                for node in affected_nodes:
                    node_id = node.get("node_id", "unknown")
                    changes = node.get("changes", [])
                    for change in changes:
                        if change.get("type") == "file_change":
                            path = change.get("path", "")
                            reason = change.get("reason", "")
                            change_type = change.get("change_type", "")
                            impact_lines.append(f"  - {path} ({change_type}): {reason}")
                impact_context = "\n" + "\n".join(impact_lines) + "\n"

        analysis_prompt = f"""You are analyzing an existing Angular application update request to determine
which files need to be regenerated.

Current Angular Project (selected files):
<current_codebase>
{current_code_block}
</current_codebase>

User Request:
\"\"\"{user_prompt}\"\"\"

Updated IR Schema summary (relevant parts):
{ir_summary}
{impact_context}
Task: Analyze the user request and determine:
1. Which exact files are affected by this change
2. For each file, what type of change is needed (style, text, layout, component-add, component-remove, theme)
3. Which IR component IDs (if any) are affected
4. Whether this change requires backend updates (true if component-add/component-remove is detected)

IMPORTANT: Adding or removing components (change_type: "component-add" or "component-remove") may require 
backend model and function updates because:
- New components might need new data models (dataModels in IR schema)
- New components might need new data sources (dataSources in IR schema) 
- New components might require new backend API functions
- Removing components might require removing associated backend functions

Respond with ONLY a JSON object in this exact format:
{{
  "affected_files": [
    {{"path": "src/app/app.component.ts", "reason": "Button color change affects root shell", "change_type": "style"}},
    {{"path": "src/styles.css", "reason": "Global theme variables need new colors", "change_type": "theme"}}
  ],
  "affected_components": [
    {{"id": "button_1", "reason": "Color change requested"}}
  ],
  "requires_backend_update": false
}}

Rules:
- Use the actual file paths from <current_codebase>.
- Change types must be one of: "style", "text", "layout", "component-add", "component-remove", "theme".
- Set "requires_backend_update" to true if ANY affected file has change_type "component-add" or "component-remove".
- Be conservative: only include files that are clearly affected by the request.
- If the request is purely theme-related, prefer global style files (e.g. src/styles.css) and any component-level styles that reference theme colors.
- If the user request explicitly asks to ADD A NEW COMPONENT (e.g., "add a new chart", "add a new card component", "add a new table", "create a new section"), classify as "component-add" and set requires_backend_update to true. These require adding new IR components and potentially backend updates.
- If the user request explicitly asks to REMOVE A COMPONENT (e.g., "remove the chart", "delete the card", "remove the table"), classify as "component-remove" and set requires_backend_update to true.
- For requests about MODIFYING EXISTING COMPONENTS (e.g., "add a button to the popup", "add one more number input", "add another field to the form", "change the button text", "update the label"), classify based on the specific change type (style, text, layout) - do NOT use "component-add"/"component-remove" for modifications to existing components.
- **NAVIGATION CHANGES**: If the request involves adding navigation (e.g., "add back button", "add next button", "navigate to screen", "go to screen"), the following files are typically affected:
  - `src/app/app.component.ts` - to add/update navigation state management (currentScreen variable, onNavigate handler)
  - `src/app/app.component.html` - to add/update conditional screen rendering (*ngIf)
  - The target screen component (e.g., `src/app/screen-two/screen-two.component.html`) - to add the navigation button
  - Classify new buttons as "component-add", navigation state changes as "layout"
- Be conservative: only include files that are clearly affected by the request. Prefer fewer files over more.
- If the request is purely theme-related, prefer global style files (e.g. src/styles.css) and any component-level styles that reference theme colors.
"""
        
        try:
            response_text, _, _, _ = self._invoke_model(
                analysis_prompt, 
                model_name=self.analysis_model_name,
                project_id=project_id,
                screen_id=screen_id,
                version=version,
                operation="analyze-angular-update",
                sub_operation="update-scope-analysis",
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
            )

            # Extract JSON from response
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                affected = result.get("affected_files", []) or []
                
                # Check if component-add/component-remove is present (Category B indicator)
                requires_backend = result.get("requires_backend_update", False)
                if not requires_backend:
                    # Auto-detect from change types
                    change_types = [f.get("change_type") for f in affected]
                    if "component-add" in change_types or "component-remove" in change_types:
                        requires_backend = True
                        result["requires_backend_update"] = True
                        logger.info("Component add/remove detected - marking as requiring backend update (Category B)")
                
                logger.info(
                    "Analysis complete: %d files affected: %s, requires_backend_update: %s",
                    len(affected),
                    [f.get("path") for f in affected],
                    requires_backend,
                )
                return result
            else:
                logger.warning("⚠️ Could not parse analysis response, defaulting to all files")
                return {
                    "affected_files": [{"path": path, "reason": "Fallback: all files", "change_type": "unknown"} 
                                     for path in current_code.keys()],
                    "affected_components": [],
                    "requires_backend_update": False
                }
        except Exception as e:
            logger.error(f"Error in analyze_update_scope: {e}")
            # Fallback: return all files
            return {
                "affected_files": [{"path": path, "reason": "Error in analysis", "change_type": "unknown"} 
                                 for path in current_code.keys()],
                "affected_components": [],
                "requires_backend_update": False
            }

    def regenerate_files_for_update(
        self,
        user_prompt: str,
        updated_ir_schema: Dict[str, Any],
        affected_files: List[Dict[str, str]],
        workspace_dir: str,
        backend_metadata: Optional[Dict[str, Any]] = None,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Tuple[List[GeneratedFile], Dict[str, Any]]:
        """
        Regenerate only the affected files for an update.

        Args:
            user_prompt: User's update request
            updated_ir_schema: Updated IR schema
            affected_files: List of affected file info from analyze_update_scope
            workspace_dir: Path to existing workspace
            backend_metadata: Backend metadata if needed
            project_id: Project identifier for tracking
            screen_id: Screen identifier for tracking
            version: Version identifier for tracking

        Returns:
            (generated_files, metadata)
        """
        # Early return if no files affected - nothing to regenerate
        logger.debug(f"[angular_generator] regenerate_files_for_update: affected_files={len(affected_files)}, workspace_dir={workspace_dir}")
        if not affected_files:
            logger.info("No affected files to regenerate, skipping LLM call")
            logger.debug("[angular_generator] regenerate_files_for_update: returning skipped=True")
            return [], {"token_usage": {}, "skipped": True}

        logger.info(f"Regenerating {len(affected_files)} files for update...")
        
        # Read current content of affected files
        current_code = {}
        for file_info in affected_files:
            file_path = file_info.get("path")
            full_path = os.path.join(workspace_dir, file_path)
            if os.path.exists(full_path):
                with open(full_path, "r", encoding="utf-8") as f:
                    current_code[file_path] = f.read()
        
        # Build focused prompt for selective regeneration
        affected_paths = [f.get("path") for f in affected_files]
        affected_summary = "\n".join([
            f"- {f.get('path')}: {f.get('reason')} ({f.get('change_type')})"
            for f in affected_files
        ])
        
        # Get relevant IR schema parts
        affected_component_ids = [f.get("id") for f in affected_files if "id" in f]
        relevant_components = [
            comp for comp in updated_ir_schema.get("components", [])
            if comp.get("id") in affected_component_ids or not affected_component_ids
        ]
        
        ir_snippet = json.dumps({
            "screen": updated_ir_schema.get("screen", {}),
            "theme": updated_ir_schema.get("theme", {}),
            "antiPatterns": updated_ir_schema.get("antiPatterns", []),
            "components": relevant_components, 
        }, indent=2)
        
        regeneration_prompt = f"""You are updating an existing Angular application. Only update the files listed below.

User Request: "{user_prompt}"

Updated IR Schema (relevant parts):
{ir_snippet}

Affected Files to Regenerate:
{affected_summary}

Current File Contents:
{chr(10).join([f'=== {path} ==={chr(10)}{content}' for path, content in list(current_code.items())[:15]])}

CRITICAL - PRESERVE EXISTING CODE:
- DO NOT change layout, structure, or styling unless explicitly requested
- DO NOT reformat, reorganize, or "improve" existing code
- DO NOT change Tailwind classes, grid layouts, flex directions, spacing unless user mentioned
- DO NOT change div nesting, HTML structure, or component hierarchy unless user mentioned
- DO NOT change button sizes, widths, paddings unless user mentioned
- DO NOT move elements, change their order, or adjust visual hierarchy unless user mentioned
- ONLY modify what the user explicitly requested
- Keep ALL existing classes, structure, spacing exactly as-is
- Each file must be COMPLETE (not partial) but preserve all unchanged parts exactly.
- Treat the current code as sacred - minimal surgical changes only

CRITICAL - DO NOT ADD NEW ELEMENTS:
- DO NOT add new buttons, inputs, components, or HTML elements unless the user explicitly requested them
- DO NOT add new CSS classes, styles, or Tailwind utilities unless user mentioned styling changes
- DO NOT add new imports, dependencies, or TypeScript code unless required by the explicit change
- If the user says "add a button", add ONLY that button - nothing else
- If the user says "change the label", change ONLY that label - nothing else

CRITICAL INSTRUCTIONS:
1. Return ONLY the files listed in "Affected Files to Regenerate"
2. Unmentioned files must be considered unchanged - DO NOT regenerate them
3. Preserve the structure and logic of existing files, only modify what the user requested
4. Keep ALL Tailwind classes unchanged unless user mentions styling
5. Keep ALL grid layouts, div structure, styling, spacing, HTML structure, nesting, hierarchy, button sizes same if not explicitily instructed by user.
6. Ensure regenerated files are compatible with the rest of the application
7. Use the updated IR schema values for affected components

Return files in <files>...</files> format with <file path="...">...</file> for each affected file."""
        
        try:
            response_text, _, _, usage_info = self._invoke_model(
                regeneration_prompt,
                project_id=project_id,
                screen_id=screen_id,
                version=version,
                operation="generate-angular-app",
                sub_operation="update-angular-files",
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
            )
            files, metadata = self._parse_generated_files(response_text)
            
            # Add usage info to metadata
            metadata["token_usage"] = usage_info
            
            logger.info(f"Regenerated {len(files)} files")
            return files, metadata
        except Exception as e:
            logger.error(f"Error in regenerate_files_for_update: {e}")
            raise

    async def update_app(
        self,
        *,
        user_prompt: str,
        project_id: str,
        screen_id: str,
        updated_ir_schema: Dict[str, Any],
        affected_files: List[Dict[str, str]],
        backend_metadata: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable[[str, str, Any], Any]] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        version: Optional[str] = None,
        existing_workspace_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Orchestrate selective Angular regeneration for updates.
        Includes progress reporting via callback.
        version: New version string for this update (creates new workspace and build dirs).
        existing_workspace_dir: Use this workspace instead of deriving from screen_id (avoids screen_id mismatch).
        """
        # Create sync wrapper
        loop = asyncio.get_running_loop()
        def sync_progress_callback(level: str, message: str, details: Any = None):
            if progress_callback:
                asyncio.run_coroutine_threadsafe(progress_callback(level, message, details), loop)

        # PYTHON 3.8 COMPATIBLE THREAD EXECUTION
        return await run_sync(
            self._update_app_sync,
            user_prompt,
            project_id,
            screen_id,
            updated_ir_schema,
            affected_files,
            backend_metadata,
            sync_progress_callback,
            org_id,
            subscriber_id,
            user_id,
            version,
            existing_workspace_dir,
        )

    def _update_app_sync(
        self,
        user_prompt: str,
        project_id: str,
        screen_id: str,
        updated_ir_schema: Dict[str, Any],
        affected_files: List[Dict[str, str]],
        backend_metadata: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        version: Optional[str] = None,
        existing_workspace_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Synchronous version of update_app. When version is set, creates a new versioned workspace and build dir."""
        logger.info(f"Starting selective Angular update for {screen_id}...")
        version = version or datetime.utcnow().strftime("%Y%m%d%H%M%S")

        if progress_callback:
            progress_callback("INFO", "Locating existing project workspace...")

        # Use provided workspace_dir (from screen doc) to avoid screen_id mismatch
        # screen_id changes per update for versioning, but workspace uses old screen_id
        old_workspace_dir = existing_workspace_dir or self._get_workspace_path(project_id, screen_id)
        logger.info(f"⚠️ old_workspace_dir: {old_workspace_dir}")
        if not old_workspace_dir or not Path(old_workspace_dir).exists():
            logger.warning(f"⚠️ Workspace not found, falling back to full generation")
            if progress_callback:
                progress_callback("WARNING", "Workspace not found. Falling back to full regeneration...")
            return self._generate_app_sync(
                user_prompt,
                project_id,
                screen_id,
                ir_schema=updated_ir_schema,
                backend_metadata=backend_metadata,
                progress_callback=progress_callback,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
            )

        # Early return if no files affected - no Angular update needed
        logger.debug(f"[angular_generator] update_app: affected_files={len(affected_files)}, screen_id={screen_id}, version={version}")
        if not affected_files:
            logger.info("No affected files for Angular update, skipping regeneration")
            logger.debug(f"[angular_generator] update_app: returning skipped=True for screen_id={screen_id}")
            if progress_callback:
                progress_callback("INFO", "No file changes needed")
            return {
                "success": True,
                "skipped": True,
                "screen_id": screen_id,
                "version": version,
                "public_url": self._build_public_url(screen_id, version),
                "message": "No file changes needed - only contract/bindings updated"
            }

        # Create new versioned workspace and copy previous version into it (generated-apps and isolated get same version)
        if progress_callback:
            progress_callback("INFO", f"Creating new workspace for version {version}...")
        workspace_dir = self._create_workspace(project_id, screen_id, version=version, overwrite=True)
        # Copy previous workspace contents into the new workspace so we have a base to merge into
        shutil.copytree(str(old_workspace_dir), workspace_dir, dirs_exist_ok=True)
        logger.info(f"Copied previous workspace to {workspace_dir}")

        if progress_callback:
            progress_callback("INFO", f"Regenerating {len(affected_files)} affected files...")

        generated_files, metadata = self.regenerate_files_for_update(
            user_prompt,
            updated_ir_schema,
            affected_files,
            workspace_dir,
            backend_metadata,
            project_id=project_id,
            screen_id=screen_id,
            version=version,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
        )
        
        logger.info("Merging regenerated files into workspace...")
        if progress_callback:
            progress_callback("INFO", "Applying changes to codebase...")
            
        self._merge_generated_files(workspace_dir, generated_files)
        self._post_process_components(workspace_dir)
        self._inject_screen_runtime(workspace_dir, screen_id, updated_ir_schema)
        self._update_index_html(workspace_dir, updated_ir_schema)
        self._write_environment_ts(workspace_dir, project_id=project_id)
        
        isolated_dir = self._prepare_isolated_directory(
            workspace_dir,
            project_id,
            screen_id,
            version=version,
        )
        
        logger.info("Installing dependencies...")
        if progress_callback:
            progress_callback("INFO", "Updating dependencies...")
        install_log = self._install_dependencies(isolated_dir)
        
        logger.info("Validating generated code...")
        validation_result = self._validate_generated_code(isolated_dir)
        if not validation_result["valid"]:
            self._fix_missing_dependencies(isolated_dir, validation_result)
        
        logger.info("Building Angular application...")
        # Pass callback to build
        build_log, dist_dir, recovery_attempts = self._build_with_recovery(
            workspace_dir=workspace_dir,
            isolated_dir=isolated_dir,
            user_prompt=user_prompt,
            project_id=project_id,
            screen_id=screen_id,
            ir_schema=updated_ir_schema,
            backend_metadata=backend_metadata,
            progress_callback=progress_callback,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
        )
        
        logger.info("Uploading build artifacts...")
        if progress_callback:
            progress_callback("INFO", "Deploying updated screen...")
            
        deploy_dir = self._upload_build(dist_dir, screen_id, version=version)
        # Public URL should point directly to the concrete version directory (screen_id-v{version})
        public_url = self._build_public_url(screen_id, version)
        
        self._cleanup_old_versions(project_id, screen_id)
        
        if progress_callback:
            progress_callback("INFO", "Update complete!")

        return {
            "success": True,
            "version": version,
            "workspace_directory": workspace_dir,
            "isolated_directory": isolated_dir,
            "dist_directory": dist_dir,
            "deploy_directory": deploy_dir,
            "public_url": public_url,
            "file_count": len(generated_files),
            "changed_files": [f.get("path") for f in affected_files],
            "model": "gemini-3.5-flash-lite",
            "token_usage": metadata.get("token_usage", {}),
            "metadata": metadata,
            "install_log": install_log,
            "build_log": build_log,
            "recovery_attempts": recovery_attempts,
        }

    def _load_prompt_template(self) -> str:
        """Load the Angular LLM instruction block from prompt_angular.py."""
        services_dir = os.path.dirname(__file__)
        backend_dir = os.path.dirname(services_dir)
        project_root = os.path.abspath(os.path.join(backend_dir, ".."))
        prompt_path = os.path.join(project_root, "prompt_angular.py")

        if not os.path.exists(prompt_path):
            raise FileNotFoundError(f"prompt_angular.py not found at {prompt_path}")

        with open(prompt_path, "r", encoding="utf-8") as file:
            return file.read().strip()

    def _generate_screen_runtime(self, screen_id: str) -> str:
        """Generate the screen-runtime.ts file that provides workflow orchestration API."""
        safe_screen_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", screen_id)
        return f'''// screen-runtime.ts - Workflow orchestration runtime for DOM-based screen loading
// This file is deterministic (not LLM generated) to keep runtime stable across screens.
export {{}};

declare global {{
  interface Window {{
    __AIQOD_SCREEN__?: {{
      runtimeVersion: string;
      screenId: string | null;
      input: any;
      listeners: Array<(data: any) => void>;
      _output: any;
      _resolve?: (output: any) => void;
      setScreenId: (id: string) => void;
      setInput: (data: any) => void;
      onInput: (cb: (data: any) => void) => void;
      complete: (output: any) => void;
      getOutput: () => any;
      waitForCompletion: () => Promise<any>;
    }};
    __SCREEN_REF__?: any;
  }}
}}

(function() {{
  const runtimeVersion = "1.0.0";
  const w = window as any;
  if (!w.__AIQOD_SCREEN__) {{
    w.__AIQOD_SCREEN__ = {{
      runtimeVersion,
      screenId: null,
      input: {{}},
      listeners: [],
      _output: null,
      _resolve: null,

      setScreenId(id: string) {{
        this.screenId = id;
      }},

      setInput(data: any) {{
        try {{
          this.input = JSON.parse(JSON.stringify(data || {{}}));
        }} catch(e) {{
          this.input = data || {{}};
        }}
        this.listeners.forEach((fn: any) => {{
          try {{
            fn(this.input);
          }} catch(err) {{
            console.error("Error in input listener:", err);
          }}
        }});
      }},

      onInput(cb: (data: any) => void) {{
        this.listeners.push(cb);
        if (Object.keys(this.input || {{}}).length > 0) {{
          try {{
            cb(this.input);
          }} catch(err) {{
            console.error("Error calling input callback:", err);
          }}
        }}
      }},

      complete(output: any) {{
        this._output = output;
        if (this._resolve) {{
          this._resolve(output);
          this._resolve = null;
        }}
      }},

      getOutput() {{
        return this._output || null;
      }},

      // Orchestrator waits on this promise
      waitForCompletion(): Promise<any> {{
        return new Promise((res) => {{
          this._resolve = res;
        }});
      }}
    }};
  }}
}})();
'''

    def _create_system_prompt(self) -> str:
        file_upload_path = self.file_upload_path
        deploy_base_url = self.deploy_base_url
        
        prompt = """

You are an expert Angular 17+ engineer. A fully configured boilerplate already exists at `angular-base-project/` with:
- Angular CLI configuration (`angular.json`), TypeScript configs, `package.json`
- `src/main.ts`, `src/index.html`, `src/styles.css` (Tailwind already wired)
- `src/app/app.config.ts` and placeholder `app.component.*`
- Pre-installed deps: `@angular/core`, `@angular/common`, `@angular/platform-browser`, `@angular/forms`, `@angular/common/http`, `rxjs`, `tailwindcss`, `aos` (with `@types/aos`)
- **CRITICAL**: If you use ANY external library (e.g., `aos`, `chart.js`, `date-fns`), you MUST:
  1. List it in `<metadata>.dependencies` with version
  2. If it needs TypeScript types, also add `@types/[package-name]` to devDependencies in metadata
  3. Common libraries already in boilerplate: `aos` (with `@types/aos`) - you can use these without adding to metadata

**Your job:** Generate only the application-specific files required to fulfill the user request (components, services, models, directives, pipes, guards, updated templates/styles). Never regenerate the boilerplate files.

## 🚫 ROUTING DISABLED - CRITICAL
This project does NOT use Angular routing:
- DO NOT generate src/app/app.routes.ts
- DO NOT import Router, Routes, RouterModule, RouterLink, ActivatedRoute
- DO NOT use <router-outlet> in templates
- DO NOT add provideRouter to app.config.ts

✅ USE DIRECT COMPONENT COMPOSITION:
- Import components directly in app.component.ts imports array
- Use components directly in app.component.html template
- Use @if or *ngIf for conditional component display
- Example: <login-form *ngIf="showLogin"></login-form>
- Pass data via @Input() properties and @Output() for events

## 🧭 IN-APP NAVIGATION (MULTI-SCREEN APPS) - CRITICAL
When the IR schema contains components with `actions` that have `do: "navigate"`, the app has multiple internal screens/views that must be switched programmatically. Since Angular routing is DISABLED, implement navigation using direct component composition:

**In app.component.ts:**
```typescript
// State variable to track current screen/view
currentScreen = 'screen1'; // Default to first screen

// Navigation handler called by child components via @Output()
onNavigate(targetScreen: string) {
  this.currentScreen = targetScreen;
}
```

**In app.component.html:**
```html
<!-- Each screen component is conditionally rendered -->
<screen-one *ngIf="currentScreen === 'screen1'" (navigate)="onNavigate($event)"></screen-one>
<screen-two *ngIf="currentScreen === 'screen2'" (navigate)="onNavigate($event)"></screen-two>
<screen-three *ngIf="currentScreen === 'screen3'" (navigate)="onNavigate($event)"></screen-three>
```

**In child screen components (e.g., screen-one.component.ts):**
```typescript
@Output() navigate = new EventEmitter<string>();

goToScreen2() {
  this.navigate.emit('screen2');
}

goBack() {
  this.navigate.emit('screen1');
}
```

**In child screen templates (e.g., screen-one.component.html):**
```html
<button (click)="goToScreen2()">Next</button>
```

**IR Schema Mapping:**
- The IR schema's `components` array contains ALL components for ALL screens
- Each screen is a separate component (e.g., `screen-one`, `screen-two`)
- Components with `actions[].flow[].do: "navigate"` indicate navigation triggers
- The `params` of the navigate action should contain the target screen identifier (e.g., `{ "targetScreen": "screen1" }`)
- Generate a separate Angular component for each logical screen
- The app.component.ts composes all screens and manages the `currentScreen` state
- Use the IR schema's `screen.selector` for the root app component selector

## 🔄 WORKFLOW ORCHESTRATION - CRITICAL (DOM-Based CDN Loading)
This Angular app will be loaded dynamically in a workflow orchestrator using DOM-based script injection.
Every generated app MUST implement the workflow runtime contract:

### 1. Screen Runtime (Already Provided)
A `screen-runtime.ts` file is automatically included that provides:
- `window.__AIQOD_SCREEN__.setInput(data)` - Receives inputs from orchestrator
- `window.__AIQOD_SCREEN__.onInput(cb)` - Subscribe to input changes
- `window.__AIQOD_SCREEN__.complete(output)` - Send output to orchestrator
- `window.__AIQOD_SCREEN__.getOutput()` - Get current output
- `window.__AIQOD_SCREEN__.setScreenId(id)` - Set screen identifier

### 2. Bootstrap / main.ts (DO NOT Generate)
`src/main.ts` is owned by the build system (standalone `bootstrapApplication`, dev auto-bootstrap, orchestrator `(window as any)['bootstrap_<screen_id>']`). **Do not include `src/main.ts` in your output.**

### 3. Input Parameter Binding (MUST Implement)
If the IR schema contains `inputParameters`, you MUST bind them in your components:

**In component ngOnInit():**
```typescript
ngOnInit() {{
  // Subscribe to workflow inputs
  if (window.__AIQOD_SCREEN__) {{
    window.__AIQOD_SCREEN__.onInput((inputData: any) => {{
      // Map inputParameters to form/component state
      // Example: if inputParameters has "employee" object
      if (inputData.employee) {{
        this.form.patchValue({{
          name: inputData.employee.name,
          email: inputData.employee.email,
          // ... map all fields from inputParameters
        }});
      }}
      
      // Or bind to component properties
      if (inputData.someParam) {{
        this.someProperty = inputData.someParam;
      }}
    }});
  }}
}}
```

**Input Binding Rules:**
- Check IR schema `inputParameters` array to see what inputs are expected
- Map each input parameter to the appropriate form control or component property
- Use defensive coding: check if input exists before binding
- Support nested objects (e.g., `inputData.employee.name`)

### 4. Output Parameter Generation (MUST Implement)
When user clicks "Next", "Submit", or completes the screen, you MUST call `complete()`:

**In submit/next handler:**
```typescript
onNext() {{
  if (this.form.invalid) {{
    this.form.markAllAsTouched();
    return;
  }}
  
  // Build output matching outputParameters from IR schema
  const output = {{
    // Map to outputParameters structure from IR schema
    // Example: if outputParameters has "employee" object
    employee: {{
      name: this.form.value.name,
      email: this.form.value.email,
      // ... include all fields defined in outputParameters
    }}
  }};
  
  // Send output to orchestrator
  if (window.__AIQOD_SCREEN__) {{
    window.__AIQOD_SCREEN__.complete(output);
  }}
}}

onSubmit() {{
  // Similar pattern for final submission
  const output = {{
    finalSubmission: {{
      // ... structure matches outputParameters
    }}
  }};
  
  if (window.__AIQOD_SCREEN__) {{
    window.__AIQOD_SCREEN__.complete(output);
  }}
}}
```

**Output Rules:**
- Check IR schema `outputParameters` array to see what outputs are expected
- Structure the output object to match the outputParameters definition
- Always call `complete()` when user finishes the screen (Next/Submit button)
- Include all required fields from outputParameters
- **Result view before complete (CRITICAL):** If the screen must *display* a
  computed result (score, confirmation summary, success panel) after an API
  call, do NOT call `complete()` in that same submit handler. Show the result
  UI first; call `complete(output)` only from a separate Continue/Done/Finish
  button. Calling `complete()` immediately unloads the screen in the workflow
  host so the user never sees the result.

### 5. Component Selector & DOM Container
- The root component selector MUST match `screen.selector` from IR schema
- The app will be bootstrapped into a container (orchestrator provides `<div id="screen-root"></div>`).
  The orchestrator sets `#screen-root` to `width: 100%; height: 100vh` so your component
  receives full viewport dimensions.
- Design as a FULL-PAGE APPLICATION, not as a widget. Your root component should fill its
  container completely using `width: 100%; min-height: 100vh` on `:host`.
- Portability means the selector name is flexible — it does NOT mean the layout should be
  content-sized or self-contained. Always claim the full available space.

## Output Contract
1. Wrap code in `<files>...</files>` with one `<file path="...">...</file>` per file. Paths are relative to the Angular project root.
2. Always include and fully implement:
   - `src/app/app.component.ts` (root shell that composes the experience using direct component composition)
   - `src/app/app.component.html`
   - `src/app/app.component.css` (or `.scss`)
3. **CRITICAL - Component Selector**: When an IR schema is provided with a `screen.selector` field, you MUST use that exact selector in the root `app.component.ts` file. The @Component decorator should have `selector: '<value-from-ir-schema>'` (e.g., if IR has `"selector": "app-dashboard"`, use `selector: 'app-dashboard'`). This allows the component to be embedded with the specified tag name.
4. Add every extra file needed for a working solution (e.g., `src/app/components/...`, `services/...`, `models/...`, `pipes/...`, `directives/...`, `guards/...`, or updates to `src/styles.css`).
5. After the `<files>` block emit `<metadata>...</metadata>` containing JSON with this shape:
```
<metadata>
{
  "dependencies": [
    {"name": "@angular/material", "version": "^17.3.0", "reason": "Stepper UI"},
    {"name": "aos", "version": "^2.3.4", "reason": "Animations", "dev": false}
  ],
  "devDependencies": [
    {"name": "@types/aos", "version": "^3.0.7", "reason": "TypeScript types for aos"}
  ],
  "providers": [
    {"provider": "provideHttpClient(withFetch())", "file": "src/app/app.config.ts", "reason": "HTTP calls"}
  ],
  "assets": [],
  "notes": "Mention assumptions or manual follow ups"
}
</metadata>
```
Use empty arrays when no additions are required. Everything listed in metadata must be reflected in the code and vice versa.
**IMPORTANT**: For any JavaScript library you import (e.g., `aos`, `chart.js`), you MUST:
1. Add the library to `dependencies` array
2. Add the corresponding `@types/[package-name]` to `devDependencies` array (if types exist)
3. Libraries already in boilerplate (aos, @types/aos) can be used without adding to metadata

## Implementation Rules
- Treat the base project as immutable boilerplate; focus on feature files that merge into it.
- Use Angular standalone components with explicit `imports` arrays and prefer `ChangeDetectionStrategy.OnPush`.
- Favor Tailwind utility classes for styling. HOWEVER, if the IR Schema provides exact pixel values (e.g., `padding: 18px`) or exact hex colors (e.g., `#1A2B3C`), you MUST extract these exact rules into the component's `.css` file and apply the standard CSS classes. Do NOT use inline styles, and do NOT overwrite exact values with generic Tailwind utilities.
- Use the `assets/figma_[node_id].png` paths provided in the IR schema for any `<img>` tags or CSS `background-image` rules (node_id will have ':' replaced with '_'). These files will be automatically provided in the project.
- Use Angular signals, reactive forms, and RxJS thoughtfully. Ensure cleanup via `takeUntilDestroyed`, `async` pipe, or signals.
- Provide accessible markup (ARIA labels, semantic elements) and responsive layouts.
- Include complete logic: no placeholders, no TODOs.
- Strongly type everything (interfaces for models, observable generics, strict null handling).
- Use dependency injection via `inject()` or constructors. Services must be `@Injectable({ providedIn: 'root' })`.
- For HTTP/async logic, include error handling (`catchError`, try/catch) and user feedback patterns.
- When defining lists, use Angular control flow (`@for`, `@if`) or structural directives with `trackBy` for performance.
- Add all required providers (e.g., `provideHttpClient`, `provideAnimations`, third-party modules) and reference them in both the code and `<metadata>.providers`.
- Include proper ARIA labels and semantic HTML
- Keep dependencies minimal and commonly used
- Prefer built-in Angular solutions over external libraries when possible
- When new dependencies are needed, choose stable versions compatible with Angular 17 and list them in `<metadata>.dependencies`.
- **DEPENDENCY REQUIREMENTS**: For every external library you import:
  - If it's a JavaScript library (e.g., `aos`, `chart.js`), you MUST also add `@types/[package-name]` to devDependencies in metadata
  - Example: If using `aos`, add both `{"name": "aos", "version": "^2.3.4"}` and `{"name": "@types/aos", "veSelectrsion": "^3.0.7", "dev": true}` to metadata
  - Libraries already in boilerplate (aos, @types/aos) can be used without adding to metadata
- Avoid `lucide-angular` in generated code (standalone components cannot safely use `ModuleWithProviders`). Prefer inline SVGs, Angular Material icons (when dependencies are declared), or simple text labels for icons.
- Angular 17 control flow requires named identifiers in `track` clauses. Use patterns like `@for (item of items; track item.id; let index = $index)` — never `track $index`.
🚫 **NEVER GENERATE THESE FILES** (they are auto-injected by the build system):
   - ❌ `src/screen-runtime.ts` - Deterministic workflow runtime (auto-injected)
   - ❌ `src/main.ts` - Bootstrap and orchestrator entry (auto-injected; do not edit)
   - ❌ Do NOT add `declare global`, `interface Window`, or ambient types for `__AIQOD_SCREEN__` — use `(window as any).__AIQOD_SCREEN__` only
   - ❌ `src/index.html` - Build system handles this
   - ❌ `angular.json`, `package.json`, `tsconfig.json` - Boilerplate files
   - ❌ `src/environments/environment.ts` - Written by the platform from backend config (apiBaseUrl, agentBasePath, projectId). Do not create it.
   - ❌ `src/environments/environment.prod.ts` - Use only a single environment.ts; do not create environment.prod.ts.

## SECURITY AND API URLS - CRITICAL
- **No secrets in Angular**: Do NOT embed API keys, tokens, or passwords in TS/HTML/CSS. Any operation that needs secrets must call backend endpoints; the backend holds credentials.
- **Public config only**: For non-secret config (e.g. feature flags, publishable keys), use `environment` or a ConfigService; never hardcode secret values.
- **URL construction**: Never hardcode full URLs (e.g. `http://172.168.1.19:5010/...`). Always build API URLs from:
  - `environment.apiBaseUrl` (backend host + port)
  - `environment.agentBasePath` (e.g. `/aiqod-agent/agent`)
  - `environment.projectId` (the real project id written by the platform — NEVER invent or fall back to `default_project`)
  Example: strip trailing slashes from environment.apiBaseUrl and environment.agentBasePath, then build: base + agentBase + "/dynamic/execute/" + environment.projectId.
- **projectId is mandatory**: Every dynamic execute URL MUST include `environment.projectId`. Do NOT use placeholders, empty strings, or `default_project`.
- **Single environment file**: The app has exactly one `src/environments/environment.ts` (written by the platform). Do not generate environment.prod.ts unless explicitly requested.

## Design Principles:
- IR schema is the source of truth. You must use it to create the UI and logic for the screen.
- IR schema is the source of truth for visual design. Consume `theme` (and top-level `antiPatterns` when present) as literal tokens — do not invent or substitute your own palette, fonts, or style direction.
- Use `theme.primary`, `theme.secondary`, `theme.accent`, `theme.background`, `theme.text`, and related color roles exactly as provided (map to CSS custom properties or Tailwind arbitrary values like `bg-[#...]`).
- Use `theme.typography.display` for headings and `theme.typography.body` for body text; do not fall back to a generic font family.
- Honor `theme.direction` for VISUAL AESTHETIC only (color mood, typography personality,
  shadow style, border treatment, motion character). `theme.direction` does NOT control
  page width or column count — those come exclusively from `layout.max_width`,
  `layout.columns`, and `layout.mode` in the IR schema.
- Do not infer layout structure from direction names. A direction called "Sketch Hand-Drawn"
  or "Enterprise SaaS" tells you how things look, not how wide the page is.
- Honor `theme.effects` when styling motion and hover states; do not introduce fallback motion
  or default palettes when those fields are present.
- Treat `antiPatterns` as hard constraints — never introduce patterns listed there.
- Ensure high contrast: light backgrounds → dark text; dark backgrounds → light text. Never generate invisible text due to palette conflicts.

## RESPONSIVE DESIGN - CRITICAL (CONTAINER QUERIES)
🚨 MANDATORY: Use container queries (@container) instead of viewport breakpoints (sm:, md:, lg:) for component portability.

### Root Component CSS — MUST include full-width rules:
The app.component.css MUST have ALL of the following. Do not omit any line:
```css
:host {
  display: block;
  width: 100%;          /* REQUIRED: container queries measure THIS box.
                           Without width:100%, :host shrinks to content width and
                           all @container(min-width:900px+) rules never fire. */
  min-height: 100vh;    /* REQUIRED: page fills the viewport height */
  container-type: inline-size;
  container-name: page-root;
}
```
This is the single most important CSS rule for responsive layout. Without `width: 100%`, every
`@container` breakpoint above the content's natural width is permanently inactive — producing a
screen that looks "mobile-only" on every desktop viewport.

### Required Syntax:
- ❌ WRONG: `class="grid-cols-1 md:grid-cols-2 lg:grid-cols-3"` (viewport breakpoints)
- ✅ CORRECT: `class="grid-cols-1 @container(min-width:400px):grid-cols-2 @container(min-width:700px):grid-cols-3"` (container queries)

### Container Query Patterns:
```html
<!-- Grid columns based on container width -->
<div class="grid grid-cols-1 
            @container(min-width:400px):grid-cols-2 
            @container(min-width:700px):grid-cols-3 
            @container(min-width:1000px):grid-cols-4">
</div>

<!-- Font sizes based on container width -->
<h1 class="text-xl 
           @container(min-width:400px):text-2xl 
           @container(min-width:700px):text-3xl 
           @container(min-width:900px):text-4xl">
</h1>

<!-- Padding based on container width -->
<div class="p-4 
            @container(min-width:500px):p-6 
            @container(min-width:800px):p-8 
            @container(min-width:1200px):p-10">
</div>

<!-- Flex direction based on container width -->
<div class="flex flex-col 
            @container(min-width:600px):flex-row">
</div>

<!-- Show/hide based on container width -->
<div class="hidden 
            @container(min-width:700px):block">
</div>

<!-- Tables scrollable on small containers -->
<div class="overflow-x-auto 
            @container(min-width:700px):overflow-x-visible">
  <table>...</table>
</div>

<!-- Reduce columns on smaller containers -->
<div class="grid grid-cols-4 
            @container(max-width:700px):grid-cols-2 
            @container(max-width:400px):grid-cols-1">
</div>
```

### Standard Container Breakpoints:
- **320px** - Very small containers
- **400px** - Small containers
- **600px** - Medium containers
- **700px** - Large containers
- **900px** - Extra large containers
- **1200px** - Full width containers

### Container-First Approach:
- Base styles for smallest container size
- Scale up using min-width: `@container(min-width:400px):text-lg`
- Scale down using max-width: `@container(max-width:400px):hidden`

### Exception - Page-Level Only:
ONLY use viewport breakpoints (`md:`, `lg:`) for page-level wrappers (header, footer, max-width containers).
All component internals MUST use @container queries.

## SPACING AND LAYOUT
- Use consistent spacing throughout: `p-4`, `p-6`, `p-8` for padding
- Use gaps in grids/flex: `gap-4`, `gap-6`, `gap-8`
- Add proper margins between sections: `mb-6`, `mb-8`, `mb-12`
- Page shell (`:host`, outermost `<div>` in `app.component.html`) MUST be `w-full min-h-screen`.
  NEVER apply `max-w-*` or `mx-auto` to the page shell — those make the entire app a narrow column.
- Apply `max-w-*` ONLY to the inner content wrapper, one level below the shell:
    ✅ CORRECT — shell fills viewport, content zone is capped:
    <div class="w-full min-h-screen bg-[theme.background]">           <!-- shell: full width -->
      <div class="w-full max-w-[1280px] mx-auto px-4 sm:px-6 lg:px-8">  <!-- content zone -->
        <!-- page content here -->
      </div>
    </div>

    ❌ WRONG — entire app becomes a narrow centered column:
    <div class="max-w-4xl mx-auto px-4">
      <!-- page content here -->
    </div>

- Full-bleed regions (hero banners, background sections, sticky headers, footers, sidebars)
  go OUTSIDE the content wrapper so they reach the true screen edges.
- Use `layout.max_width` from the IR schema for the content wrapper (e.g. `max-w-[860px]`).
  If `layout.max_width` is "none" or absent, use `max-w-screen-2xl` as the content wrapper cap.
- Add breathing room - elements should not touch screen edges
- Section padding: `py-8 @container(min-width:600px):py-12 @container(min-width:900px):py-16`

### INTERACTIVITY AND ANIMATIONS
- Prefer hover/focus colors derived from `theme.primary` / `theme.accent` (e.g. `hover:bg-[theme.primary]`, `focus:ring-[theme.primary]`) — do NOT hardcode blue/indigo/slate utility classes
- Add hover effects using theme tokens (subtle shadow lift / color shift); apply motion only when `theme.effects` specifies it
- Use `focus:` states for inputs: `focus:ring-2` using the theme primary/accent ring color and matching border
- Do not add default `transition-all duration-300` (or similar) when `theme.effects` is absent
- Button interactions: `active:scale-95 transform`
- Use Flowbite's JavaScript components with proper data attributes
- Add subtle animations using AOS (already installed): Import `import AOS from 'aos'` and initialize in `ngOnInit()` with `AOS.init()`. Use `data-aos="fade-up"` attributes in templates.
- **ARIA Attributes**: When using ARIA attributes with Angular expressions, use property binding: `[attr.aria-controls]="'prefix-' + variable"` NOT `aria-controls="prefix-{{variable}}"`. Static ARIA attributes can use `aria-label="static text"` but dynamic ones MUST use `[attr.aria-*]` binding. Examples:
  - ❌ WRONG: `aria-controls="faq-answer-{{i}}"` or `aria-labelledby="question-{{id}}-label"`
  - ✅ CORRECT: `[attr.aria-controls]="'faq-answer-' + i"` or `[attr.aria-labelledby]="'question-' + id + '-label'"`
- **ARIA Attributes**: When using ARIA attributes with Angular expressions, use property binding: `[attr.aria-controls]="'prefix-' + variable"` NOT `aria-controls="prefix-{{variable}}"`. Static ARIA attributes can use `aria-label="static text"` but dynamic ones MUST use `[attr.aria-*]` binding. Examples:
  - ❌ WRONG: `aria-controls="faq-answer-{{i}}"` or `aria-labelledby="question-{{id}}-label"`
  - ✅ CORRECT: `[attr.aria-controls]="'faq-answer-' + i"` or `[attr.aria-labelledby]="'question-' + id + '-label'"`

## FILE URL DISPLAY
When displaying uploaded documents/files, generate public URLs by replacing the filesystem path with the base URL:
- Files are stored at: `{file_upload_path}` (e.g., `{file_upload_path}/18c25.pdf`)
- Public URL: `{deploy_base_url}/uploads/18c25.pdf`
- Example: If backend returns `filePath: "{file_upload_path}/invoice123.pdf"`, display as `<a href="{deploy_base_url}/uploads/invoice123.pdf">View Document</a>`
- For images: `<img src="{deploy_base_url}/uploads/photo.jpg">`
- Extract filename from path and construct public URL in your component logic or use a pipe

## Checklist Before Responding
- [ ] The root component composes the generated UI (or router shell) using the provided files.
- [ ] All template variables are initialized in component
- [ ] All modules needed for directives/pipes are imported (CommonModule, FormsModule, ReactiveFormsModule, etc.)
- [ ] Every referenced symbol/import/file exists somewhere within `<files>`.
- [ ] Tailwind utilities or scoped CSS guarantee a responsive experience.
- [ ] Forms include validation and accessibility considerations.
- [ ] Ensure all imports are correct and components are properly registered
- [ ] Observables/signals are disposed safely.
- [ ] Metadata matches the actual code changes (dependencies, providers, assets, notes).
- [ ] Import CommonModule in standalone components that use *ngIf, *ngFor, etc.
- [ ] Output must be production-ready with zero compilation errors

Output only the `<files>` bundle followed by the `<metadata>` block. The bundle must be drop-in ready for the base Angular project and runnable after applying any metadata-driven dependency/provider updates.

"""
        # Replace placeholders with actual config values
        prompt = prompt.replace("{file_upload_path}", file_upload_path)
        prompt = prompt.replace("{deploy_base_url}", deploy_base_url)

        # Inject Angular stack-level conventions from vendored angular.csv.
        # This is separate from the theme/design pipeline — it covers Angular idioms
        # (signals, standalone, OnPush, @for track, etc.) independent of visual direction.
        stack_guidelines = get_angular_stack_guidelines()
        if stack_guidelines:
            prompt = prompt + "\n" + stack_guidelines

        return prompt
        
    def _build_angular_codegen_cached_system_text(self, *, is_figma_mode: bool) -> str:
        """Static system text for explicit context cache (figma vs tailwind variants)."""
        parts = [self.prompt_template]
        _append_angular_codegen_mode_block(parts, is_figma_mode=is_figma_mode)
        return "".join(parts)

    def _build_prompt(
        self,
        user_prompt: str,
        *,
        ir_schema: Optional[Dict[str, Any]] = None,
        backend_metadata: Optional[Dict[str, Any]] = None,
        is_figma_mode: bool = False,
    ) -> str:
        """
        Build the final prompt for LLM.
        """
        logger.debug("Constructing final prompt...")
        prompt_parts = [self.prompt_template]
        _append_angular_codegen_mode_block(prompt_parts, is_figma_mode=is_figma_mode)
        prompt_parts.append(
            self._build_angular_codegen_dynamic_prompt_parts(
                user_prompt,
                ir_schema=ir_schema,
                backend_metadata=backend_metadata,
            )
        )
        return "".join(prompt_parts)

    def _build_angular_codegen_dynamic_prompt_parts(
        self,
        user_prompt: str,
        *,
        ir_schema: Optional[Dict[str, Any]] = None,
        backend_metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Dynamic user content for codegen — IR schema, backend contract, user request."""
        prompt_parts: List[str] = []

        if ir_schema:
            prompt_parts.append("\n## FULL-STACK IR SCHEMA CONTEXT\n")
            prompt_parts.append("<ir_schema>\n")
            prompt_parts.append(json.dumps(ir_schema, indent=2))
            prompt_parts.append("\n</ir_schema>\n")

            anti_patterns = ir_schema.get("antiPatterns") or []
            if isinstance(anti_patterns, list) and anti_patterns:
                prompt_parts.append("\n## ANTI-PATTERNS (HARD CONSTRAINTS)\n")
                prompt_parts.append(
                    "Do NOT introduce any of the following in generated markup, CSS, or Tailwind classes:\n"
                )
                for item in anti_patterns:
                    if isinstance(item, str) and item.strip():
                        prompt_parts.append(f"- {item.strip()}\n")
                prompt_parts.append("\n")

            theme = ir_schema.get("theme") or {}
            if isinstance(theme, dict) and theme:
                prompt_parts.append("\n## THEME TOKENS (LITERAL SOURCE OF TRUTH)\n")
                prompt_parts.append(
                    "Use these exact tokens; do not invent substitute palettes or fonts.\n"
                )
                if theme.get("direction"):
                    prompt_parts.append(f"- direction: {theme.get('direction')}\n")
                for color_key in (
                    "primary",
                    "secondary",
                    "accent",
                    "background",
                    "text",
                    "error",
                    "success",
                    "warning",
                    "info",
                ):
                    if theme.get(color_key):
                        prompt_parts.append(f"- {color_key}: {theme.get(color_key)}\n")
                typography = theme.get("typography") if isinstance(theme.get("typography"), dict) else {}
                if typography.get("display") or typography.get("body"):
                    prompt_parts.append(
                        f"- typography.display: {typography.get('display', '')}\n"
                        f"- typography.body: {typography.get('body', '')}\n"
                    )
                effects = theme.get("effects")
                if effects:
                    prompt_parts.append(f"- effects: {json.dumps(effects)}\n")
                prompt_parts.append("\n")
            
            # WORKFLOW INPUT/OUTPUT PARAMETERS
            input_params = ir_schema.get("inputParameters", [])
            output_params = ir_schema.get("outputParameters", [])
            
            if input_params or output_params:
                prompt_parts.append("\n## WORKFLOW INPUT/OUTPUT PARAMETERS (CRITICAL)\n")
                prompt_parts.append("This screen is part of a workflow and must handle inputs/outputs:\n\n")
                
                if input_params:
                    prompt_parts.append("**INPUT PARAMETERS** (data from previous workflow steps):\n")
                    for param in input_params:
                        name = param.get("name", "unknown")
                        param_type = param.get("type", "any")
                        required = param.get("required", False)
                        description = param.get("description", "")
                        bind_to = param.get("bindTo", [])
                        prompt_parts.append(f"- {name} ({param_type}, required={required}): {description}\n")
                        if bind_to:
                            prompt_parts.append(f"  Bind to: {json.dumps(bind_to, indent=2)}\n")
                    prompt_parts.append("\n")
                    prompt_parts.append("**YOU MUST:**\n")
                    prompt_parts.append("1. Subscribe to `window.__AIQOD_SCREEN__.onInput()` in component ngOnInit()\n")
                    prompt_parts.append("2. Map input parameters to form controls or component properties\n")
                    prompt_parts.append("3. Use defensive coding to handle missing or partial inputs\n\n")
                
                if output_params:
                    prompt_parts.append("**OUTPUT PARAMETERS** (data for next workflow steps):\n")
                    for param in output_params:
                        name = param.get("name", "unknown")
                        param_type = param.get("type", "any")
                        description = param.get("description", "")
                        extract_from = param.get("extractFrom", {})
                        source_component = param.get("sourceComponent", "")
                        prompt_parts.append(f"- {name} ({param_type}): {description}\n")
                        if extract_from:
                            prompt_parts.append(f"  Extract from: {json.dumps(extract_from, indent=2)}\n")
                        if source_component:
                            prompt_parts.append(f"  Source component: {source_component}\n")
                    prompt_parts.append("\n")
                    prompt_parts.append("**YOU MUST:**\n")
                    prompt_parts.append("1. Structure output object to match outputParameters definition\n")
                    prompt_parts.append("2. Call `window.__AIQOD_SCREEN__.complete(output)` on Next/Submit\n")
                    prompt_parts.append("3. Include all required fields from outputParameters\n\n")
            
            
            # DATA MODEL CONTRACT: single source of truth for backend + frontend
            prompt_parts.append("\n## DATA MODEL CONTRACT (CRITICAL)\n")
            prompt_parts.append(
                "The IR schema contains a `dataModels` section that defines the canonical request/response\n"
                "shapes for all backend interactions, and a `dataSources` section that binds those models\n"
                "to specific function IDs.\n\n"
                "- Treat `dataModels` as the **single source of truth** for field names, types, and semantics.\n"
                "- For every backend function you call from the Angular app, its `params` object MUST\n"
                "  match exactly the corresponding input model from `dataModels` (no renames, no extra\n"
                "  fields, no missing required fields).\n"
                "- Params keys MUST be the **request model** field names — never workflow\n"
                "  `inputParameters` names unless they are identical to those request fields.\n"
                "  Map node inputs → request fields via bindTo / same name / documented action mapping.\n"
                "- When a list field includes `items.model` or `itemFields` in the backend input_schema,\n"
                "  iterate and bind using those item field names exactly.\n"
                "- When a list field uses `items.type` (primitive) or workflow `itemType: \"scalar\"`,\n"
                "  the TypeScript type is `string[]` / `number[]` / `boolean[]` — NOT\n"
                "  `Array<{ skill: string }>`. Do not wrap primitive arrays as one-field objects.\n"
                "- `itemType: \"enum\"` is also a primitive string array (`string[]`).\n"
                "- Choice controls (radio, select, toggle, rating) whose options come from a list item:\n"
                "  the submitted `[value]` MUST be the same representation the function's `databaseLogic`\n"
                "  compares against (id/code/letter/number — whatever the logic states). Display text is\n"
                "  the label only, unless databaseLogic says the compared field IS the display text.\n"
                "- Dict/map params: keys must follow databaseLogic (e.g. string list index vs item id).\n"
                "  Do not invent a parallel camelCase payload shape.\n"
                "- All Angular form controls (FormControls / FormGroup / signals / ngModel bindings) MUST be\n"
                "  derived directly from the same `dataModels` definitions used by the backend.\n"
                "- Do NOT \"interpret\" or reshape the schema; mirror it exactly in TypeScript interfaces,\n"
                "  form groups, and HTTP request payloads so that Pydantic models on the backend accept\n"
                "  the data without any validation errors.\n\n"
            )
            
            # Extract selector if present and emphasize it
            screen_selector = ir_schema.get("screen", {}).get("selector")
            if screen_selector:
                prompt_parts.append("\n## CRITICAL - COMPONENT SELECTOR REQUIREMENT\n")
                prompt_parts.append(f"The root app.component.ts MUST use this exact selector in the @Component decorator:\n")
                prompt_parts.append(f"selector: '{screen_selector}'\n\n")
                prompt_parts.append(f"This means the @Component decorator should look like:\n")
                prompt_parts.append(f"@Component({{\n")
                prompt_parts.append(f"  selector: '{screen_selector}',\n")
                prompt_parts.append(f"  standalone: true,\n")
                prompt_parts.append(f"  imports: [...],\n")
                prompt_parts.append(f"  templateUrl: './app.component.html',\n")
                prompt_parts.append(f"  styleUrls: ['./app.component.css']\n")
                prompt_parts.append(f"}})\n\n")

        if backend_metadata:
            base_path = backend_metadata.get("base_path")
            resolved_base_path = self._resolve_backend_api_path(base_path, backend_metadata)

            backend_prompt_payload = {
                "api_base_path": resolved_base_path,
                # Each function entry is produced by BackendGenerator.generate_backend and
                # has the following shape (important for constructing correct params):
                # {
                #   "id": "function_id",
                #   "type": "dynamic" | "external",
                #   "description": "...",
                #   "source_code": "...",
                #   "input_model": "ModelName",
                #   "response_model": "ModelName",
                #   "input_schema": {
                #       "name": "ModelName",
                #       "fields": [
                #           {"name": "fieldA", "type": "string", "required": true},
                #           {"name": "fieldB", "type": "dict", "required": true},
                #           ...
                #       ]
                #   },
                #   "response_schema": { ...same shape as input_schema... }
                # }
                # You MUST shape the `params` object for each call so that it includes
                # at least all `required: true` fields from input_schema.fields, with
                # appropriate JSON-serializable values that match the `type`.
                "functions": backend_metadata.get("functions") or [],
            }

            # Build a concise required-parameter checklist so the LLM never omits fields
            required_fields_lines: List[str] = []
            for fn in backend_prompt_payload["functions"]:
                fields = (fn.get("input_schema") or {}).get("fields") or []
                required = [f.get("name") for f in fields if f.get("required")]
                if required:
                    required_fields_lines.append(f"- {fn.get('id')}: required params -> {', '.join(required)}")

            prompt_parts.append("\n## BACKEND API CONTRACT\n")
            prompt_parts.append("<backend_api>\n")
            prompt_parts.append(json.dumps(backend_prompt_payload, indent=2))
            prompt_parts.append("\n</backend_api>\n")
            if required_fields_lines:
                prompt_parts.append("\nREQUIRED PARAM CHECKLIST (MUST be present in every call):\n")
                prompt_parts.append("\n".join(required_fields_lines))
                prompt_parts.append(
                    "\nFor any required field typed as 'dict' or 'list', pass an empty {} or [] "
                    "instead of omitting the field when you have no user input. "
                    "Never call a backend function with an empty params object if the checklist "
                    "shows required fields.\n"
                )
            
            project_id_for_prompt = (
                (backend_metadata or {}).get("project_id")
                or "PROJECT_ID"
            )
            example_api_path = resolved_base_path
            if not example_api_path and backend_metadata:
                api_base_url = backend_metadata.get("api_base_url") or self.api_base_url
                # api_prefix = getattr(self, "api_prefix", "") or ""
                # prefix_segment = ""
                if api_base_url:
                    example_api_path = (
                        f"{api_base_url.rstrip('/')}/aiqod-agent/agent/dynamic/execute/{project_id_for_prompt}"
                    )
                else:
                    example_api_path = f"/aiqod-agent/agent/dynamic/execute/{project_id_for_prompt}"
            print(f"example_api_path: {example_api_path}")
            print(f"resolved_base_path: {resolved_base_path}")
            # print(f"backend_metadata: {backend_metadata}")

            prompt_parts.append(
                "\nUse Angular HttpClient (services) to invoke the backend instead of mocks.\n"
                "- DO NOT use empty strings, placeholders like '/api/execute', or default values - use the exact api_base_path provided above.\n"
                "\n**ENVIRONMENT CONFIG & API CALL FORMAT:**\n"
                "- The Angular app has a single `src/environments/environment.ts` file provided by the platform.\n"
                "- It exposes:\n"
                "  - `apiBaseUrl` (e.g. `\"https://dev.aiqod.com:843/dev\"`) \n"
                "  - `agentBasePath` (e.g. `\"/aiqod-agent/agent\"`)\n"
                f"  - `projectId` (this screen's project id: `\"{project_id_for_prompt}\"`) — written by the platform\n"
                "- You MUST build the dynamic execute URL from these fields instead of hardcoding the full URL string.\n"
                "- ALWAYS use `environment.projectId` in the execute path. NEVER use `default_project`, empty string, or invent a project id.\n"
                f"- The resolved backend_api base path for this screen is: '{example_api_path}'. Use it only as a reference; do NOT inline this literal value in the code.\n"
                "\n**API CALL FORMAT:**\n"
                "All API calls must POST to the api_base_path with payloads shaped as:\n"
                '  {"function_id": "<function id>", "params": { ...fields... }, "request_meta": {"source": "angular_app", ...extraMeta}}\n'
                "- For each function, use backend_api.functions[*].input_schema.fields to decide exactly which keys to put under `params`.\n"
                "- Every field where `required` is true MUST be present in `params` with a non-null value of the correct primitive type.\n"
                "- Prefer simple, flat JSON objects for params matching the field names/types; do not invent extra wrapper levels.\n"
                "Handle loading + error states and bind UI interactions to these calls.\n"
                "\n**EXAMPLE ApiService IMPLEMENTATION (MUST USE environment.ts):**\n"
                "```typescript\n"
                "import { environment } from '../../environments/environment';\n"
                "@Injectable({ providedIn: 'root' })\n"
                "export class ApiService {\n"
                "  private http = inject(HttpClient);\n"
                "  private getApiUrl(): string {\n"
                "    const baseUrl = environment.apiBaseUrl.replace(/\\/$/, '');\n"
                "    const agentBase = environment.agentBasePath.replace(/\\/$/, '');\n"
                "    return `${baseUrl}${agentBase}/dynamic/execute/${environment.projectId}`;\n"
                "  }\n"
                "\n"
                "  callFunction<T>(functionId: string, params: any = {}): Observable<T> {\n"
                "    const obs = (window as any).__AIQOD_SCREEN__?.getObsContext?.() || {};\n"
                "    const payload = {\n"
                "      function_id: functionId,\n"
                "      params: params,\n"
                "      request_meta: {\n"
                "        source: 'angular_app',\n"
                "        execution_id: obs.execution_id,\n"
                "        workflow_id: obs.workflow_id,\n"
                "        screen_id: obs.screen_id,\n"
                "        user_id: obs.user_id || obs.userId,\n"
                "        userId: obs.user_id || obs.userId,\n"
                "        subscriber_id: obs.subscriber_id || obs.subscriberId,\n"
                "        subscriberId: obs.subscriber_id || obs.subscriberId,\n"
                "        org_id: obs.org_id || obs.orgId,\n"
                "        orgId: obs.org_id || obs.orgId,\n"
                "        _obs_ui_span_id: obs._obs_ui_span_id,\n"
                "      }\n"
                "    };\n"
                "    return this.http.post<T>(this.getApiUrl(), payload);\n"
                "  }\n"
                "}\n"
                "```\n"
            )
        
        user_instruction = f"\n\n<user_request>\n{user_prompt.strip()}\n</user_request>\n"
        prompt_parts.append(user_instruction)

        return "".join(prompt_parts)

    def _resolve_backend_api_path(
        self,
        base_path: Optional[str],
        backend_metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Normalize the backend API base path and ensure it becomes an absolute URL
        when a base URL is available.
        """
        if not base_path:
            logger.warning("Angular generator: base_path is empty")
            return ""

        normalized_path = base_path.strip()
        lower_path = normalized_path.lower()

        if lower_path.startswith("http://") or lower_path.startswith("https://"):
            logger.info("Angular generator: base_path is already a full URL: %s", normalized_path)
            return normalized_path

        if normalized_path.startswith("/aiqod-agent"):
            relative_path = normalized_path
        elif normalized_path.startswith("/"):
            relative_path = f"/aiqod-agent/agent{normalized_path}"
        else:
            relative_path = f"/aiqod-agent/agent/{normalized_path.lstrip('/')}"

        base_url = None
        if backend_metadata:
            base_url = backend_metadata.get("api_base_url")
            if base_url:
                logger.info("Angular generator: Using api_base_url from backend_metadata: %s", base_url)
        if not base_url:
            base_url = self.api_base_url
            if base_url:
                logger.info("Angular generator: Using api_base_url from Angular generator config: %s", base_url)

        if base_url:
            resolved = f"{base_url.rstrip('/')}{relative_path}"
            logger.info("Angular generator: Resolved API path: %s", resolved)
            return resolved
        
        logger.warning("Angular generator: No base URL available, returning relative path: %s", relative_path)
        return relative_path

    def _invoke_model(
        self,
        prompt_text: str,
        *,
        model_name: Optional[str] = None,
        generation_config: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        operation: str = "generate-angular-app",
        sub_operation: str = "angular-codegen",
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        cached_content: Optional[str] = None,
        cached_user_prompt: Optional[str] = None,
    ) -> tuple[str, str, str, Dict[str, Any]]:
        """Call Gemini with retry logic and return the raw response text with metadata."""
        logger.info("Sending prompt to LLM...")
        selected_model = model_name or self.model_name
        # selected_model="gemini-2.5-pro"
        selected_config = generation_config or self.generation_config
        selected_timeout = timeout or self.default_timeout
        
        # Build the config object via gemini_service
        gen_cfg = GeminiGenerateConfig(
            temperature=selected_config.get('temperature', 0.4),
            top_p=selected_config.get('top_p', 0.95),
            top_k=selected_config.get('top_k', 40),
            max_output_tokens=selected_config.get('max_output_tokens'),
            response_mime_type=selected_config.get('response_mime_type'),
            stop_sequences=selected_config.get('stop_sequences'),
            fallback_profile="coding",
        )

        last_exception = None
        recitation_fallback_used = False
        active_cache = cached_content
        cache_created_for_model = selected_model

        # Retry logic (max 3 attempts)
        for attempt in range(1, self.max_retries + 1):
            try:
                # On RECITATION, switch to the less-sensitive fix model for remaining attempts
                if attempt > 1 and last_exception:
                    prev_model = selected_model
                    selected_model, active_cache, recitation_fallback_used = (
                        _adjust_angular_retry_for_model_cache(
                            last_exception=last_exception,
                            selected_model=selected_model,
                            cached_content=active_cache,
                            recitation_fallback_used=recitation_fallback_used,
                            fix_model_name=self.fix_model_name,
                            cache_created_for_model=cache_created_for_model,
                        )
                    )
                    if selected_model != prev_model:
                        logger.warning(
                            "RECITATION detected with %s — switching to fallback model %s "
                            "for remaining attempts (cache=%s)",
                            prev_model,
                            selected_model,
                            "dropped" if not active_cache else "kept",
                        )
                        gen_cfg.temperature = 0.2
                    elif cached_content and not active_cache:
                        logger.warning(
                            "Context cache dropped after model/CachedContent mismatch "
                            "(request=%s cache_model=%s)",
                            selected_model,
                            cache_created_for_model,
                        )

                logger.info(f"API call attempt {attempt}/{self.max_retries} (model={selected_model})")

                # Track current model name for usage aggregation
                self._current_model_name = selected_model

                response = _angular_sync_llm_call(
                    selected_model,
                    user_prompt=cached_user_prompt if active_cache else prompt_text,
                    full_contents=prompt_text,
                    cached_content=active_cache,
                    config=gen_cfg,
                )
                
                logger.debug("Gemini response received.")
                
                # Check if response has candidates
                if not response.candidates:
                    logger.error("Gemini did not return any candidates for the request.")
                    raise RuntimeError("Gemini did not return any candidates for the request.")

                candidate = response.candidates[0]
                finish_reason = str(candidate.finish_reason)
                safety_ratings = str(candidate.safety_ratings) if hasattr(candidate, 'safety_ratings') else ""

                logger.info(f"Finish reason: {finish_reason}")
                
                # Check finish reason
                if candidate.finish_reason not in (1, "STOP"):
                    if _is_recitation_finish_reason(candidate.finish_reason):
                        error_msg = (
                            "Generation aborted. Finish reason: "
                            f"FinishReason.RECITATION ({finish_reason})"
                        )
                    else:
                        error_msg = f"Generation aborted. Finish reason: {finish_reason}"
                    logger.error(error_msg)
                    raise RuntimeError(error_msg)

                # Extract text from response
                if hasattr(response, "text") and response.text:
                    raw_text = response.text
                else:
                    parts = [part.text for part in candidate.content.parts if hasattr(part, "text")]
                    raw_text = "\n".join(parts)

                if not raw_text:
                    logger.error("Gemini returned empty textual content.")
                    raise RuntimeError("Gemini response had no textual content to parse.")

                # Extract token usage information
                usage_info = self._extract_usage_info(response)
                
                # Record LLM call to database
                if project_id:
                    token_data = extract_token_usage_from_response(response)
                    record_llm_call(
                        project_id=project_id,
                        screen_id=screen_id,
                        operation=operation,
                        sub_operation=sub_operation,
                        model=selected_model,
                        prompt_tokens=token_data["prompt_tokens"],
                        completion_tokens=token_data["completion_tokens"],
                        total_tokens=token_data["total_tokens"],
                        status="success",
                        version=version,
                        attempt=attempt,
                        is_retry=attempt > 1,
                        metadata={"finish_reason": finish_reason},
                        org_id=org_id,
                        subscriber_id=subscriber_id,
                        user_id=user_id,
                    )

                logger.info(f"API call succeeded on attempt {attempt}")
                logger.debug("Gemini response text extracted.")
                return raw_text, finish_reason, safety_ratings, usage_info
                
            except BillingError:
                raise
            except (TimeoutError, asyncio.TimeoutError):
                # Hard network stall — propagate immediately rather than retrying.
                raise
            except Exception as e:
                last_exception = e
                # Check if this is a fallback-eligible error (401, 429, 503) - don't retry, let gemini_service handle fallback
                if _gemini_is_fallback_eligible_error(e):
                    logger.warning(f"Fallback-eligible error detected, propagating to gemini_service for OpenAI fallback: {e}")
                    raise
                logger.warning(f"API call attempt {attempt}/{self.max_retries} failed: {str(e)}")

                if attempt < self.max_retries:
                    # Exponential backoff: 2, 4, 8 seconds
                    wait_time = 2 ** attempt
                    logger.info(f"Retrying in {wait_time} seconds...")
                    import time
                    time.sleep(wait_time)
                else:
                    logger.error(f"All {self.max_retries} API call attempts failed")
                    if isinstance(last_exception, BillingError):
                        raise last_exception
                    raise RuntimeError(f"Gemini API call failed after {self.max_retries} attempts") from last_exception
        
        # Should never reach here, but just in case
        raise RuntimeError("Unexpected error in _invoke_model") from last_exception

    def _extract_usage_info(self, response) -> Dict[str, Any]:
        """
        Extract token usage and calculate costs from Gemini API response.
        
        Returns a dictionary with:
        - prompt_tokens: Number of input tokens
        - completion_tokens: Number of output tokens
        - total_tokens: Total tokens used
        - cost_usd: Estimated cost in USD
        - cost_breakdown: Breakdown of input/output costs
        """
        usage_info = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "cost_breakdown": {
                "input_cost_usd": 0.0,
                "output_cost_usd": 0.0
            }
        }
        
        # Gemini 3 pro pricing (as of 2025)
        # Input: $4 for tokens >200k
        # Output: $18 for tokens >200k
        INPUT_COST_PER_MILLION = 4
        OUTPUT_COST_PER_MILLION = 18
        
        try:
            # Check for usage_metadata in the response
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                usage_metadata = response.usage_metadata
                prompt_tokens = getattr(usage_metadata, "prompt_token_count", 0)
                candidates_tokens = getattr(usage_metadata, "candidates_token_count", 0)
                total_tokens = getattr(usage_metadata, "total_token_count", 0)
                
                usage_info["prompt_tokens"] = prompt_tokens
                usage_info["completion_tokens"] = candidates_tokens
                usage_info["total_tokens"] = total_tokens if total_tokens > 0 else (prompt_tokens + candidates_tokens)
                
                # Calculate costs
                input_cost = (prompt_tokens / 1_000_000) * INPUT_COST_PER_MILLION
                output_cost = (candidates_tokens / 1_000_000) * OUTPUT_COST_PER_MILLION
                total_cost = input_cost + output_cost
                
                usage_info["cost_usd"] = round(total_cost, 6)
                usage_info["cost_breakdown"]["input_cost_usd"] = round(input_cost, 6)
                usage_info["cost_breakdown"]["output_cost_usd"] = round(output_cost, 6)
                
                logger.info(
                    f"Token usage - Input: {prompt_tokens}, Output: {candidates_tokens}, "
                    f"Total: {usage_info['total_tokens']}, Cost: ${total_cost:.6f}"
                )

                # Aggregate usage per model for this generator instance
                model_name = getattr(self, "model_name", None)
                # Prefer the dynamically selected model if available
                current_model = getattr(self, "_current_model_name", None) or model_name
                if current_model:
                    if not hasattr(self, "token_usage_by_model"):
                        self.token_usage_by_model = {}
                    agg = self.token_usage_by_model.setdefault(current_model, {
                        "total_input_tokens": 0,
                        "total_output_tokens": 0,
                        "total_tokens": 0,
                        "total_calls": 0,
                        "input_cost_usd": 0.0,
                        "output_cost_usd": 0.0,
                        "total_cost_usd": 0.0,
                    })
                    agg["total_input_tokens"] += prompt_tokens
                    agg["total_output_tokens"] += candidates_tokens
                    agg["total_tokens"] += usage_info["total_tokens"]
                    agg["total_calls"] += 1
                    agg["input_cost_usd"] += input_cost
                    agg["output_cost_usd"] += output_cost
                    agg["total_cost_usd"] += total_cost
            else:
                logger.warning("Usage metadata not available in Gemini response")
                
        except Exception as e:
            logger.warning(f"Error extracting usage info: {e}")
        
        return usage_info

    def reset_token_usage(self) -> None:
        """Reset aggregated LLM token usage for this AngularAppGenerator instance."""
        self.token_usage_by_model = {}

    def get_token_usage_summary(self) -> Dict[str, Dict[str, Any]]:
        """Get a copy of aggregated LLM token usage by model."""
        return getattr(self, "token_usage_by_model", {}).copy()

    def _parse_generated_files(self, response_text: str) -> tuple[List[GeneratedFile], Dict[str, Any]]:
        """Parse the <files> payload and <metadata> from LLM response."""
        logger.debug("Extracting <files> block from Gemini response...")
        files_block_match = re.search(
            r"<files>(.*?)</files>", response_text, re.DOTALL | re.IGNORECASE
        )
        if not files_block_match:
            logger.error("Missing <files> wrapper in LLM response.")
            raise ValueError("LLM response missing <files>...</files> wrapper.")

        files_block = files_block_match.group(1)
        file_entries = re.findall(
            r'<file\s+path="([^"]+)">\s*(.*?)\s*</file>',
            files_block,
            re.DOTALL | re.IGNORECASE,
        )

        if not file_entries:
            logger.error("No <file> entries found in LLM response.")
            raise ValueError("LLM response did not contain any <file path=\"...\"> entries.")

        parsed_files: List[GeneratedFile] = []
        filtered_count = 0
        for path, content in file_entries:
            cleaned_path = path.strip().lstrip("/")
            
            # Filter deterministic platform files (injected separately)
            if cleaned_path in PROTECTED_GENERATED_PATHS:
                filtered_count += 1
                logger.warning(
                    f"Filtered out LLM-generated {cleaned_path} (written deterministically by build system)",
                )
                continue
                
            cleaned_content = self._clean_generated_content(content)
            parsed_files.append(GeneratedFile(path=cleaned_path, content=cleaned_content))
        
        if filtered_count > 0:
            logger.info(f"Filtered out {filtered_count} protected files from LLM output")
        logger.info(f"Successfully parsed {len(parsed_files)} files.")
        
        # Parse metadata
        metadata = self._parse_metadata(response_text)
        
        return parsed_files, metadata

    def _clean_generated_content(self, content: str) -> str:
        """
        Remove XML CDATA wrappers and stray markers from LLM output so the
        written files remain valid TypeScript/HTML.
        """
        if not content:
            return ""

        cleaned = content.strip()

        # Strip starting <![CDATA[ (allowing accidental newlines/whitespace splits)
        cleaned = re.sub(
            r"<\s*!\s*\[CDATA\[\s*",
            "",
            cleaned,
            count=1,
            flags=re.IGNORECASE,
        )

        # Strip trailing ]]>
        cleaned = re.sub(
            r"\s*\]\]\>\s*$",
            "",
            cleaned,
            count=1,
            flags=re.IGNORECASE,
        )

        # CRITICAL FIX: Remove markdown code fences that LLMs often add
        # Remove opening fences like ```typescript, ```javascript, ```css, ```html
        cleaned = re.sub(
            r"^```[a-zA-Z]*\s*\n?",
            "",
            cleaned,
            flags=re.MULTILINE,
        )
        
        # Remove closing fences
        cleaned = re.sub(
            r"\n?```\s*$",
            "",
            cleaned,
            flags=re.MULTILINE,
        )

        return cleaned.strip()

    def _parse_metadata(self, response_text: str) -> Dict[str, Any]:
        """Parse <metadata> block from LLM response."""
        metadata_match = re.search(
            r"<metadata>(.*?)</metadata>", response_text, re.DOTALL | re.IGNORECASE
        )
        
        if not metadata_match:
            logger.warning("No <metadata> block found in LLM response. Using defaults.")
            return {
                "dependencies": [],
                "providers": [],
                "assets": [],
                "notes": ""
            }
        
        try:
            metadata_json = metadata_match.group(1).strip()
            metadata = json.loads(metadata_json)
            logger.info("Parsed metadata from LLM response.")
            return metadata
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse metadata JSON: {e}. Using defaults.")
            return {
                "dependencies": [],
                "providers": [],
                "assets": [],
                "notes": ""
            }

    def workspace_exists(self, project_id: str, screen_id: str) -> bool:
        """Check if a workspace already exists for this project/screen."""
        workspace_dir = self._get_workspace_path(project_id, screen_id)
        return workspace_dir is not None and workspace_dir.exists()

    def _get_workspace_path(self, project_id: str, screen_id: str) -> Optional[Path]:
        """Get the path to the most recent workspace directory if it exists.

        Supports both:
        - Legacy:  angular-{project_id}-{screen_id}-workspace
        - Versioned: angular-{project_id}-{screen_id}-v{version}-workspace
        """
        safe_project = re.sub(r"[^a-zA-Z0-9_\-]", "_", project_id)
        safe_screen = re.sub(r"[^a-zA-Z0-9_\-]", "_", screen_id)

        prefix = f"angular-{safe_project}-{safe_screen}"
        candidates: list[Path] = []

        if self.generated_apps_root.exists():
            for child in self.generated_apps_root.iterdir():
                if not child.is_dir():
                    continue
                name = child.name
                if not name.startswith(prefix):
                    continue
                if not name.endswith("-workspace"):
                    continue
                candidates.append(child)

        if not candidates:
            return None

        # Version strings are YYYYMMDDHHMMSS so lexicographical sort works.
        candidates.sort(key=lambda p: p.name)
        return candidates[-1]

    def _read_workspace_code(self, workspace_dir: str) -> Dict[str, str]:
        """
        Read all source code files from workspace for context.
        
        Returns:
            Dictionary mapping file paths (relative to workspace) to file contents
        """
        code_files = {}
        workspace_path = Path(workspace_dir)
        
        # Files to include (skip node_modules, dist, .angular, etc.)
        include_patterns = ["*.ts", "*.html", "*.css", "*.json"]
        exclude_dirs = {"node_modules", "dist", ".angular", ".git", "out-tsc"}
        
        for pattern in include_patterns:
            for file_path in workspace_path.rglob(pattern):
                # Skip excluded directories
                if any(excluded in file_path.parts for excluded in exclude_dirs):
                    continue
                
                # Skip very large files (likely minified or generated)
                if file_path.stat().st_size > 100_000:  # 100KB limit
                    continue
                
                try:
                    relative_path = file_path.relative_to(workspace_path)
                    content = file_path.read_text(encoding="utf-8")
                    code_files[str(relative_path)] = content
                except (UnicodeDecodeError, PermissionError) as e:
                    logger.debug(f"Skipping {file_path}: {e}")
                    continue
        
        return code_files

    def _read_full_src_context(self, workspace_dir: str) -> Dict[str, str]:
        """
        Read all source code files from the src/ directory for comprehensive error fixing context.
        
        Returns:
            Dictionary mapping file paths (relative to workspace) to file contents
        """
        code_files = {}
        workspace_path = Path(workspace_dir)
        src_path = workspace_path / "src"
        
        if not src_path.exists():
            logger.warning(f"src/ directory not found at {src_path}")
            return code_files
        
        # Files to include (skip node_modules, dist, .angular, etc.)
        include_patterns = ["*.ts", "*.html", "*.css", "*.scss", "*.json"]
        exclude_dirs = {"node_modules", "dist", ".angular", ".git", "out-tsc"}
        
        for pattern in include_patterns:
            for file_path in src_path.rglob(pattern):
                # Skip excluded directories
                if any(excluded in file_path.parts for excluded in exclude_dirs):
                    continue
                
                # Skip very large files (likely minified or generated)
                if file_path.stat().st_size > 100_000:  # 100KB limit
                    continue
                
                try:
                    relative_path = file_path.relative_to(workspace_path)
                    content = file_path.read_text(encoding="utf-8")
                    code_files[str(relative_path)] = content
                except (UnicodeDecodeError, PermissionError) as e:
                    logger.debug(f"Skipping {file_path}: {e}")
                    continue
        
        logger.info(f"Loaded {len(code_files)} files from src/ directory")
        return code_files

    def _create_workspace(
        self,
        project_id: str,
        screen_id: str,
        version: Optional[str] = None,
        overwrite: bool = True,
    ) -> str:
        """
        Create a unique workspace directory for this generation.
        
        Args:
            project_id: Project identifier
            screen_id: Screen identifier
            overwrite: If True, remove existing workspace. If False, raise error if exists.
        
        Returns:
            Path to workspace directory
        """
        safe_project = re.sub(r"[^a-zA-Z0-9_\-]", "_", project_id)
        safe_screen = re.sub(r"[^a-zA-Z0-9_\-]", "_", screen_id)
        
        if version:
            # Versioned workspace per screen:
            # generated-apps/angular-{project_id}-{screen_id}-v{version}-workspace
            workspace_name = f"angular-{safe_project}-{safe_screen}-v{version}-workspace"
        else:
            # Backward-compatible fallback (single workspace per screen)
            workspace_name = f"angular-{safe_project}-{safe_screen}-workspace"
        
        workspace_dir = self.generated_apps_root / workspace_name
        
        if workspace_dir.exists():
            if overwrite:
                logger.warning(f"Removing existing workspace: {workspace_dir}")
                shutil.rmtree(workspace_dir)
            else:
                logger.info(f"Workspace already exists: {workspace_dir} (not overwriting)")
                return str(workspace_dir)
        
        workspace_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Workspace created: {workspace_dir}")
        return str(workspace_dir)

    def _copy_base_project(self, workspace_dir: str) -> None:
        """Copy the base Angular project to the workspace."""
        if not os.path.exists(self.base_project_path):
            raise FileNotFoundError(
                f"Base project not found at {self.base_project_path}. "
                "Please ensure angular-base-project/ exists in the project root."
            )
        
        logger.info(f"Copying base project from {self.base_project_path} to {workspace_dir}")
        shutil.copytree(
            self.base_project_path,
            workspace_dir,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns('node_modules', '.angular', 'dist')
        )
        logger.info("Base project copied successfully.")

    def _write_environment_ts(
        self, workspace_dir: str, *, project_id: Optional[str] = None
    ) -> None:
        """Write src/environments/environment.ts with apiBaseUrl, agentBasePath, and projectId. No secrets."""
        env_dir = os.path.join(workspace_dir, "src", "environments")
        os.makedirs(env_dir, exist_ok=True)
        # Use api_agent_base_url from config.json as-is (it may already contain any prefix like /dev)
        api_base = (self.api_base_url or "http://localhost:8000/dev").rstrip("/")
        agent_path = (self.agent_base_path or "/aiqod-agent/agent").rstrip("/")
        resolved_project_id = (project_id or "").strip()
        if not resolved_project_id:
            logger.error(
                "Writing environment.ts with empty projectId — dynamic execute URLs will be wrong"
            )
        # Escape for TS string literal
        safe_project_id = resolved_project_id.replace("\\", "\\\\").replace('"', '\\"')
        content = f'''export const environment = {{
  production: false,
  apiBaseUrl: "{api_base}",
  agentBasePath: "{agent_path}",
  projectId: "{safe_project_id}",
}};
'''
        path = os.path.join(workspace_dir, "src", "environments", "environment.ts")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(
            "Wrote src/environments/environment.ts (apiBaseUrl=%s, agentBasePath=%s, projectId=%s)",
            api_base,
            agent_path,
            resolved_project_id,
        )

    @staticmethod
    def _is_protected_generated_path(path: str) -> bool:
        cleaned = path.strip().lstrip("/")
        return cleaned in PROTECTED_GENERATED_PATHS

    def _merge_generated_files(self, workspace_dir: str, files: List[GeneratedFile]) -> None:
        """Merge LLM-generated files into the workspace, overwriting base files as needed."""
        writable_files = [f for f in files if not self._is_protected_generated_path(f.path)]
        skipped = len(files) - len(writable_files)
        if skipped:
            logger.warning(
                "Skipped merging %s protected file(s): main.ts and screen-runtime.ts are platform-owned",
                skipped,
            )
        for generated_file in writable_files:
            absolute_path = os.path.join(workspace_dir, generated_file.path)
            parent_dir = os.path.dirname(absolute_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)

            # Double-clean the content to ensure no markdown fences slip through
            cleaned_content = self._clean_generated_content(generated_file.content)
            
            # Additional validation for common issues
            if cleaned_content.strip().startswith('```'):
                logger.warning(f"Detected markdown fence in {generated_file.path}, stripping...")
                cleaned_content = re.sub(r'^```[a-zA-Z]*\s*\n?', '', cleaned_content, flags=re.MULTILINE)
                cleaned_content = re.sub(r'\n?```\s*$', '', cleaned_content, flags=re.MULTILINE)
            
            logger.debug(f"Writing/overwriting file: {absolute_path}")
            with open(absolute_path, "w", encoding="utf-8") as file:
                file.write(cleaned_content.rstrip() + "\n")
        
        logger.info(f"Merged {len(writable_files)} generated files into workspace.")

    def _apply_metadata(self, workspace_dir: str, metadata: Dict[str, Any]) -> None:
        """Apply metadata changes: update package.json, routes, app.config.ts."""
        # Update package.json with new dependencies
        all_dependencies = []
        if metadata.get("dependencies"):
            all_dependencies.extend(metadata["dependencies"])
        if metadata.get("devDependencies"):
            # Convert devDependencies to dependencies format with dev flag
            for dev_dep in metadata["devDependencies"]:
                if isinstance(dev_dep, dict):
                    dev_dep = dev_dep.copy()
                    dev_dep["dev"] = True
                all_dependencies.append(dev_dep)
        
        if all_dependencies:
            self._update_package_json(workspace_dir, all_dependencies)
        
        # Update app.config.ts with new providers
        if metadata.get("providers"):
            self._update_app_config(workspace_dir, metadata["providers"])
        
        logger.info("Metadata applied successfully.")

    def _update_package_json(self, workspace_dir: str, dependencies: List[Dict[str, str]]) -> None:
        """Add new dependencies to package.json."""
        package_json_path = os.path.join(workspace_dir, "package.json")
        if not os.path.exists(package_json_path):
            logger.warning("package.json not found, skipping dependency updates")
            return
        
        with open(package_json_path, "r", encoding="utf-8") as f:
            package_data = json.load(f)
        
        if "dependencies" not in package_data:
            package_data["dependencies"] = {}
        if "devDependencies" not in package_data:
            package_data["devDependencies"] = {}
        
        for dep in dependencies:
            dep_name = dep.get("name")
            dep_version = dep.get("version", "latest")
            is_dev = dep.get("dev", False) or dep_name.startswith("@types/")
            
            if dep_name:
                target_dict = package_data["devDependencies"] if is_dev else package_data["dependencies"]
                target_dict[dep_name] = dep_version
                logger.info(f"Added {'dev ' if is_dev else ''}dependency: {dep_name}@{dep_version}")
        
        with open(package_json_path, "w", encoding="utf-8") as f:
            json.dump(package_data, f, indent=2)
        
        logger.info("package.json updated with new dependencies.")

    def _update_app_config(self, workspace_dir: str, providers: List[Dict[str, str]]) -> None:
        """Add new providers to app.config.ts."""
        app_config_path = os.path.join(workspace_dir, "src/app/app.config.ts")
        if not os.path.exists(app_config_path):
            logger.warning("app.config.ts not found, skipping provider updates")
            return
        
        # Read current config
        with open(app_config_path, "r", encoding="utf-8") as f:
            config_content = f.read()
        
        # Simple approach: append providers to the providers array
        # This is a basic implementation - could be more sophisticated
        for provider_info in providers:
            provider_code = provider_info.get("provider")
            if provider_code:
                # Check if provider already exists
                if provider_code not in config_content:
                    # Find the providers array and add the new provider
                    # This is a simple regex-based approach
                    config_content = re.sub(
                        r'(providers:\s*\[)',
                        r'\1' + f"\n    {provider_code},",
                        config_content,
                        count=1
                    )
                    logger.info(f"Added provider: {provider_code}")
        
        with open(app_config_path, "w", encoding="utf-8") as f:
            f.write(config_content)
        
        logger.info("app.config.ts updated with new providers.")

    def _post_process_components(self, workspace_dir: str) -> None:
        """Apply fixes to generated component files."""
        src_root = Path(workspace_dir) / "src" / "app"
        if not src_root.exists():
            return

        for ts_file in src_root.rglob("*.ts"):
            try:
                content = ts_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue

            updated_content = self._ensure_common_module(content)
            updated_content = self._fix_aos_import(updated_content)
            updated_content = self._strip_llm_window_global_declarations(updated_content)
            if updated_content != content:
                ts_file.write_text(updated_content, encoding="utf-8")

        for html_file in src_root.rglob("*.html"):
            try:
                content = html_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue

            updated_content = self._fix_ng_for_track_syntax(content)
            updated_content = self._fix_aria_attribute_bindings(updated_content)
            if updated_content != content:
                html_file.write_text(updated_content, encoding="utf-8")

    @staticmethod
    def _bootstrap_window_property_key(screen_id: str) -> str:
        """Window property for orchestrator mount: bootstrap_<screen_id> (may contain hyphens)."""
        return f"bootstrap_{screen_id}"

    @staticmethod
    def _balanced_brace_block_end(text: str, open_brace_index: int) -> int:
        """Return index after the closing brace matching text[open_brace_index] == '{'."""
        depth = 0
        for idx in range(open_brace_index, len(text)):
            char = text[idx]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return idx + 1
        return len(text)

    @classmethod
    def _strip_llm_window_global_declarations(cls, content: str) -> str:
        """
        Remove LLM-added Window/__AIQOD_SCREEN__ augmentations.
        Canonical types live only in screen-runtime.ts.
        """
        if "__AIQOD_SCREEN__" not in content:
            return content

        patterns = (
            re.compile(r"\bdeclare\s+global\s*\{", re.MULTILINE),
            re.compile(r"\binterface\s+Window\s*\{", re.MULTILINE),
        )
        updated = content
        changed = True
        while changed:
            changed = False
            for pattern in patterns:
                match = pattern.search(updated)
                while match:
                    block_start = match.start()
                    open_brace = updated.find("{", match.end() - 1)
                    if open_brace < 0:
                        break
                    block_end = cls._balanced_brace_block_end(updated, open_brace)
                    block = updated[block_start:block_end]
                    if "__AIQOD_SCREEN__" not in block:
                        break
                    updated = updated[:block_start] + updated[block_end:]
                    changed = True
                    match = pattern.search(updated, block_start)

        if updated != content:
            updated = re.sub(r"\n{3,}", "\n\n", updated)
            return updated.strip() + "\n"
        return content

    def _strip_duplicate_window_augmentations_from_app(
        self, workspace_dir: str,
    ) -> List[str]:
        """Strip LLM Window/__AIQOD_SCREEN__ declarations from app sources. Returns changed paths."""
        src_app = Path(workspace_dir) / "src" / "app"
        if not src_app.exists():
            return []

        changed_paths: List[str] = []
        for ts_file in src_app.rglob("*.ts"):
            try:
                content = ts_file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            updated = self._strip_llm_window_global_declarations(content)
            if updated != content:
                ts_file.write_text(updated, encoding="utf-8")
                changed_paths.append(
                    str(ts_file.relative_to(workspace_dir)).replace("\\", "/"),
                )
        if changed_paths:
            logger.info(
                "Removed duplicate __AIQOD_SCREEN__ Window declarations from: %s",
                ", ".join(changed_paths),
            )
        return changed_paths

    def _generate_main_ts_content(
        self,
        screen_id: str,
        ir_schema: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Canonical main.ts: dev auto-bootstrap + orchestrator window.bootstrap_<screen_id>.
        Always rewritten so LLM/fix passes cannot leave dangling appRef references.
        """
        bootstrap_key = self._bootstrap_window_property_key(screen_id)
        bootstrap_key_js = json.dumps(bootstrap_key)
        selector = "app-root"
        if ir_schema:
            screen = ir_schema.get("screen", {})
            selector = screen.get("selector", "app-root")
        screen_id_js = json.dumps(screen_id)

        return f"""import {{ bootstrapApplication }} from '@angular/platform-browser';
import {{ AppComponent }} from './app/app.component';
import {{ appConfig }} from './app/app.config';
import './screen-runtime';

// Helper: ensure the root component element exists in the DOM before bootstrapping.
// This prevents NG05104 (MissingRootElement) when index.html does not contain
// the component selector (e.g. when loaded via orchestrator or after build).
function ensureRootElement(selector: string): void {{
  if (!document.querySelector(selector)) {{
    const el = document.createElement(selector);
    // Prefer #screen-root if it exists, otherwise append to body
    const screenRoot = document.getElementById('screen-root');
    if (screenRoot) {{
      screenRoot.appendChild(el);
    }} else {{
      document.body.appendChild(el);
    }}
  }}
}}

// Auto-bootstrap for development (ng serve)
ensureRootElement('{selector}');
bootstrapApplication(AppComponent, appConfig)
  .then((appRef) => {{
    (window as any).__SCREEN_REF__ = appRef;
    if ((window as any).__AIQOD_SCREEN__) {{
      (window as any).__AIQOD_SCREEN__.setScreenId({screen_id_js});
    }}
  }})
  .catch((err) => console.error('Bootstrap error:', err));

// Export bootstrap function for DOM-based workflow loading (bracket key: screen ids may contain hyphens)
(window as any)[{bootstrap_key_js}] = () => {{
  if ((window as any).__SCREEN_REF__) {{
    if ((window as any).__AIQOD_SCREEN__) {{
      (window as any).__AIQOD_SCREEN__.setScreenId({screen_id_js});
    }}
    return Promise.resolve((window as any).__SCREEN_REF__);
  }}

  ensureRootElement('{selector}');

  return bootstrapApplication(AppComponent, appConfig)
    .then((appRef) => {{
      (window as any).__SCREEN_REF__ = appRef;
      if ((window as any).__AIQOD_SCREEN__) {{
        (window as any).__AIQOD_SCREEN__.setScreenId({screen_id_js});
      }}
      return appRef;
    }})
    .catch((err) => {{
      console.error('Bootstrap error:', err);
      throw err;
    }});
}};
"""

    def _mirror_platform_injected_files(
        self,
        workspace_dir: str,
        isolated_dir: str,
    ) -> None:
        """Keep isolated build tree in sync with deterministic platform files."""
        self._mirror_files_to_directory(
            workspace_dir,
            isolated_dir,
            ["src/main.ts", "src/screen-runtime.ts"],
        )

    def _inject_screen_runtime(
        self,
        workspace_dir: str,
        screen_id: str,
        ir_schema: Optional[Dict[str, Any]] = None,
        isolated_dir: Optional[str] = None,
    ) -> None:
        """
        Inject screen runtime and write canonical main.ts for workflow loading.

        1. screen-runtime.ts — workflow orchestration API
        2. main.ts — deterministic bootstrap (overwrites any LLM output)
        3. index.html — ensure component selector for ng serve
        """
        # Remove LLM duplicate Window types before injecting canonical runtime types
        self._strip_duplicate_window_augmentations_from_app(workspace_dir)

        # Step 1: Generate and write screen-runtime.ts
        runtime_content = self._generate_screen_runtime(screen_id)
        runtime_path = Path(workspace_dir) / "src" / "screen-runtime.ts"
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_path.write_text(runtime_content, encoding="utf-8")
        logger.info(f"Injected screen-runtime.ts at {runtime_path}")
        
        # Step 2: Write deterministic main.ts (platform-owned; avoids broken appRef from LLM edits)
        main_ts_path = Path(workspace_dir) / "src" / "main.ts"
        main_ts_path.parent.mkdir(parents=True, exist_ok=True)
        main_ts_path.write_text(
            self._generate_main_ts_content(screen_id, ir_schema),
            encoding="utf-8",
        )
        logger.info("Wrote deterministic main.ts at %s", main_ts_path)

        if isolated_dir:
            self._mirror_platform_injected_files(workspace_dir, isolated_dir)

        # Standalone-only: remove NgModule artifact if the LLM generated one
        app_module_path = Path(workspace_dir) / "src" / "app" / "app.module.ts"
        if app_module_path.exists():
            logger.info("Removing app.module.ts (standalone bootstrap uses AppComponent + appConfig)")
            try:
                app_module_path.unlink()
            except OSError as exc:
                logger.warning("Failed to remove app.module.ts: %s", exc)

        # Step 3: Ensure index.html has the component selector for standalone development
        # The orchestrator will provide its own container (screen-root) when loading dynamically
        # For standalone dev (ng serve), we need the component selector in index.html
        # NOTE: _update_index_html will handle the selector update, so we just ensure no duplicates here
        index_html_path = Path(workspace_dir) / "src" / "index.html"
        if index_html_path.exists():
            try:
                index_content = index_html_path.read_text(encoding="utf-8")
                
                # Get selector from IR schema or use default
                selector = "app-root"
                if ir_schema:
                    screen = ir_schema.get("screen", {})
                    selector = screen.get("selector", "app-root")
                
                # Count occurrences of the selector to prevent duplicates
                # Match both opening and self-closing tags
                selector_pattern = rf"<{re.escape(selector)}(?:\s[^>]*)?(?:>|/>)"
                closing_pattern = rf"</{re.escape(selector)}>"
                
                opening_matches = list(re.finditer(selector_pattern, index_content))
                closing_matches = list(re.finditer(closing_pattern, index_content))
                total_instances = len(opening_matches)
                
                if total_instances == 0:
                    # Component selector not found, add it before closing body tag
                    index_content = re.sub(
                        r"</body>",
                        f'  <{selector}></{selector}>\n</body>',
                        index_content
                    )
                    index_html_path.write_text(index_content, encoding="utf-8")
                    logger.info(f"Added component selector <{selector}> to index.html for standalone development")
                elif total_instances > 1:
                    # Duplicate found - remove all and add one
                    logger.warning(f"Found {total_instances} instances of <{selector}> in index.html, removing duplicates")
                    # Remove all opening tags (including self-closing)
                    index_content = re.sub(selector_pattern, "", index_content)
                    # Remove all closing tags
                    index_content = re.sub(closing_pattern, "", index_content)
                    # Add single instance before closing body tag
                    index_content = re.sub(
                        r"</body>",
                        f'  <{selector}></{selector}>\n</body>',
                        index_content
                    )
                    index_html_path.write_text(index_content, encoding="utf-8")
                    logger.info(f"Removed duplicates and added single <{selector}> to index.html")
                else:
                    logger.debug(f"index.html already contains component selector <{selector}> (single instance)")
                    
            except Exception as e:
                logger.warning(f"Failed to update index.html: {e}")

    def _update_index_html(self, workspace_dir: str, ir_schema: Optional[Dict[str, Any]] = None) -> None:
        """
        Update index.html to use the component selector from IR schema.
        
        Replaces <app-root></app-root> with the selector from ir_schema.screen.selector
        (e.g., <app-garnishment-order></app-garnishment-order>).
        
        Args:
            workspace_dir: Path to the workspace directory
            ir_schema: IR schema dictionary containing screen.selector
        """
        if not ir_schema:
            logger.debug("No IR schema provided, skipping index.html selector update")
            return
        
        screen = ir_schema.get("screen", {})
        selector = screen.get("selector")
        
        if not selector:
            logger.debug("No selector found in IR schema, skipping index.html update")
            return
        
        index_html_path = Path(workspace_dir) / "src" / "index.html"
        if not index_html_path.exists():
            logger.warning(f"index.html not found at {index_html_path}, skipping update")
            return
        
        try:
            content = index_html_path.read_text(encoding="utf-8")
            
            # Check for duplicates first - use a more comprehensive pattern
            # Match both opening and self-closing tags
            selector_pattern = rf"<{re.escape(selector)}(?:\s[^>]*)?(?:>|/>)"
            closing_pattern = rf"</{re.escape(selector)}>"
            
            # Find all matches (opening tags, self-closing tags, and closing tags)
            opening_matches = list(re.finditer(selector_pattern, content))
            closing_matches = list(re.finditer(closing_pattern, content))
            total_instances = len(opening_matches)
            
            if total_instances > 1:
                logger.warning(f"Found {total_instances} duplicate instances of <{selector}> in index.html, removing duplicates")
                # Remove all opening tags (including self-closing)
                content = re.sub(selector_pattern, "", content)
                # Remove all closing tags
                content = re.sub(closing_pattern, "", content)
                # Now add a single instance
                content = re.sub(
                    r"</body>",
                    f'  <{selector}></{selector}>\n</body>',
                    content
                )
                index_html_path.write_text(content, encoding="utf-8")
                logger.info(f"Removed duplicates and added single <{selector}> to index.html")
                return
            
            # Check for duplicates first - use a more comprehensive pattern
            # Match both opening and self-closing tags
            selector_pattern = rf"<{re.escape(selector)}(?:\s[^>]*)?(?:>|/>)"
            closing_pattern = rf"</{re.escape(selector)}>"
            
            # Find all matches (opening tags, self-closing tags, and closing tags)
            opening_matches = list(re.finditer(selector_pattern, content))
            closing_matches = list(re.finditer(closing_pattern, content))
            total_instances = len(opening_matches)
            
            if total_instances > 1:
                logger.warning(f"Found {total_instances} duplicate instances of <{selector}> in index.html, removing duplicates")
                # Remove all opening tags (including self-closing)
                content = re.sub(selector_pattern, "", content)
                # Remove all closing tags
                content = re.sub(closing_pattern, "", content)
                # Now add a single instance
                content = re.sub(
                    r"</body>",
                    f'  <{selector}></{selector}>\n</body>',
                    content
                )
                index_html_path.write_text(content, encoding="utf-8")
                logger.info(f"Removed duplicates and added single <{selector}> to index.html")
                return
            
            # Replace <app-root></app-root> with the new selector
            # Handle both self-closing and separate tags
            old_patterns = [
                r"<app-root></app-root>",
                r"<app-root\s*/>",
                r"<app-root\s[^>]*>",
            ]
            
            new_tag = f"<{selector}></{selector}>"
            updated = False
            
            for pattern in old_patterns:
                if re.search(pattern, content):
                    content = re.sub(pattern, new_tag, content)
                    updated = True
                    logger.info(f"Updated index.html: replaced {pattern} with {new_tag}")
            
            # If selector not found and not already present, add it
            if not updated and total_instances == 0:
                content = re.sub(
                    r"</body>",
                    f'  <{selector}></{selector}>\n</body>',
                    content
                )
                updated = True
                logger.info(f"Added component selector <{selector}> to index.html")
            
            # If selector not found and not already present, add it
            if not updated and total_instances == 0:
                content = re.sub(
                    r"</body>",
                    f'  <{selector}></{selector}>\n</body>',
                    content
                )
                updated = True
                logger.info(f"Added component selector <{selector}> to index.html")
            
            if updated:
                index_html_path.write_text(content, encoding="utf-8")
                logger.info(f"Successfully updated index.html with selector: {selector}")
            else:
                logger.debug(f"index.html already contains <{selector}> (single instance), skipping update")
                logger.debug(f"index.html already contains <{selector}> (single instance), skipping update")
                
        except (UnicodeDecodeError, PermissionError, IOError) as e:
            logger.warning(f"Failed to update index.html: {e}")

    def _ensure_common_module(self, content: str) -> str:
        """Add CommonModule to imports when structural directives or pipes are detected."""
        if "CommonModule" in content:
            return content

        needs_common = any(marker in content for marker in ["ngClass", "| number", "'number'"])
        if not needs_common:
            return content

        import_match = re.search(r"imports\s*:\s*\[([^\]]*)\]", content)
        if not import_match:
            return content

        imports_content = import_match.group(1)
        updated_imports = f"CommonModule, {imports_content}".strip().replace(", ,", ",")

        new_content = re.sub(
            r"(imports\s*:\s*\[)([^\]]*)(\])",
            lambda m: f"{m.group(1)}{self._normalize_import_list(updated_imports)}{m.group(3)}",
            content,
            count=1,
        )

        if "CommonModule" not in new_content:
            return content

        ts_import_pattern = r"import\s*{([^}]+)}\s*from\s*['\"]@angular/common['\"];"
        if re.search(ts_import_pattern, new_content):
            return new_content

        return f"import {{ CommonModule }} from '@angular/common';\n{new_content}"

    def _fix_aos_import(self, content: str) -> str:
        """
        Fix AOS import to use proper syntax that works with allowSyntheticDefaultImports.
        Converts problematic imports to the correct format.
        """
        # The import `import AOS from 'aos'` should work with allowSyntheticDefaultImports
        # But if there are issues, we can try alternative syntax
        # For now, just ensure the import is there and properly formatted
        if "import AOS from 'aos'" in content or 'import AOS from "aos"' in content:
            # Import looks correct, but ensure it's initialized properly
            # Check if AOS.init() is called in ngOnInit
            if "AOS.init()" not in content and "ngOnInit" in content:
                # Try to add AOS.init() call - but this is complex, so we'll just log
                logger.debug("AOS imported but AOS.init() may be missing")
        
        return content

    def _normalize_import_list(self, imports_str: str) -> str:
        parts = [part.strip() for part in imports_str.split(",") if part.strip()]
        seen = []
        for part in parts:
            if part not in seen:
                seen.append(part)
        return ", ".join(seen)

    def _fix_ng_for_track_syntax(self, content: str) -> str:
        """Fix invalid '@for (...; track $index; let foo = $index)' syntax."""
        pattern = r"track\s+\$index;\s*let\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*\$index"

        def replacer(match: re.Match) -> str:
            var_name = match.group(1)
            return f"let {var_name} = $index; track {var_name}"

        return re.sub(pattern, replacer, content)

    def _fix_aria_attribute_bindings(self, content: str) -> str:
        """
        Fix ARIA attribute bindings that use Angular expressions.
        Converts aria-*="{{expression}}" to [attr.aria-*]="expression"
        and aria-*="static-{{expression}}" to [attr.aria-*]="'static-' + expression"
        
        Examples:
        - aria-controls="faq-answer-{{i}}" -> [attr.aria-controls]="'faq-answer-' + i"
        - aria-labelledby="question-{{question.id}}-label" -> [attr.aria-labelledby]="'question-' + question.id + '-label'"
        """
        # Pattern to match aria-* attributes with Angular interpolation {{...}}
        # Group 1: attribute name (aria-*)
        # Group 2: quote character (" or ')
        # Group 3: prefix text before {{}}
        # Group 4: expression inside {{}}
        # Group 5: suffix text after {{}}
        # Group 6: closing quote (same as group 2)
        aria_pattern = r'\b(aria-[a-z-]+)\s*=\s*(["\'])([^"\']*?)\{\{([^}]+)\}\}([^"\']*?)\2'
        
        def replacer(match: re.Match) -> str:
            attr_name = match.group(1)
            prefix = match.group(3)
            expression = match.group(4).strip()
            suffix = match.group(5)
            
            # Build the expression with proper string concatenation
            parts = []
            if prefix:
                # Escape single quotes in prefix for JavaScript string
                escaped_prefix = prefix.replace("'", "\\'").replace("\\", "\\\\")
                parts.append(f"'{escaped_prefix}'")
            parts.append(expression)
            if suffix:
                # Escape single quotes in suffix for JavaScript string
                escaped_suffix = suffix.replace("'", "\\'").replace("\\", "\\\\")
                parts.append(f"'{escaped_suffix}'")
            
            if len(parts) > 1:
                final_expr = " + ".join(parts)
            else:
                final_expr = expression
            
            return f'[attr.{attr_name}]="{final_expr}"'
        
        updated_content = re.sub(aria_pattern, replacer, content)
        
        return updated_content

    def _set_workspace_base_href(
        self,
        workspace_dir: str,
        screen_id: str,
        version: Optional[str] = None,
    ) -> None:
        """
        Set index.html <base href> in the generated-apps workspace to "./" so assets
        resolve relative to the current URL (e.g. when served at /{screen_id}-v{version}/).
        This runs before copying to isolated-angular-apps so both locations get the
        correct base href automatically.
        """
        base_path = "./"
        index_path = Path(workspace_dir) / "src" / "index.html"
        if not index_path.exists():
            return
        try:
            content = index_path.read_text(encoding="utf-8")
            new_base = '<base href="./">'
            new_content = re.sub(r'<base\s+href="[^"]*"\s*>', new_base, content, count=1)
            if new_content != content:
                index_path.write_text(new_content, encoding="utf-8")
                logger.info("Set workspace index.html base href to ./")
        except (IOError, PermissionError, UnicodeDecodeError) as e:
            logger.warning("Could not set workspace base href: %s", e)

    def _prepare_isolated_directory(
        self,
        workspace_dir: str,
        project_id: str,
        screen_id: str,
        version: Optional[str] = None,
    ) -> str:
        """Copy workspace into an isolated build directory and set permissions."""
        # Use the provided version when available so that build folders can be
        # visually matched with their corresponding workspaces. Fall back to a
        # fresh timestamp when version is not provided (e.g., legacy callers).
        if not version:
            version = datetime.utcnow().strftime("%Y%m%d%H%M%S")

        self._set_workspace_base_href(workspace_dir, screen_id, version)

        safe_project = re.sub(r"[^a-zA-Z0-9_\-]", "_", project_id)
        safe_screen = re.sub(r"[^a-zA-Z0-9_\-]", "_", screen_id)
        # isolated-angular-apps/angular-{project_id}-{screen_id}-v{version}-build
        isolated_name = f"angular-{safe_project}-{safe_screen}-v{version}-build"
        isolated_path = self.isolated_apps_root / isolated_name

        if isolated_path.exists():
            shutil.rmtree(isolated_path)

        shutil.copytree(workspace_dir, isolated_path)
        self._set_permissions(isolated_path)
        logger.info(f"Isolated build directory created at {isolated_path}")
        return str(isolated_path)

    def _set_permissions(self, root_path: Path) -> None:
        """Ensure directories/files have sane permissions (755/644)."""
        for dirpath, dirnames, filenames in os.walk(root_path):
            os.chmod(dirpath, 0o755)
            for dirname in dirnames:
                os.chmod(os.path.join(dirpath, dirname), 0o755)
            for filename in filenames:
                file_path = os.path.join(dirpath, filename)
                try:
                    os.chmod(file_path, 0o644)
                except PermissionError:
                    logger.warning(f"Permission change failed for {file_path}")

    def _install_dependencies(self, isolated_dir: str) -> str:
        """Run npm install inside the isolated directory.
        On ERESOLVE (peer dependency conflict), retries with --legacy-peer-deps
        so that packages like ng2-charts with loose @angular/cdk ranges can install
        alongside the base Angular 17 project.
        """
        try:
            return self._run_command(["npm", "install"], cwd=isolated_dir)
        except RuntimeError as e:
            err_text = str(e)
            if "ERESOLVE" in err_text and "legacy-peer-deps" in err_text:
                logger.warning(
                    "npm install failed with ERESOLVE (peer dependency conflict). "
                    "Retrying with --legacy-peer-deps."
                )
                return self._run_command(
                    ["npm", "install", "--legacy-peer-deps"],
                    cwd=isolated_dir,
                )
            raise

    def _ensure_index_html_has_selector(self, isolated_dir: str, selector: str) -> None:
        """
        Ensure index.html has exactly ONE instance of the root component selector,
        and that any old selector (<app-root>) is removed.
        This is critical to prevent NG05104 (MissingRootElement) at runtime.
        """
        index_html_path = Path(isolated_dir) / "src" / "index.html"
        if not index_html_path.exists():
            logger.warning("index.html not found, cannot ensure root element")
            return
        try:
            content = index_html_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, IOError, PermissionError) as e:
            logger.warning(f"Could not read index.html: {e}")
            return

        # Step 1: Remove ALL Angular component tags (any selector) from the body
        # Match any Angular component-like tag: <something></something> or <something />
        opening_pattern = re.compile(r"<([a-zA-Z][a-zA-Z0-9\-]*)(?:\s[^>]*)?(?:>|/>)")
        closing_pattern = re.compile(r"</([a-zA-Z][a-zA-Z0-9\-]*)>")
        
        opening_matches = list(opening_pattern.finditer(content))
        closing_matches = list(closing_pattern.finditer(content))
        
        # Get all opening tag names found
        tag_names = set()
        for m in opening_matches:
            tag_name = m.group(1)
            # Only remove component-style tags (contain a dash - HTML custom element convention)
            if '-' in tag_name:
                tag_names.add(tag_name)
        
        # Remove all component tags
        for tag_name in tag_names:
            escaped = re.escape(tag_name)
            content = re.sub(rf"<{escaped}(?:\s[^>]*)?(?:>|/>)", "", content)
            content = re.sub(rf"</{escaped}>", "", content)
        
        # Step 2: Add the correct selector once before closing body
        content = re.sub(
            r"</body>",
            f"  <{selector}></{selector}>\n</body>",
            content,
        )
        
        logger.info(
            "Ensured index.html has single instance of <%s> (removed %d old selectors: %s)",
            selector,
            len(tag_names),
            ", ".join(sorted(tag_names)) if tag_names else "none",
        )
        
        try:
            index_html_path.write_text(content, encoding="utf-8")
        except (IOError, PermissionError) as e:
            logger.warning(f"Could not write index.html: {e}")

    def _build_angular_app(self, isolated_dir: str, screen_id: str, version: Optional[str] = None,) -> Tuple[str, str]:
        """Run npm build and return build log plus dist directory."""
        # NOTE: index.html selector is already handled by _update_index_html() and
        # _inject_screen_runtime() before the isolated directory is created.
        # The ensureRootElement() function in main.ts provides a runtime fallback
        # that creates the root element dynamically if index.html is missing it.
        # Do NOT call _ensure_index_html_has_selector() here with screen_id as
        # the selector, because screen_id != component selector from IR schema.
        # Use the production configuration, which is guaranteed to exist in the
        # base Angular workspace (`angular.json`). Some workspaces may not
        # define a "development" configuration, which would cause the build to
        # fail with: "Configuration 'development' is not set in the workspace."
        # base-href ./ so assets resolve relative to the deploy URL (e.g. /screen_id-vversion/).
        build_log = self._run_command(
            ["npx", "ng", "build", "--configuration", "production", "--output-hashing=none", "--base-href", "./"],
            cwd=isolated_dir,
        )

        dist_root = Path(isolated_dir) / "dist"
        if not dist_root.exists():
            raise RuntimeError(f"Angular build did not create dist folder at {dist_root}")

        dist_subdirs = [d for d in dist_root.iterdir() if d.is_dir()]
        if len(dist_subdirs) == 1:
            dist_dir = dist_subdirs[0]
        elif len(dist_subdirs) > 1:
            dist_dir = dist_subdirs[0]
            logger.warning(
                "Multiple dist subdirectories found. Defaulting to %s",
                dist_dir
            )
        else:
            dist_dir = dist_root

        # Ensure built index.html has base href="./" so assets resolve relative to
        # the current URL when app is served at /{screen_id}-v{version}/.
        dist_index = Path(dist_dir) / "index.html"
        if dist_index.exists():
            try:
                content = dist_index.read_text(encoding="utf-8")
                new_base = '<base href="./">'
                new_content = re.sub(r'<base\s+href="[^"]*"\s*>', new_base, content, count=1)
                if new_content != content:
                    dist_index.write_text(new_content, encoding="utf-8")
                    logger.info("Updated built index.html base href to ./ for deploy/iframe")
            except (IOError, PermissionError, UnicodeDecodeError) as e:
                logger.warning("Could not update built index.html base href: %s", e)

        return build_log, str(dist_dir)

    def _mirror_files_to_directory(
        self,
        source_dir: str,
        target_dir: str,
        file_paths: List[str],
    ) -> None:
        """Copy selected files from workspace to isolated directory."""
        for relative_path in file_paths:
            src_path = Path(source_dir) / relative_path
            dest_path = Path(target_dir) / relative_path
            if not src_path.exists():
                logger.warning("Cannot mirror missing file: %s", src_path)
                continue
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dest_path)

    def _configure_strict_typescript(self, workspace_dir: str) -> None:
        """
        Configure TypeScript for maximum error detection.
        Call this in _create_workspace after copying base project.
        """
        tsconfig_app_path = Path(workspace_dir) / "tsconfig.app.json"
        
        if tsconfig_app_path.exists():
            try:
                with open(tsconfig_app_path, "r", encoding="utf-8") as f:
                    tsconfig = json.load(f)
                
                # Add strict compiler options
                if "angularCompilerOptions" not in tsconfig:
                    tsconfig["angularCompilerOptions"] = {}
                
                tsconfig["angularCompilerOptions"].update({
                    "strictTemplates": True,
                    "fullTemplateTypeCheck": True,
                    "strictInjectionParameters": True,
                    "strictInputAccessModifiers": True,
                    "strictAttributeTypes": True,
                })
                
                # Also enforce strict TypeScript
                if "compilerOptions" not in tsconfig:
                    tsconfig["compilerOptions"] = {}
                
                tsconfig["compilerOptions"].update({
                    "strict": True,
                    "noImplicitAny": True,
                    "strictNullChecks": True,
                    "strictFunctionTypes": True,
                    "strictPropertyInitialization": True,
                })
                
                with open(tsconfig_app_path, "w", encoding="utf-8") as f:
                    json.dump(tsconfig, f, indent=2)
                
                logger.info("Configured strict TypeScript checking")
            except Exception as e:
                logger.warning(f"Failed to configure strict TypeScript: {e}")

    def _run_static_analysis(
        self, 
        isolated_dir: str,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Tuple[bool, List[Dict[str, Any]]]:
        """
        Run TypeScript compiler in noEmit mode to catch all errors without building.
        This is faster than full build and catches all errors in one pass.
        
        Args:
            isolated_dir: Path to isolated build directory
            project_id: Project identifier for tracking
            screen_id: Screen identifier for tracking
            version: Version identifier for tracking
        
        Returns:
            (is_valid, list_of_errors)
        """
        logger.info("Running static analysis (tsc --noEmit)...")
        
        try:
            # Run TypeScript compiler in check-only mode
            result = subprocess.run(
                ["npx", "tsc", "--noEmit", "--pretty", "false"],
                cwd=isolated_dir,
                capture_output=True,
                text=True,
                timeout=120  # Static analysis should be fast
            )
            
            output = (result.stdout or "") + (result.stderr or "")
            
            if result.returncode == 0:
                logger.info("Static analysis passed - no TypeScript errors found")
                return True, []
            
            # Parse TypeScript errors
            errors = self._extract_compiler_errors(
                output, 
                limit=100,  # Higher limit for static analysis
                project_id=project_id,
                screen_id=screen_id,
                version=version,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
            )
            logger.info(f"Static analysis found {len(errors)} TypeScript errors")
            
            return False, errors
            
        except subprocess.TimeoutExpired:
            logger.warning("Static analysis timed out - falling back to build-based error detection")
            return True, []  # Continue to build
        except Exception as e:
            logger.warning(f"Static analysis failed: {e} - falling back to build-based error detection")
            return True, []  # Continue to build

    def _build_with_recovery(
        self,
        *,
        workspace_dir: str,
        isolated_dir: str,
        user_prompt: str,
        project_id: str,
        screen_id: str,
        ir_schema: Optional[Dict[str, Any]] = None,
        backend_metadata: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable] = None, 
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Tuple[str, str, List[Dict[str, Any]]]:
        """
        Attempt Angular build with iterative LLM-powered recovery of compiler errors.
        Broadcasts retry events to the frontend.
        """
        attempts_log: List[Dict[str, Any]] = []

        # STEP 1: TypeScript-only validation & fix (tsc --noEmit)
        logger.info("Phase 1: Running static analysis to detect ts errors...")
        version = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        static_valid, static_errors = self._run_static_analysis(
            isolated_dir,
            project_id=project_id,
            screen_id=screen_id,
            version=version,
            org_id=org_id,
            subscriber_id=subscriber_id,
            user_id=user_id,
        )
        
        if not static_valid and static_errors:
            logger.info(f"Static analysis found {len(static_errors)} errors - attempting fix before build...")

            protected_only = all(
                (e.get("file") or "").lstrip("/") in PROTECTED_GENERATED_PATHS
                for e in static_errors
            )
            if protected_only:
                logger.info(
                    "Static errors only reference platform/runtime types; "
                    "stripping duplicate Window declarations and re-injecting platform files.",
                )
                stripped_paths = self._strip_duplicate_window_augmentations_from_app(workspace_dir)
                self._inject_screen_runtime(
                    workspace_dir, screen_id, ir_schema, isolated_dir=isolated_dir,
                )
                if stripped_paths:
                    self._mirror_files_to_directory(workspace_dir, isolated_dir, stripped_paths)
                static_valid = True
                static_errors = []

            if not protected_only:
                attempt_entry = {
                    "attempt": 0,
                    "phase": "static_analysis",
                    "errors": static_errors,
                }
                attempts_log.append(attempt_entry)

                try:
                    fix_files, fix_metadata = self._run_fix_iteration(
                        user_prompt=user_prompt,
                        workspace_dir=workspace_dir,
                        parsed_errors=static_errors,
                        attempt=0,
                        ir_schema=ir_schema,
                        backend_metadata=backend_metadata,
                        project_id=project_id,
                        screen_id=screen_id,
                        org_id=org_id,
                        subscriber_id=subscriber_id,
                        user_id=user_id,
                    )

                    if fix_files:
                        self._merge_generated_files(workspace_dir, fix_files)
                        logger.info("Re-injecting screen-runtime.ts to ensure it remains unmodified...")
                        self._inject_screen_runtime(
                            workspace_dir, screen_id, ir_schema, isolated_dir=isolated_dir,
                        )

                        self._mirror_files_to_directory(
                            workspace_dir,
                            isolated_dir,
                            [f.path for f in fix_files],
                        )

                        attempt_entry["fixed_files"] = [f.path for f in fix_files]
                        attempt_entry["fix_metadata"] = fix_metadata

                        logger.info("Applied static analysis fixes, proceeding to build...")

                except ValueError as fix_exc:
                    logger.warning(f"Static analysis fix failed: {fix_exc}")
                    # Continue to build anyway

        # STEP 2: Now attempt the actual build with retry logic

        total_attempts = self.max_fix_iterations + 1
        
        for attempt in range(total_attempts):
            current_attempt_num = attempt + 1
            logger.info(f"Angular build attempt {current_attempt_num}/{total_attempts} starting...")
            
            if progress_callback:
                if current_attempt_num == 1:
                    progress_callback("INFO", "Compiling Angular application...")
                else:
                    progress_callback("RETRY", f"Retrying compilation...", {"attempt": current_attempt_num, "max": total_attempts})

            try:
                build_log, dist_dir = self._build_angular_app(isolated_dir, screen_id = screen_id, version = version)
                logger.info(f"Angular build attempt {current_attempt_num} succeeded.")
                return build_log, dist_dir, attempts_log
                
            except RuntimeError as build_error:
                error_text = str(build_error)
                parsed_errors = self._extract_compiler_errors(
                    error_text,
                    project_id=project_id,
                    screen_id=screen_id,
                    version=version,
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                    user_id=user_id,
                )
                
                # Format error for display
                short_error = "Unknown error"
                if parsed_errors:
                    short_error = f"{len(parsed_errors)} compiler errors found."
                
                attempt_entry = {
                    "attempt": current_attempt_num,
                    "errors": parsed_errors,
                    "raw_error": self._truncate_error_log(error_text),
                }
                attempts_log.append(attempt_entry)
                
                logger.error(f"Angular build attempt {current_attempt_num} failed.")
                self._log_primary_error(parsed_errors)

                if attempt >= self.max_fix_iterations:
                    if progress_callback:
                        progress_callback("ERROR", "Build failed after maximum recovery attempts.")
                    raise AngularBuildError(
                        "Angular build failed after automated recovery attempts",
                        attempts_log,
                    ) from build_error

                # Notify frontend about the fix attempt
                if progress_callback:
                    progress_callback("RETRY", f"Build failed. AI is fixing code...", {"attempt": current_attempt_num, "error": short_error})

                try:
                    fix_files, fix_metadata = self._run_fix_iteration(
                        user_prompt=user_prompt,
                        workspace_dir=workspace_dir,
                        parsed_errors=parsed_errors,
                        attempt=current_attempt_num,
                        ir_schema=ir_schema,  
                        backend_metadata=backend_metadata, 
                        project_id=project_id,
                        screen_id=screen_id,
                        org_id=org_id,
                        subscriber_id=subscriber_id,
                        user_id=user_id,
                    )
                except ValueError as fix_exc:
                    attempt_entry["fix_error"] = str(fix_exc)
                    raise AngularBuildError(
                        "LLM fixer failed to produce a valid <files> bundle",
                        attempts_log,
                    ) from fix_exc

                if not fix_files:
                    raise AngularBuildError(
                        "LLM fixer returned no files to apply",
                        attempts_log,
                    ) from build_error

                self._merge_generated_files(workspace_dir, fix_files)
                
                logger.info("Re-injecting screen-runtime.ts to ensure it remains unmodified...")
                self._inject_screen_runtime(
                    workspace_dir, screen_id, ir_schema, isolated_dir=isolated_dir,
                )
                
                self._mirror_files_to_directory(
                    workspace_dir,
                    isolated_dir,
                    [f.path for f in fix_files],
                )
                
                attempt_entry["fixed_files"] = [f.path for f in fix_files]
                attempt_entry["fix_metadata"] = fix_metadata
                self._log_file_updates(fix_files, workspace_dir, isolated_dir, fix_metadata)

        raise AngularBuildError(
            "Angular build failed with unknown issue",
            attempts_log,
        )

    def _upload_build(self, dist_dir: str, screen_id: str, version: Optional[str] = None) -> str:
        """
        Copy build artifacts to Liberty FS directory in versioned folders.
        Creates/updates a symlink at {screen_id} pointing to the active version.
        """
        if not version:
            version = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        
        # Store dist files in flat versioned directory: LIBERTY_FS_ROOT/{screen_id}-v{version}/
        versioned_dir = self.deploy_root / f"{screen_id}-v{version}"
        
        if versioned_dir.exists():
            shutil.rmtree(versioned_dir)
        
        self.deploy_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(dist_dir, versioned_dir)
        self._set_permissions(versioned_dir)
        logger.info(f"Copied dist files to versioned directory: {versioned_dir}")
        
        # Create/update symlink {screen_id} -> {screen_id}-v{version} so public_url/{screen_id} serves the active version
        current_link = self.deploy_root / screen_id
        if current_link.exists() or current_link.is_symlink():
            if current_link.is_symlink():
                current_link.unlink()
            else:
                shutil.rmtree(current_link)
        
        # Create symlink: {screen_id} -> {screen_id}-v{version}
        relative_target = f"{screen_id}-v{version}"
        current_link.symlink_to(relative_target)
        logger.info(f"Created symlink: {current_link} -> {relative_target}")
        
        return str(versioned_dir)

    def _build_public_url(self, screen_id: str, version: Optional[str] = None) -> str:
        """
        Build public URL.
        
        - If a specific version is provided, return {base}/{screen_id}-v{version}
        - Otherwise, return the stable {base}/{screen_id} URL (for legacy callers)
        """
        base = self.deploy_base_url.rstrip("/")
        if version:
            return f"{base}/{screen_id}-v{version}"
        return f"{base}/{screen_id}"

    def remove_versioned_artifacts(
        self, project_id: str, screen_id: str, versions: List[str]
    ) -> Dict[str, Any]:
        """
        Remove workspace, build, and deploy directories for the given versions (rollback cleanup).
        Tries delete; logs and continues on permission errors (e.g. server has no delete access).
        Returns dict with removed_count and errors.
        """
        safe_project = re.sub(r"[^a-zA-Z0-9_\-]", "_", project_id)
        safe_screen = re.sub(r"[^a-zA-Z0-9_\-]", "_", screen_id)
        removed = 0
        errors = []

        for version in versions:
            # Workspace: generated-apps/angular-{project_id}-{screen_id}-v{version}-workspace
            workspace_dir = self.generated_apps_root / f"angular-{safe_project}-{safe_screen}-v{version}-workspace"
            if workspace_dir.exists():
                try:
                    shutil.rmtree(workspace_dir)
                    removed += 1
                    logger.info(f"Removed workspace for rollback: {workspace_dir.name}")
                except Exception as e:
                    errors.append(f"workspace {version}: {e}")
                    logger.warning(f"Could not remove workspace {workspace_dir}: {e}")

            # Build: isolated-angular-apps/angular-{project_id}-{screen_id}-v{version}-build
            build_dir = self.isolated_apps_root / f"angular-{safe_project}-{safe_screen}-v{version}-build"
            if build_dir.exists():
                try:
                    shutil.rmtree(build_dir)
                    removed += 1
                    logger.info(f"Removed build dir for rollback: {build_dir.name}")
                except Exception as e:
                    errors.append(f"build {version}: {e}")
                    logger.warning(f"Could not remove build dir {build_dir}: {e}")

            # Deploy: LIBERTY_FS_ROOT/{screen_id}-v{version} (match _upload_build naming)
            deploy_dir = self.deploy_root / f"{screen_id}-v{version}"
            if deploy_dir.exists() and not deploy_dir.is_symlink():
                try:
                    shutil.rmtree(deploy_dir)
                    removed += 1
                    logger.info(f"Removed deploy dir for rollback: {deploy_dir.name}")
                except Exception as e:
                    errors.append(f"deploy {version}: {e}")
                    logger.warning(f"Could not remove deploy dir {deploy_dir}: {e}")

        return {"removed_count": removed, "errors": errors}

    def update_deploy_symlink(self, screen_id: str, version: str) -> bool:
        """
        Update the {screen_id} symlink to point to a specific version's dist files.
        Used for rollback to switch which version is served at public_url/{screen_id}.
        
        Returns:
            True if symlink was updated, False if version directory doesn't exist
        """
        versioned_dir = self.deploy_root / f"{screen_id}-v{version}"
        if not versioned_dir.exists():
            logger.warning(f"Versioned dist directory not found: {versioned_dir}")
            return False
        
        current_link = self.deploy_root / screen_id
        
        # Remove existing symlink or directory
        if current_link.exists() or current_link.is_symlink():
            if current_link.is_symlink():
                current_link.unlink()
            else:
                shutil.rmtree(current_link)
        
        # Create symlink: {screen_id} -> {screen_id}-v{version}
        relative_target = f"{screen_id}-v{version}"
        current_link.symlink_to(relative_target)
        logger.info(f"Updated symlink: {current_link} -> {relative_target} (rollback to version {version})")
        return True

    def _log_generated_bundle(
        self,
        files: List[GeneratedFile],
        metadata: Dict[str, Any],
    ) -> None:
        if not files:
            logger.warning("LLM returned an empty file bundle.")
            return

        sample_paths = [f.path for f in files[:5]]
        suffix = "..." if len(files) > len(sample_paths) else ""
        logger.info(
            "Generated %s files. Sample paths: %s%s",
            len(files),
            ", ".join(sample_paths),
            suffix,
        )

        deps = metadata.get("dependencies") if isinstance(metadata, dict) else None
        dev_deps = metadata.get("devDependencies") if isinstance(metadata, dict) else None
        providers = metadata.get("providers") if isinstance(metadata, dict) else None
        logger.info(
            "Metadata summary - dependencies: %s, devDependencies: %s, providers: %s",
            len(deps) if deps else 0,
            len(dev_deps) if dev_deps else 0,
            len(providers) if providers else 0,
        )

    def _log_primary_error(self, errors: List[Dict[str, Any]]) -> None:
        if not errors:
            logger.error("Compiler output did not provide structured errors.")
            return

        primary = errors[0]
        logger.error(
            "Primary compiler error - file: %s line: %s col: %s code: %s message: %s",
            primary.get("file", "unknown"),
            primary.get("line", "?"),
            primary.get("column", "?"),
            primary.get("code", "?"),
            primary.get("message", "n/a"),
        )

    def _log_file_updates(
        self,
        fix_files: List[GeneratedFile],
        workspace_dir: str,
        isolated_dir: str,
        fix_metadata: Dict[str, Any],
    ) -> None:
        usage = (fix_metadata or {}).get("usage") or {}
        prompt_retry = (fix_metadata or {}).get("prompt_retry")
        logger.info(
            "Applied fix bundle (%s files). Prompt retry #%s, tokens in/out: %s/%s.",
            len(fix_files),
            prompt_retry,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
        )

        for generated_file in fix_files:
            workspace_path = Path(workspace_dir) / generated_file.path
            isolated_path = Path(isolated_dir) / generated_file.path
            workspace_size = workspace_path.stat().st_size if workspace_path.exists() else None
            isolated_size = isolated_path.stat().st_size if isolated_path.exists() else None
            logger.info(
                "Updated file %s -> workspace bytes: %s, isolated bytes: %s",
                generated_file.path,
                workspace_size,
                isolated_size,
            )

    def _run_fix_iteration(
        self,
        *,
        user_prompt: str,
        workspace_dir: str,
        parsed_errors: List[Dict[str, Any]],
        attempt: int,
        ir_schema: Optional[Dict[str, Any]] = None,  
        backend_metadata: Optional[Dict[str, Any]] = None,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Tuple[List[GeneratedFile], Dict[str, Any]]:
        """Request the LLM to fix specific compiler errors."""
        file_context = self._collect_error_file_context(workspace_dir, parsed_errors, use_full_context=self.use_full_context_for_fixes )
        
        fix_prompt = self._build_fix_prompt(
            user_prompt=user_prompt,
            errors=parsed_errors,
            file_context=file_context,
            attempt=attempt,
            ir_schema=ir_schema,
            backend_metadata=backend_metadata,
        )

        last_exception: Optional[Exception] = None
        for prompt_retry in range(1, self.max_fix_prompt_retries + 1):
            raw_response, finish_reason, safety_ratings, usage = self._invoke_model(
                fix_prompt,
                model_name=self.fix_model_name,
                generation_config=self.fix_generation_config,
                timeout=self.fix_timeout,
                project_id=project_id,
                screen_id=screen_id,
                operation="generate-angular-app",
                sub_operation="fix-errors",
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
            )
            logger.info(
                "Fix iteration %s.%s metadata - finish: %s usage: %s",
                attempt,
                prompt_retry,
                finish_reason,
                usage,
            )
            try:
                fix_files, _ = self._parse_generated_files(raw_response)
                return fix_files, {
                    "finish_reason": finish_reason,
                    "usage": usage,
                    "prompt_retry": prompt_retry,
                }
            except ValueError as parse_exc:
                last_exception = parse_exc
                logger.warning(
                    "Fix iteration %s.%s returned invalid format: %s",
                    attempt,
                    prompt_retry,
                    parse_exc,
                )
                raw_excerpt = raw_response[:5000]
                fix_prompt = self._build_fix_prompt(
                    user_prompt=user_prompt,
                    errors=parsed_errors,
                    file_context=file_context,
                    attempt=attempt,
                    ir_schema=ir_schema,
                    backend_metadata=backend_metadata,
                    previous_failure_reason=str(parse_exc),
                    previous_raw_response=raw_excerpt,
                )

        raise ValueError("LLM fixer failed to return a valid <files> bundle") from last_exception

    def _extract_imported_file_from_error(
        self, error_file: str, error_message: str
    ) -> Optional[str]:
        """
        Extract the imported file path from an import/export error message.
        
        Examples:
        - Module '"./app/app.component"' has no exported member 'AppComponent'
        - Module '"./components/button"' has no exported member 'ButtonComponent'
        - Cannot find module '"./services/api"'
        - was not found in './app/app.component'
        
        Returns:
            Resolved file path (e.g., "src/app/app.component.ts") or None if not found
        """
        # Pattern 1: Module '"./path/to/file"' or Module '"../path/to/file"'
        module_pattern = re.compile(
            r"Module\s+['\"]([^'\"]+)['\"]|Cannot find module\s+['\"]([^'\"]+)['\"]",
            re.IGNORECASE
        )
        
        match = module_pattern.search(error_message)
        if match:
            module_path = match.group(1) or match.group(2)
            if module_path:
                return self._resolve_import_path(error_file, module_path)
        
        # Pattern 2: was not found in './path/to/file' or was not found in "../path/to/file"
        not_found_pattern = re.compile(
            r"was not found in\s+['\"]([^'\"]+)['\"]",
            re.IGNORECASE
        )
        
        match = not_found_pattern.search(error_message)
        if match:
            module_path = match.group(1)
            if module_path:
                return self._resolve_import_path(error_file, module_path)
        
        return None
    
    def _resolve_import_path(self, from_file: str, import_path: str) -> Optional[str]:
        """
        Resolve a relative import path to an absolute file path.
        
        Args:
            from_file: The file where the import occurs (e.g., "src/main.ts")
            import_path: The import path (e.g., "./app/app.component" or "../shared/utils")
        
        Returns:
            Resolved file path (e.g., "src/app/app.component.ts") or None if cannot resolve
        """
        if not import_path or import_path == "unknown":
            return None
        
        # Remove quotes if present
        import_path = import_path.strip('"\'')
        
        # If it's already an absolute path starting with src/, return as is
        if import_path.startswith("src/"):
            # Try common extensions
            for ext in [".ts", ".js", ""]:
                candidate = import_path + ext
                if candidate.endswith(".ts") or candidate.endswith(".js"):
                    return candidate
            return import_path
        
        # Handle relative paths (./ or ../)
        if not (import_path.startswith("./") or import_path.startswith("../")):
            # Not a relative path, might be a package import - skip
            return None
        
        # Get directory of the file with the error
        from_dir = os.path.dirname(from_file)
        if not from_dir:
            from_dir = "src"
        
        # Resolve the path
        # Normalize the path (handle .. and .)
        resolved = os.path.normpath(os.path.join(from_dir, import_path))
        
        # Ensure it starts with src/
        if not resolved.startswith("src/"):
            # If it went outside src/, try to fix it
            if resolved.startswith("../"):
                # Too many .., skip
                return None
            resolved = "src/" + resolved.lstrip("/")
        
        # Try common extensions
        for ext in [".ts", ".js"]:
            candidate = resolved + ext
            if candidate.startswith("src/"):
                return candidate
        
        # Return without extension if it looks like a valid path
        if resolved.startswith("src/"):
            return resolved
        
        return None

    def _extract_errors_with_llm(
        self, 
        error_text: str,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Use Gemini Flash to extract errors when regex parsing fails.
        This is a fallback for complex/unparseable error formats.
        
        Args:
            error_text: Build error output
            project_id: Project identifier for tracking
            screen_id: Screen identifier for tracking
            version: Version identifier for tracking
        """
        logger.info("Using LLM to extract errors from build output...")
        
        # Truncate if too long
        truncated_errors = error_text[:20000] if len(error_text) > 20000 else error_text
        
        extraction_prompt = f"""You are analyzing Angular build errors. Extract ALL file references and error details.

            BUILD OUTPUT:
            {truncated_errors}

            Extract errors in this JSON format:
            {{
            "errors": [
                {{
                "file": "src/path/to/file.ts",
                "line": 42,
                "column": 10,
                "code": "TS2304",
                "message": "Cannot find name 'Something'"
                }}
            ]
            }}

            Rules:
            - Extract ALL files mentioned in errors
            - Include line/column if available (null if not)
            - Include error code if available
            - Extract the full error message
            - If you see "Module not found", extract both the missing module and the file trying to import it

            Return ONLY the JSON, no explanation."""

        try:
            # Use flash model for speed
            response_text, _, _, _ = self._invoke_model(
                extraction_prompt,
                model_name="gemini-3.5-flash-lite",  # Fast model for extraction
                generation_config={"temperature": 0.1, "max_output_tokens": 4000},
                timeout=30,
                project_id=project_id,
                screen_id=screen_id,
                version=version,
                operation="angular-build-fix",
                sub_operation="error-extraction",
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
            )
            
            # Parse JSON response
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                errors = result.get("errors", [])
                logger.info(f"LLM extracted {len(errors)} errors")
                return errors
            else:
                logger.warning("LLM did not return valid JSON")
                return []
                
        except Exception as e:
            logger.error(f"LLM error extraction failed: {e}")
            return []


    def _extract_compiler_errors(
        self,
        error_text: str,
        limit: int = 50,
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Parse Angular/TypeScript compiler output into structured errors.
        
        Args:
            error_text: Build error output
            limit: Maximum number of errors to extract
            project_id: Project identifier for tracking
            screen_id: Screen identifier for tracking
            version: Version identifier for tracking
            org_id: Organization identifier for tracking
            subscriber_id: Subscriber identifier for tracking
            user_id: User identifier for tracking
        """
        errors: List[Dict[str, Any]] = []
        if not error_text:
            return errors

        # Pattern 1: Error: src/file:line:col - error CODE: message (MOST COMMON)
        ts_pattern_colon = re.compile(
            r"Error:\s+(?P<file>src[^\s:]+):(?P<line>\d+):(?P<col>\d+)\s*-\s*error\s+(?P<code>[A-Z]+\d+)\s*:\s*(?P<message>[^\n]+)",
            re.IGNORECASE,
        )
        
        # Pattern 2: src/file(line,col): error CODE: message (ALTERNATIVE FORMAT)
        ts_pattern_paren = re.compile(
            r"(?P<file>src[^\s:(]+)\((?P<line>\d+),(?P<col>\d+)\)\s*:\s*error\s+(?P<code>[A-Z]+\d+)\s*:\s*(?P<message>[^\n]+)",
            re.IGNORECASE,
        )
        
        # Pattern 3: Angular CLI errors: Error: file - error
        ng_pattern = re.compile(
            r"Error:\s+(?P<file>src[^\s:]+)\s+-\s+(?P<message>[^\n]+)",
            re.IGNORECASE,
        )
        
        # Pattern 4: Webpack/PostCSS errors with file path
        webpack_pattern = re.compile(
            r"Error:.*?\(from.*?\).*?(?P<file>/[^\s]+(?:src[^\s]+))\s+(?P<message>[^\n]+)",
            re.IGNORECASE,
        )
        
        # Pattern 5: PostCSS/CSS errors showing file path inline
        css_error_pattern = re.compile(
            r"\./(?P<file>src[^\s]+\.css)\s*-\s*Error:.*?(?P<line>\d+):(?P<col>\d+).*?(?P<message>[^\n]+)",
            re.IGNORECASE
        )
        
        # Pattern 6: ./src/file:line:col-col - Error: message (webpack format)
        webpack_file_pattern = re.compile(
            r"\./(?P<file>src[^\s:]+):(?P<line>\d+):(?P<col_start>\d+)-(?P<col_end>\d+)\s*-\s*Error:\s*(?P<message>[^\n]+)",
            re.IGNORECASE
        )
        # NEW Pattern 7: Module not found errors
        module_not_found_pattern = re.compile(
            r"Module not found:\s*Error:\s*Can't resolve\s+['\"](?P<module>[^'\"]+)['\"]\s+in\s+['\"]?(?P<file>[^'\"]+(?:src[^'\"]+))['\"]?",
            re.IGNORECASE
        )
        
        # NEW Pattern 8: Circular dependency errors
        circular_dep_pattern = re.compile(
            r"Circular dependency detected:\s*(?P<files>[^\n]+)",
            re.IGNORECASE
        )
        
        # NEW Pattern 9: Template parse errors
        template_parse_pattern = re.compile(
            r"(?:Parser Error|Template parse errors?):\s*(?P<message>[^\n]+)\s+in\s+(?P<component>\w+)@(?P<line>\d+):(?P<col>\d+)",
            re.IGNORECASE
        )
        
        # NEW Pattern 10: Generic TypeScript errors without ERROR prefix
        ts_generic_pattern = re.compile(
            r"(?P<file>src/[^\s:(]+)\((?P<line>\d+),(?P<col>\d+)\):\s*(?P<message>.*?)(?:\n|$)",
            re.IGNORECASE
        )
        
        # NEW Pattern 11: Angular compiler errors (ng serve output)
        ng_compiler_pattern = re.compile(
            r"✖\s+\[(?P<code>[^\]]+)\]\s+(?P<file>src[^\s:]+):(?P<line>\d+):(?P<col>\d+)\s+-\s+(?P<message>[^\n]+)",
            re.IGNORECASE
        )
        
        # NEW Pattern 12: Build optimization errors
        build_opt_pattern = re.compile(
            r"ERROR in\s+(?P<file>src[^\s]+)\s+(?P<message>.*?)(?:\n\n|\Z)",
            re.IGNORECASE | re.DOTALL
        )
        
        # NEW Pattern 13: SCSS/SASS compilation errors
        scss_pattern = re.compile(
            r"Error:\s+(?P<message>.*?)\s+on line (?P<line>\d+)\s+(?:of|at)\s+(?P<file>src[^\n]+\.s[ca]ss)",
            re.IGNORECASE
        )
        
        # NEW Pattern 14: Import statement errors
        import_error_pattern = re.compile(
            r"(?P<file>src[^\s:]+):(?P<line>\d+):(?P<col>\d+)\s+-\s+error\s+TS\d+:\s+Cannot find module\s+['\"](?P<module>[^'\"]+)['\"]",
            re.IGNORECASE
        )
        
        # NEW Pattern 15: Decorator errors
        decorator_error_pattern = re.compile(
            r"(?P<file>src[^\s:]+):(?P<line>\d+):(?P<col>\d+)\s+-\s+error\s+TS\d+:\s+(?P<message>.*?decorator.*?)(?:\n|$)",
            re.IGNORECASE
        )
        for line in error_text.splitlines():
            line = line.strip()
            if not line:
                continue
                
            # Try all patterns in order
            patterns_to_try = [
                (ts_pattern_colon, "TS_COLON"),
                (ts_pattern_paren, "TS_PAREN"),
                (ng_compiler_pattern, "NG_COMPILER"),
                (webpack_file_pattern, "WEBPACK_FILE"),
                (module_not_found_pattern, "MODULE_NOT_FOUND"),
                (import_error_pattern, "IMPORT_ERROR"),
                (template_parse_pattern, "TEMPLATE_PARSE"),
                (decorator_error_pattern, "DECORATOR"),
                (css_error_pattern, "CSS"),
                (scss_pattern, "SCSS"),
                (ng_pattern, "NG"),
                (webpack_pattern, "WEBPACK"),
                (ts_generic_pattern, "TS_GENERIC"),
            ]
            
            matched = False
            for pattern, pattern_name in patterns_to_try:
                match = pattern.search(line)
                if match:
                    error_dict = self._build_error_dict_from_match(match, pattern_name, line)
                    if error_dict:
                        errors.append(error_dict)
                        matched = True
                        if len(errors) >= limit:
                            break
                        break  # Move to next line after first match
            
            if len(errors) >= limit:
                break
            
            # Special handling for circular dependencies (multiple files in one error)
            if not matched:
                circ_match = circular_dep_pattern.search(line)
                if circ_match:
                    files_chain = circ_match.group("files")
                    # Extract all files from the chain
                    file_list = re.findall(r'src/[^\s:]+\.ts', files_chain)
                    if file_list:
                        errors.append({
                            "file": file_list[0] if file_list else "unknown",
                            "related_files": file_list[1:] if len(file_list) > 1 else [],
                            "line": None,
                            "column": None,
                            "code": "CIRCULAR_DEP",
                            "message": f"Circular dependency: {files_chain}",
                        })
                        matched = True
            
            # Build optimization errors (multi-line)
            if not matched and "ERROR in" in line:
                build_match = build_opt_pattern.search(error_text[error_text.index(line):])
                if build_match:
                    errors.append({
                        "file": build_match.group("file").strip(),
                        "line": None,
                        "column": None,
                        "code": "BUILD_OPT",
                        "message": build_match.group("message").strip()[:500],
                    })
                    matched = True

        # Enhanced fallback: If no structured errors found, extract ALL file references
        if not errors:
            logger.warning("No errors matched any patterns. Using enhanced fallback extraction...")
            
            # Extract all file paths mentioned in error text
            file_refs = re.findall(r'(src/[^\s:()]+\.[a-z]+)', error_text, re.IGNORECASE)
            unique_files = list(dict.fromkeys(file_refs))[:limit]
            
            logger.info(f"Found {len(unique_files)} file references in error text")
            
            # For each file, try to extract surrounding context
            for file_path in unique_files:
                # Find the context around this file mention (up to 500 chars before and after)
                file_pattern = re.escape(file_path)
                context_pattern = re.compile(
                    rf'(.{{0,500}}){file_pattern}(.{{0,500}})',
                    re.IGNORECASE | re.DOTALL
                )
                context_match = context_pattern.search(error_text)
                
                if context_match:
                    before = context_match.group(1).strip()
                    after = context_match.group(2).strip()
                    context_message = f"{before[-200:]}...{file_path}...{after[:200]}"
                else:
                    context_message = "Build error - see full log"
                
                # Try to extract line number from context
                line_match = re.search(rf'{re.escape(file_path)}[:\(](\d+)', error_text)
                line_num = int(line_match.group(1)) if line_match else None
                
                errors.append({
                    "file": file_path,
                    "line": line_num,
                    "column": None,
                    "code": "FALLBACK",
                    "message": context_message,
                    "full_context": True,  # Flag to indicate this needs full error text
                })
            
            # If still no files found, create a generic error with full text
            if not errors:
                logger.warning("Regex extraction found nothing - trying LLM-based extraction...")
                llm_errors = self._extract_errors_with_llm(
                    error_text, 
                    project_id=project_id, 
                    screen_id=screen_id, 
                    version=version,
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                    user_id=user_id,
                )
                
                if llm_errors:
                    errors = llm_errors[:limit]
                    logger.info(f"LLM successfully extracted {len(errors)} errors")
                else:
                    # Final fallback: create generic error with full text
                    logger.error("All extraction methods failed - creating generic error")
                    errors.append({
                        "file": "unknown",
                        "line": None,
                        "column": None,
                        "code": "BUILD_FAILED",
                        "message": "Build failed - see full error text",
                        "full_error_text": self._truncate_error_log(error_text),
                        "full_context": True,
                    })
        else:
            logger.info(f"Extracted {len(errors)} errors from build output")
            for i, err in enumerate(errors[:5], 1):
                logger.info(f"  Error {i}: {err.get('file')}:{err.get('line', '?')} - {err.get('message', '')[:80]}")

        return errors[:limit]

    def _build_error_dict_from_match(
        self,
        match: re.Match,
        pattern_name: str,
        line: str
    ) -> Optional[Dict[str, Any]]:
        """Helper to build error dictionary from regex match."""
        try:
            error_dict = {}
            
            # Extract file
            if 'file' in match.groupdict():
                file_path = match.group('file').strip()
                # Clean up file path
                if not file_path.startswith('src/'):
                    src_match = re.search(r'(src/.+?)(?:\s|$)', file_path)
                    if src_match:
                        file_path = src_match.group(1)
                error_dict['file'] = file_path
            else:
                error_dict['file'] = 'unknown'
            
            # Extract line number
            if 'line' in match.groupdict() and match.group('line'):
                error_dict['line'] = int(match.group('line'))
            else:
                error_dict['line'] = None
            
            # Extract column
            if 'col' in match.groupdict() and match.group('col'):
                error_dict['column'] = int(match.group('col'))
            elif 'col_start' in match.groupdict() and match.group('col_start'):
                error_dict['column'] = int(match.group('col_start'))
            else:
                error_dict['column'] = None
            
            # Extract error code
            if 'code' in match.groupdict() and match.group('code'):
                error_dict['code'] = match.group('code')
            else:
                error_dict['code'] = pattern_name
            
            # Extract message
            if 'message' in match.groupdict() and match.group('message'):
                error_dict['message'] = match.group('message').strip()
            else:
                error_dict['message'] = line[:200]
            
            # Extract module if present (for import errors)
            if 'module' in match.groupdict() and match.group('module'):
                error_dict['missing_module'] = match.group('module')
            
            # Extract component if present (for template errors)
            if 'component' in match.groupdict() and match.group('component'):
                error_dict['component'] = match.group('component')
            
            # Extract imported file path if this is an import/export error
            imported_file = self._extract_imported_file_from_error(
                error_dict['file'], error_dict['message']
            )
            if imported_file:
                error_dict['imported_file'] = imported_file
            
            return error_dict
        
        except (ValueError, AttributeError) as e:
            logger.warning(f"Failed to parse error from pattern {pattern_name}: {e}")
            return None

    def _collect_error_file_context(
        self,
        workspace_dir: str,
        errors: List[Dict[str, Any]],
        use_full_context: bool = True,
    ) -> List[Dict[str, str]]:
        """
        Load file contents for error fixing.
        
        Args:
            workspace_dir: Path to workspace
            errors: List of parsed errors
            use_full_context: If True, load entire src/ folder. If False, only error files.
        
        Returns:
            List of file context dictionaries
        """
        contexts: List[Dict[str, str]] = []
        logger.info(f"Collecting context for {len(errors)} errors (full_context={use_full_context})...")

        # Log the errors we're trying to fix
        for i, error in enumerate(errors, 1):
            logger.info(f"  Error {i}: file='{error.get('file')}' line={error.get('line')} code={error.get('code')} msg='{error.get('message', '')[:60]}'")

        # PROTECTED FILES: deterministic platform files — never sent to the fix LLM
        PROTECTED_FILES = set(PROTECTED_GENERATED_PATHS)

        if use_full_context:
            # NEW APPROACH: Load entire src/ folder for complete context
            logger.info("Loading full src/ context for comprehensive error fixing...")
            full_context = self._read_full_src_context(workspace_dir)
            
            # Create context entries with error annotations
            error_file_map = {}
            for error in errors:
                file_path = error.get("file")
                if file_path and file_path != "unknown":
                    if file_path not in error_file_map:
                        error_file_map[file_path] = []
                    error_file_map[file_path].append({
                        "line": error.get("line"),
                        "message": error.get("message", "")[:200]
                    })
            
            # Add all files with error annotations where applicable
            # BUT exclude protected files from LLM modification
            protected_count = 0
            for file_path, content in full_context.items():
                # Check if this is a protected file
                file_path_str = str(file_path)
                is_protected = any(protected in file_path_str for protected in PROTECTED_FILES)
                
                if is_protected:
                    protected_count += 1
                    logger.debug(f"Skipping protected file from LLM context: {file_path_str}")
                    continue
                
                error_info = ""
                if file_path_str in error_file_map:
                    error_list = error_file_map[file_path_str]
                    error_lines = ", ".join([f"line {e['line']}" for e in error_list if e['line']])
                    if error_lines:
                        error_info = f" (ERRORS AT: {error_lines})"
                
                contexts.append({
                    "path": file_path_str,
                    "content": content,
                    "error_line": error_file_map.get(file_path_str, [{}])[0].get("line") if file_path_str in error_file_map else None,
                    "error_message": error_info or "Context file",
                })
            
            logger.info(f"Loaded {len(contexts)} files from full src/ context ({protected_count} protected files excluded)")
            
        else:
            # OLD APPROACH: Load only error files and related files
            seen: set[str] = set()
            
            for error in errors:
                file_path = error.get("file")
                if not file_path or file_path == "unknown":
                    continue
                
                # Collect the file with the error
                if file_path not in seen:
                    seen.add(file_path)
                    source_path = Path(workspace_dir) / file_path
                    if source_path.exists():
                        try:
                            content = source_path.read_text(encoding="utf-8")
                            contexts.append({
                                "path": file_path,
                                "content": content[: self.max_fix_file_chars],
                                "error_line": error.get("line"),
                                "error_message": error.get("message", "")[:200],
                            })
                            logger.info(f"Loaded error file context: {file_path} (line {error.get('line', '?')})")
                        except (UnicodeDecodeError, PermissionError) as e:
                            logger.warning(f"Cannot read {file_path}: {e}")
                
                # If error is in an HTML template, also load the corresponding TypeScript component
                if file_path.endswith('.html'):
                    ts_file = file_path.replace('.html', '.ts')
                    if ts_file not in seen:
                        seen.add(ts_file)
                        ts_path = Path(workspace_dir) / ts_file
                        if ts_path.exists():
                            try:
                                content = ts_path.read_text(encoding="utf-8")
                                contexts.append({
                                    "path": ts_file,
                                    "content": content[: self.max_fix_file_chars],
                                    "error_line": None,
                                    "error_message": f"Component file for template with error",
                                })
                                logger.info(f"Loaded corresponding component file: {ts_file}")
                            except (UnicodeDecodeError, PermissionError) as e:
                                logger.warning(f"Cannot read component file {ts_file}: {e}")
                
                # Also collect imported files
                imported_file = error.get("imported_file")
                if imported_file and imported_file not in seen:
                    seen.add(imported_file)
                    imported_path = Path(workspace_dir) / imported_file
                    if imported_path.exists():
                        try:
                            content = imported_path.read_text(encoding="utf-8")
                            contexts.append({
                                "path": imported_file,
                                "content": content[: self.max_fix_file_chars],
                                "error_line": None,
                                "error_message": f"Referenced in import error",
                            })
                            logger.info(f"Loaded imported file context: {imported_file}")
                        except (UnicodeDecodeError, PermissionError) as e:
                            logger.warning(f"Cannot read imported file {imported_file}: {e}")

                if len(contexts) >= self.max_error_files:
                    break

            # Fallback if no files found
            if not contexts:
                logger.warning("No error files found, loading fallback core files...")
                fallback_files = [
                    "src/app/app.component.ts",
                    "src/app/app.component.html",
                    "src/app/app.config.ts",
                    "src/main.ts",
                    "src/styles.css",
                ]
                for file_path in fallback_files:
                    if file_path in seen:
                        continue
                    source_path = Path(workspace_dir) / file_path
                    if source_path.exists():
                        try:
                            content = source_path.read_text(encoding="utf-8")
                            contexts.append({
                                "path": file_path,
                                "content": content[: self.max_fix_file_chars],
                                "error_line": None,
                                "error_message": "Fallback file",
                            })
                            seen.add(file_path)
                            logger.info(f"Loaded fallback file: {file_path}")
                        except (UnicodeDecodeError, PermissionError):
                            continue
                    if len(contexts) >= self.max_error_files:
                        break
        
        logger.info(f"Collected {len(contexts)} file contexts for error fixing")
        return contexts

    def _build_fix_prompt(
        self,
        *,
        user_prompt: str,
        errors: List[Dict[str, Any]],
        file_context: List[Dict[str, str]],
        attempt: int,
        ir_schema: Optional[Dict[str, Any]] = None,  # NEW: Add IR schema
        backend_metadata: Optional[Dict[str, Any]] = None,  # NEW: Add backend metadata
        previous_failure_reason: Optional[str] = None,
        previous_raw_response: Optional[str] = None,
    ) -> str:
        """Create a focused fixer prompt for the LLM with complete context including IR schema."""
        
        # Build error details
        error_details = []
        template_errors = []
        component_errors = []
        api_errors = []
        type_errors = []
        other_errors = []
        
        for i, error in enumerate(errors, 1):
            file_name = error.get("file", "unknown")
            line = error.get("line", "?")
            col = error.get("column", "?")
            code = error.get("code", "?")
            message = error.get("message", "")
            
            error_str = (
                f"Error {i}:\n"
                f"  File: {file_name}\n"
                f"  Location: Line {line}, Column {col}\n"
                f"  Code: {code}\n"
                f"  Message: {message}\n"
            )
            
            # Categorize errors for better guidance
            if "does not exist" in message.lower() and file_name.endswith('.html'):
                template_errors.append(error_str)
            elif "cannot find module" in message.lower() or "has no exported member" in message.lower():
                component_errors.append(error_str)
            elif "type" in message.lower() and ("property" in message.lower() or "argument" in message.lower()):
                type_errors.append(error_str)
            elif "http" in message.lower() or "observable" in message.lower():
                api_errors.append(error_str)
            else:
                other_errors.append(error_str)
        
        all_errors = template_errors + component_errors + type_errors + api_errors + other_errors

        # Organize files by category
        component_files = []
        template_files = []
        service_files = []
        model_files = []
        config_files = []
        style_files = []
        other_files = []
        
        for ctx in file_context:
            path = ctx["path"]
            error_marker = ""
            if ctx.get("error_line"):
                error_marker = f" ⚠️ ERROR LINE {ctx['error_line']}: {ctx.get('error_message', '')}"
            elif ctx.get("error_message") and ctx.get("error_message") != "Context file":
                error_marker = f" ⚠️ {ctx.get('error_message')}"
            
            file_block = f'<file path="{path}"{error_marker}>\n{ctx["content"]}\n</file>\n'
            
            if '.component.ts' in path or path.endswith('app.ts'):
                component_files.append(file_block)
            elif path.endswith('.html'):
                template_files.append(file_block)
            elif '.service.ts' in path:
                service_files.append(file_block)
            elif '/models/' in path or path.endswith('.model.ts') or path.endswith('.interface.ts'):
                model_files.append(file_block)
            elif 'app.config.ts' in path or 'app.routes.ts' in path or 'main.ts' in path:
                config_files.append(file_block)
            elif path.endswith('.css') or path.endswith('.scss'):
                style_files.append(file_block)
            else:
                other_files.append(file_block)

        # Build file sections
        file_sections = []
        if config_files:
            file_sections.append(f"=== CONFIGURATION FILES ===\n{''.join(config_files)}")
        if component_files:
            file_sections.append(f"=== COMPONENT FILES (TypeScript) ===\n{''.join(component_files)}")
        if template_files:
            file_sections.append(f"=== TEMPLATE FILES (HTML) ===\n{''.join(template_files)}")
        if service_files:
            file_sections.append(f"=== SERVICE FILES ===\n{''.join(service_files)}")
        if model_files:
            file_sections.append(f"=== MODEL/INTERFACE FILES ===\n{''.join(model_files)}")
        if style_files:
            file_sections.append(f"=== STYLE FILES ===\n{''.join(style_files)}")
        if other_files:
            file_sections.append(f"=== OTHER FILES ===\n{''.join(other_files)}")
        # Check if any errors have full_context flag
        needs_full_error_text = any(err.get("full_context") for err in errors)
        # Build the prompt
        prompt_parts = []
        
        prompt_parts.append(f"""
            You are fixing compilation errors in an Angular 17+ application. You have access to:
            1. The COMPLETE source code
            2. The IR SCHEMA (single source of truth for data models and API contracts)
            3. Backend API metadata (available endpoints and their contracts)
        {"4. FULL ERROR OUTPUT (errors were not cleanly parsed - analyze carefully)" if needs_full_error_text else ""}

            ORIGINAL USER REQUEST:
            {user_prompt}

            BUILD ERRORS (Attempt #{attempt}):
            {chr(10).join(all_errors)}
        """)

        # Add IR Schema section if available
        if ir_schema:
            prompt_parts.append("\n" + "="*80 + "\n")
            prompt_parts.append("IR SCHEMA - SINGLE SOURCE OF TRUTH\n")
            prompt_parts.append("="*80 + "\n")
            prompt_parts.append("<ir_schema>\n")
            prompt_parts.append(json.dumps(ir_schema, indent=2))
            prompt_parts.append("\n</ir_schema>\n\n")
            
            prompt_parts.append("**CRITICAL - IR SCHEMA REQUIREMENTS:**\n")
            
            # Extract and emphasize component selector
            screen_selector = ir_schema.get("screen", {}).get("selector")
            if screen_selector:
                prompt_parts.append(f"1. **COMPONENT SELECTOR**: The root component MUST use selector: '{screen_selector}'\n")
                prompt_parts.append(f"   In app.component.ts: @Component({{ selector: '{screen_selector}', ... }})\n\n")
            
            # Emphasize data models
            data_models = ir_schema.get("dataModels", {})
            if data_models:
                prompt_parts.append("2. **DATA MODELS** (use these EXACT field names and types):\n")
                for model_name, model_def in list(data_models.items()):
                    fields = model_def.get("fields", [])
                    if fields:
                        prompt_parts.append(f"   - {model_name}: {', '.join([f['name'] for f in fields[:8]])}\n")
                prompt_parts.append("\n")
            
            # Emphasize data sources (API bindings)
            data_sources = ir_schema.get("dataSources", [])
            if data_sources:
                prompt_parts.append("3. **API ENDPOINTS** (backend functions available):\n")
                for ds in data_sources[:5]:  # Show first 5
                    func_id = ds.get("function_id", "unknown")
                    input_model = ds.get("inputModel", "")
                    output_model = ds.get("outputModel", "")
                    prompt_parts.append(f"   - {func_id}: {input_model} → {output_model}\n")
                if len(data_sources) > 5:
                    prompt_parts.append(f"   ... and {len(data_sources) - 5} more endpoints\n")
                prompt_parts.append("\n")
            
            prompt_parts.append("**DATA MODEL CONTRACT:**\n")
            prompt_parts.append("- ALL TypeScript interfaces MUST match IR schema dataModels exactly\n")
            prompt_parts.append("- ALL form controls MUST use field names from IR schema\n")
            prompt_parts.append("- ALL HTTP request payloads MUST match input models\n")
            prompt_parts.append("- ALL HTTP response types MUST match output models\n")
            prompt_parts.append("- Do NOT rename, add, or remove fields from IR schema models\n")
            prompt_parts.append("- List fields with items.type (or itemType scalar) are primitive arrays (string[]), not object wrappers\n\n")
    
        # If we have fallback errors with full context, add the full error text
        if needs_full_error_text:
            full_error_texts = [
                err.get("full_error_text", "")
                for err in errors
                if err.get("full_error_text")
            ]
            if full_error_texts:
                prompt_parts.append("\n" + "="*80 + "\n")
                prompt_parts.append("FULL BUILD ERROR OUTPUT (Unparsed)\n")
                prompt_parts.append("="*80 + "\n")
                for i, error_text in enumerate(full_error_texts, 1):
                    prompt_parts.append(f"\n--- Full Error {i} ---\n")
                    prompt_parts.append(error_text)
                    prompt_parts.append("\n")
                prompt_parts.append("\n**INSTRUCTIONS FOR UNPARSED ERRORS:**\n")
                prompt_parts.append("- Carefully read the full error output above\n")
                prompt_parts.append("- Identify ALL files mentioned in the errors\n")
                prompt_parts.append("- Fix ALL issues found, not just the first one\n")
                prompt_parts.append("- Look for cascading errors (one error causing multiple symptoms)\n\n")
        
        # Add backend metadata if available
        resolved_base_path = ""
        if backend_metadata:
            prompt_parts.append("="*80 + "\n")
            prompt_parts.append("BACKEND API METADATA\n")
            prompt_parts.append("="*80 + "\n")
            
            base_path = backend_metadata.get("base_path", "")
            resolved_base_path = self._resolve_backend_api_path(base_path, backend_metadata)
            
            prompt_parts.append(f"API Base Path: {resolved_base_path}\n\n")
            
            functions = backend_metadata.get("functions", [])
            if functions:
                prompt_parts.append("Available Backend Functions:\n")
                for func in functions:
                    func_id = func.get("id", "unknown")
                    input_model = func.get("input_model", "")
                    response_model = func.get("response_model", "")
                    prompt_parts.append(f"- {func_id}: Input={input_model}, Response={response_model}\n")

                prompt_parts.append("\n")
            
            # Build example API path - use resolved path or construct fallback
            example_api_path = resolved_base_path
            project_id_for_prompt = backend_metadata.get("project_id", "PROJECT_ID")
            if not example_api_path:
                api_base_url = backend_metadata.get("api_base_url") or self.api_base_url
                api_prefix = getattr(self, "api_prefix", "") or ""
                prefix_segment = f"/{api_prefix.strip('/')}" if api_prefix else ""
                if api_base_url:
                    example_api_path = (
                        f"{api_base_url.rstrip('/')}{prefix_segment}"
                        f"/aiqod-agent/agent/dynamic/execute/{project_id_for_prompt}"
                    )
                else:
                    example_api_path = (
                        f"{prefix_segment}/aiqod-agent/agent/dynamic/execute/{project_id_for_prompt}"
                    )

            prompt_parts.append("**CRITICAL - API BASE PATH:**\n")
            prompt_parts.append(f"- The resolved backend_api base path for this screen is: '{example_api_path}'.\n")
            prompt_parts.append("- You MUST ensure that all API calls ultimately post to this resolved base path.\n")
            prompt_parts.append("- However, do NOT hardcode this full URL as a string literal in the Angular code.\n")
            prompt_parts.append(
                "- Instead, always build the URL from `environment.apiBaseUrl`, "
                "`environment.agentBasePath`, and `environment.projectId`.\n"
            )
            if not resolved_base_path:
                prompt_parts.append("- If api_base_path is empty in metadata, still ensure the final constructed URL matches the backend base path shown above.\n")
            prompt_parts.append("- DO NOT use empty strings, '/api/execute', `default_project`, or other placeholders.\n\n")
            prompt_parts.append("**ENVIRONMENT CONFIG & API CALL FORMAT:**\n")
            prompt_parts.append("- The Angular app has a single `src/environments/environment.ts` file provided by the platform.\n")
            prompt_parts.append("- It exposes:\n")
            prompt_parts.append("  - `apiBaseUrl` (e.g. `\"https://dev.aiqod.com:843/dev\"`) \n")
            prompt_parts.append("  - `agentBasePath` (e.g. `\"/aiqod-agent/agent\"`)\n")
            prompt_parts.append(
                f"  - `projectId` (this screen's project id: `\"{project_id_for_prompt}\"`)\n"
            )
            prompt_parts.append("- You MUST build the dynamic execute URL from these fields instead of hardcoding the full URL string.\n")
            prompt_parts.append("- ALWAYS use `environment.projectId` in the execute path. NEVER use `default_project`.\n")
            prompt_parts.append(f"- The resolved backend_api base path for this screen is: '{example_api_path}'. Use it only as a reference; do NOT inline this literal value in the code.\n\n")
            prompt_parts.append("**API CALL FORMAT:**\n")
            prompt_parts.append('POST to API with: {"function_id": "<id>", "params": {...}, "request_meta": {...}}\n')
            prompt_parts.append("- params MUST match the input_schema fields exactly\n")
            prompt_parts.append("- All required fields MUST be present\n\n")

        # Add current source code
        prompt_parts.append("="*80 + "\n")
        prompt_parts.append("COMPLETE APPLICATION SOURCE CODE\n")
        prompt_parts.append("="*80 + "\n")
        prompt_parts.append(chr(10).join(file_sections))
        prompt_parts.append("\n\n")

        # Add fixing instructions
        prompt_parts.append("="*80 + "\n")
        prompt_parts.append("YOUR TASK - FIX ALL ERRORS\n")
        prompt_parts.append("="*80 + "\n")
        prompt_parts.append("""
            Analyze the errors in context of:
            1. The ENTIRE application source code
            2. The IR SCHEMA (single source of truth for data contracts)
            3. The user's original request
            4. Backend API contracts

            🚫 PROTECTED FILES - NEVER MODIFY THESE:
            ❌ src/screen-runtime.ts - Deterministic workflow runtime (auto-injected)
            ❌ src/main.ts - Bootstrap entry (auto-injected). Never return main.ts in your fix bundle.
               - If you see errors in main.ts (e.g. appRef), they are fixed by the platform on the next build — fix OTHER files only.
            ❌ If you see TypeScript errors related to window.__AIQOD_SCREEN__, fix them in OTHER files or use (window as any)
               - DO NOT move type declarations to workflow.d.ts or regenerate screen-runtime.ts

            Fix ALL errors while ensuring:
            ✅ Data models match IR schema exactly
            ✅ Component selector matches IR schema requirement
            ✅ API calls use correct function IDs and params shape
            ✅ All existing functionality remains intact
            ✅ Code follows Angular 17+ best practices
            ✅ DO NOT create workflow.d.ts or move screen-runtime.ts declarations
            ✅ If TypeScript complains about window.__AIQOD_SCREEN__, ignore those errors or use (window as any)

            SPECIFIC FIX GUIDELINES BY ERROR TYPE:

            **Template Errors (Property/Method does not exist):**
        """)
        
        if template_errors:
            prompt_parts.append("⚠️ YOU HAVE TEMPLATE ERRORS - Follow this pattern:\n")
        
        prompt_parts.append("""
            - Template uses `property()` → Add signal in component: `property = signal<Type>(initialValue)`
            - Template uses `method()` → Add method in component: `method() { /* logic */ }`
            - Template uses `items()` → Add signal: `items = signal<Item[]>([])`
            - Example: If template has `garnishmentOrders()`, component needs:
            ```
            garnishmentOrders = signal<GarnishmentOrder[]>([]);
            ```

            **Component/Import Errors:**
        """)
        
        if component_errors:
            prompt_parts.append("⚠️ YOU HAVE IMPORT ERRORS - Check these:\n")
        
        prompt_parts.append("""
            - Ensure all exports have proper decorators (@Component, @Injectable)
            - Verify import paths are correct (relative paths, no typos)
            - Check standalone components have required imports in `imports: []` array
            - Verify barrel exports (index.ts) if used

            **Type Errors:**
        """)
        
        if type_errors:
            prompt_parts.append("⚠️ YOU HAVE TYPE ERRORS - Verify against IR schema:\n")
        
        prompt_parts.append("""
            - Check that TypeScript interfaces match IR schema dataModels
            - Ensure form controls use correct types from data models
            - Verify HTTP response types match backend response models
            - Use proper generics: `Observable<ResponseType>`, `signal<Type[]>([])`

            **API/HTTP Errors:**
        """)
        
        if api_errors:
            prompt_parts.append("⚠️ YOU HAVE API ERRORS - Check backend contracts:\n")
        
        prompt_parts.append("""
            - Verify function_id matches available backend functions
            - Ensure params object matches input_model structure exactly
            - Check that all required fields are present in params
            - Verify response type matches response_model from backend

            OUTPUT FORMAT (CRITICAL - NO DEVIATION):
            <files>
            <file path="src/app/app.component.ts">
            // Complete corrected TypeScript code
            // NO markdown fences like ```typescript
            // Include full file content, not just changes
            </file>
            <file path="src/app/app.component.html">
            <!-- Complete corrected HTML -->
            <!-- NO markdown fences -->
            </file>
            <file path="src/app/models/example.model.ts">
            // Complete corrected model - must match IR schema
            </file>
            </files>

            MANDATORY RULES:
            ✅ Output ONLY the <files>...</files> block
            ✅ Include ALL files that need changes (don't skip any)
            ✅ Include COMPLETE file content (not diffs or snippets)
            ✅ NO markdown fences (```typescript, ```html, ```css, etc.)
            ✅ NO explanatory text outside <files> tags
            ✅ NO comments like "// ... rest of file" - include everything
            ✅ Match IR schema data models exactly
            ✅ Use correct component selector from IR schema
            ✅ Fix ALL compilation errors listed above
            ❌ Don't add unnecessary changes
            ❌ Don't break existing functionality
            ❌ Don't deviate from IR schema contracts

            Generate the corrected files now:
            """)

        if previous_failure_reason:
            prompt_parts.append(f"\n\n⚠️ PREVIOUS ATTEMPT FAILED: {previous_failure_reason}\n")
            prompt_parts.append("You MUST follow the <files> format exactly. NO text outside <files>...</files> tags.\n")
            prompt_parts.append("Check that you're not adding markdown fences or explanations.\n")
        
        if previous_raw_response:
            prompt_parts.append(f"\n\n❌ YOUR PREVIOUS INVALID RESPONSE (DO NOT REPEAT THIS MISTAKE):\n")
            prompt_parts.append(f"{previous_raw_response[:1000]}...\n")
            prompt_parts.append("\nFix the format issues and try again.\n")

        return "".join(prompt_parts)

    def _truncate_error_log(self, error_text: str) -> str:
        """Limit stored error logs to a manageable length."""
        if not error_text:
            return ""
        if len(error_text) <= self.max_error_log_chars:
            return error_text
        return error_text[: self.max_error_log_chars] + "\n...[truncated]..."

    def _cleanup_old_versions(self, project_id: str, screen_id: str) -> None:
        """
        Clean up old versioned directories, treating each version as a single unit
        across all three locations:
        - generated-apps/: Workspace directories (angular-{project_id}-{screen_id}-v{version}-workspace)
        - isolated-angular-apps/: Build directories (angular-{project_id}-{screen_id}-v{version}-build)
        - LIBERTY_FS_ROOT/: Deploy directories ({screen_id}-v{version})

        Keeps only the most recent N versions (configurable via version_retention_count).
        When a version is deleted, its workspace, build, and deploy directories (if present)
        are all deleted together so we don't end up with mixed version IDs across locations.
        """
        keep_last_n = self.version_retention_count
        safe_project = re.sub(r"[^a-zA-Z0-9_\-]", "_", project_id)
        safe_screen = re.sub(r"[^a-zA-Z0-9_\-]", "_", screen_id)

        # Discover all versions present in any of the three locations.
        # Version format: YYYYMMDDHHMMSS (legacy) or YYYYMMDDHHMMSS_<hex8> (unique per flow step)
        version_pattern_workspace = re.compile(
            rf"^angular-{re.escape(safe_project)}-{re.escape(safe_screen)}-v(?P<version>[0-9]+(?:_[0-9a-fA-F]+)?)-workspace$"
        )
        version_pattern_build = re.compile(
            rf"^angular-{re.escape(safe_project)}-{re.escape(safe_screen)}-v(?P<version>[0-9]+(?:_[0-9a-fA-F]+)?)-build$"
        )
        version_pattern_deploy = re.compile(
            rf"^{re.escape(screen_id)}-v(?P<version>[0-9]+(?:_[0-9a-fA-F]+)?)$"
        )

        versions: set[str] = set()

        if self.generated_apps_root.exists():
            for item in self.generated_apps_root.iterdir():
                if not item.is_dir():
                    continue
                m = version_pattern_workspace.match(item.name)
                if m:
                    versions.add(m.group("version"))

        if self.isolated_apps_root.exists():
            for item in self.isolated_apps_root.iterdir():
                if not item.is_dir():
                    continue
                m = version_pattern_build.match(item.name)
                if m:
                    versions.add(m.group("version"))

        if self.deploy_root.exists():
            for item in self.deploy_root.iterdir():
                if not item.is_dir() or item.is_symlink():
                    continue
                m = version_pattern_deploy.match(item.name)
                if m:
                    versions.add(m.group("version"))

        if not versions or len(versions) <= keep_last_n:
            return

        # Our version strings are timestamps (YYYYMMDDHHMMSS), so lexical order matches
        # chronological order; keep the most recent N and delete the rest.
        sorted_versions = sorted(versions, reverse=True)
        versions_to_delete = sorted_versions[keep_last_n:]

        for version in versions_to_delete:
            # Workspace
            workspace_dir = self.generated_apps_root / f"angular-{safe_project}-{safe_screen}-v{version}-workspace"
            if workspace_dir.exists():
                try:
                    shutil.rmtree(workspace_dir)
                    logger.info(
                        f"Cleaning up old workspace for version {version}: {workspace_dir.name}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to delete old workspace {workspace_dir}: {e}")

            # Build
            build_dir = self.isolated_apps_root / f"angular-{safe_project}-{safe_screen}-v{version}-build"
            if build_dir.exists():
                try:
                    shutil.rmtree(build_dir)
                    logger.info(
                        f"Cleaning up old isolated build for version {version}: {build_dir.name}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to delete old isolated build {build_dir}: {e}")

            # Deploy
            deploy_dir = self.deploy_root / f"{screen_id}-v{version}"
            if deploy_dir.exists() and not deploy_dir.is_symlink():
                try:
                    shutil.rmtree(deploy_dir)
                    logger.info(
                        f"Cleaning up old deploy directory for version {version}: {deploy_dir.name}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to delete old deploy directory {deploy_dir}: {e}")

    def _cleanup_old_isolated_builds(self, project_id: str, screen_id: str, keep_last_n: int) -> None:
        """Clean up old isolated build directories."""
        safe_project = re.sub(r"[^a-zA-Z0-9_\-]", "_", project_id)
        safe_screen = re.sub(r"[^a-zA-Z0-9_\-]", "_", screen_id)
        prefix = f"angular-{safe_project}-{safe_screen}-"
        suffix = "-build"
        
        matching_dirs = []
        if self.isolated_apps_root.exists():
            for item in self.isolated_apps_root.iterdir():
                if item.is_dir() and item.name.startswith(prefix) and item.name.endswith(suffix):
                    matching_dirs.append(item)
        
        if len(matching_dirs) <= keep_last_n:
            return
        
        matching_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        to_delete = matching_dirs[keep_last_n:]
        for old_build in to_delete:
            try:
                logger.info(f"Cleaning up old isolated build: {old_build.name}")
                shutil.rmtree(old_build)
            except Exception as e:
                logger.warning(f"Failed to delete {old_build}: {e}")

    def _cleanup_old_workspaces(self, project_id: str, screen_id: str, keep_last_n: int) -> None:
        """Clean up old workspace directories in generated-apps."""
        safe_project = re.sub(r"[^a-zA-Z0-9_\-]", "_", project_id)
        safe_screen = re.sub(r"[^a-zA-Z0-9_\-]", "_", screen_id)
        prefix = f"angular-{safe_project}-{safe_screen}-"
        suffix = "-workspace"
        
        matching_dirs = []
        if self.generated_apps_root.exists():
            for item in self.generated_apps_root.iterdir():
                if item.is_dir() and item.name.startswith(prefix) and item.name.endswith(suffix):
                    matching_dirs.append(item)
        
        if len(matching_dirs) <= keep_last_n:
            return
        
        matching_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        to_delete = matching_dirs[keep_last_n:]
        for old_workspace in to_delete:
            try:
                logger.info(f"Cleaning up old workspace: {old_workspace.name}")
                shutil.rmtree(old_workspace)
            except Exception as e:
                logger.warning(f"Failed to delete {old_workspace}: {e}")

    def _cleanup_old_deploy_dirs(self, screen_id: str, keep_last_n: int) -> None:
        """Clean up old deploy directories in LIBERTY_FS_ROOT (excluding the symlink)."""
        safe_screen = re.sub(r"[^a-zA-Z0-9_\-]", "_", screen_id)
        prefix = f"{safe_screen}-v"
        
        matching_dirs = []
        if self.deploy_root.exists():
            for item in self.deploy_root.iterdir():
                # Match {screen_id}-v{version} directories, exclude symlinks
                if (item.is_dir() and not item.is_symlink() and 
                    item.name.startswith(prefix) and len(item.name) > len(prefix)):
                    matching_dirs.append(item)
        
        if len(matching_dirs) <= keep_last_n:
            return
        
        matching_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        to_delete = matching_dirs[keep_last_n:]
        for old_deploy in to_delete:
            try:
                logger.info(f"Cleaning up old deploy directory: {old_deploy.name}")
                shutil.rmtree(old_deploy)
            except Exception as e:
                logger.warning(f"Failed to delete {old_deploy}: {e}")

    def _validate_generated_code(self, isolated_dir: str) -> Dict[str, Any]:
        """
        Validate generated code for missing dependencies and imports.
        
        Returns:
            Dictionary with 'valid' (bool) and 'errors' (list of error messages)
        """
        errors = []
        src_root = Path(isolated_dir) / "src"
        package_json_path = Path(isolated_dir) / "package.json"
        
        if not package_json_path.exists():
            return {"valid": False, "errors": ["package.json not found"]}
        
        # Read package.json to check installed dependencies
        with open(package_json_path, "r", encoding="utf-8") as f:
            package_data = json.load(f)
        
        installed_deps = set(package_data.get("dependencies", {}).keys())
        installed_dev_deps = set(package_data.get("devDependencies", {}).keys())
        all_installed = installed_deps | installed_dev_deps
        
        # Scan TypeScript files for imports
        missing_deps = set()
        missing_types = set()
        
        for ts_file in src_root.rglob("*.ts"):
            try:
                content = ts_file.read_text(encoding="utf-8")
                
                # Find import statements
                import_pattern = r"import\s+(?:.*?\s+from\s+)?['\"]([^'\"]+)['\"]"
                imports = re.findall(import_pattern, content)
                
                for import_path in imports:
                    # Skip relative imports and Angular core packages
                    if import_path.startswith(".") or import_path.startswith("/"):
                        continue
                    
                    # Skip @angular packages (already in boilerplate)
                    if import_path.startswith("@angular/"):
                        continue
                    
                    # Extract package name (handle scoped packages like @types/node)
                    if import_path.startswith("@"):
                        # Scoped package: @scope/package or @scope/package/path
                        parts = import_path.split("/")
                        if len(parts) >= 2:
                            package_name = f"{parts[0]}/{parts[1]}"
                        else:
                            continue
                    else:
                        # Regular package: package or package/path
                        package_name = import_path.split("/")[0]
                    
                    # Check if package is installed
                    if package_name not in all_installed:
                        # Check if it's a types package
                        if package_name.startswith("@types/"):
                            missing_types.add(package_name)
                        else:
                            missing_deps.add(package_name)
                            # Also check for corresponding @types package
                            base_package = package_name.replace("@types/", "")
                            types_package = f"@types/{base_package}"
                            if types_package not in all_installed and base_package not in installed_deps:
                                # Only add if the base package exists in npm (common packages)
                                if base_package in ["aos", "lodash", "jquery", "chart.js"]:
                                    missing_types.add(types_package)
            
            except (UnicodeDecodeError, PermissionError) as e:
                logger.debug(f"Skipping {ts_file}: {e}")
                continue
        
        # Build error messages
        if missing_deps:
            errors.append(f"Missing dependencies: {', '.join(sorted(missing_deps))}")
        if missing_types:
            errors.append(f"Missing TypeScript type definitions: {', '.join(sorted(missing_types))}")
        
        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "missing_dependencies": list(missing_deps),
            "missing_types": list(missing_types)
        }

    def _fix_missing_dependencies(
        self,
        isolated_dir: str,
        validation_result: Dict[str, Any]
    ) -> bool:
        """
        Attempt to fix missing dependencies by directly updating package.json.
        Uses common version patterns for popular packages.
        
        Returns:
            True if fix was attempted, False otherwise
        """
        if not validation_result.get("missing_dependencies") and not validation_result.get("missing_types"):
            return False
        
        package_json_path = Path(isolated_dir) / "package.json"
        if not package_json_path.exists():
            return False
        
        logger.info("Attempting to auto-fix missing dependencies...")
        
        # Common package versions (fallback if LLM didn't specify)
        common_versions = {
            "aos": "^2.3.4",
            "@types/aos": "^3.0.7",
            "chart.js": "^4.4.0",
            "@types/chart.js": "^2.9.41",
            "date-fns": "^2.30.0",
            "lodash": "^4.17.21",
            "@types/lodash": "^4.14.202",
            "jquery": "^3.7.1",
            "@types/jquery": "^3.5.29",
        }
        
        try:
            with open(package_json_path, "r", encoding="utf-8") as f:
                package_data = json.load(f)
            
            missing_deps = validation_result.get("missing_dependencies", [])
            missing_types = validation_result.get("missing_types", [])
            
            added_any = False
            
            # Add missing dependencies
            if "dependencies" not in package_data:
                package_data["dependencies"] = {}
            
            for dep_name in missing_deps:
                if dep_name not in package_data["dependencies"]:
                    version = common_versions.get(dep_name, "latest")
                    package_data["dependencies"][dep_name] = version
                    logger.info(f"Added missing dependency: {dep_name}@{version}")
                    added_any = True
            
            # Add missing type definitions
            if "devDependencies" not in package_data:
                package_data["devDependencies"] = {}
            
            for type_name in missing_types:
                if type_name not in package_data["devDependencies"]:
                    version = common_versions.get(type_name, "latest")
                    package_data["devDependencies"][type_name] = version
                    logger.info(f"Added missing type definition: {type_name}@{version}")
                    added_any = True
            
            if added_any:
                # Write updated package.json
                with open(package_json_path, "w", encoding="utf-8") as f:
                    json.dump(package_data, f, indent=2)
                
                # Reinstall dependencies
                logger.info("Reinstalling dependencies with fixes...")
                self._install_dependencies(isolated_dir)
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error during auto-fix: {e}")
            return False

    # Maximum wall-clock seconds allowed for a single shell command (npm install /
    # ng build).  Without a cap, a stalled npm registry or a hanging webpack
    # process will keep the thread — and therefore the HTTP request — blocked
    # indefinitely.  35 minutes matches the historical default_timeout value.
    _COMMAND_TIMEOUT_SECONDS: int = 7200

    def _run_command(self, command: List[str], cwd: str) -> str:
        """Run a shell command and return combined stdout/stderr."""
        logger.debug("Running command: %s (cwd=%s)", " ".join(command), cwd)
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
                timeout=self._COMMAND_TIMEOUT_SECONDS,
            )
            combined_output = (result.stdout or "") + (result.stderr or "")
            return combined_output.strip()
        except subprocess.TimeoutExpired as exc:
            logger.error(
                "Command timed out after %ds: %s",
                self._COMMAND_TIMEOUT_SECONDS,
                " ".join(command),
            )
            raise RuntimeError(
                f"Command {' '.join(command)} timed out after {self._COMMAND_TIMEOUT_SECONDS}s"
            ) from exc
        except subprocess.CalledProcessError as exc:
            output = (exc.stdout or "") + (exc.stderr or "")
            logger.error("Command failed: %s", output)
            # Surface the actual compiler output so downstream error parsers can
            # reliably extract file references and diagnostics.
            detailed_message = (
                f"Command {' '.join(command)} failed with exit code {exc.returncode}\n"
                f"{output.strip()}"
            ).strip()
            raise RuntimeError(detailed_message) from exc

    # ── T8: Post-Angular FE↔BE contract audit ─────────────────────────────────

    _MAX_FE_BE_AUDIT_ATTEMPTS = 2

    def _collect_ts_source_files(self, workspace_dir: str) -> Dict[str, str]:
        """Return relative_path -> content for TS/HTML app source files in workspace."""
        result: Dict[str, str] = {}
        src_path = Path(workspace_dir) / "src" / "app"
        if not src_path.exists():
            src_path = Path(workspace_dir) / "src"
        if not src_path.exists():
            return result
        exclude_dirs = {"node_modules", "dist", ".angular", "out-tsc"}
        for file_path in src_path.rglob("*.ts"):
            if any(exc in file_path.parts for exc in exclude_dirs):
                continue
            if file_path.stat().st_size > 60_000:
                continue
            try:
                rel = str(file_path.relative_to(Path(workspace_dir)))
                result[rel] = file_path.read_text(encoding="utf-8")
            except Exception:
                pass
        return result

    def _extract_call_function_invocations(self, ts_content: str) -> List[Dict[str, Any]]:
        """
        Heuristically extract .callFunction('id', params) sites from a TS file.

        Returns list of dicts:
          function_id    – string literal passed as first arg
          response_type  – generic type T from callFunction<T>(...), or None
          param_keys     – keys found in the params object (may be empty)
          undetermined   – True when we cannot reliably parse the params
          raw_snippet    – first 250 chars of the matched call for reporting
        """
        invocations: List[Dict[str, Any]] = []

        # Match: .callFunction<T>('functionId', ...)  (generic type optional)
        call_re = re.compile(
            r'\.callFunction(?:<([^>]*)>)?\s*\(\s*[\'"]([^\'"]+)[\'"]\s*,\s*',
            re.MULTILINE,
        )

        for m in call_re.finditer(ts_content):
            # group 1 = generic type T (may be None), group 2 = function_id string
            response_type = (m.group(1) or "").strip() or None
            fn_id = m.group(2)
            rest = ts_content[m.end():]

            inv: Dict[str, Any] = {
                "function_id": fn_id,
                "response_type": response_type,
                "param_keys": [],
                "undetermined": False,
                "raw_snippet": ts_content[m.start(): m.start() + 250],
            }

            rest_stripped = rest.lstrip()
            if rest_stripped.startswith("{"):
                # Inline object literal — walk to find matching }
                depth = 0
                end_idx = 0
                for i, ch in enumerate(rest_stripped):
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end_idx = i + 1
                            break
                obj_literal = rest_stripped[:end_idx]
                # Extract keys: word followed by ':'  (skip nested value colons via simple filter)
                keys = re.findall(r'(?:^|[{,])\s*([a-zA-Z_$][a-zA-Z0-9_$]*)\s*:', obj_literal)
                inv["param_keys"] = [k for k in keys if k not in ("http", "https")]
            else:
                # Variable / spread — try to find the variable construction above
                var_name_match = re.match(r'([a-zA-Z_$][a-zA-Z0-9_$]*)\s*[,)]', rest_stripped)
                if var_name_match:
                    var_name = var_name_match.group(1)
                    # Look for: const/let varName = { ... } or varName = { ... }
                    var_def_re = re.compile(
                        r'(?:(?:const|let|var)\s+)?' + re.escape(var_name) + r'\s*[:=]\s*(\{[^}]*\})',
                        re.MULTILINE,
                    )
                    var_m = var_def_re.search(ts_content)
                    if var_m:
                        keys = re.findall(
                            r'(?:^|[{,])\s*([a-zA-Z_$][a-zA-Z0-9_$]*)\s*:',
                            var_m.group(1),
                        )
                        inv["param_keys"] = keys
                    else:
                        inv["undetermined"] = True
                else:
                    inv["undetermined"] = True

            invocations.append(inv)

        return invocations

    def _audit_fe_be_contract(
        self,
        workspace_dir: str,
        backend_metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        T8 Step A — Programmatic FE↔BE contract audit.

        For every callFunction site in the generated Angular source, verify that
        all `required: true` fields from the backend `input_schema` are present
        in the params object being passed.

        Returns a list of failure records (empty list = all good):
          {
            "file"                  : relative path
            "function_id"           : str
            "missing_required_fields": [str, ...]
            "found_keys"            : [str, ...]
            "required_fields"       : [str, ...]
            "raw_snippet"           : str
          }
        """
        # Build lookup: function_id → required field names
        required_by_fn: Dict[str, List[str]] = {}
        for fn in backend_metadata.get("functions", []):
            fn_id = fn.get("id") or fn.get("function_id")
            if not fn_id:
                continue
            input_schema = fn.get("input_schema") or {}
            fields = input_schema.get("fields") or []
            req = [f["name"] for f in fields if isinstance(f, dict) and f.get("required") is True]
            if req:
                required_by_fn[fn_id] = req

        if not required_by_fn:
            logger.debug("T8 audit: no dynamic functions with required fields — skipping")
            return []

        ts_files = self._collect_ts_source_files(workspace_dir)
        failures: List[Dict[str, Any]] = []

        for rel_path, content in ts_files.items():
            invocations = self._extract_call_function_invocations(content)
            for inv in invocations:
                fn_id = inv["function_id"]
                if fn_id not in required_by_fn:
                    continue
                if inv["undetermined"]:
                    logger.debug(
                        "T8 audit: undetermined params in %s for %s — skipping",
                        rel_path,
                        fn_id,
                    )
                    continue
                req = required_by_fn[fn_id]
                found = set(inv["param_keys"])
                missing = [f for f in req if f not in found]
                if missing:
                    failures.append(
                        {
                            "file": rel_path,
                            "function_id": fn_id,
                            "missing_required_fields": missing,
                            "found_keys": inv["param_keys"],
                            "required_fields": req,
                            "raw_snippet": inv["raw_snippet"],
                        }
                    )

        return failures

    # ── Response-side (T8b) ───────────────────────────────────────────────────

    def _extract_ts_interface_fields(
        self, type_name: str, all_ts_files: Dict[str, str]
    ) -> Optional[Dict[str, Any]]:
        """
        Find the TypeScript interface/type for *type_name* across all workspace TS files.

        Returns {"file": rel_path, "declared_fields": [str, ...]} or None if not found.
        The 'declared_fields' list contains every top-level property name declared in
        the interface (both required and optional).

        Handles:
          - interface TypeName { ... }
          - export interface TypeName extends Base { ... }
          - type TypeName = { ... }          ← requires `[^{]*` to skip the `=`
          - Nested braces inside the body   ← balanced-brace walk instead of [^}]*
        """
        # Match declaration up to and including the opening `{`.
        # `\b` prevents partial-name matches (e.g. "Response" must not match "ResponseData").
        # `[^{]*` spans optional `= `, `extends Base`, or generic constraints up to the `{`.
        decl_re = re.compile(
            r'(?:export\s+)?(?:interface|type)\s+'
            + re.escape(type_name)
            + r'\b[^{]*\{',
            re.MULTILINE | re.DOTALL,
        )
        for rel_path, content in all_ts_files.items():
            m = decl_re.search(content)
            if not m:
                continue

            # Walk from the opening `{` to find the matching closing `}`,
            # correctly handling nested braces (e.g. `field: { nested: string }`).
            open_brace_pos = m.end() - 1  # position of `{`
            depth = 0
            close_pos = open_brace_pos
            for i in range(open_brace_pos, len(content)):
                if content[i] == "{":
                    depth += 1
                elif content[i] == "}":
                    depth -= 1
                    if depth == 0:
                        close_pos = i
                        break

            body = content[open_brace_pos + 1: close_pos]

            # Extract only top-level property names (depth-0 `word:` or `word?:`).
            # We walk char-by-char to skip anything inside nested `{ }`.
            fields: List[str] = []
            inner_depth = 0
            prop_re = re.compile(r'([a-zA-Z_$][a-zA-Z0-9_$]*)\s*\??:')
            i = 0
            while i < len(body):
                ch = body[i]
                if ch == "{":
                    inner_depth += 1
                    i += 1
                    continue
                if ch == "}":
                    inner_depth -= 1
                    i += 1
                    continue
                if inner_depth == 0:
                    pm = prop_re.match(body, i)
                    if pm:
                        fields.append(pm.group(1))
                        i = pm.end()
                        continue
                i += 1

            return {"file": rel_path, "declared_fields": fields}
        return None

    def _audit_fe_be_response_contract(
        self,
        workspace_dir: str,
        backend_metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        T8b — Audit the **response** side of the FE↔BE contract.

        For every callFunction<T>('id', ...) call site:
          1. Extract the generic return type T.
          2. Find interface/type T in the workspace.
          3. Compare its declared fields against response_schema.fields (required: true).
          4. Report fields missing from the interface.

        Returns a list of failure records (empty = all good):
          {
            "kind"                   : "response"
            "file"                   : TS file that has the callFunction call
            "model_file"             : TS file where interface T is defined (or None)
            "function_id"            : str
            "response_type"          : str (TypeScript type name)
            "missing_response_fields": [str, ...]
            "declared_fields"        : [str, ...]
            "required_response_fields": [str, ...]
            "raw_snippet"            : str
          }
        """
        # Build lookup: function_id → required response field names
        required_resp_by_fn: Dict[str, List[str]] = {}
        for fn in backend_metadata.get("functions", []):
            fn_id = fn.get("id") or fn.get("function_id")
            if not fn_id:
                continue
            resp_schema = fn.get("response_schema") or {}
            fields = resp_schema.get("fields") or []
            req = [
                f["name"] for f in fields
                if isinstance(f, dict) and f.get("required") is True
            ]
            if req:
                required_resp_by_fn[fn_id] = req

        if not required_resp_by_fn:
            logger.debug("T8b audit: no dynamic functions with required response fields — skipping")
            return []

        ts_files = self._collect_ts_source_files(workspace_dir)
        failures: List[Dict[str, Any]] = []

        for rel_path, content in ts_files.items():
            invocations = self._extract_call_function_invocations(content)
            for inv in invocations:
                fn_id = inv["function_id"]
                if fn_id not in required_resp_by_fn:
                    continue
                resp_type = inv.get("response_type")
                if not resp_type or resp_type in ("any", "unknown", "void", "object"):
                    # Untyped call — we can't verify interface
                    logger.debug(
                        "T8b audit: untyped callFunction in %s for %s — skipping",
                        rel_path, fn_id,
                    )
                    continue

                req_fields = required_resp_by_fn[fn_id]
                iface_info = self._extract_ts_interface_fields(resp_type, ts_files)

                if iface_info is None:
                    # Interface not found at all — that is itself a problem
                    failures.append({
                        "kind": "response",
                        "file": rel_path,
                        "model_file": None,
                        "function_id": fn_id,
                        "response_type": resp_type,
                        "missing_response_fields": req_fields,
                        "declared_fields": [],
                        "required_response_fields": req_fields,
                        "raw_snippet": inv["raw_snippet"],
                        "issue": "interface_not_found",
                    })
                    continue

                declared = set(iface_info["declared_fields"])
                missing = [f for f in req_fields if f not in declared]
                if missing:
                    failures.append({
                        "kind": "response",
                        "file": rel_path,
                        "model_file": iface_info["file"],
                        "function_id": fn_id,
                        "response_type": resp_type,
                        "missing_response_fields": missing,
                        "declared_fields": iface_info["declared_fields"],
                        "required_response_fields": req_fields,
                        "raw_snippet": inv["raw_snippet"],
                        "issue": "missing_fields_in_interface",
                    })

        return failures

    def _build_fe_be_repair_prompt(
        self,
        workspace_dir: str,
        backend_metadata: Dict[str, Any],
        ir_schema: Optional[Dict[str, Any]],
        failures: List[Dict[str, Any]],
    ) -> str:
        """
        Build the LLM patch prompt for T8.

        Handles both kinds of failure:
          - kind="request"  (missing keys in callFunction params)
          - kind="response" (missing fields in TypeScript response interface)
        """
        ts_files = self._collect_ts_source_files(workspace_dir)

        # Split failures by kind
        req_failures = [f for f in failures if f.get("kind", "request") == "request"]
        resp_failures = [f for f in failures if f.get("kind") == "response"]

        # Build per-failure context blocks
        failure_blocks: List[str] = []
        for i, f in enumerate(failures, 1):
            kind = f.get("kind", "request")
            if kind == "request":
                failure_blocks.append(
                    f"--- Failure #{i} [REQUEST params] ---\n"
                    f"File       : {f['file']}\n"
                    f"FunctionId : {f['function_id']}\n"
                    f"Required fields  : {f['required_fields']}\n"
                    f"Fields in params : {f['found_keys']}\n"
                    f"MISSING (add to params): {f['missing_required_fields']}\n"
                    f"Snippet:\n{f['raw_snippet']}\n"
                )
            else:
                failure_blocks.append(
                    f"--- Failure #{i} [RESPONSE interface] ---\n"
                    f"Call file  : {f['file']}\n"
                    f"Model file : {f.get('model_file') or '(not found)'}\n"
                    f"FunctionId : {f['function_id']}\n"
                    f"TypeScript type : {f.get('response_type')}\n"
                    f"Required response fields : {f['required_response_fields']}\n"
                    f"Declared fields in interface: {f.get('declared_fields', [])}\n"
                    f"MISSING (add to interface): {f['missing_response_fields']}\n"
                    f"Issue      : {f.get('issue')}\n"
                    f"Snippet:\n{f['raw_snippet']}\n"
                )

        # Backend schemas for all failing function ids
        failed_fn_ids = {f["function_id"] for f in failures}
        fn_schemas: List[str] = []
        for fn in backend_metadata.get("functions", []):
            fn_id = fn.get("id") or fn.get("function_id")
            if fn_id in failed_fn_ids:
                fn_schemas.append(json.dumps({
                    "function_id": fn_id,
                    "input_schema": fn.get("input_schema"),
                    "response_schema": fn.get("response_schema"),
                }, indent=2))

        # Source files: call sites + model files + api.service.ts
        failing_files: set = {f["file"] for f in failures}
        for f in resp_failures:
            if f.get("model_file"):
                failing_files.add(f["model_file"])
        for p in ts_files:
            if "api.service" in p.lower() or "model" in p.lower():
                failing_files.add(p)

        source_blocks: List[str] = []
        for rel, content in ts_files.items():
            if rel in failing_files:
                source_blocks.append(f"=== FILE: {rel} ===\n{content}\n")

        ir_context = ""
        if ir_schema:
            ir_context = json.dumps({
                "dataModels": ir_schema.get("dataModels", {}),
                "dataSources": ir_schema.get("dataSources", []),
                "inputParameters": ir_schema.get("inputParameters", []),
                "state": ir_schema.get("state", {}),
            }, indent=2)

        # Build repair instruction sections
        req_instructions = ""
        if req_failures:
            req_instructions = (
                "\n### REQUEST side fixes\n"
                "For each REQUEST failure:\n"
                "1. Find the `callFunction` call in the listed file.\n"
                "2. Add every MISSING field to the `params` object.\n"
                "3. Read the value from an existing form control / component property / "
                "   state variable. If no source exists, add `null` as a placeholder "
                "   AND add the matching form control / `@Input()` to the component.\n"
                "4. Do NOT remove or rename existing param keys.\n"
            )
        resp_instructions = ""
        if resp_failures:
            resp_instructions = (
                "\n### RESPONSE side fixes\n"
                "For each RESPONSE failure:\n"
                "1. Open the TypeScript interface/type file shown in `Model file`.\n"
                "   If the interface is not found, create it in an appropriate model file.\n"
                "2. Add every MISSING field as a typed property.\n"
                "   Use the field type from the backend response_schema if available, "
                "   otherwise use `any`.\n"
                "3. Mark fields optional (`field?: type`) unless backend marks them required.\n"
                "4. If the component accesses `response.fieldName` but the interface "
                "   does not declare `fieldName`, add it.\n"
                "5. Do NOT remove existing interface properties.\n"
            )

        prompt = (
            "You are an Angular + Python API contract repair specialist.\n\n"
            "## PROBLEMS TO FIX\n"
            + "\n".join(failure_blocks)
            + "\n\n## BACKEND FUNCTION SCHEMAS\n"
            + "\n".join(fn_schemas)
            + "\n\n## IR SCHEMA CONTEXT\n"
            + ir_context
            + "\n\n## CURRENT SOURCE FILES\n"
            + "\n".join(source_blocks)
            + "\n\n## REPAIR INSTRUCTIONS\n"
            + req_instructions
            + resp_instructions
            + "\n\n### General rules\n"
            "- Do NOT change function signatures, routing, or unrelated logic.\n"
            "- Output ONLY the files you actually changed, in full.\n\n"
            "## OUTPUT FORMAT (strict)\n"
            "=== FILE: src/app/models/foo.model.ts ===\n"
            "<full corrected content>\n"
            "=== END FILE ===\n\n"
            "=== FILE: src/app/components/foo/foo.component.ts ===\n"
            "<full corrected content>\n"
            "=== END FILE ===\n\n"
            "(Include only files you actually changed.)\n"
        )
        return prompt

    def _parse_repair_response(self, response_text: str) -> List[GeneratedFile]:
        """Parse a T8 patch-repair LLM response into a list of GeneratedFile."""
        files: List[GeneratedFile] = []
        # The |$ branch handles the last block when LLM omits the closing === END FILE ===
        file_re = re.compile(
            r'===\s*FILE:\s*([^\n=]+?)\s*===\s*\n([\s\S]*?)(?:===\s*END FILE\s*===|(?====\s*FILE:)|$)',
            re.IGNORECASE,
        )
        for m in file_re.finditer(response_text):
            rel_path = m.group(1).strip()
            content = self._clean_generated_content(m.group(2))
            if rel_path and content.strip():
                files.append(GeneratedFile(path=rel_path, content=content))
        return files

    def _patch_fe_be_failures(
        self,
        workspace_dir: str,
        backend_metadata: Dict[str, Any],
        ir_schema: Optional[Dict[str, Any]],
        failures: List[Dict[str, Any]],
        *,
        project_id: str,
        screen_id: str,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        attempt: int = 1,
    ) -> bool:
        """
        T8 patch attempt (sync) — diff-based targeted fix.

        Sends only the failing files + contract context to the LLM.
        LLM must return ONLY the changed files.  Merges patches back into
        the workspace (no new screen_id, no npm install, no build).

        Returns True when at least one patched file was merged.
        """
        logger.info(
            "T8 patch attempt %d: %d failing site(s) across function(s) %s",
            attempt,
            len(failures),
            sorted({f["function_id"] for f in failures}),
        )

        prompt = self._build_fe_be_repair_prompt(
            workspace_dir, backend_metadata, ir_schema, failures
        )

        started_at = datetime.utcnow()
        try:
            response = sync_generate(
                self.model_name,
                prompt,
                config=GeminiGenerateConfig(temperature=0.1, max_output_tokens=16384),
            )
        except Exception as exc:
            logger.error("T8 patch attempt %d LLM call failed: %s", attempt, exc)
            return False

        finished_at = datetime.utcnow()

        if not response or not getattr(response, "candidates", None):
            logger.warning("T8 patch attempt %d: LLM returned no candidates", attempt)
            return False

        try:
            raw_text = (
                response.text
                if (hasattr(response, "text") and response.text)
                else "\n".join(
                    p.text
                    for p in response.candidates[0].content.parts
                    if hasattr(p, "text")
                )
            )
        except Exception:
            logger.warning("T8 patch attempt %d: could not extract LLM text", attempt)
            return False

        try:
            usage = extract_token_usage_from_response(response)
            record_llm_call(
                project_id=project_id or "system",
                screen_id=screen_id,
                operation="angular-generation",
                sub_operation="fe-be-contract-patch",
                model=self.model_name,
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                status="success",
                version=version,
                started_at=started_at,
                finished_at=finished_at,
                org_id=org_id,
                subscriber_id=subscriber_id,
                user_id=user_id,
            )
        except Exception as tok_exc:
            logger.debug("T8 patch: token recording failed: %s", tok_exc)

        patched_files = self._parse_repair_response(raw_text)
        if not patched_files:
            logger.warning("T8 patch attempt %d: LLM returned no file blocks", attempt)
            return False

        logger.info(
            "T8 patch attempt %d: merging %d patched file(s) into workspace",
            attempt, len(patched_files),
        )
        self._merge_generated_files(workspace_dir, patched_files)
        return True

    def _ensure_fe_be_contract_sync(
        self,
        workspace_dir: str,
        backend_metadata: Dict[str, Any],
        ir_schema: Optional[Dict[str, Any]],
        *,
        user_prompt: str,
        project_id: str,
        screen_id: str,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        """
        T8 — FE↔BE contract gate (sync, runs inside _generate_app_sync before npm build).

        Strategy:
          Phase 1 — patch loop (up to _T8_MAX_PATCH_ATTEMPTS targeted diff patches)
          Phase 2 — if still failing: one full regeneration of all app code, then
                     patch loop again (up to _T8_MAX_PATCH_ATTEMPTS patches)
          Total repair ceiling: 2 regen × 3 patches = 6 cycles max.
          Always non-blocking: logs + proceeds even if issues remain.

        Returns:
          {"passed": bool, "failures": [...], "patch_attempts": int, "regen_attempts": int}
        """
        _T8_MAX_PATCH_ATTEMPTS = 3
        _T8_MAX_REGEN_ATTEMPTS = 2

        def _combined_audit() -> List[Dict]:
            """Run both audits sharing a single workspace file scan."""
            ts_files = self._collect_ts_source_files(workspace_dir)

            # ── request side ──────────────────────────────────────────────
            required_by_fn: Dict[str, List[str]] = {}
            for fn in backend_metadata.get("functions", []):
                fn_id = fn.get("id") or fn.get("function_id")
                if not fn_id:
                    continue
                fields = (fn.get("input_schema") or {}).get("fields") or []
                req = [f["name"] for f in fields if isinstance(f, dict) and f.get("required") is True]
                if req:
                    required_by_fn[fn_id] = req

            # ── response side ─────────────────────────────────────────────
            required_resp_by_fn: Dict[str, List[str]] = {}
            for fn in backend_metadata.get("functions", []):
                fn_id = fn.get("id") or fn.get("function_id")
                if not fn_id:
                    continue
                fields = (fn.get("response_schema") or {}).get("fields") or []
                req = [f["name"] for f in fields if isinstance(f, dict) and f.get("required") is True]
                if req:
                    required_resp_by_fn[fn_id] = req

            all_failures: List[Dict] = []

            for rel_path, content in ts_files.items():
                invocations = self._extract_call_function_invocations(content)
                for inv in invocations:
                    fn_id = inv["function_id"]

                    # Request side check
                    if fn_id in required_by_fn and not inv["undetermined"]:
                        req = required_by_fn[fn_id]
                        missing = [f for f in req if f not in set(inv["param_keys"])]
                        if missing:
                            all_failures.append({
                                "kind": "request",
                                "file": rel_path,
                                "function_id": fn_id,
                                "missing_required_fields": missing,
                                "found_keys": inv["param_keys"],
                                "required_fields": req,
                                "raw_snippet": inv["raw_snippet"],
                            })

                    # Response side check
                    if fn_id in required_resp_by_fn:
                        resp_type = inv.get("response_type")
                        if resp_type and resp_type not in ("any", "unknown", "void", "object"):
                            req_fields = required_resp_by_fn[fn_id]
                            iface_info = self._extract_ts_interface_fields(resp_type, ts_files)
                            if iface_info is None:
                                all_failures.append({
                                    "kind": "response",
                                    "file": rel_path,
                                    "model_file": None,
                                    "function_id": fn_id,
                                    "response_type": resp_type,
                                    "missing_response_fields": req_fields,
                                    "declared_fields": [],
                                    "required_response_fields": req_fields,
                                    "raw_snippet": inv["raw_snippet"],
                                    "issue": "interface_not_found",
                                })
                            else:
                                declared = set(iface_info["declared_fields"])
                                missing = [f for f in req_fields if f not in declared]
                                if missing:
                                    all_failures.append({
                                        "kind": "response",
                                        "file": rel_path,
                                        "model_file": iface_info["file"],
                                        "function_id": fn_id,
                                        "response_type": resp_type,
                                        "missing_response_fields": missing,
                                        "declared_fields": iface_info["declared_fields"],
                                        "required_response_fields": req_fields,
                                        "raw_snippet": inv["raw_snippet"],
                                        "issue": "missing_fields_in_interface",
                                    })

            return all_failures

        def _patch_loop(failures: List[Dict], phase_label: str) -> List[Dict]:
            """Run up to _T8_MAX_PATCH_ATTEMPTS patch cycles; return remaining failures."""
            for patch_num in range(1, _T8_MAX_PATCH_ATTEMPTS + 1):
                patched = self._patch_fe_be_failures(
                    workspace_dir,
                    backend_metadata,
                    ir_schema,
                    failures,
                    project_id=project_id,
                    screen_id=screen_id,
                    version=version,
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                    user_id=user_id,
                    attempt=patch_num,
                )
                if not patched:
                    logger.warning(
                        "T8 [%s] patch %d produced no patches — stopping patch loop",
                        phase_label, patch_num,
                    )
                    break
                failures = _combined_audit()
                if not failures:
                    logger.info(
                        "T8 [%s] all issues resolved after patch %d.", phase_label, patch_num
                    )
                    return []
                logger.warning(
                    "T8 [%s] patch %d: %d issue(s) remain", phase_label, patch_num, len(failures)
                )
            return failures

        # ── guard ────────────────────────────────────────────────────────────
        if not workspace_dir or not Path(workspace_dir).exists():
            logger.debug("T8: workspace not found — skipping FE↔BE audit")
            return {"passed": True, "failures": [], "patch_attempts": 0, "regen_attempts": 0}

        # ── Step A: initial audit (request + response) ────────────────────────
        failures = _combined_audit()
        if not failures:
            logger.info("T8 FE↔BE audit passed with no issues (both request and response sides).")
            return {"passed": True, "failures": [], "patch_attempts": 0, "regen_attempts": 0}

        req_count = sum(1 for f in failures if f.get("kind", "request") == "request")
        resp_count = sum(1 for f in failures if f.get("kind") == "response")
        logger.warning(
            "T8 FE↔BE audit found %d violation(s): %d request-side, %d response-side",
            len(failures), req_count, resp_count,
        )
        if progress_callback:
            progress_callback(
                "WARNING",
                f"FE↔BE contract audit: {len(failures)} issue(s) "
                f"({req_count} request, {resp_count} response) — applying targeted patches...",
            )

        total_patch_attempts = 0
        total_regen_attempts = 0

        # ── Phase 1: patch loop ───────────────────────────────────────────────
        failures = _patch_loop(failures, "phase1")
        total_patch_attempts += _T8_MAX_PATCH_ATTEMPTS

        if not failures:
            if progress_callback:
                progress_callback("INFO", "FE↔BE contract audit passed after patching.")
            return {
                "passed": True, "failures": [],
                "patch_attempts": total_patch_attempts, "regen_attempts": 0,
            }

        # ── Phase 2: regenerate + patch loop (up to _T8_MAX_REGEN_ATTEMPTS) ──
        for regen_num in range(1, _T8_MAX_REGEN_ATTEMPTS + 1):
            total_regen_attempts += 1
            logger.info(
                "T8: %d issue(s) remain after patching — full regeneration %d/%d",
                len(failures), regen_num, _T8_MAX_REGEN_ATTEMPTS,
            )
            if progress_callback:
                progress_callback(
                    "WARNING",
                    f"FE↔BE: {len(failures)} issue(s) persist — triggering full regeneration "
                    f"({regen_num}/{_T8_MAX_REGEN_ATTEMPTS})...",
                )

            # Full LLM regeneration of all app files (reuses same prompt + backend context)
            try:
                final_prompt = self._build_prompt(
                    user_prompt,
                    ir_schema=ir_schema,
                    backend_metadata=backend_metadata,
                )
                raw_response, _, _, _ = self._invoke_model(
                    final_prompt,
                    model_name=self.model_name,
                    generation_config=self.generation_config,
                    project_id=project_id,
                    screen_id=screen_id,
                    version=version,
                    operation="generate-angular-app",
                    sub_operation=f"fe-be-regen-{regen_num}",
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                    user_id=user_id,
                )
                regen_files, _ = self._parse_generated_files(raw_response)
                self._merge_generated_files(workspace_dir, regen_files)
                logger.info(
                    "T8 regen %d: merged %d file(s) into workspace",
                    regen_num, len(regen_files),
                )
            except Exception as regen_exc:
                logger.error("T8 regen %d failed: %s — continuing with patch loop", regen_num, regen_exc)

            # Re-audit after regen (both sides)
            failures = _combined_audit()
            if not failures:
                logger.info("T8 FE↔BE audit passed after regen %d.", regen_num)
                if progress_callback:
                    progress_callback("INFO", f"FE↔BE contract resolved after regeneration {regen_num}.")
                return {
                    "passed": True, "failures": [],
                    "patch_attempts": total_patch_attempts,
                    "regen_attempts": total_regen_attempts,
                }

            # Patch loop after regen
            failures = _patch_loop(failures, f"regen{regen_num}")
            total_patch_attempts += _T8_MAX_PATCH_ATTEMPTS

            if not failures:
                if progress_callback:
                    progress_callback("INFO", f"FE↔BE contract resolved after regen {regen_num} + patching.")
                return {
                    "passed": True, "failures": [],
                    "patch_attempts": total_patch_attempts,
                    "regen_attempts": total_regen_attempts,
                }

        # ── All cycles exhausted — non-blocking, log and proceed ─────────────
        req_left = sum(1 for f in failures if f.get("kind", "request") == "request")
        resp_left = sum(1 for f in failures if f.get("kind") == "response")
        logger.error(
            "T8 FE↔BE audit: %d issue(s) remain after %d regen(s) + %d patch cycle(s) "
            "(%d request-side, %d response-side). Runtime errors possible.",
            len(failures), total_regen_attempts, total_patch_attempts, req_left, resp_left,
        )
        if progress_callback:
            progress_callback(
                "WARNING",
                f"FE↔BE contract: {len(failures)} issue(s) could not be resolved "
                f"({req_left} request, {resp_left} response). "
                "Proceeding — some backend calls may fail at runtime.",
            )
        return {
            "passed": False,
            "failures": failures,
            "patch_attempts": total_patch_attempts,
            "regen_attempts": total_regen_attempts,
        }

    # ── end T8 ────────────────────────────────────────────────────────────────

    def _write_files_to_temp(
        self, *, files: List[GeneratedFile], project_id: str, screen_id: str
    ) -> str:
        """Create a temp directory and persist the generated Angular app structure.
        
        NOTE: This method is kept for backwards compatibility but is deprecated.
        Use _create_workspace + _copy_base_project + _merge_generated_files instead.
        """
        logger.debug("Preparing temp directory name...")
        safe_project = re.sub(r"[^a-zA-Z0-9_\-]", "_", project_id)
        safe_screen = re.sub(r"[^a-zA-Z0-9_\-]", "_", screen_id)
        temp_dir = tempfile.mkdtemp(
            prefix=f"angular_app_{safe_project}_{safe_screen}_"
        )
        logger.info(f"Temporary directory created: {temp_dir}")
        for generated_file in files:
            absolute_path = os.path.join(temp_dir, generated_file.path)
            parent_dir = os.path.dirname(absolute_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)

            logger.debug(f"Writing file: {absolute_path}")
            with open(absolute_path, "w", encoding="utf-8") as file:
                file.write(generated_file.content.rstrip() + "\n")
        
        logger.info("All generated files written successfully.")
        return temp_dir

