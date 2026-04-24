from __future__ import annotations

import os
import json
import asyncio
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel
from zoneinfo import ZoneInfo

from sqlalchemy import String, Integer, Boolean, DateTime, Text, select, func
from sqlalchemy.orm import Mapped, mapped_column, DeclarativeBase
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

load_dotenv()

# ----------------- CONFIG -----------------

SNAPSHOT_TZ_NAME = os.getenv("SNAPSHOT_TZ_NAME", "Europe/Moscow")
DB_URL = os.getenv("DB_URL", "sqlite+aiosqlite:///./data.db")
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "5"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "20"))
RETRIES = int(os.getenv("RETRIES", "3"))

EXTERNAL_STATS_API_TEMPLATE = os.getenv(
    "EXTERNAL_STATS_API_TEMPLATE",
    "https://example.com/statsapi/?username={username}&token={token}",
)

UA = {"User-Agent": "balance-snapshot-api/1.0 (+httpx; linux)"}


def get_proxy_string() -> Optional[str]:
    return (
        os.getenv("http_proxy")
        or os.getenv("HTTP_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("HTTPS_PROXY")
    )


# ----------------- DATABASE MODELS -----------------

class Base(DeclarativeBase):
    pass


class ModelAccount(Base):
    __tablename__ = "model_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    token: Mapped[str] = mapped_column(String(256))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(128), index=True)
    token_balance: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    raw_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="success")
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tz_name: Mapped[str] = mapped_column(String(64), default=SNAPSHOT_TZ_NAME)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


engine = create_async_engine(DB_URL, echo=False, future=True)
AsyncSessionMaker = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ----------------- SCHEMAS -----------------

class ModelIn(BaseModel):
    username: str
    token: str
    is_active: bool = True


class ModelOut(BaseModel):
    id: int
    username: str
    is_active: bool
    created_at: datetime


class SnapshotOut(BaseModel):
    id: int
    username: str
    token_balance: Optional[int]
    status: str
    error_message: Optional[str]
    captured_at: datetime
    tz_name: str


class SyncReq(BaseModel):
    models: List[ModelIn] = []


# ----------------- APP -----------------

app = FastAPI(
    title="Balance Snapshot API",
    version="1.0",
    description="API service for collecting, storing and comparing account balance snapshots.",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_minute_window(day_str: str, hhmm: str, tz_name: str) -> tuple[datetime, datetime]:
    """
    Returns a semi-open UTC window for one local minute:
    [day HH:MM:00; day HH:MM+1:00)
    """
    tz = ZoneInfo(tz_name)

    try:
        hh, mm = hhmm.split(":")
        hh = int(hh)
        mm = int(mm)

        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail=f"Bad time format: {hhmm}. Expected HH:MM.")

    start_local = datetime.fromisoformat(f"{day_str}T{hh:02d}:{mm:02d}:00").replace(tzinfo=tz)

    if mm == 59:
        end_local = start_local.replace(hour=(hh + 1) % 24, minute=0)
    else:
        end_local = start_local.replace(minute=mm + 1)

    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


async def create_http_client() -> httpx.AsyncClient:
    proxy = get_proxy_string()

    return httpx.AsyncClient(
        http2=False,
        headers=UA,
        timeout=REQUEST_TIMEOUT,
        trust_env=True,
        proxies=proxy if proxy else None,
    )


async def fetch_once(username: str, token: str) -> dict:
    url = EXTERNAL_STATS_API_TEMPLATE.format(username=username, token=token)
    last_exc: Optional[Exception] = None

    async with await create_http_client() as client:
        for attempt in range(RETRIES + 1):
            try:
                response = await client.get(url)

                content_type = response.headers.get("content-type", "")
                body_preview = response.text[:1000]

                if response.status_code != 200:
                    raise RuntimeError(
                        f"HTTP {response.status_code}; content_type={content_type}; body={body_preview}"
                    )

                if "application/json" not in content_type.lower():
                    raise RuntimeError(
                        f"Non-JSON response; content_type={content_type}; body={body_preview}"
                    )

                data = response.json()

                if not isinstance(data, dict):
                    raise ValueError(f"Unexpected JSON: {data}")

                return {
                    "status": "success",
                    "data": data,
                    "raw_text": response.text,
                }

            except Exception as exc:
                last_exc = exc

                if attempt < RETRIES:
                    await asyncio.sleep(0.6 * (attempt + 1))
                else:
                    return {
                        "status": "error",
                        "error": (
                            f"{type(last_exc).__name__}: {last_exc}; "
                            f"repr={repr(last_exc)}; "
                            f"args={getattr(last_exc, 'args', None)}"
                        ),
                    }


