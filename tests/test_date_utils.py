"""
tests/test_date_utils.py

외부 의존성(pykrx, utils.logger)은 unittest.mock으로 전부 패치.
각 테스트 setUp/tearDown에서 date_utils._market_open_cache.clear() 호출.
pykrx 는 patch("utils.date_utils.stock") 으로 패치.

[패치 전략]
date_utils.py 는 is_market_open() 내부에서 `from pykrx import stock` 을 로컬 임포트.
`patch("utils.date_utils.stock")` 이 실제로 동작하려면:
  1. pykrx 스텁을 sys.modules 에 등록하되, pykrx.stock 접근 시
     항상 date_utils.stock 을 반환하는 프록시로 만든다.
  2. date_utils 를 sys.modules["utils.date_utils"] 에 등록하고
     utils 스텁에 date_utils 속성으로도 등록한다.
이렇게 하면 patch("utils.date_utils.stock")이 date_utils.stock 을 교체하고,
함수 내부의 `from pykrx import stock` 이 교체된 mock 을 받는다.
"""

import importlib
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

KST = timezone(timedelta(hours=9))


# ──────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────
def _kst(year, month, day) -> datetime:
    return datetime(year, month, day, 9, 0, 0, tzinfo=KST)


# ──────────────────────────────────────────────
# 모듈 로드 (stub 주입 포함)
# ──────────────────────────────────────────────
def _load_date_utils():
    """
    date_utils 를 로드하면서 의존성 stub 을 sys.modules 에 주입.
    pykrx 스텁은 .stock 속성 접근 시 항상 date_utils.stock 을 반환하는
    프록시로 구성한다 → patch("utils.date_utils.stock") 이 동작하는 핵심.
    """
    # utils.logger stub
    utils_mod = types.ModuleType("utils")
    logger_mod = types.ModuleType("utils.logger")
    logger_mod.logger = MagicMock()
    utils_mod.logger = logger_mod
    sys.modules["utils"] = utils_mod
    sys.modules["utils.logger"] = logger_mod

    # date_utils 로드
    import pathlib
    _CANDIDATES = [
        pathlib.Path(__file__).parent.parent / "utils" / "date_utils.py",
        pathlib.Path(__file__).parent / "utils" / "date_utils.py",
        pathlib.Path("utils/date_utils.py"),
        pathlib.Path("korea_stock_bot-main/utils/date_utils.py"),
    ]
    mod = None
    for p in _CANDIDATES:
        if p.exists():
            spec = importlib.util.spec_from_file_location("utils.date_utils", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            break
    if mod is None:
        mod = importlib.import_module("utils.date_utils")

    # date_utils.stock 초기값 주입 (patch 가 교체할 대상)
    mod.stock = MagicMock(name="pykrx.stock_default")

    # sys.modules 에 등록 — patch("utils.date_utils.stock") 이 찾을 수 있도록
    sys.modules["utils.date_utils"] = mod
    utils_mod.date_utils = mod

    # pykrx 프록시 — .stock 접근 시 항상 date_utils.stock 을 반환
    # → 함수 내 `from pykrx import stock` 이 patch 로 교체된 mock 을 받음
    _mod_ref = mod  # 클로저용

    class _PykrxProxy(types.ModuleType):
        @property
        def stock(self_inner):
            return _mod_ref.stock

    pykrx_proxy = _PykrxProxy("pykrx")
    sys.modules["pykrx"] = pykrx_proxy

    return mod


date_utils = _load_date_utils()


# ══════════════════════════════════════════════════════════════
# 공통 setUp/tearDown mixin — 캐시 초기화
# ══════════════════════════════════════════════════════════════
class _CacheMixin:
    def setUp(self):
        date_utils._market_open_cache.clear()

    def tearDown(self):
        date_utils._market_open_cache.clear()


# ══════════════════════════════════════════════════════════════
# [TestGetPrevTradingDay]
# ══════════════════════════════════════════════════════════════
class TestGetPrevTradingDay(_CacheMixin, unittest.TestCase):
    def test_monday_returns_friday(self):
        """월요일 → 3일 전(금요일)."""
        monday = _kst(2024, 1, 8)           # 2024-01-08 월요일
        self.assertEqual(monday.weekday(), 0)
        result = date_utils.get_prev_trading_day(monday)
        self.assertEqual(result.date(), (monday - timedelta(days=3)).date())

    def test_tuesday_returns_monday(self):
        """화요일 → 1일 전(월요일)."""
        tuesday = _kst(2024, 1, 9)          # 2024-01-09 화요일
        self.assertEqual(tuesday.weekday(), 1)
        result = date_utils.get_prev_trading_day(tuesday)
        self.assertEqual(result.date(), (tuesday - timedelta(days=1)).date())

    def test_friday_returns_thursday(self):
        """금요일 → 1일 전(목요일)."""
        friday = _kst(2024, 1, 12)          # 2024-01-12 금요일
        self.assertEqual(friday.weekday(), 4)
        result = date_utils.get_prev_trading_day(friday)
        self.assertEqual(result.date(), (friday - timedelta(days=1)).date())

    def test_saturday_returns_none(self):
        """토요일 → None."""
        saturday = _kst(2024, 1, 13)        # 2024-01-13 토요일
        self.assertEqual(saturday.weekday(), 5)
        self.assertIsNone(date_utils.get_prev_trading_day(saturday))

    def test_sunday_returns_none(self):
        """일요일 → None."""
        sunday = _kst(2024, 1, 14)          # 2024-01-14 일요일
        self.assertEqual(sunday.weekday(), 6)
        self.assertIsNone(date_utils.get_prev_trading_day(sunday))


# ══════════════════════════════════════════════════════════════
# [TestIsMarketOpenWeekend]
# ══════════════════════════════════════════════════════════════
class TestIsMarketOpenWeekend(_CacheMixin, unittest.TestCase):
    def test_saturday_false_no_pykrx(self):
        """토요일 → False, pykrx 호출 0회."""
        saturday = _kst(2024, 1, 13)
        with patch("utils.date_utils.stock") as mock_stock:
            result = date_utils.is_market_open(saturday)
        self.assertFalse(result)
        mock_stock.get_market_ticker_list.assert_not_called()

    def test_sunday_false_no_pykrx(self):
        """일요일 → False, pykrx 호출 0회."""
        sunday = _kst(2024, 1, 14)
        with patch("utils.date_utils.stock") as mock_stock:
            result = date_utils.is_market_open(sunday)
        self.assertFalse(result)
        mock_stock.get_market_ticker_list.assert_not_called()


# ══════════════════════════════════════════════════════════════
# [TestIsMarketOpenCache]
# ARCHITECTURE §5 BUG 핵심 — pykrx 장중 중복 호출 방지
# ══════════════════════════════════════════════════════════════
class TestIsMarketOpenCache(_CacheMixin, unittest.TestCase):
    _WEEKDAY = _kst(2024, 1, 8)    # 월요일 (평일)
    _TUESDAY = _kst(2024, 1, 9)    # 화요일

    def test_first_call_hits_pykrx(self):
        """평일 첫 호출 → pykrx 1회 호출."""
        with patch("utils.date_utils.stock") as mock_stock:
            mock_stock.get_market_ticker_list.return_value = ["005930"]
            date_utils.is_market_open(self._WEEKDAY)
        mock_stock.get_market_ticker_list.assert_called_once()

    def test_second_call_uses_cache(self):
        """같은 날 2번째 호출 → pykrx 추가 호출 0회."""
        with patch("utils.date_utils.stock") as mock_stock:
            mock_stock.get_market_ticker_list.return_value = ["005930"]
            date_utils.is_market_open(self._WEEKDAY)
            date_utils.is_market_open(self._WEEKDAY)
        self.assertEqual(mock_stock.get_market_ticker_list.call_count, 1)

    def test_third_call_still_uses_cache(self):
        """3번째 호출도 캐시 사용 — pykrx 여전히 1회만."""
        with patch("utils.date_utils.stock") as mock_stock:
            mock_stock.get_market_ticker_list.return_value = ["005930"]
            for _ in range(3):
                date_utils.is_market_open(self._WEEKDAY)
        self.assertEqual(mock_stock.get_market_ticker_list.call_count, 1)

    def test_different_date_hits_pykrx_again(self):
        """다른 날짜 → 캐시 미스 → pykrx 재호출."""
        with patch("utils.date_utils.stock") as mock_stock:
            mock_stock.get_market_ticker_list.return_value = ["005930"]
            date_utils.is_market_open(self._WEEKDAY)   # 첫째 날
            date_utils.is_market_open(self._TUESDAY)   # 다른 날
        self.assertEqual(mock_stock.get_market_ticker_list.call_count, 2)

    def test_cache_key_is_yyyymmdd(self):
        """캐시 키가 정확히 'YYYYMMDD' 8자리 형식이어야 한다."""
        with patch("utils.date_utils.stock") as mock_stock:
            mock_stock.get_market_ticker_list.return_value = ["005930"]
            date_utils.is_market_open(self._WEEKDAY)

        keys = list(date_utils._market_open_cache.keys())
        self.assertEqual(len(keys), 1)
        key = keys[0]
        self.assertEqual(len(key), 8, f"캐시 키 '{key}' 가 8자리가 아님")
        self.assertTrue(key.isdigit(), f"캐시 키 '{key}' 가 숫자로만 구성되지 않음")
        # YYYYMMDD 파싱 검증
        parsed = datetime.strptime(key, "%Y%m%d")
        self.assertEqual(parsed.date(), self._WEEKDAY.date())


# ══════════════════════════════════════════════════════════════
# [TestIsMarketOpenFallback]
# ══════════════════════════════════════════════════════════════
class TestIsMarketOpenFallback(_CacheMixin, unittest.TestCase):
    _WEEKDAY = _kst(2024, 1, 8)

    def test_pykrx_exception_returns_true(self):
        """pykrx 예외 발생 → True 반환 (봇 뻗음 없음)."""
        with patch("utils.date_utils.stock") as mock_stock:
            mock_stock.get_market_ticker_list.side_effect = Exception("pykrx error")
            result = date_utils.is_market_open(self._WEEKDAY)
        self.assertTrue(result)

    def test_holiday_empty_list_returns_false(self):
        """pykrx 가 [] 반환 (공휴일) → False."""
        with patch("utils.date_utils.stock") as mock_stock:
            mock_stock.get_market_ticker_list.return_value = []
            result = date_utils.is_market_open(self._WEEKDAY)
        self.assertFalse(result)


# ══════════════════════════════════════════════════════════════
# [TestFormatters]
# ══════════════════════════════════════════════════════════════
class TestFormatters(unittest.TestCase):
    _DT = datetime(2024, 7, 5, tzinfo=KST)   # 2024-07-05

    def test_fmt_kr(self):
        """'M월 DD일' 형식 — 월은 0패딩 없음, 일은 2자리."""
        result = date_utils.fmt_kr(self._DT)
        self.assertEqual(result, "7월 05일")

    def test_fmt_num(self):
        """'YYYY-MM-DD' 형식."""
        result = date_utils.fmt_num(self._DT)
        self.assertEqual(result, "2024-07-05")

    def test_fmt_ymd(self):
        """'YYYYMMDD' 8자리 형식."""
        result = date_utils.fmt_ymd(self._DT)
        self.assertEqual(result, "20240705")
        self.assertEqual(len(result), 8)


if __name__ == "__main__":
    unittest.main()
