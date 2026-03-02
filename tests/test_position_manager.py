"""
tests/test_position_manager.py
position_manager 핵심 동작 단위 테스트

[검증 항목]
  1. open_position()  — DB 정상 저장 (pick_type 포함)
  2. 손절가 계산      — AI 제공값 우선 / 기본값(-3%) 폴백 / Trailing Stop(-5%)
  3. force_close_all()— 단타만 청산, 스윙은 건드리지 않음
  4. Trailing Stop    — stop_loss 절대 하향 불가

[Mock 전략]
  - kis.order_client.sell / get_current_price (lazy import → 함수 단위 직접 패치)
  - utils.watchlist_state.get_sector / get_market_env / get_kospi_level
  - tracking.trading_journal.record_journal
  - tracking.db_schema.get_conn → tempfile SQLite
"""

import sqlite3
import sys, os, tempfile, unittest
from unittest.mock import MagicMock, patch

# 경로 설정
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ── 임시 DB ──────────────────────────────────────────────────

def _make_temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    return path


def _init_schema(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trading_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT, name TEXT, buy_time TEXT, sell_time TEXT,
            buy_price INTEGER, sell_price INTEGER, qty INTEGER,
            profit_rate REAL, profit_amount INTEGER,
            trigger_source TEXT, close_reason TEXT,
            mode TEXT DEFAULT 'VTS', buy_market_context TEXT
        );
        CREATE TABLE IF NOT EXISTS positions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            trading_id INTEGER, ticker TEXT NOT NULL, name TEXT,
            buy_time TEXT NOT NULL, buy_price INTEGER NOT NULL,
            qty INTEGER NOT NULL, trigger_source TEXT,
            mode TEXT DEFAULT 'VTS', peak_price INTEGER DEFAULT 0,
            stop_loss REAL, market_env TEXT DEFAULT '',
            sector TEXT DEFAULT '', pick_type TEXT DEFAULT 'single_day'
        );
        CREATE TABLE IF NOT EXISTS trading_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trading_id INTEGER, ticker TEXT NOT NULL, name TEXT,
            buy_time TEXT, sell_time TEXT, buy_price INTEGER, sell_price INTEGER,
            profit_rate REAL, trigger_source TEXT, close_reason TEXT, market_env TEXT,
            situation_analysis TEXT DEFAULT '{}', judgment_evaluation TEXT DEFAULT '{}',
            lessons TEXT DEFAULT '[]', pattern_tags TEXT DEFAULT '[]',
            one_line_summary TEXT, created_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()


# ── Base ─────────────────────────────────────────────────────

