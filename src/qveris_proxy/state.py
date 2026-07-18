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

from .access_keys import (
    ProxyAccessKeyConsumeResult,
    ProxyAccessKeyRecord,
)


_PROXY_ACCESS_KEY_COLUMNS = """
    id, kind, name, secret_hash, prefix, suffix, enabled,
    request_limit, requests_used, requests_per_minute,
    max_concurrency, expires_at, created_at, updated_at,
    last_used_at, rpm_window_started_at, rpm_requests
"""


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

            CREATE TABLE IF NOT EXISTS proxy_access_keys (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ('primary', 'managed')),
                name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 64),
                secret_hash TEXT NOT NULL UNIQUE CHECK(length(secret_hash) = 64),
                prefix TEXT NOT NULL CHECK(length(prefix) <= 16),
                suffix TEXT NOT NULL CHECK(length(suffix) <= 16),
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                request_limit INTEGER CHECK(
                    request_limit IS NULL
                    OR request_limit BETWEEN 1 AND 1000000000000
                ),
                requests_used INTEGER NOT NULL DEFAULT 0 CHECK(requests_used >= 0),
                requests_per_minute INTEGER CHECK(
                    requests_per_minute IS NULL
                    OR requests_per_minute BETWEEN 1 AND 1000000
                ),
                max_concurrency INTEGER NOT NULL DEFAULT 8 CHECK(
                    max_concurrency BETWEEN 1 AND 1024
                ),
                expires_at REAL CHECK(
                    expires_at IS NULL
                    OR expires_at BETWEEN 1 AND 253402300799
                ),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_used_at REAL,
                rpm_window_started_at REAL NOT NULL DEFAULT 0,
                rpm_requests INTEGER NOT NULL DEFAULT 0 CHECK(rpm_requests >= 0)
            );
            CREATE INDEX IF NOT EXISTS idx_proxy_access_keys_created
                ON proxy_access_keys(created_at, id);
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

    @staticmethod
    def _valid_secret_hash(value: str) -> bool:
        return len(value) == 64 and all(
            character in "0123456789abcdef" for character in value
        )

    @staticmethod
    def _proxy_access_key_from_row(row: sqlite3.Row) -> ProxyAccessKeyRecord:
        return ProxyAccessKeyRecord(
            id=str(row["id"]),
            kind=str(row["kind"]),  # type: ignore[arg-type]
            name=str(row["name"]),
            prefix=str(row["prefix"]),
            suffix=str(row["suffix"]),
            enabled=bool(row["enabled"]),
            request_limit=(
                int(row["request_limit"]) if row["request_limit"] is not None else None
            ),
            requests_used=int(row["requests_used"]),
            requests_per_minute=(
                int(row["requests_per_minute"])
                if row["requests_per_minute"] is not None
                else None
            ),
            max_concurrency=int(row["max_concurrency"]),
            expires_at=(
                float(row["expires_at"]) if row["expires_at"] is not None else None
            ),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            last_used_at=(
                float(row["last_used_at"]) if row["last_used_at"] is not None else None
            ),
        )

    async def ensure_primary_proxy_access_key(
        self,
        *,
        secret_hash: str,
        prefix: str,
        suffix: str,
        max_concurrency: int,
    ) -> ProxyAccessKeyRecord:
        if not self._valid_secret_hash(secret_hash):
            raise ValueError("invalid proxy access key hash")
        now = self._wall_time()
        async with self._lock:
            try:
                with self._connection:
                    row = self._connection.execute(
                        f"SELECT {_PROXY_ACCESS_KEY_COLUMNS} "
                        "FROM proxy_access_keys WHERE id = 'primary'"
                    ).fetchone()
                    if row is None:
                        self._connection.execute(
                            """
                            INSERT INTO proxy_access_keys(
                                id, kind, name, secret_hash, prefix, suffix,
                                enabled, request_limit, requests_used,
                                requests_per_minute, max_concurrency, expires_at,
                                created_at, updated_at, last_used_at,
                                rpm_window_started_at, rpm_requests
                            )
                            VALUES (
                                'primary', 'primary', 'Primary', ?, ?, ?,
                                1, NULL, 0, NULL, ?, NULL, ?, ?, NULL, 0, 0
                            )
                            """,
                            (
                                secret_hash,
                                prefix,
                                suffix,
                                max_concurrency,
                                now,
                                now,
                            ),
                        )
                    elif (
                        str(row["kind"]) != "primary"
                        or str(row["secret_hash"]) != secret_hash
                        or str(row["prefix"]) != prefix
                        or str(row["suffix"]) != suffix
                    ):
                        self._connection.execute(
                            """
                            UPDATE proxy_access_keys
                            SET kind = 'primary', secret_hash = ?, prefix = ?,
                                suffix = ?, updated_at = ?
                            WHERE id = 'primary'
                            """,
                            (secret_hash, prefix, suffix, now),
                        )
                    current = self._connection.execute(
                        f"SELECT {_PROXY_ACCESS_KEY_COLUMNS} "
                        "FROM proxy_access_keys WHERE id = 'primary'"
                    ).fetchone()
            except sqlite3.IntegrityError:
                raise ValueError("proxy access key already exists") from None
        assert current is not None
        return self._proxy_access_key_from_row(current)

    async def create_proxy_access_key(
        self,
        *,
        key_id: str,
        name: str,
        secret_hash: str,
        prefix: str,
        suffix: str,
        enabled: bool,
        request_limit: int | None,
        requests_per_minute: int | None,
        max_concurrency: int,
        expires_at: float | None,
    ) -> ProxyAccessKeyRecord:
        if key_id == "primary" or not key_id:
            raise ValueError("invalid managed proxy access key id")
        if not self._valid_secret_hash(secret_hash):
            raise ValueError("invalid proxy access key hash")
        now = self._wall_time()
        async with self._lock:
            try:
                with self._connection:
                    self._connection.execute(
                        """
                        INSERT INTO proxy_access_keys(
                            id, kind, name, secret_hash, prefix, suffix,
                            enabled, request_limit, requests_used,
                            requests_per_minute, max_concurrency, expires_at,
                            created_at, updated_at, last_used_at,
                            rpm_window_started_at, rpm_requests
                        )
                        VALUES (?, 'managed', ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, NULL, 0, 0)
                        """,
                        (
                            key_id,
                            name,
                            secret_hash,
                            prefix,
                            suffix,
                            int(enabled),
                            request_limit,
                            requests_per_minute,
                            max_concurrency,
                            expires_at,
                            now,
                            now,
                        ),
                    )
                    row = self._connection.execute(
                        f"SELECT {_PROXY_ACCESS_KEY_COLUMNS} "
                        "FROM proxy_access_keys WHERE id = ?",
                        (key_id,),
                    ).fetchone()
            except sqlite3.IntegrityError:
                raise ValueError(
                    "proxy access key already exists or is invalid"
                ) from None
        assert row is not None
        return self._proxy_access_key_from_row(row)

    async def list_proxy_access_keys(self) -> list[ProxyAccessKeyRecord]:
        async with self._lock:
            rows = self._connection.execute(
                f"SELECT {_PROXY_ACCESS_KEY_COLUMNS} FROM proxy_access_keys "
                "ORDER BY kind = 'primary' DESC, created_at, id"
            ).fetchall()
        return [self._proxy_access_key_from_row(row) for row in rows]

    async def get_proxy_access_key(self, key_id: str) -> ProxyAccessKeyRecord | None:
        async with self._lock:
            row = self._connection.execute(
                f"SELECT {_PROXY_ACCESS_KEY_COLUMNS} "
                "FROM proxy_access_keys WHERE id = ?",
                (key_id,),
            ).fetchone()
        return self._proxy_access_key_from_row(row) if row is not None else None

    async def get_proxy_access_key_by_hash(
        self, secret_hash: str
    ) -> ProxyAccessKeyRecord | None:
        if not self._valid_secret_hash(secret_hash):
            return None
        async with self._lock:
            row = self._connection.execute(
                f"SELECT {_PROXY_ACCESS_KEY_COLUMNS} "
                "FROM proxy_access_keys WHERE secret_hash = ?",
                (secret_hash,),
            ).fetchone()
        return self._proxy_access_key_from_row(row) if row is not None else None

    async def inspect_proxy_access_key(
        self, secret_hash: str
    ) -> ProxyAccessKeyConsumeResult:
        if not self._valid_secret_hash(secret_hash):
            return ProxyAccessKeyConsumeResult(key=None, reason="invalid")
        now = self._wall_time()
        async with self._lock:
            row = self._connection.execute(
                f"SELECT {_PROXY_ACCESS_KEY_COLUMNS} "
                "FROM proxy_access_keys WHERE secret_hash = ?",
                (secret_hash,),
            ).fetchone()
        if row is None:
            return ProxyAccessKeyConsumeResult(key=None, reason="invalid")
        key = self._proxy_access_key_from_row(row)
        if not key.enabled:
            return ProxyAccessKeyConsumeResult(key=key, reason="disabled")
        if key.expires_at is not None and key.expires_at <= now:
            return ProxyAccessKeyConsumeResult(key=key, reason="expired")
        if key.request_limit is not None and key.requests_used >= key.request_limit:
            return ProxyAccessKeyConsumeResult(key=key, reason="request_limit")
        rpm_window_started_at = float(row["rpm_window_started_at"])
        rpm_requests = int(row["rpm_requests"])
        if (
            key.requests_per_minute is not None
            and rpm_window_started_at > 0
            and now < rpm_window_started_at + 60
            and rpm_requests >= key.requests_per_minute
        ):
            return ProxyAccessKeyConsumeResult(
                key=key,
                reason="rate_limit",
                retry_after=max(0.001, rpm_window_started_at + 60 - now),
            )
        return ProxyAccessKeyConsumeResult(key=key)

    async def update_proxy_access_key(
        self,
        key_id: str,
        *,
        name: str,
        enabled: bool,
        request_limit: int | None,
        requests_per_minute: int | None,
        max_concurrency: int,
        expires_at: float | None,
    ) -> ProxyAccessKeyRecord | None:
        now = self._wall_time()
        async with self._lock:
            try:
                with self._connection:
                    cursor = self._connection.execute(
                        """
                        UPDATE proxy_access_keys
                        SET name = ?, enabled = ?, request_limit = ?,
                            requests_per_minute = ?, max_concurrency = ?,
                            expires_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            name,
                            int(enabled),
                            request_limit,
                            requests_per_minute,
                            max_concurrency,
                            expires_at,
                            now,
                            key_id,
                        ),
                    )
                    if cursor.rowcount == 0:
                        return None
                    row = self._connection.execute(
                        f"SELECT {_PROXY_ACCESS_KEY_COLUMNS} "
                        "FROM proxy_access_keys WHERE id = ?",
                        (key_id,),
                    ).fetchone()
            except sqlite3.IntegrityError:
                raise ValueError("proxy access key settings are invalid") from None
        assert row is not None
        return self._proxy_access_key_from_row(row)

    async def delete_proxy_access_key(self, key_id: str) -> bool:
        async with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM proxy_access_keys WHERE id = ? AND kind <> 'primary'",
                (key_id,),
            )
            self._connection.commit()
            return cursor.rowcount == 1

    async def reset_proxy_access_key_usage(
        self, key_id: str
    ) -> ProxyAccessKeyRecord | None:
        now = self._wall_time()
        async with self._lock:
            with self._connection:
                cursor = self._connection.execute(
                    """
                    UPDATE proxy_access_keys
                    SET requests_used = 0, rpm_window_started_at = 0,
                        rpm_requests = 0, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, key_id),
                )
                if cursor.rowcount == 0:
                    return None
                row = self._connection.execute(
                    f"SELECT {_PROXY_ACCESS_KEY_COLUMNS} "
                    "FROM proxy_access_keys WHERE id = ?",
                    (key_id,),
                ).fetchone()
        assert row is not None
        return self._proxy_access_key_from_row(row)

    async def consume_proxy_access_key(
        self, secret_hash: str
    ) -> ProxyAccessKeyConsumeResult:
        if not self._valid_secret_hash(secret_hash):
            return ProxyAccessKeyConsumeResult(key=None, reason="invalid")

        now = self._wall_time()
        async with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    f"SELECT {_PROXY_ACCESS_KEY_COLUMNS} "
                    "FROM proxy_access_keys WHERE secret_hash = ?",
                    (secret_hash,),
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return ProxyAccessKeyConsumeResult(key=None, reason="invalid")

                key = self._proxy_access_key_from_row(row)
                if not key.enabled:
                    self._connection.commit()
                    return ProxyAccessKeyConsumeResult(key=key, reason="disabled")
                if key.expires_at is not None and key.expires_at <= now:
                    self._connection.commit()
                    return ProxyAccessKeyConsumeResult(key=key, reason="expired")
                if (
                    key.request_limit is not None
                    and key.requests_used >= key.request_limit
                ):
                    self._connection.commit()
                    return ProxyAccessKeyConsumeResult(key=key, reason="request_limit")

                rpm_window_started_at = float(row["rpm_window_started_at"])
                rpm_requests = int(row["rpm_requests"])
                if rpm_window_started_at <= 0 or now >= rpm_window_started_at + 60:
                    rpm_window_started_at = now
                    rpm_requests = 0
                if (
                    key.requests_per_minute is not None
                    and rpm_requests >= key.requests_per_minute
                ):
                    retry_after = max(0.001, rpm_window_started_at + 60 - now)
                    self._connection.commit()
                    return ProxyAccessKeyConsumeResult(
                        key=key,
                        reason="rate_limit",
                        retry_after=retry_after,
                    )

                next_rpm_requests = (
                    rpm_requests + 1 if key.requests_per_minute is not None else 0
                )
                self._connection.execute(
                    """
                    UPDATE proxy_access_keys
                    SET requests_used = requests_used + 1,
                        last_used_at = ?, updated_at = ?,
                        rpm_window_started_at = ?, rpm_requests = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        now,
                        rpm_window_started_at,
                        next_rpm_requests,
                        key.id,
                    ),
                )
                consumed = self._connection.execute(
                    f"SELECT {_PROXY_ACCESS_KEY_COLUMNS} "
                    "FROM proxy_access_keys WHERE id = ?",
                    (key.id,),
                ).fetchone()
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        assert consumed is not None
        return ProxyAccessKeyConsumeResult(
            key=self._proxy_access_key_from_row(consumed)
        )

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
