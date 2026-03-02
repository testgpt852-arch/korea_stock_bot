"""
tests/test_data_collector.py
data_collector 단위 테스트

검증 항목:
  [C1] 캐시 키 계약 — get_cache() 반환값이 ARCHITECTURE §2 규격과 정확히 일치
  [C2] 각 수집기의 반환 타입 계약 — list/dict/None 타입 보장
  [C3] fund_concentration_result 키 이름 — "자금유입비율" (ratio / 거래대금시총비율 금지)
  [C4] success_flags 정확성 — 성공/실패 수집기별 True/False
  [C5] 수집 실패 fallback — 개별 수집기 예외 시 전체 봇 중단 없이 None/빈값
  [C6] price_data fallback — 실패 시 None (빈 dict 아님)
  [C7] 삭제된 키 참조 금지 — signals / market_summary / score_summary / report_picks / volatility 미존재
  [C8] get_cache() 초기 상태 — run() 미호출 시 빈 dict
  [C9] is_fresh() — 수집 직후 True, 오래된 캐시 False
  [C10] _send_raw_data_to_telegram 실패 시 비치명적 (run() 완료)
  [C11] 병렬 수집기 12개 모두 호출됨 확인
  [C12] config 플래그 OFF 시 해당 수집기 빈 리스트 반환

실행 방법:
    cd korea_stock_bot-main
    python -m pytest tests/test_data_collector.py -v
"""

import asyncio
import sys
import os
import types
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, AsyncMock

# ── 프로젝트 루트 sys.path 추가 ───────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ══════════════════════════════════════════════════════════════════
# Stub 모듈 주입 — 실제 API / 텔레그램 / pykrx 차단
# ══════════════════════════════════════════════════════════════════

def _make_stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


KST = timezone(timedelta(hours=9))

# ── logger stub ──────────────────────────────────────────────────
_logger_mock = MagicMock()
_logger_mod = _make_stub("utils.logger", logger=_logger_mock)
_utils_pkg = types.ModuleType("utils")
_utils_pkg.__path__ = []
_utils_pkg.__package__ = "utils"
sys.modules["utils"] = _utils_pkg
sys.modules["utils.logger"] = _logger_mod

# ── config stub ──────────────────────────────────────────────────
_config_stub = _make_stub(
    "config",
    GEOPOLITICS_ENABLED=False,
    SECTOR_ETF_ENABLED=True,
    SHORT_INTEREST_ENABLED=True,
    EVENT_CALENDAR_ENABLED=True,
    FUND_INFLOW_CAP_MIN=30_000_000_000,
    FUND_INFLOW_TOP_N=20,
    CLOSING_STRENGTH_TOP_N=20,
    VOLUME_FLAT_TOP_N=20,
    PRICE_CAP_MAX=300_000_000_000,
    PRICE_GAINER_MIN_RATE=15.0,
)
sys.modules["config"] = _config_stub

# ── date_utils stub ───────────────────────────────────────────────
from datetime import date as _date

_date_utils_stub = _make_stub(
    "utils.date_utils",
    get_today=lambda: _date(2024, 1, 15),
    get_prev_trading_day=lambda d: _date(2024, 1, 12),
    fmt_ymd=lambda d: d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d),
)
sys.modules["utils.date_utils"] = _date_utils_stub

# ── telegram stub ─────────────────────────────────────────────────
_telegram_send_mock = MagicMock()
_telegram_sender_stub = _make_stub("telegram.sender", send=_telegram_send_mock)
_telegram_pkg = types.ModuleType("telegram")
_telegram_pkg.__path__ = []
sys.modules["telegram"] = _telegram_pkg
sys.modules["telegram.sender"] = _telegram_sender_stub

# ── pykrx stub (차단) ─────────────────────────────────────────────
_pykrx_stub = _make_stub("pykrx")
_pykrx_stock_stub = _make_stub("pykrx.stock")
sys.modules["pykrx"] = _pykrx_stub
sys.modules["pykrx.stock"] = _pykrx_stock_stub


# ══════════════════════════════════════════════════════════════════
# 수집기별 표준 반환값 픽스처 (ARCHITECTURE §3 계약 준수)
# ══════════════════════════════════════════════════════════════════

FIXTURE_DART = [
    {"종목명": "테스트A", "종목코드": "000001", "공시종류": "수주", "핵심내용": "...",
     "공시시각": "08:30", "신뢰도": "원본", "내부자여부": False,
     "규모": "150억", "본문요약": "계약금액 150억", "rcept_no": "20240115000001"},
]

