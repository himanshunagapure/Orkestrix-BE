"""
Package installer for sandbox functions.
Automatically detects and installs required Python packages.
"""

import ast
import logging
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Set, Optional

logger = logging.getLogger(__name__)

# Cache for stdlib module detection
_stdlib_modules_cache: Optional[Set[str]] = None

# Map of import names to pip package names (when they differ)
IMPORT_TO_PACKAGE = {
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "pytesseract": "pytesseract",  # Note: Also requires Tesseract OCR engine installed at system level
    "PyPDF2": "PyPDF2",
    "pdfplumber": "pdfplumber",
    "docx": "python-docx",  # python-docx package provides 'docx' module
    "reportlab": "reportlab",
    "fpdf": "fpdf2",
    "weasyprint": "weasyprint",
    "stripe": "stripe",
    "bcrypt": "bcrypt",
    "jwt": "PyJWT",
    "pandas": "pandas",
    "numpy": "numpy",
    "requests": "requests",
    "httpx": "httpx",
    "aiohttp": "aiohttp",
    "redis": "redis",
    "celery": "celery",
    "sqlalchemy": "sqlalchemy",
    "openpyxl": "openpyxl",
    "xlrd": "xlrd",
    "matplotlib": "matplotlib",
    "seaborn": "seaborn",
    "plotly": "plotly",
    "boto3": "boto3",
    "tweepy": "tweepy",
    "selenium": "selenium",
    "scrapy": "scrapy",
    "openai": "openai",
    "anthropic": "anthropic",
}

# Packages that should NOT be installed (stdlib or already available)
# This is a fallback list for modules that might not be detected dynamically
STDLIB_MODULES_FALLBACK = {
    # Core packages that are always available (not stdlib but pre-installed)
    "bson",
    "pydantic",
    "motor",
    "pymongo",
}

# Cache file for tracking system-level dependencies
SYSTEM_DEPS_CACHE_FILE = Path("/tmp/sandbox_system_deps.txt")


def is_stdlib_module(module_name: str) -> bool:
    """
    Dynamically detect if a module is part of Python's standard library.
    
    Args:
        module_name: Name of the module to check
        
    Returns:
        True if the module is part of stdlib, False otherwise
    """
    global _stdlib_modules_cache
    
    # Initialize cache on first call
    if _stdlib_modules_cache is None:
        _stdlib_modules_cache = set()
        
        # Method 1: Use sys.stdlib_module_names (Python 3.10+)
        # This is the most reliable and fastest method
        if hasattr(sys, 'stdlib_module_names'):
            _stdlib_modules_cache = set(sys.stdlib_module_names)
        else:
            # Method 2: For older Python versions, build cache dynamically
            # We'll populate it as we check modules
            pass
    
    # Check cache first (works for Python 3.10+ with full cache, or cached results)
    if module_name in _stdlib_modules_cache:
        return True
    
    # If we have the full stdlib set (Python 3.10+), we're done
    if hasattr(sys, 'stdlib_module_names'):
        return False
    
    # For older Python versions, check dynamically and cache the result
    try:
        import importlib.util
        import pkgutil
        
        # Check if it's a builtin module first
        if module_name in sys.builtin_module_names:
            _stdlib_modules_cache.add(module_name)
            return True
        
        # Try to find the module spec (doesn't import the module)
        spec = importlib.util.find_spec(module_name)
        
        if spec is None:
            # Module doesn't exist or can't be found
            return False
        
        # Check if it's a namespace package or has an origin
        if spec.origin is None:
            # Namespace package - check if it's in stdlib paths
            if spec.submodule_search_locations:
                # Check if any search location is in stdlib
                for location in spec.submodule_search_locations:
                    if pkgutil.get_importer(location) is not None:
                        # This is a heuristic - if it's importable via pkgutil
                        # and not in site-packages, it's likely stdlib
                        stdlib_path = Path(location)
                        if 'site-packages' not in str(stdlib_path) and 'dist-packages' not in str(stdlib_path):
                            _stdlib_modules_cache.add(module_name)
                            return True
            return False
        
        # Check if the module's file is in a stdlib path
        origin_path = Path(spec.origin)
        
        # Get standard library paths (exclude site-packages)
        stdlib_paths = []
        for path in sys.path:
            path_str = str(path)
            if 'site-packages' not in path_str and 'dist-packages' not in path_str:
                stdlib_paths.append(Path(path))
        
        # Check if origin is in any stdlib path
        for stdlib_path in stdlib_paths:
            try:
                # Check if origin_path is relative to stdlib_path
                origin_path.relative_to(stdlib_path)
                _stdlib_modules_cache.add(module_name)
                return True
            except ValueError:
                # Not relative to this stdlib path, continue
                continue
        
        # Not in stdlib paths
        return False
        
    except (ImportError, ValueError, AttributeError, TypeError) as e:
        # If we can't determine, check builtin modules as fallback
        if module_name in sys.builtin_module_names:
            _stdlib_modules_cache.add(module_name)
            return True
        logger.debug(f"Could not determine if {module_name} is stdlib: {e}")
        return False


