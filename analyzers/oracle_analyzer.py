"""
analyzers/oracle_analyzer.py
쪽집게 분석기 — 테마/수급/공시/T5·T6·T3 종합 → 내일 주도 테마 + 종목 픽 + 진입조건

[ARCHITECTURE 의존성]
oracle_analyzer ← closing_report (price_result, theme_result, T5/T6/T3 결과 전달)
oracle_analyzer ← morning_report (price_result, theme_result, 공시 AI 결과 전달)
oracle_analyzer → telegram_bot.format_oracle_section() 이 포맷

[설계 원칙 — 윌리엄 오닐 CAN SLIM 철학 적용]
- 아침봇·마감봇 보고서에 "데이터 나열"이 아닌 "결론" 추가
- 모든 픽에 진입가·목표가·손절가·R/R 명시 (판단 전가 금지)
- 손절 철칙: -7% 절대 (O'Neil 규칙)
- 시장 환경별 R/R 기준 분기: 강세장 1.2+ / 약세장·횡보 2.0+ / 기본 1.5+
- 컨플루언스 스코어링: 기관/외인 수급 + 소외도 에너지 + T5 마감강도 + 공시 AI + T3/T6

[규칙 — ARCHITECTURE.md 준수]
- 분석·점수 계산만 담당 — DB 기록·텔레그램 발송·KIS API 호출·수집 로직 금지
- 외부 API 호출 없음 — 입력 데이터 기반 순수 계산
- 실패 시 빈 result 반환 (비치명적) — 호출처에서 oracle=None 허용
- closing_report에서만 T5/T6/T3 파라미터 전달 (morning_report는 None) — rule #16 준수

[v10.0 Phase 1 추가]
- _score_theme(): 철강/방산 섹터 감지 시 +20 부스팅 로직 추가
  rule #94 준수: 신호6(지정학) 결과는 signal_analyzer → signals 경유로만 주입됨
  oracle_analyzer 내부에서 geopolitics_data 직접 참조 금지

[v10.0 Phase 3 추가]
- analyze() 파라미터에 sector_scores(dict) 추가
  signal_result["sector_scores"] 경유로만 주입 (rule #94 계열 준수)
  _score_theme()에서 sector_flow_analyzer 섹터 점수를 컨플루언스에 반영 (+10~+20)

[반환값 규격]
{
    "picks": [           ← 최대 5종목
        {
            "rank":          int,      # 1~5 순위
            "ticker":        str,      # 종목코드 (없으면 "")
            "name":          str,      # 종목명
            "theme":         str,      # 소속 테마
            "entry_price":   int,      # 진입가 (전일 종가)
            "target_price":  int,      # 목표가
            "stop_price":    int,      # 손절가
            "target_pct":    float,    # 목표 수익률 (%)
            "stop_pct":      float,    # 손절 기준 (%) — 항상 -7.0
            "rr_ratio":      float,    # 손익비 (소수점 1자리)
            "score":         int,      # 컨플루언스 점수 (0~100)
            "badges":        list[str],# 판단 근거 배지 목록
            "position_type": str,      # 포지션 타입 (오늘★ / 내일 / 모니터 / 대장)
        }
    ],
    "top_themes": [      ← 상위 3 테마
        {
            "theme":   str,
            "score":   int,
            "factors": list[str],   # 점수 기여 요인 설명
            "leader":  str,         # 대장주명
            "leader_change": float, # 대장주 등락률
        }
    ],
    "market_env":     str,    # 시장 환경 (강세장 / 약세장/횡보 / 횡보 / "")
    "rr_threshold":   float,  # 적용된 R/R 기준
    "one_line":       str,    # 한 줄 요약 (텔레그램 맨 하단 표시용)
    "has_data":       bool,   # 실제 픽이 있는지 여부
}
"""

from utils.logger import logger

# ── 포지션 타입별 목표 수익률 (오닐 비율 적용) ─────────────────────
_TARGET_PCT = {
    "오늘★": 0.15,   # 당일 상한가 근처 → 내일 추가 15% 목표
    "내일":  0.12,
    "모니터": 0.10,
    "대장":  0.08,
    "":      0.10,   # 기본값
}

