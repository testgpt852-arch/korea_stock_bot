"""
tests/test_db_schema.py
tracking/db_schema.py 단위 테스트

[테스트 대상]
- init_db(): DB 초기화, 테이블 생성 검증
- get_conn(): 커넥션 반환 검증
- 마이그레이션 idempotent 검증 (여러 번 실행해도 안전)
- [v7.0] kospi_index_stats 테이블 생성 및 데이터 조회 검증 (Priority 3)

[실행 방법]
    cd korea_stock_bot
    python -m pytest tests/test_db_schema.py -v

[설계 원칙]
- 임시 DB 파일 사용 (테스트 간 격리)
- 각 테스트 후 임시 파일 삭제
"""

import sys
import os
import sqlite3
import unittest
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123")


class TestInitDb(unittest.TestCase):
    """init_db() — 테이블 생성 및 구조 검증"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self.tmp.close()
        os.environ["DB_PATH"] = self.tmp.name
        import config
        config.DB_PATH = self.tmp.name

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except Exception:
            pass

    def _get_tables(self):
        """현재 DB의 테이블 목록 반환"""
        conn = sqlite3.connect(self.tmp.name)
        tables = set(
            row[0] for row in
            conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        )
        conn.close()
        return tables

    def test_all_required_tables_created(self):
        """필수 테이블 7종 모두 생성 확인"""
        from tracking.db_schema import init_db
        init_db()

        tables = self._get_tables()
        required_tables = [
            "alert_history",
            "performance_tracker",
            "trading_history",
            "positions",
            "trading_principles",
            "trading_journal",
        ]
        for table in required_tables:
            self.assertIn(table, tables, f"테이블 '{table}' 미생성")

    def test_kospi_index_stats_table_created(self):
        """[Priority 3] kospi_index_stats 테이블 생성 확인"""
        from tracking.db_schema import init_db
        init_db()

        tables = self._get_tables()
        self.assertIn(
            "kospi_index_stats", tables,
            "kospi_index_stats 테이블 미생성 — Priority 3 지수 레벨 학습 미구현"
        )

    def test_kospi_index_stats_columns(self):
        """[Priority 3] kospi_index_stats 컬럼 구조 검증"""
        from tracking.db_schema import init_db
        init_db()

        conn = sqlite3.connect(self.tmp.name)
        cols = {
            row[1] for row in
            conn.execute("PRAGMA table_info(kospi_index_stats)").fetchall()
        }
        conn.close()

        required_cols = [
            "id", "trade_date", "kospi_level", "kospi_range",
            "win_count", "total_count", "win_rate", "avg_profit_rate",
        ]
        for col in required_cols:
            self.assertIn(col, cols, f"kospi_index_stats 컬럼 '{col}' 없음")

    def test_init_db_idempotent(self):
        """init_db() 여러 번 호출해도 오류 없음"""
        from tracking.db_schema import init_db
        try:
            init_db()
            init_db()  # 두 번째 호출
            init_db()  # 세 번째 호출
        except Exception as e:
            self.fail(f"init_db() 반복 호출 시 예외 발생: {e}")

    def test_trigger_stats_view_created(self):
        """trigger_stats 뷰 생성 확인"""
        from tracking.db_schema import init_db
        init_db()

        conn = sqlite3.connect(self.tmp.name)
        views = set(
            row[0] for row in
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view'"
            ).fetchall()
        )
        conn.close()
        self.assertIn("trigger_stats", views)


class TestGetConn(unittest.TestCase):
    """get_conn() — 커넥션 반환 및 CRUD 검증"""

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

    def test_get_conn_returns_connection(self):
        """get_conn()이 sqlite3.Connection을 반환해야 한다"""
        from tracking.db_schema import get_conn
        conn = get_conn()
        self.assertIsInstance(conn, sqlite3.Connection)
        conn.close()

    def test_basic_insert_and_select(self):
        """기본 INSERT / SELECT 동작 확인"""
        from tracking.db_schema import get_conn
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO alert_history
                    (ticker, name, alert_time, alert_date, change_rate, source, price_at_alert)
                VALUES ('005930', '삼성전자', '2026-02-27T10:00:00+09:00',
                        '20260227', 3.5, 'rate', 70000)
            """)
            conn.commit()

        with get_conn() as conn:
            row = conn.execute(
                "SELECT ticker, name FROM alert_history WHERE ticker='005930'"
            ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], "005930")
        self.assertEqual(row[1], "삼성전자")

    def test_positions_table_columns(self):
        """positions 테이블 v4.2/v4.4 컬럼 모두 존재 확인"""
        from tracking.db_schema import get_conn
        with get_conn() as conn:
            cols = {
                row[1] for row in
                conn.execute("PRAGMA table_info(positions)").fetchall()
            }

        v42_cols = ["peak_price", "stop_loss", "market_env"]
        v44_cols = ["sector"]
        for col in v42_cols + v44_cols:
            self.assertIn(col, cols, f"positions.{col} 컬럼 없음")

    def test_trading_journal_compression_columns(self):
        """trading_journal v6.0 압축 컬럼 존재 확인"""
        from tracking.db_schema import get_conn
        with get_conn() as conn:
            cols = {
                row[1] for row in
                conn.execute("PRAGMA table_info(trading_journal)").fetchall()
            }

        v60_cols = ["compression_layer", "summary_text", "compressed_at"]
        for col in v60_cols:
            self.assertIn(col, cols, f"trading_journal.{col} 컬럼 없음")


if __name__ == "__main__":
    unittest.main(verbosity=2)
