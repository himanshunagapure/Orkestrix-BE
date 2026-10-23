import re
from typing import Dict, Any, List, Optional
import random

class ImageURLValidator:
    """Validates and fixes image URLs to ensure they work"""
    
    def __init__(self):
        # Curated working Picsum IDs by category
        self.picsum_ids = {
            'business': [20, 26, 48, 58, 180, 326, 367],
            'office': [15, 20, 180, 326, 367],
            'nature': [10, 18, 28, 42, 60, 78, 82, 88, 103],
            'landscape': [10, 18, 28, 42, 60],
            'technology': [180, 326, 367, 403, 418, 429],
            'people': [64, 65, 91, 177, 338, 399, 433, 453],
            'portrait': [64, 65, 91, 177, 338],
            'food': [292, 312, 326, 431, 445, 488],
            'architecture': [1, 40, 63, 77, 164, 244, 342],
            'building': [1, 40, 63, 77, 164],
            'abstract': [219, 250, 274, 323, 381, 416],
            'city': [1, 244, 367, 403],
            'workspace': [15, 20, 180, 326],
            'team': [64, 91, 177, 338],
            'product': [292, 312, 431],
            'default': [1, 10, 20, 28, 40, 48, 60, 77, 82, 103]
        }
        
        # Default dimensions
        self.default_size = {'width': 800, 'height': 600}
        
    async def validate_and_fix_components(self, components: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Recursively validate and fix image URLs in components"""
        for component in components:
            # Fix image components
            if component.get('type') == 'image':
                await self._fix_image_component(component)
            
            # Fix card components with images
            elif component.get('type') == 'card':
                props = component.get('props', {})
                if 'image' in props:
                    props['image'] = self._get_valid_url(
                        props['image'], 
                        context='business'
                    )
            
            # Fix grid items with images
            elif component.get('type') == 'grid':
                props = component.get('props', {})
                items = props.get('items', [])
                for item in items:
                    if isinstance(item, dict) and 'image' in item:
                        item['image'] = self._get_valid_url(
                            item['image'],
                            context='product'
                        )
            
            # Fix avatar components
            elif component.get('type') == 'avatar':
                props = component.get('props', {})
                if 'src' in props:
                    # Use smaller size for avatars and people category
                    props['src'] = self._get_valid_url(
                        props['src'],
                        context='people',
                        width=200,
                        height=200
                    )
            
            # Recursively process children
            children = component.get('children', [])
            if children:
                await self.validate_and_fix_components(children)
        
        return components
    
    async def _fix_image_component(self, component: Dict[str, Any]):
        """Fix image component URL"""
        props = component.get('props', {})
        if 'src' in props:
            original_url = props['src']
            
            # Extract dimensions if specified
            width = props.get('width', 800)
            height = props.get('height', 600)
            
            # Try to parse numeric dimensions
            if isinstance(width, str):
                width = int(re.sub(r'\D', '', width)) if re.sub(r'\D', '', width) else 800
            if isinstance(height, str):
                height = int(re.sub(r'\D', '', height)) if re.sub(r'\D', '', height) else 600
            
            # Determine context from alt text or component properties
            context = self._determine_context(props)
            
            props['src'] = self._get_valid_url(original_url, context, width, height)
    
    def _determine_context(self, props: Dict[str, Any]) -> str:
        """Determine image context from component properties"""
        alt_text = props.get('alt', '').lower()
        
        # Check alt text for keywords
        for category in self.picsum_ids.keys():
            if category in alt_text:
                return category
        
        return 'default'
    
    def _get_valid_url(
        self, 
        url: str, 
        context: str = 'default',
        width: int = 800,
        height: int = 600
    ) -> str:
        """Get a valid image URL, fixing broken ones"""
        
        # If URL is already using a working service, keep it
        if 'picsum.photos' in url:
            return url
        
        if 'placeholder.com' in url or 'via.placeholder.com' in url:
            return url
            
        if 'dummyimage.com' in url:
            return url
        
        # If it's a broken Unsplash URL, replace it
        if 'unsplash.com' in url:
            return self._create_picsum_url(context, width, height)
        
        # If it's any other potentially broken URL, replace it
        if url.startswith('http'):
            return self._create_picsum_url(context, width, height)
        
        # If it's a relative or invalid URL, replace it
        return self._create_picsum_url(context, width, height)
    
    def _create_picsum_url(self, context: str, width: int = 800, height: int = 600) -> str:
        """Create a working Picsum URL with appropriate ID"""
        # Get IDs for the context, fallback to default
        ids = self.picsum_ids.get(context, self.picsum_ids['default'])
        
        # Pick a random ID from the category
        image_id = random.choice(ids)
        
        return f"https://picsum.photos/id/{image_id}/{width}/{height}"
    
    def get_contextual_image(self, context: str, width: int = 800, height: int = 600) -> str:
        """Public method to get a contextual image URL"""
        return self._create_picsum_url(context, width, height)
    
    def get_placeholder_image(
        self, 
        width: int = 800, 
        height: int = 600,
        bg_color: str = "3B82F6",
        text_color: str = "FFFFFF",
        text: Optional[str] = None
    ) -> str:
        """Get a placeholder image URL"""
        text = text or "Image"
        text = text.replace(' ', '+')
        return f"https://via.placeholder.com/{width}x{height}/{bg_color}/{text_color}?text={text}"