# 손절 기준 (O'Neil -7% 절대 철칙)
_STOP_PCT = -0.07

# R/R 기준 — 시장 환경별 분기
_RR_THRESHOLD = {
    "강세장":     1.2,
    "약세장":     2.0,
    "약세장/횡보": 2.0,
    "횡보":       2.0,
    "":           1.5,
}


# ══════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════

def analyze(
    theme_map: list,
    price_by_name: dict,
    institutional: list,
    ai_dart_results: list,
    signals: list,
    market_env: str = "",
    closing_strength: list | None = None,   # 마감봇에서만 전달 (T5) — rule #16
    volume_flat: list | None = None,         # 마감봇에서만 전달 (T6) — rule #16
    fund_inflow: list | None = None,         # 마감봇에서만 전달 (T3) — rule #16
    sector_scores: dict | None = None,       # v10.0 Phase 3: 섹터 수급 점수 — signal_result 경유
    event_scores:  dict | None = None,        # v10.0 Phase 4-1: 기업 이벤트 점수 — signal_result 경유
) -> dict:
    """
    컨플루언스 스코어링으로 내일 주도 테마와 종목 픽을 결정한다.

    Args:
        theme_map:        theme_analyzer.analyze() 반환값["theme_map"]
        price_by_name:    price_collector.collect_daily() 반환값["by_name"]
        institutional:    price_collector.collect_daily() 반환값["institutional"]
        ai_dart_results:  ai_analyzer.analyze_dart() 반환값
        signals:          signal_result["signals"] (ai_analyzer.analyze_closing 포함)
        market_env:       watchlist_state.get_market_env() — 강세장/약세장/횡보/""
        closing_strength: closing_strength.analyze() 반환값 — 마감봇 전용
        volume_flat:      volume_flat.analyze() 반환값 — 마감봇 전용
        fund_inflow:      fund_inflow_analyzer.analyze() 반환값 — 마감봇 전용

    Returns:
        쪽집게 분석 결과 dict (규격은 모듈 docstring 참조)
    """
    _empty = _empty_result(market_env)

    try:
        if not theme_map and not signals:
            logger.info("[oracle] 테마·신호 데이터 없음 — 쪽집게 분석 생략")
            return _empty

        # 보조 데이터 인덱싱
        inst_map    = _build_inst_map(institutional)
        dart_map    = _build_dart_map(ai_dart_results)
        cs_set      = _build_code_set(closing_strength or [])    # T5
        vf_set      = _build_code_set(volume_flat     or [])    # T6
        fi_set      = _build_code_set(fund_inflow     or [])    # T3
        signal_map  = _build_signal_map(signals)
        sector_scores_map = sector_scores or {}                  # Phase 3: 섹터 수급 점수
        event_scores_map  = event_scores  or {}                  # Phase 4-1: 기업 이벤트 점수
        rr_threshold = _RR_THRESHOLD.get(market_env, 1.5)

        # ── 1. 테마별 컨플루언스 점수 계산 ─────────────────────
        scored_themes = []
        for theme in theme_map:
            score, factors = _score_theme(
                theme, price_by_name, inst_map, dart_map,
                cs_set, vf_set, fi_set, signal_map, sector_scores_map,
                event_scores_map,
            )
            if score > 0:
                scored_themes.append({
                    "theme":         theme.get("테마명", ""),
                    "score":         score,
                    "factors":       factors,
                    "leader":        theme.get("대장주", ""),
                    "leader_change": theme.get("대장등락률", 0.0),
                    "_theme_obj":    theme,
                })

        scored_themes.sort(key=lambda x: x["score"], reverse=True)
        top_themes = [
            {k: v for k, v in t.items() if k != "_theme_obj"}
            for t in scored_themes[:3]
        ]

        # ── 2. 상위 테마에서 픽 추출 ────────────────────────────
        picks = []
        seen_names = set()

        for theme_entry in scored_themes[:3]:
            theme_obj = theme_entry["_theme_obj"]
            theme_name = theme_entry["theme"]

            for stock in theme_obj.get("종목들", []):
                name = stock.get("종목명", "")
                if not name or name in seen_names:
                    continue

                info = price_by_name.get(name, {})
                code = info.get("종목코드", "")
                close_price = info.get("종가", 0) or info.get("현재가", 0)

                if close_price <= 0:
                    continue

                position_type = stock.get("포지션", "")
                pick = _build_pick(
                    name=name,
                    ticker=code,
                    theme=theme_name,
                    entry_price=close_price,
                    position_type=position_type,
                    theme_score=theme_entry["score"],
                    inst_map=inst_map,
                    dart_map=dart_map,
                    cs_set=cs_set,
                    vf_set=vf_set,
                    fi_set=fi_set,
                    rr_threshold=rr_threshold,
                )
                if pick:
                    seen_names.add(name)
                    picks.append(pick)

                if len(picks) >= 5:
                    break
            if len(picks) >= 5:
                break

        # 픽 순위 부여
        for i, p in enumerate(picks, 1):
            p["rank"] = i

        # ── 3. 한 줄 요약 ────────────────────────────────────────
        one_line = _build_one_line(picks, top_themes, market_env, rr_threshold)

        return {
            "picks":        picks,
            "top_themes":   top_themes,
            "market_env":   market_env,
            "rr_threshold": rr_threshold,
            "one_line":     one_line,
            "has_data":     bool(picks),
        }

    except Exception as e:
        logger.warning(f"[oracle] 쪽집게 분석 실패 (비치명적): {e}", exc_info=True)
        return _empty


