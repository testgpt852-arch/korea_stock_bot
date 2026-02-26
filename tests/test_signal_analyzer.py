"""
tests/test_signal_analyzer.py
analyzers/signal_analyzer.py 단위 테스트

[테스트 대상]
- _dart_strength(): 공시 강도 판단 (1~5)
- _dart_to_theme(): 공시 → 테마명 변환
- analyze(): 신호 1~5 통합 분석 (입력/출력 구조 검증)

[실행 방법]
    cd korea_stock_bot
    python -m pytest tests/test_signal_analyzer.py -v

[설계 원칙]
- 외부 API 호출 없음 (순수 로직만 테스트)
- config 의존성 최소화 (import 전에 환경변수 패치)
- 각 테스트는 독립적으로 실행 가능
"""

import sys
import os
import unittest

# 프로젝트 루트를 sys.path에 추가 (상대 import 허용)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# config 의존 모듈 import 전 최소 환경변수 설정
os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123")
os.environ.setdefault("DB_PATH", "/tmp/test_bot.sqlite")

from analyzers.signal_analyzer import (
    _dart_strength,
    _dart_to_theme,
    analyze,
)


class TestDartStrength(unittest.TestCase):
    """_dart_strength() — 공시 강도 점수 반환 테스트"""

    def test_strength_5_for_contract(self):
        """수주/단일판매공급계약 → 강도 5"""
        dart = {"공시종류": "단일판매공급계약체결", "종목명": "테스트"}
        self.assertEqual(_dart_strength(dart), 5)

    def test_strength_5_for_order(self):
        """수주 공시 → 강도 5"""
        dart = {"공시종류": "수주공시", "종목명": "테스트"}
        self.assertEqual(_dart_strength(dart), 5)

    def test_strength_4_for_dividend(self):
        """배당 결정 → 강도 4"""
        dart = {"공시종류": "배당결정", "종목명": "테스트"}
        self.assertEqual(_dart_strength(dart), 4)

    def test_strength_4_for_buyback(self):
        """자사주 취득 결정 → 강도 4"""
        dart = {"공시종류": "자사주취득결정", "종목명": "테스트"}
        self.assertEqual(_dart_strength(dart), 4)

    def test_strength_3_for_mou(self):
        """MOU 체결 → 강도 3"""
        dart = {"공시종류": "MOU체결", "종목명": "테스트"}
        self.assertEqual(_dart_strength(dart), 3)

    def test_strength_3_for_major_shareholder(self):
        """주요주주 공시 → 강도 3"""
        dart = {"공시종류": "주요주주소유주식변동신고서", "종목명": "테스트"}
        self.assertEqual(_dart_strength(dart), 3)

    def test_strength_1_for_unknown(self):
        """알 수 없는 공시 → 강도 1 (기본값)"""
        dart = {"공시종류": "임시주주총회소집공고", "종목명": "테스트"}
        self.assertEqual(_dart_strength(dart), 1)

    def test_strength_5_for_patent(self):
        """특허 관련 공시 → 강도 5"""
        dart = {"공시종류": "특허결정통지서", "종목명": "테스트"}
        self.assertEqual(_dart_strength(dart), 5)


class TestDartToTheme(unittest.TestCase):
    """_dart_to_theme() — 공시 종류 → 테마명 변환 테스트"""

    def test_contract_theme(self):
        """수주/공급계약 → '{종목명} 수주'"""
        result = _dart_to_theme("단일판매공급계약체결", "삼성전자")
        self.assertIn("수주", result)
        self.assertIn("삼성전자", result)

    def test_dividend_theme(self):
        """배당 → '{종목명} 배당'"""
        result = _dart_to_theme("배당결정", "현대차")
        self.assertIn("배당", result)
        self.assertIn("현대차", result)

    def test_buyback_theme(self):
        """자사주 → '{종목명} 자사주'"""
        result = _dart_to_theme("자사주취득결정", "LG전자")
        self.assertIn("자사주", result)

    def test_unknown_theme(self):
        """알 수 없는 공시 → '{종목명} 공시'"""
        result = _dart_to_theme("임시주주총회공고", "카카오")
        self.assertIn("공시", result)
        self.assertIn("카카오", result)

    def test_patent_theme(self):
        """특허/판결 → '{종목명} 특허/소송'"""
        result = _dart_to_theme("특허결정", "셀트리온")
        self.assertIn("특허", result)


