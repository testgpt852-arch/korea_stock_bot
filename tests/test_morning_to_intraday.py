"""
tests/test_morning_to_intraday.py
아침봇 → 장중봇 연결 통합 테스트

검증 항목:
  [T1] picks 15종목이 WebSocket 워치리스트에 정확히 들어가는지
  [T2] 유형(공시/테마/순환매/숏스퀴즈) → pick_type(단타/스윙) 변환 정확성
  [T3] intraday_analyzer가 픽 외 종목 알림을 무시하는지 (analyze_ws_tick / poll_all_markets)
  [T4] set_watchlist → get_watchlist 상태 전파 일관성
  [T5] 종목코드 유효성 검사 (6자리 아닌 코드 필터링)
  [T6] 전일거래량 fallback (price_data 없는 종목 → 1)
  [T7] poll_all_markets 워밍업 사이클 — 첫 폴에서 알림 없음
  [T8] poll_all_markets 픽 외 종목 REST 호출 차단 (KIS mock 검증)

실행 방법:
    cd korea_stock_bot-main
    python -m pytest tests/test_morning_to_intraday.py -v
"""

import sys
import os
import types
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch, call
from typing import Any

# ── 프로젝트 루트를 sys.path에 추가 ──────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ═══════════════════════════════════════════════════════════════════
# Stub 모듈 주입 — 실제 API/DB/텔레그램 호출 차단
# ═══════════════════════════════════════════════════════════════════

def _make_stub(name: str, **attrs) -> types.ModuleType:
    """최소 stub 모듈 생성 헬퍼"""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


# config stub — 장중봇 판단 임계값
_config_stub = _make_stub(
    "config",
    PRICE_CHANGE_MIN      = 3.0,
    MAX_CATCH_RATE        = 29.0,
    PRICE_DELTA_MIN       = 1.5,
    VOLUME_DELTA_MIN      = 50.0,
    CONFIRM_CANDLES       = 2,
    ORDERBOOK_ENABLED     = True,
    ORDERBOOK_BID_ASK_GOOD= 2.0,
    ORDERBOOK_BID_ASK_MIN = 1.3,
    ORDERBOOK_TOP3_RATIO_MIN = 0.5,
    MIN_CHANGE_RATE       = 1.0,
    WS_ORDERBOOK_SLOTS    = 20,
    WS_WATCHLIST_MAX      = 40,
)
sys.modules["config"] = _config_stub

# logger stub
_logger_stub = MagicMock()
_logger_mod   = _make_stub("utils.logger", logger=_logger_stub)

# utils 패키지 stub — __path__ 필요 (reload 시 parent.__path__ 참조)
_utils_pkg = types.ModuleType("utils")
_utils_pkg.__path__ = []   # 패키지로 인식되도록 __path__ 추가
_utils_pkg.__package__ = "utils"
sys.modules["utils"]        = _utils_pkg
sys.modules["utils.logger"] = _logger_mod

# kis stub (REST/WebSocket 호출 차단)
_kis_pkg           = types.ModuleType("kis")
_kis_pkg.__path__  = []
sys.modules["kis"]                 = _kis_pkg
_rest_client_stub  = _make_stub("kis.rest_client",
                                get_stock_price=MagicMock(return_value=None),
                                get_orderbook  =MagicMock(return_value=None))
_ws_client_stub    = _make_stub("kis.websocket_client")
sys.modules["kis.rest_client"]     = _rest_client_stub
sys.modules["kis.websocket_client"]= _ws_client_stub
sys.modules["kis.auth"]            = _make_stub("kis.auth")
sys.modules["kis.order_client"]    = _make_stub("kis.order_client")

# telegram stub
_tg_pkg = types.ModuleType("telegram")
_tg_pkg.__path__ = []
sys.modules["telegram"]         = _tg_pkg
sys.modules["telegram.sender"]  = _make_stub("telegram.sender", send=MagicMock())

# 나머지 패키지/모듈 stub
for _pkg in ["collectors", "analyzers", "reports", "tracking", "traders"]:
    _m = types.ModuleType(_pkg)
    _m.__path__ = []
    sys.modules[_pkg] = _m

for _m in [
    "collectors.data_collector",
    "analyzers.morning_analyzer",
    "reports.morning_report",
    "tracking.db_schema",
    "traders.position_manager",
    "utils.date_utils", "utils.state_manager",
]:
    if _m not in sys.modules:
        sys.modules[_m] = _make_stub(_m)


