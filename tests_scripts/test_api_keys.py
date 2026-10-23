"""
Test to demonstrate current limitations with fields from non-previous steps
"""

import asyncio
import json
import sys
from pathlib import Path
import logging

# Configure logging to see what's happening
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s - %(name)s - %(message)s'
)

sys.path.insert(0, str(Path(__file__).parent))

from services.schema_mapper import SchemaMapper
from services.schema_validator import SchemaValidator
from crewai import LLM


def get_gemini_api_key():
    """Get API key"""
    import os
    from google.cloud import secretmanager
    
    try:
        # Try both paths (from backend dir and from root)
        config_path = 'config.json' if Path('config.json').exists() else 'backend/config.json'
        with open(config_path, 'r') as f:
            config = json.load(f)
        client = secretmanager.SecretManagerServiceClient()
        project_id = config.get("gcp_project_id", "202844388711")
        secret_id = config.get("gcp_secret_id", "Himanshu")
        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        return os.environ.get('GEMINI_API_KEY')


def create_test_workflow_multi_source1():
    """
    Test workflow where Step 3 needs data from BOTH Step 1 and Step 2
    AND has some static/config values
    """
    return {
        "workflowId": "test_multi_source",
        "name": "Multi-Source Mapping Test",
        "steps": [
            {
                "stepId": "step_1",
                "taskTitle": "User Input",
                "type": "ui",
                "outputParameters": {
                    "user_id": "user_123",
                    "user_details":{
                        "user_name": "John Doe",
                        "email": "john@example.com",
                        "address": "123 Main St, delhi",
                    },
                    "shipping_address": "135, Mumbai",
                }
            },
            {
                "stepId": "step_2",
                "taskTitle": "OCR Processing",
                "type": "ai_agent",
                "outputParameters": {
                    "invoice_number": "INV-001",
                    "total": 5000,
                    "date": "2024-10-01",
                    "items": [
                        {"item_id": "item_1", "quantity": 2, "price": 1000},
                        {"item_id": "item_2", "quantity": 1, "price": 3000}
                    ],
                    "amount_due": 5000,

                }
            },
            {
                "stepId": "step_3",
                "taskTitle": "Save to Database",
                "type": "ai_agent",
                "inputMappings": {
                    # From step_1 (not previous step!)
                    "userId": "context.step_1.outputParameters.userId",
                    "userName": "context.step_1.outputParameters.userName",
                    "userEmail": "context.step_1.outputParameters.userEmail",
                    "userAddress": "context.step_1.outputParameters.userAddress",
                    "shippingAddress": "context.step_1.outputParameters.shippingAddress",

                    # From step_2 (previous step)
                    "invoiceNumber": "context.step_2.outputParameters.invoiceNumber",
                    "totalAmount": "context.step_2.outputParameters.totalAmount",
                    
                    # Static/config value (not from any step!)
                    "apiEndpoint": "https://api.example.com/save",
                    "apiKey": "static-key-12345"
                }
            }
        ]
    }