def check_tesseract_installed() -> bool:
    """
    Check if Tesseract OCR engine is installed at system level.
    
    Returns:
        True if Tesseract is available, False otherwise
    """
    # Check cache first
    if SYSTEM_DEPS_CACHE_FILE.exists():
        try:
            installed_deps = SYSTEM_DEPS_CACHE_FILE.read_text().strip().split("\n")
            if "tesseract" in installed_deps:
                # Verify it's still available
                tesseract_path = shutil.which("tesseract")
                if tesseract_path:
                    return True
        except Exception:
            pass
    
    # Check if tesseract command is available in PATH
    tesseract_path = shutil.which("tesseract")
    if tesseract_path:
        try:
            # Verify it's actually tesseract by checking version
            result = subprocess.run(
                ["tesseract", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                # Cache the result
                try:
                    SYSTEM_DEPS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                    SYSTEM_DEPS_CACHE_FILE.write_text("tesseract\n")
                except Exception:
                    pass
                logger.info("Tesseract OCR engine is available at: %s", tesseract_path)
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
            logger.debug(f"Tesseract check failed: {e}")
    return False


def install_tesseract_system_package() -> bool:
    """
    Attempt to install Tesseract OCR engine using system package manager.
    
    Returns:
        True if installation succeeded or Tesseract is already installed, False otherwise
    """
    # Check if already installed
    if check_tesseract_installed():
        logger.info("Tesseract OCR engine is already installed")
        return True
    
    system = platform.system().lower()
    logger.info(f"Attempting to install Tesseract OCR engine on {system}")
    
    try:
        if system == "linux":
            # Try to detect Linux distribution
            distro = ""
            try:
                # Try /etc/os-release first (most reliable)
                if Path("/etc/os-release").exists():
                    os_release = Path("/etc/os-release").read_text()
                    for line in os_release.split("\n"):
                        if line.startswith("ID="):
                            distro = line.split("=")[1].strip('"').lower()
                            break
            except Exception:
                pass
            
            # Try apt (Debian/Ubuntu)
            if shutil.which("apt-get") or "debian" in distro or "ubuntu" in distro:
                logger.info("Installing tesseract-ocr using apt-get...")
                # Try without sudo first (in case we're already root)
                result = subprocess.run(
                    ["apt-get", "update", "-qq"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if result.returncode == 0:
                    result = subprocess.run(
                        ["apt-get", "install", "-y", "-qq", "tesseract-ocr"],
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    if result.returncode == 0:
                        logger.info("Successfully installed tesseract-ocr")
                        try:
                            SYSTEM_DEPS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                            SYSTEM_DEPS_CACHE_FILE.write_text("tesseract\n")
                        except Exception:
                            pass
                        return True
                    else:
                        # Try with sudo if available
                        if shutil.which("sudo"):
                            logger.info("Retrying with sudo...")
                            result = subprocess.run(
                                ["sudo", "apt-get", "update", "-qq"],
                                capture_output=True,
                                text=True,
                                timeout=120,
                            )
                            if result.returncode == 0:
                                result = subprocess.run(
                                    ["sudo", "apt-get", "install", "-y", "-qq", "tesseract-ocr"],
                                    capture_output=True,
                                    text=True,
                                    timeout=120,
                                )
                                if result.returncode == 0:
                                    logger.info("Successfully installed tesseract-ocr (with sudo)")
                                    try:
                                        SYSTEM_DEPS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                                        SYSTEM_DEPS_CACHE_FILE.write_text("tesseract\n")
                                    except Exception:
                                        pass
                                    return True
                        logger.warning(
                            f"apt-get install failed: {result.stderr}. "
                            "Install manually: sudo apt-get install -y tesseract-ocr"
                        )
            
            # Try yum/dnf (CentOS/RHEL/Fedora)
            elif shutil.which("yum") or shutil.which("dnf") or "centos" in distro or "rhel" in distro or "fedora" in distro:
                package_manager = "dnf" if shutil.which("dnf") else "yum"
                logger.info(f"Installing tesseract using {package_manager}...")
                
                result = subprocess.run(
                    [package_manager, "install", "-y", "-q", "tesseract"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if result.returncode == 0:
                    logger.info("Successfully installed tesseract")
                    try:
                        SYSTEM_DEPS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                        SYSTEM_DEPS_CACHE_FILE.write_text("tesseract\n")
                    except Exception:
                        pass
                    return True
                else:
                    # Try with sudo if available
                    if shutil.which("sudo"):
                        logger.info(f"Retrying with sudo...")
                        result = subprocess.run(
                            ["sudo", package_manager, "install", "-y", "-q", "tesseract"],
                            capture_output=True,
                            text=True,
                            timeout=120,
                        )
                        if result.returncode == 0:
                            logger.info(f"Successfully installed tesseract (with sudo)")
                            try:
                                SYSTEM_DEPS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                                SYSTEM_DEPS_CACHE_FILE.write_text("tesseract\n")
                            except Exception:
                                pass
                            return True
                    logger.warning(
                        f"{package_manager} install failed: {result.stderr}. "
                        f"Install manually: sudo {package_manager} install -y tesseract"
                    )
            else:
                logger.warning("Could not detect Linux package manager (apt-get/yum/dnf)")
        
        elif system == "darwin":  # macOS
            if shutil.which("brew"):
                logger.info("Installing tesseract using Homebrew...")
                result = subprocess.run(
                    ["brew", "install", "tesseract"],
                    capture_output=True,
                    text=True,
                    timeout=300,  # Homebrew can be slow
                )
                if result.returncode == 0:
                    logger.info("Successfully installed tesseract")
                    try:
                        SYSTEM_DEPS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                        SYSTEM_DEPS_CACHE_FILE.write_text("tesseract\n")
                    except Exception:
                        pass
                    return True
                else:
                    logger.warning(f"brew install failed: {result.stderr}")
            else:
                logger.warning("Homebrew not found. Install Tesseract manually: brew install tesseract")
        
        elif system == "windows":
            logger.warning(
                "Windows detected. Tesseract must be installed manually. "
                "Download from: https://github.com/UB-Mannheim/tesseract/wiki"
            )
        
        else:
            logger.warning(f"Unsupported system: {system}. Install Tesseract manually.")
    
    except subprocess.TimeoutExpired:
        logger.error("Timeout installing Tesseract OCR engine")
    except FileNotFoundError:
        logger.warning("Package manager not found. Install Tesseract manually.")
    except PermissionError:
        logger.warning(
            "Permission denied. Tesseract installation requires sudo/root privileges. "
            "Install manually: apt-get install tesseract-ocr (Ubuntu/Debian) or "
            "brew install tesseract (macOS)"
        )
    except Exception as e:
        logger.error(f"Error installing Tesseract OCR engine: {e}")
    
        return False


class PackageInstaller:
    """Manages automatic package installation for sandbox functions."""
    
    def __init__(self, cache_file: Optional[Path] = None):
        """
        Initialize package installer.
        
        Args:
            cache_file: Path to cache file tracking installed packages
        """
        self.cache_file = cache_file or Path("/tmp/sandbox_installed_packages.txt")
        self._installed_packages: Set[str] = self._load_cache()
    
    def _load_cache(self) -> Set[str]:
        """Load previously installed packages from cache."""
        if self.cache_file.exists():
            try:
                return set(self.cache_file.read_text().strip().split("\n"))
            except Exception as e:
                logger.warning(f"Failed to load package cache: {e}")
        return set()
    
    def _save_cache(self) -> None:
        """Save installed packages to cache."""
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text("\n".join(sorted(self._installed_packages)))
        except Exception as e:
            logger.warning(f"Failed to save package cache: {e}")
    
    def extract_imports(self, source_code: str) -> Set[str]:
        """
        Extract import statements from Python source code.
        
        Args:
            source_code: Python source code
            
        Returns:
            Set of package names that need to be imported
        """
        imports = set()

        def _collect_from_tree(tree: ast.AST) -> None:
            for node in ast.walk(tree):
                # Handle "import package"
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        # Get top-level package name
                        package = alias.name.split(".")[0]
                        # Skip empty package names
                        if package:
                            imports.add(package)
                
                # Handle "from package import ..."
                elif isinstance(node, ast.ImportFrom):
                    # Skip relative imports (from . import X, from .. import Y)
                    if node.level > 0:
                        continue
                    
                    if node.module:
                        # Get top-level package name
                        package = node.module.split(".")[0]
                        # Skip empty package names
                        if package:
                            imports.add(package)

        def _collect_via_regex(text: str) -> None:
            import re
            # Match both at start of line and indented (inside function bodies)
            import_pattern = r'^\s*(?:from\s+(\S+)|import\s+(\S+))'
            for line in text.split('\n'):
                match = re.match(import_pattern, line)
                if match:
                    package = (match.group(1) or match.group(2)).split('.')[0]
                    if package:
                        imports.add(package)
        
        try:
            _collect_from_tree(ast.parse(source_code))
        except SyntaxError as e:
            # Tool function_code is often a bare _run body (top-level `return`), which is
            # invalid as a module. Wrap and retry before falling back to regex.
            try:
                indented = "\n".join(
                    f"    {line}" if line.strip() else line
                    for line in (source_code or "").splitlines()
                )
                _collect_from_tree(ast.parse(f"def __sb_extract_imports__():\n{indented or '    pass'}\n"))
            except SyntaxError:
                logger.debug(f"Failed to parse source code for imports: {e}")
                _collect_via_regex(source_code)
        
        logger.debug(f"Extracted imports: {imports}")
        return imports
    
    def is_module_importable(self, import_name: str) -> bool:
        """True when the top-level module can already be imported in this environment."""
        if not import_name:
            return False
        try:
            import importlib.util

            return importlib.util.find_spec(import_name) is not None
        except (ImportError, ValueError, ModuleNotFoundError, AttributeError):
            return False

    def get_packages_to_install(self, imports: Set[str]) -> Set[str]:
        """
        Determine which packages need to be installed.
        
        Args:
            imports: Set of import names from source code
            
        Returns:
            Set of pip package names that need to be installed
        """
        packages_to_install = set()
        
        for import_name in imports:
            # Skip stdlib modules (dynamic detection)
            if is_stdlib_module(import_name):
                logger.debug(f"Skipping stdlib module: {import_name}")
                continue
            
            # Skip fallback list (pre-installed packages)
            if import_name in STDLIB_MODULES_FALLBACK:
                logger.debug(f"Skipping pre-installed package: {import_name}")
                continue
            
            # Skip internal modules (services.*, repositories.*, models.*)
            if import_name.startswith(('services', 'repositories', 'models')):
                logger.debug(f"Skipping internal module: {import_name}")
                continue

            pip_package = IMPORT_TO_PACKAGE.get(import_name, import_name)

            # Already importable in this venv — remember and skip pip
            if self.is_module_importable(import_name):
                self._installed_packages.add(pip_package)
                logger.debug(f"Module already importable: {import_name}")
                continue
            
            # Skip if already installed (cache)
            if pip_package in self._installed_packages:
                logger.debug(f"Package already installed: {pip_package}")
                continue
            
            packages_to_install.add(pip_package)
        
        return packages_to_install
    
    def install_package(self, package_name: str) -> bool:
        """
        Install a single package using pip.
        
        Args:
            package_name: Name of the pip package
            
        Returns:
            True if installation succeeded, False otherwise
        """
        try:
            logger.info(f"Installing package: {package_name}")
            
            # Use subprocess to run pip install
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet", package_name],
                capture_output=True,
                text=True,
                timeout=60,  # 60 second timeout per package
            )
            
            if result.returncode == 0:
                logger.info(f"Successfully installed: {package_name}")
                self._installed_packages.add(package_name)
                self._save_cache()
                try:
                    import importlib

                    importlib.invalidate_caches()
                except Exception:
                    pass
                
                # Special handling for pytesseract - also check/install Tesseract OCR engine
                if package_name == "pytesseract":
                    logger.info("pytesseract installed. Checking for Tesseract OCR engine...")
                    if not check_tesseract_installed():
                        logger.info("Tesseract OCR engine not found. Attempting to install...")
                        if install_tesseract_system_package():
                            logger.info("Tesseract OCR engine installed successfully")
                        else:
                            logger.warning(
                                "Failed to automatically install Tesseract OCR engine. "
                                "OCR functionality may not work. Install manually: "
                                "apt-get install tesseract-ocr (Ubuntu/Debian) or "
                                "brew install tesseract (macOS) or yum install tesseract (CentOS/RHEL)"
                            )
                    else:
                        logger.info("Tesseract OCR engine is already available")
                
                return True
            else:
                error_msg = result.stderr or result.stdout or "Unknown error"
                logger.error(
                    f"Failed to install {package_name}: {error_msg}"
                )
                # Special note for pytesseract about system dependency
                if package_name == "pytesseract":
                    logger.warning(
                        "Note: pytesseract requires Tesseract OCR engine to be installed at system level. "
                        "Install with: apt-get install tesseract-ocr (Ubuntu/Debian) or "
                        "brew install tesseract (macOS)"
                )
                return False
        
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout installing {package_name}")
            return False
        except Exception as e:
            logger.error(f"Error installing {package_name}: {e}")
            return False
    
    def install_packages(self, package_names: Set[str]) -> dict:
        """
        Install multiple packages.
        
        Args:
            package_names: Set of package names to install
            
        Returns:
            Dict with installation results: {"installed": [...], "failed": [...]}
        """
        if not package_names:
            return {"installed": [], "failed": []}
        
        installed = []
        failed = []
        
        for package in package_names:
            if self.install_package(package):
                installed.append(package)
            else:
                failed.append(package)
        
        return {
            "installed": installed,
            "failed": failed,
        }
    
    def ensure_packages_for_code(self, source_code: str) -> dict:
        """
        Extract imports from code and install missing packages.
        
        Args:
            source_code: Python source code
            
        Returns:
            Dict with installation results
        """
        # Extract imports
        imports = self.extract_imports(source_code)
        logger.info(f"Detected imports: {imports}")
        
        # Check for pytesseract and ensure Tesseract OCR engine is installed
        if "pytesseract" in imports:
            if not check_tesseract_installed():
                logger.info("pytesseract detected but Tesseract OCR engine not found. Attempting to install...")
                if install_tesseract_system_package():
                    logger.info("Tesseract OCR engine installed successfully")
                else:
                    logger.warning(
                        "Failed to automatically install Tesseract OCR engine. "
                        "OCR functionality may not work. Install manually: "
                        "apt-get install tesseract-ocr (Ubuntu/Debian) or "
                        "brew install tesseract (macOS) or yum install tesseract (CentOS/RHEL)"
                    )
            else:
                logger.info("Tesseract OCR engine is available")
        
        # Determine what needs installation
        packages_to_install = self.get_packages_to_install(imports)
        
        if not packages_to_install:
            logger.info("All required packages already installed")
            return {"installed": [], "failed": [], "already_installed": True}
        
        logger.info(f"Installing packages: {packages_to_install}")
        
        # Install packages
        result = self.install_packages(packages_to_install)
        result["already_installed"] = False
        
        return result

    def ensure_packages_for_imports(
        self,
        import_statements: list,
        extra_source: str = "",
    ) -> dict:
        """
        Ensure packages for an explicit list of import statements (gap tools / DynamicToolRegistry).

        Combines the import list with optional function body source so AST extraction
        covers both the imports array and any inline references.
        """
        parts: list[str] = []
        for stmt in import_statements or []:
            text = str(stmt or "").strip()
            if text:
                parts.append(text)
        if extra_source and str(extra_source).strip():
            parts.append(str(extra_source))
        if not parts:
            return {"installed": [], "failed": [], "already_installed": True}
        return self.ensure_packages_for_code("\n".join(parts))


# Global installer instance
_package_installer: Optional[PackageInstaller] = None


def get_package_installer() -> PackageInstaller:
    """Get or create global package installer instance."""
    global _package_installer
    if _package_installer is None:
        _package_installer = PackageInstaller()
    return _package_installer


__all__ = ["PackageInstaller", "get_package_installer"]