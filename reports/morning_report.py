"""
reports/morning_report.py
아침봇 보고서 조립 전담 (08:30 / 07:30 실행)

[v13.0 전면 재작성 — REDESIGN_v13.md §5]

[실행 흐름]
① data_collector.get_cache() → cache dict 수신
② morning_analyzer.analyze(cache) → 3단계 Gemini 분석
   반환: {"market_env": dict, "candidates": dict, "picks": list}
③ picks 15종목 텔레그램 발송 (신규 포맷)
④ intraday_analyzer.set_watchlist(picks) → 장중봇 감시 등록
⑤ WebSocket 워치리스트 + 섹터맵 저장

[v13.0 변경사항]
- run() 시그니처: cache: dict 단일 인수
- morning_analyzer.analyze(cache) 단일 호출
- v12 키(signals, oracle_result, ai_dart_results 등) 참조 전부 제거
- picks 15종목 전용 텔레그램 메시지 신규 작성

[수정이력]
- v1.0: 기본 구조
- v12.0 Step 6: morning_analyzer 통합
- v13.0: v13 3단계 구조로 전면 재작성 (cache 단일 인수)
"""

from utils.logger import logger
from utils.date_utils import get_today, get_prev_trading_day, fmt_kr
import analyzers.morning_analyzer  as morning_analyzer
import analyzers.intraday_analyzer as intraday_analyzer
import telegram.sender             as telegram_bot
import utils.watchlist_state       as watchlist_state
import config


async def run(cache: dict = None) -> None:
    """
    아침봇 메인 실행 함수 (main.py 가 이것만 호출).

    Args:
        cache: data_collector.get_cache() 반환값 (dict).
               캐시 없거나 비어있으면 내부에서 직접 수집 fallback.
    """
    cache = cache or {}

    today = get_today()
    prev  = get_prev_trading_day(today)
    today_str = fmt_kr(today)
    prev_str  = fmt_kr(prev) if prev else "N/A"

    logger.info(f"[morning] 아침봇 시작 — {today_str} (기준: {prev_str})")

    try:
        # ── ① 캐시 없으면 직접 수집 fallback ────────────────
        if not cache:
            logger.info("[morning] 캐시 없음 — 직접 수집 fallback 시작")
            cache = await _collect_fallback(prev, today)

        price_data = cache.get("price_data")

        # ── ② 시장 환경 조기 결정 ────────────────────────────
        if price_data:
            _early_env = watchlist_state.determine_and_set_market_env(price_data)
            logger.info(f"[morning] 시장 환경 결정: {_early_env or '(미지정)'}")

        # ── ③ morning_analyzer.analyze(cache) — 3단계 Gemini ─
        logger.info("[morning] 3단계 Gemini 분석 시작...")
        morning_result = await morning_analyzer.analyze(cache)

        market_env = morning_result.get("market_env", {})
        candidates = morning_result.get("candidates", {})
        picks      = morning_result.get("picks", [])

        logger.info(
            f"[morning] 분석 완료 — "
            f"환경:{market_env.get('환경','?')} "
            f"후보:{len(candidates.get('후보종목', []))}개 "
            f"픽:{len(picks)}종목"
        )

        # ── ④ 텔레그램 발송 ──────────────────────────────────

        # 4-a. 시장환경 요약 메시지
        env_msg = _format_market_env(market_env, today_str, prev_str, price_data)
        await telegram_bot.send_async(env_msg)

        # 4-b. picks 15종목 발송 (핵심)
        if picks:
            picks_msg = _format_picks(picks, market_env)
            await telegram_bot.send_async(picks_msg)
        else:
            await telegram_bot.send_async(
                f"⚠️ [{today_str}] 아침봇 픽 없음\n"
                f"시장환경: {market_env.get('환경', '불명')}\n"
                f"후보: {len(candidates.get('후보종목', []))}개 → 조건 미달"
            )

        # 4-c. 후보 제외근거 로깅 (디버그)
        excluded = candidates.get("제외근거", "")
        if excluded:
            logger.info(f"[morning] 제외근거: {excluded}")

        # ── ⑤ intraday 픽 워치리스트 등록 ───────────────────
        try:
            if picks:
                intraday_analyzer.set_watchlist(picks)
                logger.info(
                    f"[morning] intraday 픽 워치리스트 등록 — {len(picks)}종목"
                )
            else:
                logger.info("[morning] picks 없음 — intraday 워치리스트 미등록")
        except Exception as e:
            logger.warning(f"[morning] intraday set_watchlist 실패 (비치명적): {e}")

        # ── ⑥ WebSocket 워치리스트 저장 ─────────────────────
        ws_watchlist = _build_ws_watchlist(price_data)
        watchlist_state.set_watchlist(ws_watchlist)
        logger.info(f"[morning] WebSocket 워치리스트 — {len(ws_watchlist)}종목")

        # ── ⑦ 섹터 맵 저장 ──────────────────────────────────
        sector_map = _build_sector_map(price_data)
        watchlist_state.set_sector_map(sector_map)
        logger.info(f"[morning] 섹터 맵 — {len(sector_map)}종목")

        # ── ⑧ 시장 환경 최종 확인 ───────────────────────────
        market_env_state = watchlist_state.get_market_env() or ""
        logger.info(f"[morning] 시장 환경 최종: {market_env_state or '(미지정)'}")

        logger.info("[morning] 아침봇 완료 ✅")

    except Exception as e:
        logger.error(f"[morning] 아침봇 실패: {e}", exc_info=True)
        try:
            await telegram_bot.send_async(f"⚠️ 아침봇 오류\n{str(e)[:200]}")
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════
# 텔레그램 포맷 함수
# ══════════════════════════════════════════════════════════════

