from datetime import datetime, date, timezone, timedelta
from typing import Optional, Union, Any


def utc_now() -> datetime:
    """
    Get current UTC datetime (timezone-aware)
    Recommended for all new code
    """
    return datetime.now(timezone.utc)


def utc_now_naive() -> datetime:
    """
    Get current UTC datetime (timezone-naive)
    Use only if database doesn't support timezone-aware datetimes
    """
    return datetime.utcnow()


def to_utc(dt: datetime) -> datetime:
    """
    Convert datetime to UTC timezone
    Handles both naive and aware datetimes
    """
    if dt is None:
        return utc_now()
    
    if dt.tzinfo is None:
        # Assume it's UTC if naive
        return dt.replace(tzinfo=timezone.utc)
    
    return dt.astimezone(timezone.utc)


def make_aware(dt: datetime, tz: timezone = timezone.utc) -> datetime:
    """
    Make naive datetime timezone-aware
    Default: UTC
    """
    if dt is None:
        return utc_now()
    
    if dt.tzinfo is not None:
        return dt  # Already aware
    
    return dt.replace(tzinfo=tz)


def make_naive(dt: datetime) -> datetime:
    """
    Convert timezone-aware datetime to naive (strips timezone info)
    """
    if dt is None:
        return utc_now_naive()
    
    if dt.tzinfo is None:
        return dt  # Already naive
    
    return dt.replace(tzinfo=None)


def parse_date(date_input: Optional[Union[str, date]]) -> Optional[date]:
    """
    Parse date string to date object.
    Supports multiple formats.
    If input is already a date object, returns as is.
    """
    if not date_input:
        return None

    # If already a date, just return
    if isinstance(date_input, date):
        return date_input

    # Otherwise expect a string
    date_str = str(date_input).strip()

    formats = [
        "%Y-%m-%d",           # 2024-01-15
        "%d/%m/%Y",           # 15/01/2024
        "%m/%d/%Y",           # 01/15/2024
        "%d-%m-%Y",           # 15-01-2024
        "%m-%d-%Y",           # 01-15-2024
        "%Y/%m/%d",           # 2024/01/15
        "%B %d, %Y",          # January 15, 2024
        "%d %B %Y",           # 15 January 2024
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue

    return None


def parse_datetime(dt_str: str) -> Optional[datetime]:
    """
    Parse datetime string to datetime object
    Returns timezone-aware datetime in UTC
    """
    if not dt_str:
        return None
    
    formats = [
        "%Y-%m-%dT%H:%M:%S.%fZ",      # ISO with microseconds
        "%Y-%m-%dT%H:%M:%SZ",          # ISO without microseconds
        "%Y-%m-%d %H:%M:%S",           # SQL format
        "%d/%m/%Y %H:%M:%S",           # DD/MM/YYYY HH:MM:SS
        "%m/%d/%Y %H:%M:%S",           # MM/DD/YYYY HH:MM:SS
    ]
    
    for fmt in formats:
        try:
            dt = datetime.strptime(dt_str.strip(), fmt)
            return make_aware(dt)
        except ValueError:
            continue
    
    # Try ISO format with timezone
    try:
        return datetime.fromisoformat(dt_str.strip())
    except ValueError:
        pass
    
    return None


def format_datetime(dt: Optional[datetime], fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format datetime to string"""
    if dt is None:
        return ""
    return dt.strftime(fmt)


def format_datetime_iso(dt: Optional[datetime]) -> str:
    """Format datetime to ISO 8601 format"""
    if dt is None:
        return ""
    return dt.isoformat()


def format_date(d: Optional[date], fmt: str = "%Y-%m-%d") -> str:
    """Format date to string"""
    if d is None:
        return ""
    return d.strftime(fmt)


def date_range(start: date, end: date) -> list[date]:
    """
    Generate list of dates between start and end (inclusive)
    """
    if start > end:
        start, end = end, start
    
    days = (end - start).days + 1
    return [start + timedelta(days=i) for i in range(days)]


def start_of_month(d: Optional[date] = None) -> date:
    """Get first day of month"""
    if d is None:
        d = date.today()
    return date(d.year, d.month, 1)


def end_of_month(d: Optional[date] = None) -> date:
    """Get last day of month"""
    if d is None:
        d = date.today()
    
    # Go to next month, then subtract one day
    if d.month == 12:
        next_month = date(d.year + 1, 1, 1)
    else:
        next_month = date(d.year, d.month + 1, 1)
    
    return next_month - timedelta(days=1)


def days_between(start: date, end: date) -> int:
    """Calculate days between two dates"""
    return abs((end - start).days)


def is_within_days(d: date, target: date, days: int) -> bool:
    """Check if date is within N days of target date"""
    return abs((d - target).days) <= days


def add_business_days(d: date, days: int) -> date:
    """
    Add business days (Mon-Fri) to date
    Skips weekends
    """
    current = d
    remaining = abs(days)
    direction = 1 if days > 0 else -1
    
    while remaining > 0:
        current += timedelta(days=direction)
        # Monday = 0, Sunday = 6
        if current.weekday() < 5:  # Mon-Fri
            remaining -= 1
    
    return current


def sanitize_for_json(data: Any) -> Any:
    """
    Convert Python date/datetime objects to JSON-safe format
    Recursively processes dicts and lists
    """
    if data is None:
        return None
    
    if isinstance(data, (datetime, date)):
        return data.isoformat()
    
    if isinstance(data, dict):
        return {k: sanitize_for_json(v) for k, v in data.items()}
    
    if isinstance(data, list):
        return [sanitize_for_json(item) for item in data]
    
    return data