# ══════════════════════════════════════════════════════════════════════
# 내부 헬퍼 — 인덱싱
# ══════════════════════════════════════════════════════════════════════

def _build_inst_map(institutional: list) -> dict[str, dict]:
    """종목명 → {기관순매수, 외국인순매수} 맵"""
    result = {}
    for s in institutional:
        name = s.get("종목명", "")
        if name:
            result[name] = {
                "기관순매수":  s.get("기관순매수",   0),
                "외국인순매수": s.get("외국인순매수", 0),
            }
    return result


def _build_dart_map(ai_dart_results: list) -> dict[str, dict]:
    """종목명 → {점수, 이유, 상한가확률} 맵"""
    result = {}
    for r in ai_dart_results:
        name = r.get("종목명", "")
        if name:
            result[name] = r
    return result


def _build_code_set(items: list) -> set[str]:
    """T5/T6/T3 분석 결과에서 종목코드 set 추출"""
    return {s.get("종목코드", "") for s in items if s.get("종목코드")}


def _build_signal_map(signals: list) -> dict[str, dict]:
    """
    테마명 → 신호 dict 맵.
    신호의 관련종목명도 맵에 포함 (종목명 → 신호).
    """
    result = {}
    for s in signals:
        theme = s.get("테마명", "")
        if theme:
            result[theme] = s
        for name in s.get("관련종목", []):
            if name:
                result[name] = s
    return result


# ══════════════════════════════════════════════════════════════════════
# 내부 헬퍼 — 스코어링
# ══════════════════════════════════════════════════════════════════════

