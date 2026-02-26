"""
tests/test_watchlist_state.py
utils/watchlist_state.py 단위 테스트

[테스트 대상]
- set_watchlist / get_watchlist / is_ready / clear()
- set_market_env / get_market_env
- determine_and_set_market_env() — KOSPI 등락률 기반 판단
- set_sector_map / get_sector

[실행 방법]
    cd korea_stock_bot
    python -m pytest tests/test_watchlist_state.py -v

[설계 원칙]
- 외부 의존성 없음 (순수 인메모리 상태 모듈)
- 각 테스트마다 clear()로 상태 초기화
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123")
os.environ.setdefault("DB_PATH", "/tmp/test_bot.sqlite")

import utils.watchlist_state as ws


class TestWatchlistBasics(unittest.TestCase):
    """워치리스트 기본 CRUD 테스트"""

    def setUp(self):
        ws.clear()

    def tearDown(self):
        ws.clear()

    def test_initial_state_empty(self):
        """초기 상태: 워치리스트 비어있음"""
        self.assertEqual(ws.get_watchlist(), {})
        self.assertFalse(ws.is_ready())

    def test_set_and_get_watchlist(self):
        """저장 후 복원 — 동일 데이터 반환"""
        stocks = {
            "005930": {"종목명": "삼성전자", "전일거래량": 10000000, "우선순위": 1},
            "035420": {"종목명": "NAVER",   "전일거래량": 5000000,  "우선순위": 2},
        }
        ws.set_watchlist(stocks)
        result = ws.get_watchlist()
        self.assertEqual(result, stocks)

    def test_is_ready_after_set(self):
        """워치리스트 저장 후 is_ready() == True"""
        ws.set_watchlist({"000001": {"종목명": "테스트"}})
        self.assertTrue(ws.is_ready())

    def test_get_watchlist_returns_copy(self):
        """get_watchlist()는 복사본 반환 — 원본 수정 안 됨"""
        stocks = {"000001": {"종목명": "테스트"}}
        ws.set_watchlist(stocks)

        copy = ws.get_watchlist()
        copy["999999"] = {"종목명": "변조"}  # 외부 수정

        self.assertNotIn("999999", ws.get_watchlist())

    def test_clear_resets_watchlist(self):
        """clear() 후 워치리스트 초기화"""
        ws.set_watchlist({"000001": {}})
        ws.clear()
        self.assertEqual(ws.get_watchlist(), {})
        self.assertFalse(ws.is_ready())


class TestMarketEnv(unittest.TestCase):
    """시장 환경 저장/조회 테스트"""

    def setUp(self):
        ws.clear()

    def tearDown(self):
        ws.clear()

    def test_initial_env_empty(self):
        """초기 상태: 시장 환경 빈 문자열"""
        self.assertEqual(ws.get_market_env(), "")

    def test_set_and_get_market_env(self):
        """환경 저장 후 조회"""
        ws.set_market_env("강세장")
        self.assertEqual(ws.get_market_env(), "강세장")

    def test_clear_resets_env(self):
        """clear() 후 시장 환경 초기화"""
        ws.set_market_env("약세장/횡보")
        ws.clear()
        self.assertEqual(ws.get_market_env(), "")


class TestDetermineMarketEnv(unittest.TestCase):
    """determine_and_set_market_env() — KOSPI 등락률 기반 자동 판단"""

    def setUp(self):
        ws.clear()

    def tearDown(self):
        ws.clear()

    def test_bull_market_above_1pct(self):
        """KOSPI +1.0% 이상 → 강세장"""
        price_data = {"kospi": {"change_rate": 1.5}, "kosdaq": {"change_rate": 0.5}}
        env = ws.determine_and_set_market_env(price_data)
        self.assertEqual(env, "강세장")
        self.assertEqual(ws.get_market_env(), "강세장")

    def test_bull_market_exact_boundary(self):
        """KOSPI 정확히 +1.0% → 강세장 (경계값)"""
        price_data = {"kospi": {"change_rate": 1.0}, "kosdaq": {}}
        env = ws.determine_and_set_market_env(price_data)
        self.assertEqual(env, "강세장")

    def test_bear_market_below_minus_1pct(self):
        """KOSPI -1.0% 이하 → 약세장/횡보"""
        price_data = {"kospi": {"change_rate": -1.5}, "kosdaq": {}}
        env = ws.determine_and_set_market_env(price_data)
        self.assertEqual(env, "약세장/횡보")

    def test_neutral_market_between(self):
        """KOSPI -0.5% ~ +0.5% → 횡보"""
        for rate in [0.0, 0.5, -0.5, 0.9, -0.9]:
            ws.clear()
            price_data = {"kospi": {"change_rate": rate}, "kosdaq": {}}
            env = ws.determine_and_set_market_env(price_data)
            self.assertEqual(env, "횡보", f"rate={rate} 에서 횡보 판단 실패")

    def test_none_price_data(self):
        """price_data=None → 빈 문자열 반환"""
        env = ws.determine_and_set_market_env(None)
        self.assertEqual(env, "")

    def test_empty_kospi_data(self):
        """kospi 키 없을 때 → 횡보 (change_rate=0 처리)"""
        price_data = {"kospi": {}, "kosdaq": {}}
        env = ws.determine_and_set_market_env(price_data)
        self.assertEqual(env, "횡보")


class TestSectorMap(unittest.TestCase):
    """섹터 맵 저장/조회 테스트"""

    def setUp(self):
        ws.clear()

    def tearDown(self):
        ws.clear()

    def test_set_and_get_sector(self):
        """섹터 맵 저장 후 개별 종목 조회"""
        sector_map = {
            "005930": "반도체",
            "035720": "인터넷",
            "207940": "바이오",
        }
        ws.set_sector_map(sector_map)
        self.assertEqual(ws.get_sector("005930"), "반도체")
        self.assertEqual(ws.get_sector("035720"), "인터넷")

    def test_unknown_ticker_returns_empty(self):
        """맵에 없는 종목 → 빈 문자열 반환"""
        ws.set_sector_map({"000001": "테스트"})
        result = ws.get_sector("999999")
        self.assertEqual(result, "")

    def test_clear_resets_sector_map(self):
        """clear() 후 섹터 맵 초기화"""
        ws.set_sector_map({"005930": "반도체"})
        ws.clear()
        self.assertEqual(ws.get_sector("005930"), "")

    def test_get_sector_map_returns_full_map(self):
        """get_sector_map()으로 전체 맵 조회 가능"""
        sector_map = {"005930": "반도체", "035420": "인터넷"}
        ws.set_sector_map(sector_map)
        result = ws.get_sector_map()
        self.assertEqual(result, sector_map)


if __name__ == "__main__":
    unittest.main(verbosity=2)