def create_test_workflow_multi_source():
    return {
  "steps": [
    {
      "stepId": "step_1",
      "stepNumber": 1,
      "taskTitle": "Upload Invoice PDF",
      "description": "User uploads an invoice PDF document.",
      "inputParameters": [
        {
          "name": "screen_title",
          "dataType": "String",
          "value": "",
          "label": "screen_title",
          "description": "Title shown on upload screen"
        }
      ],
      "outputParameters": [
        {
          "name": "doc_id",
          "dataType": "String",
          "value": "",
          "label": "doc_id",
          "description": "Internal document identifier"
        },
        {
          "name": "storage_path",
          "dataType": "String",
          "value": "",
          "label": "storage_path",
          "description": "File system path of uploaded PDF"
        },
        {
          "name": "uploaded_filename",
          "dataType": "String",
          "value": "",
          "label": "uploaded_filename",
          "description": "Original uploaded file name"
        }
      ],
      "inputMappings": {},
      "nextSteps": [2],
      "screenId": "screen_upload_001",
      "projectId": "invoice_project"
    },

    {
      "stepId": "step_2",
      "stepNumber": 2,
      "taskTitle": "OCR & Invoice Extraction",
      "description": "Runs OCR and extracts structured invoice data.",
      "inputParameters": [
        {
          "name": "documentId",
          "dataType": "String",
          "value": "",
          "label": "documentId",
          "description": "Unique document identifier"
        },
        {
          "name": "filePath",
          "dataType": "String",
          "value": "",
          "label": "filePath",
          "description": "Path of the uploaded PDF file"
        },
        {
          "name": "originalFileName",
          "dataType": "String",
          "value": "",
          "label": "originalFileName",
          "description": "Original name of the PDF"
        }
      ],
      "outputParameters": [
        {
          "name": "invoice_id",
          "dataType": "String",
          "value": "",
          "label": "invoice_id",
          "description": "Extracted invoice number"
        },
        {
          "name": "invoice_payload",
          "dataType": "String",
          "value": "",
          "label": "invoice_payload",
          "description": "Structured invoice JSON after OCR"
        }
      ],
      "inputMappings": {
        "documentId": "step_1.data.documentId",
        "filePath": "step_1.data.filePath",
        "originalFileName": "step_1.data.originalFileName"
      },
      "nextSteps": [3],
      "screenId": "screen_ocr_002",
      "projectId": "invoice_project"
    },

    {
      "stepId": "step_3",
      "stepNumber": 3,
      "taskTitle": "Review & Save Invoice",
      "description": "User reviews extracted invoice data and saves it to accounting system.",
      "inputParameters": [
        {
          "name": "documentId",
          "dataType": "String",
          "value": "",
          "label": "documentId",
          "description": "Document identifier for persistence"
        },
        {
          "name": "invoiceNumber",
          "dataType": "String",
          "value": "",
          "label": "invoiceNumber",
          "description": "Invoice number to be stored"
        },
        {
          "name": "invoiceData",
          "dataType": "String",
          "value": "",
          "label": "invoiceData",
          "description": "Final structured invoice JSON"
        }
      ],
      "outputParameters": [
        {
          "name": "save_status",
          "dataType": "String",
          "value": "",
          "label": "save_status",
          "description": "Status of invoice save operation"
        }
      ],
      "inputMappings": {
        "documentId": "step_1.data.doc_id",
        "invoiceNumber": "step_2.data.invoiceNumber",
        "invoiceData": "step_2.data.invoicePayload"
      },
      "nextSteps": [],
      "screenId": "screen_save_003",
      "projectId": "invoice_project"
    }
  ]
}


async def test_multi_source():
    """Test the multi-source scenario"""
    print("\n" + "="*80)
    print("TEST: Multi-Source Field Mapping")
    print("="*80 + "\n")
    
    api_key = get_gemini_api_key()
    print(api_key)
    llm = LLM(model="gemini/gemini-3.5-flash-lite", temperature=0.7, api_key=api_key)
    
    # workflow = create_test_workflow_multi_source()
    
    # print("TESTING SCENARIO:")
    
    # # Validate
    # validator = SchemaValidator()
    # validation_result = validator.validate_step_mappings(workflow['steps'])
    # print(f"\n1. Validation: {'✓ Valid' if validation_result['valid'] else '✗ Invalid'}")
    
    # # Analyze
    # mapper = SchemaMapper(llm, api_key)
    # analysis_result = await mapper.analyze_workflow_mappings(workflow)
    # print(f"2. Analysis: {'⚠ Issues Found' if analysis_result['has_issues'] else '✓ No Issues'}")
    
    # # Try to fix
    # fixed_workflow, mapping_changes = await mapper.fix_workflow_mappings(workflow, analysis_result)
    
    # print(f"\n3. Auto-Fix Results:")
    # if mapping_changes:
    #     print(f"   ✓ Fixed {len(mapping_changes)} mappings")
    #     print(fixed_workflow)

    # else:
    #     print("   ⚠ No changes made")
    
    

if __name__ == "__main__":
    asyncio.run(test_multi_source())
