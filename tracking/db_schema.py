"""
tracking/db_schema.py
SQLite DB 스키마 정의 + 초기화
(Phase 3, v3.3 신규 / v3.4 / v3.5 / v4.2 / v4.3 Phase3 업데이트 / v7.0 KOSPI 지수 레벨 학습)

[역할]
DDL(테이블·인덱스·뷰 생성) + init_db() + get_conn() 만 담당.
분석·발송·수집 로직 없음.
main.py 시작 시 init_db() 1회 호출.

[테이블]
  alert_history          ← 장중봇 알림 발송 기록 (alert_recorder가 INSERT)
  performance_tracker    ← 알림 후 1/3/7일 수익률 추적 행 (performance_tracker 배치 UPDATE)
  trading_history        ← Phase 4 모의투자 매매 이력 (position_manager가 기록)
  positions              ← [v3.4] 현재 오픈 포지션 (position_manager 전용)
                           [v4.2] peak_price / stop_loss / market_env 컬럼 추가 (Trailing Stop)
  trading_principles     ← [v3.5] AI 학습용 매매 원칙 DB (principles_extractor가 기록)
  trading_journal        ← [v4.3 Phase3] 거래 완료 시 AI 회고 일지 (trading_journal 모듈)
                           Prism trading_journal_agent 기능 경량화 구현.
                           situation_analysis / judgment_evaluation / lessons / pattern_tags 포함.
  kospi_index_stats      ← [v7.0 Priority3 신규] KOSPI/KOSDAQ 지수 레벨별 매매 승률 통계
                           memory_compressor.update_index_stats()가 매주 배치 업데이트.
                           kospi_range 기준 레벨별 승률을 AI 프롬프트에 주입 가능.

[뷰]
  trigger_stats          ← 트리거별 7일 승률 집계 (weekly_report 조회용)

[ARCHITECTURE 의존성]
db_schema ← tracking/alert_recorder       (get_conn 사용)
db_schema ← tracking/performance_tracker  (get_conn 사용)
db_schema ← traders/position_manager      (get_conn 사용)
db_schema ← tracking/principles_extractor (get_conn 사용)  ← v3.5 추가
db_schema ← tracking/trading_journal      (get_conn 사용)  ← v4.3 추가
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
    [v4.2] 기존 DB 마이그레이션 자동 실행 (_migrate_v42).
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
                close_reason    TEXT,               -- take_profit_1 / take_profit_2 / stop_loss / trailing_stop / force_close / manual
                mode            TEXT DEFAULT 'VTS'  -- VTS=모의 / REAL=실전
            )
        """)

        # ── 4. 오픈 포지션 (Phase 4, v3.4 신규 / v4.2 / v4.4 확장) ──
        # [v4.2] Trailing Stop 지원을 위해 3개 컬럼 추가:
        #   peak_price  — 진입 후 최고가. Trailing Stop 기준점.
        #   stop_loss   — 현재 손절가 (원). AI 제공값 or config 기본값에서 시작,
        #                  peak_price 갱신 시 자동 상향 (하향 불가).
        #   market_env  — 진입 시 시장 환경 ("강세장" / "약세장/횡보" / "").
        #                  Trailing Stop 비율 결정에 사용 (강세 0.92, 약세 0.95).
        # [v4.4] 섹터 분산 체크 지원을 위해 1개 컬럼 추가:
        #   sector      — 진입 종목의 섹터 (아침봇 price_data["by_sector"] 기반).
        #                  동일 섹터 SECTOR_CONCENTRATION_MAX 초과 시 can_buy=False.
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
                mode            TEXT DEFAULT 'VTS',
                peak_price      INTEGER DEFAULT 0,  -- [v4.2] 진입 후 최고가 (Trailing Stop 기준)
                stop_loss       REAL,               -- [v4.2] 현재 손절가 (원, AI 제공 or 기본값)
                market_env      TEXT DEFAULT '',    -- [v4.2] 진입 시 시장 환경
                sector          TEXT DEFAULT ''     -- [v4.4] 진입 종목 섹터 (섹터 분산 체크용)
            )
        """)

        # ── 5. 매매 원칙 DB (Phase 5, v3.5 신규) ──────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS trading_principles (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT    NOT NULL,   -- ISO 8601 KST
                condition_desc  TEXT    NOT NULL,   -- 발동 조건 (예: "마감 강도 0.85↑ + 거래량 50%↑")
                action          TEXT    NOT NULL,   -- 행동 (buy / hold / skip)
                result_summary  TEXT,               -- 결과 요약 (예: "7/9 성공")
                win_count       INTEGER DEFAULT 0,  -- 성공 횟수
                total_count     INTEGER DEFAULT 0,  -- 총 발생 횟수
                win_rate        REAL    DEFAULT 0.0, -- 승률 (%)
                confidence      TEXT    DEFAULT 'low', -- low / medium / high
                is_active       INTEGER DEFAULT 1,  -- 1=활성 / 0=비활성
                trigger_source  TEXT,               -- 어떤 트리거에서 파생됐는지
                last_updated    TEXT                -- 마지막 업데이트 KST
            )
        """)

        # ── 6. 거래 일지 (Phase 3, v4.3 신규) ────────────────
        # Prism trading_journal_agent 기능 경량화 구현.
        # position_manager.close_position() 직후 trading_journal.record_journal() 자동 기록.
        #
        # situation_analysis : JSON — 매수/매도 당시 상황 비교
        #   {"buy_context_summary": str, "sell_context_summary": str, "key_changes": [str]}
        # judgment_evaluation : JSON — 판단 품질 평가
        #   {"buy_quality": str, "sell_quality": str, "missed_signals": [str]}
        # lessons : JSON 배열 — 추출된 교훈 (principles_extractor로 전달)
        #   [{"condition": str, "action": str, "priority": "high/medium/low"}]
        # pattern_tags : JSON 배열 — 패턴 태그
        #   ["강세장진입", "원칙준수익절", "트레일링스탑작동", ...]
        c.execute("""
            CREATE TABLE IF NOT EXISTS trading_journal (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                trading_id          INTEGER REFERENCES trading_history(id),
                ticker              TEXT    NOT NULL,
                name                TEXT,
                buy_time            TEXT,               -- ISO 8601 KST (매수 시각)
                sell_time           TEXT,               -- ISO 8601 KST (매도 시각)
                buy_price           INTEGER,
                sell_price          INTEGER,
                profit_rate         REAL,               -- 수익률 (%)
                trigger_source      TEXT,               -- 진입 트리거
                close_reason        TEXT,               -- 청산 사유
                market_env          TEXT,               -- 시장 환경
                situation_analysis  TEXT    DEFAULT '{}',   -- JSON: 매수/매도 상황 비교
                judgment_evaluation TEXT    DEFAULT '{}',   -- JSON: 판단 품질 평가
                lessons             TEXT    DEFAULT '[]',   -- JSON 배열: 교훈 목록
                pattern_tags        TEXT    DEFAULT '[]',   -- JSON 배열: 패턴 태그
                one_line_summary    TEXT,               -- 한 줄 요약 (장기 기억용)
                created_at          TEXT    NOT NULL    -- ISO 8601 KST
            )
        """)

        # ── 6. KOSPI 지수 레벨별 승률 통계 [v7.0 Priority3 신규] ───────
        # Prism memory_compressor_agent의 지수 변곡점 분석 기능 경량화 구현.
        # 매매 청산 후 당시 KOSPI 레벨을 기록하고 레벨별 승률을 집계.
        # 예: "KOSPI 2400~2600: 승률 72%, 평균수익 +4.3%"
        # → memory_compressor.update_index_stats() 가 매주 배치 업데이트.
        # → ai_context.py 가 AI 프롬프트에 주입 (buy_at_kospi 레벨 참고용).
        c.execute("""
            CREATE TABLE IF NOT EXISTS kospi_index_stats (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date      TEXT    NOT NULL,   -- YYYY-MM-DD (마지막 집계 기준일)
                kospi_level     INTEGER NOT NULL,   -- KOSPI 레벨 대표값 (예: 2500)
                kospi_range     TEXT    NOT NULL,   -- 레벨 범위 (예: "2400~2600")
                win_count       INTEGER DEFAULT 0,  -- 해당 레벨에서 승리(수익) 거래 수
                total_count     INTEGER DEFAULT 0,  -- 해당 레벨 전체 거래 수
                win_rate        REAL    DEFAULT 0.0, -- 승률 (%)
                avg_profit_rate REAL    DEFAULT 0.0, -- 평균 수익률 (%)
                last_updated    TEXT                -- ISO 8601 KST 마지막 업데이트 시각
            )
        """)
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_kospi_range
            ON kospi_index_stats(kospi_range)
        """)

        # ── 7. 트리거별 승률 뷰 ────────────────────────────────
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

        # ── 8. 인덱스 ─────────────────────────────────────────
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
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_principles_confidence
            ON trading_principles(confidence, trigger_source)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_journal_ticker
            ON trading_journal(ticker, created_at)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_journal_pattern
            ON trading_journal(pattern_tags)
        """)

        conn.commit()
        logger.info(f"[db] DB 초기화 완료 — {db_path}")

    except Exception as e:
        logger.error(f"[db] DB 초기화 실패: {e}")
        raise
    finally:
        conn.close()

    # [v4.2] 기존 DB에 Trailing Stop 컬럼 마이그레이션 (idempotent)
    _migrate_v42(db_path)
    # [v4.3] trading_journal 테이블 마이그레이션 (idempotent)
    _migrate_v43(db_path)
    # [v4.4] positions.sector 컬럼 마이그레이션 (idempotent)
    _migrate_v44(db_path)
    # [v6.0] trading_journal 압축 레이어 컬럼 마이그레이션 (idempotent)
    _migrate_v60(db_path)
    # [v7.0] kospi_index_stats 테이블 마이그레이션 (idempotent)
    _migrate_v70(db_path)