class BasePositionTest(unittest.TestCase):

    def setUp(self):
        self.db_path = _make_temp_db()
        _init_schema(self.db_path)

        import config as cfg
        cfg.DB_PATH                   = self.db_path
        cfg.TRADING_MODE              = "VTS"
        cfg.AUTO_TRADE_ENABLED        = True
        cfg.POSITION_MAX              = 3
        cfg.POSITION_MAX_BULL         = 5
        cfg.POSITION_MAX_NEUTRAL      = 3
        cfg.POSITION_MAX_BEAR         = 2
        cfg.POSITION_BUY_AMOUNT       = 1_000_000
        cfg.TAKE_PROFIT_1             = 5.0
        cfg.TAKE_PROFIT_2             = 10.0
        cfg.STOP_LOSS                 = -3.0
        cfg.DAILY_LOSS_LIMIT          = -3.0
        cfg.SECTOR_CONCENTRATION_MAX  = 2
        cfg.KIS_FAILURE_SAFE_LOSS_PCT = -1.5

        self._mock_sell       = MagicMock(return_value={"success": True, "sell_price": 10000})
        self._mock_get_price  = MagicMock(return_value={"현재가": 10000})
        self._mock_journal    = MagicMock()

        self.patches = [
            patch("tracking.db_schema.get_conn",
                  side_effect=lambda: sqlite3.connect(self.db_path)),
            patch("kis.order_client.sell",               self._mock_sell),
            patch("kis.order_client.get_current_price",  self._mock_get_price),
            patch("utils.watchlist_state.get_sector",     MagicMock(return_value="반도체")),
            patch("utils.watchlist_state.get_market_env", MagicMock(return_value="")),
            patch("utils.watchlist_state.get_kospi_level",MagicMock(return_value=2500.0)),
            patch("tracking.trading_journal.record_journal", self._mock_journal),
        ]
        for p in self.patches:
            p.start()

        import traders.position_manager as pm
        self.pm = pm

    def tearDown(self):
        for p in self.patches:
            p.stop()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _one(self, sql, params=()):
        conn = self._conn()
        row = conn.execute(sql, params).fetchone()
        conn.close()
        return row

    def _insert_pos(self, ticker, name, buy_price, qty,
                    pick_type="단타", market_env="",
                    stop_loss=None, peak_price=None):
        conn = self._conn()
        c = conn.cursor()
        c.execute("""INSERT INTO trading_history
            (ticker,name,buy_time,buy_price,qty,trigger_source,mode)
            VALUES (?,?,datetime('now'),?,?,'test','VTS')""",
            (ticker, name, buy_price, qty))
        tid = c.lastrowid
        sl  = stop_loss   if stop_loss   is not None else round(buy_price * 0.97)
        pk  = peak_price  if peak_price  is not None else buy_price
        c.execute("""INSERT INTO positions
            (trading_id,ticker,name,buy_time,buy_price,qty,
             trigger_source,mode,peak_price,stop_loss,market_env,pick_type)
            VALUES (?,?,?,datetime('now'),?,?,'test','VTS',?,?,?,?)""",
            (tid, ticker, name, buy_price, qty, pk, sl, market_env, pick_type))
        pid = c.lastrowid
        conn.commit()
        conn.close()
        return pid, tid


# ══════════════════════════════════════════════════════════════
# 1. open_position() — DB 저장 검증
# ══════════════════════════════════════════════════════════════

class TestOpenPosition(BasePositionTest):

    def _open(self, **kw):
        kw.setdefault("ticker",         "005930")
        kw.setdefault("name",           "삼성전자")
        kw.setdefault("buy_price",      70_000)
        kw.setdefault("qty",            10)
        kw.setdefault("trigger_source", "volume")
        return self.pm.open_position(**kw)

    def test_returns_positive_id(self):
        pid = self._open()
        self.assertIsNotNone(pid)
        self.assertGreater(pid, 0)

    def test_positions_row_saved(self):
        self._open()
        cnt = self._one("SELECT COUNT(*) FROM positions WHERE ticker='005930'")[0]
        self.assertEqual(cnt, 1)

    def test_trading_history_row_saved(self):
        self._open()
        cnt = self._one("SELECT COUNT(*) FROM trading_history WHERE ticker='005930'")[0]
        self.assertEqual(cnt, 1)

    def test_pick_type_단타(self):
        self._open(pick_type="단타")
        val = self._one("SELECT pick_type FROM positions WHERE ticker='005930'")[0]
        self.assertEqual(val, "단타")

    def test_pick_type_스윙(self):
        self._open(pick_type="스윙")
        val = self._one("SELECT pick_type FROM positions WHERE ticker='005930'")[0]
        self.assertEqual(val, "스윙")

    def test_default_pick_type_is_단타(self):
        self.pm.open_position(
            ticker="005930", name="삼성전자",
            buy_price=70_000, qty=10, trigger_source="volume")
        val = self._one("SELECT pick_type FROM positions WHERE ticker='005930'")[0]
        self.assertEqual(val, "단타")

    def test_initial_peak_equals_buy_price(self):
        self._open(buy_price=70_000)
        peak = self._one("SELECT peak_price FROM positions WHERE ticker='005930'")[0]
        self.assertEqual(peak, 70_000)

    def test_market_env_saved(self):
        self._open(market_env="강세장")
        val = self._one("SELECT market_env FROM positions WHERE ticker='005930'")[0]
        self.assertEqual(val, "강세장")

    def test_sector_saved(self):
        self._open(sector="반도체")
        val = self._one("SELECT sector FROM positions WHERE ticker='005930'")[0]
        self.assertEqual(val, "반도체")

    def test_db_error_returns_none_from_get_conn(self):
        """
        [버그 수정 검증] open_position()의 get_conn()을 try 블록 안으로 이동.
        DB 연결 자체가 실패해도 예외가 전파되지 않고 None을 반환해야 한다.
        """
        with patch("tracking.db_schema.get_conn",
                   side_effect=Exception("DB 장애")):
            result = self.pm.open_position(
                ticker="005930", name="삼성전자",
                buy_price=70_000, qty=10, trigger_source="volume")
        self.assertIsNone(result)


