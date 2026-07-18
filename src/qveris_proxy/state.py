from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class StoredCooldown:
    scope: str
    account_id: str
    name: str
    until_epoch: float
    failure_count: int = 0
    retain_after_expiry: bool = False
    delete: bool = False
    clears: tuple[tuple[str, str], ...] = ()


class StateStore:
    def __init__(
        self,
        path: str,
        *,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._wall_time = wall_time
        self._lock = asyncio.Lock()
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._initialize()

    def _initialize(self) -> None:
        if self._path != ":memory:":
            self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS affinities (
                affinity_hash TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                expires_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_affinities_expires
                ON affinities(expires_at);

            CREATE TABLE IF NOT EXISTS cooldowns (
                scope TEXT NOT NULL,
                account_id TEXT NOT NULL,
                name TEXT NOT NULL,
                until_epoch REAL NOT NULL,
                failure_count INTEGER NOT NULL DEFAULT 0,
                retain_after_expiry INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(scope, account_id, name)
            );

            CREATE TABLE IF NOT EXISTS quota_snapshots (
                account_id TEXT PRIMARY KEY,
                http_status INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                checked_at REAL NOT NULL,
                success_at REAL,
                attempt_valid INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS admin_browser_claims (
                claim_key TEXT PRIMARY KEY,
                claimed_at REAL NOT NULL
            );
            """
        )
        cooldown_columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(cooldowns)")
        }
        if "failure_count" not in cooldown_columns:
            self._connection.execute(
                "ALTER TABLE cooldowns ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0"
            )
        if "retain_after_expiry" not in cooldown_columns:
            self._connection.execute(
                "ALTER TABLE cooldowns ADD COLUMN "
                "retain_after_expiry INTEGER NOT NULL DEFAULT 0"
            )
        quota_columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(quota_snapshots)")
        }
        if "success_at" not in quota_columns:
            self._connection.execute(
                "ALTER TABLE quota_snapshots ADD COLUMN success_at REAL"
            )
            self._connection.execute(
                "UPDATE quota_snapshots SET success_at = checked_at "
                "WHERE http_status = 200"
            )
        if "attempt_valid" not in quota_columns:
            self._connection.execute(
                "ALTER TABLE quota_snapshots ADD COLUMN "
                "attempt_valid INTEGER NOT NULL DEFAULT 0"
            )
            self._connection.execute(
                "UPDATE quota_snapshots SET attempt_valid = 1 "
                "WHERE http_status = 200 AND success_at IS NOT NULL"
            )
        self._connection.commit()

    @staticmethod
    def _hash_affinity(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    async def claim_admin_browser(self, claim_key: str) -> bool:
        if len(claim_key) != 64 or any(
            character not in "0123456789abcdef" for character in claim_key
        ):
            raise ValueError("invalid admin browser claim key")
        async with self._lock:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO admin_browser_claims(claim_key, claimed_at) "
                "VALUES (?, ?)",
                (claim_key, self._wall_time()),
            )
            self._connection.commit()
            return cursor.rowcount == 1

    async def get_affinity(self, value: str) -> str | None:
        key = self._hash_affinity(value)
        now = self._wall_time()
        async with self._lock:
            row = self._connection.execute(
                "SELECT account_id, expires_at FROM affinities WHERE affinity_hash = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            if float(row["expires_at"]) <= now:
                self._connection.execute(
                    "DELETE FROM affinities WHERE affinity_hash = ?", (key,)
                )
                self._connection.commit()
                return None
            return str(row["account_id"])

    async def set_affinities(
        self, values: set[str], account_id: str, ttl_seconds: float
    ) -> None:
        if not values:
            return
        now = self._wall_time()
        expires_at = now + ttl_seconds
        rows = [
            (self._hash_affinity(value), account_id, expires_at, now)
            for value in values
            if value
        ]
        async with self._lock:
            self._connection.execute(
                "DELETE FROM affinities WHERE expires_at <= ?", (now,)
            )
            self._connection.executemany(
                """
                INSERT INTO affinities(affinity_hash, account_id, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(affinity_hash) DO UPDATE SET
                    account_id = excluded.account_id,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
            self._connection.commit()

    async def save_cooldown(self, record: StoredCooldown) -> None:
        async with self._lock:
            with self._connection:
                self._write_cooldown(record)

    def _write_cooldown(self, record: StoredCooldown) -> None:
        for scope, name in record.clears:
            self._connection.execute(
                "DELETE FROM cooldowns WHERE scope = ? AND account_id = ? AND name = ?",
                (scope, record.account_id, name),
            )
        if record.delete:
            self._connection.execute(
                "DELETE FROM cooldowns WHERE scope = ? AND account_id = ? AND name = ?",
                (record.scope, record.account_id, record.name),
            )
            return
        self._connection.execute(
            """
            INSERT INTO cooldowns(
                scope, account_id, name, until_epoch,
                failure_count, retain_after_expiry
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, account_id, name) DO UPDATE SET
                until_epoch = MAX(cooldowns.until_epoch, excluded.until_epoch),
                failure_count = MAX(
                    cooldowns.failure_count, excluded.failure_count
                ),
                retain_after_expiry = MAX(
                    cooldowns.retain_after_expiry,
                    excluded.retain_after_expiry
                )
            """,
            (
                record.scope,
                record.account_id,
                record.name,
                record.until_epoch,
                record.failure_count,
                int(record.retain_after_expiry),
            ),
        )

    async def load_cooldowns(self) -> list[StoredCooldown]:
        now = self._wall_time()
        async with self._lock:
            self._connection.execute(
                "DELETE FROM cooldowns "
                "WHERE until_epoch <= ? AND retain_after_expiry = 0",
                (now,),
            )
            rows = self._connection.execute(
                "SELECT scope, account_id, name, until_epoch, "
                "failure_count, retain_after_expiry FROM cooldowns"
            ).fetchall()
            self._connection.commit()
        return [
            StoredCooldown(
                scope=str(row["scope"]),
                account_id=str(row["account_id"]),
                name=str(row["name"]),
                until_epoch=float(row["until_epoch"]),
                failure_count=int(row["failure_count"]),
                retain_after_expiry=bool(row["retain_after_expiry"]),
            )
            for row in rows
        ]

    async def delete_cooldown(self, scope: str, account_id: str, name: str) -> None:
        async with self._lock:
            self._connection.execute(
                "DELETE FROM cooldowns WHERE scope = ? AND account_id = ? AND name = ?",
                (scope, account_id, name),
            )
            self._connection.commit()

    async def save_quota_snapshot(
        self,
        account_id: str,
        http_status: int,
        snapshot: dict[str, Any],
        *,
        valid_snapshot: bool | None = None,
    ) -> None:
        await self.save_quota_observation(
            account_id,
            http_status,
            snapshot,
            valid_snapshot=valid_snapshot,
        )

    async def save_quota_observation(
        self,
        account_id: str,
        http_status: int,
        snapshot: dict[str, Any],
        *,
        valid_snapshot: bool | None = None,
        transition: StoredCooldown | None = None,
    ) -> None:
        encoded = json.dumps(snapshot, ensure_ascii=True, separators=(",", ":"))
        checked_at = self._wall_time()
        is_valid = http_status == 200 if valid_snapshot is None else valid_snapshot
        success_at = checked_at if is_valid else None
        async with self._lock:
            with self._connection:
                if transition is not None:
                    self._write_cooldown(transition)
                if is_valid:
                    self._connection.execute(
                        """
                        INSERT INTO quota_snapshots(
                            account_id, http_status, snapshot_json, checked_at,
                            success_at, attempt_valid
                        )
                        VALUES (?, ?, ?, ?, ?, 1)
                        ON CONFLICT(account_id) DO UPDATE SET
                            http_status = excluded.http_status,
                            snapshot_json = excluded.snapshot_json,
                            checked_at = excluded.checked_at,
                            success_at = excluded.success_at,
                            attempt_valid = 1
                        """,
                        (account_id, http_status, encoded, checked_at, success_at),
                    )
                else:
                    self._connection.execute(
                        """
                        INSERT INTO quota_snapshots(
                            account_id, http_status, snapshot_json, checked_at,
                            success_at, attempt_valid
                        )
                        VALUES (?, ?, '{}', ?, NULL, 0)
                        ON CONFLICT(account_id) DO UPDATE SET
                            http_status = excluded.http_status,
                            checked_at = excluded.checked_at,
                            attempt_valid = 0
                        """,
                        (account_id, http_status, checked_at),
                    )

    async def quota_snapshots(self) -> dict[str, dict[str, Any]]:
        async with self._lock:
            rows = self._connection.execute(
                "SELECT account_id, http_status, snapshot_json, checked_at, "
                "success_at, attempt_valid "
                "FROM quota_snapshots"
            ).fetchall()
        return {
            str(row["account_id"]): {
                "http_status": int(row["http_status"]),
                "checked_at": float(row["checked_at"]),
                "last_success_at": (
                    float(row["success_at"]) if row["success_at"] is not None else None
                ),
                "stale": not bool(row["attempt_valid"]),
                "credits": json.loads(str(row["snapshot_json"])),
            }
            for row in rows
        }

    async def purge_account(self, account_id: str) -> None:
        await self.purge_accounts((account_id,))

    async def purge_accounts(self, account_ids: Iterable[str]) -> None:
        rows = tuple((account_id,) for account_id in dict.fromkeys(account_ids))
        if not rows:
            return
        async with self._lock:
            with self._connection:
                self._connection.executemany(
                    "DELETE FROM affinities WHERE account_id = ?", rows
                )
                self._connection.executemany(
                    "DELETE FROM cooldowns WHERE account_id = ?", rows
                )
                self._connection.executemany(
                    "DELETE FROM quota_snapshots WHERE account_id = ?", rows
                )

    async def close(self) -> None:
        async with self._lock:
            self._connection.close()
