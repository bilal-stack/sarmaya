from sqlalchemy import Column, String, Integer, Float, ForeignKey
from sqlalchemy.orm import relationship
from app.models.base import BaseModel, TimestampMixin

class PurchaseOrder(BaseModel, TimestampMixin):
    __tablename__ = "purchase_orders"
    reference = Column(String, nullable=False)
    vendor_id = Column(Integer, ForeignKey("vendors.id"))
    vendor = relationship("Vendor")
    total = Column(Float, default=0.0)
