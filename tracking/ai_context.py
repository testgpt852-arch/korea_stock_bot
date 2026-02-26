"""
tracking/ai_context.py
AI 프롬프트에 주입할 컨텍스트 조회 전담 (Phase 5, v3.5 신규 / v4.3 Phase3 업데이트)

[역할]
DB에서 네 가지 컨텍스트를 읽어 문자열로 조합해 반환:
  1. 트리거별 7일 승률  (trigger_stats 뷰 → performance_tracker)
  2. 종목별 과거 거래 이력  (trading_history 테이블)
  3. 고신뢰(high) 매매 원칙  (trading_principles 테이블)
  4. [v4.3] 종목별 거래 일지 요약  (trading_journal 테이블) — Prism 벤치마킹
     → 매수 당시 판단 + 교훈이 다음 매수 결정에 반영

반환된 문자열은 ai_analyzer.analyze_spike() 에 ai_context 파라미터로 전달된다.
프롬프트 주입 방식은 ai_analyzer가 담당 — 이 모듈은 "텍스트 조합"만 한다.

[실행 시점]
realtime_alert._send_ai_followup() → build_spike_context(ticker, source) 동기 호출
(loop.run_in_executor 경유)

[데이터가 없을 때]
테이블이 비어있거나 해당 종목 이력이 없으면 빈 문자열("") 반환.
AI 프롬프트에서 컨텍스트 없으면 기존 방식대로 판단.

[ARCHITECTURE 의존성]
ai_context ← tracking/db_schema       (get_conn)
ai_context ← tracking/performance_tracker  (trigger_stats 뷰)
ai_context ← traders/position_manager  (trading_history — 읽기 전용)
ai_context ← tracking/trading_journal  (get_journal_context — 읽기 전용)  ← v4.3 추가
ai_context → analyzers/ai_analyzer    (문자열 반환만 — 직접 의존 없음)
ai_context → reports/realtime_alert   (build_spike_context 호출)

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

    [v4.3] trading_journal 컨텍스트 추가:
    → 같은 종목을 과거에 매매한 경험 + 추출된 교훈이 이번 판단에 반영

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

    # [v4.3] 거래 일지 컨텍스트 (Prism 벤치마킹 — 매수 vs 매도 교훈)
    journal_line = _get_journal_context(ticker)
    if journal_line:
        parts.append(journal_line)

    # [v4.4] 포트폴리오 현황 컨텍스트 (Phase 4 신규)
    # 현재 보유 종목 목록 + 총 노출도 + 당일 P&L → AI가 집중 위험 판단 가능
    portfolio_line = _get_portfolio_context()
    if portfolio_line:
        parts.append(portfolio_line)

    # [v7.0 Priority3] KOSPI 지수 레벨별 과거 승률 컨텍스트
    # 현재 시장 환경(watchlist_state)에서 KOSPI 레벨을 가져와 해당 레벨 승률 주입
    index_line = _get_index_level_context()
    if index_line:
        parts.append(index_line)

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


def _get_journal_context(ticker: str) -> str:
    """
    [v4.3 Phase 3] trading_journal에서 같은 종목 과거 일지 조회 → 프롬프트용 반환.
    Prism get_context_for_ticker() 벤치마킹 — 매수 당시 vs 매도 당시 교훈 주입.
    최근 2건만 (토큰 절약).
    """
    try:
        from tracking.trading_journal import get_journal_context
        return get_journal_context(ticker)
    except Exception as e:
        logger.debug(f"[ai_context] journal 컨텍스트 조회 실패 ({ticker}): {e}")
        return ""


def _get_high_conf_principles(source: str) -> str:
    """
    trading_principles 에서 confidence='high' 원칙 최대 3개 조회 → 프롬프트용 반환.
    trigger_source 가 source 와 일치하거나 NULL인 원칙만.
    [v4.3] is_active=1 필터 추가 (db_schema v4.3에서 컬럼 추가)
    """
    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        # is_active 컬럼 존재 여부 확인 (구버전 DB 호환)
        c.execute("PRAGMA table_info(trading_principles)")
        cols = {row[1] for row in c.fetchall()}
        active_filter = "AND is_active = 1" if "is_active" in cols else ""

        c.execute(f"""
            SELECT condition_desc, action, result_summary, win_rate
            FROM trading_principles
            WHERE confidence = 'high'
              AND (trigger_source = ? OR trigger_source IS NULL)
              {active_filter}
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


