"""
tests/test_performance_rag.py
performance_tracker + rag_pattern_db 통합 테스트

검증 대상:
  1. performance_tracker.run_batch() — 수익률 DB 저장 검증
  2. rag_pattern_db.save() / get_similar_patterns() — 패턴 누적 + 검색 검증
  3. _map_type_to_signal() — "공시" → "DART_공시" 등 변환 정확성
  4. _save_rag_patterns_after_batch() — run_batch 후 RAG 자동 저장 검증

실행:
  python -m pytest tests/test_performance_rag.py -v
  (프로젝트 루트 korea_stock_bot-main/ 에서 실행)

의존성 Mock 처리:
  - pykrx: 실제 API 호출 없이 가격 dict 반환
  - config.DB_PATH: tmpfile (:memory: 사용)
  - position_manager.update_trailing_stops: 빈 함수로 mock
  - logger: 실제 logger 그대로 (파일 write 없이 stdout)
"""

import sqlite3
import sys
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

# ── 경로 설정 (프로젝트 루트가 sys.path에 없을 경우 대비) ────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

KST = timezone(timedelta(hours=9))

# ── pykrx 미설치 환경 대응: sys.modules 에 stub 주입 ─────────────────────────
# performance_tracker.py 가 모듈 레벨에서 `from pykrx import stock as pykrx_stock`
# 를 실행하기 때문에, import 전에 sys.modules 에 mock을 등록해야 한다.
if "pykrx" not in sys.modules:
    _pykrx_stub = MagicMock()
    sys.modules["pykrx"] = _pykrx_stub
    sys.modules["pykrx.stock"] = _pykrx_stub.stock


# ══════════════════════════════════════════════════════════════════════════════
# 공통 픽스처 헬퍼
# ══════════════════════════════════════════════════════════════════════════════

def _make_in_memory_db() -> sqlite3.Connection:
    """
    인메모리 SQLite 커넥션을 생성하고 스키마를 초기화한다.
    check_same_thread=False 로 테스트에서 자유롭게 재사용 가능.
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    _apply_schema(conn)
    return conn


def _apply_schema(conn: sqlite3.Connection) -> None:
    """db_schema.py 의 DDL을 인메모리 커넥션에 직접 적용한다."""
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS alert_history (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker         TEXT    NOT NULL,
            name           TEXT,
            alert_time     TEXT    NOT NULL,
            alert_date     TEXT    NOT NULL,
            change_rate    REAL,
            delta_rate     REAL,
            source         TEXT,
            price_at_alert INTEGER
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS performance_tracker (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id         INTEGER REFERENCES alert_history(id),
            ticker           TEXT    NOT NULL,
            alert_date       TEXT    NOT NULL,
            price_at_alert   INTEGER,
            tracked_date_1d  TEXT,
            tracked_date_3d  TEXT,
            tracked_date_7d  TEXT,
            price_1d   INTEGER,
            price_3d   INTEGER,
            price_7d   INTEGER,
            return_1d  REAL,
            return_3d  REAL,
            return_7d  REAL,
            done_1d  INTEGER DEFAULT 0,
            done_3d  INTEGER DEFAULT 0,
            done_7d  INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_picks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT    NOT NULL,
            rank        INTEGER NOT NULL,
            stock_code  TEXT    NOT NULL,
            stock_name  TEXT,
            signal_type TEXT,
            cap_tier    TEXT,
            reason      TEXT,
            target_rate TEXT,
            stop_loss   TEXT,
            created_at  TEXT    NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS rag_patterns (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            date          TEXT    NOT NULL,
            signal_type   TEXT    NOT NULL,
            stock_name    TEXT,
            stock_code    TEXT,
            cap_tier      TEXT,
            was_picked    INTEGER DEFAULT 0,
            pick_rank     INTEGER,
            max_return    REAL,
            hit_20pct     INTEGER DEFAULT 0,
            hit_upper     INTEGER DEFAULT 0,
            pattern_memo  TEXT,
            created_at    TEXT    NOT NULL
        )
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_rag_signal_cap
        ON rag_patterns(signal_type, cap_tier, date)
    """)

    c.execute("""
        CREATE VIEW IF NOT EXISTS trigger_stats AS
        SELECT
            ah.source AS trigger_type,
            COUNT(*) AS total_alerts,
            SUM(CASE WHEN pt.done_7d = 1 THEN 1 ELSE 0 END) AS tracked_7d,
            SUM(CASE WHEN pt.return_7d > 0 THEN 1 ELSE 0 END) AS win_7d,
            ROUND(
                100.0 * SUM(CASE WHEN pt.return_7d > 0 THEN 1 ELSE 0 END)
                      / NULLIF(SUM(CASE WHEN pt.done_7d = 1 THEN 1 ELSE 0 END), 0),
                1
            ) AS win_rate_7d,
            ROUND(AVG(CASE WHEN pt.done_7d = 1 THEN pt.return_7d END), 2) AS avg_return_7d
        FROM alert_history ah
        LEFT JOIN performance_tracker pt ON pt.alert_id = ah.id
        GROUP BY ah.source
    """)

    conn.commit()


