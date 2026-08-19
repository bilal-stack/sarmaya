from datetime import timedelta
from typing import Optional, Dict
from jose import JWTError, jwt
from passlib.context import CryptContext
import logging

from app.core.config import settings
from app.utils.datetime_helpers import utc_now

logger = logging.getLogger(__name__)

# Use pbkdf2_sha256 to avoid bcrypt backend issues and 72-byte limit
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against a hash. Returns False on verification errors (and logs them)."""
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except Exception as e:
        # Log the error for debugging (backend/bcrypt issues, length errors, etc.)
        logger.exception("Password verification failed: %s", e)
        return False


def get_password_hash(password: str) -> str:
    """Generate password hash"""
    return pwd_context.hash(password)


#: What a token is allowed to do. Absent means an ordinary access token, which
#: is what every token minted before multi-factor auth existed carries — so the
#: check that reads this treats absence as access and only ever *adds*
#: restrictions.
PURPOSE_ACCESS = "access"
#: Can do exactly one thing: be exchanged for a real token by somebody holding
#: the second factor. If this were an ordinary token, MFA would be a screen
#: rather than a control — the password alone would already have got you in.
PURPOSE_MFA_CHALLENGE = "mfa_challenge"

#: A challenge is for finishing a sign-in that is already underway. Long enough
#: to open an authenticator app, short enough that one left in a log or a
#: browser history is not a standing invitation.
MFA_CHALLENGE_MINUTES = 5


def create_mfa_challenge_token(user_id: str, tenant_id: str) -> str:
    """A token that proves the password step passed, and nothing else.

    Deliberately carries no role and no token_version: it is not an identity,
    it is a receipt for one half of a login.
    """
    return create_access_token(
        {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "purpose": PURPOSE_MFA_CHALLENGE,
        },
        expires_delta=timedelta(minutes=MFA_CHALLENGE_MINUTES),
    )


def create_access_token(data: Dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    Create JWT access token
    
    Args:
        data: Payload to encode (must include 'sub', 'tenant_id', 'email', 'role')
        expires_delta: Optional expiration time
    
    Returns:
        Encoded JWT token string
    """
    to_encode = data.copy()
    
    if expires_delta:
        expire = utc_now() + expires_delta
    else:
        expire = utc_now() + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )
    
    to_encode.update({"exp": expire})
    
    encoded_jwt = jwt.encode(
        to_encode, 
        settings.SECRET_KEY, 
        algorithm=settings.ALGORITHM
    )
    
    return encoded_jwt


def decode_access_token(token: str) -> Optional[Dict]:
    """
    Decode and validate JWT access token
    
    Args:
        token: JWT token string
    
    Returns:
        Decoded token payload (dict) or None if invalid
    """
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM]
        )
        return payload
    except JWTError as e:
        logger.warning(f"Invalid token: {e}")
        return None