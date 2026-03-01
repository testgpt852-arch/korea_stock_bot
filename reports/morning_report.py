"""
reports/morning_report.py
아침봇 보고서 조립 전담 (08:30 / 07:30 실행)

[v12.0 Step 6 — 대폭 단순화]
morning_analyzer.analyze() 하나만 호출하도록 리팩토링.

기존: ai_analyzer, geopolitics_analyzer, theme_analyzer, oracle_analyzer,
      sector_flow_analyzer, event_impact_analyzer 개별 호출
변경: morning_analyzer.analyze() 단일 호출 → 모든 분석 통합 수행

[실행 흐름]
① data_collector / 개별 수집기 → 데이터 수집
② morning_analyzer.analyze()  → 신호1~8 + AI분석 + 테마 + 쪽집게 통합
③ watchlist_state              → 시장환경 결정 + 워치리스트 저장
④ telegram_bot                 → 보고서 조립·발송
⑤ accuracy_tracker             → 예측 기록 (비치명적)

[수정이력]
- v1.0: 기본 구조
- v12.0 Step 6: morning_analyzer 통합 — 개별 analyzer 직접 의존성 제거
"""

from utils.logger import logger
from utils.date_utils import get_today, get_prev_trading_day, fmt_kr
import collectors.filings        as dart_collector
import collectors.market_global  as market_collector
import collectors.news_naver     as news_naver
import collectors.news_newsapi   as news_newsapi
import collectors.price_domestic as price_collector
import analyzers.morning_analyzer  as morning_analyzer   # v12.0: 통합 모듈
import analyzers.intraday_analyzer as intraday_analyzer  # v13.0: set_watchlist 연결
import telegram.sender             as telegram_bot
import utils.watchlist_state       as watchlist_state
import config