def _migrate_v42(db_path: str) -> None:
    """
    [v4.2 Phase 2] positions 테이블에 Trailing Stop 컬럼 추가.
    이미 존재하는 컬럼은 건너뜀 (idempotent — 여러 번 실행해도 안전).
    기존 DB를 새로 만들지 않고 ALTER TABLE로 안전하게 추가.
    """
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        # 현재 positions 컬럼 목록 조회
        c.execute("PRAGMA table_info(positions)")
        existing_cols = {row[1] for row in c.fetchall()}

        migrations = [
            ("peak_price", "INTEGER DEFAULT 0"),
            ("stop_loss",  "REAL"),
            ("market_env", "TEXT DEFAULT ''"),
        ]
        added = []
        for col_name, col_def in migrations:
            if col_name not in existing_cols:
                c.execute(f"ALTER TABLE positions ADD COLUMN {col_name} {col_def}")
                added.append(col_name)

        if added:
            conn.commit()
            logger.info(f"[db] v4.2 마이그레이션 완료 — 추가된 컬럼: {added}")
        else:
            logger.info("[db] v4.2 마이그레이션 — 이미 최신 스키마")

    except Exception as e:
        logger.warning(f"[db] v4.2 마이그레이션 경고: {e}")
    finally:
        conn.close()


