import asyncio
import base64
import json
import logging
import time
import zlib

import aiohttp

log = logging.getLogger("db")

PENDING_TTL = 3600  # seconds a pending copy job stays valid
CHUNK = 400_000     # characters per stored chunk (keeps every request small)


class DatabaseError(Exception):
    pass


def _pack(obj) -> str:
    return base64.b64encode(zlib.compress(json.dumps(obj).encode("utf-8"), 6)).decode("ascii")


def _unpack(text: str):
    return json.loads(zlib.decompress(base64.b64decode(text)).decode("utf-8"))


def _arg(value) -> dict:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def _val(v: dict):
    t = v["type"]
    if t == "null":
        return None
    if t == "integer":
        return int(v["value"])
    if t == "float":
        return float(v["value"])
    if t == "blob":
        return base64.b64decode(v["base64"] + "==")
    return v["value"]


def _rows(result: dict) -> list:
    names = [c["name"] for c in result.get("cols", [])]
    return [dict(zip(names, (_val(x) for x in row))) for row in result.get("rows", [])]


class Database:
    """Turso (hosted libSQL) over its HTTP API. No extra dependency, aiohttp ships with discord.py."""

    def __init__(self, url: str, token: str):
        url = url.strip().rstrip("/")
        if url.startswith("libsql://"):
            url = "https://" + url[len("libsql://"):]
        self.base = url
        self.token = token
        self.session: aiohttp.ClientSession | None = None

    # ---------- low level ----------
    async def _pipeline(self, statements: list) -> list:
        """statements = [(sql, [args]), ...]. Returns one result dict per statement."""
        requests = [
            {"type": "execute", "stmt": {"sql": sql, "args": [_arg(a) for a in args]}}
            for sql, args in statements
        ]
        requests.append({"type": "close"})
        headers = {"Authorization": f"Bearer {self.token}"}

        last_error = None
        for attempt in range(3):
            try:
                async with self.session.post(
                    f"{self.base}/v2/pipeline", json={"requests": requests}, headers=headers
                ) as resp:
                    if resp.status >= 500:
                        raise aiohttp.ClientError(f"server error {resp.status}")
                    if resp.status != 200:
                        raise DatabaseError(f"Turso returned {resp.status}: {(await resp.text())[:200]}")
                    data = await resp.json()
                break
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                await asyncio.sleep(0.5 * (attempt + 1))
        else:
            raise DatabaseError(f"Could not reach Turso: {last_error}")

        out = []
        for res in data["results"][: len(statements)]:
            if res["type"] == "error":
                raise DatabaseError(res["error"].get("message", "unknown error"))
            out.append(res["response"]["result"])
        return out

    async def _one(self, sql: str, args: list | None = None) -> dict:
        return (await self._pipeline([(sql, args or [])]))[0]

    # ---------- blobs (big payloads stored in chunks) ----------
    async def _put_blob(self, key: str, text: str):
        await self._one("DELETE FROM blobs WHERE key = ?", [key])
        for idx, start in enumerate(range(0, len(text), CHUNK)):
            await self._one(
                "INSERT INTO blobs (key, idx, chunk) VALUES (?, ?, ?)",
                [key, idx, text[start:start + CHUNK]],
            )

    async def _get_blob(self, key: str) -> str:
        res = await self._one("SELECT chunk FROM blobs WHERE key = ? ORDER BY idx", [key])
        return "".join(r["chunk"] for r in _rows(res))

    async def _del_blob(self, key: str | None):
        if key:
            await self._one("DELETE FROM blobs WHERE key = ?", [key])

    # ---------- setup ----------
    async def connect(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        await self._pipeline([
            ("CREATE TABLE IF NOT EXISTS owners (user_id INTEGER PRIMARY KEY, added_at INTEGER NOT NULL)", []),
            (
                "CREATE TABLE IF NOT EXISTS clones ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, guild_id INTEGER NOT NULL, "
                "guild_name TEXT NOT NULL, blob_key TEXT NOT NULL, created_at INTEGER NOT NULL, "
                "UNIQUE (user_id, guild_id))",
                [],
            ),
            (
                "CREATE TABLE IF NOT EXISTS pending ("
                "target_guild_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                "blob_key TEXT NOT NULL, created_at INTEGER NOT NULL)",
                [],
            ),
            (
                "CREATE TABLE IF NOT EXISTS blobs ("
                "key TEXT NOT NULL, idx INTEGER NOT NULL, chunk TEXT NOT NULL, PRIMARY KEY (key, idx))",
                [],
            ),
        ])
        # drop expired pending jobs left over from before a restart
        cutoff = int(time.time()) - PENDING_TTL
        await self._pipeline([
            ("DELETE FROM blobs WHERE key IN (SELECT blob_key FROM pending WHERE created_at < ?)", [cutoff]),
            ("DELETE FROM pending WHERE created_at < ?", [cutoff]),
        ])
        log.info("Connected to Turso")

    async def close(self):
        if self.session is not None:
            await self.session.close()

    # ---------- owners ----------
    async def add_owner(self, user_id: int) -> bool:
        res = await self._one(
            "INSERT OR IGNORE INTO owners (user_id, added_at) VALUES (?, ?)",
            [user_id, int(time.time())],
        )
        return res.get("affected_row_count", 0) > 0

    async def is_owner(self, user_id: int) -> bool:
        res = await self._one("SELECT 1 AS x FROM owners WHERE user_id = ?", [user_id])
        return len(_rows(res)) > 0

    async def remove_owner(self, user_id: int):
        """Removes the owner and every clone / pending job of theirs.
        Returns (was_owner, clones_deleted)."""
        res = await self._one("DELETE FROM owners WHERE user_id = ?", [user_id])
        if res.get("affected_row_count", 0) == 0:
            return False, 0
        results = await self._pipeline([
            ("DELETE FROM blobs WHERE key IN (SELECT blob_key FROM clones WHERE user_id = ?)", [user_id]),
            ("DELETE FROM blobs WHERE key IN (SELECT blob_key FROM pending WHERE user_id = ?)", [user_id]),
            ("DELETE FROM clones WHERE user_id = ?", [user_id]),
            ("DELETE FROM pending WHERE user_id = ?", [user_id]),
        ])
        return True, results[2].get("affected_row_count", 0)

    # ---------- clones ----------
    async def save_clone(self, user_id: int, guild_id: int, guild_name: str, snapshot: dict):
        ts = int(time.time())
        key = f"clone:{user_id}:{guild_id}:{ts}"
        # new data first, then switch the row over, then drop the old data
        await self._put_blob(key, _pack(snapshot))
        old = _rows(await self._one(
            "SELECT blob_key FROM clones WHERE user_id = ? AND guild_id = ?", [user_id, guild_id]
        ))
        await self._one(
            "INSERT INTO clones (user_id, guild_id, guild_name, blob_key, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, guild_id) DO UPDATE SET "
            "guild_name = excluded.guild_name, blob_key = excluded.blob_key, created_at = excluded.created_at",
            [user_id, guild_id, guild_name, key, ts],
        )
        if old and old[0]["blob_key"] != key:
            await self._del_blob(old[0]["blob_key"])

    async def list_clones(self, user_id: int):
        res = await self._one(
            "SELECT id, guild_id, guild_name, created_at FROM clones WHERE user_id = ? ORDER BY created_at DESC",
            [user_id],
        )
        return _rows(res)

    async def get_clone(self, user_id: int, clone_id: int):
        rows = _rows(await self._one(
            "SELECT id, guild_id, guild_name, blob_key, created_at FROM clones WHERE id = ? AND user_id = ?",
            [clone_id, user_id],
        ))
        if not rows:
            return None
        d = rows[0]
        text = await self._get_blob(d.pop("blob_key"))
        if not text:
            return None
        d["snapshot"] = _unpack(text)
        return d

    async def delete_clone(self, user_id: int, clone_id: int):
        rows = _rows(await self._one(
            "SELECT blob_key FROM clones WHERE id = ? AND user_id = ?", [clone_id, user_id]
        ))
        if not rows:
            return
        await self._pipeline([
            ("DELETE FROM clones WHERE id = ? AND user_id = ?", [clone_id, user_id]),
            ("DELETE FROM blobs WHERE key = ?", [rows[0]["blob_key"]]),
        ])

    # ---------- pending jobs ----------
    async def set_pending(self, target_guild_id: int, user_id: int, snapshot: dict, options: list):
        ts = int(time.time())
        key = f"pending:{target_guild_id}:{ts}"
        await self._put_blob(key, _pack({"snapshot": snapshot, "options": options}))
        old = _rows(await self._one(
            "SELECT blob_key FROM pending WHERE target_guild_id = ?", [target_guild_id]
        ))
        await self._one(
            "INSERT OR REPLACE INTO pending (target_guild_id, user_id, blob_key, created_at) VALUES (?, ?, ?, ?)",
            [target_guild_id, user_id, key, ts],
        )
        if old and old[0]["blob_key"] != key:
            await self._del_blob(old[0]["blob_key"])

    async def pop_pending(self, target_guild_id: int):
        rows = _rows(await self._one(
            "SELECT user_id, blob_key, created_at FROM pending WHERE target_guild_id = ?",
            [target_guild_id],
        ))
        if not rows:
            return None
        row = rows[0]
        text = await self._get_blob(row["blob_key"])
        await self._pipeline([
            ("DELETE FROM pending WHERE target_guild_id = ?", [target_guild_id]),
            ("DELETE FROM blobs WHERE key = ?", [row["blob_key"]]),
        ])
        if not text or time.time() - row["created_at"] > PENDING_TTL:
            return None
        payload = _unpack(text)
        return {"user_id": row["user_id"], "snapshot": payload["snapshot"], "options": payload["options"]}
