"""
reports/morning_report.py
아침봇 보고서 조립 전담 (08:30 실행)

[실행 흐름 — ARCHITECTURE.md 준수]
① dart_collector   → 전날 공시 수집
② market_collector → 미국증시·원자재
③ news_collector   → 리포트·정책뉴스
④ signal_analyzer  → 신호 1~5 통합 판단
⑤ ai_analyzer      → 주요 공시 호재/악재 점수화 (GOOGLE_AI_API_KEY 있을 때만)
⑥ theme_analyzer   → 순환매 지도
⑦ 보고서 조립 → 텔레그램 발송

[수정이력]
- v1.0: 기본 구조
- v1.3: ai_analyzer.analyze_dart() 호출 추가 (아키텍처 명세 준수)
"""

from utils.logger import logger
from utils.date_utils import get_today, get_prev_trading_day, fmt_kr
import collectors.dart_collector   as dart_collector
import collectors.market_collector as market_collector
import collectors.news_collector   as news_collector
import analyzers.signal_analyzer   as signal_analyzer
import analyzers.theme_analyzer    as theme_analyzer
import analyzers.ai_analyzer       as ai_analyzer
import notifiers.telegram_bot      as telegram_bot


async def run() -> None:
    """아침봇 메인 실행 함수 (AsyncIOScheduler에서 호출)"""
    today = get_today()
    prev  = get_prev_trading_day(today)

    today_str = fmt_kr(today)
    prev_str  = fmt_kr(prev) if prev else "N/A"

    logger.info(f"[morning] 아침봇 시작 — {today_str} (기준: {prev_str})")

    try:
        # ── ① 데이터 수집 ────────────────────────────────────
        logger.info("[morning] 데이터 수집 중...")
        dart_data   = dart_collector.collect(prev)
        market_data = market_collector.collect(prev)
        news_data   = news_collector.collect(today)

        # ── ② 신호 분석 ──────────────────────────────────────
        logger.info("[morning] 신호 분석 중...")
        signal_result = signal_analyzer.analyze(dart_data, market_data, news_data)

        # ── ③ AI 공시 분석 (GOOGLE_AI_API_KEY 있을 때만 실행) ─
        ai_dart_results = []
        if dart_data:
            logger.info("[morning] AI 공시 분석 중...")
            ai_dart_results = ai_analyzer.analyze_dart(dart_data)
            if ai_dart_results:
                logger.info(f"[morning] AI 공시 분석 완료 — {len(ai_dart_results)}건")
                # 강도 높은 신호에 AI 점수 반영
                _enrich_signals_with_ai(signal_result["signals"], ai_dart_results)

        # ── ④ 테마 분석 ──────────────────────────────────────
        logger.info("[morning] 테마 분석 중...")
        theme_result = theme_analyzer.analyze(signal_result)

        # ── ⑤ 보고서 조립 ────────────────────────────────────
        report = {
            "today_str":       today_str,
            "prev_str":        prev_str,
            "signals":         signal_result["signals"],
            "market_summary":  signal_result["market_summary"],
            "commodities":     signal_result["commodities"],
            "volatility":      signal_result["volatility"],
            "report_picks":    signal_result["report_picks"],
            "policy_summary":  signal_result["policy_summary"],
            "theme_map":       theme_result["theme_map"],
            "ai_dart_results": ai_dart_results,    # AI 공시 분석 추가
        }

        # ── ⑥ 텔레그램 발송 ──────────────────────────────────
        logger.info("[morning] 텔레그램 발송 중...")
        message = telegram_bot.format_morning_report(report)
        await telegram_bot.send_async(message)

        logger.info("[morning] 아침봇 완료 ✅")

    except Exception as e:
        logger.error(f"[morning] 아침봇 실패: {e}", exc_info=True)
        try:
            await telegram_bot.send_async(f"⚠️ 아침봇 오류\n{str(e)[:200]}")
        except Exception:
            pass


def _enrich_signals_with_ai(signals: list[dict], ai_results: list[dict]) -> None:
    """
    AI 공시 분석 결과를 신호에 반영 (강도 조정)
    신호 리스트를 직접 수정 (in-place)
    """
    ai_map = {r["종목명"]: r for r in ai_results}

    for signal in signals:
        관련종목 = signal.get("관련종목", [])
        if not 관련종목:
            continue
        종목명 = 관련종목[0]
        if 종목명 in ai_map:
            ai = ai_map[종목명]
            # AI 점수 8 이상이면 강도 +1 (최대 5)
            if ai["점수"] >= 8:
                signal["강도"] = min(5, signal.get("강도", 3) + 1)
                signal["ai_메모"] = f"AI: {ai['이유']} ({ai['상한가확률']})"