def _load_real_module(mod_name: str, file_path: str, package: str = "") -> types.ModuleType:
    """실제 .py 파일을 sys.modules stub 환경에서 안전하게 로드"""
    spec = _ilu.spec_from_file_location(mod_name, file_path)
    mod  = _ilu.module_from_spec(spec)
    if package:
        mod.__package__ = package
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── utils.watchlist_state 실제 모듈 로드 ─────────────────────────
import importlib.util as _ilu

watchlist_state = _load_real_module(
    "utils.watchlist_state",
    os.path.join(ROOT, "utils", "watchlist_state.py"),
    package="utils",
)

# ── analyzers.intraday_analyzer 실제 모듈 로드 ───────────────────
intraday_analyzer = _load_real_module(
    "analyzers.intraday_analyzer",
    os.path.join(ROOT, "analyzers", "intraday_analyzer.py"),
    package="analyzers",
)

# morning_report._build_ws_watchlist_from_picks 는 독립 함수이므로 직접 임포트
# (morning_report 전체 임포트 없이 필요한 함수만 추출)
def _build_ws_watchlist_from_picks(picks: list[dict], price_data: dict | None) -> dict:
    """
    morning_report._build_ws_watchlist_from_picks 의 동일 로직 인라인 복사.
    실제 파일 변경 시 이 함수도 동기화 필요.
    """
    by_code: dict = (price_data or {}).get("by_code", {})
    watchlist: dict = {}
    for pick in picks:
        code = pick.get("종목코드", "")
        if not code or len(code) != 6:
            continue
        watchlist[code] = {
            "종목명":     pick.get("종목명", ""),
            "전일거래량": max(by_code.get(code, {}).get("거래량", 1), 1),
            "우선순위":   1,
            "유형":       pick.get("유형", ""),
        }
    return watchlist


# ── 테스트 픽 픽스처 ─────────────────────────────────────────────

_TYPES_MAP = [
    ("공시",    "단타"),
    ("테마",    "단타"),
    ("순환매",  "스윙"),
    ("숏스퀴즈","스윙"),
]

def _make_picks(n: int = 15) -> list[dict]:
    """n개의 테스트 픽 생성 (유형을 순환하며 할당)"""
    types_cycle = [t for t, _ in _TYPES_MAP]
    picks = []
    for i in range(n):
        code = f"{100001 + i:06d}"
        유형  = types_cycle[i % len(types_cycle)]
        picks.append({
            "순위":      i + 1,
            "종목코드":  code,
            "종목명":    f"테스트종목{i+1:02d}",
            "유형":      유형,
            "근거":      f"테스트 근거 {i+1}",
            "목표등락률": "15%",
            "손절기준":  "-5%",
            "테마여부":  True,
            "매수시점":  "09:00~09:30",
            "cap_tier": "소형_1000억미만",
        })
    return picks


def _make_price_data(picks: list[dict]) -> dict:
    """picks 기반 price_data 픽스처 생성"""
    by_code = {
        p["종목코드"]: {"거래량": 500_000 + i * 10_000}
        for i, p in enumerate(picks)
    }
    return {"by_code": by_code}


# ═══════════════════════════════════════════════════════════════════
# [T1] WebSocket 워치리스트 정확성
# ═══════════════════════════════════════════════════════════════════