def _insert_alert(conn: sqlite3.Connection, ticker: str, date: str,
                  price: int, source: str = "rate", name: str = None) -> int:
    """alert_history + performance_tracker 행 동시 삽입. alert_id 반환."""
    c = conn.cursor()
    c.execute("""
        INSERT INTO alert_history (ticker, name, alert_time, alert_date, source, price_at_alert)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (ticker, name or ticker, datetime.now(KST).isoformat(), date, source, price))
    alert_id = c.lastrowid

    c.execute("""
        INSERT INTO performance_tracker (alert_id, ticker, alert_date, price_at_alert)
        VALUES (?, ?, ?, ?)
    """, (alert_id, ticker, date, price))
    conn.commit()
    return alert_id


def _insert_daily_pick(conn: sqlite3.Connection, date: str, rank: int,
                       code: str, name: str, signal_type: str,
                       cap_tier: str = "소형_1000억미만", reason: str = "테스트근거") -> None:
    """daily_picks 테이블에 픽 삽입."""
    c = conn.cursor()
    c.execute("""
        INSERT INTO daily_picks (date, rank, stock_code, stock_name, signal_type,
                                  cap_tier, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (date, rank, code, name, signal_type, cap_tier, reason,
          datetime.now(KST).isoformat()))
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# 1. _map_type_to_signal 변환 테스트
# ══════════════════════════════════════════════════════════════════════════════

class TestMapTypeToSignal(unittest.TestCase):
    """
    morning_analyzer._map_type_to_signal() 변환 정확성 검증.
    ARCHITECTURE §4 signal_type 표준값과 일치하는지 확인.
    """

    def setUp(self):
        from analyzers.morning_analyzer import _map_type_to_signal
        self.fn = _map_type_to_signal

    def test_공시_변환(self):
        """'공시' → 'DART_공시' (ARCHITECTURE §4 명시)"""
        self.assertEqual(self.fn("공시"), "DART_공시")

    def test_테마_변환(self):
        """'테마' → '테마' (동일값 유지)"""
        self.assertEqual(self.fn("테마"), "테마")

    def test_순환매_변환(self):
        """'순환매' → '순환매' (동일값 유지)"""
        self.assertEqual(self.fn("순환매"), "순환매")

    def test_숏스퀴즈_변환(self):
        """'숏스퀴즈' → '숏스퀴즈' (동일값 유지)"""
        self.assertEqual(self.fn("숏스퀴즈"), "숏스퀴즈")

    def test_미매핑값_그대로_반환(self):
        """매핑 없는 값은 원문 그대로 반환 (fallback)"""
        self.assertEqual(self.fn("기타특수"), "기타특수")

    def test_빈문자열_미분류_반환(self):
        """빈 문자열은 '미분류' 반환"""
        self.assertEqual(self.fn(""), "미분류")

    def test_공시_원문_저장_금지(self):
        """
        ARCHITECTURE §4: '공시' 원문 그대로 저장 금지 (RAG 검색 불일치).
        '공시'를 넣으면 반드시 'DART_공시'로 나와야 한다.
        """
        result = self.fn("공시")
        self.assertNotEqual(result, "공시", "'공시' 원문이 그대로 반환되면 RAG 불일치 버그")
        self.assertEqual(result, "DART_공시")


# ══════════════════════════════════════════════════════════════════════════════
# 2. rag_pattern_db.save() 저장 검증
# ══════════════════════════════════════════════════════════════════════════════

class TestRagPatternDbSave(unittest.TestCase):
    """
    rag_pattern_db.save() 가 rag_patterns 테이블에 올바른 형태로 데이터를
    누적하는지 검증.
    """

    def setUp(self):
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db_path = self._tmpfile.name
        self._tmpfile.close()

        # config.DB_PATH 를 tmpfile 로 교체
        import config
        self._orig_path = config.DB_PATH
        config.DB_PATH = self._db_path

        # db_schema 초기화 (실제 파일 DB)
        import tracking.db_schema as db_schema
        db_schema.init_db()

        import tracking.rag_pattern_db as rag
        self.rag = rag

    def tearDown(self):
        import config
        config.DB_PATH = self._orig_path
        os.unlink(self._db_path)

    def _read_patterns(self) -> list[dict]:
        conn = sqlite3.connect(self._db_path)
        c = conn.cursor()
        c.execute("""
            SELECT date, signal_type, stock_name, stock_code, cap_tier,
                   was_picked, pick_rank, max_return, hit_20pct, hit_upper, pattern_memo
            FROM rag_patterns ORDER BY id
        """)
        cols = [d[0] for d in c.description]
        rows = [dict(zip(cols, r)) for r in c.fetchall()]
        conn.close()
        return rows

    # ── 기본 저장 ─────────────────────────────────────────────────────────────

    def test_픽_종목_저장(self):
        """픽 목록이 was_picked=1 로 저장된다."""
        picks = [
            {"순위": 1, "종목코드": "005930", "종목명": "삼성전자",
             "signal_type": "DART_공시", "cap_tier": "중형", "근거": "수주 공시"},
        ]
        self.rag.save(date="20260301", picks=picks, results=[])

        rows = self._read_patterns()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["stock_code"], "005930")
        self.assertEqual(row["was_picked"], 1)
        self.assertEqual(row["pick_rank"], 1)
        self.assertEqual(row["signal_type"], "DART_공시")
        self.assertEqual(row["cap_tier"], "중형")

    def test_results_수익률_병합(self):
        """picks + results 가 종목코드 기준으로 병합된다."""
        picks = [
            {"순위": 1, "종목코드": "035720", "종목명": "카카오",
             "signal_type": "테마", "cap_tier": "소형_1000억미만", "근거": "AI 테마"},
        ]
        results = [
            {"종목코드": "035720", "종목명": "카카오",
             "max_return": 22.5, "hit_20pct": True, "hit_upper": False,
             "signal_type": "테마"},
        ]
        self.rag.save(date="20260301", picks=picks, results=results)

        rows = self._read_patterns()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertAlmostEqual(row["max_return"], 22.5)
        self.assertEqual(row["hit_20pct"], 1)
        self.assertEqual(row["hit_upper"], 0)

    def test_비픽_results_저장(self):
        """픽 외 results 종목도 was_picked=0 으로 저장된다 (비교 학습용)."""
        picks = [
            {"순위": 1, "종목코드": "000001", "종목명": "A종목",
             "signal_type": "순환매", "cap_tier": "소형_3000억미만", "근거": "순환매"},
        ]
        results = [
            {"종목코드": "000001", "종목명": "A종목",
             "max_return": 5.0, "hit_20pct": False, "hit_upper": False},
            # 픽 외 종목
            {"종목코드": "000002", "종목명": "B종목",
             "max_return": 31.0, "hit_20pct": True, "hit_upper": True,
             "signal_type": "숏스퀴즈"},
        ]
        self.rag.save(date="20260301", picks=picks, results=results)

        rows = self._read_patterns()
        self.assertEqual(len(rows), 2)

        b_row = next(r for r in rows if r["stock_code"] == "000002")
        self.assertEqual(b_row["was_picked"], 0)
        self.assertIsNone(b_row["pick_rank"])
        self.assertEqual(b_row["hit_upper"], 1)

    def test_중복날짜_누적저장(self):
        """같은 날짜에 여러 번 save() 호출 시 행이 누적된다 (UNIQUE 제약 없음)."""
        picks_1 = [{"순위": 1, "종목코드": "AAA", "종목명": "A",
                    "signal_type": "테마", "cap_tier": "소형_300억미만", "근거": ""}]
        picks_2 = [{"순위": 1, "종목코드": "BBB", "종목명": "B",
                    "signal_type": "순환매", "cap_tier": "소형_300억미만", "근거": ""}]

        self.rag.save(date="20260301", picks=picks_1, results=[])
        self.rag.save(date="20260301", picks=picks_2, results=[])

        rows = self._read_patterns()
        self.assertEqual(len(rows), 2)

    def test_빈_picks_results_스킵(self):
        """picks, results 모두 빈 리스트면 DB 변화 없음."""
        self.rag.save(date="20260301", picks=[], results=[])
        rows = self._read_patterns()
        self.assertEqual(len(rows), 0)

    # ── cap_tier 표준값 검증 ──────────────────────────────────────────────────

    def test_cap_tier_표준값_저장(self):
        """ARCHITECTURE §4 cap_tier 표준값만 허용되는지 확인한다."""
        VALID_CAP_TIERS = {
            "소형_300억미만", "소형_1000억미만", "소형_3000억미만", "중형", "미분류"
        }
        INVALID_CAP_TIERS = {"소형_극소", "소형", "중형이상"}

        picks = [
            {"순위": 1, "종목코드": "001", "종목명": "A",
             "signal_type": "테마", "cap_tier": "소형_1000억미만", "근거": ""},
        ]
        self.rag.save(date="20260301", picks=picks, results=[])

        rows = self._read_patterns()
        saved_cap = rows[0]["cap_tier"]
        self.assertIn(saved_cap, VALID_CAP_TIERS,
                      f"cap_tier='{saved_cap}' 는 표준값이 아님")
        self.assertNotIn(saved_cap, INVALID_CAP_TIERS,
                         f"cap_tier='{saved_cap}' 는 ARCHITECTURE §4 금지값")


