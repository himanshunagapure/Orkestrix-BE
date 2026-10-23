import json
import google.generativeai as genai
from typing import List, Dict, Any
import copy
from datetime import datetime
import asyncio
import functools

from services.llm_token_tracker import record_llm_call
from services.llm_utils import extract_token_usage_from_response

async def run_sync(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    pfunc = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(None, pfunc)

class WorkflowJSONGenerator:
    def __init__(self, gemini_api_key: str):
        """Initialize the workflow generator with Gemini API"""
        genai.configure(api_key=gemini_api_key)
        self.model = genai.GenerativeModel('gemini-3-flash-preview')
    
    def generate_workflow_json(self, usecase_description: str, tools_selected: List[Dict], additional_details: Dict, org_id: str = None, subscriber_id: str = None) -> Dict:
 
        # Extract process_execution_id from additional_details
        process_execution_id = additional_details.get('process_execution_id')
        if org_id is None:
            # Check both camelCase (orgId) and snake_case (org_id) for compatibility
            org_id = additional_details.get('orgId') or additional_details.get('org_id')
        if subscriber_id is None:
            # Check both camelCase (subscriberId) and snake_case (subscriber_id) for compatibility
            subscriber_id = additional_details.get('subscriberId') or additional_details.get('subscriber_id')    
        
        # STEP 1: Modify AI agent tools to add output_format_type
        modified_tools = self._prepare_ai_agent_tools(
            tools_selected, 
            usecase_description, 
            org_id, 
            subscriber_id,
            process_execution_id
        )
        # self.model = genai.GenerativeModel('gemini-3-flash-preview')
        # STEP 2: Create the prompt for Gemini
        prompt1 = self._create_gemini_prompt(usecase_description, modified_tools)
        
        # STEP 3: Generate workflow structure using Gemini (Pattern Analysis)
        started_at = datetime.utcnow()
        response = self.model.generate_content(prompt1)
        finished_at = datetime.utcnow()
        
        # Record LLM call for pattern analysis
        token_data = extract_token_usage_from_response(response)
        record_llm_call(
            org_id=org_id,
            subscriber_id=subscriber_id,
            project_id="workflow",
            screen_id=None,
            operation="save-process-configs",
            sub_operation="workflow-pattern-analysis",
            model="gemini-3-flash-preview",
            prompt_tokens=token_data["prompt_tokens"],
            completion_tokens=token_data["completion_tokens"],
            total_tokens=token_data["total_tokens"],
            status="success",
            started_at=started_at,
            finished_at=finished_at,
            metadata={
                "tools_count": len(modified_tools),
                "process_execution_id": process_execution_id
            }
        )
        
        pattern_analysis = self._parse_gemini_response(response.text)
        print('='*50)
        print(json.dumps(pattern_analysis,indent=2))
        print('='*50)
        prompt2 = self._create_gemini_prompt_final(usecase_description, modified_tools, pattern_analysis, additional_details)
        
        # STEP 4: Generate final workflow structure
        started_at = datetime.utcnow()
        response = self.model.generate_content(prompt2)
        finished_at = datetime.utcnow()
        
        # Record LLM call for workflow generation
        token_data = extract_token_usage_from_response(response)
        record_llm_call(
            org_id=org_id,
            subscriber_id=subscriber_id,
            project_id="workflow",
            screen_id=None,
            operation="save-process-configs",
            sub_operation="workflow-structure-generation",
            model="gemini-3-flash-preview",
            prompt_tokens=token_data["prompt_tokens"],
            completion_tokens=token_data["completion_tokens"],
            total_tokens=token_data["total_tokens"],
            status="success",
            started_at=started_at,
            finished_at=finished_at,
            metadata={
                "tools_count": len(modified_tools),
                "process_execution_id": process_execution_id
            }
        )
        
        workflow_structure = self._parse_gemini_response(response.text)
        print('='*50)
        print(json.dumps(workflow_structure,indent=2))
        print('='*50)
        # STEP 4: Build the complete workflow JSON
        
        
        return workflow_structure
    
    def _prepare_ai_agent_tools(self, tools_selected: List[Dict], usecase_description: str, org_id: str = None, subscriber_id: str = None, process_execution_id: str = None) -> List[Dict]:
        """
        Pre-process tools to add output_format_type to AI agent tools
        This happens BEFORE JSON generation
        """
        modified_tools = copy.deepcopy(tools_selected)
        
        for i, tool in enumerate(modified_tools):
            if 'assignmentConfig' in tool:
                tool['assignment_context'] = tool['assignmentConfig']

            # --- EXISTING LOGIC (Preserved) ---
            if self._is_ai_agent_step(tool):
                # Analyze what outputs are needed by next steps
                next_tools = modified_tools[i + 1:] if i < len(modified_tools) - 1 else []
                prev_tools = modified_tools[:i]
                
                # Generate output_format_type based on next step requirements
                ipop_format = self._generate_ipoutput_params_format(
                    tool, 
                    prev_tools,
                    next_tools, 
                    usecase_description,
                    org_id,
                    subscriber_id,
                    process_execution_id
                )
                input_format = ipop_format['input_params']
                output_format = ipop_format['output_params']
                
                # Add output_format_type to inputParameters
                if 'inputParameters' not in tool:
                    tool['inputParameters'] = {}
                
                tool['inputParameters']['output_format_type'] = json.dumps(output_format)
                for params in input_format:
                    tool['inputParameters'][params] = input_format[params]

                tool['inputParameters']['savedAgentPath'] = tool.get('savedAgentPath','')
                tool['inputParameters']['savedAgentDescription'] = tool.get('savedAgentDescription','')
                
                print(f"\n[AI Agent Tool Modified] {tool.get('taskTitle')}")
                print(f"output_format_type: {output_format}\n")
                print(f"input params format: {input_format}")
        
        return modified_tools
    
    def _generate_ipoutput_params_format(self, ai_agent_tool: Dict, 
                                       prev_tools: List[Dict],
                                       next_tools: List[Dict], 
                                       usecase_description: str,
                                       org_id: str = None,
                                       subscriber_id: str = None,
                                       process_execution_id: str = None) -> str:
        """
        Generate the output_format_type string for AI agent
        This tells the AI agent what output structure to produce
        """
        
        # Extract what next steps need
        required_outputs = self._extract_required_inputs(next_tools)
        
        # Extract what previous steps provide
        available_inputs = self._extract_available_outputs(prev_tools)
        
        # Use Gemini to intelligently determine output format
        prompt = f"""
You are analyzing an AI agent in a workflow to determine what output format it should produce.

USE CASE:
{usecase_description}

AI AGENT DETAILS:
- Task Title: {ai_agent_tool.get('taskTitle')}
- Description: {ai_agent_tool.get('description')}
- Agent Description: {ai_agent_tool.get('savedAgentDescription', 'N/A')}

AVAILABLE INPUTS (from previous steps):
{json.dumps(available_inputs, indent=2)}

REQUIRED BY NEXT STEPS:
{json.dumps(required_outputs, indent=2)}

TASK:
Based on the AI agent's role and what the next steps need, determine what input and output parameters this AI agent should need/produce.

Rules:
1. Include all parameters that next steps require as input
2. Add any additional outputs that make sense for this agent's task
3. Use appropriate data types: string, number, boolean, array, object
4. Keep parameter names clear and consistent

Provide a JSON object with the input and output parameters (no explanations, no markdown):
{{
"input_params":{{
    "param_name": "<data_type value>",
    "another_param": "<data_type value>"
}},
"output_params":{{
    "param_name": "<data_type value>",
    "another_param": "<data_type value>"
}}
}}
Examples of format:
- String: "register_id": "<string value>"
- Number: "count": "<number value>"
- Boolean: "is_active": "<true/false>"
- Array: "items": ["<array items>"]
- Object: "metadata": {{"<key>": "<value>"}}
"""
        
        try:
            started_at = datetime.utcnow()
            response = self.model.generate_content(prompt)
            finished_at = datetime.utcnow()
            
            # Record LLM call for output format generation
            token_data = extract_token_usage_from_response(response)
            record_llm_call(
                org_id=org_id,
                subscriber_id=subscriber_id,
                project_id="workflow",
                screen_id=None,
                operation="save-process-configs",
                sub_operation="ai-agent-output-format-generation",
                model="gemini-3-flash-preview",
                prompt_tokens=token_data["prompt_tokens"],
                completion_tokens=token_data["completion_tokens"],
                total_tokens=token_data["total_tokens"],
                status="success",
                started_at=started_at,
                finished_at=finished_at,
                metadata={
                    "agent_title": ai_agent_tool.get('taskTitle', 'unknown'),
                    "process_execution_id": process_execution_id
                }
            )
            
            response_text = response.text.strip()
            
            # Extract JSON from response
            start_idx = response_text.find('{')
            end_idx = response_text.rfind('}') + 1
            
            if start_idx != -1 and end_idx != 0:
                json_str = response_text[start_idx:end_idx]
                # Validate it's proper JSON
                
                return json.loads(json_str)
            else:
                return {},"{'output':'<string result>'}"
                
        except Exception as e:
            print(f"Error generating output format with LLM: {e}")
            return {},"{'output':'<string result>'}"

    def _extract_available_outputs(self, prev_tools: List[Dict]) -> Dict:
        """Extract all available outputs from previous steps"""
        available = {}
        
        for i, tool in enumerate(prev_tools):
            step_id = f"step_{i + 1}"
            output_params = tool.get("outputParameters", {})
            
            for key in output_params.keys():
                available[f"{step_id}.{key}"] = {
                    "stepId": step_id,
                    "stepTitle": tool.get("taskTitle"),
                    "paramName": key,
                    "dataType": self._infer_data_type(key)
                }
        
        return available
    
    def _extract_required_inputs(self, next_tools: List[Dict]) -> Dict:
        """Extract required inputs from next steps"""
        required = {}
        
        for tool in next_tools:
            input_params = tool.get("inputParameters", {})
            
            for key in input_params.keys():
                # Skip output_format_type as it's not a real input
                if key == "output_format_type":
                    continue
                    
                required[key] = {
                    "stepTitle": tool.get("taskTitle"),
                    "paramName": key,
                    "dataType": self._infer_data_type(key)
                }
        
        return required
    
    def _infer_data_type(self, param_name: str) -> str:
        """Infer data type from parameter name"""
        param_lower = param_name.lower()
        
        if "id" in param_lower:
            return "string"
        elif "count" in param_lower or "amount" in param_lower or "number" in param_lower:
            return "number"
        elif "is_" in param_lower or "has_" in param_lower or "flag" in param_lower:
            return "boolean"
        elif "list" in param_lower or "items" in param_lower or "array" in param_lower:
            return "array"
        elif "data" in param_lower or "details" in param_lower or "info" in param_lower:
            return "object"
        else:
            return "string"
    
    def _is_ai_agent_step(self, tool: Dict) -> bool:
        """Check if the tool is an AI agent"""
        task_title = tool.get("taskTitle", "").lower()
        return "ai agent" in task_title or "ai_agent" in task_title
    
    def _create_gemini_prompt(self, usecase_description: str, tools_selected: List[Dict]) -> str:
        """Create a detailed prompt for Gemini to analyze the workflow"""
        
        tools_info = json.dumps(tools_selected, indent=2)
        
        
        STAGE_1_PROMPT = f"""
You are a workflow pattern detection expert. Analyze the use case and tools to identify required workflow patterns.

USE CASE DESCRIPTION:
{usecase_description}

AVAILABLE TOOLS:
{tools_info}

TASK:
Carefully analyze the use case and tools to detect:

1. **LOOP DETECTION**:
   - Does any tool output a LIST/ARRAY/COLLECTION of items?
   - Does any subsequent tool need to process EACH item individually?
   - Keywords to look for: "multiple", "each", "all files", "batch", "collection"
   
2. **CONDITIONAL LOGIC DETECTION**:
   - Are there any if-then scenarios mentioned?
   - Does processing depend on validation, approval, or status checks?
   - Keywords to look for: "if", "check", "validate", "approve", "based on"
   
3. **PARALLEL EXECUTION DETECTION**:
   - Can multiple operations run simultaneously without dependencies?
   - Are there independent processing branches?
   - Keywords to look for: "simultaneously", "parallel", "at the same time"

4. **DATA FLOW ANALYSIS**:
   - Which tool outputs feed into which tool inputs?
   - Are there array-to-single or single-to-array transformations?

Provide your analysis in the following JSON format:
{{
    "hasLoop": true/false,
    "loopDetails": {{
        "sourceStep": "step number that outputs collection",
        "sourceOutput": "parameter name containing array/list",
        "loopType": "foreach/while/for",
        "loopVariable": "suggested variable name",
        "stepsInLoop": [step numbers that should be in loop],
        "reasoning": "why loop is needed"
    }},
    "hasConditional": true/false,
    "conditionalDetails": {{
        "conditionStep": "step number where condition is checked",
        "conditionType": "if-then-else/switch/validation",
        "conditionOn": "parameter to check",
        "branches": [
            {{
                "branchName": "success/failure/approved/rejected",
                "condition": "condition description",
                "stepsInBranch": [step numbers]
            }}
        ],
        "reasoning": "why conditional is needed"
    }},
    "hasParallel": true/false,
    "parallelDetails": {{
        "parallelSteps": [step numbers that can run parallel],
        "reasoning": "why parallel execution is beneficial"
    }},
    "dataFlowAnalysis": [
        {{
            "fromStep": 1,
            "fromOutput": "output parameter name",
            "toStep": 2,
            "toInput": "input parameter name",
            "transformationType": "array-to-single/single-to-array/direct"
        }}
    ],
    "recommendedStructure": "brief description of recommended workflow structure",
    "reasoning": "overall explanation of why these patterns are needed"
}}

Think step by step:
1. First, examine each tool's input and output parameters
2. Identify which outputs are arrays/lists vs single values
3. Check if subsequent tools expect single values when previous outputs arrays
4. Look for decision points in the use case description
5. Determine execution order and dependencies
6. If there is a single tool and it is AI agent, no loop and conditional is needed.

Be very thorough in your analysis. If unsure, explain your reasoning.
"""



        
        
        return STAGE_1_PROMPT
    
    def _create_gemini_prompt_final(self, usecase_description:str, tools_selected: List[Dict], pattern_analysis:dict, additional_details: Dict) -> str:
        tools_info = json.dumps(tools_selected,indent=2)
        STAGE_2_PROMPT = f"""
You are a workflow automation expert. Based on the pattern analysis, create a complete workflow structure.

USE CASE DESCRIPTION:
{usecase_description}

AVAILABLE TOOLS:
{tools_info}

PATTERN ANALYSIS FROM STAGE 1:
{pattern_analysis}

ADDITIONAL DETAILS:
{json.dumps(additional_details, indent=2)}

TASK:
Create a detailed workflow structure that implements the patterns identified in Stage 1.

IMPORTANT GUIDELINES:
1. If hasLoop=true, create a loop step with nested steps inside loopLogic.steps[]
2. If hasConditional=true, create conditional branches with nested steps inside conditionalLogic.branches[].steps[]
3. Use hierarchical step numbering:
   - Main steps: 1, 2, 3
   - Loop steps: 2.L1, 2.L2, 2.L3
   - Conditional steps: 3.1, 3.2 (for branches)
   - Parallel steps: 4.P1, 4.P2
4. For loops, use the loopVariable reference format: "loopVariable.{{variable_name}}.{{property}}"
5. Map tool outputs to subsequent tool inputs carefully
6. ASSIGNMENT MAPPING: Check tools for 'assignment_context'. If present, map 'assignedToUserIds' to 'assignments.users', 'assignedToRoleIds' to 'assignments.roles', and 'assignedToNames' to 'assignments.names'.

Provide your workflow in the following JSON format:
{{
    "workflowName": "Brief descriptive name",
    "workflowId": "wf_{{random_id}}",
    "description": "Detailed workflow description",
    "version": "1.0",
    "createdAt": "{{current_timestamp}}",
    "globalVariables": {{}},
    "steps": [
        {{
            "stepId": "step_1",
            "stepNumber": 1,
            "taskTitle": "Tool title from tools list",
            "description": "Tool description",
            "category": "ui/automatic/api",
            "botId": "from tools list",
            "type": "ui/ai_agent/api/loop",
            "assignments": {{
                "users": ["id_from_assignment_context"],
                "roles": ["id_from_assignment_context"],
                "names": ["name_from_assignment_context"]
            }},
            "inputParameters": {{}},
            "outputParameters": {{}},
            "inputMappings": {{}},
            "conditionalLogic": {{
                "type": "if/switch/none",
                "condition": "condition description",
                "branches": [
                    {{
                        "branchName": "then/else",
                        "condition": "specific condition",
                        "steps": [
                            {{
                                "stepId": "step_1_1",
                                "stepNumber": "1.1",
                                "taskTitle": "...",
                                "inputMappings": {{}},
                                "assignments": {{
                                    "users": [],
                                    "roles": [],
                                    "names": []
                                }},
                                "conditionalLogic": {{"type": "none"}},
                                "loopLogic": {{"type": "none"}},
                                "nextSteps": []
                            }}
                        ]
                    }}
                ]
            }},
            "loopLogic": {{
                "type": "foreach/while/for/none",
                "iterateOver": "stepNumber.outputParameter",
                "loopVariable": "variable_name",
                "steps": [
                    {{
                        "stepId": "step_2_L1",
                        "stepNumber": "2.L1",
                        "taskTitle": "...",
                        "description": "...",
                        "category": "...",
                        "botId": "...",
                        "type": "...",
                        "assignments": {{
                            "users": [],
                            "roles": [],
                            "names": []
                        }},
                        "inputParameters": {{}},
                        "outputParameters": {{}},
                        "inputMappings": {{
                            "param": "loopVariable.variable_name.property"
                        }},
                        "apiConfig": {{
                            "url": "from tools if applicable",
                            "method": "POST",
                            "headers": {{"Content-Type": "application/json"}},
                            "timeout": 60
                        }},
                        "agentConfig": {{
                            "savedAgentPath": "from tools if applicable",
                            "savedAgentDescription": "from tools"
                        }},
                        "conditionalLogic": {{"type": "none"}},
                        "loopLogic": {{"type": "none"}},
                        "errorHandling": {{
                            "onError": "stop",
                            "retryCount": 3,
                            "retryDelay": 5
                        }},
                        "nextSteps": ["2.L2"]
                    }}
                ]
            }},
            "parallelExecution": {{
                "enabled": true/false,
                "parallelSteps": []
            }},
            "errorHandling": {{
                "onError": "stop/continue",
                "retryCount": 3,
                "retryDelay": 5
            }},
            "nextSteps": [2]
        }}
    ],
    "dataFlowExplanation": "Complete explanation of data flow through the workflow"
}}

CRITICAL REQUIREMENTS:
- If pattern analysis shows hasLoop=true, you MUST create a step with loopLogic.type != "none"
- If pattern analysis shows hasConditional=true, you MUST create conditionalLogic.branches
- All steps inside loops must use "loopVariable.{{name}}.{{property}}" for referencing loop items
- Include complete apiConfig and agentConfig blocks for ai_agent type steps
- Ensure all inputMappings reference valid previous step outputs
- Add the additional details data to globalVariables if relevant.
- MANDATORY: You must populate the 'assignments' object for every step using the 'assignment_context' data. If no data exists, leave arrays empty.


Generate the complete workflow structure now.
"""
        
        return STAGE_2_PROMPT

    def _parse_gemini_response(self, response_text: str) -> Dict:
        """Parse Gemini's response and extract JSON structure"""
        try:
            # Remove markdown code blocks if present
            response_text = response_text.strip()
            
            # Remove ```json and ``` markers
            if response_text.startswith('```'):
                # Find the first newline after ```
                first_newline = response_text.find('\n')
                if first_newline != -1:
                    response_text = response_text[first_newline + 1:]
                
                # Remove trailing ```
                if response_text.endswith('```'):
                    response_text = response_text[:-3]
                
                response_text = response_text.strip()
            
            # Try to extract JSON from the response
            start_idx = response_text.find('{')
            end_idx = response_text.rfind('}') + 1
            
            if start_idx != -1 and end_idx != 0:
                json_str = response_text[start_idx:end_idx]
                
                # Validate and parse JSON
                parsed_json = json.loads(json_str)
                
                # Validate that it has the expected structure
                if isinstance(parsed_json, dict):
                    return parsed_json
                else:
                    raise ValueError("Parsed JSON is not a dictionary")
            else:
                raise ValueError("No JSON object found in response")
                
        except json.JSONDecodeError as e:
            print(f"[JSON Parse Error] Failed to parse Gemini response: {e}")
            print(f"[JSON Parse Error] Response text (first 500 chars): {response_text[:500]}")
            # Fallback: create basic structure
            return {
                "workflowName": "Generated Workflow",
                "globalVariables": {},
                "steps": [],
                "dataFlowExplanation": f"JSON parsing error: {str(e)}"
            }
        except Exception as e:
            print(f"[Parse Error] Unexpected error parsing Gemini response: {e}")
            print(f"[Parse Error] Response text (first 500 chars): {response_text[:500]}")
            return {
                "workflowName": "Generated Workflow",
                "globalVariables": {},
                "steps": [],
                "dataFlowExplanation": f"Parsing error: {str(e)}"
            }
    async def generate_workflow_json_async(self, user_prompt, tools_list, additional_details=None):
        """
        Async wrapper for generate_workflow_json to prevent blocking the event loop.
        """
        return await run_sync(
            self.generate_workflow_json,
            user_prompt,
            tools_list,
            additional_details
        )