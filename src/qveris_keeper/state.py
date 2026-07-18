from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable


@dataclass(frozen=True, slots=True)
class AccountStatus:
    account_id: str
    state: str = "starting"
    reason: str = "startup"
    verify_http_status: int = 0
    userinfo_http_status: int = 0
    last_probe_at: float | None = None
    last_authenticated_at: float | None = None
    last_login_at: float | None = None
    last_touch_at: float | None = None
    last_touch_local_date: str | None = None
    next_action_at: float = 0.0
    failure_count: int = 0
    updated_at: float = 0.0

    def evolve(self, **changes: object) -> AccountStatus:
        return replace(self, **changes)


class KeeperStateStore:
    def __init__(
        self,
        path: str,
        *,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._wall_time = wall_time
        self._lock = asyncio.Lock()
        self._closed = False
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=5000")
        if path != ":memory:":
            self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS keeper_account_status (
                account_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                reason TEXT NOT NULL,
                verify_http_status INTEGER NOT NULL,
                userinfo_http_status INTEGER NOT NULL,
                last_probe_at REAL,
                last_authenticated_at REAL,
                last_login_at REAL,
                last_touch_at REAL,
                last_touch_local_date TEXT,
                next_action_at REAL NOT NULL,
                failure_count INTEGER NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        self._connection.commit()

    async def load(self, account_ids: set[str]) -> dict[str, AccountStatus]:
        if not account_ids:
            return {}
        placeholders = ",".join("?" for _ in account_ids)
        async with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM keeper_account_status WHERE account_id IN ({placeholders})",
                tuple(sorted(account_ids)),
            ).fetchall()
        return {
            str(row["account_id"]): AccountStatus(
                account_id=str(row["account_id"]),
                state=str(row["state"]),
                reason=str(row["reason"]),
                verify_http_status=int(row["verify_http_status"]),
                userinfo_http_status=int(row["userinfo_http_status"]),
                last_probe_at=row["last_probe_at"],
                last_authenticated_at=row["last_authenticated_at"],
                last_login_at=row["last_login_at"],
                last_touch_at=row["last_touch_at"],
                last_touch_local_date=row["last_touch_local_date"],
                next_action_at=float(row["next_action_at"]),
                failure_count=int(row["failure_count"]),
                updated_at=float(row["updated_at"]),
            )
            for row in rows
        }

    async def save(self, status: AccountStatus) -> AccountStatus:
        if status.updated_at <= 0:
            status = status.evolve(updated_at=self._wall_time())
        async with self._lock:
            self._connection.execute(
                """
                INSERT INTO keeper_account_status(
                    account_id, state, reason, verify_http_status,
                    userinfo_http_status, last_probe_at, last_authenticated_at,
                    last_login_at, last_touch_at, last_touch_local_date,
                    next_action_at, failure_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    state = excluded.state,
                    reason = excluded.reason,
                    verify_http_status = excluded.verify_http_status,
                    userinfo_http_status = excluded.userinfo_http_status,
                    last_probe_at = excluded.last_probe_at,
                    last_authenticated_at = excluded.last_authenticated_at,
                    last_login_at = excluded.last_login_at,
                    last_touch_at = excluded.last_touch_at,
                    last_touch_local_date = excluded.last_touch_local_date,
                    next_action_at = excluded.next_action_at,
                    failure_count = excluded.failure_count,
                    updated_at = excluded.updated_at
                """,
                (
                    status.account_id,
                    status.state,
                    status.reason,
                    status.verify_http_status,
                    status.userinfo_http_status,
                    status.last_probe_at,
                    status.last_authenticated_at,
                    status.last_login_at,
                    status.last_touch_at,
                    status.last_touch_local_date,
                    status.next_action_at,
                    status.failure_count,
                    status.updated_at,
                ),
            )
            self._connection.commit()
        return status

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()