def _format_market_env(
    market_env: dict,
    today_str: str,
    prev_str: str,
    price_data: dict | None,
) -> str:
    """시장환경 요약 텔레그램 메시지 생성."""
    환경  = market_env.get("환경", "불명")
    테마  = market_env.get("주도테마후보", [])
    영향  = market_env.get("한국시장영향", "")

    환경_이모지 = {"리스크온": "🟢", "리스크오프": "🔴", "중립": "🟡"}.get(환경, "⚪")

    lines = [
        f"📅 [{today_str}] 아침봇 — 시장환경 분석",
        "",
        f"{환경_이모지} 시장환경: {환경}",
    ]

    if 영향:
        lines.append(f"📌 {영향}")

    if 테마:
        lines.append(f"🎯 주도테마 후보: {' / '.join(테마[:5])}")

    # 전날 지수
    if price_data:
        kospi  = price_data.get("kospi",  {})
        kosdaq = price_data.get("kosdaq", {})
        if kospi.get("change_rate") is not None:
            lines.append(
                f"\n📊 전날({prev_str}) 지수\n"
                f"  KOSPI  {kospi.get('close', 0):,.0f} ({kospi.get('change_rate', 0):+.2f}%)\n"
                f"  KOSDAQ {kosdaq.get('close', 0):,.0f} ({kosdaq.get('change_rate', 0):+.2f}%)"
            )

    return "\n".join(lines)


def _format_picks(picks: list[dict], market_env: dict) -> str:
    """
    최종 픽 15종목 텔레그램 메시지 생성.

    포함 정보: 순위 / 종목명 / 유형 / 근거 / 목표등락률 / 손절기준 / 매수시점
    """
    환경 = market_env.get("환경", "중립")
    환경_이모지 = {"리스크온": "🟢", "리스크오프": "🔴", "중립": "🟡"}.get(환경, "⚪")

    유형_이모지 = {
        "공시":    "📋",
        "테마":    "🎯",
        "순환매":  "🔄",
        "숏스퀴즈": "💥",
    }

    lines = [
        f"🏆 아침봇 최종 픽 {len(picks)}종목 [{환경_이모지} {환경}]",
        "─" * 28,
    ]

    for pick in picks:
        순위    = pick.get("순위", "?")
        종목명  = pick.get("종목명", "")
        종목코드 = pick.get("종목코드", "")
        근거    = pick.get("근거", "")
        목표    = pick.get("목표등락률", "")
        손절    = pick.get("손절기준", "")
        매수    = pick.get("매수시점", "")
        유형    = pick.get("유형", "")
        테마    = pick.get("테마여부", False)

        이모지   = 유형_이모지.get(유형, "📌")
        테마표시 = " 🏷️테마" if 테마 else ""
        코드표시 = f"({종목코드})" if 종목코드 else ""

        lines.append(f"\n{순위}위 {이모지} {종목명}{코드표시}{테마표시}")
        if 근거:
            lines.append(f"   📝 {근거}")
        if 목표 or 손절:
            parts = []
            if 목표:
                parts.append(f"목표 {목표}")
            if 손절:
                parts.append(f"손절 {손절}")
            lines.append(f"   🎯 {' | '.join(parts)}")
        if 매수:
            lines.append(f"   ⏰ {매수}")

    lines.append(f"\n{'─' * 28}")
    lines.append("⚠️ 본 픽은 참고용입니다. 투자 판단은 본인 책임.")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# fallback 수집 (캐시 없을 때)