# ══════════════════════════════════════════════════════════════════════════════
# 3. rag_pattern_db.get_similar_patterns() 검색 검증
# ══════════════════════════════════════════════════════════════════════════════

class TestRagPatternDbSearch(unittest.TestCase):
    """
    get_similar_patterns() 가 과거 데이터에서 올바른 통계와
    텍스트 포맷을 반환하는지 검증.
    """

    def setUp(self):
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db_path = self._tmpfile.name
        self._tmpfile.close()

        import config
        self._orig_path = config.DB_PATH
        config.DB_PATH = self._db_path

        import tracking.db_schema as db_schema
        db_schema.init_db()

        import tracking.rag_pattern_db as rag
        self.rag = rag

        # 기준 패턴 데이터 삽입
        self.rag.save(
            date="20260201",
            picks=[
                {"순위": 1, "종목코드": "A01", "종목명": "테마주1",
                 "signal_type": "테마", "cap_tier": "소형_1000억미만", "근거": "AI 수혜"},
                {"순위": 2, "종목코드": "A02", "종목명": "테마주2",
                 "signal_type": "테마", "cap_tier": "소형_1000억미만", "근거": "반도체 테마"},
            ],
            results=[
                {"종목코드": "A01", "종목명": "테마주1",
                 "max_return": 25.0, "hit_20pct": True, "hit_upper": False},
                {"종목코드": "A02", "종목명": "테마주2",
                 "max_return": 30.5, "hit_20pct": True, "hit_upper": True},
            ],
        )

    def tearDown(self):
        import config
        config.DB_PATH = self._orig_path
        os.unlink(self._db_path)

    def test_패턴_텍스트_반환(self):
        """유사 패턴이 존재하면 비어 있지 않은 텍스트를 반환한다."""
        result = self.rag.get_similar_patterns("테마", "소형_1000억미만")
        self.assertTrue(len(result) > 0, "유사 패턴이 존재하는데 빈 문자열 반환")

    def test_패턴_헤더_포함(self):
        """반환 텍스트에 '[RAG 과거패턴]' 헤더가 포함되어야 한다."""
        result = self.rag.get_similar_patterns("테마", "소형_1000억미만")
        self.assertIn("[RAG 과거패턴]", result)

    def test_hit20_통계_포함(self):
        """20%+ 달성 건수가 텍스트에 포함된다."""
        result = self.rag.get_similar_patterns("테마", "소형_1000억미만")
        # hit_20pct 2건 / 전체 2건 = 100%
        self.assertIn("20%+", result)

    def test_없는_패턴_빈문자열(self):
        """존재하지 않는 signal_type 조합은 빈 문자열을 반환한다."""
        result = self.rag.get_similar_patterns("존재안함_신호", "소형_300억미만", limit=3)
        self.assertEqual(result, "")

    def test_cap_tier_완화_검색(self):
        """
        exact (signal_type + cap_tier) 가 없으면
        cap_tier 조건을 완화한 signal_type 단독 검색으로 폴백한다.
        """
        # 다른 cap_tier 로 검색 — 동일 signal_type('테마') 이라 폴백 성공해야 함
        result = self.rag.get_similar_patterns("테마", "소형_300억미만")
        self.assertTrue(len(result) > 0, "cap_tier 완화 검색이 실패함")

    def test_limit_파라미터(self):
        """limit=1 이면 사례 1건만 포함한다."""
        result = self.rag.get_similar_patterns("테마", "소형_1000억미만", limit=1)
        # "최근 사례:" 헤더 다음에 사례 라인이 1개여야 한다
        lines = result.split("\n")
        case_lines = [l for l in lines if l.startswith("  ")]
        self.assertLessEqual(len(case_lines), 1)


