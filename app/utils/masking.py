"""Partial masking for account identifiers.

Enough of the value survives to confirm you are looking at the right account —
which is what a reviewer comparing a payment against a remittance advice needs
— without the response carrying the credential itself.
"""
from typing import Optional

VISIBLE_SUFFIX = 4
MASK_CHAR = "•"  # bullet


def mask_account(value: Optional[str]) -> Optional[str]:
    """`PK36SCBL0000001123456702` -> `••••6702`.

    Short values are masked entirely rather than partially: revealing the last
    four of a six-character value gives away most of it.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return stripped
    if len(stripped) <= VISIBLE_SUFFIX + 2:
        return MASK_CHAR * len(stripped)
    return MASK_CHAR * VISIBLE_SUFFIX + stripped[-VISIBLE_SUFFIX:]