FIXTURE_MARKET_DATA = {
    "us_market": {
        "sectors": {"Technology": {"change": "+2.5%"}, "Energy": {"change": "-1.1%"}},
        "nasdaq": "상승",
    },
    "commodities": {"WTI": {"change": "+1.2%"}, "금": {"change": "-0.5%"}},
    "forex": {"USD/KRW": 1325.0},
}

FIXTURE_NEWS_NAVER = {"articles": [{"title": "뉴스A", "link": "http://n.news.naver.com/1"}]}
FIXTURE_NEWS_NEWSAPI = {"articles": [{"title": "Global News A"}]}
FIXTURE_NEWS_RSS = [{"title": "지정학 뉴스", "link": "http://rss.example.com/1"}]

FIXTURE_PRICE_DATA = {
    "date": "20240112",
    "kospi": {"close": 2500.0, "change_rate": 0.3},
    "kosdaq": {"close": 800.0, "change_rate": -0.1},
    "upper_limit": [{"종목명": "상한가종목", "종목코드": "000010", "등락률": 30.0, "시가총액": 50_000_000_000}],
    "top_gainers": [{"종목명": "급등종목", "종목코드": "000011", "등락률": 18.5, "시가총액": 80_000_000_000}],
    "by_name": {"상한가종목": {"종목코드": "000010"}},
    "by_code": {"000010": {"종목명": "상한가종목"}},
}

FIXTURE_SECTOR_ETF = [{"섹터": "반도체", "등락률": 2.1, "순매수": 5_000_000_000}]

FIXTURE_SHORT_DATA = [
    {"종목명": "공매도A", "종목코드": "000020", "short_ratio": 12.5},
]

FIXTURE_EVENT_CALENDAR = [{"종목명": "이벤트종목", "이벤트": "실적발표", "날짜": "20240115"}]

FIXTURE_CLOSING_STRENGTH = [
    {"종목코드": "000030", "종목명": "마감강도A", "마감강도": 0.95,
     "등락률": 3.2, "거래량증가율": 120.0, "종가": 15000, "고가": 15200, "저가": 14500},
]

FIXTURE_VOLUME_SURGE = [
    {"종목코드": "000040", "종목명": "거래량급증A", "등락률": 1.5,
     "거래량증가율": 650.0, "거래량": 2_000_000, "전일거래량": 300_000, "종가": 8000},
]

# [ARCHITECTURE §3] fund_concentration_result — 키 이름 "자금유입비율" 필수
FIXTURE_FUND_CONCENTRATION = [
    {"종목코드": "000050", "종목명": "자금집중A",
     "등락률": 4.1, "자금유입비율": 35.2, "거래대금": 15_000_000_000,
     "시가총액": 42_000_000_000, "종가": 12000},
]

# ARCHITECTURE §2 — 허용 캐시 키 전체 목록
REQUIRED_CACHE_KEYS = {
    "collected_at",
    "dart_data",
    "market_data",
    "news_naver",
    "news_newsapi",
    "news_global_rss",
    "price_data",
    "sector_etf_data",
    "short_data",
    "event_calendar",
    "closing_strength_result",
    "volume_surge_result",
    "fund_concentration_result",
    "success_flags",
}

# ARCHITECTURE §2 — 삭제된 키 (절대 존재 금지)
DELETED_CACHE_KEYS = {"signals", "market_summary", "score_summary", "report_picks", "volatility"}

# success_flags 표준 키 목록
REQUIRED_FLAG_KEYS = {
    "filings", "market_global", "news_naver", "news_newsapi", "news_global_rss",
    "price_domestic", "sector_etf", "short_interest", "event_calendar",
    "closing_strength", "volume_surge", "fund_concentration",
}


# ══════════════════════════════════════════════════════════════════
# 헬퍼 — 수집기 패치 컨텍스트
# ══════════════════════════════════════════════════════════════════

