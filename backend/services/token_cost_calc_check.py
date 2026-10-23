"""
MongoDB Aggregation Script to Calculate Token and Cost Totals
Groups by unique combinations of project_id and screen_id
"""

from pymongo import MongoClient
from typing import List, Dict, Optional
import json


def calculate_token_costs_by_project_and_screen(db) -> List[Dict]:
    """
    Calculate token and cost totals grouped by project_id and screen_id
    
    Args:
        db: MongoDB database instance
        
    Returns:
        List of dictionaries containing totals for each project/screen combination
    """
    collection = db['llm_token_count']
    
    pipeline = [
        {
            '$group': {
                '_id': {
                    'project_id': '$project_id',
                    'screen_id': '$screen_id'
                },
                'total_prompt_tokens': {'$sum': '$prompt_tokens'},
                'total_completion_tokens': {'$sum': '$completion_tokens'},
                'total_tokens': {'$sum': '$total_tokens'},
                'total_input_cost_usd': {'$sum': '$pricing.input_cost_usd'},
                'total_output_cost_usd': {'$sum': '$pricing.output_cost_usd'},
                'total_cost_usd': {'$sum': '$pricing.total_cost_usd'},
                'record_count': {'$sum': 1}
            }
        },
        {
            '$sort': {
                '_id.project_id': 1,
                '_id.screen_id': 1
            }
        },
        {
            '$project': {
                '_id': 0,
                'project_id': '$_id.project_id',
                'screen_id': '$_id.screen_id',
                'total_prompt_tokens': 1,
                'total_completion_tokens': 1,
                'total_tokens': 1,
                'total_input_cost_usd': {'$round': ['$total_input_cost_usd', 6]},
                'total_output_cost_usd': {'$round': ['$total_output_cost_usd', 6]},
                'total_cost_usd': {'$round': ['$total_cost_usd', 6]},
                'record_count': 1
            }
        }
    ]
    
    results = list(collection.aggregate(pipeline))
    return results


def calculate_token_costs_for_specific_screen(
    db, 
    project_id: str, 
    screen_id: str
) -> Optional[Dict]:
    """
    Calculate token and cost totals for a specific project_id and screen_id
    
    Args:
        db: MongoDB database instance
        project_id: The project ID to filter by
        screen_id: The screen ID to filter by
        
    Returns:
        Dictionary containing totals for the specified project/screen, or None if not found
    """
    collection = db['llm_token_count2']
    
    pipeline = [
        {
            '$match': {
                'project_id': project_id,
                'screen_id': screen_id
            }
        },
        {
            '$group': {
                '_id': None,
                'total_prompt_tokens': {'$sum': '$prompt_tokens'},
                'total_completion_tokens': {'$sum': '$completion_tokens'},
                'total_tokens': {'$sum': '$total_tokens'},
                'total_input_cost_usd': {'$sum': '$pricing.input_cost_usd'},
                'total_output_cost_usd': {'$sum': '$pricing.output_cost_usd'},
                'total_cost_usd': {'$sum': '$pricing.total_cost_usd'},
                'record_count': {'$sum': 1}
            }
        },
        {
            '$project': {
                '_id': 0,
                'project_id': project_id,
                'screen_id': screen_id,
                'total_prompt_tokens': 1,
                'total_completion_tokens': 1,
                'total_tokens': 1,
                'total_input_cost_usd': {'$round': ['$total_input_cost_usd', 6]},
                'total_output_cost_usd': {'$round': ['$total_output_cost_usd', 6]},
                'total_cost_usd': {'$round': ['$total_cost_usd', 6]},
                'record_count': 1
            }
        }
    ]
    
    results = list(collection.aggregate(pipeline))
    return results[0] if results else None


