import asyncio

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.auth import authenticate_user, create_access_token, login_locked_for, record_login_failure

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/token", response_model=TokenResponse)
async def login(body: LoginRequest):
    locked = login_locked_for()
    if locked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed sign-ins — try again in {max(1, locked // 60)} min",
            headers={"Retry-After": str(locked)},
        )
    username = authenticate_user(body.username, body.password)
    if not username:
        record_login_failure()
        await asyncio.sleep(0.5)         # a little friction per wrong guess
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )
    token = create_access_token(username)
    return TokenResponse(access_token=token)