async def run(
    geopolitics_raw:           list = None,   # news_global_rss 수집 결과
    event_cache:               list = None,   # event_calendar 결과
    sector_etf_data:           list = None,   # sector_etf 결과
    short_data:                list = None,   # short_interest 결과
    # [v12.0 Step 7] data_collector 사전 수집 결과 (있으면 중복 수집 생략)
    closing_strength_result:   list = None,   # 마감강도 데이터
    volume_surge_result:       list = None,   # 거래량급증 데이터
    fund_concentration_result: list = None,   # 자금집중 데이터
) -> None:
    """아침봇 메인 실행 함수 (AsyncIOScheduler에서 호출)

    [v12.0 Step 7] data_collector 통합:
    - data_collector.get_cache()가 있으면 수집 단계 skip → 분석만 수행
    - 없으면 기존대로 직접 수집 (fallback)
    - closing_strength_result / volume_surge_result / fund_concentration_result:
      data_collector가 06:00에 수집한 마감강도/거래량급증/자금집중 데이터 (있으면 재사용)
    """
    today = get_today()
    prev  = get_prev_trading_day(today)
    today_str = fmt_kr(today)
    prev_str  = fmt_kr(prev) if prev else "N/A"

    logger.info(f"[morning] 아침봇 시작 — {today_str} (기준: {prev_str})")

    try:
        # ── ① 데이터 수집 (data_collector 캐시 우선, fallback 직접 수집) ──
        from collectors.data_collector import get_cache, is_fresh
        _dc = get_cache() if is_fresh(max_age_minutes=180) else {}

        if _dc:
            logger.info("[morning] data_collector 캐시 사용 ✅ (직접 수집 생략)")
            dart_data   = _dc.get("dart_data")   or dart_collector.collect(prev)
            market_data = _dc.get("market_data") or market_collector.collect(prev)
            _naver      = _dc.get("news_naver")  or {}
            _newsapi    = _dc.get("news_newsapi") or {}
        else:
            logger.info("[morning] data_collector 캐시 없음 — 직접 수집 fallback")
            dart_data   = dart_collector.collect(prev)
            market_data = market_collector.collect(prev)
            _naver   = news_naver.collect(today)
            _newsapi = news_newsapi.collect(today)

        news_data = {
            "reports":        _naver.get("reports", [])     + _newsapi.get("reports", []),
            "policy_news":    _naver.get("policy_news", []) + _newsapi.get("policy_news", []),
            "datalab_trends": _naver.get("datalab_trends", []),
        }

        # ── ② 전날 가격 데이터 (캐시 우선) ─────────────────
        price_data = _dc.get("price_data") if _dc else None
        if price_data is None and prev:
            logger.info("[morning] 전날 가격 데이터 수집 중...")
            try:
                price_data = price_collector.collect_daily(prev)
                logger.info(
                    f"[morning] 가격 수집 완료 — "
                    f"상한가:{len(price_data.get('upper_limit',[]))}개 "
                    f"급등:{len(price_data.get('top_gainers',[]))}개 "
                    f"기관/외인:{len(price_data.get('institutional',[]))}종목"
                )
            except Exception as e:
                logger.warning(f"[morning] 가격 수집 실패 ({e}) — 순환매 지도 생략")
        elif price_data:
            logger.info(
                f"[morning] 가격 데이터 캐시 사용 — "
                f"상한가:{len(price_data.get('upper_limit',[]))}개 "
                f"급등:{len(price_data.get('top_gainers',[]))}개"
            )

        # ── ③ 시장 환경 조기 결정 (종목픽 호출 전 선행) ────────
        if price_data:
            _early_env = watchlist_state.determine_and_set_market_env(price_data)
            logger.info(f"[morning] 시장 환경 결정: {_early_env or '(미지정)'}")

        # ── ④ morning_analyzer.analyze() — 통합 분석 ──────────
        # [v12.0] 모든 분석 로직을 morning_analyzer에 위임
        # geopolitics_analyzer, theme_analyzer, oracle_analyzer,
        # sector_flow_analyzer, event_impact_analyzer 직접 호출 제거
        logger.info("[morning] 통합 분석 중 (morning_analyzer)...")

        # 기업 이벤트 캐시 처리
        _event_input: list | None = None
        if config.EVENT_CALENDAR_ENABLED:
            if event_cache:
                logger.info(f"[morning] 이벤트 캐시 사용 — {len(event_cache)}건")
                _event_input = event_cache
            else:
                try:
                    import collectors.event_calendar as ev_cal
                    _event_input = ev_cal.collect(today)
                    logger.info(f"[morning] 이벤트 캘린더 수집 — {len(_event_input)}건")
                except Exception as e:
                    logger.warning(f"[morning] 이벤트 수집 실패: {e}")

        morning_result = await morning_analyzer.analyze(
            dart_data                  = dart_data,
            market_data                = market_data,
            news_data                  = news_data,
            price_data                 = price_data,
            geopolitics_raw            = geopolitics_raw,
            event_calendar             = _event_input,
            sector_etf_data            = sector_etf_data,
            short_data                 = short_data,
            # [v12.0 Step 7] data_collector 사전 수집 마감강도/거래량급증/자금집중
            closing_strength_result    = closing_strength_result,
            volume_surge_result        = volume_surge_result,
            fund_concentration_result  = fund_concentration_result,
            # [v12.0 Step 8] data_collector._build_signals() 결과 전달 (signal_analyzer 흡수)
            prebuilt_signals           = _dc.get("signals")         if _dc else None,
            prebuilt_market_summary    = _dc.get("market_summary")  if _dc else None,
            prebuilt_commodities       = _dc.get("commodities")     if _dc else None,
            prebuilt_volatility        = _dc.get("volatility")      if _dc else None,
            prebuilt_report_picks      = _dc.get("report_picks")    if _dc else None,
            prebuilt_policy_summary    = _dc.get("policy_summary")  if _dc else None,
            prebuilt_sector_scores     = _dc.get("sector_scores")   if _dc else None,
            prebuilt_event_scores      = _dc.get("event_scores")    if _dc else None,
        )

        # 결과 추출
        signal_result = {
            "signals":        morning_result.get("signals",        []),
            "market_summary": morning_result.get("market_summary", {}),
            "commodities":    morning_result.get("commodities",    {}),
            "volatility":     morning_result.get("volatility",     ""),
            "report_picks":   morning_result.get("report_picks",   []),
            "policy_summary": morning_result.get("policy_summary", []),
            "sector_scores":  morning_result.get("sector_scores",  {}),
            "event_scores":   morning_result.get("event_scores",   {}),
        }
        ai_dart_results  = morning_result.get("ai_dart_results",      [])
        theme_result     = morning_result.get("theme_result",         {"theme_map": []})
        oracle_result    = morning_result.get("oracle_result",        None)
        geopolitics_data = morning_result.get("geopolitics_analyzed", [])

        # [v13.0] picks 추출 → intraday_analyzer.set_watchlist() 연결
        # morning_analyzer._pick_final() 반환값 picks 리스트 (최대 15종목).
        # morning_analyzer 개편 전이면 빈 리스트.
        _picks_for_intraday: list[dict] = morning_result.get("picks", [])

        logger.info(
            f"[morning] 통합 분석 완료 — "
            f"신호:{len(signal_result['signals'])}개 "
            f"공시AI:{len(ai_dart_results)}건 "
            f"테마:{len(theme_result.get('theme_map',[]))}개 "
            f"쪽집게:{len(oracle_result.get('picks',[]) if oracle_result else [])}개"
        )

        # ── ⑤ 보고서 조립 ─────────────────────────────────────
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
            "prev_kospi":         price_data.get("kospi",  {}) if price_data else {},
            "prev_kosdaq":        price_data.get("kosdaq", {}) if price_data else {},
            "prev_institutional": price_data.get("institutional", []) if price_data else [],
            "oracle":             oracle_result,
        }

        # ── ⑥ 텔레그램 발송 (쪽집게 → 핵심 요약 → 상세) ──────
        logger.info("[morning] 텔레그램 발송 중...")

        if oracle_result and oracle_result.get("has_data"):
            oracle_msg = telegram_bot.format_pick_stocks_section(oracle_result)
            if oracle_msg:
                await telegram_bot.send_async(oracle_msg)

        summary_msg = telegram_bot.format_morning_summary(report)
        await telegram_bot.send_async(summary_msg)

        if config.FULL_REPORT_FORMAT:
            message = telegram_bot.format_morning_report_full(
                report, geopolitics_data=geopolitics_data
            )
        else:
            message = telegram_bot.format_morning_report(
                report, geopolitics_data=geopolitics_data
            )
        await telegram_bot.send_async(message)

        # ── ⑦ [v13.0] intraday_analyzer 픽 워치리스트 등록 ──────
        # morning_analyzer._pick_final() 결과 picks 15종목을 장중봇 감시 대상으로 등록.
        # 발송 직후 호출 — realtime_alert 가 09:00에 poll_all_markets() 시작 전에 완료돼야 함.
        try:
            if _picks_for_intraday:
                intraday_analyzer.set_watchlist(_picks_for_intraday)
                logger.info(
                    f"[morning] intraday 픽 워치리스트 등록 — {len(_picks_for_intraday)}종목"
                )
            else:
                logger.info("[morning] picks 없음 — intraday 워치리스트 미등록")
        except Exception as _intra_e:
            logger.warning(f"[morning] intraday set_watchlist 실패 (비치명적): {_intra_e}")

        # ── ⑧ 예측 기록 ──────────────────────────────────────
        try:
            from tracking import accuracy_tracker
            accuracy_tracker.record_prediction(
                date_str       = today_str,
                oracle_result  = oracle_result,
                signal_sources = signal_result.get("signals", []),
            )
        except Exception as _acc_e:
            logger.warning(f"[morning] 예측 기록 실패 (비치명적): {_acc_e}")

        # ── ⑨ WebSocket 워치리스트 저장 ──────────────────────
        ws_watchlist = _build_ws_watchlist(price_data, signal_result)
        watchlist_state.set_watchlist(ws_watchlist)
        logger.info(f"[morning] 워치리스트 저장 — {len(ws_watchlist)}종목")

        # ── ⑩ 시장 환경 최종 확인 ────────────────────────────
        market_env = watchlist_state.get_market_env() or ""
        logger.info(f"[morning] 시장 환경 최종: {market_env or '(미지정)'}")

        # ── ⑪ 섹터 맵 저장 ────────────────────────────────────
        sector_map = _build_sector_map(price_data)
        watchlist_state.set_sector_map(sector_map)
        logger.info(f"[morning] 섹터 맵 저장 — {len(sector_map)}종목")

        logger.info("[morning] 아침봇 완료 ✅")

    except Exception as e:
        logger.error(f"[morning] 아침봇 실패: {e}", exc_info=True)
        try:
            await telegram_bot.send_async(f"⚠️ 아침봇 오류\n{str(e)[:200]}")
        except Exception:
            pass


# ── 내부 헬퍼 ─────────────────────────────────────────────────

def _build_ws_watchlist(
    price_data:    dict | None,
    signal_result: dict,
) -> dict[str, dict]:
    """WebSocket 구독용 워치리스트 생성 (우선순위: 상한가>급등>기관>신호)."""
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
    for signal in signal_result.get("signals", []):
        for 종목명 in signal.get("관련종목", [])[:3]:
            add(종목명, 4)

    sorted_items = sorted(watchlist.items(), key=lambda x: x[1]["우선순위"])
    result = dict(sorted_items[:config.WS_WATCHLIST_MAX])

    p = {1: 0, 2: 0, 3: 0, 4: 0}
    for v in result.values():
        p[v["우선순위"]] = p.get(v["우선순위"], 0) + 1
    logger.info(
        f"[morning] 워치리스트 — "
        f"상한가:{p[1]} 급등:{p[2]} 기관:{p[3]} 신호:{p[4]} 합계:{len(result)}"
    )
    return result


def _build_sector_map(price_data: dict | None) -> dict[str, str]:
    """price_data[\"by_sector\"] → {종목코드: 섹터명} 역방향 맵."""
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
