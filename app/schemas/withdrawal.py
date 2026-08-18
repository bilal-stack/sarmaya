from pydantic import BaseModel, field_validator

MIN_REASON = 10


class WithdrawRequest(BaseModel):
    """Why a record is being withdrawn.

    Sent as a body on DELETE rather than a query parameter: a reason is free
    text a person writes, and free text does not belong in a URL, where it is
    logged by every proxy between here and the server.

    A minimum length rather than merely "not blank", because "x" satisfies
    "required" and explains nothing — and the explanation is the entire point
    of keeping the record.
    """
    reason: str

    @field_validator("reason")
    @classmethod
    def reason_is_meaningful(cls, v: str) -> str:
        cleaned = (v or "").strip()
        if len(cleaned) < MIN_REASON:
            raise ValueError(
                f"Give a reason of at least {MIN_REASON} characters. It is the "
                "only explanation anyone reading the trail later will have."
            )
        return cleaned