# ══════════════════════════════════════════════════════════════
# 2. 손절가 계산
# ══════════════════════════════════════════════════════════════

class TestStopLossCalculation(BasePositionTest):

    def _sl(self, ticker="005930"):
        return self._one("SELECT stop_loss FROM positions WHERE ticker=?", (ticker,))[0]

    # ── 기본 손절가 ──────────────────────────────────────────

    def test_default_minus3pct(self):
        """AI 손절가 미제공 → -3% (70000×0.97=67900)."""
        self.pm.open_position(ticker="005930", name="삼성전자",
                              buy_price=70_000, qty=10, trigger_source="v",
                              stop_loss_price=None)
        self.assertEqual(self._sl(), round(70_000 * 0.97))

    def test_ai_overrides_default(self):
        """AI 제공 65000 → 기본값 무시."""
        self.pm.open_position(ticker="005930", name="삼성전자",
                              buy_price=70_000, qty=10, trigger_source="v",
                              stop_loss_price=65_000)
        self.assertEqual(self._sl(), 65_000)

    def test_zero_falls_back_to_default(self):
        """stop_loss_price=0 → 기본값(-3%)."""
        self.pm.open_position(ticker="005930", name="삼성전자",
                              buy_price=70_000, qty=10, trigger_source="v",
                              stop_loss_price=0)
        self.assertEqual(self._sl(), round(70_000 * 0.97))

    def test_small_cap_rounding(self):
        """저가주 1000원 → 손절가 970 (반올림 정합성)."""
        self.pm.open_position(ticker="999999", name="소형", buy_price=1_000,
                              qty=100, trigger_source="r")
        self.assertEqual(self._sl("999999"), 970)

    # ── Trailing Stop 계산 ───────────────────────────────────

    def test_trailing_stop_bear_minus5pct(self):
        """약세장 TS = peak × 0.95 = -5%  ← ARCHITECTURE 핵심 버그 방지."""
        ts = self.pm._calc_trailing_stop(80_000, "약세장/횡보")
        self.assertEqual(ts, round(80_000 * 0.95))

    def test_trailing_stop_bull_minus8pct(self):
        """강세장 TS = peak × 0.92 = -8%."""
        ts = self.pm._calc_trailing_stop(100_000, "강세장")
        self.assertEqual(ts, round(100_000 * 0.92))

    def test_trailing_stop_no_env_uses_bear_ratio(self):
        """환경 미지정 → 보수적 약세장 비율 0.95."""
        self.assertEqual(
            self.pm._calc_trailing_stop(100_000, ""),
            self.pm._calc_trailing_stop(100_000, "약세장/횡보"))

    def test_rounding_applied(self):
        ts = self.pm._calc_trailing_stop(99_999, "약세장/횡보")
        self.assertEqual(ts, round(99_999 * 0.95))


# ══════════════════════════════════════════════════════════════
# 3. force_close_all() — 단타/스윙 분기
# ══════════════════════════════════════════════════════════════

