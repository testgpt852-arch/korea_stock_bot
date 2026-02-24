"""
reports/morning_report.py
아침봇 보고서 조립 전담 (08:30 / 07:40 실행)

[실행 흐름 — ARCHITECTURE.md 준수]
① dart_collector    → 전날 공시 수집
② market_collector  → 미국증시·원자재·섹터 ETF
③ news_collector    → 리포트·정책뉴스
④ price_collector   → 전날 가격 데이터 (상한가·급등 — 순환매 지도용) ← v2.1 추가
⑤ signal_analyzer   → 신호 1~5 통합 판단 (신호4 price_data 활용)    ← v2.1 변경
⑥ ai_analyzer       → 주요 공시 호재/악재 점수화 (GOOGLE_AI_API_KEY 있을 때만)
⑦ theme_analyzer    → 순환매 지도 (price_data로 소외도 계산)          ← v2.1 변경
⑧ 보고서 조립 → 텔레그램 발송

[수정이력]
- v1.0: 기본 구조
- v1.3: ai_analyzer.analyze_dart() 호출 추가
- v2.1: price_collector.collect_daily() 직접 호출 추가
        → 마감봇(closing_report) 의존 완전 제거
        → 순환매 지도가 아침봇 단독으로 작동
        signal_analyzer에 price_data 전달
        theme_analyzer에 price_data["by_name"] 전달
"""

from utils.logger import logger
from utils.date_utils import get_today, get_prev_trading_day, fmt_kr
import collectors.dart_collector   as dart_collector
import collectors.market_collector as market_collector
import collectors.news_collector   as news_collector
import collectors.price_collector  as price_collector   # v2.1 추가
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
        # ── ① 데이터 수집 ─────────────────────────────────────
        logger.info("[morning] 데이터 수집 중...")
        dart_data   = dart_collector.collect(prev)
        market_data = market_collector.collect(prev)
        news_data   = news_collector.collect(today)

        # ── ② 전날 가격 데이터 수집 (v2.1 추가) ───────────────
        # 마감봇 의존 없이 직접 pykrx로 전날 상한가·급등 수집
        # → signal_analyzer 신호4(순환매) + theme_analyzer 소외도 계산에 사용
        price_data = None
        if prev:
            logger.info("[morning] 전날 가격 데이터 수집 중 (순환매 지도용)...")
            try:
                price_data = price_collector.collect_daily(prev)
                logger.info(
                    f"[morning] 가격 수집 완료 — "
                    f"상한가:{len(price_data.get('upper_limit', []))}개 "
                    f"급등:{len(price_data.get('top_gainers', []))}개"
                )
            except Exception as e:
                logger.warning(f"[morning] 가격 수집 실패 ({e}) — 순환매 지도 생략")
                price_data = None

        # ── ③ 신호 분석 (v2.1: price_data 전달) ───────────────
        logger.info("[morning] 신호 분석 중...")
        signal_result = signal_analyzer.analyze(
            dart_data, market_data, news_data, price_data
        )

        # ── ④ AI 공시 분석 ────────────────────────────────────
        ai_dart_results = []
        if dart_data:
            logger.info("[morning] AI 공시 분석 중...")
            ai_dart_results = ai_analyzer.analyze_dart(dart_data)
            if ai_dart_results:
                logger.info(f"[morning] AI 공시 분석 완료 — {len(ai_dart_results)}건")
                _enrich_signals_with_ai(signal_result["signals"], ai_dart_results)

        # ── ⑤ 테마 분석 (v2.1: price_data["by_name"] 전달) ───
        # theme_analyzer가 price_data로 소외도를 실제 수치로 계산
        logger.info("[morning] 테마 분석 중...")
        price_by_name = price_data.get("by_name", {}) if price_data else {}
        theme_result = theme_analyzer.analyze(signal_result, price_by_name)

        # ── ⑥ 보고서 조립 ─────────────────────────────────────
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
            "ai_dart_results": ai_dart_results,
            # v2.1: 전날 지수 정보 추가 (telegram_bot 포맷용)
            "prev_kospi":      price_data.get("kospi",  {}) if price_data else {},
            "prev_kosdaq":     price_data.get("kosdaq", {}) if price_data else {},
        }

        # ── ⑦ 텔레그램 발송 ──────────────────────────────────
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
    """AI 공시 분석 결과를 신호에 반영 (강도 조정) — in-place"""
    ai_map = {r["종목명"]: r for r in ai_results}

    for signal in signals:
        관련종목 = signal.get("관련종목", [])
        if not 관련종목:
            continue
        종목명 = 관련종목[0]
        if 종목명 in ai_map:
            ai = ai_map[종목명]
            if ai["점수"] >= 8:
                signal["강도"] = min(5, signal.get("강도", 3) + 1)
                signal["ai_메모"] = f"AI: {ai['이유']} ({ai['상한가확률']})"
