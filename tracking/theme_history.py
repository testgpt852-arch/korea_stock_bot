"""
tracking/theme_history.py
이벤트→급등 섹터 이력 DB 누적 전담

[ARCHITECTURE rule #95 — 절대 금지]
- DB 저장·조회만 담당. 분석·발송·AI 호출 절대 금지.
- main.py 마감봇 완료 후 자동 기록 (closing_report.py에서 호출).

[v10.0 Phase 3 신규]

DB 테이블: theme_event_history
  id              INTEGER PRIMARY KEY AUTOINCREMENT
  date            TEXT     NOT NULL          # 날짜 (YYYY-MM-DD)
  event_type      TEXT                       # 지정학 이벤트 유형
  event_summary   TEXT                       # 이벤트 요약 (한국어)
  signal_type     TEXT                       # "신호6" | "신호7" | ""
  triggered_sector TEXT    NOT NULL          # 급등 발화 섹터명
  top_ticker      TEXT                       # 대장주 종목코드
  top_name        TEXT                       # 대장주 종목명
  top_change_pct  REAL                       # 대장주 등락률 (%)
  sector_avg_pct  REAL                       # 섹터 평균 등락률 (%)
  oracle_score    INTEGER                    # oracle_analyzer 픽 점수
  created_at      TEXT     DEFAULT CURRENT_TIMESTAMP

사용 목적:
  - 이벤트→섹터 급등 패턴 누적 학습
  - 향후 geopolitics_analyzer 가중치 조정 참고 데이터
  - 설계문서 §신호군G "과거 이벤트-섹터 패턴 DB" 구현
"""

from datetime import datetime
from utils.logger import logger
import config

try:
    from tracking.db_schema import get_conn
    _DB_AVAILABLE = True
except ImportError:
    _DB_AVAILABLE = False
    logger.warning("[theme_history] db_schema 미로드 — theme_history 비활성화")


# ── DDL ─────────────────────────────────────────────────────────────
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS theme_event_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    date             TEXT    NOT NULL,
    event_type       TEXT,
    event_summary    TEXT,
    signal_type      TEXT,
    triggered_sector TEXT    NOT NULL,
    top_ticker       TEXT,
    top_name         TEXT,
    top_change_pct   REAL,
    sector_avg_pct   REAL,
    oracle_score     INTEGER,
    created_at       TEXT    DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_IDX_DATE = """
CREATE INDEX IF NOT EXISTS idx_theme_history_date
ON theme_event_history (date)
"""


