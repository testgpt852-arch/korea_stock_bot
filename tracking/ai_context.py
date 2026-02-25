"""
tracking/ai_context.py
AI 프롬프트에 주입할 컨텍스트 조회 전담 (Phase 5, v3.5 신규)

[역할]
DB에서 세 가지 컨텍스트를 읽어 문자열로 조합해 반환:
  1. 트리거별 7일 승률  (trigger_stats 뷰 → performance_tracker)
  2. 종목별 과거 거래 이력  (trading_history 테이블)
  3. 고신뢰(high) 매매 원칙  (trading_principles 테이블)

반환된 문자열은 ai_analyzer.analyze_spike() 에 ai_context 파라미터로 전달된다.
프롬프트 주입 방식은 ai_analyzer가 담당 — 이 모듈은 "텍스트 조합"만 한다.

[실행 시점]
realtime_alert._send_ai_followup() → build_spike_context(ticker, source) 동기 호출
(loop.run_in_executor 경유)

[데이터가 없을 때]
테이블이 비어있거나 해당 종목 이력이 없으면 빈 문자열("") 반환.
AI 프롬프트에서 컨텍스트 없으면 기존 방식대로 판단.

[ARCHITECTURE 의존성]
ai_context ← tracking/db_schema  (get_conn)
ai_context ← tracking/performance_tracker  (trigger_stats 뷰)
ai_context ← traders/position_manager  (trading_history — 읽기 전용)
ai_context → analyzers/ai_analyzer  (문자열 반환만 — 직접 의존 없음)
ai_context → reports/realtime_alert  (build_spike_context 호출)

[절대 금지 규칙 — ARCHITECTURE #29]
이 파일은 DB 조회 + 문자열 반환만 담당.
AI API 호출·텔레그램 발송·매수 로직 절대 금지.
모든 함수는 동기(sync) — realtime_alert 에서 run_in_executor 경유 호출.
"""

from __future__ import annotations

import sqlite3
from utils.logger import logger
import tracking.db_schema as db_schema


# ── 공개 API ──────────────────────────────────────────────────

def build_spike_context(ticker: str, source: str) -> str:
    """
    급등 종목 AI 분석용 컨텍스트 문자열 조합.
    realtime_alert._send_ai_followup() 에서 호출.

    Args:
        ticker: 종목코드 (6자리)
        source: 감지 소스 (volume / rate / gap_up / websocket 등)

    Returns:
        AI 프롬프트에 주입할 컨텍스트 문자열.
        데이터 없으면 "".
    """
    parts: list[str] = []

    trigger_line = _get_trigger_stats_line(source)
    if trigger_line:
        parts.append(trigger_line)

    history_line = _get_ticker_history(ticker)
    if history_line:
        parts.append(history_line)

    principles_line = _get_high_conf_principles(source)
    if principles_line:
        parts.append(principles_line)

    return "\n".join(parts)


# ── 내부 함수 ─────────────────────────────────────────────────

def _get_trigger_stats_line(source: str) -> str:
    """
    trigger_stats 뷰에서 트리거별 7일 승률 조회 → 프롬프트용 1줄 반환.
    데이터 부족(tracked < 5) 시 빈 문자열 반환.
    """
    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT trigger_type, win_rate_7d, avg_return_7d, tracked_7d
            FROM trigger_stats
            WHERE tracked_7d >= 5
            ORDER BY win_rate_7d DESC
        """)
        rows = c.fetchall()
        if not rows:
            return ""

        # 전체 트리거 요약
        stats_parts = [
            f"{r[0]}: 승률 {r[1]:.0f}%(n={r[3]}, 평균 {r[2]:+.1f}%)"
            for r in rows
        ]

        # 현재 감지 소스 강조
        current_rate = next((r[1] for r in rows if r[0] == source), None)
        note = f" ※ 현재 감지소스 [{source}] 7일 승률 {current_rate:.0f}%" if current_rate else ""

        return f"[트리거 성과] {' / '.join(stats_parts)}{note}"

    except Exception as e:
        logger.debug(f"[ai_context] trigger_stats 조회 실패: {e}")
        return ""
    finally:
        conn.close()


def _get_ticker_history(ticker: str) -> str:
    """
    trading_history 에서 해당 종목 과거 거래 최대 3건 조회 → 프롬프트용 1줄 반환.
    청산 완료(sell_time NOT NULL)된 거래만 조회.
    """
    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT name, buy_price, sell_price, profit_rate, close_reason,
                   SUBSTR(buy_time, 1, 10)
            FROM trading_history
            WHERE ticker = ?
              AND sell_time IS NOT NULL
            ORDER BY buy_time DESC
            LIMIT 3
        """, (ticker,))
        rows = c.fetchall()
        if not rows:
            return ""

        name = rows[0][0] or ticker
        items: list[str] = []
        for row in rows:
            _, buy, sell, rate, reason, date = row
            if rate is None:
                continue
            icon = "✅" if rate > 0 else "❌"
            reason_short = {
                "take_profit_1": "1차익절",
                "take_profit_2": "2차익절",
                "stop_loss":     "손절",
                "force_close":   "강제청산",
            }.get(reason or "", reason or "?")
            items.append(f"{icon}{rate:+.1f}%({reason_short}, {date})")

        if not items:
            return ""

        avg = sum(
            r[3] for r in rows if r[3] is not None
        ) / len([r for r in rows if r[3] is not None])

        return (
            f"[{name} 과거 거래 {len(items)}건] "
            f"{' / '.join(items)} — 평균 {avg:+.1f}%"
        )

    except Exception as e:
        logger.debug(f"[ai_context] 종목 이력 조회 실패 ({ticker}): {e}")
        return ""
    finally:
        conn.close()


def _get_high_conf_principles(source: str) -> str:
    """
    trading_principles 에서 confidence='high' 원칙 최대 3개 조회 → 프롬프트용 반환.
    trigger_source 가 source 와 일치하거나 NULL인 원칙만.
    """
    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT condition_desc, action, result_summary, win_rate
            FROM trading_principles
            WHERE confidence = 'high'
              AND (trigger_source = ? OR trigger_source IS NULL)
            ORDER BY win_rate DESC
            LIMIT 3
        """, (source,))
        rows = c.fetchall()
        if not rows:
            return ""

        items = [
            f"'{r[0]}' → {r[1]} ({r[2]}, 승률 {r[3]:.0f}%)"
            for r in rows
        ]
        return f"[매매 원칙] {' / '.join(items)}"

    except Exception as e:
        logger.debug(f"[ai_context] 매매 원칙 조회 실패: {e}")
        return ""
    finally:
        conn.close()
