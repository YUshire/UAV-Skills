"""Bounded read-only SELECT execution against one snapshot table."""
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import time


class SnapshotError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def select(path, table, sql, parameters=(), limit=100):
    if not isinstance(sql, str) or not re.match(r"^\s*(SELECT|WITH)\b", sql, re.IGNORECASE):
        raise ValueError("sql 只接受一条 SELECT 或 WITH ... SELECT")
    if not isinstance(parameters, (list, tuple, dict)):
        raise ValueError("parameters 须为数组或命名参数对象")
    if table not in {"uav_status", "uav_resource", "geo_library"}:
        raise ValueError("不支持的快照表")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit 须为 1..100 的整数")
    deadline = time.monotonic() + 3.0

    def authorize(action, arg1, arg2, database, trigger):
        if action == sqlite3.SQLITE_READ:
            # SQLite omits the database name for a column-free table read,
            # including SELECT count(*) and SELECT 1 FROM table.
            allowed_database = database == "main" or (database is None and arg2 == "")
            return sqlite3.SQLITE_OK if arg1 == table and allowed_database else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_DENY if arg2 == "load_extension" else sqlite3.SQLITE_OK
        return sqlite3.SQLITE_OK if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE} else sqlite3.SQLITE_DENY

    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.set_authorizer(authorize)
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            cursor = db.execute(sql, parameters)
            records = cursor.fetchmany(limit + 1)
            return [dict(row) for row in records[:limit]], len(records) > limit
    except sqlite3.Error as exc:
        database_errors = {sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB,
                           sqlite3.SQLITE_IOERR, sqlite3.SQLITE_FULL, sqlite3.SQLITE_PERM}
        code = "DATABASE_ERROR" if (getattr(exc, "sqlite_errorcode", 0) & 255) in database_errors else "INVALID_ARGUMENT"
        raise SnapshotError(code, "只读查询失败：" + str(exc)) from exc