class TestWsWatchlistBuild(unittest.TestCase):
    """[T1] _build_ws_watchlist_from_picks → 15종목 정확히 포함"""

    def setUp(self):
        self.picks      = _make_picks(15)
        self.price_data = _make_price_data(self.picks)

    def test_all_15_picks_in_watchlist(self):
        """픽 15종목 전부 워치리스트에 등록되는지"""
        ws = _build_ws_watchlist_from_picks(self.picks, self.price_data)
        self.assertEqual(len(ws), 15, f"워치리스트 크기 불일치: {len(ws)}")

    def test_pick_codes_match_exactly(self):
        """워치리스트 종목코드가 픽과 완전 일치하는지"""
        ws          = _build_ws_watchlist_from_picks(self.picks, self.price_data)
        pick_codes  = {p["종목코드"] for p in self.picks}
        ws_codes    = set(ws.keys())
        self.assertEqual(ws_codes, pick_codes)

    def test_watchlist_contains_required_fields(self):
        """워치리스트 각 항목에 필수 필드(종목명, 전일거래량, 유형) 존재하는지"""
        ws = _build_ws_watchlist_from_picks(self.picks, self.price_data)
        for code, info in ws.items():
            with self.subTest(code=code):
                self.assertIn("종목명",     info, f"{code} 종목명 누락")
                self.assertIn("전일거래량", info, f"{code} 전일거래량 누락")
                self.assertIn("유형",       info, f"{code} 유형 누락")

    def test_유형_carried_into_watchlist(self):
        """픽의 유형이 워치리스트에 그대로 전달되는지"""
        ws = _build_ws_watchlist_from_picks(self.picks, self.price_data)
        for pick in self.picks:
            code = pick["종목코드"]
            self.assertEqual(ws[code]["유형"], pick["유형"],
                             f"{code} 유형 불일치")

    def test_전일거래량_from_price_data(self):
        """전일거래량이 price_data["by_code"]에서 정확히 로드되는지"""
        ws = _build_ws_watchlist_from_picks(self.picks, self.price_data)
        by_code = self.price_data["by_code"]
        for code, info in ws.items():
            expected = by_code[code]["거래량"]
            self.assertEqual(info["전일거래량"], expected,
                             f"{code} 전일거래량 불일치")


# ═══════════════════════════════════════════════════════════════════
# [T2] pick_type(단타/스윙) 변환 정확성
# ═══════════════════════════════════════════════════════════════════

_DAYTRADING = {"공시", "테마"}   # realtime_alert 내부 상수 미러링

def _resolve_pick_type(유형: str) -> str:
    """realtime_alert 변환 로직 미러링"""
    return "단타" if 유형 in _DAYTRADING else "스윙"


class TestPickTypeMapping(unittest.TestCase):
    """[T2] 유형 → pick_type 변환 규칙 검증"""

    def test_공시_is_단타(self):
        self.assertEqual(_resolve_pick_type("공시"), "단타")

    def test_테마_is_단타(self):
        self.assertEqual(_resolve_pick_type("테마"), "단타")

    def test_순환매_is_스윙(self):
        self.assertEqual(_resolve_pick_type("순환매"), "스윙")

    def test_숏스퀴즈_is_스윙(self):
        self.assertEqual(_resolve_pick_type("숏스퀴즈"), "스윙")

    def test_unknown_type_defaults_to_스윙(self):
        """ARCHITECTURE 미정의 유형 → 기본값 스윙 (안전 방향)"""
        self.assertEqual(_resolve_pick_type("알수없는유형"), "스윙")

    def test_empty_type_defaults_to_스윙(self):
        self.assertEqual(_resolve_pick_type(""), "스윙")

    def test_all_types_in_picks_correct(self):
        """픽 15종목 전체 유형→pick_type 일괄 검증"""
        expected = {
            "공시":    "단타",
            "테마":    "단타",
            "순환매":  "스윙",
            "숏스퀴즈":"스윙",
        }
        for 유형, 기대_pick_type in expected.items():
            with self.subTest(유형=유형):
                self.assertEqual(_resolve_pick_type(유형), 기대_pick_type)

    def test_force_close_all_excludes_스윙(self):
        """
        [ARCHITECTURE §4 계약] force_close_all → 단타만 청산, 스윙 제외.
        pick_type 변환 후 단타/스윙 분리 시뮬레이션.
        """
        picks = _make_picks(15)
        단타_picks = [p for p in picks if _resolve_pick_type(p["유형"]) == "단타"]
        스윙_picks = [p for p in picks if _resolve_pick_type(p["유형"]) == "스윙"]

        # 강제청산 대상: 단타만
        force_close_targets = [p["종목코드"] for p in 단타_picks]
        swing_codes         = {p["종목코드"] for p in 스윙_picks}

        self.assertTrue(len(force_close_targets) > 0, "단타 종목이 없음")
        self.assertTrue(len(swing_codes) > 0,         "스윙 종목이 없음")
        # 강제청산 대상에 스윙 종목 없어야 함
        self.assertTrue(swing_codes.isdisjoint(set(force_close_targets)),
                        "스윙 종목이 강제청산 대상에 포함됨")


# ═══════════════════════════════════════════════════════════════════
# [T3] intraday_analyzer — 픽 외 종목 무시 검증
# ═══════════════════════════════════════════════════════════════════

