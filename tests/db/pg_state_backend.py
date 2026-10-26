"""測試專用：以 psycopg 對本機 scratch PostgreSQL 實作 StateBackend（與 SupabaseStateBackend 同一個介面）。

目的：用真實 SQL（INSERT ... ON CONFLICT DO NOTHING）驗證 store 的原子性與到期語意；不連 Supabase。
每次操作各開一條連線，所以多執行緒測試是真的並行。
"""

import uuid
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from app.state.errors import StateUnavailableError


def _adapt(value):
    return Jsonb(value) if isinstance(value, (dict, list)) else value


def _plain(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


class PsycopgStateBackend:
    def __init__(self, uri: str):
        self._uri = uri
        self.broken = False

    def _connect(self):
        if self.broken:
            raise StateUnavailableError("simulated backend outage")
        try:
            return psycopg.connect(self._uri, autocommit=True)
        except psycopg.Error as exc:
            raise StateUnavailableError(type(exc).__name__) from exc

    def insert_if_absent(self, table, row, conflict_columns) -> bool:
        cols = list(row)
        sql = (f"insert into {table} ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))}) "
               f"on conflict ({', '.join(conflict_columns)}) do nothing")
        with self._connect() as conn:
            return conn.execute(sql, [_adapt(row[c]) for c in cols]).rowcount == 1

    def select_one(self, table, match):
        where = " and ".join(f"{c} = %s" for c in match)
        with self._connect() as conn:
            cur = conn.execute(f"select * from {table} where {where} limit 1", list(match.values()))
            r = cur.fetchone()
            if r is None:
                return None
            return {d.name: _plain(v) for d, v in zip(cur.description, r)}

    def delete_expired(self, table, match, now) -> None:
        where = " and ".join(f"{c} = %s" for c in match)
        with self._connect() as conn:
            conn.execute(f"delete from {table} where {where} and expires_at <= %s", [*match.values(), now])

    def update_if(self, table, match, values) -> bool:
        sets = ", ".join(f"{c} = %s" for c in values)
        where = " and ".join(f"{c} = %s" for c in match)
        with self._connect() as conn:
            return conn.execute(f"update {table} set {sets} where {where}",
                                [*(_adapt(v) for v in values.values()), *match.values()]).rowcount == 1

    def delete(self, table, match) -> None:
        where = " and ".join(f"{c} = %s" for c in match)
        with self._connect() as conn:
            conn.execute(f"delete from {table} where {where}", list(match.values()))
