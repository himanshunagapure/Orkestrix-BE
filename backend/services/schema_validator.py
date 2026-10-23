"""
Schema Validator Service
Validates workflow step mappings and detects potential issues
"""

from typing import Dict, List, Any, Optional
import logging

logger = logging.getLogger(__name__)


class SchemaValidator:
    """Validates schema mappings between workflow steps"""
    
    def validate_step_mappings(self, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Check all inputMappings in workflow for potential issues
        
        Returns:
            Dict with validation results
        """
        issues = []
        warnings = []
        
        for i, step in enumerate(steps):
            step_id = step.get('stepId')
            input_mappings = step.get('inputMappings', {})
            
            if i == 0:
                # First step should not have input mappings from previous steps
                if input_mappings:
                    warnings.append({
                        "step_id": step_id,
                        "message": "First step has input mappings (may be static values)"
                    })
                continue
            
            if not input_mappings:
                warnings.append({
                    "step_id": step_id,
                    "message": "No input mappings defined"
                })
                continue
            
            # Validate each mapping
            for target_field, mapping_path in input_mappings.items():
                if not isinstance(mapping_path, str):
                    issues.append({
                        "step_id": step_id,
                        "field": target_field,
                        "issue": f"Mapping path must be a string, got {type(mapping_path)}"
                    })
                    continue
                
                # Check if it follows context.stepId.outputParameters format
                if not mapping_path.startswith('context.'):
                    issues.append({
                        "step_id": step_id,
                        "field": target_field,
                        "issue": f"Mapping path should start with 'context.', got: {mapping_path}"
                    })
        
        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "warnings": warnings,
            "steps_checked": len(steps)
        }
    
    def detect_mapping_issues(
        self,
        source_step: Dict[str, Any],
        target_step: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Find mismatches between source output and target input mappings
        
        Returns:
            List of detected issues
        """
        issues = []
        
        source_step_id = source_step.get('stepId')
        target_step_id = target_step.get('stepId')
        
        # Get source output schema
        source_output = source_step.get('outputParameters', source_step.get('inputParameters', {}))
        
        # Get target input mappings
        target_mappings = target_step.get('inputMappings', {})
        
        if not target_mappings:
            return issues
        
        # Extract available source fields
        source_fields = self._extract_field_paths(source_output)
        
        # Check each target mapping
        for target_field, mapping_path in target_mappings.items():
            if not isinstance(mapping_path, str):
                continue
            
            # Extract the field path from context.stepId.outputParameters.field
            if mapping_path.startswith(f'context.{source_step_id}.outputParameters.'):
                field_path = mapping_path.replace(f'context.{source_step_id}.outputParameters.', '')
                
                # Check if this field exists in source
                if field_path not in source_fields:
                    issues.append({
                        "source_step": source_step_id,
                        "target_step": target_step_id,
                        "target_field": target_field,
                        "mapping_path": mapping_path,
                        "issue": f"Field '{field_path}' not found in source output",
                        "available_fields": source_fields[:10]  # Show first 10 for reference
                    })
        
        return issues
    
    def get_expected_schemas(self, steps: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """
        Extract expected input/output structures for each step
        
        Returns:
            Dict mapping step_id to schema info
        """
        schemas = {}
        
        for step in steps:
            step_id = step.get('stepId')
            
            schemas[step_id] = {
                "input_parameters": step.get('inputParameters', {}),
                "input_mappings": step.get('inputMappings', {}),
                "output_parameters": step.get('outputParameters', {}),
                "step_type": step.get('type'),
                "task_title": step.get('taskTitle', '')
            }
        
        return schemas
    
    def _extract_field_paths(self, obj: Any, prefix: str = "") -> List[str]:
        """Recursively extract all field paths from an object"""
        fields = []
        
        if isinstance(obj, dict):
            for key, value in obj.items():
                current_path = f"{prefix}.{key}" if prefix else key
                fields.append(current_path)
                
                if isinstance(value, (dict, list)):
                    fields.extend(self._extract_field_paths(value, current_path))
        
        elif isinstance(obj, list) and obj:
            # For arrays, sample the first item
            fields.extend(self._extract_field_paths(obj[0], prefix))
        
        return fields