"""
reports/morning_report.py
아침봇 보고서 조립 전담 (08:30 / 07:40 실행)

[실행 흐름 — ARCHITECTURE.md 준수]
① dart_collector    → 전날 공시 수집
② market_collector  → 미국증시·원자재·섹터 ETF
③ news_collector    → 리포트·정책뉴스
④ price_collector   → 전날 가격 데이터 (상한가·급등·기관/외인) ← v2.1 추가
⑤ signal_analyzer   → 신호 1~5 통합 판단 (신호4 price_data 활용)    ← v2.1 변경
⑥ ai_analyzer.analyze_dart()  → 주요 공시 호재/악재 점수화
⑦ ai_analyzer.analyze_closing() → 신호4 제네릭 라벨 교체             ← v2.4 추가
   ("상한가 순환매"/"KOSPI 급등 순환매" → "바이오신약", "방산" 등 실제 테마명)
⑧ theme_analyzer    → 순환매 지도 (price_data로 소외도 계산)
⑨ watchlist_state   → WebSocket 워치리스트 저장 (장중봇용)            ← v3.1 추가
⑩ 보고서 조립 → 텔레그램 발송

[수정이력]
- v1.0: 기본 구조
- v1.3: ai_analyzer.analyze_dart() 호출 추가
- v2.1: price_collector.collect_daily() 직접 호출 추가
        → 마감봇(closing_report) 의존 완전 제거
        → 순환매 지도가 아침봇 단독으로 작동
        signal_analyzer에 price_data 전달
        theme_analyzer에 price_data["by_name"] 전달
- v2.2: 전날 기관/외인 순매수 데이터 보고서에 추가
        (price_data["institutional"] → report["prev_institutional"])
- v2.4: ai_analyzer.analyze_closing(price_data) 추가
        신호4 "상한가 순환매" 제네릭 라벨을 AI 실제 테마명으로 교체
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
import utils.watchlist_state        as watchlist_state   # v3.1 추가


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
        # 마감봇 의존 없이 직접 pykrx로 전날 상한가·급등·기관/외인 수집
        # → signal_analyzer 신호4(순환매) + theme_analyzer 소외도 계산에 사용
        # → 아침봇 기관/외인 섹션에도 활용 (v2.2 추가)
        price_data = None
        if prev:
            logger.info("[morning] 전날 가격 데이터 수집 중 (순환매 지도 + 기관/외인용)...")
            try:
                price_data = price_collector.collect_daily(prev)
                logger.info(
                    f"[morning] 가격 수집 완료 — "
                    f"상한가:{len(price_data.get('upper_limit', []))}개 "
                    f"급등:{len(price_data.get('top_gainers', []))}개 "
                    f"기관/외인:{len(price_data.get('institutional', []))}종목"
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

        # ── ④-b AI 순환매 테마 그룹핑 (v2.4) ────────────────
        # signal4 "상한가 순환매"/"KOSPI 급등 순환매" 제네릭 라벨을
        # ai_analyzer.analyze_closing()으로 실제 테마명(바이오신약, 방산 등)으로 교체
        # ARCHITECTURE 규칙: T5/T6/T3 분석기 호출 금지지만
        # ai_analyzer.analyze_closing()은 허용 (ai_analyzer → morning_report 의존성 기존 존재)
        if price_data:
            try:
                logger.info("[morning] AI 순환매 테마 그룹핑 중 (신호4 교체)...")
                ai_closing_signals = ai_analyzer.analyze_closing(price_data)
                if ai_closing_signals:
                    # 신호4(순환매) 엔트리만 교체 — 신호1/2/3/5는 유지
                    non_signal4 = [
                        s for s in signal_result["signals"]
                        if "신호4" not in s.get("발화신호", "")
                    ]
                    signal_result["signals"] = non_signal4 + ai_closing_signals
                    signal_result["signals"].sort(key=lambda x: x["강도"], reverse=True)
                    logger.info(
                        f"[morning] 신호4 AI 교체 완료 — {len(ai_closing_signals)}개 테마"
                    )
                else:
                    logger.info("[morning] AI 테마 결과 없음 (저변동/AI 미설정) — 기존 신호4 유지")
            except Exception as e:
                logger.warning(f"[morning] AI 테마 그룹핑 실패 ({e}) — 기존 신호4 유지")

        # ── ⑤ 테마 분석 (v2.1: price_data["by_name"] 전달) ───
        # theme_analyzer가 price_data로 소외도를 실제 수치로 계산
        logger.info("[morning] 테마 분석 중...")
        price_by_name = price_data.get("by_name", {}) if price_data else {}
        theme_result = theme_analyzer.analyze(signal_result, price_by_name)

        # ── ⑥ 보고서 조립 ─────────────────────────────────────
        report = {
            "today_str":          today_str,
            "prev_str":           prev_str,
            "signals":            signal_result["signals"],
            "market_summary":     signal_result["market_summary"],
            "commodities":        signal_result["commodities"],
            "volatility":         signal_result["volatility"],
            "report_picks":       signal_result["report_picks"],
            "policy_summary":     signal_result["policy_summary"],
            "theme_map":          theme_result["theme_map"],
            "ai_dart_results":    ai_dart_results,
            # v2.1: 전날 지수 정보 추가 (telegram_bot 포맷용)
            "prev_kospi":         price_data.get("kospi",  {}) if price_data else {},
            "prev_kosdaq":        price_data.get("kosdaq", {}) if price_data else {},
            # v2.2: 전날 기관/외인 순매수 추가
            "prev_institutional": price_data.get("institutional", []) if price_data else [],
        }

        # ── ⑦ 텔레그램 발송 ──────────────────────────────────
        logger.info("[morning] 텔레그램 발송 중...")
        message = telegram_bot.format_morning_report(report)
        await telegram_bot.send_async(message)

        # ── ⑧ WebSocket 워치리스트 저장 (v3.1) ──────────────
        # 장중봇(09:00)이 시작될 때 이 목록으로 WebSocket 구독
        # price_data 없으면 워치리스트 빈 상태 유지 (장중봇은 REST 폴링만 사용)
        ws_watchlist = _build_ws_watchlist(price_data, signal_result)
        watchlist_state.set_watchlist(ws_watchlist)
        logger.info(
            f"[morning] WebSocket 워치리스트 저장 — {len(ws_watchlist)}종목 "
            f"(장중봇이 09:00에 구독 예정)"
        )

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


def _build_ws_watchlist(
    price_data: dict | None,
    signal_result: dict,
) -> dict[str, dict]:
    """
    WebSocket 구독용 워치리스트 생성 (v3.1)

    [우선순위별 소스]
    1. 전날 상한가 (upper_limit)       — 오늘 순환매 가장 유력
    2. 전날 급등 상위 20 (top_gainers) — 모멘텀 지속 후보
    3. 기관 순매수 상위 10 (institutional) — 스마트머니 추적
    4. 신호 관련종목 각 3개 (signal_result) — 공시·섹터·리포트 종목

    [중복 제거]
    종목코드 기준. 먼저 등장한 우선순위가 유지됨.
    config.WS_WATCHLIST_MAX(50)개 초과 시 우선순위 낮은 것부터 제거.

    반환: {종목코드: {"종목명": str, "전일거래량": int, "우선순위": int}}
    """
    import config as _config

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
        if code not in watchlist:   # 먼저 등록된 우선순위 유지
            watchlist[code] = {
                "종목명":     종목명,
                "전일거래량": max(info.get("거래량", 0), 1),  # 0 나누기 방지
                "우선순위":   priority,
            }

    # ① 전날 상한가 (전체)
    for s in price_data.get("upper_limit", []):
        add(s["종목명"], 1)

    # ② 전날 급등 상위 20 (7%↑, 상한가 제외)
    for s in price_data.get("top_gainers", [])[:20]:
        add(s["종목명"], 2)

    # ③ 기관 순매수 상위 10
    for s in price_data.get("institutional", [])[:10]:
        add(s.get("종목명", ""), 3)

    # ④ 신호 관련종목 (각 신호의 대장+소외 상위 3개)
    for signal in signal_result.get("signals", []):
        for 종목명 in signal.get("관련종목", [])[:3]:
            add(종목명, 4)

    # 우선순위 정렬 → 상위 WS_WATCHLIST_MAX개만
    sorted_items = sorted(watchlist.items(), key=lambda x: x[1]["우선순위"])
    result = dict(sorted_items[:_config.WS_WATCHLIST_MAX])

    p_counts = {1: 0, 2: 0, 3: 0, 4: 0}
    for v in result.values():
        p_counts[v["우선순위"]] = p_counts.get(v["우선순위"], 0) + 1
    logger.info(
        f"[morning] 워치리스트 구성 — "
        f"상한가:{p_counts[1]} 급등:{p_counts[2]} "
        f"기관:{p_counts[3]} 신호:{p_counts[4]} "
        f"합계:{len(result)}/{_config.WS_WATCHLIST_MAX}"
    )
    return result