# ══════════════════════════════════════════════════════════════

async def _collect_fallback(prev, today) -> dict:
    """
    data_collector 캐시가 없을 때 최소한의 데이터를 직접 수집해 cache dict 반환.
    """
    import collectors.filings        as dart_collector
    import collectors.market_global  as market_collector
    import collectors.news_naver     as news_naver
    import collectors.price_domestic as price_collector

    dart_data   = []
    market_data = {}
    naver_data  = {}
    price_data  = None

    try:
        dart_data = dart_collector.collect(prev)
    except Exception as e:
        logger.warning(f"[morning] fallback DART 수집 실패: {e}")

    try:
        market_data = market_collector.collect(prev)
    except Exception as e:
        logger.warning(f"[morning] fallback market 수집 실패: {e}")

    try:
        naver_data = news_naver.collect(today)
    except Exception as e:
        logger.warning(f"[morning] fallback 뉴스 수집 실패: {e}")

    try:
        if prev:
            price_data = price_collector.collect_daily(prev)
    except Exception as e:
        logger.warning(f"[morning] fallback 가격 수집 실패: {e}")

    return {
        "dart_data":                 dart_data,
        "market_data":               market_data,
        "news_naver":                naver_data,
        "news_newsapi":              {},
        "news_global_rss":           [],
        "price_data":                price_data,
        "sector_etf_data":           [],
        "short_data":                [],
        "event_calendar":            [],
        "closing_strength_result":   [],
        "volume_surge_result":       [],
        "fund_concentration_result": [],
        "success_flags":             {},
    }


# ══════════════════════════════════════════════════════════════
# 내부 헬퍼
# ══════════════════════════════════════════════════════════════

def _build_ws_watchlist(price_data: dict | None) -> dict[str, dict]:
    """
    WebSocket 구독용 워치리스트 생성 (상한가 > 급등 > 기관 순 우선순위).
    v13.0: signal 기반 등록 제거 (AI picks가 intraday_analyzer로 별도 전달).
    """
    if not price_data:
        logger.warning("[morning] price_data 없음 — WebSocket 워치리스트 비어있음")
        return {}

    by_name: dict[str, dict] = price_data.get("by_name", {})
    watchlist: dict[str, dict] = {}

    def add(종목명: str, priority: int) -> None:
        info = by_name.get(종목명, {})
        code = info.get("종목코드", "")
        if not code or len(code) != 6:
            return
        if code not in watchlist:
            watchlist[code] = {
                "종목명":     종목명,
                "전일거래량": max(info.get("거래량", 0), 1),
                "우선순위":   priority,
            }

    for s in price_data.get("upper_limit", []):
        add(s["종목명"], 1)
    for s in price_data.get("top_gainers", [])[:20]:
        add(s["종목명"], 2)
    for s in price_data.get("institutional", [])[:10]:
        add(s.get("종목명", ""), 3)

    sorted_items = sorted(watchlist.items(), key=lambda x: x[1]["우선순위"])
    result = dict(sorted_items[:config.WS_WATCHLIST_MAX])

    p = {1: 0, 2: 0, 3: 0}
    for v in result.values():
        p[v["우선순위"]] = p.get(v["우선순위"], 0) + 1
    logger.info(
        f"[morning] WebSocket 워치리스트 — "
        f"상한가:{p[1]} 급등:{p[2]} 기관:{p[3]} 합계:{len(result)}"
    )
    return result


def _build_sector_map(price_data: dict | None) -> dict[str, str]:
    """price_data["by_sector"] → {종목코드: 섹터명} 역방향 맵."""
    if not price_data:
        return {}
    by_sector = price_data.get("by_sector", {})
    if not by_sector:
        return {}
    sector_map: dict[str, str] = {}
    for sector_name, stocks in by_sector.items():
        if not isinstance(stocks, list):
            continue
        for stock in stocks:
            code = stock.get("종목코드", "")
            if code and len(code) == 6:
                sector_map[code] = sector_name
    return sector_map
