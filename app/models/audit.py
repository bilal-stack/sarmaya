from sqlalchemy import Column, String, Integer
from app.models.base import Base, TimestampMixin

class Audit(Base, TimestampMixin):
    __tablename__ = "audits"
    action = Column(String, nullable=False)
    user_id = Column(Integer, nullable=True)
    details = Column(String, nullable=True)