def _make_collector_stubs(overrides: dict | None = None):
    """
    12개 수집기 전체를 Fixture 반환값으로 mock.
    overrides: {"filings": Exception("fail"), "price_domestic": None, ...}
    """
    defaults = {
        "filings":            FIXTURE_DART,
        "market_global":      FIXTURE_MARKET_DATA,
        "news_naver":         FIXTURE_NEWS_NAVER,
        "news_newsapi":       FIXTURE_NEWS_NEWSAPI,
        "news_global_rss":    FIXTURE_NEWS_RSS,
        "price_domestic":     FIXTURE_PRICE_DATA,
        "sector_etf":         FIXTURE_SECTOR_ETF,
        "short_interest":     FIXTURE_SHORT_DATA,
        "event_calendar":     FIXTURE_EVENT_CALENDAR,
        "closing_strength":   FIXTURE_CLOSING_STRENGTH,
        "volume_surge":       FIXTURE_VOLUME_SURGE,
        "fund_concentration": FIXTURE_FUND_CONCENTRATION,
    }
    if overrides:
        defaults.update(overrides)
    return defaults


def _patch_safe_collect(stubs: dict):
    """
    data_collector._safe_collect 를 직접 패치해
    각 name에 해당하는 stub 값을 반환하도록 대체.
    """
    async def _fake_safe_collect(name, fn, *args):
        val = stubs.get(name, None)
        if isinstance(val, Exception):
            return None   # _safe_collect는 예외 시 None 반환
        return val

    return patch("collectors.data_collector._safe_collect", side_effect=_fake_safe_collect)


# ══════════════════════════════════════════════════════════════════
# 테스트 클래스
# ══════════════════════════════════════════════════════════════════

class TestCacheKeyContract(unittest.IsolatedAsyncioTestCase):
    """[C1] 캐시 키 계약 검증"""

    async def test_all_required_keys_present(self):
        """run() 후 캐시에 ARCHITECTURE §2 전체 키 존재"""
        import collectors.data_collector as dc

        with _patch_safe_collect(_make_collector_stubs()):
            cache = await dc.run()

        self.assertEqual(
            set(cache.keys()), REQUIRED_CACHE_KEYS,
            f"캐시 키 불일치\n  누락: {REQUIRED_CACHE_KEYS - set(cache.keys())}\n  초과: {set(cache.keys()) - REQUIRED_CACHE_KEYS}"
        )

    async def test_deleted_keys_absent(self):
        """[C7] 삭제된 키(signals/market_summary 등)가 캐시에 없어야 함"""
        import collectors.data_collector as dc

        with _patch_safe_collect(_make_collector_stubs()):
            cache = await dc.run()

        forbidden = DELETED_CACHE_KEYS & set(cache.keys())
        self.assertFalse(forbidden, f"삭제된 키가 캐시에 존재: {forbidden}")

    async def test_collected_at_is_kst_iso_string(self):
        """collected_at — KST ISO 8601 형식 문자열"""
        import collectors.data_collector as dc

        with _patch_safe_collect(_make_collector_stubs()):
            cache = await dc.run()

        ts = cache.get("collected_at")
        self.assertIsInstance(ts, str, "collected_at must be str")
        # fromisoformat 으로 파싱 가능해야 함
        parsed = datetime.fromisoformat(ts)
        self.assertIsNotNone(parsed)

    async def test_success_flags_has_all_keys(self):
        """[C4] success_flags — 12개 수집기 키 모두 존재"""
        import collectors.data_collector as dc

        with _patch_safe_collect(_make_collector_stubs()):
            cache = await dc.run()

        flags = cache.get("success_flags", {})
        self.assertEqual(
            set(flags.keys()), REQUIRED_FLAG_KEYS,
            f"success_flags 키 불일치\n  누락: {REQUIRED_FLAG_KEYS - set(flags.keys())}"
        )

    async def test_success_flags_all_true_on_full_success(self):
        """[C4] 모든 수집기 성공 시 success_flags 전체 True"""
        import collectors.data_collector as dc

        with _patch_safe_collect(_make_collector_stubs()):
            cache = await dc.run()

        flags = cache["success_flags"]
        failed = {k: v for k, v in flags.items() if not v}
        self.assertFalse(failed, f"성공해야 할 수집기가 False: {failed}")