async def run_snapshot(tz_name: Optional[str] = None) -> dict:
    tz = ZoneInfo(tz_name or SNAPSHOT_TZ_NAME)
    captured_at = datetime.now(tz).astimezone(timezone.utc)

    async with AsyncSessionMaker() as session:
        accounts = list(
            (
                await session.execute(
                    select(ModelAccount).where(ModelAccount.is_active == True)
                )
            ).scalars()
        )

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    results: Dict[str, dict] = {}

    async def job(account: ModelAccount) -> None:
        async with semaphore:
            result = await fetch_once(account.username, account.token)

            async with AsyncSessionMaker() as session:
                if result["status"] == "success":
                    data = result["data"]
                    balance = data.get("token_balance")
                    balance = int(balance) if balance is not None else None

                    snapshot = BalanceSnapshot(
                        username=data.get("username", account.username),
                        token_balance=balance,
                        raw_json=result.get("raw_text") or json.dumps(data, ensure_ascii=False),
                        status="success",
                        error_message=None,
                        tz_name=tz.key,
                        captured_at=captured_at,
                    )
                else:
                    snapshot = BalanceSnapshot(
                        username=account.username,
                        token_balance=None,
                        raw_json=None,
                        status="error",
                        error_message=result.get("error") or "",
                        tz_name=tz.key,
                        captured_at=captured_at,
                    )

                session.add(snapshot)
                await session.commit()

            results[account.username] = result

    await asyncio.gather(*[job(account) for account in accounts])

    return {
        "captured_at_utc": captured_at.isoformat(),
        "tz_name": tz.key,
        "results": results,
    }


@app.on_event("startup")
async def on_startup() -> None:
    await init_db()


# ----------------- ROUTES -----------------

@app.get("/health")
async def health():
    async with AsyncSessionMaker() as session:
        total = (await session.execute(select(func.count(ModelAccount.id)))).scalar() or 0

    return {
        "status": "ok",
        "models_in_db": total,
        "proxy_configured": bool(get_proxy_string()),
        "timeout": REQUEST_TIMEOUT,
        "retries": RETRIES,
    }


@app.get("/models", response_model=List[ModelOut])
async def list_models():
    async with AsyncSessionMaker() as session:
        rows = list(
            (
                await session.execute(
                    select(ModelAccount).order_by(ModelAccount.id)
                )
            ).scalars()
        )

    return [
        ModelOut(
            id=row.id,
            username=row.username,
            is_active=row.is_active,
            created_at=row.created_at,
        )
        for row in rows
    ]


@app.post("/models", response_model=ModelOut)
async def add_model(model: ModelIn):
    username = model.username.strip().lower()
    token = model.token.strip()

    async with AsyncSessionMaker() as session:
        existing = (
            await session.execute(
                select(ModelAccount).where(func.lower(ModelAccount.username) == username)
            )
        ).scalar_one_or_none()

        if existing:
            existing.token = token
            existing.is_active = model.is_active
            await session.commit()
            await session.refresh(existing)

            return ModelOut(
                id=existing.id,
                username=existing.username,
                is_active=existing.is_active,
                created_at=existing.created_at,
            )

        row = ModelAccount(username=username, token=token, is_active=model.is_active)

        session.add(row)
        await session.commit()
        await session.refresh(row)

        return ModelOut(
            id=row.id,
            username=row.username,
            is_active=row.is_active,
            created_at=row.created_at,
        )


