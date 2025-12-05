from sqlalchemy import Column, String
from app.models.base import Base, TimestampMixin

class Workflow(Base, TimestampMixin):
    __tablename__ = "workflows"
    name = Column(String, nullable=False)
    definition = Column(String, nullable=True)
