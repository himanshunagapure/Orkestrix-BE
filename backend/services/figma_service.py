import re
import httpx
import asyncio
import time
import logging
import urllib.parse
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

class FigmaService:
    def __init__(self, access_token):
        self.access_token = access_token
        self.headers = {'X-Figma-Token': access_token}
        self.api_base = "https://api.figma.com/v1"
        self._image_cache = {}  
        self.CACHE_DURATION = 3600  # Cache lasts for 1 Hour

    def extract_file_info(self, url):
        """Extracts file_key and node_id from a Figma URL."""
        # 1. Extract File Key
        file_key_match = re.search(r"figma\.com/(?:file|design)/([a-zA-Z0-9]+)", url)
        if not file_key_match:
            logger.warning("No Figma file key found in URL")
            return None, None
            
        file_key = file_key_match.group(1)
        
        # 2. Extract Node ID
        node_id_match = re.search(r"node-id=([^&]+)", url)
        node_id = None
        
        if node_id_match:
            raw_id = node_id_match.group(1)
            # Decode URL (converts %3A to :)
            node_id = urllib.parse.unquote(raw_id)
            
            # Convert 0-1 to 0:1 format
            if "-" in node_id and ":" not in node_id:
                node_id = node_id.replace("-", ":")
            
        return file_key, node_id

    async def get_node_data_async(self, figma_url: str, user_token: str = None) -> Optional[dict]:
        try:
            # 1. Token Handling
            token = user_token if user_token else self.access_token
            if not token: 
                print("Figma Service: No token available (System or User).")
                return None

            # 2. Extract File Key
            key_match = re.search(r"figma\.com/(?:file|design)/([a-zA-Z0-9]+)", figma_url)
            if not key_match: 
                print(f"Figma Service: Could not extract file key from: {figma_url}")
                return None
            file_key = key_match.group(1)

            # 3. Extract and Fix Node ID (The Critical Fix)
            node_id = "0:1"
            node_match = re.search(r"node-id=([^&]+)", figma_url)
            if node_match:
                raw_id = node_match.group(1)
                # Decode first (just in case)
                node_id = urllib.parse.unquote(raw_id)
                
                # FIX: If ID looks like "1-2088" (Browser format), convert to "1:2088" (API format)
                if "-" in node_id and ":" not in node_id:
                    formatted_id = node_id.replace("-", ":")
                    print(f"Figma Service: Formatted node-id '{node_id}' -> '{formatted_id}'")
                    node_id = formatted_id
                else:
                    # Just ensure standard URL decoding didn't miss anything
                    node_id = node_id.replace("%3A", ":")
            
            # 4. Construct URL with Fixed ID
            url = f"https://api.figma.com/v1/files/{file_key}/nodes?ids={node_id}&geometry=paths"
            print(f"Figma Service: Requesting Node Data -> {url}")
            
            headers = {"X-Figma-Token": token}

            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    # Validate we actually got the node
                    nodes = data.get("nodes", {})
                    if node_id in nodes:
                         return nodes[node_id].get("document", {})
                    else:
                         print(f"Figma Service: API returned 200 but node '{node_id}' was not in response.")
                         return None
                
                print(f"Figma API Error (Data): Status {response.status_code} | Body: {response.text[:200]}")
                return None
        except Exception as e:
            print(f"Figma Data Error: {e}")
            return None

    async def download_bulk_images(self, file_key: str, node_ids: list) -> dict:
        """
        Fetches multiple image assets from Figma in a single API call.
        Returns a dictionary of {safe_filename: image_bytes}.
        """
        assets_data = {}
        if not node_ids:
            return assets_data
            
        # Limit to max 50 ids at a time to avoid URL length limits
        ids_str = ",".join(node_ids[:50])
        url = f"{self.api_base}/images/{file_key}?ids={ids_str}&format=png"
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"Figma Service: Requesting bulk image URLs -> {url}")
        
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers=self.headers)
                if resp.status_code == 200:
                    images = resp.json().get("images", {})
                    for node_id, img_url in images.items():
                        if img_url:
                            logger.info(f"⬇Figma Service: Downloading asset {node_id}...")
                            img_resp = await client.get(img_url)
                            if img_resp.status_code == 200:
                                safe_id = node_id.replace(':', '_')
                                filename = f"figma_{safe_id}.png"
                                assets_data[filename] = img_resp.content
                else:
                    logger.error(f"Figma API Error (Bulk Image): Status {resp.status_code}")
        except Exception as e:
            logger.error(f"Figma Bulk Image Error: {e}")
            
        return assets_data

    async def extract_and_download_assets(self, figma_json_str: str, target_figma_url: str, requested_image_ids: list = None) -> dict:
        """
        Parses Figma JSON to find image/vector nodes, or uses explicitly requested IDs, 
        then downloads them.
        """
        import json
        import logging
        logger = logging.getLogger(__name__)
        
        assets_data = {}
        if not target_figma_url:
            return assets_data

        try:
            image_nodes = []
            
            # NEW FIX: If we have explicit IDs requested by the LLM Schema, use ONLY those.
            if requested_image_ids:
                # Remove duplicates
                image_nodes = list(set(requested_image_ids))
                print(f"Figma Service: Synchronized! Downloading exactly {len(image_nodes)} assets requested by UI Schema.")
            
            # FALLBACK: If no explicit IDs, parse the JSON tree (Original behavior)
            elif figma_json_str:
                figma_data = json.loads(figma_json_str)
                def find_images(node):
                    if not isinstance(node, dict): return
                    node_type = node.get("type", "")
                    has_image_fill = any(
                        isinstance(fill, dict) and fill.get("type") == "IMAGE" 
                        for fill in node.get("fills", [])
                    )
                    
                    if (has_image_fill or node_type == "VECTOR") and node.get("id"):
                        image_nodes.append(node.get("id"))
                        
                    for child in node.get("children", []):
                        find_images(child)
                
                find_images(figma_data)
            
            if image_nodes:
                file_key, _ = self.extract_file_info(target_figma_url)
                if file_key:
                    # Call the bulk downloader
                    assets_data = await self.download_bulk_images(file_key, image_nodes)
                    
        except Exception as e:
            logger.warning(f"Failed to extract/download Figma assets: {e}")
            
        return assets_data

    async def get_node_image_async(self, figma_url: str, user_token: str = None) -> Optional[bytes]:
        try:
            token = user_token if user_token else self.access_token
            if not token: return None

            key_match = re.search(r"figma\.com/(?:file|design)/([a-zA-Z0-9]+)", figma_url)
            if not key_match: return None
            file_key = key_match.group(1)

            # Same Fix for Image ID
            node_id = "0:1"
            node_match = re.search(r"node-id=([^&]+)", figma_url)
            if node_match:
                raw_id = node_match.group(1)
                node_id = urllib.parse.unquote(raw_id)
                if "-" in node_id and ":" not in node_id:
                    node_id = node_id.replace("-", ":")
                else:
                    node_id = node_id.replace("%3A", ":")

            url = f"https://api.figma.com/v1/images/{file_key}?ids={node_id}&format=png"
            print(f"Figma Service: Requesting Image URL -> {url}")
            
            headers = {"X-Figma-Token": token}

            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    image_url = resp.json().get("images", {}).get(node_id)
                    if image_url:
                        print(f"Figma Service: Downloading Image content...")
                        img_resp = await client.get(image_url)
                        return img_resp.content
                    else:
                        print("Figma Service: No image URL found in response.")
                else:
                    print(f"Figma API Error (Image): Status {resp.status_code}")
            return None
        except Exception as e:
            print(f"Figma Image Error: {e}")
            return None
    # Sync wrapper (if needed elsewhere)
    def get_node_image(self, url):
        return asyncio.run(self.get_node_image_async(url))
        