@app.delete("/models/{username}")
async def delete_model(username: str):
    async with AsyncSessionMaker() as session:
        row = (
            await session.execute(
                select(ModelAccount).where(func.lower(ModelAccount.username) == username.lower())
            )
        ).scalar_one_or_none()

        if not row:
            raise HTTPException(status_code=404, detail="Model not found")

        await session.delete(row)
        await session.commit()

    return {"ok": True}


@app.post("/models/{username}/deactivate")
async def deactivate_model(username: str):
    username = username.strip().lower()

    async with AsyncSessionMaker() as session:
        row = (
            await session.execute(
                select(ModelAccount).where(func.lower(ModelAccount.username) == username)
            )
        ).scalar_one_or_none()

        if not row:
            raise HTTPException(status_code=404, detail="Model not found")

        row.is_active = False
        await session.commit()

    return {"ok": True, "username": username, "is_active": False}


@app.post("/models/{username}/activate")
async def activate_model(username: str):
    username = username.strip().lower()

    async with AsyncSessionMaker() as session:
        row = (
            await session.execute(
                select(ModelAccount).where(func.lower(ModelAccount.username) == username)
            )
        ).scalar_one_or_none()

        if not row:
            raise HTTPException(status_code=404, detail="Model not found")

        row.is_active = True
        await session.commit()

    return {"ok": True, "username": username, "is_active": True}


@app.post("/models/sync")
async def models_sync(
    request: Request,
    req: SyncReq,
    deactivate_missing: bool = Query(False),
):
    need_key = os.getenv("CRM_SYNC_KEY", "")
    got_key = request.headers.get("X-CRM-Key", "")

    if need_key and got_key != need_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

    normalized_models = []

    for model in req.models:
        username = model.username.strip().lower()
        token = model.token.strip()

        if username and token:
            normalized_models.append(
                ModelIn(username=username, token=token, is_active=model.is_active)
            )

    usernames = [model.username for model in normalized_models]

    async with AsyncSessionMaker() as session:
        for model in normalized_models:
            existing = (
                await session.execute(
                    select(ModelAccount).where(func.lower(ModelAccount.username) == model.username)
                )
            ).scalar_one_or_none()

            if existing:
                existing.token = model.token
                existing.is_active = model.is_active
            else:
                session.add(
                    ModelAccount(
                        username=model.username,
                        token=model.token,
                        is_active=model.is_active,
                    )
                )

        deactivated = 0

        if deactivate_missing and usernames:
            rows = list(
                (
                    await session.execute(
                        select(ModelAccount).where(ModelAccount.username.not_in(usernames))
                    )
                ).scalars()
            )

            for row in rows:
                if row.is_active:
                    row.is_active = False
                    deactivated += 1

        await session.commit()

    return {
        "ok": True,
        "received": len(normalized_models),
        "deactivated": deactivated,
    }


@app.post("/snapshot/run")
async def api_run_snapshot(tz_name: Optional[str] = None):
    return await run_snapshot(tz_name)


@app.get("/snapshots", response_model=List[SnapshotOut])
async def list_snapshots(
    username: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
):
    async with AsyncSessionMaker() as session:
        statement = select(BalanceSnapshot)

        if username:
            statement = statement.where(func.lower(BalanceSnapshot.username) == username.lower())

        statement = statement.order_by(BalanceSnapshot.captured_at.desc()).limit(limit)

        rows = list((await session.execute(statement)).scalars())

    return [
        SnapshotOut(
            id=row.id,
            username=row.username,
            token_balance=row.token_balance,
            status=row.status,
            error_message=row.error_message,
            captured_at=row.captured_at,
            tz_name=row.tz_name,
        )
        for row in rows
    ]