class TestForceCloseAll(BasePositionTest):

    def setUp(self):
        super().setUp()
        import config; config.AUTO_TRADE_ENABLED = True

    def _remaining(self):
        conn = self._conn()
        rows = conn.execute(
            "SELECT ticker, pick_type FROM positions WHERE mode='VTS'"
        ).fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}

    # ── 단타 청산 ────────────────────────────────────────────

    def test_단타_is_closed(self):
        """단타 → force_close_all() 후 positions 에서 사라진다."""
        self._insert_pos("005930", "삼성전자", 70_000, 10, pick_type="단타")
        self._mock_sell.return_value      = {"success": True, "sell_price": 71_000}
        self._mock_get_price.return_value = {"현재가": 71_000}

        result = self.pm.force_close_all()

        self.assertNotIn("005930", self._remaining())
        self.assertEqual(len(result), 1)

    def test_단타_close_reason(self):
        """청산 이력의 close_reason = 'force_close'."""
        self._insert_pos("005930", "삼성전자", 70_000, 10, pick_type="단타")
        self._mock_sell.return_value      = {"success": True, "sell_price": 70_000}
        self._mock_get_price.return_value = {"현재가": 70_000}
        self.pm.force_close_all()
        row = self._one("SELECT close_reason FROM trading_history WHERE ticker='005930'")
        self.assertEqual(row[0], "force_close")

    # ── 스윙 보존 ────────────────────────────────────────────

    def test_스윙_is_preserved(self):
        """스윙 → force_close_all() 후 positions 에 남는다."""
        self._insert_pos("035720", "카카오", 50_000, 20, pick_type="스윙")
        self._mock_get_price.return_value = {"현재가": 51_000}
        result = self.pm.force_close_all()
        self.assertIn("035720", self._remaining())
        self.assertEqual(len(result), 0)

    def test_스윙_sell_not_called(self):
        """스윙만 있으면 KIS sell이 호출되지 않는다."""
        self._insert_pos("035720", "카카오", 50_000, 20, pick_type="스윙")
        self._mock_get_price.return_value = {"현재가": 51_000}
        self.pm.force_close_all()
        self._mock_sell.assert_not_called()

    # ── 혼재 시나리오 ─────────────────────────────────────────

    def test_mixed_only_단타_closed(self):
        """단타 2 + 스윙 1: 단타만 청산, 스윙 유지."""
        self._insert_pos("005930", "삼성전자",  70_000, 10, pick_type="단타")
        self._insert_pos("000660", "SK하이닉스",120_000,  5, pick_type="단타")
        self._insert_pos("035720", "카카오",    50_000, 20, pick_type="스윙")
        self._mock_sell.return_value      = {"success": True, "sell_price": 70_000}
        self._mock_get_price.return_value = {"현재가": 70_000}

        result = self.pm.force_close_all()
        rem = self._remaining()

        self.assertIn("035720", rem)         # 스윙 잔존
        self.assertNotIn("005930", rem)      # 단타 삭제
        self.assertNotIn("000660", rem)      # 단타 삭제
        self.assertEqual(len(result), 2)

    def test_only_swings_empty_result(self):
        self._insert_pos("035720", "카카오",   50_000, 20, pick_type="스윙")
        self._insert_pos("000660", "SK하이닉스",120_000, 5, pick_type="스윙")
        self.assertEqual(self.pm.force_close_all(), [])

    def test_no_positions_empty_result(self):
        self.assertEqual(self.pm.force_close_all(), [])

    def test_disabled_no_close(self):
        """AUTO_TRADE_ENABLED=False → 단타도 청산 안 함."""
        import config; config.AUTO_TRADE_ENABLED = False
        self._insert_pos("005930", "삼성전자", 70_000, 10, pick_type="단타")
        self.assertEqual(self.pm.force_close_all(), [])
        self.assertIn("005930", self._remaining())

    def test_profit_rate_accuracy(self):
        """청산 후 profit_rate 계산 (70000→71400 ≈ +2.0%)."""
        self._insert_pos("005930", "삼성전자", 70_000, 10, pick_type="단타")
        self._mock_sell.return_value      = {"success": True, "sell_price": 71_400}
        self._mock_get_price.return_value = {"현재가": 71_400}
        self.pm.force_close_all()
        row = self._one("SELECT profit_rate FROM trading_history WHERE ticker='005930'")
        self.assertAlmostEqual(row[0], 2.0, places=1)


# ══════════════════════════════════════════════════════════════
# 4. Trailing Stop — 절대 하향 불가
# ══════════════════════════════════════════════════════════════

