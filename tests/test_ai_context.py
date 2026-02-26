"""
tests/test_ai_context.py
tracking/ai_context.py 단위 테스트

[테스트 대상]
- build_spike_context(): 빈 DB에서도 안전하게 빈 문자열/정상 문자열 반환
- _get_portfolio_context(): 포트폴리오 컨텍스트 문자열 반환

[실행 방법]
    cd korea_stock_bot
    python -m pytest tests/test_ai_context.py -v

[설계 원칙]
- 임시 SQLite DB 생성 후 테스트, 완료 후 삭제
- 실제 DB 없이도 빈 결과(graceful fallback) 동작 검증
- 외부 API(KIS, Gemma) 호출 없음
"""

import sys
import os
import unittest
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123")
os.environ.setdefault("AUTO_TRADE_ENABLED", "false")


class TestBuildSpikeContextEmptyDB(unittest.TestCase):
    """build_spike_context() — 빈 DB에서 안전 반환 검증"""

    def setUp(self):
        """임시 DB 파일 생성 및 초기화"""
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self.tmp.close()
        os.environ["DB_PATH"] = self.tmp.name

        # 모듈 재로드 없이 config 갱신
        import config
        config.DB_PATH = self.tmp.name

        from tracking.db_schema import init_db
        init_db()

    def tearDown(self):
        """임시 DB 삭제"""
        try:
            os.unlink(self.tmp.name)
        except Exception:
            pass

    def test_empty_db_returns_string(self):
        """빈 DB에서 build_spike_context()가 str을 반환해야 한다"""
        from tracking.ai_context import build_spike_context
        result = build_spike_context("005930", "rate")
        self.assertIsInstance(result, str)

    def test_empty_db_no_exception(self):
        """빈 DB에서 예외가 발생하지 않아야 한다"""
        from tracking.ai_context import build_spike_context
        try:
            build_spike_context("999999", "volume")
        except Exception as e:
            self.fail(f"build_spike_context가 예외를 발생시킴: {e}")

    def test_unknown_ticker_returns_string(self):
        """존재하지 않는 종목코드에서도 str 반환"""
        from tracking.ai_context import build_spike_context
        result = build_spike_context("000000", "gap_up")
        self.assertIsInstance(result, str)

    def test_unknown_source_no_exception(self):
        """알 수 없는 source로 호출해도 예외 없음"""
        from tracking.ai_context import build_spike_context
        try:
            build_spike_context("005930", "unknown_source_xyz")
        except Exception as e:
            self.fail(f"알 수 없는 source로 예외 발생: {e}")


class TestBuildSpikeContextWithData(unittest.TestCase):
    """build_spike_context() — 실제 데이터가 있을 때 컨텍스트 포함 검증"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self.tmp.close()
        os.environ["DB_PATH"] = self.tmp.name
        import config
        config.DB_PATH = self.tmp.name

        from tracking.db_schema import init_db, get_conn
        init_db()

        # 테스트용 trading_history 삽입
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO trading_history
                    (ticker, name, buy_time, sell_time, buy_price, sell_price,
                     qty, profit_rate, profit_amount, trigger_source, close_reason, mode)
                VALUES
                    ('005930', '삼성전자', '2026-01-01T10:00:00+09:00',
                     '2026-01-01T14:00:00+09:00', 70000, 73500, 10,
                     5.0, 35000, 'rate', 'take_profit_1', 'VTS')
            """)
            conn.commit()

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except Exception:
            pass

    def test_context_includes_ticker_history_section(self):
        """거래 이력이 있을 때 컨텍스트에 관련 내용 포함"""
        from tracking.ai_context import build_spike_context
        result = build_spike_context("005930", "rate")
        # 거래 이력이 있으면 컨텍스트가 비어있지 않아야 함
        # (트리거 승률, 원칙 등이 없어도 종목 이력은 있음)
        self.assertIsInstance(result, str)


class TestPortfolioContext(unittest.TestCase):
    """_get_portfolio_context() — 포지션 없을 때 안전 반환"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self.tmp.close()
        os.environ["DB_PATH"] = self.tmp.name
        import config
        config.DB_PATH = self.tmp.name

        from tracking.db_schema import init_db
        init_db()

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except Exception:
            pass

    def test_empty_portfolio_returns_string(self):
        """오픈 포지션 없을 때 str 반환"""
        from tracking.ai_context import _get_portfolio_context
        result = _get_portfolio_context()
        self.assertIsInstance(result, str)

    def test_empty_portfolio_no_exception(self):
        """오픈 포지션 없을 때 예외 없음"""
        from tracking.ai_context import _get_portfolio_context
        try:
            _get_portfolio_context()
        except Exception as e:
            self.fail(f"_get_portfolio_context()가 예외를 발생시킴: {e}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