@app.get("/balance/compare")
async def balance_compare(username: str):
    async with AsyncSessionMaker() as session:
        last_snapshot = (
            await session.execute(
                select(BalanceSnapshot)
                .where(func.lower(BalanceSnapshot.username) == username.lower())
                .order_by(BalanceSnapshot.captured_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        account = (
            await session.execute(
                select(ModelAccount).where(func.lower(ModelAccount.username) == username.lower())
            )
        ).scalar_one_or_none()

    if last_snapshot:
        snapshot_part = {
            "status": last_snapshot.status,
            "token_balance": last_snapshot.token_balance,
            "captured_at": last_snapshot.captured_at.isoformat(),
            "tz_name": last_snapshot.tz_name,
            "error": last_snapshot.error_message or "",
        }
    else:
        snapshot_part = {
            "status": "not-found",
            "token_balance": None,
            "captured_at": None,
            "tz_name": SNAPSHOT_TZ_NAME,
            "error": "",
        }

    if not account:
        current_part = {
            "status": "error",
            "token_balance": None,
            "error": "Model not found",
            "fetched_at_utc": utc_now().isoformat(),
        }
    else:
        result = await fetch_once(account.username, account.token)

        if result["status"] == "success":
            balance = result["data"].get("token_balance")
            balance = int(balance) if balance is not None else None

            current_part = {
                "status": "success",
                "token_balance": balance,
                "fetched_at_utc": utc_now().isoformat(),
            }
        else:
            current_part = {
                "status": "error",
                "token_balance": None,
                "error": result.get("error") or "",
                "fetched_at_utc": utc_now().isoformat(),
            }

    return {
        "username": username,
        "snapshot": snapshot_part,
        "current": current_part,
    }


@app.get("/balance/compare_times")
async def balance_compare_times(
    username: str,
    day: Optional[str] = None,
    times: str = Query("07:29,07:55", description="Comma-separated HH:MM list"),
    tz_name: str = SNAPSHOT_TZ_NAME,
):
    if not day:
        day = datetime.now(ZoneInfo(tz_name)).date().isoformat()

    async with AsyncSessionMaker() as session:
        account = (
            await session.execute(
                select(ModelAccount).where(func.lower(ModelAccount.username) == username.lower())
            )
        ).scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Model not found")

    current_result = await fetch_once(account.username, account.token)

    if current_result["status"] == "success":
        current_balance = current_result["data"].get("token_balance")
        current_balance = int(current_balance) if current_balance is not None else None

        current = {
            "status": "success",
            "token_balance": current_balance,
            "fetched_at_utc": utc_now().isoformat(),
        }
    else:
        current = {
            "status": "error",
            "token_balance": None,
            "error": current_result.get("error") or "",
            "fetched_at_utc": utc_now().isoformat(),
        }

    requested_times = [item.strip() for item in times.split(",") if item.strip()]
    output_times: Dict[str, Any] = {}

    async with AsyncSessionMaker() as session:
        for hhmm in requested_times:
            start_utc, end_utc = local_minute_window(day, hhmm, tz_name)

            snapshot = (
                await session.execute(
                    select(BalanceSnapshot)
                    .where(func.lower(BalanceSnapshot.username) == username.lower())
                    .where(BalanceSnapshot.captured_at >= start_utc)
                    .where(BalanceSnapshot.captured_at < end_utc)
                    .where(BalanceSnapshot.status == "success")
                    .order_by(BalanceSnapshot.captured_at.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            if snapshot:
                output_times[hhmm] = {
                    "status": "success",
                    "token_balance": snapshot.token_balance,
                    "captured_at": snapshot.captured_at.isoformat(),
                    "tz_name": snapshot.tz_name,
                }
            else:
                output_times[hhmm] = {
                    "status": "not-found",
                    "token_balance": None,
                    "captured_at": None,
                    "tz_name": tz_name,
                }

    return {
        "username": username,
        "day": day,
        "tz_name": tz_name,
        "current": current,
        "times": output_times,
    }


@app.get("/balance/compare0729_0755")
async def balance_compare_0729_0755(
    username: str = Query(...),
    day: Optional[str] = Query(None),
    tz_name: str = Query(SNAPSHOT_TZ_NAME),
):
    return await balance_compare_times(
        username=username,
        day=day,
        times="07:29,07:55",
        tz_name=tz_name,
    )