def calculate_grand_totals(db) -> Optional[Dict]:
    """
    Calculate grand totals across all projects and screens
    
    Args:
        db: MongoDB database instance
        
    Returns:
        Dictionary containing grand totals, or None if no records found
    """
    collection = db['llm_token_count']
    
    pipeline = [
        {
            '$group': {
                '_id': None,
                'total_prompt_tokens': {'$sum': '$prompt_tokens'},
                'total_completion_tokens': {'$sum': '$completion_tokens'},
                'total_tokens': {'$sum': '$total_tokens'},
                'total_input_cost_usd': {'$sum': '$pricing.input_cost_usd'},
                'total_output_cost_usd': {'$sum': '$pricing.output_cost_usd'},
                'total_cost_usd': {'$sum': '$pricing.total_cost_usd'},
                'total_records': {'$sum': 1},
                'unique_projects': {'$addToSet': '$project_id'},
                'unique_screens': {'$addToSet': '$screen_id'}
            }
        },
        {
            '$project': {
                '_id': 0,
                'total_prompt_tokens': 1,
                'total_completion_tokens': 1,
                'total_tokens': 1,
                'total_input_cost_usd': {'$round': ['$total_input_cost_usd', 6]},
                'total_output_cost_usd': {'$round': ['$total_output_cost_usd', 6]},
                'total_cost_usd': {'$round': ['$total_cost_usd', 6]},
                'total_records': 1,
                'unique_project_count': {'$size': '$unique_projects'},
                'unique_screen_count': {'$size': '$unique_screens'}
            }
        }
    ]
    
    results = list(collection.aggregate(pipeline))
    return results[0] if results else None


def calculate_token_costs_by_project(db) -> List[Dict]:
    """
    Calculate token and cost totals grouped by project_id only
    
    Args:
        db: MongoDB database instance
        
    Returns:
        List of dictionaries containing totals for each project
    """
    collection = db['llm_token_count']
    
    pipeline = [
        {
            '$group': {
                '_id': '$project_id',
                'total_prompt_tokens': {'$sum': '$prompt_tokens'},
                'total_completion_tokens': {'$sum': '$completion_tokens'},
                'total_tokens': {'$sum': '$total_tokens'},
                'total_input_cost_usd': {'$sum': '$pricing.input_cost_usd'},
                'total_output_cost_usd': {'$sum': '$pricing.output_cost_usd'},
                'total_cost_usd': {'$sum': '$pricing.total_cost_usd'},
                'record_count': {'$sum': 1},
                'screen_count': {'$addToSet': '$screen_id'}
            }
        },
        {
            '$sort': {'_id': 1}
        },
        {
            '$project': {
                '_id': 0,
                'project_id': '$_id',
                'total_prompt_tokens': 1,
                'total_completion_tokens': 1,
                'total_tokens': 1,
                'total_input_cost_usd': {'$round': ['$total_input_cost_usd', 6]},
                'total_output_cost_usd': {'$round': ['$total_output_cost_usd', 6]},
                'total_cost_usd': {'$round': ['$total_cost_usd', 6]},
                'record_count': 1,
                'unique_screen_count': {'$size': '$screen_count'}
            }
        }
    ]
    
    results = list(collection.aggregate(pipeline))
    return results


def main():
    """
    Example usage of the aggregation functions
    """
    # Connect to MongoDB
    connection_string = 'mongodb://localhost:27017'
    client = MongoClient(connection_string)
    
    try:
        # Connect to your database
        db = client['aiqod-dev']
        
        print("=" * 80)
        print("TOKEN AND COST ANALYSIS REPORT")
        print("=" * 80)

        # 1. Get totals for specific project/screen
        print("\n2. SPECIFIC PROJECT/SCREEN TOTALS:")
        print("-" * 80)
        specific_result = calculate_token_costs_for_specific_screen(
            db,
            'screen_1770639344472',
            'screen_1770639344472'
        )
        if specific_result:
            print(json.dumps(specific_result, indent=2))
        else:
            print("No records found for specified project/screen")

        # # 2. Get totals by project and screen
        # print("\n1. TOTALS BY PROJECT AND SCREEN:")
        # print("-" * 80)
        # results = calculate_token_costs_by_project_and_screen(db)
        # for result in results:
        #     print(json.dumps(result, indent=2))
        #     print("-" * 40)
        

        
        # # 4. Get grand totals
        # print("\n4. GRAND TOTALS:")
        # print("-" * 80)
        # grand_totals = calculate_grand_totals(db)
        # if grand_totals:
        #     print(json.dumps(grand_totals, indent=2))
        # else:
        #     print("No records found")
        
        # print("\n" + "=" * 80)
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        client.close()


if __name__ == "__main__":
    main()