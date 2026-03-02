"""
tests/test_alert_recorder.py
tracking/alert_recorder.record_alert() 단위 테스트

실행:
    python -m unittest tests.test_alert_recorder -v   # 프로젝트 루트에서
"""

import os
import sys
import types
import sqlite3
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call

# ─────────────────────────────────────────────
# sys.path: 프로젝트 루트를 최우선으로 추가
# ─────────────────────────────────────────────
_THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
_PROJ_ROOT = os.path.dirname(_THIS_DIR)
if _PROJ_ROOT not in sys.path:
    sys.path.insert(0, _PROJ_ROOT)

# ─────────────────────────────────────────────
# 외부 의존성 스텁
# ─────────────────────────────────────────────
def _install_stubs() -> None:
    if "utils" not in sys.modules:
        sys.modules["utils"] = types.ModuleType("utils")
    if "utils.logger" not in sys.modules:
        _m = types.ModuleType("utils.logger")
        class _NullLogger:
            def debug(self, *a, **k): pass
            def warning(self, *a, **k): pass
            def info(self, *a, **k): pass
            def error(self, *a, **k): pass
        _m.logger = _NullLogger()
        sys.modules["utils.logger"] = _m
    # db_schema: 실제 파일이 있지만 get_conn 은 각 테스트에서 patch
    if "tracking.db_schema" not in sys.modules:
        _db = types.ModuleType("tracking.db_schema")
        _db.get_conn = lambda: None
        sys.modules["tracking.db_schema"] = _db

_install_stubs()

import tracking.alert_recorder as ar  # noqa: E402


# ─────────────────────────────────────────────
# 헬퍼: sqlite3.Connection 래퍼
# sqlite3.Connection.close 는 read-only 슬롯이라 직접 교체 불가 →
# 위임 래퍼에서 close 를 제어
# ─────────────────────────────────────────────
KST = timezone(timedelta(hours=9))

_DDL_ALERT_HISTORY = """
CREATE TABLE IF NOT EXISTS alert_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker           TEXT,
    name             TEXT,
    alert_time       TEXT,
    alert_date       TEXT,
    change_rate      REAL,
    delta_rate       REAL,
    source           TEXT,
    price_at_alert   REAL
)
"""

_DDL_PERFORMANCE_TRACKER = """
CREATE TABLE IF NOT EXISTS performance_tracker (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id       INTEGER,
    ticker         TEXT,
    alert_date     TEXT,
    price_at_alert REAL
)
"""


class _ConnProxy:
    """sqlite3.Connection 에 close 스파이를 붙인 래퍼.

    - keep_open=True  : close() 호출 횟수를 기록하지만 실제로는 닫지 않음
                        → record_alert 실행 후에도 conn.execute() 로 조회 가능
    - keep_open=False : 호출 횟수 기록 + 실제 close 수행
    """

    def __init__(self, real_conn: sqlite3.Connection, keep_open: bool = True):
        self._conn = real_conn
        self._keep_open = keep_open
        self.close_call_count = 0

    # sqlite3.Connection 의 메서드들을 그대로 위임
    def cursor(self):       return self._conn.cursor()
    def execute(self, *a, **k): return self._conn.execute(*a, **k)
    def executemany(self, *a, **k): return self._conn.executemany(*a, **k)
    def commit(self):       return self._conn.commit()
    def rollback(self):     return self._conn.rollback()

    def close(self):
        self.close_call_count += 1
        if not self._keep_open:
            self._conn.close()


def _make_conn(keep_open: bool = True) -> "_ConnProxy":
    raw = sqlite3.connect(":memory:")
    raw.execute(_DDL_ALERT_HISTORY)
    raw.execute(_DDL_PERFORMANCE_TRACKER)
    raw.commit()
    return _ConnProxy(raw, keep_open=keep_open)


def _sample(missing: list = None, **overrides) -> dict:
    d = {
        "종목코드": "005930",
        "종목명":   "삼성전자",
        "등락률":   5.23,
        "직전대비": 1.10,
        "감지소스": "volume_surge",
        "현재가":   78_400,
    }
    d.update(overrides)
    for k in (missing or []):
        d.pop(k, None)
    return d


