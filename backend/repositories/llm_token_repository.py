"""
LLM Token Repository - Database operations for LLM token tracking
"""
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime
from bson import ObjectId

from services.database import Database

logger = logging.getLogger(__name__)


class LLMTokenRepository:
    """Repository for LLM token usage records"""
    
    def __init__(self):
        self.collection_name = "llm_token_count"
        self.db = Database()
    
    async def insert_record(self, record: Dict[str, Any]) -> str:
        """
        Insert a new LLM token usage record
        
        Args:
            record: Dictionary containing all fields from the schema
            
        Returns:
            str: The inserted record ID
        """
        try:
            # Add timestamp if not provided
            if "started_at" not in record:
                record["started_at"] = datetime.utcnow().isoformat() + "Z"
            
            result = await self.db.insert_one(self.collection_name, record)
            logger.info(f"Inserted LLM token record: {result.get('_id')}")
            return str(result.get("_id"))
        except Exception as e:
            logger.error(f"Failed to insert LLM token record: {e}")
            raise
    
    async def update_record(self, record_id: str, updates: Dict[str, Any]) -> bool:
        """
        Update an existing LLM token record
        
        Args:
            record_id: The record ID to update
            updates: Dictionary of fields to update
            
        Returns:
            bool: True if update was successful
        """
        try:
            result = await self.db.update_one(
                self.collection_name,
                {"_id": ObjectId(record_id)},
                {"$set": updates}
            )
            return result.get("modified_count", 0) > 0
        except Exception as e:
            logger.error(f"Failed to update LLM token record {record_id}: {e}")
            return False
    
    async def get_records_by_project(
        self, 
        project_id: str, 
        screen_id: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get all LLM token records for a project/screen"""
        try:
            filters = {"project_id": project_id}
            if screen_id:
                filters["screen_id"] = screen_id
            
            records = await self.db.find(
                self.collection_name,
                filters=filters,
                limit=limit
            )
            return records
        except Exception as e:
            logger.error(f"Failed to get LLM token records: {e}")
            return []
    
    async def get_total_cost(
        self, 
        project_id: str, 
        screen_id: Optional[str] = None
    ) -> float:
        """Calculate total cost for a project/screen"""
        try:
            records = await self.get_records_by_project(project_id, screen_id, limit=10000)
            total_cost = sum(
                record.get("pricing", {}).get("cost_usd", 0) 
                for record in records
            )
            return total_cost
        except Exception as e:
            logger.error(f"Failed to calculate total cost: {e}")
            return 0.0
