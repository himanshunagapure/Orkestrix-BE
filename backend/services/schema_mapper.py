"""
Schema Mapper Service - AI-Powered Automatic Schema Mapping
Handles automatic mapping between mismatched data structures at workflow design time.
"""
import asyncio
import functools
import json
import re
import hashlib
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
from google import genai
from google.genai import types
import logging

from services.llm_token_tracker import record_llm_call
from services.llm_utils import extract_token_usage_from_response

logger = logging.getLogger(__name__)


async def run_sync(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    pfunc = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(None, pfunc)

# In-memory cache for mappings (cleared on server restart)
_mapping_cache: Dict[str, Dict[str, Any]] = {}
MAX_CACHE_SIZE = 1000

# Common field name variations and patterns
COMMON_FIELD_MAPPINGS = {
    "invoice_number": ["invoiceNumber", "invoice_no", "invNumber", "invoiceNo"],
    "invoice_date": ["invoiceDate", "issueDate", "invoiceIssueDate"],
    "due_date": ["dueDate", "paymentDue", "dueDatePayment"],
    "customer_name": ["customerName", "client", "clientName", "customer"],
    "vendor_name": ["vendorName", "supplier", "supplierName", "vendor"],
    "total_amount": ["totalAmount"],
    "line_items": ["lineItems"],
    "created_at": ["createdAt", "dateCreated", "createDate"],
    "updated_at": ["updatedAt", "dateUpdated", "updateDate"],
}


class SchemaMapper:
    """Handles automatic schema mapping between workflow steps"""
    
    def __init__(self, llm_client, api_key: str):
        """
        Initialize SchemaMapper with LLM client
        
        Args:
            llm_client: The LLM instance (Gemini)
            api_key: Gemini API key
        """
        self.llm = llm_client
        self.api_key = api_key
        self.mapping_cache = {}
        
    def camel_to_snake(self, name: str) -> str:
        """Convert camelCase to snake_case"""
        s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
        return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()
    
    def snake_to_camel(self, name: str) -> str:
        """Convert snake_case to camelCase"""
        components = name.split('_')
        return components[0] + ''.join(x.title() for x in components[1:])
    
    def generate_schema_hash(self, schema: Dict[str, Any]) -> str:
        """Generate hash for schema structure"""
        schema_str = json.dumps(sorted(schema.keys()) if isinstance(schema, dict) else schema, sort_keys=True)
        return hashlib.md5(schema_str.encode()).hexdigest()
    
    def extract_field_names(self, obj: Any, prefix: str = "") -> List[str]:
        """
        Recursively extract all field names from an object
        Handles both formats:
        1. Object format: {"field1": "value1", "field2": "value2"}
        2. Array format: [{"name": "field1", "value": "..."}, {"name": "field2", "value": "..."}]
        """
        fields = []
        
        if isinstance(obj, dict):
            for key, value in obj.items():
                current_path = f"{prefix}.{key}" if prefix else key
                fields.append(current_path)
                if isinstance(value, (dict, list)):
                    fields.extend(self.extract_field_names(value, current_path))
        elif isinstance(obj, list) and obj:
            # Check if it's an array of parameter objects (workflow format)
            if all(isinstance(item, dict) and 'name' in item for item in obj):
                # Array format: [{"name": "field1", ...}, {"name": "field2", ...}]
                for item in obj:
                    field_name = item.get('name')
                    if field_name:
                        current_path = f"{prefix}.{field_name}" if prefix else field_name
                        fields.append(current_path)
            else:
                # Regular array - sample first item
                fields.extend(self.extract_field_names(obj[0], prefix))
        
        return fields
    
    async def generate_mappings_with_llm(
        self,
        source_schema: Dict[str, Any],
        unmapped_fields: List[str],
        source_step_id: str,
        target_step_id: str,
        context: Optional[Dict[str, Any]] = None,
        uses_context_prefix: bool = True,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        process_execution_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Use LLM to generate mappings for fields that couldn't be matched by patterns
        
        Returns:
            Dict with 'mappings', 'confidence', and 'notes'
        """
        if not unmapped_fields:
            return {"mappings": {}, "confidence": 1.0, "notes": "All fields matched by patterns"}
        
        # Check if we're dealing with multiple source steps
        is_multi_source = source_step_id == "all_previous_steps"
        
        if is_multi_source:
            # source_schema contains all previous steps' schemas
            priority_steps = context.get('priority_steps', []) if context else []
            fallback_steps = context.get('fallback_steps', []) if context else []
            
            # Determine mapping format based on uses_context_prefix flag
            if uses_context_prefix:
                mapping_format = "context.<stepId>.outputParameters.<field_path>"
                example_mapping1 = "context.step_1.outputParameters.sourceField1"
                example_mapping2 = "context.step_2.outputParameters.nested.sourceField2"
            else:
                mapping_format = "<stepId>.<field_path>"
                example_mapping1 = "step_1.sourceField1"
                example_mapping2 = "step_2.nested.sourceField2"
            
            prompt = f"""You are a data mapping expert. Analyze the following schemas and generate correct field mappings.

AVAILABLE SOURCE STEPS:

PRIORITY STEPS (explicitly referenced in workflow dependencies):
{json.dumps({k: source_schema[k] for k in priority_steps if k in source_schema}, indent=2)}

FALLBACK STEPS (available but not explicitly referenced):
{json.dumps({k: source_schema[k] for k in fallback_steps if k in source_schema}, indent=2)}

TARGET STEP: {target_step_id}
Unmapped Target Fields that need mapping:
{json.dumps(unmapped_fields, indent=2)}

Task: Generate inputMappings for the unmapped target fields.

MAPPING RULES:
1. PRIORITY: Search PRIORITY STEPS first (these are the workflow's intended dependencies)
2. FALLBACK: If field not found in priority steps, search FALLBACK STEPS
3. NOT FOUND: If field doesn't exist in ANY step, mark it as unmappable (will be kept blank)
4. Use dot notation: "{mapping_format}"
5. Match fields based on semantic meaning and case variations (snake_case/camelCase)
6. Handle nested structures correctly (use dot notation: parent.child.subchild)

Output ONLY valid JSON in this exact format:
{{
  "mappings": {{
    "targetField1": "{example_mapping1}",
    "targetField2": "{example_mapping2}"
  }},
  "confidence": 0.95,
  "unmappable_fields": ["fieldX"],
  "notes": "Brief explanation of mapping logic"
}}
"""
        else:
            # Single source step (original behavior)
            source_fields = self.extract_field_names(source_schema)
            
            # Determine mapping format based on uses_context_prefix flag
            if uses_context_prefix:
                mapping_format = f"context.{source_step_id}.outputParameters.field_path"
                example_mapping1 = f"context.{source_step_id}.outputParameters.sourceField1"
                example_mapping2 = f"context.{source_step_id}.outputParameters.nested.sourceField2"
            else:
                mapping_format = f"{source_step_id}.field_path"
                example_mapping1 = f"{source_step_id}.sourceField1"
                example_mapping2 = f"{source_step_id}.nested.sourceField2"

            prompt = f"""You are a data mapping expert. Analyze the following schemas and generate correct field mappings.

SOURCE STEP: {source_step_id}
Available Source Fields:
{json.dumps(source_fields, indent=2)}

TARGET STEP: {target_step_id}
Unmapped Target Fields that need mapping:
{json.dumps(unmapped_fields, indent=2)}

Context: {json.dumps(context or {}, indent=2)}

Task: Generate inputMappings for the unmapped target fields.

Rules:
1. Use dot notation: "{mapping_format}"
2. Match fields based on semantic meaning, not just names
3. Handle nested structures correctly
4. If a target field cannot be mapped to any source field, mark it as unmappable

Output ONLY valid JSON in this exact format:
{{
  "mappings": {{
    "targetField1": "{example_mapping1}",
    "targetField2": "{example_mapping2}"
  }},
  "confidence": 0.95,
  "unmappable_fields": ["fieldX"],
  "notes": "Brief explanation of mapping logic"
}}
"""
        
        try:
            # Call Gemini API
            started_at = datetime.utcnow()
            client = genai.Client(api_key=self.api_key)
            response = client.models.generate_content(
                model='gemini-3-flash-preview',
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,  # Low temperature for consistent output
                    response_mime_type="application/json"
                )
            )
            finished_at = datetime.utcnow()
            
            # Parse response
            result_text = response.text
            result = json.loads(result_text)
            
            # Record LLM call
            token_data = extract_token_usage_from_response(response)
            record_llm_call(
                org_id=org_id,
                subscriber_id=subscriber_id,
                project_id="workflow",  # workflow-level operation
                screen_id=target_step_id,
                operation="save-process-configs",
                sub_operation="schema-mapping",
                model="gemini-3-flash-preview",
                prompt_tokens=token_data["prompt_tokens"],
                completion_tokens=token_data["completion_tokens"],
                total_tokens=token_data["total_tokens"],
                status="success",
                started_at=started_at,
                finished_at=finished_at,
                metadata={
                    "source_step": source_step_id,
                    "target_step": target_step_id,
                    "unmapped_fields_count": len(unmapped_fields),
                    "process_execution_id": process_execution_id
                }
            )
            
            logger.info(f"LLM generated mappings with confidence: {result.get('confidence', 0)}")
            logger.info(f"LLM mappings: {json.dumps(result.get('mappings', {}), indent=2)}")
            logger.info(f"LLM unmappable fields: {result.get('unmappable_fields', [])}")
            return result
            
        except Exception as e:
            logger.error(f"LLM mapping generation failed: {e}")
            
            # Record failed attempt
            record_llm_call(
                org_id=org_id,
                subscriber_id=subscriber_id,
                project_id="workflow",
                screen_id=target_step_id,
                operation="save-process-configs",
                sub_operation="schema-mapping",
                model="gemini-3-flash-preview",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                status="failed",
                metadata={
                    "error": str(e),
                    "process_execution_id": process_execution_id
                }
            )
            
            return {
                "mappings": {},
                "confidence": 0.0,
                "notes": f"LLM call failed: {str(e)}"
            }
    
    def _sync_generate_mappings_with_llm(
        self,
        source_schema: Dict[str, Any],
        unmapped_fields: List[str],
        source_step_id: str,
        target_step_id: str,
        context: Optional[Dict[str, Any]] = None,
        uses_context_prefix: bool = True,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        process_execution_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Synchronous version of generate_mappings_with_llm for use with run_in_executor
        Keeps the LLM token tracking functionality
        """
        if not unmapped_fields:
            return {"mappings": {}, "confidence": 1.0, "notes": "All fields matched by patterns"}
        
        is_multi_source = source_step_id == "all_previous_steps"
        
        if is_multi_source:
            priority_steps = context.get('priority_steps', []) if context else []
            fallback_steps = context.get('fallback_steps', []) if context else []
            
            if uses_context_prefix:
                mapping_format = "context.<stepId>.outputParameters.<field_path>"
                example_mapping1 = "context.step_1.outputParameters.sourceField1"
                example_mapping2 = "context.step_2.outputParameters.nested.sourceField2"
            else:
                mapping_format = "<stepId>.<field_path>"
                example_mapping1 = "step_1.sourceField1"
                example_mapping2 = "step_2.nested.sourceField2"
            
            prompt = f"""You are a data mapping expert. Analyze the following schemas and generate correct field mappings.

AVAILABLE SOURCE STEPS:

PRIORITY STEPS (explicitly referenced in workflow dependencies):
{json.dumps({k: source_schema[k] for k in priority_steps if k in source_schema}, indent=2)}

FALLBACK STEPS (available but not explicitly referenced):
{json.dumps({k: source_schema[k] for k in fallback_steps if k in source_schema}, indent=2)}

TARGET STEP: {target_step_id}
Unmapped Target Fields that need mapping:
{json.dumps(unmapped_fields, indent=2)}

Task: Generate inputMappings for the unmapped target fields.

MAPPING RULES:
1. PRIORITY: Search PRIORITY STEPS first (these are the workflow's intended dependencies)
2. FALLBACK: If field not found in priority steps, search FALLBACK STEPS
3. NOT FOUND: If field doesn't exist in ANY step, mark it as unmappable (will be kept blank)
4. Use dot notation: "{mapping_format}"
5. Match fields based on semantic meaning and case variations (snake_case/camelCase)
6. Handle nested structures correctly (use dot notation: parent.child.subchild)

Output ONLY valid JSON in this exact format:
{{
  "mappings": {{
    "targetField1": "{example_mapping1}",
    "targetField2": "{example_mapping2}"
  }},
  "confidence": 0.95,
  "unmappable_fields": ["fieldX"],
  "notes": "Brief explanation of mapping logic"
}}
"""
        else:
            source_fields = self.extract_field_names(source_schema)
            
            if uses_context_prefix:
                mapping_format = f"context.{source_step_id}.outputParameters.field_path"
                example_mapping1 = f"context.{source_step_id}.outputParameters.sourceField1"
                example_mapping2 = f"context.{source_step_id}.outputParameters.nested.sourceField2"
            else:
                mapping_format = f"{source_step_id}.field_path"
                example_mapping1 = f"{source_step_id}.sourceField1"
                example_mapping2 = f"{source_step_id}.nested.sourceField2"

            prompt = f"""You are a data mapping expert. Analyze the following schemas and generate correct field mappings.

SOURCE STEP: {source_step_id}
Available Source Fields:
{json.dumps(source_fields, indent=2)}

TARGET STEP: {target_step_id}
Unmapped Target Fields that need mapping:
{json.dumps(unmapped_fields, indent=2)}

Context: {json.dumps(context or {}, indent=2)}

Task: Generate inputMappings for the unmapped target fields.

Rules:
1. Use dot notation: "{mapping_format}"
2. Match fields based on semantic meaning, not just names
3. Handle nested structures correctly
4. If a target field cannot be mapped to any source field, mark it as unmappable

Output ONLY valid JSON in this exact format:
{{
  "mappings": {{
    "targetField1": "{example_mapping1}",
    "targetField2": "{example_mapping2}"
  }},
  "confidence": 0.95,
  "unmappable_fields": ["fieldX"],
  "notes": "Brief explanation of mapping logic"
}}
"""
        
        try:
            started_at = datetime.utcnow()
            client = genai.Client(api_key=self.api_key)
            response = client.models.generate_content(
                model='gemini-3-flash-preview',
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json"
                )
            )
            finished_at = datetime.utcnow()
            
            result_text = response.text
            result = json.loads(result_text)
            
            # Record LLM call (token tracking included)
            token_data = extract_token_usage_from_response(response)
            record_llm_call(
                org_id=org_id,
                subscriber_id=subscriber_id,
                project_id="workflow",
                screen_id=target_step_id,
                operation="save-process-configs",
                sub_operation="schema-mapping",
                model="gemini-3-flash-preview",
                prompt_tokens=token_data["prompt_tokens"],
                completion_tokens=token_data["completion_tokens"],
                total_tokens=token_data["total_tokens"],
                status="success",
                started_at=started_at,
                finished_at=finished_at,
                metadata={
                    "source_step": source_step_id,
                    "target_step": target_step_id,
                    "unmapped_fields_count": len(unmapped_fields),
                    "process_execution_id": process_execution_id
                }
            )
            
            logger.info(f"LLM generated mappings with confidence: {result.get('confidence', 0)}")
            logger.info(f"LLM mappings: {json.dumps(result.get('mappings', {}), indent=2)}")
            logger.info(f"LLM unmappable fields: {result.get('unmappable_fields', [])}")
            return result
            
        except Exception as e:
            logger.error(f"LLM mapping generation failed: {e}")
            
            # Record failed attempt
            record_llm_call(
                org_id=org_id,
                subscriber_id=subscriber_id,
                project_id="workflow",
                screen_id=target_step_id,
                operation="save-process-configs",
                sub_operation="schema-mapping",
                model="gemini-3-flash-preview",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                status="failed",
                metadata={
                    "error": str(e),
                    "process_execution_id": process_execution_id
                }
            )
            
            return {
                "mappings": {},
                "confidence": 0.0,
                "notes": f"LLM call failed: {str(e)}"
            }

    def _parse_llm_response(self, response_text: str) -> Dict[str, Any]:
        """Parse and repair LLM JSON response"""
        try:
            from json_repair import repair_json
            return json.loads(repair_json(response_text))
        except Exception as e:
            logger.error(f"Failed to parse LLM response: {e}")
            return {"valid": False, "issues": ["Invalid AI response format"]}
    
    def _analyze_workflow_mappings_sync(
        self, 
        workflow_config: Dict[str, Any], 
        project_id: Optional[str] = None,
        screen_id: Optional[str] = None,
        version: Optional[str] = None,
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        user_id: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Analyzes workflow mappings for logical errors.
        """
        prompt = f"""
        Analyze the following workflow configuration for data mapping issues:
        {json.dumps(workflow_config, indent=2)}
        
        Validate that:
        1. All steps have valid input/output mappings
        2. Data types are compatible between steps
        3. No circular dependencies exist
        
        Return ONLY valid JSON with "valid" (boolean) and "issues" (list of strings).
        """
        
        try:
            # Check if self.llm is the CrewAI wrapper (which has a .call method) 
            # or the raw client (which has .models.generate_content)
            if hasattr(self.llm, 'call'):
                # Use the CrewAI/LangChain wrapper interface
                response_text = self.llm.call(prompt)
            elif hasattr(self.llm, 'models'):
                # Use the raw Google GenAI Client interface
                response = self.llm.models.generate_content(
                    model='gemini-3-flash-preview',
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                        response_mime_type="application/json"
                    )
                )
                response_text = response.text
            else:
                raise AttributeError("LLM client does not support 'call' or 'models.generate_content'")

            return self._parse_llm_response(response_text)
            
        except Exception as e:
            logger.error(f"Mapping analysis failed: {str(e)}")
            return {"valid": False, "issues": [str(e)]}
    
    def extract_referenced_step_ids(self, input_mappings: Dict[str, str]) -> List[str]:
        """
        Extract step IDs that are referenced in input mappings
        Handles multiple formats:
        1. "context.step_1.outputParameters.field" → "step_1"
        2. "step_1.data.field" → "step_1"
        3. "step_1.outputParameters.field" → "step_1"
        
        Returns ordered list of referenced step IDs (preserves workflow dependencies)
        """
        referenced_steps = []
        seen = set()
        
        for mapping_value in input_mappings.values():
            if not isinstance(mapping_value, str):
                continue
            
            # Try to extract step_id from various formats
            step_id = None
            
            # Format 1: "context.step_1.outputParameters.field"
            if mapping_value.startswith('context.'):
                parts = mapping_value.split('.')
                if len(parts) >= 2:
                    step_id = parts[1]
            # Format 2 & 3: "step_1.data.field" or "step_1.outputParameters.field"
            elif '.' in mapping_value:
                parts = mapping_value.split('.')
                # Check if first part looks like a step_id (contains "step")
                if 'step' in parts[0].lower():
                    step_id = parts[0]
            
            if step_id and step_id not in seen:
                referenced_steps.append(step_id)
                seen.add(step_id)
        
        return referenced_steps
    
    def fix_workflow_mappings(
        self,
        workflow_json: Dict[str, Any],
        analysis: Dict[str, Any],
        org_id: Optional[str] = None,
        subscriber_id: Optional[str] = None,
        process_execution_id: Optional[str] = None
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        Fix mapping issues in workflow
        
        Strategy:
        1. Respect workflow's intended dependencies (referenced steps in mappings)
        2. Search those steps FIRST for matching fields
        3. Fallback to other previous steps if field not found
        4. Keep field blank if not found anywhere
        5. Preserve static values (non-context mappings)
        
        Returns:
            Tuple of (fixed_workflow_json, mapping_changes)
        """
        steps = workflow_json.get('steps', [])
        mapping_changes = []
        
        for i, step in enumerate(steps):
            if i == 0:
                continue  # Skip first step
            
            step_id = step.get('stepId')
            input_mappings = step.get('inputMappings', {})
            
            if not input_mappings:
                continue
            
            # Extract which steps this step intends to use (workflow dependencies)
            referenced_step_ids = self.extract_referenced_step_ids(input_mappings)
            logger.info(f"Step {step_id} references: {referenced_step_ids}")
            
            # Build PRIORITIZED map of fields:
            # Priority 1: Fields from referenced steps (intended dependencies)
            # Priority 2: Fields from other previous steps (fallback)
            priority_source_fields = {}  # Referenced steps
            fallback_source_fields = {}  # Other steps
            
            for j in range(i):
                prev_step = steps[j]
                prev_step_id = prev_step.get('stepId')
                prev_output = prev_step.get('outputParameters', prev_step.get('inputParameters', {}))
                
                # Extract fields with their source step ID
                for field in self.extract_field_names(prev_output):
                    if prev_step_id in referenced_step_ids:
                        # Priority: This step is explicitly referenced
                        if field not in priority_source_fields:  # Keep first occurrence
                            priority_source_fields[field] = prev_step_id
                    else:
                        # Fallback: Available but not explicitly referenced
                        if field not in fallback_source_fields:
                            fallback_source_fields[field] = prev_step_id
            
            # Separate mappings into:
            # 1. context-based (from previous steps) - need fixing
            # 2. static values (literals) - preserve as-is
            context_mappings = {}
            static_mappings = {}
            
            # Detect mapping format from existing mappings
            uses_context_prefix = False
            for mapping_value in input_mappings.values():
                if isinstance(mapping_value, str) and mapping_value.startswith('context.'):
                    uses_context_prefix = True
                    break
            
            for target_field, mapping_value in input_mappings.items():
                # Check if it's a data mapping (from previous step)
                is_data_mapping = False
                if isinstance(mapping_value, str):
                    if mapping_value.startswith('context.'):
                        is_data_mapping = True
                    elif '.' in mapping_value and any(step_word in mapping_value.lower() for step_word in ['step_', 'step']):
                        is_data_mapping = True
                
                if is_data_mapping:
                    context_mappings[target_field] = mapping_value
                else:
                    # Static value, config, or literal - preserve it
                    static_mappings[target_field] = mapping_value
            
            logger.info(f"Step {step_id} mapping format: {'context.' if uses_context_prefix else 'step.data.'} prefix")
            
            # Try pattern matching on context-based mappings only
            # Strategy: Search prioritized steps FIRST, then fallback to others
            pattern_mappings = {}
            unmapped_fields = []
            
            for target_field, current_mapping in context_mappings.items():
                # Extract just the field name (last part of dot notation)
                target_field_name = target_field.split('.')[-1] if '.' in target_field else target_field
                matched = False
                
                # PRIORITY 1: Try to find in REFERENCED steps first
                # Strategy 1: Direct match in priority steps
                if target_field_name in priority_source_fields:
                    source_step_id = priority_source_fields[target_field_name]
                    # Use appropriate format based on original workflow format
                    if uses_context_prefix:
                        pattern_mappings[target_field] = f"context.{source_step_id}.outputParameters.{target_field_name}"
                    else:
                        pattern_mappings[target_field] = f"{source_step_id}.data.{target_field_name}"
                    matched = True
                    logger.info(f"✓ Direct match (PRIORITY): {target_field} → {target_field_name} (from {source_step_id})")
                    continue
                
                # Strategy 2: Case conversion in priority steps
                snake_version = self.camel_to_snake(target_field_name)
                if snake_version in priority_source_fields:
                    source_step_id = priority_source_fields[snake_version]
                    if uses_context_prefix:
                        pattern_mappings[target_field] = f"context.{source_step_id}.outputParameters.{snake_version}"
                    else:
                        pattern_mappings[target_field] = f"{source_step_id}.data.{snake_version}"
                    matched = True
                    logger.info(f"✓ Snake case match (PRIORITY): {target_field} → {snake_version} (from {source_step_id})")
                    continue
                
                camel_version = self.snake_to_camel(target_field_name)
                if camel_version in priority_source_fields:
                    source_step_id = priority_source_fields[camel_version]
                    if uses_context_prefix:
                        pattern_mappings[target_field] = f"context.{source_step_id}.outputParameters.{camel_version}"
                    else:
                        pattern_mappings[target_field] = f"{source_step_id}.{camel_version}"
                    matched = True
                    logger.info(f"✓ Camel case match (PRIORITY): {target_field} → {camel_version} (from {source_step_id})")
                    continue
                
                # PRIORITY 2: Fallback to other steps if not found in referenced steps
                # Strategy 3: Direct match in fallback steps
                if target_field_name in fallback_source_fields:
                    source_step_id = fallback_source_fields[target_field_name]
                    if uses_context_prefix:
                        pattern_mappings[target_field] = f"context.{source_step_id}.outputParameters.{target_field_name}"
                    else:
                        pattern_mappings[target_field] = f"{source_step_id}.{target_field_name}"
                    matched = True
                    logger.info(f"✓ Direct match (FALLBACK): {target_field} → {target_field_name} (from {source_step_id})")
                    continue
                
                # Strategy 4: Case conversion in fallback steps
                if snake_version in fallback_source_fields:
                    source_step_id = fallback_source_fields[snake_version]
                    if uses_context_prefix:
                        pattern_mappings[target_field] = f"context.{source_step_id}.outputParameters.{snake_version}"
                    else:
                        pattern_mappings[target_field] = f"{source_step_id}.{snake_version}"
                    matched = True
                    logger.info(f"✓ Snake case match (FALLBACK): {target_field} → {snake_version} (from {source_step_id})")
                    continue
                
                if camel_version in fallback_source_fields:
                    source_step_id = fallback_source_fields[camel_version]
                    if uses_context_prefix:
                        pattern_mappings[target_field] = f"context.{source_step_id}.outputParameters.{camel_version}"
                    else:
                        pattern_mappings[target_field] = f"{source_step_id}.{camel_version}"
                    matched = True
                    logger.info(f"✓ Camel case match (FALLBACK): {target_field} → {camel_version} (from {source_step_id})")
                    continue
                
                # Strategy 5: Known variations (try priority first, then fallback)
                for source_field, variations in COMMON_FIELD_MAPPINGS.items():
                    if target_field_name.lower() in [v.lower() for v in variations]:
                        if source_field in priority_source_fields:
                            source_step_id = priority_source_fields[source_field]
                            if uses_context_prefix:
                                pattern_mappings[target_field] = f"context.{source_step_id}.outputParameters.{source_field}"
                            else:
                                pattern_mappings[target_field] = f"{source_step_id}.{source_field}"
                            matched = True
                            logger.info(f"✓ Variation match (PRIORITY): {target_field} → {source_field} (from {source_step_id})")
                            break
                        elif source_field in fallback_source_fields:
                            source_step_id = fallback_source_fields[source_field]
                            if uses_context_prefix:
                                pattern_mappings[target_field] = f"context.{source_step_id}.outputParameters.{source_field}"
                            else:
                                pattern_mappings[target_field] = f"{source_step_id}.{source_field}"
                            matched = True
                            logger.info(f"✓ Variation match (FALLBACK): {target_field} → {source_field} (from {source_step_id})")
                            break
                
                if not matched:
                    unmapped_fields.append(target_field)
                    logger.warning(f"✗ No match found for: {target_field} (will try LLM or keep blank)")
            
            # If there are unmapped fields, use LLM as last resort
            llm_mappings = {}
            confidence = 1.0
            
            if unmapped_fields:
                # Build schemas: prioritize referenced steps, include others as fallback
                primary_schemas = {}
                fallback_schemas = {}
                
                for j in range(i):
                    prev_step = steps[j]
                    prev_step_id = prev_step.get('stepId')
                    prev_output = prev_step.get('outputParameters', prev_step.get('inputParameters', {}))
                    
                    if prev_step_id in referenced_step_ids:
                        primary_schemas[prev_step_id] = prev_output
                    else:
                        fallback_schemas[prev_step_id] = prev_output
                
                # Combine: priority schemas first, then fallback
                combined_schema = {**primary_schemas, **fallback_schemas}
                
                llm_result = self._sync_generate_mappings_with_llm(
                    combined_schema,
                    unmapped_fields,
                    "all_previous_steps",  # Indicate multiple sources
                    step_id,
                    context={"priority_steps": list(primary_schemas.keys()), "fallback_steps": list(fallback_schemas.keys())},
                    uses_context_prefix=uses_context_prefix,
                    org_id=org_id,
                    subscriber_id=subscriber_id,
                    process_execution_id=process_execution_id
                )
                llm_mappings = llm_result.get('mappings', {})
                confidence = llm_result.get('confidence', 0)
                
                # Log which fields LLM couldn't map (will be kept blank)
                unmappable_fields = llm_result.get('unmappable_fields', [])
                if unmappable_fields:
                    logger.warning(f"⚠ Fields not found in any previous step (keeping blank): {unmappable_fields}")
            
            # Merge: pattern matches + LLM results + preserved static values
            # Note: Unmappable fields are NOT included (kept blank as per requirement)
            final_mappings = {**pattern_mappings, **llm_mappings, **static_mappings}
            
            # Update workflow if mappings changed
            if final_mappings != input_mappings:
                step['inputMappings'] = final_mappings
                
                # Record changes (only for context-based mappings that were fixed)
                for field, new_mapping in {**pattern_mappings, **llm_mappings}.items():
                    old_mapping = context_mappings.get(field, "")
                    if old_mapping != new_mapping:
                        mapping_changes.append({
                            "step": step_id,
                            "field": field,
                            "original_mapping": old_mapping,
                            "corrected_mapping": new_mapping,
                            "method": "pattern" if field in pattern_mappings else "llm",
                            "confidence": confidence if field in llm_mappings else 1.0
                        })
        
        return workflow_json, mapping_changes
    
    def get_cached_mapping(self, source_hash: str, target_hash: str) -> Optional[Dict[str, str]]:
        """Check in-memory cache for mapping"""
        cache_key = f"{source_hash}:{target_hash}"
        
        if cache_key in _mapping_cache:
            logger.info(f"Cache hit for {cache_key}")
            return _mapping_cache[cache_key]
        
        return None
    
    def cache_mapping(self, source_hash: str, target_hash: str, mappings: Dict[str, str]):
        """Store mapping in memory cache"""
        cache_key = f"{source_hash}:{target_hash}"
        
        # Limit cache size
        if len(_mapping_cache) >= MAX_CACHE_SIZE:
            # Remove oldest entry
            oldest_key = next(iter(_mapping_cache))
            del _mapping_cache[oldest_key]
            logger.info(f"Cache size limit reached, removed {oldest_key}")
        
        _mapping_cache[cache_key] = mappings
        logger.info(f"Cached mapping for {cache_key}")

    async def analyze_workflow_mappings_async(self, workflow_config: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """Async wrapper that offloads to thread"""
        return await run_sync(
            self._analyze_workflow_mappings_sync,  # Call the new renamed method
            workflow_config,
            **kwargs
        )

    # NEW ASYNC WRAPPER
    def fix_workflow_mappings_async(self, workflow, analysis_result, org_id=None, subscriber_id=None, process_execution_id=None):
        return  run_sync(
            self.fix_workflow_mappings,
            workflow,
            analysis_result,
            org_id=org_id,
            subscriber_id=subscriber_id,
            process_execution_id=process_execution_id
        )