class TestIntradayAnalyzerWatchlistFilter(unittest.TestCase):
    """[T3] analyze_ws_tick / poll_all_markets — 픽 외 종목 알림 차단"""

    def setUp(self):
        self.picks = _make_picks(5)  # 테스트용 5종목
        intraday_analyzer.reset()
        intraday_analyzer.set_watchlist(self.picks)

    def tearDown(self):
        intraday_analyzer.reset()

    # ── analyze_ws_tick ───────────────────────────────────────────

    def test_non_pick_ticker_returns_none(self):
        """픽에 없는 종목코드 → analyze_ws_tick → None 반환"""
        non_pick_ticker = "999999"
        # 해당 코드가 픽에 없는지 확인
        pick_codes = {p["종목코드"] for p in self.picks}
        self.assertNotIn(non_pick_ticker, pick_codes)

        tick = {
            "종목코드":   non_pick_ticker,
            "종목명":     "외부종목",
            "등락률":     10.0,
            "누적거래량": 1_000_000,
        }
        result = intraday_analyzer.analyze_ws_tick(tick, prdy_vol=500_000)
        self.assertIsNone(result,
            f"픽 외 종목({non_pick_ticker})이 None이 아닌 값 반환: {result}")

    def test_pick_ticker_can_return_alert(self):
        """픽 종목이고 조건 충족 시 → analyze_ws_tick → None이 아닌 값 반환"""
        pick = self.picks[0]
        ticker = pick["종목코드"]

        tick = {
            "종목코드":   ticker,
            "종목명":     pick["종목명"],
            "등락률":     10.0,    # PRICE_CHANGE_MIN(3.0) 이상
            "누적거래량": 1_000_000,
            "체결시각":   "091500",
        }
        result = intraday_analyzer.analyze_ws_tick(tick, prdy_vol=500_000)
        self.assertIsNotNone(result,
            f"픽 종목({ticker}) 조건 충족인데 None 반환")
        self.assertEqual(result["종목코드"], ticker)

    def test_multiple_non_pick_tickers_all_ignored(self):
        """여러 픽 외 종목 → 모두 None"""
        pick_codes = {p["종목코드"] for p in self.picks}
        non_pick_codes = [f"{900000 + i:06d}" for i in range(5)]
        for code in non_pick_codes:
            self.assertNotIn(code, pick_codes)

        for code in non_pick_codes:
            with self.subTest(ticker=code):
                tick = {"종목코드": code, "등락률": 15.0, "누적거래량": 1_000_000}
                result = intraday_analyzer.analyze_ws_tick(tick, prdy_vol=500_000)
                self.assertIsNone(result, f"{code} 필터링 실패")

    def test_below_price_change_min_returns_none(self):
        """픽 종목이지만 등락률이 PRICE_CHANGE_MIN 미만 → None"""
        pick = self.picks[0]
        tick = {
            "종목코드":   pick["종목코드"],
            "등락률":     1.0,    # PRICE_CHANGE_MIN(3.0) 미만
            "누적거래량": 1_000_000,
        }
        result = intraday_analyzer.analyze_ws_tick(tick, prdy_vol=500_000)
        self.assertIsNone(result)

    def test_above_max_catch_rate_returns_none(self):
        """픽 종목이지만 등락률이 MAX_CATCH_RATE 초과 → None (상한가 근접 추격 방지)"""
        pick = self.picks[0]
        tick = {
            "종목코드":   pick["종목코드"],
            "등락률":     30.0,   # MAX_CATCH_RATE(29.0) 초과
            "누적거래량": 1_000_000,
        }
        result = intraday_analyzer.analyze_ws_tick(tick, prdy_vol=500_000)
        self.assertIsNone(result)

    # ── poll_all_markets — KIS REST 호출 대상 검증 ─────────────────

    def test_poll_only_calls_get_stock_price_for_picks(self):
        """[T8] poll_all_markets가 픽 종목코드만 get_stock_price 호출하는지"""
        pick_codes = [p["종목코드"] for p in self.picks]

        # get_stock_price: 기본값 None → 워밍업 사이클
        _rest_client_stub.get_stock_price.reset_mock()
        _rest_client_stub.get_stock_price.return_value = None

        with patch("kis.rest_client.get_stock_price",
                   _rest_client_stub.get_stock_price):
            intraday_analyzer.poll_all_markets()

        called_codes = [
            c.args[0]
            for c in _rest_client_stub.get_stock_price.call_args_list
        ]
        # 호출된 코드가 모두 픽 코드인지
        non_pick_called = [c for c in called_codes if c not in pick_codes]
        self.assertEqual(non_pick_called, [],
            f"픽 외 종목에 REST 호출 발생: {non_pick_called}")

    def test_poll_calls_exactly_pick_count(self):
        """poll_all_markets가 픽 수만큼만 REST 호출하는지"""
        _rest_client_stub.get_stock_price.reset_mock()
        _rest_client_stub.get_stock_price.return_value = None

        with patch("kis.rest_client.get_stock_price",
                   _rest_client_stub.get_stock_price):
            intraday_analyzer.poll_all_markets()

        call_count = _rest_client_stub.get_stock_price.call_count
        self.assertEqual(call_count, len(self.picks),
            f"REST 호출 횟수 불일치 — 기대:{len(self.picks)} 실제:{call_count}")