def _migrate_v43(db_path: str) -> None:
    """
    [v4.3 Phase 3] trading_journal 테이블 생성 (기존 DB에 없을 경우).
    trading_principles 테이블에 is_active 컬럼 추가 (없을 경우).
    idempotent — 여러 번 실행해도 안전.
    """
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()

        # trading_journal 테이블 (없으면 생성)
        c.execute("""
            CREATE TABLE IF NOT EXISTS trading_journal (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                trading_id          INTEGER REFERENCES trading_history(id),
                ticker              TEXT    NOT NULL,
                name                TEXT,
                buy_time            TEXT,
                sell_time           TEXT,
                buy_price           INTEGER,
                sell_price          INTEGER,
                profit_rate         REAL,
                trigger_source      TEXT,
                close_reason        TEXT,
                market_env          TEXT,
                situation_analysis  TEXT    DEFAULT '{}',
                judgment_evaluation TEXT    DEFAULT '{}',
                lessons             TEXT    DEFAULT '[]',
                pattern_tags        TEXT    DEFAULT '[]',
                one_line_summary    TEXT,
                created_at          TEXT    NOT NULL
            )
        """)

        # 인덱스
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_journal_ticker
            ON trading_journal(ticker, created_at)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_journal_pattern
            ON trading_journal(pattern_tags)
        """)

        # trading_principles.is_active 컬럼 추가 (기존 DB 호환)
        c.execute("PRAGMA table_info(trading_principles)")
        existing_cols = {row[1] for row in c.fetchall()}
        if "is_active" not in existing_cols:
            c.execute("ALTER TABLE trading_principles ADD COLUMN is_active INTEGER DEFAULT 1")
            logger.info("[db] v4.3 마이그레이션 — trading_principles.is_active 추가")

        conn.commit()
        logger.info("[db] v4.3 마이그레이션 완료 — trading_journal 테이블 확인")

    except Exception as e:
        logger.warning(f"[db] v4.3 마이그레이션 경고: {e}")
    finally:
        conn.close()


def _migrate_v44(db_path: str) -> None:
    """
    [v4.4 Phase 4] positions 테이블에 sector 컬럼 추가.
    섹터 분산 체크 기능을 위해 진입 시 섹터 정보를 저장.
    이미 존재하는 컬럼은 건너뜀 (idempotent — 여러 번 실행해도 안전).
    """
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        c.execute("PRAGMA table_info(positions)")
        existing_cols = {row[1] for row in c.fetchall()}

        if "sector" not in existing_cols:
            c.execute("ALTER TABLE positions ADD COLUMN sector TEXT DEFAULT ''")
            conn.commit()
            logger.info("[db] v4.4 마이그레이션 완료 — positions.sector 컬럼 추가")
        else:
            logger.info("[db] v4.4 마이그레이션 — 이미 최신 스키마")

    except Exception as e:
        logger.warning(f"[db] v4.4 마이그레이션 경고: {e}")
    finally:
        conn.close()


def _migrate_v60(db_path: str) -> None:
    """
    [v6.0 5번/P1] trading_journal 테이블에 기억 압축 지원 컬럼 추가.
    - compression_layer: 1=원문(0~7일) / 2=AI요약(8~30일) / 3=핵심(31일+)
    - summary_text: Layer 2/3 압축 시 AI가 생성한 요약 텍스트
    - compressed_at: 압축 처리 시각 (NULL = 미압축)
    이미 존재하면 건너뜀 (idempotent).
    """
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        c.execute("PRAGMA table_info(trading_journal)")
        existing_cols = {row[1] for row in c.fetchall()}

        migrations = [
            ("compression_layer", "INTEGER DEFAULT 1"),   # 1=원문, 2=요약, 3=핵심
            ("summary_text",      "TEXT"),                # 압축 후 요약 텍스트
            ("compressed_at",     "TEXT"),                # 압축 처리 시각 (ISO 8601)
        ]
        added = []
        for col_name, col_def in migrations:
            if col_name not in existing_cols:
                c.execute(f"ALTER TABLE trading_journal ADD COLUMN {col_name} {col_def}")
                added.append(col_name)

        if added:
            conn.commit()
            logger.info(f"[db] v6.0 마이그레이션 완료 — trading_journal 추가 컬럼: {added}")
        else:
            logger.info("[db] v6.0 마이그레이션 — 이미 최신 스키마")

    except Exception as e:
        logger.warning(f"[db] v6.0 마이그레이션 경고: {e}")
    finally:
        conn.close()


def _migrate_v70(db_path: str) -> None:
    """
    [v7.0 Priority3] kospi_index_stats 테이블 추가.
    Prism memory_compressor_agent의 지수 변곡점 분석 기능 경량화 구현.
    기존 DB에 테이블이 없으면 생성 (idempotent — 여러 번 실행해도 안전).
    """
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()

        # kospi_index_stats 테이블 (없으면 생성)
        c.execute("""
            CREATE TABLE IF NOT EXISTS kospi_index_stats (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date      TEXT    NOT NULL,
                kospi_level     INTEGER NOT NULL,
                kospi_range     TEXT    NOT NULL,
                win_count       INTEGER DEFAULT 0,
                total_count     INTEGER DEFAULT 0,
                win_rate        REAL    DEFAULT 0.0,
                avg_profit_rate REAL    DEFAULT 0.0,
                last_updated    TEXT
            )
        """)
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_kospi_range
            ON kospi_index_stats(kospi_range)
        """)

        conn.commit()
        logger.info("[db] v7.0 마이그레이션 완료 — kospi_index_stats 테이블 확인")

    except Exception as e:
        logger.warning(f"[db] v7.0 마이그레이션 경고: {e}")
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