class TestTrailingStopNeverDecreases(BasePositionTest):

    def test_update_peak_never_lowers_stop_loss(self):
        """
        _update_peak(new_stop < current_stop) → DB stop_loss 변경 없음.
        SQL MAX(stop_loss, ?) 구문 검증.
        """
        pid, _ = self._insert_pos(
            "005930", "삼성전자", 70_000, 10,
            stop_loss=67_000, peak_price=70_000)

        self.pm._update_peak(pid, 70_000, 65_000)   # 65000 < 67000 → 무시

        sl = self._one("SELECT stop_loss FROM positions WHERE id=?", (pid,))[0]
        self.assertGreaterEqual(sl, 67_000,
            f"stop_loss가 기존(67000)보다 낮아졌음: {sl}")

    def test_update_peak_raises_when_higher(self):
        """새 stop이 기존보다 높을 때 상향 반영."""
        pid, _ = self._insert_pos(
            "005930", "삼성전자", 70_000, 10,
            stop_loss=67_000, peak_price=70_000)

        self.pm._update_peak(pid, 78_000, 72_000)

        row = self._one("SELECT stop_loss, peak_price FROM positions WHERE id=?", (pid,))
        self.assertEqual(row[0], 72_000)
        self.assertEqual(row[1], 78_000)

    def test_batch_update_never_lowers(self):
        """
        update_trailing_stops() 배치:
        peak=105000, stop=98000, 현재가=103000, 약세장
        → new_stop = round(105000 × 0.95) = 99750 (상향)
        """
        self._insert_pos("005930", "삼성전자", 100_000, 10,
                         stop_loss=98_000, peak_price=105_000,
                         market_env="약세장/횡보")
        self._mock_get_price.return_value = {"현재가": 103_000}

        updated = self.pm.update_trailing_stops()

        row   = self._one("SELECT stop_loss, peak_price FROM positions WHERE ticker='005930'")
        sl, _ = row
        expected = round(max(105_000, 103_000) * 0.95)  # 99750

        self.assertGreaterEqual(updated, 1)
        self.assertGreaterEqual(sl, 98_000,
            f"stop_loss가 기존(98000)보다 낮아짐: {sl}")
        self.assertEqual(sl, expected,
            f"예상 trailing_stop={expected}, 실제={sl}")

    def test_stop_loss_monotone_non_decreasing(self):
        """
        가격 상승↔하락 반복 — stop_loss는 단조 비감소.
        어느 순간에도 이전 값보다 낮아지는 일 없어야 함 (핵심 버그 방지).
        """
        self._insert_pos("005930", "삼성전자", 100_000, 10,
                         stop_loss=round(100_000 * 0.97),
                         peak_price=100_000, market_env="약세장/횡보")

        prices    = [102_000, 110_000, 108_000, 115_000, 112_000, 120_000]
        prev_stop = round(100_000 * 0.97)

        for price in prices:
            self._mock_get_price.return_value = {"현재가": price}
            self.pm.update_trailing_stops()
            sl = self._one(
                "SELECT stop_loss FROM positions WHERE ticker='005930'"
            )[0]
            self.assertGreaterEqual(
                sl, prev_stop,
                f"가격={price}일 때 stop_loss({sl})가 이전({prev_stop})보다 낮아짐! "
                f"← ARCHITECTURE 핵심 버그"
            )
            prev_stop = sl

    def test_peak_price_never_decreases(self):
        """현재가 < peak → peak_price 변경 없음."""
        self._insert_pos("005930", "삼성전자", 100_000, 10,
                         stop_loss=97_000, peak_price=105_000,
                         market_env="약세장/횡보")
        self._mock_get_price.return_value = {"현재가": 103_000}

        self.pm.update_trailing_stops()

        pk = self._one("SELECT peak_price FROM positions WHERE ticker='005930'")[0]
        self.assertEqual(pk, 105_000,
            f"현재가(103000) < peak(105000)인데 peak 변경됨: {pk}")


# ══════════════════════════════════════════════════════════════
# 5. _calc_trailing_stop() 파라미터화 검증
# ══════════════════════════════════════════════════════════════