def _get_portfolio_context() -> str:
    """
    [v4.4 Phase 4] 현재 오픈 포지션 현황 → 프롬프트용 문자열 반환.
    AI가 기존 보유 종목·섹터·총 노출도를 인식하고 신규 매수 판단에 반영할 수 있게 함.
    Prism portfolio_intelligence_agent 경량화 구현.

    반환 예시:
    [현재 포트폴리오] 보유 2종목 (A주 +3.2% 반도체, B주 -0.5% 바이오)
                     총 노출 200만원 / 당일 미실현 P&L: +13,400원
    """
    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        import config as _config
        c.execute("""
            SELECT ticker, name, buy_price, qty, peak_price, stop_loss, market_env, sector
            FROM positions WHERE mode = ?
            ORDER BY buy_time
        """, (_config.TRADING_MODE,))
        rows = c.fetchall()
    except Exception as e:
        logger.debug(f"[ai_context] 포트폴리오 조회 실패: {e}")
        return ""
    finally:
        conn.close()

    if not rows:
        return ""

    # 현재가 조회 시도 (실패해도 매수가 기준으로 표시)
    try:
        from kis import order_client
        price_getter = lambda ticker: (
            order_client.get_current_price(ticker).get("현재가", 0)
        )
    except Exception:
        price_getter = lambda ticker: 0

    total_invested  = 0
    total_unrealized = 0
    items: list[str] = []

    for row in rows:
        ticker, name, buy_price, qty, peak_price, stop_loss, menv, sector = row
        try:
            cur = price_getter(ticker)
        except Exception:
            cur = 0
        if cur <= 0:
            cur = buy_price  # 조회 실패 시 매수가로 대체

        invested   = buy_price * qty
        unrealized = (cur - buy_price) * qty
        profit_pct = (cur - buy_price) / buy_price * 100 if buy_price > 0 else 0

        total_invested   += invested
        total_unrealized += unrealized

        sector_tag = f" {sector}" if sector else ""
        items.append(
            f"{name}({ticker}) {profit_pct:+.1f}%{sector_tag}"
        )

    if not items:
        return ""

    pnl_sign = "+" if total_unrealized >= 0 else ""
    return (
        f"[현재 포트폴리오] 보유 {len(items)}종목 ({', '.join(items)}) | "
        f"총 노출 {total_invested//10000}만원 / "
        f"미실현 P&L: {pnl_sign}{total_unrealized:,}원"
    )


def _get_index_level_context() -> str:
    """
    [v7.0 Priority3] 현재 KOSPI 레벨 기반 과거 승률 컨텍스트 반환.
    kospi_index_stats 테이블에서 해당 레벨 구간 + 인접 구간 승률 조회.

    [절대 금지 규칙 — ARCHITECTURE #29]
    DB 조회 + 문자열 반환만. AI API 호출 금지.
    """
    try:
        # 현재 KOSPI 레벨은 watchlist_state에 저장된 시장 환경 정보에서 추론
        # (아침봇이 price_data를 가지고 있으나 여기서 직접 접근 불가)
        # → memory_compressor.get_index_context()에 위임
        from tracking.memory_compressor import get_index_context
        return get_index_context(current_kospi=None)
    except Exception as e:
        logger.debug(f"[ai_context] _get_index_level_context 실패 (비치명적): {e}")
        return ""