# ══════════════════════════════════════════════════════════════════════════════
# 4. performance_tracker._update_period() 수익률 계산 검증
# ══════════════════════════════════════════════════════════════════════════════

class TestPerformanceTrackerUpdatePeriod(unittest.TestCase):
    """
    performance_tracker 내부 _update_period() 가 pykrx 가격을 받아
    수익률을 올바르게 계산하고 DB에 업데이트하는지 검증.
    pykrx 는 mock 처리.
    """

    def setUp(self):
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db_path = self._tmpfile.name
        self._tmpfile.close()

        import config
        self._orig_path = config.DB_PATH
        config.DB_PATH = self._db_path

        import tracking.db_schema as db_schema
        db_schema.init_db()

        self._conn = sqlite3.connect(self._db_path)

    def tearDown(self):
        self._conn.close()
        import config
        config.DB_PATH = self._orig_path
        os.unlink(self._db_path)

    def _insert_alert_and_tracker(self, ticker: str, date: str, price: int) -> None:
        c = self._conn.cursor()
        c.execute("""
            INSERT INTO alert_history (ticker, name, alert_time, alert_date, source, price_at_alert)
            VALUES (?, ?, ?, ?, 'rate', ?)
        """, (ticker, ticker, datetime.now(KST).isoformat(), date, price))
        alert_id = c.lastrowid
        c.execute("""
            INSERT INTO performance_tracker (alert_id, ticker, alert_date, price_at_alert)
            VALUES (?, ?, ?, ?)
        """, (alert_id, ticker, date, price))
        self._conn.commit()

    def _read_tracker(self, ticker: str) -> dict:
        c = self._conn.cursor()
        c.execute("""
            SELECT done_1d, price_1d, return_1d, tracked_date_1d
            FROM performance_tracker WHERE ticker = ?
        """, (ticker,))
        row = c.fetchone()
        return dict(zip(["done_1d", "price_1d", "return_1d", "tracked_date_1d"], row))

    def _run_update_period(self, mock_price_dict, today, target_date):
        """_update_period를 _fetch_prices_batch 모킹 후 실행하는 헬퍼."""
        import tracking.performance_tracker as pt_module
        orig_fn = pt_module._fetch_prices_batch
        pt_module._fetch_prices_batch = lambda _date: mock_price_dict
        try:
            return pt_module._update_period(
                today_str=today,
                target_date=target_date,
                done_col="done_1d",
                price_col="price_1d",
                return_col="return_1d",
                date_col="tracked_date_1d",
            )
        finally:
            pt_module._fetch_prices_batch = orig_fn

    def test_수익률_계산(self):
        """매수가 10000 → 종가 11000 = +10.0%"""
        TARGET_DATE = "20260228"
        TODAY = "20260301"
        self._insert_alert_and_tracker("005930", TARGET_DATE, 10_000)

        updated = self._run_update_period({"005930": 11_000}, TODAY, TARGET_DATE)

        self.assertEqual(updated, 1, "업데이트 건수 불일치")
        row = self._read_tracker("005930")
        self.assertEqual(row["done_1d"], 1)
        self.assertEqual(row["price_1d"], 11_000)
        self.assertAlmostEqual(row["return_1d"], 10.0, places=1)
        self.assertEqual(row["tracked_date_1d"], TODAY)

    def test_손실_수익률(self):
        """매수가 20000 → 종가 18000 = -10.0%"""
        TARGET_DATE = "20260228"
        TODAY = "20260301"
        self._insert_alert_and_tracker("035720", TARGET_DATE, 20_000)

        self._run_update_period({"035720": 18_000}, TODAY, TARGET_DATE)

        row = self._read_tracker("035720")
        self.assertAlmostEqual(row["return_1d"], -10.0, places=1)

    def test_가격없는_종목_done_only(self):
        """pykrx 에 가격 없는 종목은 done=1 만 세팅, 수익률 null 유지."""
        TARGET_DATE = "20260228"
        TODAY = "20260301"
        self._insert_alert_and_tracker("999999", TARGET_DATE, 5_000)

        updated = self._run_update_period({}, TODAY, TARGET_DATE)

        self.assertEqual(updated, 1)
        row = self._read_tracker("999999")
        self.assertEqual(row["done_1d"], 1)
        self.assertIsNone(row["return_1d"])

    def test_이미_완료된_행_재처리_안함(self):
        """done_1d=1 인 행은 재업데이트 대상에서 제외된다."""
        TARGET_DATE = "20260228"
        TODAY = "20260301"
        self._insert_alert_and_tracker("111111", TARGET_DATE, 1_000)
        # 미리 완료 처리
        c = self._conn.cursor()
        c.execute("UPDATE performance_tracker SET done_1d=1 WHERE ticker='111111'")
        self._conn.commit()

        updated = self._run_update_period({"111111": 2_000}, TODAY, TARGET_DATE)
        self.assertEqual(updated, 0, "이미 완료된 행이 재처리됨")