class TestCalcTrailingStopTable(BasePositionTest):

    CASES = [
        (100_000, "강세장",     0.92),
        (100_000, "약세장/횡보",0.95),
        (100_000, "횡보",       0.95),
        (100_000, "",           0.95),
        (200_000, "강세장",     0.92),
        (200_000, "약세장/횡보",0.95),
        (1_000,   "약세장/횡보",0.95),
    ]

    def test_ratio_table(self):
        for peak, env, ratio in self.CASES:
            with self.subTest(peak=peak, env=env):
                ts = self.pm._calc_trailing_stop(peak, env)
                self.assertEqual(ts, round(peak * ratio),
                    f"peak={peak}, env={env!r}: 기대={round(peak*ratio)}, 실제={ts}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ══════════════════════════════════════════════════════════════
# 6. DB 연결 실패 — 모든 공개 함수 안전 반환 검증
#    get_conn()이 try 블록 안에 있어야만 통과하는 테스트
# ══════════════════════════════════════════════════════════════

class TestDbConnFailure(BasePositionTest):
    """
    DB 연결(get_conn) 자체가 실패할 때 각 함수가
    예외를 전파하지 않고 안전한 기본값을 반환하는지 검증.
    position_manager.py 의 모든 get_conn() 호출이 try 블록 안에
    위치해야만 이 테스트들이 통과한다.
    """

    def _fail_conn(self):
        return patch("tracking.db_schema.get_conn",
                     side_effect=Exception("DB 연결 불가"))

    def test_open_position_returns_none_on_conn_failure(self):
        """get_conn 실패 → None 반환 (예외 전파 없음)."""
        with self._fail_conn():
            result = self.pm.open_position(
                ticker="005930", name="삼성전자",
                buy_price=70_000, qty=10, trigger_source="volume")
        self.assertIsNone(result)

    def test_can_buy_returns_false_on_conn_failure(self):
        """get_conn 실패 → (False, 오류메시지) 반환."""
        with self._fail_conn():
            ok, msg = self.pm.can_buy("005930")
        self.assertFalse(ok)
        self.assertIn("오류", msg)

    def test_check_exit_returns_empty_on_conn_failure(self):
        """get_conn 실패 → 빈 리스트 반환."""
        with self._fail_conn():
            result = self.pm.check_exit()
        self.assertEqual(result, [])

    def test_update_trailing_stops_returns_zero_on_conn_failure(self):
        """get_conn 실패 → 0 반환."""
        with self._fail_conn():
            result = self.pm.update_trailing_stops()
        self.assertEqual(result, 0)

    def test_update_trailing_stops_batch_conn2_failure(self):
        """
        pending 일괄 커밋 시 conn2 get_conn 실패 → 예외 없이 0 반환.
        update_trailing_stops의 두 번째 get_conn(conn2) 검증.
        """
        self._insert_pos("005930", "삼성전자", 100_000, 10,
                         stop_loss=97_000, peak_price=100_000,
                         market_env="약세장/횡보")
        self._mock_get_price.return_value = {"현재가": 105_000}
        call_count = 0

        def selective_fail():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return sqlite3.connect(self.db_path)   # 첫 번째(읽기)는 정상
            raise Exception("conn2 장애")               # 두 번째(쓰기)는 실패

        with patch("tracking.db_schema.get_conn", side_effect=selective_fail):
            result = self.pm.update_trailing_stops()
        self.assertEqual(result, 0)

    def test_get_open_positions_returns_empty_on_conn_failure(self):
        """get_conn 실패 → 빈 리스트 반환."""
        with self._fail_conn():
            result = self.pm.get_open_positions()
        self.assertEqual(result, [])

    def test_update_peak_silent_on_conn_failure(self):
        """get_conn 실패 → 예외 없이 조용히 실패, DB 변경 없음."""
        pid, _ = self._insert_pos("005930", "삼성전자", 70_000, 10)
        with self._fail_conn():
            result = self.pm._update_peak(pid, 75_000, 69_000)
        self.assertIsNone(result)
        pk = self._one("SELECT peak_price FROM positions WHERE id=?", (pid,))[0]
        self.assertEqual(pk, 70_000)

    def test_force_close_all_returns_empty_on_conn_failure(self):
        """get_conn 실패 → 빈 리스트 반환 (예외 없음)."""
        with self._fail_conn():
            result = self.pm.force_close_all()
        self.assertEqual(result, [])
