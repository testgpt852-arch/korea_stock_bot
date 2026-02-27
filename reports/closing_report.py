"""
reports/closing_report.py
마감봇 보고서 조립 전담 (18:30 실행)

[ARCHITECTURE 의존성]
closing_report → price_collector, theme_analyzer, ai_analyzer, telegram_bot
closing_report → closing_strength, volume_flat, fund_inflow_analyzer  ← v3.2 T5/T6/T3
closing_report → oracle_analyzer                                       ← v8.1 쪽집게봇
closing_report → sector_etf_collector, short_interest_collector,      ← v10.0 Phase 3
                 sector_flow_analyzer, theme_history
수정 시 이 파일만 수정 (나머지 건드리지 않음)

[실행 흐름]
① price_collector  → 전종목 등락률 + 기관/공매도
② ai_analyzer      → 테마 그룹핑 + 소외주 식별 (v6.0 #5·#6 자동화)
③ theme_analyzer   → 소외도 수치 계산 (ai_analyzer 결과 활용)
④ closing_strength → T5 마감 강도 상위 종목 (v3.2)
   volume_flat     → T6 횡보 거래량 급증 종목 (v3.2)
   fund_inflow_analyzer → T3 시총 대비 자금유입 종목 (v3.2)
   _update_watchlist_from_closing() → 내일 WebSocket 워치리스트 보강 (v3.2)
④-b oracle_analyzer → 테마·수급·공시·T5/T6/T3 종합 → 내일 픽 + 진입조건  ← v8.1 신규
④-c sector_etf_collector + short_interest_collector → sector_flow_analyzer  ← v10.0 Phase 3
    → 신호7 (섹터수급) → oracle_analyzer 점수 반영 + theme_history 기록
⑤ telegram_bot     → 쪽집게 섹션 선발송 → 마감 리포트 후발송              ← v8.1 변경

[수정이력]
- v8.1: oracle_analyzer 통합
        쪽집게 섹션(format_oracle_section) 선발송 추가
        T5/T6/T3 결과를 oracle_analyzer에 전달 (rule #16 준수 — 마감봇에서만)
- v10.0 Phase 3: sector_etf_collector + short_interest_collector 통합
        sector_flow_analyzer 신호7 → signal_result 주입
        theme_history.record_closing() 마감봇 완료 후 자동 기록 (rule #95)
"""