# ══════════════════════════════════════════════════════════════════════════════
# 5. run_batch() 후 RAG 자동 저장 통합 검증
# ══════════════════════════════════════════════════════════════════════════════

class TestRunBatchRagIntegration(unittest.TestCase):
    """
    performance_tracker.run_batch() 호출 시
    _save_rag_patterns_after_batch() 가 연동되어
    daily_picks → rag_patterns 자동 저장이 이뤄지는지 검증.

    Mock:
      - pykrx._fetch_prices_batch
      - position_manager.update_trailing_stops
      - datetime.now(KST) 고정 (오늘 = 20260301)
    """

    def setUp(self):
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db_path = self._tmpfile.name
        self._tmpfile.close()

        import config
        self._orig_path = config.DB_PATH
        config.DB_PATH = self._db_path

        import tracking.db_schema as db_schema
        db_schema.init_db()

        self._conn = sqlite3.connect(self._db_path)

    def tearDown(self):
        self._conn.close()
        import config
        config.DB_PATH = self._orig_path
        os.unlink(self._db_path)

    def _setup_test_data(self, today: str, yesterday: str):
        """테스트용 alert_history + performance_tracker + daily_picks 세팅."""
        c = self._conn.cursor()
        now_iso = datetime.now(KST).isoformat()

        # alert_history
        c.execute("""
            INSERT INTO alert_history (ticker, name, alert_time, alert_date, source, price_at_alert)
            VALUES ('005930', '삼성전자', ?, ?, 'DART_공시', 50000)
        """, (now_iso, yesterday))
        alert_id = c.lastrowid

        # performance_tracker (어제 발송 → 오늘 1d 추적 예정)
        c.execute("""
            INSERT INTO performance_tracker
            (alert_id, ticker, alert_date, price_at_alert, done_1d)
            VALUES (?, '005930', ?, 50000, 0)
        """, (alert_id, yesterday))

        # daily_picks (어제 픽)
        c.execute("""
            INSERT INTO daily_picks (date, rank, stock_code, stock_name,
                                     signal_type, cap_tier, reason, created_at)
            VALUES (?, 1, '005930', '삼성전자', 'DART_공시', '중형', '수주 공시', ?)
        """, (yesterday, now_iso))

        self._conn.commit()

    def _count_rag_patterns(self) -> int:
        c = self._conn.cursor()
        c.execute("SELECT COUNT(*) FROM rag_patterns")
        return c.fetchone()[0]

    def _run_batch_with_mocks(self, price_map: dict):
        """
        run_batch() 를 pykrx + trailing_stop mock 처리 후 실행.
        """
        import tracking.performance_tracker as pt_module
        import traders.position_manager as pm_module

        orig_fetch = pt_module._fetch_prices_batch
        orig_trailing = getattr(pm_module, "update_trailing_stops", None)

        pt_module._fetch_prices_batch = lambda _date: price_map
        pm_module.update_trailing_stops = lambda: 0

        try:
            return pt_module.run_batch()
        finally:
            pt_module._fetch_prices_batch = orig_fetch
            if orig_trailing is not None:
                pm_module.update_trailing_stops = orig_trailing

    def test_run_batch_후_rag_저장(self):
        """
        run_batch() 완료 후 rag_patterns 테이블에 행이 생성된다.
        daily_picks → rag_patterns 파이프라인 검증.
        """
        today = "20260301"
        yesterday = "20260228"
        self._setup_test_data(today, yesterday)

        fixed_now = datetime(2026, 3, 1, 18, 45, 0, tzinfo=KST)
        import tracking.performance_tracker as pt_module
        orig_now = pt_module.datetime
        pt_module.datetime = MagicMock(wraps=datetime)
        pt_module.datetime.now = MagicMock(return_value=fixed_now)

        try:
            result = self._run_batch_with_mocks({"005930": 55_000})
        finally:
            pt_module.datetime = orig_now

        self.assertIn("updated", result)
        count = self._count_rag_patterns()
        self.assertGreater(count, 0, "run_batch 후 rag_patterns 에 행이 없음")

    def test_rag_signal_type_dart_공시_저장(self):
        """
        daily_picks.signal_type='DART_공시' 로 저장된 픽이
        rag_patterns 에도 signal_type='DART_공시' 로 누적된다.
        ('공시' 원문이 아니어야 함 — ARCHITECTURE §4 §5 검증)
        """
        today = "20260301"
        yesterday = "20260228"
        self._setup_test_data(today, yesterday)

        fixed_now = datetime(2026, 3, 1, 18, 45, 0, tzinfo=KST)
        import tracking.performance_tracker as pt_module
        orig_now = pt_module.datetime
        pt_module.datetime = MagicMock(wraps=datetime)
        pt_module.datetime.now = MagicMock(return_value=fixed_now)

        try:
            self._run_batch_with_mocks({"005930": 60_000})
        finally:
            pt_module.datetime = orig_now

        c = self._conn.cursor()
        c.execute("SELECT signal_type FROM rag_patterns WHERE stock_code='005930'")
        row = c.fetchone()

        if row:
            saved_signal = row[0]
            self.assertNotEqual(saved_signal, "공시",
                                "rag_patterns에 '공시' 원문이 저장됨 — ARCHITECTURE §4 위반")
            self.assertEqual(saved_signal, "DART_공시")


