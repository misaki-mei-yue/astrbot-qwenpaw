"""Persistent account identities and explicit, private-chat account linking.

The database contains opaque workspace identifiers, never user identifiers in
workspace names. Group workspaces belong to an account and a conversation, not
to its private person workspace. Callers must authorize both account owners
before offering these operations; possession of a link code is not an owner
allowlist. No method imports or merges existing QwenPaw memory.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from typing import Callable


class IdentityError(RuntimeError):
    """A safe, user-facing identity failure containing no credentials."""


class IdentityStore:
    LINK_TTL_SECONDS = 600

    def __init__(self, db: sqlite3.Connection, *, clock: Callable[[], float] = time.time):
        self.db = db
        self.clock = clock
        # Individual statements deliberately avoid executescript's implicit
        # commit, because BridgeStore owns this shared connection.
        with self._transaction():
            for statement in (
                """CREATE TABLE IF NOT EXISTS persons (
                    person_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL UNIQUE,
                    activated INTEGER NOT NULL DEFAULT 0 CHECK (activated IN (0,1)),
                    created REAL NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS accounts (
                    platform TEXT NOT NULL, platform_id TEXT NOT NULL,
                    user_id TEXT NOT NULL, person_id TEXT NOT NULL,
                    PRIMARY KEY(platform, platform_id, user_id),
                    FOREIGN KEY(person_id) REFERENCES persons(person_id)
                )""",
                """CREATE TABLE IF NOT EXISTS group_scopes (
                    platform TEXT NOT NULL, platform_id TEXT NOT NULL,
                    user_id TEXT NOT NULL, origin TEXT NOT NULL,
                    agent_id TEXT NOT NULL UNIQUE,
                    PRIMARY KEY(platform, platform_id, user_id, origin),
                    FOREIGN KEY(platform, platform_id, user_id)
                        REFERENCES accounts(platform, platform_id, user_id)
                )""",
                """CREATE TABLE IF NOT EXISTS link_codes (
                    code_hash TEXT PRIMARY KEY, source_person_id TEXT NOT NULL,
                    source_platform TEXT NOT NULL, source_platform_id TEXT NOT NULL,
                    source_user_id TEXT NOT NULL, expires REAL NOT NULL,
                    consumed REAL,
                    FOREIGN KEY(source_person_id) REFERENCES persons(person_id)
                )""",
                "CREATE INDEX IF NOT EXISTS accounts_person_id ON accounts(person_id)",
            ):
                self.db.execute(statement)

    @contextmanager
    def _transaction(self):
        # An IMMEDIATE transaction serializes competing redemption attempts
        # before reading the code. Nested use does not commit callers' changes.
        nested = self.db.in_transaction
        savepoint = "identity_" + secrets.token_hex(8)
        started = False
        try:
            self.db.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
            started = True
            yield
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}" if nested else "COMMIT")
        except Exception as exc:
            if started:
                if nested:
                    self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    self.db.rollback()
            if isinstance(exc, sqlite3.Error):
                raise IdentityError("身份资料暂时无法保存，请稍后重试。") from None
            raise

    def _row(self, statement: str, parameters=()) -> dict | None:
        cursor = self.db.cursor()
        cursor.row_factory = sqlite3.Row
        row = cursor.execute(statement, parameters).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _text(value: str, label: str, limit: int) -> str:
        if (not isinstance(value, str) or not value.strip() or len(value) > limit
                or any(unicodedata.category(char).startswith("C") for char in value)):
            raise IdentityError(f"{label}格式不正确，请检查平台配置。")
        return value

    @classmethod
    def _account_key(cls, platform: str, platform_id: str, user_id: str) -> tuple[str, str, str]:
        return (cls._text(platform, "平台类型", 128),
                cls._text(platform_id, "机器人标识", 256),
                cls._text(user_id, "用户标识", 512))

    @classmethod
    def _identity_key(cls, platform: str, platform_id: str, user_id: str,
                      origin: str, is_private: bool):
        key = cls._account_key(platform, platform_id, user_id)
        cls._text(origin, "会话标识", 1024)
        if type(is_private) is not bool:
            raise IdentityError("无法确认会话类型，请检查平台配置。")
        return key

    @staticmethod
    def _private_only(is_private: bool):
        if is_private is not True:
            raise IdentityError("账号绑定只能在与机器人的私聊中进行。")

    def _ensure_account(self, key: tuple[str, str, str]) -> dict:
        row = self._account(key)
        if row is None:
            person_id, agent_id = "p_" + secrets.token_hex(16), "abp_" + secrets.token_hex(16)
            self.db.execute("INSERT INTO persons VALUES (?,?,0,?)",
                            (person_id, agent_id, self.clock()))
            self.db.execute("INSERT INTO accounts VALUES (?,?,?,?)", (*key, person_id))
            row = {"person_id": person_id, "agent_id": agent_id, "activated": 0}
        return row

    def _account(self, key: tuple[str, str, str]) -> dict | None:
        return self._row("""SELECT p.person_id, p.agent_id, p.activated FROM accounts a
            JOIN persons p ON a.person_id=p.person_id
            WHERE a.platform=? AND a.platform_id=? AND a.user_id=?""", key)

    @staticmethod
    def _private_result(person: dict) -> dict:
        return {"person_id": person["person_id"], "agent_id": person["agent_id"], "scope": "private"}

    def resolve(self, platform: str, platform_id: str, user_id: str,
                origin: str, is_private: bool) -> dict:
        key = self._identity_key(platform, platform_id, user_id, origin, is_private)
        with self._transaction():
            person = self._ensure_account(key)
            if is_private:
                return self._private_result(person)
            row = self._row("""SELECT agent_id FROM group_scopes
                WHERE platform=? AND platform_id=? AND user_id=? AND origin=?""", (*key, origin))
            if row is None:
                row = {"agent_id": "abg_" + secrets.token_hex(16)}
                self.db.execute("INSERT INTO group_scopes VALUES (?,?,?,?,?)", (*key, origin, row["agent_id"]))
            return {"person_id": person["person_id"], "agent_id": row["agent_id"], "scope": "group"}

    def lookup(self, platform: str, platform_id: str, user_id: str) -> dict | None:
        """Return an existing private identity without creating any account."""
        person = self._account(self._account_key(platform, platform_id, user_id))
        return self._private_result(person) if person else None

    def get_person(self, person_id: str) -> dict | None:
        self._text(person_id, "个人标识", 128)
        row = self._row("SELECT person_id, agent_id, activated FROM persons WHERE person_id=?", (person_id,))
        if row:
            row["activated"] = bool(row["activated"])
        return row

    def activate(self, person_id: str):
        """Mark a private workspace used BEFORE any external creation or run.

        This marker is deliberately irreversible: even a failed remote request
        might have written memory. Never call this for a group workspace.
        """
        self._text(person_id, "个人标识", 128)
        with self._transaction():
            cursor = self.db.execute("UPDATE persons SET activated=1 WHERE person_id=?", (person_id,))
            if cursor.rowcount != 1:
                raise IdentityError("找不到这个用户的身份资料。")

    def create_link(self, platform: str, platform_id: str, user_id: str,
                    origin: str, is_private: bool) -> str:
        key = self._identity_key(platform, platform_id, user_id, origin, is_private)
        self._private_only(is_private)
        with self._transaction():
            source = self._ensure_account(key)
            code = secrets.token_urlsafe(32)
            self.db.execute("INSERT INTO link_codes VALUES (?,?,?,?,?,?,NULL)",
                            (hashlib.sha256(code.encode("ascii")).hexdigest(), source["person_id"],
                             *key, self.clock() + self.LINK_TTL_SECONDS))
            return code

    def redeem_link(self, platform: str, platform_id: str, user_id: str,
                    origin: str, is_private: bool, code: str) -> dict:
        key = self._identity_key(platform, platform_id, user_id, origin, is_private)
        self._private_only(is_private)
        if not isinstance(code, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", code) is None:
            raise IdentityError("绑定码无效、已使用或已过期，请重新生成。")
        code_hash = hashlib.sha256(code.encode("ascii")).hexdigest()
        with self._transaction():
            link = self._row("SELECT * FROM link_codes WHERE code_hash=?", (code_hash,))
            now = self.clock()
            if link is None or link["consumed"] is not None or now >= link["expires"]:
                raise IdentityError("绑定码无效、已使用或已过期，请重新生成。")
            source_key = (link["source_platform"], link["source_platform_id"], link["source_user_id"])
            if key == source_key:
                raise IdentityError("请在要绑定的另一个账号私聊中使用绑定码。")
            source = self._account(source_key)
            if source is None or source["person_id"] != link["source_person_id"]:
                raise IdentityError("发起账号的身份已经改变，请重新生成绑定码。")
            target = self._ensure_account(key)
            already_linked = target["person_id"] == source["person_id"]
            if not already_linked:
                count = self.db.execute("SELECT COUNT(*) FROM accounts WHERE person_id=?",
                                        (target["person_id"],)).fetchone()[0]
                if target["activated"] or count != 1:
                    raise IdentityError("这个账号已有独立工作区或绑定关系，为保留原有记忆，不能自动合并。")
                # Keep the old person row and every group workspace untouched.
                # No external files, messages or memories are copied/deleted.
                self.db.execute("""UPDATE accounts SET person_id=?
                    WHERE platform=? AND platform_id=? AND user_id=?""", (source["person_id"], *key))
            cursor = self.db.execute("""UPDATE link_codes SET consumed=?
                WHERE code_hash=? AND consumed IS NULL AND expires>?""", (now, code_hash, now))
            if cursor.rowcount != 1:
                raise IdentityError("绑定码无效、已使用或已过期，请重新生成。")
            return {**self._private_result(source), "already_linked": already_linked}
