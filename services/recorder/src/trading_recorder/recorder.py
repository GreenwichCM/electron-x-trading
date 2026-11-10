"""Copy the Redis streams `orders`, `fills`, `alerts` and `screen_orders` into SQL Server, for good.

`screen_orders` (from dropcopy) are orders entered on the ElectronX screen; they go into the same tables
as ours, with source 'electronx'.

Redis keeps only the latest 100,000 or so entries of each stream; SQL Server keeps everything. The
recorder reads from where its last commit ended (dbo.stream_offsets), so stopping it, or SQL Server
being down for a while, only delays the copy, as long as Redis has not trimmed the entries yet.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from redis import Redis
from redis.exceptions import RedisError
from trading_db import LATEST, Connection, DbError, schema_version

from . import store
from .rows import alert_row, decode_entry, fill_row, order_row

log = logging.getLogger("recorder")

STREAMS = ("orders", "fills", "alerts", "screen_orders")
RETRY_S = 2.0


class SchemaError(Exception):
    pass


def _id(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _key(entry_id: str) -> tuple[int, int]:
    ms, _, seq = entry_id.partition("-")
    return int(ms), int(seq or 0)


class Recorder:
    def __init__(
        self,
        redis: Redis,
        connect: Callable[[], Connection],
        batch: int = 500,
        block_ms: int = 1000,
    ):
        self.redis = redis
        self.connect = connect
        self.batch = batch
        self.block_ms = block_ms
        self.conn: Connection | None = None
        self.offsets: dict[str, str] = {}
        self.recorded = 0

    # ---- connection ------------------------------------------------------------------------

    def open(self) -> None:
        """Connect, check the schema, and load where each stream got to."""
        conn = self.connect()
        version = schema_version(conn)
        if version < LATEST:
            conn.close()
            raise SchemaError(
                f"database schema is at version {version}, this recorder needs {LATEST}: run "
                "`uv run --env-file services/db.env python -m trading_db migrate`"
            )
        self.conn = conn
        self.offsets = {s: "0-0" for s in STREAMS} | store.load_offsets(conn)
        self._warn_about_gaps()

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # best effort: the connection may already be dead
                pass
            self.conn = None

    def _warn_about_gaps(self) -> None:
        """Entries Redis trimmed (or a wiped Redis) before the recorder saw them cannot be recorded."""
        for stream, last in self.offsets.items():
            if last == "0-0":
                continue
            first = self.redis.xrange(stream, "-", "+", count=1)
            if first and _key(_id(first[0][0])) > _key(last):
                log.warning(
                    "%s: the stream now starts at %s, after the last entry recorded (%s); anything in between "
                    "was not recorded (trimmed by Redis, or Redis was wiped)",
                    stream,
                    _id(first[0][0]),
                    last,
                )

    # ---- work ------------------------------------------------------------------------------

    def step(self) -> int:
        """Read whatever is new (waiting up to block_ms), write it, commit. Returns entries recorded."""
        if self.conn is None:
            self.open()
        assert self.conn is not None
        found = self.redis.xread(dict(self.offsets), count=self.batch, block=self.block_ms) or []
        if not found:
            return 0
        new_offsets: dict[str, str] = {}
        n = 0
        try:
            for stream, entries in found:
                name = _id(stream)
                for entry_id, fields in entries:
                    eid = _id(entry_id)
                    self._write(name, eid, fields)
                    new_offsets[name] = eid
                    n += 1
            for name, eid in new_offsets.items():
                store.save_offset(self.conn, name, eid)
            self.conn.commit()
        except DbError:
            self._rollback()
            raise
        self.offsets.update(new_offsets)
        self.recorded += n
        return n

    def _write(self, stream: str, entry_id: str, fields: dict) -> None:
        assert self.conn is not None
        try:
            data, raw = decode_entry(fields)
            if stream in ("orders", "screen_orders"):
                row = order_row(entry_id, data)
            elif stream == "fills":
                row = fill_row(entry_id, data)
            else:
                row = alert_row(entry_id, data, raw)
        except (KeyError, ValueError, TypeError) as exc:
            log.error("%s %s cannot be recorded (%r); skipping it: %s", stream, entry_id, exc, fields)
            return
        if stream in ("orders", "screen_orders"):
            store.write_order(self.conn, row)
        elif stream == "fills":
            store.write_fill(self.conn, row)
        else:
            store.write_alert(self.conn, row)

    def _rollback(self) -> None:
        try:
            if self.conn is not None:
                self.conn.rollback()
        except Exception:  # best effort: on a dead connection there is nothing to undo
            pass

    def run(self, stop: threading.Event) -> None:
        """step() until `stop` is set, riding out Redis and SQL Server outages."""
        reported = 0.0
        while not stop.is_set():
            try:
                self.step()
            except SchemaError:
                raise
            except DbError as exc:
                log.warning("SQL Server error (%s); reconnecting in %.0fs", exc, RETRY_S)
                self.close()
                stop.wait(RETRY_S)
            except (RedisError, OSError) as exc:
                log.warning("Redis error (%s); retrying in %.0fs", exc, RETRY_S)
                stop.wait(RETRY_S)
            if time.monotonic() - reported > 60:
                reported = time.monotonic()
                log.info("recorded %d entries so far; at %s", self.recorded, self.offsets)
        self.close()
