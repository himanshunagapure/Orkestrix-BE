"""
Deployment Service for AI Screen Builder
Handles deployment of generated screens as standalone React apps
"""

import os
import json
import shutil
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime
import subprocess

class DeploymentService:
    def __init__(self, base_deploy_path: str = "./deployed_screens"):
        """
        Initialize deployment service
        
        Args:
            base_deploy_path: Root directory where all deployed screens will be stored
        """
        self.base_deploy_path = Path(base_deploy_path)
        self.base_deploy_path.mkdir(parents=True, exist_ok=True)
        
        # Template paths
        self.template_path = Path("./deployment_template")
        
    def create_deployment_template(self):
        """
        Create the base React template structure that will be copied for each deployment
        This only needs to be run once during setup
        """
        template = self.template_path
        template.mkdir(parents=True, exist_ok=True)
        
        # Create package.json
        package_json = {
            "name": "screen-runtime",
            "version": "1.0.0",
            "private": True,
            "dependencies": {
                "react": "^18.2.0",
                "react-dom": "^18.2.0",
                "react-scripts": "5.0.1",
                "lucide-react": "^0.263.1",
                "@reduxjs/toolkit": "^1.9.5",
                "react-redux": "^8.1.2"
            },
            "scripts": {
                "start": "react-scripts start",
                "build": "react-scripts build",
                "serve": "serve -s build -l 3000"
            },
            "eslintConfig": {
                "extends": ["react-app"]
            },
            "browserslist": {
                "production": [">0.2%", "not dead", "not op_mini all"],
                "development": ["last 1 chrome version", "last 1 firefox version", "last 1 safari version"]
            }
        }
        
        with open(template / "package.json", "w") as f:
            json.dump(package_json, f, indent=2)
        
        # Create public directory
        public_dir = template / "public"
        public_dir.mkdir(exist_ok=True)
        
        # Create index.html
        index_html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="theme-color" content="#000000" />
    <meta name="description" content="AI Generated Screen" />
    <title>Generated Screen</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
    </style>
</head>
<body>
    <noscript>You need to enable JavaScript to run this app.</noscript>
    <div id="root"></div>
</body>
</html>"""
        
        with open(public_dir / "index.html", "w") as f:
            f.write(index_html)
        
        # Create src directory structure
        src_dir = template / "src"
        src_dir.mkdir(exist_ok=True)
        
        # Create store directory
        store_dir = src_dir / "store"
        store_dir.mkdir(exist_ok=True)
        
        # Create index.js (entry point)
        index_js = """import React from 'react';
