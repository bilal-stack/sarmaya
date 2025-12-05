from pydantic import BaseModel

class Ping(BaseModel):
    pong: bool = True
