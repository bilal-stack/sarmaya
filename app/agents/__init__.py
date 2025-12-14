"""Agent-based AI system with SQL tools"""

from app.agents.duplicate_agent import DuplicateDetectionAgent
from app.agents.query_agent import QueryAgent

__all__ = [
    "DuplicateDetectionAgent",
    "QueryAgent"
]