from datetime import datetime
from utils.logger import logger
from utils.date_utils import get_today, get_prev_trading_day, fmt_kr, is_market_open
import collectors.price_collector as price_collector
import analyzers.theme_analyzer        as theme_analyzer
import analyzers.ai_analyzer           as ai_analyzer
import analyzers.closing_strength      as closing_strength    # v3.2: T5 마감 강도
import analyzers.volume_flat           as volume_flat         # v3.2: T6 횡보 거래량
import analyzers.fund_inflow_analyzer  as fund_inflow_analyzer  # v3.2: T3 시총 자금유입
import analyzers.oracle_analyzer       as oracle_analyzer     # v8.1: 쪽집게봇
import analyzers.sector_flow_analyzer  as sector_flow_analyzer  # v10.0 Phase 3: 신호7
import utils.watchlist_state           as watchlist_state     # v3.2: 마감봇→장중봇 워치리스트
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

        # ── 4. T5/T6/T3 트리거 분석 (v3.2) ──────────────────────
        target_ymd = target.strftime("%Y%m%d")

        logger.info("[closing] T5 마감 강도 분석 중...")
        try:
            cs_result = closing_strength.analyze(target_ymd)
        except Exception as _e:
            logger.warning(f"[closing] T5 분석 실패: {_e}")
            cs_result = []

        logger.info("[closing] T6 횡보 거래량 급증 분석 중...")
        try:
            vf_result = volume_flat.analyze(target_ymd)
        except Exception as _e:
            logger.warning(f"[closing] T6 분석 실패: {_e}")
            vf_result = []

        logger.info("[closing] T3 시총 대비 자금 유입 분석 중...")
        try:
            fi_result = fund_inflow_analyzer.analyze(target_ymd)
        except Exception as _e:
            logger.warning(f"[closing] T3 분석 실패: {_e}")
            fi_result = []

        # 내일 WebSocket 워치리스트에 T5/T6 종목 추가
        _update_watchlist_from_closing(cs_result, vf_result, price_result)

        # ── 4-c. 섹터 ETF + 공매도 잔고 분석 (v10.0 Phase 3) ───
        # rule #92: sector_etf_collector는 마감봇(18:30) 전용 — 장중 호출 금지
        # rule #95: theme_history는 저장 전용 — 분석 없음
        logger.info("[closing] 섹터 ETF 자금흐름 + 공매도 잔고 분석 중 (신호7)...")
        sector_flow_result = {}
        try:
            import collectors.sector_etf_collector    as sector_etf_collector
            import collectors.short_interest_collector as short_interest_collector

            etf_data   = sector_etf_collector.collect(target)
            short_data = short_interest_collector.collect(target)

            sector_flow_result = sector_flow_analyzer.analyze(
                etf_data=etf_data,
                short_data=short_data,
                price_by_sector=price_result.get("by_sector", {}),
            )
            sf_signals = sector_flow_result.get("signals", [])
            # 신호7 → ai_signals에 추가 (rule #94 계열: signal_analyzer 경유)
            ai_signals = ai_signals + sf_signals
            logger.info(
                f"[closing] 신호7 추가: {len(sf_signals)}개 "
                f"(상위섹터: {sector_flow_result.get('top_sectors', [])})"
            )
        except Exception as _e:
            logger.warning(f"[closing] 섹터ETF/공매도 분석 실패 (비치명적): {_e}")
            sector_flow_result = {}

        # ── 4-b. 쪽집게 분석 (v8.1 신규) ─────────────────────
        # T5/T6/T3 + 신호7 결과까지 종합 → 내일 주도 테마 + 종목 픽 + 진입조건
        # rule #16 준수: closing_report에서만 T5/T6/T3 전달 가능
        logger.info("[closing] 쪽집게 분석 중 (내일 픽 + 진입조건)...")
        market_env_val = watchlist_state.get_market_env() or ""
        try:
            oracle_result = oracle_analyzer.analyze(
                theme_map=theme_result.get("theme_map", []),
                price_by_name=price_result.get("by_name", {}),
                institutional=price_result.get("institutional", []),
                ai_dart_results=[],          # 마감봇은 공시 분석 없음
                signals=ai_signals,
                market_env=market_env_val,
                closing_strength=cs_result,  # T5 — 마감봇 전용
                volume_flat=vf_result,       # T6 — 마감봇 전용
                fund_inflow=fi_result,       # T3 — 마감봇 전용
                sector_scores=sector_flow_result.get("sector_scores"),  # Phase 3 신호7
            )
        except Exception as _e:
            logger.warning(f"[closing] 쪽집게 분석 실패 (비치명적): {_e}")
            oracle_result = None

        # ── 5. 보고서 조립 ─────────────────────────────────────
        report = {
            "today_str":        today_str,
            "target_str":       target_str,
            "kospi":            price_result.get("kospi",         {}),
            "kosdaq":           price_result.get("kosdaq",        {}),
            "upper_limit":      price_result.get("upper_limit",   []),
            "top_gainers":      price_result.get("top_gainers",   []),
            "top_losers":       price_result.get("top_losers",    []),
            "institutional":    price_result.get("institutional", []),
            "short_selling":    price_result.get("short_selling", []),
            "theme_map":        theme_result.get("theme_map",     []),
            "volatility":       signal_result["volatility"],
            # v3.2: T5/T6/T3 트리거 결과
            "closing_strength": cs_result,
            "volume_flat":      vf_result,
            "fund_inflow":      fi_result,
            # v8.1: 쪽집게 분석 결과
            "oracle":           oracle_result,
        }

        # ── 5-a. theme_history 기록 (v10.0 Phase 3) ────────────
        # rule #95: 마감봇 완료 후 이벤트→급등 섹터 이력 저장 (분석 없음)
        try:
            from tracking import theme_history
            theme_history.record_closing(
                date_str=target_str,
                top_gainers=price_result.get("top_gainers", []),
                signals=ai_signals,
                oracle_result=oracle_result,
                geo_events=None,   # 마감봇에서 geopolitics_data 없으면 None
            )
        except Exception as _e:
            logger.warning(f"[closing] theme_history 기록 실패 (비치명적): {_e}")

        # ── 5-b. 실제 급등 기록 + 예측 정확도 업데이트 (v10.6 Phase 4-2) ──
        # rule #100: accuracy_tracker는 저장·계산만 담당 — 발송 금지
        accuracy_stats = {}
        try:
            from tracking import accuracy_tracker
            accuracy_tracker.record_actual(
                date_str=target_str,
                actual_top_gainers=price_result.get("top_gainers", []),
                actual_upper_limit=price_result.get("upper_limit", []),
            )
            accuracy_stats = accuracy_tracker.get_accuracy_stats(last_n=14)
            logger.info(
                f"[closing] 정확도 업데이트 완료 — "
                f"{target_str}: {accuracy_stats.get('avg_accuracy', 0):.1%} "
                f"({accuracy_stats.get('sample_count', 0)}일 누적)"
            )
        except Exception as _acc_e:
            logger.warning(f"[closing] 정확도 업데이트 실패 (비치명적): {_acc_e}")

        # accuracy_stats를 report에 추가 (format_closing_report_full에서 활용)
        report["accuracy_stats"] = accuracy_stats

        # ── 6. 텔레그램 발송 (v8.1: 쪽집게 섹션 선발송) ──────────
        logger.info("[closing] 텔레그램 발송 중...")

        # [v8.1] 쪽집게 픽 섹션 먼저 발송 (가장 중요한 결론 → 즉시 확인)
        if oracle_result and oracle_result.get("has_data"):
            oracle_msg = telegram_bot.format_oracle_section(oracle_result)
            if oracle_msg:
                await telegram_bot.send_async(oracle_msg)

        # 전체 마감 리포트 후발송 — [v10.6 Phase 4-2] FULL_REPORT_FORMAT 분기
        import config as _cfg_fmt
        if _cfg_fmt.FULL_REPORT_FORMAT:
            message = telegram_bot.format_closing_report_full(report)
        else:
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