class TestCacheValueTypes(unittest.IsolatedAsyncioTestCase):
    """[C2] 캐시 값 타입 계약 — list/dict/None"""

    async def _run_full(self):
        import collectors.data_collector as dc
        with _patch_safe_collect(_make_collector_stubs()):
            return await dc.run()

    async def test_dart_data_is_list(self):
        cache = await self._run_full()
        self.assertIsInstance(cache["dart_data"], list)

    async def test_market_data_is_dict(self):
        cache = await self._run_full()
        self.assertIsInstance(cache["market_data"], dict)

    async def test_market_data_has_required_subkeys(self):
        """market_data — us_market / commodities / forex 모두 포함"""
        cache = await self._run_full()
        md = cache["market_data"]
        for key in ("us_market", "commodities", "forex"):
            self.assertIn(key, md, f"market_data에 '{key}' 누락")

    async def test_news_naver_is_dict(self):
        cache = await self._run_full()
        self.assertIsInstance(cache["news_naver"], dict)

    async def test_news_newsapi_is_dict(self):
        cache = await self._run_full()
        self.assertIsInstance(cache["news_newsapi"], dict)

    async def test_news_global_rss_is_list(self):
        cache = await self._run_full()
        self.assertIsInstance(cache["news_global_rss"], list)

    async def test_price_data_is_dict_on_success(self):
        """price_data — 성공 시 dict"""
        cache = await self._run_full()
        self.assertIsInstance(cache["price_data"], dict)

    async def test_price_data_has_required_subkeys(self):
        """price_data — upper_limit / top_gainers / by_code / by_name 포함"""
        cache = await self._run_full()
        pd = cache["price_data"]
        for key in ("upper_limit", "top_gainers", "by_code", "by_name"):
            self.assertIn(key, pd, f"price_data에 '{key}' 누락")

    async def test_sector_etf_data_is_list(self):
        cache = await self._run_full()
        self.assertIsInstance(cache["sector_etf_data"], list)

    async def test_short_data_is_list(self):
        cache = await self._run_full()
        self.assertIsInstance(cache["short_data"], list)

    async def test_event_calendar_is_list(self):
        cache = await self._run_full()
        self.assertIsInstance(cache["event_calendar"], list)

    async def test_closing_strength_result_is_list(self):
        cache = await self._run_full()
        self.assertIsInstance(cache["closing_strength_result"], list)

    async def test_volume_surge_result_is_list(self):
        cache = await self._run_full()
        self.assertIsInstance(cache["volume_surge_result"], list)

    async def test_fund_concentration_result_is_list(self):
        cache = await self._run_full()
        self.assertIsInstance(cache["fund_concentration_result"], list)

    async def test_success_flags_is_dict_of_bools(self):
        cache = await self._run_full()
        flags = cache["success_flags"]
        self.assertIsInstance(flags, dict)
        for k, v in flags.items():
            self.assertIsInstance(v, bool, f"success_flags['{k}']가 bool이 아님: {type(v)}")


class TestFundConcentrationKeyContract(unittest.IsolatedAsyncioTestCase):
    """[C3] fund_concentration_result 키 이름 계약 — "자금유입비율" 필수"""

    async def test_fund_concentration_key_is_자금유입비율(self):
        """[ARCHITECTURE §3, §5 BUG-06] ratio / 거래대금시총비율 사용 금지"""
        import collectors.data_collector as dc

        with _patch_safe_collect(_make_collector_stubs()):
            cache = await dc.run()

        result = cache["fund_concentration_result"]
        self.assertTrue(len(result) > 0, "fixture가 비어있어 키 검증 불가")

        for item in result:
            self.assertIn(
                "자금유입비율", item,
                f"fund_concentration_result 원소에 '자금유입비율' 키 누락: {item.keys()}"
            )
            # 금지 키 확인
            self.assertNotIn("ratio", item, "'ratio' 키 사용 금지 (ARCHITECTURE §5 BUG-06)")
            self.assertNotIn("거래대금시총비율", item, "'거래대금시총비율' 키 사용 금지")

    async def test_fund_concentration_ratio_is_float(self):
        """자금유입비율 값이 float"""
        import collectors.data_collector as dc

        with _patch_safe_collect(_make_collector_stubs()):
            cache = await dc.run()

        for item in cache["fund_concentration_result"]:
            self.assertIsInstance(
                item["자금유입비율"], float,
                f"자금유입비율가 float이 아님: {type(item['자금유입비율'])}"
            )