def _score_theme(
    theme: dict,
    price_by_name: dict,
    inst_map: dict,
    dart_map: dict,
    cs_set: set,
    vf_set: set,
    fi_set: set,
    signal_map: dict,
    sector_scores: dict = None,   # v10.0 Phase 3: 섹터 수급 점수 (sector_flow_analyzer)
    event_scores:  dict = None,   # v10.0 Phase 4-1: 기업 이벤트 점수 (event_impact_analyzer)
) -> tuple[int, list[str]]:
    """
    테마 하나의 컨플루언스 점수(0~115)와 근거 목록을 반환.

    [점수 배분 — 스마트머니 우선]
    기관/외인 수급  최대 30점  (스마트머니 확인)
    소외도 에너지   최대 25점  (순환매 회전 에너지)
    T5 마감 강도    최대 20점  (마감봇 전용 — 내일 갭업 예측)
    공시 AI 점수    최대 15점  (펀더멘털 촉매)
    T3/T6 보조      최대 10점  (자금유입 확인)
    신호 강도 보너스 최대 5점   (모멘텀 강도)
    [v10.0 Phase 1] 철강/방산 테마 부스팅 최대 20점
    [v10.0 Phase 3] 섹터 수급 보너스 최대 10점 (sector_flow_analyzer Z-스코어 기반)
    """
    score = 0
    factors = []
    stocks = theme.get("종목들", [])
    theme_name = theme.get("테마명", "")

    # ── 기관/외인 수급 (최대 30점) ─────────────────────────────
    inst_count = 0
    frgn_count = 0
    for st in stocks:
        name = st.get("종목명", "")
        m = inst_map.get(name, {})
        if m.get("기관순매수", 0) > 0:
            inst_count += 1
        if m.get("외국인순매수", 0) > 0:
            frgn_count += 1

    smart_money_count = inst_count + frgn_count
    if smart_money_count >= 6:
        score += 30; factors.append(f"기관/외인 {smart_money_count}종목 순매수 ★★★")
    elif smart_money_count >= 4:
        score += 22; factors.append(f"기관/외인 {smart_money_count}종목 순매수 ★★")
    elif smart_money_count >= 2:
        score += 14; factors.append(f"기관/외인 {smart_money_count}종목 순매수 ★")
    elif smart_money_count >= 1:
        score += 7;  factors.append(f"기관/외인 {smart_money_count}종목 순매수")

    # ── 소외도 에너지 (최대 25점) ──────────────────────────────
    # 소외도가 높은 종목 = 테마 내 아직 오르지 않은 순환매 후보
    total_소외 = sum(
        st.get("소외도", 0.0) for st in stocks
        if isinstance(st.get("소외도"), (int, float))
    )
    avg_소외 = total_소외 / len(stocks) if stocks else 0
    if avg_소외 >= 5.0:
        score += 25; factors.append(f"소외도 평균 {avg_소외:.1f} ★★★")
    elif avg_소외 >= 3.0:
        score += 18; factors.append(f"소외도 평균 {avg_소외:.1f} ★★")
    elif avg_소외 >= 1.5:
        score += 10; factors.append(f"소외도 평균 {avg_소외:.1f} ★")
    elif avg_소외 > 0:
        score += 5;  factors.append(f"소외도 평균 {avg_소외:.1f}")

    # ── T5 마감 강도 (최대 20점) — 마감봇 전용 ──────────────────
    cs_count = sum(
        1 for st in stocks
        if price_by_name.get(st.get("종목명", ""), {}).get("종목코드", "") in cs_set
    )
    if cs_count >= 3:
        score += 20; factors.append(f"T5 마감강도 {cs_count}종목 ★★★")
    elif cs_count == 2:
        score += 14; factors.append(f"T5 마감강도 {cs_count}종목 ★★")
    elif cs_count == 1:
        score += 8;  factors.append(f"T5 마감강도 {cs_count}종목 ★")

    # ── 공시 AI 점수 (최대 15점) ───────────────────────────────
    max_dart_score = max(
        (dart_map.get(st.get("종목명", ""), {}).get("점수", 0) for st in stocks),
        default=0,
    )
    if max_dart_score >= 9:
        score += 15; factors.append(f"공시 AI {max_dart_score}/10 ★★★")
    elif max_dart_score >= 7:
        score += 10; factors.append(f"공시 AI {max_dart_score}/10 ★★")
    elif max_dart_score >= 5:
        score += 5;  factors.append(f"공시 AI {max_dart_score}/10 ★")

    # ── T3 자금유입 + T6 횡보급증 보조 (최대 10점) ──────────────
    fi_count = sum(
        1 for st in stocks
        if price_by_name.get(st.get("종목명", ""), {}).get("종목코드", "") in fi_set
    )
    vf_count = sum(
        1 for st in stocks
        if price_by_name.get(st.get("종목명", ""), {}).get("종목코드", "") in vf_set
    )
    if fi_count >= 2:
        score += 7;  factors.append(f"T3 자금유입 {fi_count}종목")
    elif fi_count == 1:
        score += 4;  factors.append(f"T3 자금유입 {fi_count}종목")
    if vf_count >= 1:
        score += 3;  factors.append(f"T6 횡보급증 {vf_count}종목")

    # ── 신호 강도 보너스 (최대 5점) ────────────────────────────
    sig = signal_map.get(theme_name)
    if sig:
        sig_strength = sig.get("강도", 0)
        if sig_strength >= 5:
            score += 5; factors.append(f"신호강도 ★★★★★")
        elif sig_strength >= 4:
            score += 4; factors.append(f"신호강도 ★★★★")
        elif sig_strength >= 3:
            score += 2; factors.append(f"신호강도 ★★★")

    # ── v10.0 Phase 1: 철강/방산 테마 부스팅 (+20) ────────────
    # 설계 근거: 지정학·원자재 신호로 발화한 철강/방산 테마는
    # 기존 수급 데이터(기관/외인)만으로 포착되지 않으므로
    # 테마명 기반 예외 부스팅 적용 (design doc §4.1 참조)
    BOOST_THEMES = {
        "철강/비철금속", "철강", "방산", "산업재/방산",
        "에너지솔루션", "자동차부품",
    }
    if theme_name in BOOST_THEMES:
        # 신호6(지정학) 또는 신호2(철강ETF)에서 발화한 테마에만 부스팅
        has_geo_signal = any(
            "신호6" in sig.get("발화신호", "") or "신호2" in sig.get("발화신호", "")
            for sig in signal_map.values()
            if sig.get("테마명", "") == theme_name
        )
        # signal_map에 해당 테마 신호 자체가 존재하면 부스팅
        if theme_name in signal_map or has_geo_signal:
            score += 20
            factors.append(f"🌍 지정학/철강ETF 신호 테마 부스팅 +20 [v10]")
            logger.info(f"[oracle] {theme_name} 부스팅 +20 (현재 점수: {score})")

    # ── v10.0 Phase 3: 섹터 수급 보너스 (+10 또는 +20) ─────────
    # sector_flow_analyzer Z-스코어 기반 ETF 거래량 이상 섹터에 추가 점수
    # rule #94 계열: sector_scores는 signal_result["sector_scores"] 경유로만 주입
    if sector_scores:
        sf_score = sector_scores.get(theme_name, 0)
        if sf_score >= 30:
            score += 20
            factors.append(f"📊 섹터ETF+공매도 수급 신호 +20 [신호7·Z≥2]")
            logger.info(f"[oracle] {theme_name} 섹터수급 보너스 +20 (sf_score={sf_score})")
        elif sf_score >= 15:
            score += 10
            factors.append(f"📊 섹터ETF 거래량 이상 +10 [신호7]")
            logger.info(f"[oracle] {theme_name} 섹터수급 보너스 +10 (sf_score={sf_score})")

    # ── v10.0 Phase 4-1: 기업 이벤트 보너스 (+5~+15) ──────────
    # event_impact_analyzer 기업 이벤트 캘린더 기반 수급 예측
    # rule #94 계열: event_scores는 signal_result["event_scores"] 경유로만 주입
    if event_scores:
        # 테마 내 종목들의 이벤트 점수 최댓값 조회
        theme_stocks = [s.get("name", "") for s in theme.get("stocks", [])]
        max_ev_score = 0
        for stock_name in theme_stocks:
            # by_code 없이 종목명→ticker 직접 매핑이 어려우므로 theme_name 기준 폴백
            ev_strength = event_scores.get(stock_name, 0)
            max_ev_score = max(max_ev_score, ev_strength)
        if max_ev_score >= 5:
            score += 15
            factors.append(f"📅 기업이벤트 D-1 수급 예측 +15 [신호8·강도{max_ev_score}]")
            logger.info(f"[oracle] {theme_name} 기업이벤트 보너스 +15 (ev_strength={max_ev_score})")
        elif max_ev_score >= 4:
            score += 10
            factors.append(f"📅 기업이벤트 D-2 수급 예측 +10 [신호8·강도{max_ev_score}]")
            logger.info(f"[oracle] {theme_name} 기업이벤트 보너스 +10 (ev_strength={max_ev_score})")
        elif max_ev_score >= 3:
            score += 5
            factors.append(f"📅 기업이벤트 일정 +5 [신호8·강도{max_ev_score}]")

    return score, factors