def _update_watchlist_from_closing(
    closing_strength_result: list,
    volume_flat_result: list,
    price_result: dict,
) -> None:
    """
    마감봇 T5/T6 결과로 내일 워치리스트 보강 (v3.2 신규)
    아침봇(morning_report)이 생성한 워치리스트에 마감봇 발견 종목을 추가한다.

    [우선순위]
    - T5 마감 강도 상위 종목: 우선순위 5 (강봉 → 내일 추가 상승 가능성)
    - T6 횡보 거래량 상위 종목: 우선순위 6 (세력 매집 패턴)
    단, 워치리스트 한도(WS_WATCHLIST_MAX=40) 초과 시 기존 항목 유지 우선
    """
    import config
    current = watchlist_state.get_watchlist()
    additions = {}

    # T5 마감 강도 종목 추가
    for s in closing_strength_result:
        ticker = s["종목코드"]
        if ticker not in current and len(current) + len(additions) < config.WS_WATCHLIST_MAX:
            by_code = price_result.get("by_code", {})
            prdy_vol = by_code.get(ticker, {}).get("거래량", 1)
            additions[ticker] = {
                "종목명":   s["종목명"],
                "전일거래량": prdy_vol,
                "우선순위":   5,
            }

    # T6 횡보 거래량 종목 추가
    for s in volume_flat_result:
        ticker = s["종목코드"]
        if ticker not in current and ticker not in additions:
            if len(current) + len(additions) < config.WS_WATCHLIST_MAX:
                additions[ticker] = {
                    "종목명":   s["종목명"],
                    "전일거래량": s.get("거래량", 1),
                    "우선순위":   6,
                }

    if additions:
        merged = {**current, **additions}
        watchlist_state.set_watchlist(merged)
        logger.info(
            f"[closing] 마감봇 워치리스트 보강: "
            f"T5 {len(closing_strength_result)}종목 + T6 {len(volume_flat_result)}종목 "
            f"→ {len(additions)}종목 추가 (총 {len(merged)}종목)"
        )
    else:
        logger.info("[closing] 워치리스트 추가 없음 (한도 초과 또는 이미 포함)")