def record_closing(
    date_str:        str,
    top_gainers:     list[dict],
    signals:         list[dict],
    oracle_result:   dict | None = None,
    geo_events:      list[dict] | None = None,
) -> int:
    """
    마감봇 완료 후 이벤트→급등 섹터 이력 기록.

    rule #95: 저장만 수행. 분석·AI 호출·발송 없음.
    closing_report.py에서 마감봇 완료 후 호출.

    Args:
        date_str:      기준일 (YYYY-MM-DD 또는 YYYYMMDD)
        top_gainers:   price_collector 반환값["top_gainers"]
        signals:       signal_analyzer 반환값["signals"]
        oracle_result: oracle_analyzer.analyze() 반환값 (없으면 None)
        geo_events:    geopolitics_analyzer.analyze() 반환값 (없으면 None)

    Returns:
        기록된 행 수 (0이면 기록 없음)
    """
    if not config.THEME_HISTORY_ENABLED:
        logger.info("[theme_history] THEME_HISTORY_ENABLED=false — 기록 건너뜀")
        return 0

    if not _DB_AVAILABLE:
        return 0

    # 날짜 정규화 YYYYMMDD → YYYY-MM-DD
    if len(date_str) == 8 and "-" not in date_str:
        date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    rows_to_insert = _build_rows(date_str, top_gainers, signals, oracle_result, geo_events)

    if not rows_to_insert:
        logger.info("[theme_history] 기록할 데이터 없음")
        return 0

    inserted = 0
    try:
        with get_conn() as conn:
            # [v10.7 이슈 #6] 인라인 CREATE TABLE 제거 — db_schema.init_db()에서 일괄 초기화
            for row in rows_to_insert:
                conn.execute(
                    """
                    INSERT INTO theme_event_history
                        (date, event_type, event_summary, signal_type,
                         triggered_sector, top_ticker, top_name,
                         top_change_pct, sector_avg_pct, oracle_score)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["date"],
                        row["event_type"],
                        row["event_summary"],
                        row["signal_type"],
                        row["triggered_sector"],
                        row["top_ticker"],
                        row["top_name"],
                        row["top_change_pct"],
                        row["sector_avg_pct"],
                        row["oracle_score"],
                    ),
                )
                inserted += 1

            conn.commit()
        logger.info(f"[theme_history] {date_str} {inserted}건 이력 기록 완료")
    except Exception as e:
        logger.warning(f"[theme_history] 이력 기록 실패 (비치명적): {e}")

    return inserted


def query_sector_patterns(
    sector: str,
    limit: int = 20,
) -> list[dict]:
    """
    특정 섹터의 과거 급등 이벤트 이력 조회.

    rule #95: 조회만. 분석·발송 없음.

    Args:
        sector: 섹터명 (부분 일치)
        limit:  최대 반환 건수

    Returns:
        list[dict] — 이벤트 이력 최신순
    """
    if not _DB_AVAILABLE:
        return []

    try:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT date, event_type, event_summary, signal_type,
                       triggered_sector, top_name, top_change_pct,
                       sector_avg_pct, oracle_score
                FROM theme_event_history
                WHERE triggered_sector LIKE ?
                ORDER BY date DESC
                LIMIT ?
                """,
                (f"%{sector}%", limit),
            ).fetchall()

        return [
            {
                "date":             r[0],
                "event_type":       r[1],
                "event_summary":    r[2],
                "signal_type":      r[3],
                "triggered_sector": r[4],
                "top_name":         r[5],
                "top_change_pct":   r[6],
                "sector_avg_pct":   r[7],
                "oracle_score":     r[8],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[theme_history] 조회 실패 (비치명적): {e}")
        return []


def query_event_patterns(
    event_type: str,
    limit: int = 10,
) -> list[dict]:
    """
    특정 이벤트 유형의 과거 패턴 조회 (geopolitics_analyzer 참고용).

    rule #95: 조회만. 분석 없음.
    """
    if not _DB_AVAILABLE:
        return []

    try:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT date, event_summary, triggered_sector,
                       top_change_pct, sector_avg_pct
                FROM theme_event_history
                WHERE event_type LIKE ?
                ORDER BY date DESC
                LIMIT ?
                """,
                (f"%{event_type}%", limit),
            ).fetchall()

        return [
            {
                "date":             r[0],
                "event_summary":    r[1],
                "triggered_sector": r[2],
                "top_change_pct":   r[3],
                "sector_avg_pct":   r[4],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[theme_history] 이벤트 조회 실패 (비치명적): {e}")
        return []


# ══════════════════════════════════════════════════════════════════
# 내부 함수 (rule #95: 분석 로직 최소화 — 저장 준비만)
# ══════════════════════════════════════════════════════════════════

def _build_rows(
    date_str:      str,
    top_gainers:   list[dict],
    signals:       list[dict],
    oracle_result: dict | None,
    geo_events:    list[dict] | None,
) -> list[dict]:
    """
    DB 삽입용 row 목록 구성.

    신호가 있는 섹터 + 실제 급등 종목을 연결.
    rule #95: 조인/매핑만 수행. AI 분석 없음.
    """
    rows = []

    # oracle 픽 → 종목코드/점수 인덱싱
    oracle_scores: dict[str, int] = {}
    oracle_tickers: dict[str, str] = {}
    if oracle_result and oracle_result.get("has_data"):
        for pick in oracle_result.get("picks", []):
            name = pick.get("name", "")
            oracle_scores[name]  = pick.get("score", 0)
            oracle_tickers[name] = pick.get("ticker", "")

    # 지정학 이벤트 인덱싱 (섹터 → 이벤트)
    geo_by_sector: dict[str, dict] = {}
    for ev in (geo_events or []):
        for sec in ev.get("affected_sectors", ev.get("영향섹터", [])):
            geo_by_sector.setdefault(str(sec), ev)

    # 신호가 있는 섹터 순회
    for sig in signals:
        sector = sig.get("테마명", "")
        if not sector:
            continue

        signal_type = ""
        raw = sig.get("발화신호", "")
        if "신호6" in raw:
            signal_type = "신호6"
        elif "신호7" in raw:
            signal_type = "신호7"
        elif "신호2" in raw and "철강" in raw:
            signal_type = "신호2(철강)"

        # 해당 섹터 급등 종목 찾기 (top_gainers 기반)
        top_ticker = ""
        top_name   = ""
        top_change  = 0.0
        for g in top_gainers:
            g_sector = g.get("업종명", g.get("sector", ""))
            # 섹터명 부분 일치
            if sector in g_sector or g_sector in sector:
                top_ticker  = g.get("종목코드", g.get("ticker", ""))
                top_name    = g.get("종목명", g.get("name", ""))
                top_change  = float(g.get("등락률", g.get("change_pct", 0.0)))
                break

        # 지정학 이벤트 연결
        geo_ev = geo_by_sector.get(sector, {})
        event_type    = geo_ev.get("event_type", geo_ev.get("이벤트유형", ""))
        event_summary = geo_ev.get("event_summary_kr", geo_ev.get("이벤트요약", ""))

        # 섹터 평균 등락률 (top_gainers에서 동일 섹터 집계)
        sector_changes = [
            float(g.get("등락률", g.get("change_pct", 0.0)))
            for g in top_gainers
            if sector in g.get("업종명", g.get("sector", ""))
            or g.get("업종명", g.get("sector", "")) in sector
        ]
        sector_avg = sum(sector_changes) / len(sector_changes) if sector_changes else 0.0

        oracle_score = oracle_scores.get(top_name, 0)

        rows.append({
            "date":             date_str,
            "event_type":       event_type,
            "event_summary":    event_summary[:200] if event_summary else "",
            "signal_type":      signal_type,
            "triggered_sector": sector,
            "top_ticker":       top_ticker,
            "top_name":         top_name,
            "top_change_pct":   round(top_change, 2),
            "sector_avg_pct":   round(sector_avg, 2),
            "oracle_score":     oracle_score,
        })

    return rows
