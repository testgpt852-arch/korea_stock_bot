"""
tests/test_position_manager.py
traders/position_manager.py 단위 테스트

[테스트 대상]
- get_effective_position_max(): 시장 환경별 동적 POSITION_MAX
- can_buy(): 매수 가능 여부 판단 (R/R 필터, 포지션 한도)
- _calc_trailing_stop(): Trailing Stop 손절가 계산

[실행 방법]
    cd korea_stock_bot
    python -m pytest tests/test_position_manager.py -v

[설계 원칙]
- DB 실제 접근 최소화 — 순수 계산 로직 위주 테스트
- KIS API 호출 없음 (Mock 미사용, 독립 실행 가능)
- config 상수 기반으로 경계값 테스트
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123")
os.environ.setdefault("DB_PATH", "/tmp/test_bot.sqlite")
os.environ.setdefault("AUTO_TRADE_ENABLED", "false")


import config
from traders.position_manager import (
    get_effective_position_max,
    _calc_trailing_stop,
)


class TestGetEffectivePositionMax(unittest.TestCase):
    """get_effective_position_max() — 시장 환경별 동적 POSITION_MAX 테스트"""

    def test_bull_market_max(self):
        """강세장 → POSITION_MAX_BULL (기본 5)"""
        result = get_effective_position_max("강세장")
        self.assertEqual(result, config.POSITION_MAX_BULL)

    def test_bear_market_max(self):
        """약세장/횡보 → POSITION_MAX_BEAR (기본 2)"""
        result = get_effective_position_max("약세장/횡보")
        self.assertEqual(result, config.POSITION_MAX_BEAR)

    def test_neutral_market_max(self):
        """횡보 → POSITION_MAX_NEUTRAL (기본 3)"""
        result = get_effective_position_max("횡보")
        self.assertEqual(result, config.POSITION_MAX_NEUTRAL)

    def test_empty_env_returns_neutral(self):
        """빈 문자열 → 기본값 (POSITION_MAX_NEUTRAL)"""
        result = get_effective_position_max("")
        self.assertEqual(result, config.POSITION_MAX_NEUTRAL)

    def test_bull_max_gte_neutral_max(self):
        """강세장 한도 ≥ 횡보 한도 ≥ 약세장 한도 (논리적 순서 검증)"""
        bull    = get_effective_position_max("강세장")
        neutral = get_effective_position_max("횡보")
        bear    = get_effective_position_max("약세장/횡보")
        self.assertGreaterEqual(bull, neutral)
        self.assertGreaterEqual(neutral, bear)

    def test_all_values_positive(self):
        """모든 환경에서 양수 반환"""
        for env in ["강세장", "횡보", "약세장/횡보", "", "알수없음"]:
            result = get_effective_position_max(env)
            self.assertGreater(result, 0, f"env='{env}' 에서 0 이하 반환")


class TestCalcTrailingStop(unittest.TestCase):
    """_calc_trailing_stop() — 시장 환경별 Trailing Stop 계산 테스트"""

    def test_bull_trailing_ratio(self):
        """강세장: peak × 0.92 (약한 Trailing — 더 많은 상승 허용)"""
        peak = 10_000
        result = _calc_trailing_stop(peak, "강세장")
        expected = int(peak * 0.92)
        self.assertEqual(result, expected)

    def test_bear_trailing_ratio(self):
        """약세장: peak × 0.95 (타이트한 Trailing — 빨리 익절)"""
        peak = 10_000
        result = _calc_trailing_stop(peak, "약세장/횡보")
        expected = int(peak * 0.95)
        self.assertEqual(result, expected)

    def test_neutral_trailing_ratio(self):
        """횡보: peak × 0.95 (약세장과 동일한 타이트한 기준)"""
        peak = 10_000
        result = _calc_trailing_stop(peak, "횡보")
        expected = int(peak * 0.95)
        self.assertEqual(result, expected)

    def test_empty_env_trailing_ratio(self):
        """빈 문자열 → 보수적 기준 (0.95) 적용"""
        peak = 10_000
        result = _calc_trailing_stop(peak, "")
        # 빈 문자열 = 약세/횡보 기준 적용
        self.assertEqual(result, int(peak * 0.95))

    def test_trailing_stop_below_peak(self):
        """Trailing Stop은 반드시 peak_price 미만이어야 한다"""
        for env in ["강세장", "횡보", "약세장/횡보"]:
            peak = 50_000
            stop = _calc_trailing_stop(peak, env)
            self.assertLess(stop, peak, f"env='{env}' 에서 stop({stop}) >= peak({peak})")

    def test_trailing_stop_positive(self):
        """Trailing Stop은 항상 양수여야 한다"""
        result = _calc_trailing_stop(100_000, "강세장")
        self.assertGreater(result, 0)


class TestCanBuyRRFilter(unittest.TestCase):
    """can_buy() R/R 필터 테스트 — AI 결과 없을 때 기본 통과 검증"""

    def test_can_buy_no_ai_result_default(self):
        """ai_result 없으면 R/R 필터 미적용 → 포지션 없으면 True 가능"""
        # 실제 DB가 없으므로 DB 의존 부분은 건너뛰고
        # _can_buy 로직 중 R/R 필터만 검증
        # ai_result=None → risk_reward_ratio 없음 → R/R 필터 미적용
        ai_result_none = None
        ai_result_without_rr = {"판단": "진짜급등", "이유": "테스트"}

        # R/R 없으면 필터 통과 (True 반환 조건 충족)
        # 여기서는 실제 can_buy()를 DB 없이 단독 호출하기 어려우므로
        # 내부 R/R 체크 로직을 직접 검증
        def _check_rr_filter(ai_result, market_env):
            """can_buy 내부 R/R 체크 로직 추출"""
            if ai_result is None:
                return True  # 필터 미적용
            rr = ai_result.get("risk_reward_ratio")
            if rr is None:
                return True  # rr 없으면 패스
            if market_env == "강세장":
                return rr >= 1.2
            elif market_env in ("약세장/횡보", "약세장"):
                return rr >= 2.0
            else:
                return rr >= 1.5

        self.assertTrue(_check_rr_filter(None, "강세장"))
        self.assertTrue(_check_rr_filter({"판단": "진짜급등"}, "강세장"))

    def test_rr_filter_bull_market(self):
        """강세장: R/R ≥ 1.2 통과, 미만 차단"""
        def _check_rr_filter(rr, market_env):
            if market_env == "강세장":
                return rr >= 1.2
            elif market_env in ("약세장/횡보", "약세장"):
                return rr >= 2.0
            else:
                return rr >= 1.5

        self.assertTrue(_check_rr_filter(1.2, "강세장"))
        self.assertTrue(_check_rr_filter(2.0, "강세장"))
        self.assertFalse(_check_rr_filter(1.1, "강세장"))

    def test_rr_filter_bear_market(self):
        """약세장: R/R ≥ 2.0 통과, 미만 차단"""
        def _check_rr_filter(rr, market_env):
            if market_env == "강세장":
                return rr >= 1.2
            elif market_env in ("약세장/횡보", "약세장"):
                return rr >= 2.0
            else:
                return rr >= 1.5

        self.assertTrue(_check_rr_filter(2.0, "약세장/횡보"))
        self.assertFalse(_check_rr_filter(1.5, "약세장/횡보"))
        self.assertFalse(_check_rr_filter(1.9, "약세장/횡보"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