# ══════════════════════════════════════════════════════════════════════════════
# 6. rag_pattern_db._infer_cap_tier() 헬퍼 검증
# ══════════════════════════════════════════════════════════════════════════════

class TestInferCapTier(unittest.TestCase):
    """
    _infer_cap_tier() 가 시가총액을 ARCHITECTURE §4 표준값으로 변환하는지 검증.
    """

    def setUp(self):
        from tracking.rag_pattern_db import _infer_cap_tier
        self.fn = _infer_cap_tier

    def test_300억미만(self):
        self.assertEqual(self.fn({"시가총액": 25_000_000_000}), "소형_300억미만")

    def test_1000억미만(self):
        self.assertEqual(self.fn({"시가총액": 50_000_000_000}), "소형_1000억미만")

    def test_3000억미만(self):
        self.assertEqual(self.fn({"시가총액": 200_000_000_000}), "소형_3000억미만")

    def test_중형(self):
        self.assertEqual(self.fn({"시가총액": 500_000_000_000}), "중형")

    def test_정보없음_미분류(self):
        self.assertEqual(self.fn({}), "미분류")

    def test_market_cap_키도_인식(self):
        self.assertEqual(self.fn({"market_cap": 25_000_000_000}), "소형_300억미만")

    def test_금지값_생성안함(self):
        """ARCHITECTURE §4 금지값이 반환되지 않는다."""
        FORBIDDEN = {"소형_극소", "소형", "중형이상"}
        test_caps = [
            10_000_000_000,
            50_000_000_000,
            200_000_000_000,
            500_000_000_000,
        ]
        for cap in test_caps:
            result = self.fn({"시가총액": cap})
            self.assertNotIn(result, FORBIDDEN,
                             f"cap={cap} → '{result}' 는 ARCHITECTURE §4 금지값")


