"""
services/gemini_screen_generator.py

Service for generating complete HTML screens using Google Gemini LLM
"""

import google.generativeai as genai
import os
import json
import re
from typing import Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()

class GeminiScreenGenerator:
    """
    Generates production-ready HTML/Tailwind/Flowbite code from prompts and IR schemas
    using Google Gemini LLM with detailed instructions to avoid hallucinations.
    """
    
    def __init__(self):
        """Initialize Gemini API with configuration"""
        api_key = os.getenv('GOOGLE_API_KEY')
        if not api_key:
            raise ValueError("GOOGLE_API_KEY environment variable not set")
        
        genai.configure(api_key=api_key)
        
        # Configure generation settings for optimal output
        self.generation_config = {
            'temperature': 0.7,  # Balance between creativity and consistency
            'top_p': 0.95,
            'top_k': 40,
            'max_output_tokens': 8192,  # Allow for complex screens
        }
        # Initialize model
        self.model = genai.GenerativeModel('gemini-3.6-flash')
    
    def _create_detailed_prompt(self, user_prompt: str, ir_schema: Dict[str, Any]) -> str:
        """
        Creates a comprehensive prompt for Gemini to generate high-quality HTML code
        with extensive instructions to avoid hallucinations.
        """
        
        # Extract key information from IR schema
        screen_info = ir_schema.get('screen', {})
        screen_type = screen_info.get('type', 'mixed')
        screen_title = screen_info.get('title', 'Screen')
        
        theme = ir_schema.get('theme', {})
        components = ir_schema.get('components', [])
        
        # Build component list for context
        component_types = [comp.get('type', 'unknown') for comp in components]
        component_summary = ', '.join(set(component_types)) if component_types else 'various UI elements'
        
        prompt = f"""You are an expert UI/UX developer specializing in creating beautiful, modern, and fully functional web interfaces using HTML, Tailwind CSS, and Flowbite components.

## USER REQUEST:
{user_prompt}

## SCREEN TYPE:
{screen_type}

## SCREEN TITLE:
{screen_title}

## DESIGN SPECIFICATIONS:
Primary Color: {theme.get('primary', '#3B82F6')}
Secondary Color: {theme.get('secondary', '#F3F4F6')}
Accent Color: {theme.get('accent', '#10B981')}
Background: {theme.get('background', '#FFFFFF')}
Text Color: {theme.get('text', '#1F2937')}
Error Color: {theme.get('error', '#EF4444')}
Success Color: {theme.get('success', '#10B981')}
Font Family: {theme.get('font', 'Inter')}
Border Radius: {theme.get('radius', '8px')}

## COMPONENTS TO INCLUDE:
{component_summary}

## COMPLETE IR SCHEMA CONTEXT:
```json
{json.dumps(ir_schema, indent=2)}
```

## CRITICAL REQUIREMENTS:

### 1. UNDERSTAND THE INTENT
- Carefully analyze the user's request and the IR schema
- Identify the primary purpose of the screen (e.g., data entry, display, navigation, dashboard)
- Consider the user's workflow and create an intuitive experience
- Think about the information hierarchy and what should be most prominent
- Match the screen type: {screen_type}

### 2. DESIGN PRINCIPLES
- Use blue ({theme.get('primary', '#3B82F6')}) as the primary color theme consistently
- Create a modern, clean, and professional design
- Ensure proper visual hierarchy with headings, spacing, and emphasis
- Use white space effectively - don't overcrowd elements
- Make the design visually balanced and aesthetically pleasing
- Apply consistent border radius ({theme.get('radius', '8px')}), shadows, and transitions
- Use the specified theme colors throughout

### 3. COMPONENT IMPLEMENTATION
- Use ONLY Flowbite components from their official library
- Each component must be fully functional and independent
- NO placeholder text like "Your content here" or "Add your items"
- Use realistic, meaningful sample data for all components
- For forms: include proper labels, placeholders, and validation attributes
- For tables: show at least 5-7 realistic data rows with diverse data
- For cards: include complete content with images, text, and actions
- For charts: use Chart.js with realistic data points (minimum 6 data points)
- For stats/KPIs: show actual numbers with proper formatting ($1,234.56, 45.2%, etc.)
- For navigation: include all menu items specified in IR schema
- For buttons: use appropriate variants (primary, secondary, outline, ghost)

### 4. RESPONSIVE DESIGN - CRITICAL
- Make everything fully responsive using Tailwind's responsive classes
- Use breakpoints: `sm:`, `md:`, `lg:`, `xl:`, `2xl:`
- Mobile-first approach: base styles for mobile, then scale up
- Stack elements vertically on mobile (`flex-col`), use grids on larger screens
- Adjust font sizes: `text-sm md:text-base lg:text-lg`
- Adjust padding: `p-4 md:p-6 lg:p-8`
- Adjust grid columns: `grid-cols-1 md:grid-cols-2 lg:grid-cols-3`
- Ensure tables are scrollable on mobile: `overflow-x-auto`

### 5. SPACING AND LAYOUT
- Use consistent spacing throughout: `p-4`, `p-6`, `p-8` for padding
- Use gaps in grids/flex: `gap-4`, `gap-6`, `gap-8`
- Add proper margins between sections: `mb-6`, `mb-8`, `mb-12`
- Use container classes: `max-w-7xl`, `max-w-6xl`, `container mx-auto px-4`
- Add breathing room - elements should not touch screen edges
- Section padding: `py-8 md:py-12 lg:py-16`

### 6. INTERACTIVITY AND ANIMATIONS
- Add hover effects: `hover:bg-blue-600`, `hover:shadow-lg`, `transition-all duration-300`
- Use `focus:` states for inputs: `focus:ring-2 focus:ring-blue-500 focus:border-blue-500`
- Add smooth transitions: `transition-all duration-300 ease-in-out`
- Button interactions: `active:scale-95 transform`
- Use Flowbite's JavaScript components with proper data attributes
- Add subtle animations using AOS: `data-aos="fade-up"` for scroll animations

### 7. ACCESSIBILITY - MANDATORY
- Include proper ARIA labels: `aria-label`, `aria-labelledby`, `aria-describedby`
- Use semantic HTML: `<nav>`, `<main>`, `<section>`, `<article>`, `<header>`, `<footer>`
- Ensure sufficient color contrast (WCAG AA minimum)
- Add alt text for all images: descriptive and meaningful
- Make interactive elements keyboard accessible: proper `tabindex`, focus states
- Use `role` attributes where appropriate

### 8. DATA REALISM - NO PLACEHOLDERS
- For employee/user data: use diverse, realistic names (John Anderson, Maria Garcia, etc.)
- For dates: use actual date formats (Jan 15, 2024, 10:30 AM)
- For numbers/metrics: use realistic values with proper formatting
  * Currency: $1,234.56, €2,450.00
  * Percentages: 45.2%, +12.5%, -3.8%
  * Large numbers: 1.2M, 450K, 3.5B
- For status badges: use appropriate colors
  * Success/Active/Completed: green (`bg-green-100 text-green-800`)
  * Warning/Pending: yellow (`bg-yellow-100 text-yellow-800`)
  * Error/Inactive: red (`bg-red-100 text-red-800`)
  * Info: blue (`bg-blue-100 text-blue-800`)
- For descriptions: write actual meaningful sentences
- For email addresses: realistic formats (john.anderson@company.com)
- For phone numbers: proper formatting ((555) 123-4567)

### 9. COMPONENT INDEPENDENCE
- Each component should be self-contained
- Don't create dependencies between components
- Use separate sections/containers for different features
- Avoid shared state or complex interactions between elements
- Each section should work independently

### 10. ICONS AND VISUALS
- Use Font Awesome icons consistently: `<i class="fas fa-user"></i>`
- Icon sizes: `text-lg`, `text-xl`, `text-2xl`
- Use icons for: actions, status indicators, navigation, visual enhancement
- Add icons to buttons: `<i class="fas fa-plus mr-2"></i>Add Item`
- Use appropriate icons for context (fa-user, fa-envelope, fa-chart-line, etc.)

## IMAGE GUIDELINES
When using images, use ONLY these GUARANTEED working services:

**Lorem Picsum** (RECOMMENDED - always works):
- Random image: "src": "https://picsum.photos/800/600"
- Specific image: "src": "https://picsum.photos/id/237/800/600"
- Grayscale: "src": "https://picsum.photos/800/600?grayscale"
- Blur effect: "src": "https://picsum.photos/800/600?blur=2"

Valid IDs (use these for consistency):
- Business/Office: 0, 15, 20, 26, 48, 58, 180, 326, 367
- Nature/Landscape: 10, 18, 28, 42, 60, 78, 82, 88, 103
- Technology: 180, 326, 367, 403, 418, 429
- People/Portraits: 64, 65, 91, 177, 338, 399, 433, 453
- Food: 292, 312, 326, 431, 445, 488
- Architecture: 1, 40, 63, 77, 164, 244, 342
- Abstract/Patterns: 219, 250, 274, 323, 381, 416

### 11. FORMS AND VALIDATION
- Use Flowbite form components with proper structure
- Include labels: `<label class="block mb-2 text-sm font-medium text-gray-900">`
- Input styling: `bg-gray-50 border border-gray-300 text-gray-900 text-sm rounded-lg focus:ring-blue-500 focus:border-blue-500 block w-full p-2.5`
- Add validation attributes: `required`, `pattern`, `minlength`, `maxlength`
- Show helper text: `<p class="mt-2 text-sm text-gray-500">Helper text here</p>`
- Error states: `border-red-500 focus:ring-red-500`

### 12. TABLES AND DATA DISPLAY
- Use Flowbite table components
- Include table headers: `<thead class="text-xs text-gray-700 uppercase bg-gray-50">`
- Alternating row colors: `odd:bg-white even:bg-gray-50`
- Hover states: `hover:bg-gray-100 transition-colors`
- Minimum 5 data rows with diverse, realistic data
- Add actions column with buttons: Edit, Delete, View
- Make tables responsive: wrap in `<div class="overflow-x-auto">`

### 13. CHARTS AND VISUALIZATIONS
- Use Chart.js for data visualization
- Include proper canvas elements: `<canvas id="myChart"></canvas>`
- Add Chart.js initialization script at the end
- Use realistic data with at least 6 data points
- Include proper labels and legends
- Chart types: line, bar, pie, doughnut, area
- Example:
```javascript
<script>
const ctx = document.getElementById('myChart');
new Chart(ctx, {{
  type: 'line',
  data: {{
    labels: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun'],
    datasets: [{{
      label: 'Sales',
      data: [12, 19, 3, 5, 2, 3],
      borderColor: 'rgb(59, 130, 246)',
      tension: 0.1
    }}]
  }}
}});
</script>
```

### 14. NAVIGATION COMPONENTS
- Use Flowbite navbar components
- Include logo/brand: `<span class="text-xl font-bold">Brand Name</span>`
- Navigation links with hover effects
- Mobile menu toggle button: `<button data-collapse-toggle="navbar-default">`
- Responsive navigation: hidden on mobile, visible on desktop
- Active link styling: `text-blue-600 font-semibold`

## OUTPUT REQUIREMENTS - CRITICAL:

1. Generate ONLY the HTML body content
2. DO NOT include `<html>`, `<head>`, or `<title>` tags
3. Start directly with the main content: `<div>`, `<section>`, `<nav>`, etc.
4. DO NOT include any explanatory text, comments, or markdown code blocks
5. Output must be production-ready HTML that can be directly inserted
6. Use ONLY CDN-loaded libraries (already included in template)
7. Ensure all Flowbite components have proper `data-` attributes
8. Include Chart.js initialization scripts at the end if using charts
9. Include AOS animation attributes: `data-aos="fade-up"` for scroll effects
10. Make the code clean, properly indented, and well-organized

## FLOWBITE COMPONENT EXAMPLES:

### Button:
```html
<button class="text-white bg-blue-700 hover:bg-blue-800 focus:ring-4 focus:ring-blue-300 font-medium rounded-lg text-sm px-5 py-2.5 focus:outline-none">
  <i class="fas fa-plus mr-2"></i>Add New
</button>
```

### Card:
```html
<div class="max-w-sm bg-white border border-gray-200 rounded-lg shadow hover:shadow-xl transition-shadow duration-300">
  <img class="rounded-t-lg" src="https://images.unsplash.com/photo-1560250097-0b93528c311a?w=800" alt="Card image">
  <div class="p-5">
    <h5 class="mb-2 text-2xl font-bold tracking-tight text-gray-900">Card Title</h5>
    <p class="mb-3 font-normal text-gray-700">Realistic description text that provides context and value.</p>
    <a href="#" class="inline-flex items-center px-3 py-2 text-sm font-medium text-white bg-blue-700 rounded-lg hover:bg-blue-800">
      Read more <i class="fas fa-arrow-right ml-2"></i>
    </a>
  </div>
</div>
```

### Table:
```html
<div class="overflow-x-auto">
  <table class="w-full text-sm text-left text-gray-500">
    <thead class="text-xs text-gray-700 uppercase bg-gray-50">
      <tr>
        <th class="px-6 py-3">Name</th>
        <th class="px-6 py-3">Position</th>
        <th class="px-6 py-3">Status</th>
      </tr>
    </thead>
    <tbody>
      <tr class="bg-white border-b hover:bg-gray-50">
        <td class="px-6 py-4 font-medium text-gray-900">John Anderson</td>
        <td class="px-6 py-4">Senior Developer</td>
        <td class="px-6 py-4"><span class="bg-green-100 text-green-800 text-xs font-medium px-2.5 py-0.5 rounded">Active</span></td>
      </tr>
    </tbody>
  </table>
</div>
```

## ANTI-PATTERNS TO AVOID:
❌ Placeholder text: "Your content here", "Add items", "Lorem ipsum"
❌ Empty tables or lists
❌ Missing data in components
❌ Non-functional buttons or links
❌ Broken Flowbite component attributes
❌ Inline styles instead of Tailwind classes
❌ Non-responsive fixed widths
❌ Missing hover/focus states
❌ Inconsistent spacing
❌ Poor color contrast
❌ Missing semantic HTML
❌ No accessibility attributes

Now, generate the complete, production-ready HTML body content based on the user's request and IR schema. Remember: Output ONLY the HTML code with no additional text, explanations, or code block markers. The output should start directly with an HTML element like `<div>`, `<nav>`, or `<section>`."""

        return prompt
    
    def _extract_html_from_response(self, response_text: str) -> str:
        """
        Extracts clean HTML from the LLM response, removing markdown code blocks or extra text.
        """
        # Remove markdown code blocks if present
        code_block_pattern = r'```html\s*(.*?)\s*```'
        match = re.search(code_block_pattern, response_text, re.DOTALL | re.IGNORECASE)
        
        if match:
            return match.group(1).strip()
        
        # Remove any other code block markers
        response_text = re.sub(r'```\s*', '', response_text)
        
        # Look for the first occurrence of a common HTML opening tag
        html_start_patterns = [
            r'<div', r'<section', r'<main', r'<article', 
            r'<nav', r'<header', r'<aside', r'<footer'
        ]
        
        for pattern in html_start_patterns:
            match = re.search(pattern, response_text, re.IGNORECASE)
            if match:
                start_idx = match.start()
                return response_text[start_idx:].strip()
        
        # If no specific pattern found, return cleaned text
        return response_text.strip()
    
    def _generate_final_html(self, generated_code: str) -> str:
        """
        Wraps the generated code in the complete HTML template with all required CDN libraries.
        """
        template = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="description" content="AI Website Builder - Modern TailwindCSS + Flowbite Template">
  <title>AI Website Builder</title>
  <!-- Tailwind CSS -->
  <script src="https://cdn.tailwindcss.com"></script>
  <!-- Flowbite CSS & JS -->
  <link href="https://cdnjs.cloudflare.com/ajax/libs/flowbite/2.3.0/flowbite.min.css" rel="stylesheet">
  <script src="https://cdnjs.cloudflare.com/ajax/libs/flowbite/2.3.0/flowbite.min.js"></script>
  <!-- Font Awesome Icons -->
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.2/css/all.min.css">
  <!-- Chart.js -->
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <!-- AOS Animation Library -->
  <link href="https://cdnjs.cloudflare.com/ajax/libs/aos/2.3.4/aos.css" rel="stylesheet">
  <script src="https://cdnjs.cloudflare.com/ajax/libs/aos/2.3.4/aos.js"></script>
  <script>
    // Initialize AOS animations on page load
    document.addEventListener('DOMContentLoaded', function() {{
      if (typeof AOS !== 'undefined') {{
        AOS.init({{
          duration: 800,
          once: true,
          offset: 100
        }});
      }}
    }});
  </script>