import ReactDOM from 'react-dom/client';
import { Provider } from 'react-redux';
import store from './store/store';
import App from './App';

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(
  <React.StrictMode>
    <Provider store={store}>
      <App />
    </Provider>
  </React.StrictMode>
);"""
        
        with open(src_dir / "index.js", "w") as f:
            f.write(index_js)
        
        print(f"✓ Deployment template created at {template}")
    
    def deploy_screen(
        self,
        screen_id: str,
        project_id: str,
        screen_spec: Dict[str, Any],
        components: list,
        theme: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Deploy a screen as a standalone React app
        
        Args:
            screen_id: Unique identifier for the screen
            project_id: Project this screen belongs to
            screen_spec: Screen specification from IR schema
            components: List of component configurations
            theme: Theme configuration
            metadata: Additional metadata
            
        Returns:
            Dict containing deployment info (url, paths, etc.)
        """
        try:
            # Create deployment directory
            deploy_path = self.base_deploy_path / project_id / screen_id
            
            # Remove existing deployment if present
            if deploy_path.exists():
                shutil.rmtree(deploy_path)
            
            # Copy template
            shutil.copytree(self.template_path, deploy_path)
            
            # Create src directory
            src_dir = deploy_path / "src"
            
            # Generate and write ComponentRenderer.tsx
            self._write_component_renderer(src_dir)
            
            # Generate and write Redux store
            self._write_redux_store(src_dir / "store", theme)
            
            # Generate and write App.js with screen data
            self._write_app_file(
                src_dir,
                screen_spec,
                components,
                metadata
            )
            
            # Write deployment metadata
            deployment_info = {
                "screen_id": screen_id,
                "project_id": project_id,
                "deployed_at": datetime.utcnow().isoformat(),
                "url": f"/{project_id}/{screen_id}",
                "path": str(deploy_path),
                "screen_title": screen_spec.get("title", "Untitled Screen"),
                "metadata": metadata or {}
            }
            
            with open(deploy_path / "deployment.json", "w") as f:
                json.dump(deployment_info, f, indent=2)
            
            # Build the React app
            build_success = self._build_react_app(deploy_path)
            
            if not build_success:
                return {
                    "success": False,
                    "error": "Build failed",
                    "deployment_info": deployment_info
                }
            
            return {
                "success": True,
                "deployment_info": deployment_info,
                "build_path": str(deploy_path / "build"),
                "url": deployment_info["url"]
            }
            
        except Exception as e:
            print(f"Deployment error: {str(e)}")
            import traceback
            traceback.print_exc()
            return {
                "success": False,
                "error": str(e)
            }
    
    def _write_component_renderer(self, src_dir: Path):
        """Write the ComponentRenderer.tsx file"""
        # Read from your existing ComponentRenderer.tsx
        # For now, we'll create a simplified version
        renderer_content = """// ComponentRenderer.tsx - Renders UI components from IR schema
import React from 'react';
import { useSelector } from 'react-redux';
import {
    Link, AlertTriangle, TrendingUp, Search, Code
} from 'lucide-react';

const ComponentRenderer = ({ component }) => {
    const theme = useSelector(state => state.theme.theme);
    
    const themeVars = {
        '--primary-color': theme.primary_color || '#2563EB',
        '--secondary-color': theme.secondary_color || '#F3F4F6',
        '--accent-color': theme.accent_color || '#14B8A6',
        '--background-color': theme.background_color || '#FFFFFF',
        '--text-color': theme.text_color || '#1F2937',
        '--font-family': theme.font_family || 'Inter',
        '--border-radius': '8px',
    };
    
    const commonProps = {
        className: component.styling?.class_names?.join(' ') || '',
        style: {
            ...themeVars,
            width: component.layout?.width,
            padding: component.styling?.padding || component.layout?.padding,
            margin: component.styling?.margin,
            backgroundColor: component.styling?.background,
            borderRadius: component.styling?.radius || 'var(--border-radius)',
        }
    };
    
    const renderChildren = () => {
        if (!component.children?.length) return null;
        return component.children.map((child, idx) => (
            <ComponentRenderer key={child.id || idx} component={child} />
        ));
    };
    
    // Render based on component type
    switch (component.type) {
        case 'container':
        case 'card':
            return (
                <div {...commonProps} className={`bg-white p-4 rounded-lg shadow-sm ${commonProps.className}`}>
                    {component.properties?.title && (
                        <h3 className="font-medium mb-2">{component.properties.title}</h3>
                    )}
                    {renderChildren()}
                </div>
            );
            
        case 'text':
            const variants = {
                'h1': 'text-2xl font-bold',
                'h2': 'text-xl font-semibold',
                'h3': 'text-lg font-medium',
                'body': 'text-base',
                'caption': 'text-sm text-gray-500'
            };
            return (
                <div {...commonProps} className={variants[component.properties?.variant] || variants.body}>
                    {component.properties?.content || component.properties?.text}
                </div>
            );
            
        case 'button':
            return (
                <button
                    {...commonProps}
                    className={`px-6 py-3 rounded-lg font-medium text-white ${commonProps.className}`}
                    style={{
                        ...commonProps.style,
                        backgroundColor: component.styling?.backgroundColor || 'var(--primary-color)'
                    }}
                >
                    {component.properties?.text || 'Button'}
                </button>
            );
            
        case 'input':
            return (
                <div className="flex flex-col gap-1">
                    <label className="text-sm font-medium">{component.properties?.label}</label>
                    <input
                        type={component.properties?.type || 'text'}
                        placeholder={component.properties?.placeholder}
                        className="border border-gray-300 px-3 py-2 rounded-lg"
                        style={{ borderRadius: 'var(--border-radius)' }}
                    />
                </div>
            );
            
        case 'table':
            const columns = component.properties?.columns || [];
            const data = component.properties?.data || [];
            return (
                <div {...commonProps} className="bg-white rounded-lg shadow-sm overflow-auto">
                    <table className="min-w-full text-sm">
                        <thead className="bg-gray-50">
                            <tr>
                                {columns.map((col, i) => (
                                    <th key={i} className="px-4 py-2 text-left font-semibold">
                                        {col.label || col.field}
                                    </th>
                                ))}
                            </tr>
                        </thead>
                        <tbody>
                            {data.map((row, i) => (
                                <tr key={i} className="border-t">
                                    {columns.map((col, j) => (
                                        <td key={j} className="px-4 py-2">{row[col.field]}</td>
                                    ))}
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            );
            
        case 'grid':
            return (
                <div
                    {...commonProps}
                    className="grid gap-4"
                    style={{
                        ...commonProps.style,
                        gridTemplateColumns: `repeat(${component.properties?.columns || 3}, 1fr)`
                    }}
                >
                    {renderChildren()}
                </div>
            );
            
        default:
            return (
                <div {...commonProps} className="bg-gray-100 p-4 rounded-lg">
                    <div className="text-sm text-gray-600">
                        Component: {component.type}
                    </div>
                    {renderChildren()}
                </div>
            );
    }
};

export default ComponentRenderer;"""
        
        with open(src_dir / "ComponentRenderer.js", "w") as f:
            f.write(renderer_content)
    
    def _write_redux_store(self, store_dir: Path, theme: Dict[str, Any]):
        """Write Redux store configuration"""
        store_content = f"""import {{ configureStore, createSlice }} from '@reduxjs/toolkit';

const themeSlice = createSlice({{
    name: 'theme',
    initialState: {{
        theme: {json.dumps(theme)}
    }},
    reducers: {{}}
}});

const store = configureStore({{
    reducer: {{
        theme: themeSlice.reducer
    }}
}});

export default store;"""
        
        with open(store_dir / "store.js", "w") as f:
            f.write(store_content)
    
    def _write_app_file(
        self,
        src_dir: Path,
        screen_spec: Dict[str, Any],
        components: list,
        metadata: Optional[Dict[str, Any]]
    ):
        """Write the main App.js file with screen data"""
        app_content = f"""import React from 'react';
import ComponentRenderer from './ComponentRenderer';

const screenData = {json.dumps({
    'screen_spec': screen_spec,
    'components': components,
    'metadata': metadata or {}
}, indent=2)};

function App() {{
    return (
        <div className="min-h-screen" style={{{{
            backgroundColor: 'var(--background-color)',
            color: 'var(--text-color)',
            fontFamily: 'var(--font-family)'
        }}}}>
            <div className="max-w-7xl mx-auto p-6">
                <h1 className="text-3xl font-bold mb-6">
                    {{screenData.screen_spec.title || 'Generated Screen'}}
                </h1>
                <div className="flex flex-col gap-4">
                    {{screenData.components.map((component, idx) => (
                        <ComponentRenderer key={{component.id || idx}} component={{component}} />
                    ))}}
                </div>
            </div>
        </div>
    );
}}

export default App;"""
        
        with open(src_dir / "App.js", "w") as f:
            f.write(app_content)
    
    def _build_react_app(self, deploy_path: Path) -> bool:
        """Build the React application"""
        try:
            print(f"Building React app at {deploy_path}...")
            
            # Install dependencies (only if node_modules doesn't exist)
            if not (deploy_path / "node_modules").exists():
                print("Installing dependencies...")
                subprocess.run(
                    ["npm", "install"],
                    cwd=deploy_path,
                    check=True,
                    capture_output=True
                )
            
            # Build the app
            print("Building production bundle...")
            result = subprocess.run(
                ["npm", "run", "build"],
                cwd=deploy_path,
                check=True,
                capture_output=True,
                text=True
            )
            
            print("✓ Build completed successfully")
            return True
            
        except subprocess.CalledProcessError as e:
            print(f"Build failed: {e.stderr}")
            return False
        except Exception as e:
            print(f"Build error: {str(e)}")
            return False
    
    def get_deployment_info(self, screen_id: str, project_id: str) -> Optional[Dict[str, Any]]:
        """Get deployment information for a screen"""
        deploy_path = self.base_deploy_path / project_id / screen_id
        info_file = deploy_path / "deployment.json"
        
        if not info_file.exists():
            return None
        
        with open(info_file, "r") as f:
            return json.load(f)
    
    def delete_deployment(self, screen_id: str, project_id: str) -> bool:
        """Delete a deployed screen"""
        try:
            deploy_path = self.base_deploy_path / project_id / screen_id
            if deploy_path.exists():
                shutil.rmtree(deploy_path)
                return True
            return False
        except Exception as e:
            print(f"Delete error: {str(e)}")
            return False