# ─────────────────────────────────────────────
# TestRecordAlertDbInsert
# ─────────────────────────────────────────────
class TestRecordAlertDbInsert(unittest.TestCase):
    """INSERT 실행 여부 / 반환값 / 커넥션 관리."""

    def test_inserts_alert_history(self):
        """alert_history 테이블에 정확히 1행 INSERT 되어야 한다."""
        conn = _make_conn()
        with patch.object(ar.db_schema, "get_conn", return_value=conn):
            ar.record_alert(_sample())
        rows = conn.execute("SELECT * FROM alert_history").fetchall()
        self.assertEqual(len(rows), 1)

    def test_inserts_performance_tracker(self):
        """performance_tracker 테이블에 정확히 1행 INSERT 되어야 한다."""
        conn = _make_conn()
        with patch.object(ar.db_schema, "get_conn", return_value=conn):
            ar.record_alert(_sample())
        rows = conn.execute("SELECT * FROM performance_tracker").fetchall()
        self.assertEqual(len(rows), 1)

    def test_returns_alert_id_on_success(self):
        """성공 시 양의 정수 id 를 반환해야 한다."""
        conn = _make_conn()
        with patch.object(ar.db_schema, "get_conn", return_value=conn):
            result = ar.record_alert(_sample())
        self.assertIsInstance(result, int)
        self.assertGreater(result, 0)

    def test_returns_none_on_db_failure(self):
        """get_conn() 이 예외를 던지면 None 을 반환하고 봇이 뻗지 않아야 한다."""
        with patch.object(ar.db_schema, "get_conn",
                          side_effect=Exception("connection failed")):
            result = ar.record_alert(_sample())
        self.assertIsNone(result)

    def test_conn_always_closed_on_success(self):
        """성공 경로에서도 conn.close() 가 반드시 호출되어야 한다."""
        conn = _make_conn(keep_open=True)
        with patch.object(ar.db_schema, "get_conn", return_value=conn):
            ar.record_alert(_sample())
        self.assertEqual(conn.close_call_count, 1,
                         "성공 후 conn.close() 가 정확히 1회 호출되어야 한다")

    def test_conn_always_closed(self):
        """INSERT 도중 예외가 발생해도 conn.close() 가 반드시 호출되어야 한다."""
        conn = _make_conn(keep_open=True)
        # cursor().execute() 에서 예외를 유발해 finally 경로 검증
        original_cursor = conn.cursor

        def _bad_cursor():
            c = original_cursor()
            real_execute = c.execute
            call_count = [0]
            def _failing_execute(sql, *args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:   # 첫 번째 INSERT 에서 실패
                    raise RuntimeError("simulated write error")
                return real_execute(sql, *args, **kwargs)
            c.execute = _failing_execute
            return c

        conn.cursor = _bad_cursor
        with patch.object(ar.db_schema, "get_conn", return_value=conn):
            result = ar.record_alert(_sample())

        self.assertIsNone(result)
        self.assertEqual(conn.close_call_count, 1,
                         "실패 후에도 conn.close() 가 정확히 1회 호출되어야 한다")


# ─────────────────────────────────────────────
# TestRecordAlertKeyMapping
# ─────────────────────────────────────────────
class TestRecordAlertKeyMapping(unittest.TestCase):
    """analysis 딕셔너리 키 → DB 컬럼 매핑 정확성."""

    _COLS = ["id", "ticker", "name", "alert_time", "alert_date",
             "change_rate", "delta_rate", "source", "price_at_alert"]

    def _run(self, analysis: dict) -> dict:
        conn = _make_conn()
        with patch.object(ar.db_schema, "get_conn", return_value=conn):
            ar.record_alert(analysis)
        row = conn.execute("SELECT * FROM alert_history").fetchone()
        return dict(zip(self._COLS, row))

    def test_ticker_from_종목코드(self):
        """analysis['종목코드'] 값이 DB ticker 컬럼에 저장되어야 한다."""
        row = self._run(_sample())
        self.assertEqual(row["ticker"], "005930")

    def test_name_fallback_to_ticker(self):
        """종목명 키가 없으면 ticker(종목코드) 값을 name 으로 대체해야 한다."""
        analysis = _sample(missing=["종목명"])
        row = self._run(analysis)
        self.assertEqual(row["name"], analysis["종목코드"])

    def test_change_rate_from_등락률(self):
        """analysis['등락률'] 값이 DB change_rate 컬럼에 저장되어야 한다."""
        row = self._run(_sample(등락률=7.77))
        self.assertAlmostEqual(row["change_rate"], 7.77, places=2)

    def test_source_from_감지소스(self):
        """analysis['감지소스'] 값이 DB source 컬럼에 저장되어야 한다."""
        row = self._run(_sample(감지소스="ws_tick"))
        self.assertEqual(row["source"], "ws_tick")

    def test_missing_keys_safe(self):
        """필수 키가 전혀 없는 빈 dict 를 전달해도 예외 없이 1행 삽입되어야 한다."""
        conn = _make_conn()
        with patch.object(ar.db_schema, "get_conn", return_value=conn):
            try:
                ar.record_alert({})
            except Exception as exc:
                self.fail(f"빈 dict 전달 시 예외 발생: {exc}")
        rows = conn.execute("SELECT * FROM alert_history").fetchall()
        self.assertEqual(len(rows), 1)


# ─────────────────────────────────────────────
# TestRecordAlertDateFormat
# ─────────────────────────────────────────────
class TestRecordAlertDateFormat(unittest.TestCase):
    """alert_date / alert_time 포맷 검증."""

    def _fetch_time_fields(self, fixed_dt: datetime) -> tuple:
        conn = _make_conn()
        real_datetime = datetime

        class _FakeDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_dt

        with (
            patch.object(ar.db_schema, "get_conn", return_value=conn),
            patch(f"{ar.__name__}.datetime", _FakeDatetime),
        ):
            ar.record_alert(_sample())

        row = conn.execute(
            "SELECT alert_date, alert_time FROM alert_history"
        ).fetchone()
        return row[0], row[1]

    def test_alert_date_is_yyyymmdd(self):
        """alert_date 는 'YYYYMMDD' 8자리 숫자 문자열이어야 한다."""
        fixed = datetime(2025, 7, 4, 10, 30, 0, tzinfo=KST)
        alert_date, _ = self._fetch_time_fields(fixed)
        self.assertRegex(alert_date, r"^\d{8}$",
                         f"alert_date 포맷이 YYYYMMDD 가 아님: {alert_date!r}")
        self.assertEqual(alert_date, "20250704")

    def test_alert_time_is_kst_iso(self):
        """alert_time 은 KST ISO 8601 형식(+09:00 오프셋 포함)이어야 한다."""
        fixed = datetime(2025, 7, 4, 10, 30, 0, tzinfo=KST)
        _, alert_time = self._fetch_time_fields(fixed)
        # 예: "2025-07-04T10:30:00+09:00"
        self.assertRegex(
            alert_time,
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+09:00$",
            f"alert_time ISO 8601+KST 포맷 불일치: {alert_time!r}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