# ═══════════════════════════════════════════════════════════════════
# [T4] set_watchlist → get_watchlist 상태 전파 일관성
# ═══════════════════════════════════════════════════════════════════

class TestWatchlistStateFlow(unittest.TestCase):
    """[T4] 아침봇 → 장중봇 상태 전파 검증"""

    def setUp(self):
        watchlist_state.clear()
        intraday_analyzer.reset()

    def tearDown(self):
        watchlist_state.clear()
        intraday_analyzer.reset()

    def test_intraday_watchlist_reflects_morning_picks(self):
        """intraday set_watchlist 후 get_watchlist 동일 종목 반환"""
        picks = _make_picks(15)
        intraday_analyzer.set_watchlist(picks)

        returned = intraday_analyzer.get_watchlist()
        self.assertEqual(len(returned), 15)

        pick_codes = {p["종목코드"] for p in picks}
        ret_codes  = {p["종목코드"] for p in returned}
        self.assertEqual(pick_codes, ret_codes)

    def test_watchlist_state_flow(self):
        """watchlist_state.set_watchlist → get_watchlist 흐름"""
        picks      = _make_picks(15)
        price_data = _make_price_data(picks)
        ws_wl      = _build_ws_watchlist_from_picks(picks, price_data)

        watchlist_state.set_watchlist(ws_wl)
        retrieved = watchlist_state.get_watchlist()

        self.assertEqual(len(retrieved), 15)
        self.assertEqual(set(retrieved.keys()), {p["종목코드"] for p in picks})

    def test_reset_clears_intraday_watchlist(self):
        """intraday reset() 후 get_watchlist 빈 리스트 반환"""
        intraday_analyzer.set_watchlist(_make_picks(5))
        intraday_analyzer.reset()
        self.assertEqual(intraday_analyzer.get_watchlist(), [])

    def test_poll_returns_empty_without_watchlist(self):
        """워치리스트 미등록 상태에서 poll_all_markets → 빈 리스트"""
        # reset 후 set_watchlist 미호출
        result = intraday_analyzer.poll_all_markets()
        self.assertEqual(result, [])


# ═══════════════════════════════════════════════════════════════════
# [T5] 종목코드 유효성 검사 — 6자리 아닌 코드 필터링
# ═══════════════════════════════════════════════════════════════════