</head>
<body class="bg-gray-50">
{generated_code}
</body>
</html>"""
        
        return template
    
    async def generate_screen_html(
        self, 
        prompt: str, 
        ir_schema: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Main method to generate complete HTML screen from prompt and IR schema.
        
        Args:
            prompt: User's natural language description
            ir_schema: Intermediate Representation schema
            
        Returns:
            Dict containing:
                - success: bool
                - html: complete HTML string
                - metadata: generation info
                - error: error message if failed
        """
        try:
            print("\n=== GEMINI SCREEN GENERATION START ===")
            print(f"Prompt: {prompt[:100]}...")
            print(f"Screen Type: {ir_schema.get('screen', {}).get('type', 'unknown')}")
            print(f"Components Count: {len(ir_schema.get('components', []))}")
            
            # Create detailed prompt for Gemini
            print("\n[1/4] Creating detailed prompt...")
            detailed_prompt = self._create_detailed_prompt(prompt, ir_schema)
            print(f"Prompt length: {len(detailed_prompt)} characters")

            # Generate content using Gemini
            print("\n[2/4] Calling Gemini API...")
            response = self.model.generate_content(
              detailed_prompt,
              generation_config=genai.types.GenerationConfig(
                temperature=0.7,
                top_p=0.95,
                top_k=40
              )
            )
            
            # Check if response was blocked
            print("\n[3/4] Checking response status...")
            if not response.candidates:
                print("❌ ERROR: No candidates returned from Gemini")
                return {
                    "success": False,
                    "html": None,
                    "metadata": None,
                    "error": "No response candidates returned. Response may have been blocked."
                }
            
            candidate = response.candidates[0]
            print(f"Finish Reason: {candidate.finish_reason}")
            print(f"Safety Ratings: {candidate.safety_ratings}")
            
            # Check finish reason
            if candidate.finish_reason != 1:  # 1 = STOP (normal completion)
                finish_reason_map = {
                    2: "MAX_TOKENS",
                    3: "SAFETY",
                    4: "RECITATION",
                    5: "OTHER"
                }
                reason = finish_reason_map.get(candidate.finish_reason, "UNKNOWN")
                print(f"❌ ERROR: Generation stopped due to: {reason}")
                
                error_msg = f"Generation blocked. Reason: {reason}"
                if reason == "SAFETY":
                    error_msg += f". Safety ratings: {candidate.safety_ratings}"
                
                return {
                    "success": False,
                    "html": None,
                    "metadata": None,
                    "error": error_msg
                }
            
            # Check if response has content
            if not candidate.content or not candidate.content.parts:
                print("❌ ERROR: Response has no content parts")
                return {
                    "success": False,
                    "html": None,
                    "metadata": None,
                    "error": f"No content in response. Safety ratings: {candidate.safety_ratings}"
                }
            
            # Extract text from response
            generated_text = candidate.content.parts[0].text
            print(f"✓ Generated text length: {len(generated_text)} characters")
            
            # Extract and clean the generated HTML
            print("\n[3/4] Extracting and wrapping HTML...")
            generated_html = self._extract_html_from_response(response.text)
            
            print("\n[4/4] Extracting and wrapping HTML...")
            # Wrap in complete HTML template
            final_html = self._generate_final_html(generated_html)
            
            return {
                "success": True,
                "html": final_html,
                "metadata": {
                    "prompt": prompt,
                    "screen_type": ir_schema.get('screen', {}).get('type', 'unknown'),
                    "component_count": len(ir_schema.get('components', [])),
                    "model": "gemini-3.6-flash",
                    "generated_at": "now",
                    "finish_reason": candidate.finish_reason,
                    "safety_ratings": str(candidate.safety_ratings)
                },
                "error": None
            }
            
        except Exception as e:
            print(f"\n❌ ERROR in generate_screen_html: {str(e)}")
            import traceback
            traceback.print_exc()
            return {
                "success": False,
                "html": None,
                "metadata": None,
                "error": str(e)
            }
    
    def validate_ir_schema(self, ir_schema: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates IR schema before generation to catch errors early.
        
        Returns:
            Dict with validation result
        """
        try:
            required_fields = ['screen', 'components']
            missing_fields = [field for field in required_fields if field not in ir_schema]
            
            if missing_fields:
                return {
                    "valid": False,
                    "errors": [f"Missing required field: {field}" for field in missing_fields]
                }
            
            if not ir_schema.get('components'):
                return {
                    "valid": False,
                    "errors": ["Components array cannot be empty"]
                }
            
            return {
                "valid": True,
                "errors": [],
                "component_count": len(ir_schema.get('components', []))
            }
            
        except Exception as e:
            return {
                "valid": False,
                "errors": [f"Validation failed: {str(e)}"]
            }