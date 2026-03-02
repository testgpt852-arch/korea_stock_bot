"""
tests/test_realtime_alert.py

reports/realtime_alert.py 단위 테스트
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• 외부 의존성 전체 unittest.mock으로 패치
• async 테스트 → IsolatedAsyncioTestCase
• ARCHITECTURE §4, §5 계약 검증

[주의] import telegram.sender as telegram_bot 처럼
       'import pkg.sub as alias' 는
       sys.modules["pkg"].sub 경유로 바인딩된다.
       → 부모 패키지를 types.ModuleType으로 만들고
         .sub 속성을 직접 설정해야 stub이 올바로 주입된다.
"""

import asyncio
import inspect
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock

# ── 프로젝트 루트를 sys.path에 추가 ─────────────────────────────
# tests/ 안에서 직접 실행하든, 루트에서 실행하든 동작하도록 보장.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ──────────────────────────────────────────────────────────────
# sys.modules stub 주입 — 반드시 realtime_alert import 전에 호출
# ──────────────────────────────────────────────────────────────

def _make_module(name: str) -> types.ModuleType:
    return types.ModuleType(name)


def _inject_stubs() -> None:
    """
    realtime_alert 가 import 하는 모든 외부 패키지·모듈을 stub으로 등록.

    핵심 원칙:
      import pkg.sub as alias   →  alias = getattr(sys.modules["pkg"], "sub")
      따라서 부모 패키지(types.ModuleType)에 .sub = stub_object 설정 필수.
    """

    # ── utils ──────────────────────────────────────────────
    utils_mod = _make_module("utils")

    logger_stub = MagicMock()
    utils_logger_stub = _make_module("utils.logger")
    utils_logger_stub.logger = logger_stub
    utils_mod.logger = utils_logger_stub

    sm_stub = _make_module("utils.state_manager")
    sm_stub.can_alert = MagicMock(return_value=True)
    sm_stub.mark_alerted = MagicMock()
    sm_stub.reset = MagicMock()
    utils_mod.state_manager = sm_stub

    ws_state_stub = _make_module("utils.watchlist_state")
    ws_state_stub.get_watchlist = MagicMock(return_value={})
    ws_state_stub.get_market_env = MagicMock(return_value="보합")
    ws_state_stub.get_sector = MagicMock(return_value="전기전자")
    ws_state_stub.clear = MagicMock()
    utils_mod.watchlist_state = ws_state_stub

    sys.modules.setdefault("utils", utils_mod)
    sys.modules.setdefault("utils.logger", utils_logger_stub)
    sys.modules.setdefault("utils.state_manager", sm_stub)
    sys.modules.setdefault("utils.watchlist_state", ws_state_stub)

    # ── analyzers ─────────────────────────────────────────
    analyzers_mod = _make_module("analyzers")

    ia_stub = _make_module("analyzers.intraday_analyzer")
    ia_stub.poll_all_markets = MagicMock(return_value=[])
    ia_stub.analyze_ws_tick = MagicMock(return_value=None)
    ia_stub.analyze_orderbook = MagicMock(return_value={})
    ia_stub.analyze_ws_orderbook_tick = MagicMock(return_value={})
    ia_stub.reset = MagicMock()
    ia_stub.set_watchlist = MagicMock()
    analyzers_mod.intraday_analyzer = ia_stub

    sys.modules.setdefault("analyzers", analyzers_mod)
    sys.modules.setdefault("analyzers.intraday_analyzer", ia_stub)

    # ── telegram ──────────────────────────────────────────
    telegram_mod = _make_module("telegram")

    ts_stub = _make_module("telegram.sender")
    ts_stub.send_async = AsyncMock()
    ts_stub.format_realtime_alert = MagicMock(return_value="<alert>")
    ts_stub.format_trade_executed = MagicMock(return_value="<executed>")
    ts_stub.format_trade_closed = MagicMock(return_value="<closed>")
    telegram_mod.sender = ts_stub

    sys.modules.setdefault("telegram", telegram_mod)
    sys.modules.setdefault("telegram.sender", ts_stub)

    # ── kis ───────────────────────────────────────────────
    kis_mod = _make_module("kis")

    ws_client_inner = MagicMock()
    ws_client_inner.connect = AsyncMock()
    ws_client_inner.disconnect = AsyncMock()
    ws_client_inner.connected = False
    ws_client_inner.subscribe = AsyncMock()
    ws_client_inner.subscribe_orderbook = AsyncMock()
    ws_client_inner.receive_loop = AsyncMock()
    ws_client_inner.subscribed_tickers = []
    ws_client_inner.subscribed_ob = []

    wsc_stub = _make_module("kis.websocket_client")
    wsc_stub.ws_client = ws_client_inner
    kis_mod.websocket_client = wsc_stub

    oc_stub = _make_module("kis.order_client")
    oc_stub.buy = MagicMock(return_value={"success": False, "message": "test"})
    kis_mod.order_client = oc_stub

    sys.modules.setdefault("kis", kis_mod)
    sys.modules.setdefault("kis.websocket_client", wsc_stub)
    sys.modules.setdefault("kis.order_client", oc_stub)

    # ── tracking ──────────────────────────────────────────
    tracking_mod = _make_module("tracking")

    ar_stub = _make_module("tracking.alert_recorder")
    ar_stub.record_alert = MagicMock()
    tracking_mod.alert_recorder = ar_stub

    sys.modules.setdefault("tracking", tracking_mod)
    sys.modules.setdefault("tracking.alert_recorder", ar_stub)

    # ── traders ───────────────────────────────────────────
    traders_mod = _make_module("traders")

    pm_stub = _make_module("traders.position_manager")
    pm_stub.can_buy = MagicMock(return_value=(True, "OK"))
    pm_stub.open_position = MagicMock()
    pm_stub.check_exit = MagicMock(return_value=[])
    pm_stub.force_close_all = MagicMock()
    pm_stub.final_close_all = MagicMock()
    traders_mod.position_manager = pm_stub

    sys.modules.setdefault("traders", traders_mod)
    sys.modules.setdefault("traders.position_manager", pm_stub)

    # ── config ────────────────────────────────────────────
    cfg_stub = types.ModuleType("config")
    cfg_stub.POLL_INTERVAL_SEC = 10
    cfg_stub.ORDERBOOK_ENABLED = False
    cfg_stub.WS_ORDERBOOK_ENABLED = False
    cfg_stub.WS_ORDERBOOK_SLOTS = 20
    cfg_stub.WS_WATCHLIST_MAX = 40
    cfg_stub.AUTO_TRADE_ENABLED = True
    cfg_stub.MIN_ENTRY_CHANGE = 3.0
    cfg_stub.MAX_ENTRY_CHANGE = 29.0
    cfg_stub.TRADING_MODE = "paper"
    sys.modules.setdefault("config", cfg_stub)