class TestCodeValidation(unittest.TestCase):
    """[T5] 워치리스트 / intraday 양쪽 모두 6자리 아닌 코드 필터링"""

    def test_short_code_excluded_from_watchlist(self):
        """5자리 종목코드 → 워치리스트에서 제외"""
        bad_picks = [
            {"종목코드": "12345",  "종목명": "짧은코드", "유형": "공시"},
            {"종목코드": "1234567","종목명": "긴코드",   "유형": "테마"},
            {"종목코드": "",       "종목명": "빈코드",   "유형": "공시"},
        ]
        ws = _build_ws_watchlist_from_picks(bad_picks, None)
        self.assertEqual(ws, {}, f"잘못된 코드가 워치리스트에 포함됨: {ws}")

    def test_mixed_valid_invalid_codes(self):
        """유효·무효 코드 혼합 → 유효한 6자리 코드만 포함"""
        picks = [
            {"종목코드": "123456", "종목명": "정상",   "유형": "공시"},
            {"종목코드": "12345",  "종목명": "5자리",  "유형": "공시"},
            {"종목코드": "654321", "종목명": "정상2",  "유형": "테마"},
        ]
        ws = _build_ws_watchlist_from_picks(picks, None)
        self.assertEqual(set(ws.keys()), {"123456", "654321"})

    def test_intraday_ignores_invalid_code_in_picks(self):
        """intraday에 잘못된 코드 픽 등록 시 poll에서 skip"""
        bad_pick = {"종목코드": "ABC", "종목명": "잘못된코드", "유형": "공시"}
        intraday_analyzer.reset()
        intraday_analyzer.set_watchlist([bad_pick])

        _rest_client_stub.get_stock_price.reset_mock()
        with patch("kis.rest_client.get_stock_price",
                   _rest_client_stub.get_stock_price):
            intraday_analyzer.poll_all_markets()

        # 잘못된 코드는 REST 호출 없어야 함
        self.assertEqual(_rest_client_stub.get_stock_price.call_count, 0)

    def tearDown(self):
        intraday_analyzer.reset()


# ═══════════════════════════════════════════════════════════════════
# [T6] 전일거래량 fallback
# ═══════════════════════════════════════════════════════════════════

class TestPrevVolumeFallback(unittest.TestCase):
    """[T6] price_data에 없는 종목 → 전일거래량 1 기본값"""

    def test_missing_price_data_defaults_to_1(self):
        """price_data=None → 전일거래량 1"""
        picks = [{"종목코드": "111111", "종목명": "테스트", "유형": "공시"}]
        ws = _build_ws_watchlist_from_picks(picks, None)
        self.assertEqual(ws["111111"]["전일거래량"], 1)

    def test_missing_code_in_price_data_defaults_to_1(self):
        """price_data에 해당 코드 없음 → 전일거래량 1"""
        picks      = [{"종목코드": "222222", "종목명": "테스트", "유형": "테마"}]
        price_data = {"by_code": {"333333": {"거래량": 999_999}}}  # 222222 없음
        ws = _build_ws_watchlist_from_picks(picks, price_data)
        self.assertEqual(ws["222222"]["전일거래량"], 1)

    def test_zero_volume_in_price_data_defaults_to_1(self):
        """거래량=0인 경우에도 최소 1 보장"""
        picks      = [{"종목코드": "444444", "종목명": "테스트", "유형": "순환매"}]
        price_data = {"by_code": {"444444": {"거래량": 0}}}
        ws = _build_ws_watchlist_from_picks(picks, price_data)
        self.assertGreaterEqual(ws["444444"]["전일거래량"], 1)


# ═══════════════════════════════════════════════════════════════════
# [T7] poll_all_markets 워밍업 사이클
# ═══════════════════════════════════════════════════════════════════

class TestPollWarmup(unittest.TestCase):
    """[T7] 첫 번째 poll — 스냅샷 저장만, 알림 없음"""

    def setUp(self):
        intraday_analyzer.reset()
        # 픽 등록
        self.picks = _make_picks(3)
        intraday_analyzer.set_watchlist(self.picks)

    def tearDown(self):
        intraday_analyzer.reset()

    def _make_price_row(self, ticker: str, rate: float = 5.0) -> dict:
        return {
            "현재가": 10_000,
            "등락률": rate,
            "거래량": 1_000_000,
        }

    def test_first_poll_returns_no_alerts(self):
        """첫 poll(워밍업) → 알림 리스트 빈값"""
        price_rows = {p["종목코드"]: self._make_price_row(p["종목코드"]) for p in self.picks}

        def fake_get_price(ticker):
            return price_rows.get(ticker)

        with patch("kis.rest_client.get_stock_price", side_effect=fake_get_price):
            result = intraday_analyzer.poll_all_markets()   # 1st = 워밍업

        self.assertEqual(result, [],
            f"워밍업 사이클에서 알림 발생: {result}")

    def test_second_poll_can_fire_alerts(self):
        """2nd poll — 등락률 급등 조건 충족 시 알림 발생 가능"""
        # 1st poll: 워밍업 (등락률 1.0%)
        low_rows  = {p["종목코드"]: self._make_price_row(p["종목코드"], 1.0) for p in self.picks}
        # 2nd poll: 급등 조건 (CONFIRM_CANDLES=2 이상 필요 → 모멘텀 대신 가격도달 테스트)
        high_rows = {p["종목코드"]: self._make_price_row(p["종목코드"], 14.0) for p in self.picks}
        # 목표등락률 15% → 14.0% ≥ 15*0.9=13.5% → 가격도달_목표 알림

        call_count = [0]
        def fake_get_price(ticker):
            call_count[0] += 1
            # 첫 3번(픽 수) 호출 = 워밍업, 이후 = 2nd poll
            if call_count[0] <= len(self.picks):
                return low_rows.get(ticker)
            return high_rows.get(ticker)

        with patch("kis.rest_client.get_stock_price", side_effect=fake_get_price), \
             patch("kis.rest_client.get_orderbook", return_value=None):
            intraday_analyzer.poll_all_markets()       # 워밍업
            result = intraday_analyzer.poll_all_markets()  # 실제 감시

        # 가격도달_목표 알림이 하나라도 발생해야 함
        self.assertGreater(len(result), 0,
            "2nd poll에서 가격도달_목표 알림이 발생하지 않음")
        for alert in result:
            self.assertEqual(alert["알림유형"], "가격도달_목표")
            self.assertIn(alert["종목코드"], {p["종목코드"] for p in self.picks},
                "픽 외 종목 알림 발생")