class TestFallbackBehavior(unittest.IsolatedAsyncioTestCase):
    """[C5] 수집 실패 시 fallback — 봇 중단 없이 빈값/None"""

    async def test_single_collector_failure_does_not_crash(self):
        """개별 수집기 실패 시 run() 정상 완료"""
        import collectors.data_collector as dc

        stubs = _make_collector_stubs({"filings": Exception("DART API 오류")})
        with _patch_safe_collect(stubs):
            try:
                cache = await dc.run()
            except Exception as e:
                self.fail(f"단일 수집기 실패 시 run()이 예외를 던짐: {e}")

        self.assertIsNotNone(cache)

    async def test_filings_failure_yields_empty_list(self):
        """filings 실패 → dart_data == []"""
        import collectors.data_collector as dc

        stubs = _make_collector_stubs({"filings": Exception("DART 실패")})
        with _patch_safe_collect(stubs):
            cache = await dc.run()

        self.assertEqual(cache["dart_data"], [])

    async def test_market_global_failure_yields_empty_dict(self):
        """market_global 실패 → market_data == {}"""
        import collectors.data_collector as dc

        stubs = _make_collector_stubs({"market_global": Exception("시장 데이터 실패")})
        with _patch_safe_collect(stubs):
            cache = await dc.run()

        self.assertEqual(cache["market_data"], {})

    async def test_price_domestic_failure_yields_none(self):
        """[C6] price_domestic 실패 → price_data is None (빈 dict 아님)"""
        import collectors.data_collector as dc

        stubs = _make_collector_stubs({"price_domestic": Exception("pykrx 오류")})
        with _patch_safe_collect(stubs):
            cache = await dc.run()

        # None이어야 함 — {} 아님
        self.assertIsNone(
            cache["price_data"],
            "price_data 실패 시 None이어야 함 (ARCHITECTURE §2: dict | None)"
        )

    async def test_all_collectors_fail_cache_still_valid(self):
        """모든 수집기 실패 시에도 캐시 구조는 유지"""
        import collectors.data_collector as dc

        stubs = {k: Exception("전체 실패") for k in _make_collector_stubs().keys()}
        with _patch_safe_collect(stubs):
            cache = await dc.run()

        self.assertEqual(set(cache.keys()), REQUIRED_CACHE_KEYS)
        self.assertIsNone(cache["price_data"])
        self.assertEqual(cache["dart_data"], [])
        self.assertEqual(cache["market_data"], {})
        self.assertEqual(cache["news_global_rss"], [])

    async def test_all_collectors_fail_success_flags_all_false(self):
        """[C4] 모든 수집기 실패 → success_flags 전체 False"""
        import collectors.data_collector as dc

        stubs = {k: Exception("전체 실패") for k in _make_collector_stubs().keys()}
        with _patch_safe_collect(stubs):
            cache = await dc.run()

        flags = cache["success_flags"]
        self.assertTrue(all(not v for v in flags.values()), f"전체 실패 시 모두 False여야 함: {flags}")

    async def test_partial_failure_success_flags_accuracy(self):
        """[C4] 일부 실패 → 실패한 수집기만 False"""
        import collectors.data_collector as dc

        failing = {"filings", "news_newsapi", "fund_concentration"}
        stubs = _make_collector_stubs(
            {k: Exception("의도적 실패") for k in failing}
        )
        with _patch_safe_collect(stubs):
            cache = await dc.run()

        flags = cache["success_flags"]
        # filings → success_flags["filings"] False
        self.assertFalse(flags["filings"])
        self.assertFalse(flags["news_newsapi"])
        self.assertFalse(flags["fund_concentration"])
        # 나머지는 True
        for k in REQUIRED_FLAG_KEYS - {"filings", "news_newsapi", "fund_concentration"}:
            self.assertTrue(flags[k], f"성공 수집기인데 False: {k}")

    async def test_news_global_rss_list_collectors_fallback_to_empty_list(self):
        """list 타입 수집기 실패 → 빈 리스트 (None 아님)"""
        import collectors.data_collector as dc

        list_collectors = {
            "news_global_rss": Exception("RSS 실패"),
            "sector_etf": Exception("ETF 실패"),
            "short_interest": Exception("공매도 실패"),
            "event_calendar": Exception("이벤트 실패"),
            "closing_strength": Exception("마감강도 실패"),
            "volume_surge": Exception("거래량 실패"),
            "fund_concentration": Exception("자금집중 실패"),
        }
        stubs = _make_collector_stubs(list_collectors)
        with _patch_safe_collect(stubs):
            cache = await dc.run()

        for key in ("news_global_rss", "sector_etf_data", "short_data",
                    "event_calendar", "closing_strength_result",
                    "volume_surge_result", "fund_concentration_result"):
            self.assertIsInstance(cache[key], list, f"'{key}' 실패 시 빈 리스트여야 함")
            self.assertEqual(cache[key], [], f"'{key}' 실패 시 [] 이어야 함")


