import json
import time
import zlib

import aiosqlite

PENDING_TTL = 3600  # seconds a pending copy job stays valid


def _pack(obj) -> bytes:
    return zlib.compress(json.dumps(obj).encode("utf-8"), 6)


def _unpack(blob: bytes):
    return json.loads(zlib.decompress(blob).decode("utf-8"))


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS owners (
                user_id INTEGER PRIMARY KEY,
                added_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                guild_name TEXT NOT NULL,
                data BLOB NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE (user_id, guild_id)
            );
            CREATE TABLE IF NOT EXISTS pending (
                target_guild_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                data BLOB NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
        await self.conn.commit()

    # ---------- owners ----------
    async def add_owner(self, user_id: int) -> bool:
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO owners (user_id, added_at) VALUES (?, ?)",
            (user_id, int(time.time())),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def is_owner(self, user_id: int) -> bool:
        cur = await self.conn.execute("SELECT 1 FROM owners WHERE user_id = ?", (user_id,))
        return await cur.fetchone() is not None

    async def remove_owner(self, user_id: int):
        """Removes the owner and every clone / pending job of theirs.
        Returns (was_owner, clones_deleted)."""
        cur = await self.conn.execute("DELETE FROM owners WHERE user_id = ?", (user_id,))
        if cur.rowcount == 0:
            await self.conn.commit()
            return False, 0
        cur = await self.conn.execute("DELETE FROM clones WHERE user_id = ?", (user_id,))
        deleted = cur.rowcount
        await self.conn.execute("DELETE FROM pending WHERE user_id = ?", (user_id,))
        await self.conn.commit()
        return True, deleted

    # ---------- clones ----------
    async def save_clone(self, user_id: int, guild_id: int, guild_name: str, snapshot: dict):
        await self.conn.execute(
            """
            INSERT INTO clones (user_id, guild_id, guild_name, data, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (user_id, guild_id) DO UPDATE SET
                guild_name = excluded.guild_name,
                data = excluded.data,
                created_at = excluded.created_at
            """,
            (user_id, guild_id, guild_name, _pack(snapshot), int(time.time())),
        )
        await self.conn.commit()

    async def list_clones(self, user_id: int):
        cur = await self.conn.execute(
            "SELECT id, guild_id, guild_name, created_at FROM clones WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def get_clone(self, user_id: int, clone_id: int):
        cur = await self.conn.execute(
            "SELECT id, guild_id, guild_name, data, created_at FROM clones WHERE id = ? AND user_id = ?",
            (clone_id, user_id),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        d = dict(row)
        d["snapshot"] = _unpack(d.pop("data"))
        return d

    async def delete_clone(self, user_id: int, clone_id: int):
        await self.conn.execute("DELETE FROM clones WHERE id = ? AND user_id = ?", (clone_id, user_id))
        await self.conn.commit()

    # ---------- pending jobs ----------
    async def set_pending(self, target_guild_id: int, user_id: int, snapshot: dict, options: list):
        await self.conn.execute(
            "INSERT OR REPLACE INTO pending (target_guild_id, user_id, data, created_at) VALUES (?, ?, ?, ?)",
            (target_guild_id, user_id, _pack({"snapshot": snapshot, "options": options}), int(time.time())),
        )
        await self.conn.commit()

    async def pop_pending(self, target_guild_id: int):
        cur = await self.conn.execute(
            "SELECT user_id, data, created_at FROM pending WHERE target_guild_id = ?",
            (target_guild_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        await self.conn.execute("DELETE FROM pending WHERE target_guild_id = ?", (target_guild_id,))
        await self.conn.commit()
        if time.time() - row["created_at"] > PENDING_TTL:
            return None
        payload = _unpack(row["data"])
        return {"user_id": row["user_id"], "snapshot": payload["snapshot"], "options": payload["options"]}