_inject_stubs()

# stub 주입 완료 후 안전하게 import
import reports.realtime_alert as realtime_alert  # noqa: E402


# ──────────────────────────────────────────────────────────────
# 편의 헬퍼 — stub 객체 접근
# ──────────────────────────────────────────────────────────────

def _telegram():
    return sys.modules["telegram.sender"]

def _alert_recorder():
    return sys.modules["tracking.alert_recorder"]

def _intraday():
    return sys.modules["analyzers.intraday_analyzer"]

def _position_manager():
    return sys.modules["traders.position_manager"]

def _watchlist_state():
    return sys.modules["utils.watchlist_state"]

def _cfg():
    return sys.modules["config"]


# ──────────────────────────────────────────────────────────────
# TestPickTypeMapping  §4 + §5 BUG (pick_type 미전달 방지)
# ──────────────────────────────────────────────────────────────

class TestPickTypeMapping(unittest.IsolatedAsyncioTestCase):
    """
    ARCHITECTURE §4 pick_type 계약
      "공시"/"테마"         → "단타"
      "순환매"/"숏스퀴즈" 및 기타 → "스윙"

    §5 BUG: open_position() pick_type 미전달 시 force_close_all 분기 불가
    → _handle_trade_signal_numeric 이 유형 → pick_type 결정 후
      _handle_trade_signal → open_position() 으로 반드시 전달하는지 확인.
    """

    async def asyncSetUp(self):
        # order_client.buy 성공 → open_position 호출 경로 확보
        oc = sys.modules["kis.order_client"]
        oc.buy.return_value = {
            "success": True,
            "buy_price": 70000,
            "qty": 1,
            "total_amt": 70000,
            "message": "",
        }
        pm = _position_manager()
        pm.can_buy.reset_mock()
        pm.can_buy.return_value = (True, "OK")
        pm.open_position.reset_mock()

        tb = _telegram()
        tb.send_async.reset_mock()
        tb.send_async.side_effect = None

        _cfg().AUTO_TRADE_ENABLED = True
        _cfg().MIN_ENTRY_CHANGE = 3.0
        _cfg().MAX_ENTRY_CHANGE = 29.0

    def _analysis(self, 유형: str) -> dict:
        return {
            "종목코드": "005930",
            "종목명": "삼성전자",
            "등락률": 5.0,
            "감지소스": "WS",
            "유형": 유형,
            "호가분석": None,
        }

    async def _run_and_get_pick_type(self, 유형: str) -> str:
        """_handle_trade_signal_numeric 실행 후 open_position 에 전달된 pick_type 반환."""
        pm = _position_manager()
        pm.open_position.reset_mock()

        await realtime_alert._handle_trade_signal_numeric(self._analysis(유형))
        # create_task 로 생성된 _handle_trade_signal 완료 대기
        await asyncio.sleep(0.05)

        if pm.open_position.called:
            kwargs = pm.open_position.call_args.kwargs
            return kwargs.get("pick_type", "__kwargs_없음__")
        return "__not_called__"

    async def test_공시_단타(self):
        pick_type = await self._run_and_get_pick_type("공시")
        self.assertEqual(pick_type, "단타", "유형='공시' → pick_type='단타' 이어야 한다 (§4)")

    async def test_테마_단타(self):
        pick_type = await self._run_and_get_pick_type("테마")
        self.assertEqual(pick_type, "단타", "유형='테마' → pick_type='단타' 이어야 한다 (§4)")

    async def test_순환매_스윙(self):
        pick_type = await self._run_and_get_pick_type("순환매")
        self.assertEqual(pick_type, "스윙", "유형='순환매' → pick_type='스윙' 이어야 한다 (§4)")

    async def test_숏스퀴즈_스윙(self):
        pick_type = await self._run_and_get_pick_type("숏스퀴즈")
        self.assertEqual(pick_type, "스윙", "유형='숏스퀴즈' → pick_type='스윙' 이어야 한다 (§4)")

    async def test_unknown_스윙(self):
        """유형 빈 문자열(미지정) → 기본값 '스윙' (§4 표준 외 값은 스윙 처리)."""
        pick_type = await self._run_and_get_pick_type("")
        self.assertEqual(pick_type, "스윙", "유형='' → pick_type 기본값 '스윙' 이어야 한다 (§4)")