class TestTelegramFailureNonFatal(unittest.IsolatedAsyncioTestCase):
    """[C10] 텔레그램 발송 실패 시 비치명적"""

    async def test_telegram_failure_does_not_crash_run(self):
        """_send_raw_data_to_telegram 예외 → run() 정상 반환"""
        import collectors.data_collector as dc

        with _patch_safe_collect(_make_collector_stubs()):
            with patch("collectors.data_collector._send_raw_data_to_telegram",
                       side_effect=Exception("텔레그램 서버 오류")):
                try:
                    cache = await dc.run()
                except Exception as e:
                    self.fail(f"텔레그램 실패가 run()을 중단시킴: {e}")

        self.assertIsNotNone(cache)
        self.assertIn("dart_data", cache)

    async def test_cache_is_populated_even_when_telegram_fails(self):
        """텔레그램 실패와 무관하게 캐시는 채워짐"""
        import collectors.data_collector as dc

        with _patch_safe_collect(_make_collector_stubs()):
            with patch("collectors.data_collector._send_raw_data_to_telegram",
                       side_effect=RuntimeError("연결 불가")):
                cache = await dc.run()

        self.assertEqual(set(cache.keys()), REQUIRED_CACHE_KEYS)
        self.assertIsNotNone(cache["collected_at"])


class TestGetCacheAndIsFresh(unittest.IsolatedAsyncioTestCase):
    """[C8][C9] get_cache() 초기 상태 / is_fresh() 동작"""

    def setUp(self):
        # 모듈 리로드로 _cache 초기화
        import importlib
        import collectors.data_collector as dc
        dc._cache = {}

    def test_get_cache_returns_empty_before_run(self):
        """[C8] run() 미호출 시 get_cache() == {}"""
        import collectors.data_collector as dc
        dc._cache = {}
        result = dc.get_cache()
        self.assertEqual(result, {})

    def test_is_fresh_false_before_run(self):
        """[C9] 캐시 없으면 is_fresh() == False"""
        import collectors.data_collector as dc
        dc._cache = {}
        self.assertFalse(dc.is_fresh())

    async def test_is_fresh_true_after_run(self):
        """[C9] run() 직후 is_fresh() == True"""
        import collectors.data_collector as dc

        with _patch_safe_collect(_make_collector_stubs()):
            await dc.run()

        self.assertTrue(dc.is_fresh(max_age_minutes=180))

    def test_is_fresh_false_with_old_timestamp(self):
        """[C9] 오래된 캐시 → is_fresh(max_age_minutes=1) == False"""
        import collectors.data_collector as dc

        old_ts = datetime(2020, 1, 1, 0, 0, 0, tzinfo=KST).isoformat()
        dc._cache = {"collected_at": old_ts}

        self.assertFalse(dc.is_fresh(max_age_minutes=1))

    def test_get_cache_returns_same_object_as_run_result(self):
        """get_cache()가 run() 반환값과 동일 객체"""
        import collectors.data_collector as dc

        dc._cache = {"collected_at": "2024-01-15T06:00:00+09:00", "dart_data": []}
        result = dc.get_cache()
        self.assertIs(result, dc._cache)