def _build_pick(
    name: str,
    ticker: str,
    theme: str,
    entry_price: int,
    position_type: str,
    theme_score: int,
    inst_map: dict,
    dart_map: dict,
    cs_set: set,
    vf_set: set,
    fi_set: set,
    rr_threshold: float,
) -> dict | None:
    """
    단일 종목 픽을 생성. R/R 기준 미달 시 None 반환.

    진입가: 전일 종가 (price_by_name에서 가져온 값)
    목표가: 진입가 × (1 + target_pct) — 포지션 타입별 차등
    손절가: 진입가 × 0.93 (O'Neil -7% 철칙)
    R/R:    (목표 - 진입) / (진입 - 손절)
    """
    target_pct   = _TARGET_PCT.get(position_type, _TARGET_PCT[""])
    target_price = round(entry_price * (1 + target_pct))
    stop_price   = round(entry_price * (1 + _STOP_PCT))

    expected_return = target_price - entry_price
    expected_loss   = entry_price  - stop_price

    if expected_loss <= 0:
        return None

    rr_ratio = round(expected_return / expected_loss, 1)

    if rr_ratio < rr_threshold:
        return None

    # 판단 근거 배지
    badges = []
    m = inst_map.get(name, {})
    if m.get("기관순매수", 0) > 0 and m.get("외국인순매수", 0) > 0:
        badges.append("기관/외인↑")
    elif m.get("기관순매수", 0) > 0:
        badges.append("기관↑")
    elif m.get("외국인순매수", 0) > 0:
        badges.append("외인↑")

    if ticker in cs_set:
        badges.append("마감강도↑")
    if ticker in vf_set:
        badges.append("횡보급증")
    if ticker in fi_set:
        badges.append("자금유입↑")

    dart = dart_map.get(name, {})
    if dart.get("점수", 0) >= 7:
        badges.append(f"공시AI {dart['점수']}/10")

    return {
        "rank":          0,          # 호출처에서 부여
        "ticker":        ticker,
        "name":          name,
        "theme":         theme,
        "entry_price":   entry_price,
        "target_price":  target_price,
        "stop_price":    stop_price,
        "target_pct":    round(target_pct * 100, 1),
        "stop_pct":      round(_STOP_PCT * 100, 1),
        "rr_ratio":      rr_ratio,
        "score":         theme_score,
        "badges":        badges,
        "position_type": position_type,
    }


def _build_one_line(
    picks: list,
    top_themes: list,
    market_env: str,
    rr_threshold: float,
) -> str:
    """텔레그램 맨 하단 표시용 한 줄 요약"""
    if not picks:
        return f"[{market_env or '장세미정'}] 조건 충족 픽 없음 (R/R {rr_threshold:.1f}x 미달)"

    theme_names = " · ".join(t["theme"] for t in top_themes[:2])
    best = picks[0]
    return (
        f"[{market_env or '장세미정'}] 주도테마: {theme_names} | "
        f"최선픽: {best['name']} "
        f"(진입{best['entry_price']:,} → 목표{best['target_price']:,} / "
        f"손절{best['stop_price']:,}  R/R {best['rr_ratio']:.1f})"
    )


def _empty_result(market_env: str) -> dict:
    rr = _RR_THRESHOLD.get(market_env, 1.5)
    return {
        "picks":        [],
        "top_themes":   [],
        "market_env":   market_env,
        "rr_threshold": rr,
        "one_line":     f"[{market_env or '장세미정'}] 분석 데이터 부족",
        "has_data":     False,
    }