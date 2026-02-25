"""
reports/closing_report.py
마감봇 보고서 조립 전담 (18:30 실행)

[ARCHITECTURE 의존성]
closing_report → price_collector, theme_analyzer, ai_analyzer, telegram_bot
수정 시 이 파일만 수정 (나머지 건드리지 않음)

[실행 흐름]
① price_collector  → 전종목 등락률 + 기관/공매도
② ai_analyzer      → 테마 그룹핑 + 소외주 식별 (v6.0 #5·#6 자동화)
③ theme_analyzer   → 소외도 수치 계산 (ai_analyzer 결과 활용)
④ telegram_bot     → 발송
"""

from datetime import datetime
from utils.logger import logger
from utils.date_utils import get_today, get_prev_trading_day, fmt_kr, is_market_open
import collectors.price_collector as price_collector
import analyzers.theme_analyzer   as theme_analyzer
import analyzers.ai_analyzer      as ai_analyzer
import notifiers.telegram_bot     as telegram_bot


async def run() -> None:
    """마감봇 메인 실행 함수 (AsyncIOScheduler에서 호출)"""
    today  = get_today()
    target = _resolve_target_date(today)

    if target is None:
        logger.error("[closing] 유효한 거래일 없음 — 종료")
        return

    today_str  = fmt_kr(today)
    target_str = fmt_kr(target)
    logger.info(f"[closing] 마감봇 시작 — {today_str} (기준: {target_str} 마감)")

    try:
        # ── 1. 가격·수급 데이터 수집 ──────────────────────────
        logger.info("[closing] 가격·수급 데이터 수집 중...")
        price_result = price_collector.collect_daily(target)

        # ── 2. AI 테마 그룹핑 (v6.0 핵심) ─────────────────────
        # ai_analyzer가 상한가+급등 종목을 테마별로 묶고 소외주를 식별
        # → theme_analyzer 호환 signals 형식으로 반환
        logger.info("[closing] AI 테마 그룹핑 중...")
        ai_signals = ai_analyzer.analyze_closing(price_result)

        # AI 실패 또는 저변동 장세 시 fallback: 상한가/급등 단순 나열
        if not ai_signals:
            logger.info("[closing] AI 테마 없음 — fallback 단순 나열")
            ai_signals = _fallback_signals(price_result)

        signal_result = {
            "signals":    ai_signals,
            "volatility": _judge_volatility(price_result),
        }

        # ── 3. 테마 분석 (소외도 수치 계산) ────────────────────
        # theme_analyzer가 ai_signals의 관련종목 등락률로 소외도를 계산
        logger.info("[closing] 순환매 소외도 계산 중...")
        theme_result = theme_analyzer.analyze(signal_result, price_result["by_name"])

        # ── 4. 보고서 조립 ─────────────────────────────────────
        report = {
            "today_str":     today_str,
            "target_str":    target_str,
            "kospi":         price_result.get("kospi",         {}),
            "kosdaq":        price_result.get("kosdaq",        {}),
            "upper_limit":   price_result.get("upper_limit",   []),
            "top_gainers":   price_result.get("top_gainers",   []),
            "top_losers":    price_result.get("top_losers",    []),
            "institutional": price_result.get("institutional", []),
            "short_selling": price_result.get("short_selling", []),
            "theme_map":     theme_result.get("theme_map",     []),
            "volatility":    signal_result["volatility"],
        }

        # ── 5. 텔레그램 발송 ───────────────────────────────────
        logger.info("[closing] 텔레그램 발송 중...")
        message = telegram_bot.format_closing_report(report)
        await telegram_bot.send_async(message)

        logger.info("[closing] 마감봇 완료 ✅")

    except Exception as e:
        logger.error(f"[closing] 마감봇 실패: {e}", exc_info=True)
        try:
            await telegram_bot.send_async(f"⚠️ 마감봇 오류 발생\n{str(e)[:200]}")
        except Exception:
            pass


# ── 내부 헬퍼 ──────────────────────────────────────────────────

def _resolve_target_date(today: datetime) -> datetime | None:
    """
    마감봇 조회 기준일 결정
      - 주말·공휴일           → 전 거래일
      - 평일 16:00 이후       → 오늘 (장 마감 데이터 확정)
      - 평일 16:00 이전(새벽) → 전 거래일

    [수정 이유 v3.1]
    is_market_open()은 "현재 장이 열려있는지(실시간 개폐 여부)"를 반환.
    18:30 실행 시 장이 닫혀있으므로 False → 주말/공휴일 분기로 빠져
    오늘(거래일)임에도 전 거래일을 반환하는 버그 발생.

    [해결책]
    get_prev_trading_day(today).date() < today.date() 이면 오늘이 거래일.
    → is_market_open() 의존 제거, 거래일 여부를 날짜 비교로만 판단.
    """
    prev = get_prev_trading_day(today)

    # 전 거래일 < 오늘 날짜 → 오늘은 거래일
    today_is_trading_day = prev.date() < today.date()

    if not today_is_trading_day:
        return prev  # 주말·공휴일 → 전 거래일

    # 거래일: 장 마감 확정(16:00↑)이면 오늘, 그 이전이면 전 거래일
    return today if today.hour >= 16 else prev


def _fallback_signals(price_result: dict) -> list[dict]:
    """
    AI 실패 시 fallback: 상한가/급등 종목을 각각 하나의 그룹으로 묶음
    theme_analyzer가 소외도를 계산할 수 있도록 관련종목 여러 개 포함
    """
    signals = []
    upper   = price_result.get("upper_limit", [])
    gainers = price_result.get("top_gainers", [])

    if upper:
        signals.append({
            "테마명":   "📌 상한가 종목",
            "발화신호": f"오늘 상한가: {len(upper)}종목",
            "강도":     5,
            "신뢰도":   "pykrx",
            "발화단계": "오늘",
            "상태":     "신규",
            "관련종목": [s["종목명"] for s in upper],
            "ai_memo":  "AI 미설정 — 자동 그룹핑",
        })
    if gainers:
        signals.append({
            "테마명":   "🚀 급등 주도주",
            "발화신호": f"오늘 급등(7%↑): {len(gainers)}종목",
            "강도":     4,
            "신뢰도":   "pykrx",
            "발화단계": "오늘",
            "상태":     "신규",
            "관련종목": [s["종목명"] for s in gainers[:10]],
            "ai_memo":  "AI 미설정 — 자동 그룹핑",
        })
    return signals


def _judge_volatility(price_result: dict) -> str:
    """오늘 실제 지수 등락률 기준 변동성 판단 (v6.0 RULE 4)"""
    kospi_rate  = price_result.get("kospi",  {}).get("change_rate", None)
    kosdaq_rate = price_result.get("kosdaq", {}).get("change_rate", None)
    if kospi_rate is None and kosdaq_rate is None:
        return "판단불가"
    rate = max(abs(kospi_rate or 0), abs(kosdaq_rate or 0))
    if rate >= 2.0:   return "고변동"
    elif rate >= 1.0: return "중변동"
    else:             return "저변동"   # v6.0 RULE 4: 순환매 에너지 없음