# ═══════════════════════════════════════════════════════════════════
# [통합] 아침봇 → 장중봇 End-to-End 흐름 시뮬레이션
# ═══════════════════════════════════════════════════════════════════

class TestEndToEndFlow(unittest.TestCase):
    """아침봇 픽 생성 → 워치리스트 등록 → 장중봇 감시 전체 흐름"""

    def setUp(self):
        watchlist_state.clear()
        intraday_analyzer.reset()

    def tearDown(self):
        watchlist_state.clear()
        intraday_analyzer.reset()

    def test_full_pipeline(self):
        """
        [E2E] 아침봇 픽 15종목 →
              1. _build_ws_watchlist_from_picks → watchlist_state 저장
              2. intraday_analyzer.set_watchlist → 장중봇 등록
              3. analyze_ws_tick — 픽 종목 알림 발생
              4. analyze_ws_tick — 픽 외 종목 차단
        """
        # Step 1: 아침봇 픽 15종목 생성
        picks      = _make_picks(15)
        price_data = _make_price_data(picks)

        # Step 2: WebSocket 워치리스트 생성 + 상태 저장
        ws_wl = _build_ws_watchlist_from_picks(picks, price_data)
        watchlist_state.set_watchlist(ws_wl)
        self.assertEqual(len(watchlist_state.get_watchlist()), 15)

        # Step 3: 장중봇 intraday에 픽 등록
        intraday_analyzer.set_watchlist(picks)
        self.assertEqual(len(intraday_analyzer.get_watchlist()), 15)

        # Step 4: 픽 종목 알림 발생 검증
        pick = picks[0]
        tick_in = {
            "종목코드":   pick["종목코드"],
            "종목명":     pick["종목명"],
            "등락률":     8.0,
            "누적거래량": 1_000_000,
        }
        alert = intraday_analyzer.analyze_ws_tick(tick_in, prdy_vol=500_000)
        self.assertIsNotNone(alert, "픽 종목 알림 없음")
        self.assertEqual(alert["종목코드"], pick["종목코드"])

        # Step 5: 픽 외 종목 차단 검증
        tick_out = {
            "종목코드":   "999999",
            "종목명":     "외부종목",
            "등락률":     15.0,
            "누적거래량": 5_000_000,
        }
        blocked = intraday_analyzer.analyze_ws_tick(tick_out, prdy_vol=500_000)
        self.assertIsNone(blocked, "픽 외 종목이 차단되지 않음")

    def test_유형_pick_type_e2e(self):
        """[E2E] 픽 15종목 유형 → pick_type 변환 → 단타/스윙 분류"""
        picks = _make_picks(15)
        for pick in picks:
            with self.subTest(유형=pick["유형"], 코드=pick["종목코드"]):
                pt = _resolve_pick_type(pick["유형"])
                if pick["유형"] in ("공시", "테마"):
                    self.assertEqual(pt, "단타")
                else:
                    self.assertEqual(pt, "스윙")


if __name__ == "__main__":
    unittest.main(verbosity=2)