# ──────────────────────────────────────────────────────────────
# TestEntryFilter  숫자 조건 필터 (_handle_trade_signal_numeric)
# ──────────────────────────────────────────────────────────────

class TestEntryFilter(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        cfg = _cfg()
        cfg.MIN_ENTRY_CHANGE = 3.0
        cfg.MAX_ENTRY_CHANGE = 29.0
        cfg.AUTO_TRADE_ENABLED = True

        pm = _position_manager()
        pm.can_buy.reset_mock()
        pm.can_buy.return_value = (True, "OK")
        pm.open_position.reset_mock()

    def _analysis(self, change_rate: float, 호가강도: str | None = None) -> dict:
        호가분석 = {"호가강도": 호가강도} if 호가강도 is not None else None
        return {
            "종목코드": "000660",
            "종목명": "SK하이닉스",
            "등락률": change_rate,
            "감지소스": "REST",
            "유형": "테마",
            "호가분석": 호가분석,
        }

    async def test_below_min_skips(self):
        """등락률 < MIN_ENTRY_CHANGE → open_position 미호출 (early return)."""
        await realtime_alert._handle_trade_signal_numeric(self._analysis(1.0))
        _position_manager().open_position.assert_not_called()

    async def test_above_max_skips(self):
        """등락률 > MAX_ENTRY_CHANGE → open_position 미호출 (early return)."""
        await realtime_alert._handle_trade_signal_numeric(self._analysis(30.0))
        _position_manager().open_position.assert_not_called()

    async def test_약세_호가강도_skips(self):
        """호가강도='약세' → open_position 미호출 (매도 우세 보류)."""
        await realtime_alert._handle_trade_signal_numeric(self._analysis(5.0, "약세"))
        _position_manager().open_position.assert_not_called()

    async def test_valid_condition_proceeds(self):
        """
        등락률 범위 내 + 호가강도 정상 → can_buy() 가 호출되어야 한다.
        can_buy=False 로 설정해 open_position 은 차단하되 can_buy 호출 여부만 검증.
        """
        pm = _position_manager()
        pm.can_buy.return_value = (False, "테스트_차단")

        await realtime_alert._handle_trade_signal_numeric(self._analysis(5.0))
        await asyncio.sleep(0.05)

        pm.can_buy.assert_called_once()

    async def test_can_buy_false_skips(self):
        """can_buy() → (False, reason) → open_position 미호출."""
        pm = _position_manager()
        pm.can_buy.return_value = (False, "포지션 한도 초과")

        await realtime_alert._handle_trade_signal_numeric(self._analysis(5.0))
        await asyncio.sleep(0.05)

        pm.open_position.assert_not_called()


# ──────────────────────────────────────────────────────────────
# TestDispatchAlerts  _dispatch_alerts
# ──────────────────────────────────────────────────────────────

class TestDispatchAlerts(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        tb = _telegram()
        tb.send_async.reset_mock()
        tb.send_async.side_effect = None
        tb.format_realtime_alert.reset_mock()
        tb.format_realtime_alert.return_value = "<alert>"

        ar = _alert_recorder()
        ar.record_alert.reset_mock()

        pm = _position_manager()
        pm.can_buy.reset_mock()
        pm.can_buy.return_value = (True, "OK")

        _cfg().AUTO_TRADE_ENABLED = False  # 기본적으로 자동매매 OFF

    def _analysis(self) -> dict:
        return {
            "종목코드": "035720",
            "종목명": "카카오",
            "등락률": 6.5,
            "감지소스": "WS",
            "유형": "테마",
            "호가분석": None,
        }

    async def test_sends_telegram(self):
        """send_async 가 1회 호출되어야 한다."""
        await realtime_alert._dispatch_alerts(self._analysis())
        _telegram().send_async.assert_called_once()

    async def test_calls_record_alert(self):
        """alert_recorder.record_alert 가 분석 dict 를 인수로 1회 호출되어야 한다."""
        analysis = self._analysis()
        await realtime_alert._dispatch_alerts(analysis)
        _alert_recorder().record_alert.assert_called_once_with(analysis)

    async def test_calls_format_realtime_alert(self):
        """format_realtime_alert 가 분석 dict 를 인수로 호출되어야 한다."""
        analysis = self._analysis()
        await realtime_alert._dispatch_alerts(analysis)
        _telegram().format_realtime_alert.assert_called_once_with(analysis)

    async def test_auto_trade_disabled_no_trade_task(self):
        """
        AUTO_TRADE_ENABLED=False 이면
        _handle_trade_signal_numeric 이 태스크로 생성되지 않아야 한다.
        (can_buy 미호출로 검증)
        """
        _cfg().AUTO_TRADE_ENABLED = False
        pm = _position_manager()
        pm.can_buy.reset_mock()

        await realtime_alert._dispatch_alerts(self._analysis())
        await asyncio.sleep(0.05)  # pending task flush

        pm.can_buy.assert_not_called()


# ──────────────────────────────────────────────────────────────
# TestHandleExitResults  _handle_exit_results
# ──────────────────────────────────────────────────────────────

class TestHandleExitResults(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        tb = _telegram()
        tb.send_async.reset_mock()
        tb.send_async.side_effect = None
        tb.format_trade_closed.reset_mock()
        tb.format_trade_closed.return_value = "<closed>"

    def _closed_list(self, n: int = 3) -> list[dict]:
        return [
            {
                "종목코드": f"00000{i}",
                "종목명": f"종목{i}",
                "close_reason": "take_profit_1",  # §4 close_reason 표준값
                "profit_rate": 5.0,
            }
            for i in range(n)
        ]

    async def test_calls_format_trade_closed(self):
        """format_trade_closed 가 청산 건수(3건)만큼 호출되어야 한다."""
        closed_list = self._closed_list(3)
        await realtime_alert._handle_exit_results(closed_list)
        self.assertEqual(_telegram().format_trade_closed.call_count, 3)

    async def test_sends_telegram_per_position(self):
        """send_async 가 청산 건수(2건)만큼 호출되어야 한다."""
        closed_list = self._closed_list(2)
        await realtime_alert._handle_exit_results(closed_list)
        self.assertEqual(_telegram().send_async.call_count, 2)

    async def test_telegram_failure_continues(self):
        """
        send_async 에서 예외가 발생해도
        나머지 청산 건이 계속 처리되어야 한다.
        (1번째만 예외, 2·3번째는 정상 처리)
        """
        tb = _telegram()
        tb.send_async.side_effect = [Exception("네트워크 오류"), None, None]

        # 예외가 외부로 전파되면 안 된다
        try:
            await realtime_alert._handle_exit_results(self._closed_list(3))
        except Exception as exc:
            self.fail(f"예외가 외부로 전파됨: {exc}")

        # format_trade_closed 는 3건 모두 호출됨
        self.assertEqual(tb.format_trade_closed.call_count, 3)
        # send_async 도 3건 시도 (1건 실패 포함)
        self.assertEqual(tb.send_async.call_count, 3)

        tb.send_async.side_effect = None  # 이후 테스트 영향 방지


# ──────────────────────────────────────────────────────────────
# TestGetRunningLoopUsage  §5 BUG-07
# ──────────────────────────────────────────────────────────────

class TestGetRunningLoopUsage(unittest.TestCase):
    """
    ARCHITECTURE §5 BUG-07:
      ❌ asyncio.get_event_loop()    — deprecated, async 컨텍스트에서 잘못된 루프 반환 가능
      ✅ asyncio.get_running_loop()  — async 컨텍스트 전용, 올바른 현재 루프 반환

    소스 코드의 실제 실행 라인(주석 줄 제외)에
    get_event_loop 호출이 없어야 한다.
    """

    def test_no_get_event_loop_in_source(self):
        source_lines = inspect.getsource(realtime_alert).splitlines()
        violations = []
        for lineno, line in enumerate(source_lines, start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # 주석 줄 제외
            if "get_event_loop" in stripped:
                violations.append((lineno, line.rstrip()))

        self.assertEqual(
            violations,
            [],
            msg=(
                "ARCHITECTURE §5 BUG-07 위반 — "
                "실행 코드에 get_event_loop() 사용 금지.\n"
                "async 컨텍스트에서는 asyncio.get_running_loop() 사용:\n"
                + "\n".join(f"  L{ln}: {code}" for ln, code in violations)
            ),
        )


if __name__ == "__main__":
    unittest.main()