class TestAnalyzeOutputStructure(unittest.TestCase):
    """analyze() — 반환값 구조 및 필수 키 검증"""

    def _make_empty_inputs(self):
        """최소 입력 데이터 생성"""
        dart_data = []
        market_data = {
            "us_market": {
                "nasdaq": "+0.5%", "sp500": "+0.3%", "dow": "+0.2%",
                "summary": "혼조세", "신뢰도": "yfinance",
                "sectors": {}
            },
            "commodities": {}
        }
        news_data = {"reports": [], "news": []}
        price_data = {
            "kospi":  {"change_rate": 0.5, "close": 2800},
            "kosdaq": {"change_rate": 1.2, "close": 900},
            "upper_limit": [],
            "top_gainers": [],
            "top_losers": [],
            "institutional": [],
            "by_sector": {},
            "by_code": {},
            "by_name": {},
        }
        return dart_data, market_data, news_data, price_data

    def test_analyze_returns_dict(self):
        """analyze() 가 dict를 반환해야 한다"""
        dart_data, market_data, news_data, price_data = self._make_empty_inputs()
        result = analyze(dart_data, market_data, news_data, price_data)
        self.assertIsInstance(result, dict)

    def test_analyze_has_required_keys(self):
        """반환 dict에 필수 키가 있어야 한다"""
        dart_data, market_data, news_data, price_data = self._make_empty_inputs()
        result = analyze(dart_data, market_data, news_data, price_data)

        required_keys = ["signals", "market_summary"]
        for key in required_keys:
            self.assertIn(key, result, f"필수 키 '{key}' 없음")

    def test_analyze_signals_is_list(self):
        """signals 값이 list여야 한다"""
        dart_data, market_data, news_data, price_data = self._make_empty_inputs()
        result = analyze(dart_data, market_data, news_data, price_data)
        self.assertIsInstance(result["signals"], list)

    def test_analyze_dart_signal_structure(self):
        """DART 공시 신호가 있을 때 필수 필드 검증"""
        dart_data = [{
            "종목명": "테스트주식",
            "종목코드": "123456",
            "공시종류": "단일판매공급계약체결",
            "핵심내용": "1000억 수주",
            "공시시각": "09:00",
            "신뢰도": "DART",
            "내부자여부": False,
        }]
        _, market_data, news_data, price_data = self._make_empty_inputs()
        result = analyze(dart_data, market_data, news_data, price_data)

        dart_signals = [s for s in result["signals"] if "신호1" in s.get("발화신호", "")]
        self.assertGreater(len(dart_signals), 0, "DART 신호가 생성되지 않음")

        sig = dart_signals[0]
        signal_required_keys = ["테마명", "발화신호", "강도", "신뢰도", "관련종목"]
        for key in signal_required_keys:
            self.assertIn(key, sig, f"신호 필수 필드 '{key}' 없음")

    def test_analyze_no_dart_signal_for_low_strength(self):
        """강도 0 공시 (해당 없음 → 기본값 1 이상이므로 신호 생성됨)"""
        # _dart_strength 는 최소 1 반환하므로 신호가 생성되어야 함
        dart_data = [{
            "종목명": "테스트주식",
            "종목코드": "123456",
            "공시종류": "임시주주총회공고",  # 강도 1
            "핵심내용": "임시 주총",
            "공시시각": "09:00",
            "신뢰도": "DART",
            "내부자여부": False,
        }]
        _, market_data, news_data, price_data = self._make_empty_inputs()
        result = analyze(dart_data, market_data, news_data, price_data)
        # 강도 1이어도 신호가 생성 (0이 아니면 포함)
        dart_signals = [s for s in result["signals"] if "신호1" in s.get("발화신호", "")]
        self.assertEqual(len(dart_signals), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