class TestConfigFlagsDisabled(unittest.IsolatedAsyncioTestCase):
    """[C12] config 플래그 OFF 시 해당 수집기 빈 리스트 반환"""

    async def test_geopolitics_disabled_returns_empty_rss(self):
        """GEOPOLITICS_ENABLED=False → _collect_global_rss() == []"""
        import collectors.data_collector as dc

        original = _config_stub.GEOPOLITICS_ENABLED
        _config_stub.GEOPOLITICS_ENABLED = False

        try:
            result = dc._collect_global_rss()
            self.assertEqual(result, [], "GEOPOLITICS_ENABLED=False 시 [] 반환해야 함")
        finally:
            _config_stub.GEOPOLITICS_ENABLED = original

    async def test_sector_etf_disabled_returns_empty(self):
        """SECTOR_ETF_ENABLED=False → _collect_sector_etf() == []"""
        import collectors.data_collector as dc

        original = _config_stub.SECTOR_ETF_ENABLED
        _config_stub.SECTOR_ETF_ENABLED = False

        try:
            result = dc._collect_sector_etf(_date(2024, 1, 12))
            self.assertEqual(result, [])
        finally:
            _config_stub.SECTOR_ETF_ENABLED = original

    async def test_short_interest_disabled_returns_empty(self):
        """SHORT_INTEREST_ENABLED=False → _collect_short_interest() == []"""
        import collectors.data_collector as dc

        original = _config_stub.SHORT_INTEREST_ENABLED
        _config_stub.SHORT_INTEREST_ENABLED = False

        try:
            result = dc._collect_short_interest(_date(2024, 1, 12))
            self.assertEqual(result, [])
        finally:
            _config_stub.SHORT_INTEREST_ENABLED = original

    async def test_event_calendar_disabled_returns_empty(self):
        """EVENT_CALENDAR_ENABLED=False → _collect_event_calendar() == []"""
        import collectors.data_collector as dc

        original = _config_stub.EVENT_CALENDAR_ENABLED
        _config_stub.EVENT_CALENDAR_ENABLED = False

        try:
            result = dc._collect_event_calendar(_date(2024, 1, 15))
            self.assertEqual(result, [])
        finally:
            _config_stub.EVENT_CALENDAR_ENABLED = original

    async def test_none_date_returns_safe_fallback(self):
        """prev_date=None → 관련 수집기들이 None 날짜 안전 처리"""
        import collectors.data_collector as dc

        # prev_date 없으면 filings/market_global/price/sector/short 모두 빈값 반환
        result_filings = dc._collect_filings(None)
        self.assertEqual(result_filings, [])

        result_market = dc._collect_market_global(None)
        self.assertEqual(result_market, {})

        result_price = dc._collect_price_domestic(None)
        self.assertIsNone(result_price)

        result_closing = dc._collect_closing_strength(None)
        self.assertEqual(result_closing, [])

        result_volume = dc._collect_volume_surge(None)
        self.assertEqual(result_volume, [])

        result_fund = dc._collect_fund_concentration(None)
        self.assertEqual(result_fund, [])


class TestSafeCollect(unittest.IsolatedAsyncioTestCase):
    """[C5] _safe_collect — 예외 시 None 반환, 봇 중단 없음"""

    async def test_safe_collect_returns_value_on_success(self):
        """_safe_collect — 정상 호출 시 반환값 그대로"""
        import collectors.data_collector as dc

        result = await dc._safe_collect("test", lambda: [1, 2, 3])
        self.assertEqual(result, [1, 2, 3])

    async def test_safe_collect_returns_none_on_exception(self):
        """_safe_collect — 예외 시 None (비치명적)"""
        import collectors.data_collector as dc

        def _raise():
            raise RuntimeError("의도적 예외")

        result = await dc._safe_collect("test_fail", _raise)
        self.assertIsNone(result)

    async def test_safe_collect_uses_get_running_loop(self):
        """[ARCHITECTURE §5 BUG-01] get_running_loop() 사용 검증 (asyncio 컨텍스트)
        - 주석 줄은 제외하고 실제 코드 줄만 검사
        """
        import collectors.data_collector as dc
        import inspect

        src = inspect.getsource(dc._safe_collect)

        # 주석 제거 — 줄 전체 주석 및 인라인 주석(공백2개+# 패턴) 제거
        code_lines = []
        for line in src.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # 인라인 주석 제거: "  # ..." 패턴 제거
            import re as _re
            code_only_line = _re.sub(r"\s{2,}#.*$", "", line)
            code_lines.append(code_only_line)
        code_only = "\n".join(code_lines)

        self.assertIn(
            "get_running_loop", code_only,
            "_safe_collect이 get_running_loop() 대신 get_event_loop()를 사용함"
        )
        self.assertNotIn(
            "get_event_loop", code_only,
            "_safe_collect 실행 코드에 deprecated get_event_loop() 잔존 (주석 아닌 실제 호출)"
        )

    async def test_safe_collect_with_args(self):
        """_safe_collect — 인자 전달 정상 동작"""
        import collectors.data_collector as dc

        result = await dc._safe_collect("with_args", lambda x, y: x + y, 3, 7)
        self.assertEqual(result, 10)