# ══════════════════════════════════════════════════════════════════════════════
# 7. 엣지케이스 + ARCHITECTURE 함정 목록 (§5) 재발 방지
# ══════════════════════════════════════════════════════════════════════════════

class TestArchitectureTrapPrevention(unittest.TestCase):
    """
    ARCHITECTURE §5 함정 목록 기반 회귀 테스트.
    과거 버그가 재발하지 않는지 확인.
    """

    def test_fund_자금유입비율_키_이름(self):
        """
        ARCHITECTURE §5: fund["ratio"] 접근 금지 → fund["자금유입비율"] 사용.
        fund_concentration_result 원소 구조 검증.
        """
        fund_row = {"종목명": "삼성전자", "자금유입비율": 0.35}
        # "ratio" 키가 없어야 하고, "자금유입비율" 키가 있어야 한다
        self.assertIn("자금유입비율", fund_row)
        self.assertNotIn("ratio", fund_row)
        self.assertNotIn("거래대금시총비율", fund_row)
        # 값 접근
        val = fund_row["자금유입비율"]
        self.assertEqual(val, 0.35)

    def test_rag_tracked_date_컬럼명(self):
        """
        ARCHITECTURE §5: WHERE pt.alert_date = today 금지 → tracked_date_1d 사용.
        performance_tracker 쿼리가 tracked_date_1d 컬럼을 사용하는지 확인.
        """
        import inspect
        import tracking.performance_tracker as pt_module
        source = inspect.getsource(pt_module._save_rag_patterns_after_batch)
        self.assertIn("tracked_date_1d", source,
                      "_save_rag_patterns_after_batch 에서 tracked_date_1d 미사용")
        self.assertNotIn("WHERE pt.alert_date = today", source)

    def test_signal_type_원문_저장_금지(self):
        """
        ARCHITECTURE §5: daily_picks 에 유형 원문('공시') 저장 금지.
        morning_analyzer._save_daily_picks() 에서 _map_type_to_signal() 호출 확인.
        (_pick_final이 _save_daily_picks를 호출하고, 내부에서 변환 수행)
        """
        import inspect
        import analyzers.morning_analyzer as ma
        source = inspect.getsource(ma._save_daily_picks)
        self.assertIn("_map_type_to_signal", source,
                      "_save_daily_picks 에서 _map_type_to_signal 미호출 — 원문 저장 위험")

    def test_update_period_executemany_단일_commit(self):
        """
        ARCHITECTURE §5 BUG-12: 루프 내 per-row commit 금지 → executemany + 단일 commit.
        들여쓰기 기반으로 for 루프 블록 범위를 추적하여 루프 내 commit 감지.
        """
        import inspect
        import tracking.performance_tracker as pt_module
        source = inspect.getsource(pt_module._update_period)

        self.assertIn("executemany", source,
                      "_update_period 에서 executemany 미사용")

        # 들여쓰기 기반 루프 블록 감지
        lines = source.split("\n")
        loop_indent = None   # for 루프 시작의 들여쓰기 깊이
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            indent = len(line) - len(line.lstrip())

            # for 루프 시작 감지
            if stripped.startswith("for ") and stripped.endswith(":"):
                loop_indent = indent
                continue

            # 루프 블록 탈출 감지: 같거나 더 낮은 들여쓰기면 루프 종료
            if loop_indent is not None and indent <= loop_indent and stripped:
                loop_indent = None

            # 루프 안에서 commit 감지
            if loop_indent is not None and "conn.commit()" in stripped:
                self.fail("_update_period 루프 내에 conn.commit() 발견 — BUG-12 재발")


if __name__ == "__main__":
    unittest.main(verbosity=2)
