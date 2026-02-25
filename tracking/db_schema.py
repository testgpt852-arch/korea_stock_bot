"""
tracking/db_schema.py
SQLite DB 스키마 정의 + 초기화 (Phase 3, v3.3 신규 / v3.4 업데이트)

[역할]
DDL(테이블·인덱스·뷰 생성) + init_db() + get_conn() 만 담당.
분석·발송·수집 로직 없음.
main.py 시작 시 init_db() 1회 호출.

[테이블]
  alert_history          ← 장중봇 알림 발송 기록 (alert_recorder가 INSERT)
  performance_tracker    ← 알림 후 1/3/7일 수익률 추적 행 (performance_tracker 배치 UPDATE)
  trading_history        ← Phase 4 모의투자 매매 이력 (position_manager가 기록)
  positions              ← [v3.4] 현재 오픈 포지션 (position_manager 전용)

[뷰]
  trigger_stats          ← 트리거별 7일 승률 집계 (weekly_report 조회용)

[ARCHITECTURE 의존성]
db_schema ← tracking/alert_recorder   (get_conn 사용)
db_schema ← tracking/performance_tracker (get_conn 사용)
db_schema ← traders/position_manager  (get_conn 사용)
db_schema ← main.py  (init_db 1회 호출)
db_schema → (없음, 최하위 모듈)

[절대 금지 규칙 — ARCHITECTURE #18]
이 파일은 DDL + init_db() + get_conn() 만 담당.
분석·발송·수집 로직 절대 금지.
"""

import os
import sqlite3
from utils.logger import logger
import config


def init_db() -> None:
    """
    DB 파일 생성 + 테이블·인덱스·뷰 초기화.
    main.py 시작 시 1회 호출. 이미 존재하면 변경 없음 (IF NOT EXISTS).
    """
    db_path = config.DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()

        # ── 1. 알림 이력 ──────────────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS alert_history (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker         TEXT    NOT NULL,
                name           TEXT,
                alert_time     TEXT    NOT NULL,   -- ISO 8601 KST (예: 2026-02-26T10:15:30+09:00)
                alert_date     TEXT    NOT NULL,   -- YYYYMMDD (날짜별 집계용)
                change_rate    REAL,              -- 알림 시점 누적 등락률 (%)
                delta_rate     REAL,              -- 직전대비 추가 등락률 (%) — REST 감지 시
                source         TEXT,              -- volume / rate / gap_up / websocket
                price_at_alert INTEGER            -- 알림 시점 현재가 (원, 수익률 계산 기준)
            )
        """)

        # ── 2. 수익률 추적 ────────────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS performance_tracker (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id         INTEGER REFERENCES alert_history(id),
                ticker           TEXT    NOT NULL,
                alert_date       TEXT    NOT NULL,   -- YYYYMMDD

                price_at_alert   INTEGER,

                -- 추적 완료 날짜 (NULL = 아직 미추적)
                tracked_date_1d  TEXT,
                tracked_date_3d  TEXT,
                tracked_date_7d  TEXT,

                -- 추적 시점 종가
                price_1d   INTEGER,
                price_3d   INTEGER,
                price_7d   INTEGER,

                -- 수익률 (%)
                return_1d  REAL,
                return_3d  REAL,
                return_7d  REAL,

                -- 추적 완료 플래그 (0 = 미완, 1 = 완료)
                done_1d  INTEGER DEFAULT 0,
                done_3d  INTEGER DEFAULT 0,
                done_7d  INTEGER DEFAULT 0
            )
        """)

        # ── 3. 매매 이력 (Phase 4 — v3.4 활성화) ─────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS trading_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker          TEXT,
                name            TEXT,
                buy_time        TEXT,               -- ISO 8601 KST
                sell_time       TEXT,               -- NULL = 미청산
                buy_price       INTEGER,
                sell_price      INTEGER,
                qty             INTEGER,
                profit_rate     REAL,               -- 수익률 (%)
                profit_amount   INTEGER,            -- 손익 금액 (원)
                trigger_source  TEXT,               -- 매수 트리거 종류
                close_reason    TEXT,               -- take_profit_1 / take_profit_2 / stop_loss / force_close / manual
                mode            TEXT DEFAULT 'VTS'  -- VTS=모의 / REAL=실전
            )
        """)

        # ── 4. 오픈 포지션 (Phase 4, v3.4 신규) ──────────────
        # 현재 보유 중인 포지션 전용 테이블.
        # position_manager.open_position() 에서 INSERT,
        # position_manager.close_position() 에서 DELETE.
        # trading_history 와 1:1 대응 (position_id = trading_history.id).
        c.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                trading_id      INTEGER REFERENCES trading_history(id),
                ticker          TEXT    NOT NULL,
                name            TEXT,
                buy_time        TEXT    NOT NULL,   -- ISO 8601 KST
                buy_price       INTEGER NOT NULL,
                qty             INTEGER NOT NULL,
                trigger_source  TEXT,               -- 진입 트리거 종류
                mode            TEXT DEFAULT 'VTS'
            )
        """)

        # ── 5. 트리거별 승률 뷰 ────────────────────────────────
        c.execute("""
            CREATE VIEW IF NOT EXISTS trigger_stats AS
            SELECT
                ah.source                                                  AS trigger_type,
                COUNT(*)                                                   AS total_alerts,
                SUM(CASE WHEN pt.done_7d = 1 THEN 1 ELSE 0 END)           AS tracked_7d,
                SUM(CASE WHEN pt.return_7d > 0 THEN 1 ELSE 0 END)         AS win_7d,
                ROUND(
                    100.0 * SUM(CASE WHEN pt.return_7d > 0 THEN 1 ELSE 0 END)
                          / NULLIF(SUM(CASE WHEN pt.done_7d = 1 THEN 1 ELSE 0 END), 0),
                    1
                )                                                          AS win_rate_7d,
                ROUND(AVG(CASE WHEN pt.done_7d = 1 THEN pt.return_7d END), 2) AS avg_return_7d
            FROM alert_history ah
            LEFT JOIN performance_tracker pt ON pt.alert_id = ah.id
            GROUP BY ah.source
        """)

        # ── 6. 인덱스 ─────────────────────────────────────────
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_alert_date
            ON alert_history(alert_date)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_perf_done
            ON performance_tracker(done_1d, done_3d, done_7d, alert_date)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_positions_ticker
            ON positions(ticker)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_trading_hist_date
            ON trading_history(buy_time)
        """)

        conn.commit()
        logger.info(f"[db] DB 초기화 완료 — {db_path}")

    except Exception as e:
        logger.error(f"[db] DB 초기화 실패: {e}")
        raise
    finally:
        conn.close()


def get_conn() -> sqlite3.Connection:
    """
    DB 커넥션 반환. 호출부에서 명시적 close() 또는 with 문 필수.

    Usage:
        conn = db_schema.get_conn()
        try:
            c = conn.cursor()
            c.execute(...)
            conn.commit()
        finally:
            conn.close()
    """
    return sqlite3.connect(config.DB_PATH)