class TestSendRawDataTelegram(unittest.TestCase):
    """_send_raw_data_to_telegram — 핵심 메시지 구조 검증"""

    def _call_send(self, cache: dict):
        """_send_raw_data_to_telegram 호출 후 전송 메시지 반환"""
        import collectors.data_collector as dc
        _telegram_send_mock.reset_mock()

        dc._send_raw_data_to_telegram(cache)

        self.assertTrue(_telegram_send_mock.called, "send()가 호출되지 않음")
        return _telegram_send_mock.call_args[0][0]

    def _make_full_cache(self):
        return {
            "dart_data": FIXTURE_DART,
            "market_data": FIXTURE_MARKET_DATA,
            "price_data": FIXTURE_PRICE_DATA,
            "fund_concentration_result": FIXTURE_FUND_CONCENTRATION,
            "short_data": FIXTURE_SHORT_DATA,
            "volume_surge_result": FIXTURE_VOLUME_SURGE,
            "success_flags": {k: True for k in REQUIRED_FLAG_KEYS},
        }

    def test_message_contains_section_headers(self):
        """발송 메시지에 주요 섹션 헤더 포함"""
        cache = self._make_full_cache()
        msg = self._call_send(cache)

        for header in ("📊", "DART 공시", "자금집중", "공매도", "거래량 급증"):
            self.assertIn(header, msg, f"섹션 헤더 누락: {header}")

    def test_message_contains_gemini_fallback_notice(self):
        """Gemini 장애 안내 메시지 포함 (ARCHITECTURE _send_raw_data_to_telegram 명세)"""
        cache = self._make_full_cache()
        msg = self._call_send(cache)
        self.assertIn("Gemini", msg, "Gemini 장애 안내 메시지 누락")

    def test_message_contains_failed_flags(self):
        """실패 수집기 이름이 메시지에 포함"""
        cache = self._make_full_cache()
        cache["success_flags"] = {k: True for k in REQUIRED_FLAG_KEYS}
        cache["success_flags"]["filings"] = False
        cache["success_flags"]["market_global"] = False

        msg = self._call_send(cache)
        self.assertIn("filings", msg, "실패한 수집기 'filings'가 메시지에 미포함")
        self.assertIn("market_global", msg, "실패한 수집기 'market_global'가 메시지에 미포함")

    def test_message_with_empty_fund_concentration(self):
        """fund_concentration_result 비어있을 때 '해당 없음' 포함"""
        cache = self._make_full_cache()
        cache["fund_concentration_result"] = []
        msg = self._call_send(cache)
        self.assertIn("해당 없음", msg)

    def test_send_raises_on_telegram_error(self):
        """send() 예외 시 _send_raw_data_to_telegram도 예외 전파"""
        import collectors.data_collector as dc
        _telegram_send_mock.side_effect = RuntimeError("텔레그램 오류")

        try:
            with self.assertRaises(RuntimeError):
                dc._send_raw_data_to_telegram(self._make_full_cache())
        finally:
            _telegram_send_mock.side_effect = None


class TestCollectorCallCount(unittest.IsolatedAsyncioTestCase):
    """[C11] 병렬 수집기 12개 모두 호출되는지 확인"""

    async def test_all_12_collectors_are_called(self):
        """run() → 12개 _safe_collect 호출 각각 name 확인"""
        import collectors.data_collector as dc

        called_names = []

        async def _tracking_safe_collect(name, fn, *args):
            called_names.append(name)
            return _make_collector_stubs().get(name, None)

        with patch("collectors.data_collector._safe_collect",
                   side_effect=_tracking_safe_collect):
            await dc.run()

        expected_names = {
            "filings", "market_global", "news_naver", "news_newsapi",
            "news_global_rss", "price_domestic", "sector_etf", "short_interest",
            "event_calendar", "closing_strength", "volume_surge", "fund_concentration",
        }
        self.assertEqual(set(called_names), expected_names,
                         f"호출 누락: {expected_names - set(called_names)}\n"
                         f"초과 호출: {set(called_names) - expected_names}")

    async def test_exactly_12_collectors_called(self):
        """수집기가 정확히 12번 호출됨"""
        import collectors.data_collector as dc

        call_count = []

        async def _counting_collect(name, fn, *args):
            call_count.append(name)
            return _make_collector_stubs().get(name, None)

        with patch("collectors.data_collector._safe_collect",
                   side_effect=_counting_collect):
            await dc.run()

        self.assertEqual(len(call_count), 12, f"수집기 호출 횟수 오류: {len(call_count)} (expected 12)\n호출목록: {call_count}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
