"""
Money and currency utilities
"""
from decimal import Decimal
from typing import Union


def money_to_float(v: Union[Decimal, float, int, None]) -> float:
    """
    Convert Decimal/any numeric type to float
    
    Args:
        v: Value to convert (Decimal, float, int, or None)
    
    Returns:
        Float value (0.0 if None or conversion fails)
    
    Examples:
        >>> money_to_float(Decimal("100.50"))
        100.5
        >>> money_to_float(None)
        0.0
        >>> money_to_float(100)
        100.0
    """
    if v is None:
        return 0.0
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(v)
    except Exception:
        return 0.0


def format_currency(amount: float, currency: str = "PKR") -> str:
    """
    Format amount as currency string
    
    Args:
        amount: Numeric amount
        currency: Currency code (default: PKR)
    
    Returns:
        Formatted string (e.g., "PKR 1,234.56")
    
    Examples:
        >>> format_currency(1234.56)
        'PKR 1,234.56'
        >>> format_currency(1234.56, "USD")
        'USD 1,234.56'
    """
    return f"{currency} {amount:,.2f}"
