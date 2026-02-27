"""
analyzers/signal_analyzer.py
신호 1~5 통합 판단 전담
- dart, market, news, price 수집 결과를 받아 신호별로 정리
- 수집/발송 로직 없음

[수정이력]
- v2.1: 신호4 추가, 섹터 ETF 신호 추가, price_data 파라미터 추가
- v2.2: 전선주 하드코딩 → config.COPPER_KR_STOCKS 참조
        신호4 저변동 스킵 조건 개선 (종목 있으면 스킵 안 함)
- v2.3: 종목명 하드코딩 전면 제거
        _get_sector_top_stocks() 신규 — price_data["by_sector"]에서
        업종명 키워드 매칭으로 실제 등락률 상위 종목을 동적으로 조회
        US_SECTOR_KR_MAP(종목 고정) → US_SECTOR_KR_INDUSTRY(업종 키워드) 사용
        COMMODITY_KR_INDUSTRY 신규 사용
- v10.0 Phase 1: 신호2 확장 — 미국 철강 ETF(XME/SLX) 급등 감지 로직 추가
        _analyze_steel_etf(): XME/SLX가 STEEL_ETF_ALERT_THRESHOLD 이상 급등 시
        '철강/비철금속' 테마 신호2 추가 발화
        analyze() 파라미터에 geopolitics_data(기본 None) 추가 — Phase 2 신호6 대비
- v10.0 Phase 2: 신호6 — 지정학·정책 이벤트 신호 통합
        geopolitics_data(geopolitics_collector.collect() 반환값) 주입 시 신호6 생성
        _analyze_geopolitics() 추가
- v10.0 Phase 3: 신호7 — 섹터 자금흐름 + 공매도 잔고 신호 통합
        sector_flow_data(sector_flow_analyzer.analyze() 반환값) 주입 시 신호7 생성
        rule #94 계열 준수: sector_flow_analyzer → signal_analyzer → oracle_analyzer 경유 필수
- v10.0 Phase 4-1: 신호8 — 기업 이벤트 캘린더 + 네이버 DataLab 트렌드 신호 통합
        event_impact_data(event_impact_analyzer.analyze() 반환값) 주입 시 신호8 생성
        datalab_data(news_collector.collect()["datalab_trends"] 반환값) 주입 시 DataLab 신호 추가
"""

import config
from utils.logger import logger


def analyze(
    dart_data:          list[dict],
    market_data:        dict,
    news_data:          dict,
    price_data:         dict = None,
    geopolitics_data:   list[dict] = None,   # v10.0 Phase 2: 지정학 이벤트 (None이면 신호6 생략)
    sector_flow_data:   dict       = None,   # v10.0 Phase 3: 섹터 자금흐름 (None이면 신호7 생략)
    event_impact_data:  list[dict] = None,   # v10.0 Phase 4-1: 기업 이벤트 신호 (None이면 신호8 생략)
    datalab_data:       list[dict] = None,   # v10.0 Phase 4-1: DataLab 트렌드 (None이면 생략)
) -> dict:
    """
    신호 1~8 통합 분석
    반환: dict {signals, market_summary, commodities, volatility,
                report_picks, policy_summary, sector_scores, event_scores}

    [v10.0 추가]
    - geopolitics_data: geopolitics_collector.collect() 반환값.
      None(기본)이면 신호6 생략 (Phase 1·2 하위 호환).
    - sector_flow_data: sector_flow_analyzer.analyze() 반환값.
      None(기본)이면 신호7 생략 (Phase 1·2 하위 호환).
      sector_scores 포함 시 oracle_analyzer에 전달 가능.
    - event_impact_data: event_impact_analyzer.analyze() 반환값.
      None(기본)이면 신호8 생략 (Phase 4-1 이전 하위 호환).
      event_scores 포함 시 oracle_analyzer에 전달 가능.
    - datalab_data: news_collector.collect()["datalab_trends"] 반환값.
      None(기본)이면 DataLab 신호 생략.
    """
    logger.info("[signal] 신호 1~5 분석 시작")

    signals        = []
    market_summary = market_data.get("us_market", {})
    commodities    = market_data.get("commodities", {})

    # ── 신호 1: DART 공시 ────────────────────────────────────
    for dart in dart_data:
        strength = _dart_strength(dart)
        if strength == 0:
            continue
        signals.append({
            "테마명":   _dart_to_theme(dart["공시종류"], dart["종목명"]),
            "발화신호": f"신호1: {dart['공시종류']} [{dart['종목명']}|{dart['공시시각']}]",
            "강도":     strength,
            "신뢰도":   dart["신뢰도"],
            "발화단계": "1일차",
            "상태":     "신규",
            "관련종목": [dart["종목명"]],
        })

    # ── 신호 2: 미국증시 + 원자재 + 섹터 연동 ────────────────
    # v2.3: price_data["by_sector"]를 넘겨서 실제 대장주 동적 조회
    by_sector = price_data.get("by_sector", {}) if price_data else {}
    us_signals = _analyze_us_market(market_summary, commodities, by_sector)
    signals.extend(us_signals)

    # v10.0 Phase 1: 신호2 확장 — 미국 철강 ETF 급등 전용 감지
    sectors = market_summary.get("sectors", {})
    steel_signals = _analyze_steel_etf(sectors, by_sector)
    signals.extend(steel_signals)

    # ── 신호 3: 증권사 리포트 ────────────────────────────────
    reports = news_data.get("reports", [])
    for report in reports[:5]:
        if report["액션"] in ["목표가상향", "신규매수"]:
            signals.append({
                "테마명":   f"{report['종목명']} ({report['증권사']})",
                "발화신호": f"신호3: {report['액션']} [{report['증권사']}|오늘]",
                "강도":     4,
                "신뢰도":   report["신뢰도"],
                "발화단계": "1일차",
                "상태":     "신규",
                "관련종목": [report["종목명"]],
            })

    # ── 신호 4: 전날 급등/상한가 순환매 ─────────────────────
    if price_data:
        price_signals = _analyze_prev_price(price_data)
        signals.extend(price_signals)
        logger.info(f"[signal] 신호4 (순환매): {len(price_signals)}개 테마 감지")

    # ── 신호 5: 정책·시황 ────────────────────────────────────
    policy = news_data.get("policy_news", [])
    for p in policy[:3]:
        signals.append({
            "테마명":   "정책/시황",
            "발화신호": f"신호5: {p['제목'][:40]}",
            "강도":     2,
            "신뢰도":   "스니펫",
            "발화단계": "불명",
            "상태":     "모니터",
            "관련종목": [],
        })

    # ── 신호 6: 지정학·정책 이벤트 (v10.0 Phase 2) ────────────
    if geopolitics_data:
        geo_signals = _analyze_geopolitics(geopolitics_data, by_sector)
        signals.extend(geo_signals)
        logger.info(f"[signal] 신호6 (지정학): {len(geo_signals)}개 이벤트 발화")

    # ── 신호 7: 섹터 자금흐름 + 공매도 잔고 (v10.0 Phase 3) ───
    # rule #94 계열: sector_flow_analyzer → signal_analyzer → oracle_analyzer 경유 필수
    # oracle_analyzer에 직접 전달 금지 — signals 리스트에 추가
    sector_scores: dict = {}
    if sector_flow_data:
        sf_signals = sector_flow_data.get("signals", [])
        signals.extend(sf_signals)
        sector_scores = sector_flow_data.get("sector_scores", {})
        logger.info(
            f"[signal] 신호7 (섹터수급): {len(sf_signals)}개 신호 추가 "
            f"(섹터점수 {len(sector_scores)}개)"
        )

    # ── 신호 8: 기업 이벤트 캘린더 (v10.0 Phase 4-1) ──────────
    # rule #94 계열: event_impact_analyzer → signal_analyzer → oracle_analyzer 경유 필수
    event_scores: dict = {}   # {ticker: strength} oracle_analyzer 전달용
    if event_impact_data:
        ev_signals = _analyze_event_impact(event_impact_data, price_data)
        signals.extend(ev_signals)
        # event_scores 구성: 이벤트 발생 종목 강도 매핑
        for ev in event_impact_data:
            ticker = ev.get(ticker, )
            if ticker:
                event_scores[ticker] = max(
                    event_scores.get(ticker, 0), ev.get(strength, 3)
                )
        logger.info(f"[signal] 신호8 (기업이벤트): {len(ev_signals)}개 신호 추가")

    # ── DataLab 트렌드 신호 (v10.0 Phase 4-1) ──────────────────
    if datalab_data:
        dl_signals = _analyze_datalab_trends(datalab_data, price_data)
        signals.extend(dl_signals)
        logger.info(f"[signal] DataLab 트렌드: {len(dl_signals)}개 신호 추가")

    signals.sort(key=lambda x: x["강도"], reverse=True)
    logger.info(f"[signal] 총 {len(signals)}개 신호 감지 (신호1~8)")

    volatility = _judge_volatility(market_summary, price_data)

    return {
        "signals":        signals,
        "market_summary": market_summary,
        "commodities":    commodities,
        "volatility":     volatility,
        "report_picks":   reports[:5],
        "policy_summary": policy[:3],
        "sector_scores":  sector_scores,   # v10.0 Phase 3: oracle_analyzer에 전달
        "event_scores":   event_scores,    # v10.0 Phase 4-1: oracle_analyzer에 전달
    }


# ══════════════════════════════════════════════════════════════
# 신호 2: 미국증시·원자재·섹터 분석 (v2.3 개편)
# ══════════════════════════════════════════════════════════════

def _analyze_us_market(us: dict, commodities: dict, by_sector: dict) -> list[dict]:
    """
    미국증시 섹터 + 원자재 → 국내 연동 테마 시그널

    [v2.3 변경]
    기존: config에 하드코딩된 종목 리스트를 관련종목으로 사용
    수정: by_sector(price_data["by_sector"])에서 해당 업종의
         실제 등락률 상위 종목을 동적으로 조회
    → 매일 그날 실제로 움직인 종목이 대장주로 표시됨
    """
    signals = []

    # 섹터 ETF 연동 신호
    sectors = us.get("sectors", {})
    for sector_name, sector_data in sectors.items():
        change_str = sector_data.get("change", "N/A")
        if change_str == "N/A":
            continue
        try:
            pct = float(change_str.replace("%", "").replace("+", ""))
        except ValueError:
            continue

        if abs(pct) < config.US_SECTOR_SIGNAL_MIN:
            continue

        direction = "강세↑" if pct > 0 else "약세↓"
        강도 = 4 if abs(pct) >= 3.0 else 3 if abs(pct) >= 2.0 else 2

        # v2.3: 업종명 키워드로 실제 등락률 상위 종목 조회
        industry_keywords = config.US_SECTOR_KR_INDUSTRY.get(sector_name, [])
        관련종목 = _get_sector_top_stocks(by_sector, industry_keywords)

        signals.append({
            "테마명":   sector_name,
            "발화신호": f"신호2: 미국 {sector_name} {direction} {change_str} [섹터ETF|전날]",
            "강도":     강도 if pct > 0 else 1,
            "신뢰도":   sector_data.get("신뢰도", "yfinance"),
            "발화단계": "불명",
            "상태":     "모니터" if pct > 0 else "경고",
            "관련종목": 관련종목,
        })

    # 구리 강세 → 전선주 (v2.3: 업종명 키워드로 동적 조회)
    copper = commodities.get("copper", {})
    if _is_positive(copper.get("change", "N/A")):
        keywords  = config.COMMODITY_KR_INDUSTRY.get("copper", [])
        관련종목  = _get_sector_top_stocks(by_sector, keywords)
        signals.append({
            "테마명":   "전선/구리",
            "발화신호": f"신호2: 구리 {copper['change']} [LME|전날]",
            "강도":     3,
            "신뢰도":   copper.get("신뢰도", "N/A"),
            "발화단계": "불명",
            "상태":     "모니터",
            "관련종목": 관련종목,
        })

    # 은 강세 → 귀금속/태양광 (v2.3: 업종명 키워드로 동적 조회)
    silver = commodities.get("silver", {})
    if _is_positive(silver.get("change", "N/A")):
        keywords  = config.COMMODITY_KR_INDUSTRY.get("silver", [])
        관련종목  = _get_sector_top_stocks(by_sector, keywords)
        signals.append({
            "테마명":   "귀금속/태양광",
            "발화신호": f"신호2: 은 {silver['change']} [COMEX|전날]",
            "강도":     2,
            "신뢰도":   silver.get("신뢰도", "N/A"),
            "발화단계": "불명",
            "상태":     "모니터",
            "관련종목": 관련종목,
        })

    return signals


# ══════════════════════════════════════════════════════════════
# v10.0 Phase 1 — 철강 ETF 급등 감지 (신호2 확장)
# ══════════════════════════════════════════════════════════════

def _analyze_steel_etf(sectors: dict, by_sector: dict) -> list[dict]:
    """
    미국 철강 ETF(XME, SLX) 급등 → '철강/비철금속' 신호2 추가 발화.

    config.STEEL_ETF_ALERT_THRESHOLD(기본 3.0%) 이상 급등 시 독립 신호 생성.
    US_SECTOR_TICKERS에서 이미 XME/SLX를 처리하지만, 임계값이 낮아
    단순 1% 움직임도 신호가 될 수 있음. 이 함수는 철강 전용 고임계값 필터.
    """
    result = []
    steel_etfs = ["철강/비철금속", "철강"]   # config.US_SECTOR_TICKERS의 value와 일치
    threshold = config.STEEL_ETF_ALERT_THRESHOLD

    for etf_label in steel_etfs:
        data = sectors.get(etf_label, {})
        change_str = data.get("change", "N/A")
        if change_str == "N/A":
            continue
        try:
            pct = float(change_str.replace("%", "").replace("+", ""))
        except ValueError:
            continue

        if pct < threshold:
            continue

        # threshold 이상 — 철강 테마 고강도 발화
        keywords  = config.COMMODITY_KR_INDUSTRY.get("steel", [])
        관련종목  = _get_sector_top_stocks(by_sector, keywords)

        강도 = 5 if pct >= 5.0 else 4   # 5% 이상이면 최강도

        result.append({
            "테마명":   "철강/비철금속",
            "발화신호": f"신호2: 미국 {etf_label} ETF {change_str} 급등 — 철강 테마 선행 신호 [v10]",
            "강도":     강도,
            "신뢰도":   data.get("신뢰도", "yfinance"),
            "발화단계": "1일차",
            "상태":     "신규",
            "관련종목": 관련종목,
        })
        logger.info(f"[signal] 신호2(철강ETF): {etf_label} {change_str} — 철강 테마 발화 (강도{강도})")
        break   # XME, SLX 중 하나만 발화 (중복 방지)

    return result


# ══════════════════════════════════════════════════════════════
# v10.0 Phase 2 — 지정학·정책 이벤트 신호 (신호6)
# ══════════════════════════════════════════════════════════════

def _analyze_geopolitics(geopolitics_data: list[dict], by_sector: dict) -> list[dict]:
    """
    geopolitics_collector.collect() 반환값을 받아 신호6으로 변환.

    각 이벤트 dict 형식:
    {
        event_type:        str,   # 이벤트 유형
        affected_sectors:  list,  # 영향 섹터 리스트
        impact_direction:  str,   # '+' 또는 '-'
        confidence:        float, # 0~1
        source_url:        str,
        event_summary_kr:  str,   # 한국어 요약
    }
    """
    signals = []
    min_confidence = config.GEOPOLITICS_CONFIDENCE_MIN

    for event in geopolitics_data:
        confidence = event.get("confidence", 0.0)
        if confidence < min_confidence:
            continue

        affected = event.get("affected_sectors", [])
        direction = event.get("impact_direction", "+")
        summary_kr = event.get("event_summary_kr", "")
        event_type = event.get("event_type", "지정학이벤트")

        for sector in affected[:3]:   # 최대 3개 섹터로 제한
            keywords   = config.US_SECTOR_KR_INDUSTRY.get(sector, [sector])
            관련종목   = _get_sector_top_stocks(by_sector, keywords)

            강도 = 5 if confidence >= 0.85 else 4 if confidence >= 0.70 else 3
            상태  = "신규" if direction == "+" else "경고"

            signals.append({
                "테마명":   sector,
                "발화신호": (
                    f"신호6: {event_type} — {summary_kr[:50]}"
                    f" [신뢰도:{confidence:.0%}|지정학]"
                ),
                "강도":     강도,
                "신뢰도":   f"geo:{confidence:.2f}",
                "발화단계": "1일차",
                "상태":     상태,
                "관련종목": 관련종목,
            })

    return signals


def _get_sector_top_stocks(
    by_sector: dict, keywords: list[str], top_n: int = None
) -> list[str]:
    """
    업종명 키워드로 price_data["by_sector"]를 검색해서
    실제 등락률 상위 종목명 리스트 반환

    [동작 방식]
    1. by_sector의 모든 업종명을 순회
    2. 업종명에 keyword가 포함(in)되면 해당 업종 매칭
    3. 매칭된 업종들의 종목 합산 후 등락률 내림차순 상위 N개 반환

    [by_sector가 없을 때]
    빈 리스트 반환 → theme_analyzer에서 소외도 N/A로 표시
    → 기존 동작과 동일, 에러 없음

    Args:
        by_sector:  price_data["by_sector"]
        keywords:   config의 업종명 키워드 리스트 (예: ["전기/전선", "전선"])
        top_n:      None이면 config.SECTOR_TOP_N 사용
    """
    if not by_sector or not keywords:
        return []

    if top_n is None:
        top_n = config.SECTOR_TOP_N

    matched: list[dict] = []
    for sector_name, entries in by_sector.items():
        if any(kw in sector_name for kw in keywords):
            matched.extend(entries)

    if not matched:
        return []

    # 등락률 내림차순 → 상위 top_n → 종목명만 추출
    matched.sort(key=lambda x: x["등락률"], reverse=True)
    return [e["종목명"] for e in matched[:top_n]]


# ══════════════════════════════════════════════════════════════
# 신호 4: 전날 급등/상한가 순환매 (v2.2 기준 유지)
# ══════════════════════════════════════════════════════════════

def _analyze_prev_price(price_data: dict) -> list[dict]:
    """
    전날 상한가·급등 종목 → 순환매 신호 생성

    [v2.2 저변동 스킵 조건]
    지수 0%여도 상한가·급등 종목 있으면 스킵하지 않음
    (지수 0% = pykrx 단일날짜 조회 이슈 가능성)
    """
    signals = []

    kospi_rate  = price_data.get("kospi",  {}).get("change_rate", 0)
    kosdaq_rate = price_data.get("kosdaq", {}).get("change_rate", 0)
    upper_limit = price_data.get("upper_limit", [])
    top_gainers = price_data.get("top_gainers", [])
    has_movers  = bool(upper_limit) or bool(top_gainers)

    is_low_vol = (abs(kospi_rate) < 1.0) and (abs(kosdaq_rate) < 1.0)

    if is_low_vol and not has_movers:
        logger.info(
            f"[signal] 저변동 장세 + 급등종목 없음 — 코스피:{kospi_rate:+.2f}% "
            f"코스닥:{kosdaq_rate:+.2f}% → 신호4 스킵"
        )
        return []

    if is_low_vol and has_movers:
        logger.info(
            f"[signal] 지수 저변동 — 코스피:{kospi_rate:+.2f}% "
            f"코스닥:{kosdaq_rate:+.2f}% — 상한가:{len(upper_limit)}개 "
            f"급등:{len(top_gainers)}개 존재 → 신호4 진행"
        )

    # 상한가 그룹
    if upper_limit:
        sorted_upper = sorted(upper_limit, key=lambda x: x["등락률"], reverse=True)
        관련종목 = [s["종목명"] for s in sorted_upper[:10]]
        signals.append({
            "테마명":   "상한가 순환매",
            "발화신호": (
                f"신호4: 전날 상한가 {len(upper_limit)}종목 "
                f"[대장:{sorted_upper[0]['종목명']} {sorted_upper[0]['등락률']:+.1f}%|pykrx]"
            ),
            "강도":     5,
            "신뢰도":   "pykrx",
            "발화단계": "2일차",
            "상태":     "진행",
            "관련종목": 관련종목,
            "ai_memo":  f"전날 상한가 {len(upper_limit)}종목 — 오늘 2·3등주 순환매 주목",
        })

    # 급등 그룹 (KOSPI / KOSDAQ 분리)
    upper_names  = {s["종목명"] for s in upper_limit}
    gainers_only = [s for s in top_gainers if s["종목명"] not in upper_names]

    for market in ["KOSPI", "KOSDAQ"]:
        market_gainers = [s for s in gainers_only if s.get("시장") == market][:10]
        if not market_gainers:
            continue

        top  = market_gainers[0]
        rate = top["등락률"]
        강도  = 5 if rate >= 20 else 4 if rate >= 10 else 3

        signals.append({
            "테마명":   f"{market} 급등 순환매",
            "발화신호": (
                f"신호4: 전날 {market} 급등 {len(market_gainers)}종목 "
                f"[대장:{top['종목명']} {rate:+.1f}%|pykrx]"
            ),
            "강도":     강도,
            "신뢰도":   "pykrx",
            "발화단계": "2일차",
            "상태":     "진행",
            "관련종목": [s["종목명"] for s in market_gainers],
            "ai_memo":  f"전날 대장 {top['종목명']} {rate:+.1f}% — 소외주 오늘 순환매 가능",
        })

    return signals


# ══════════════════════════════════════════════════════════════
# 내부 헬퍼
# ══════════════════════════════════════════════════════════════

def _dart_strength(dart: dict) -> int:
    report = dart.get("공시종류", "")
    if any(kw in report for kw in ["단일판매공급계약체결", "수주"]):
        return 5
    if any(kw in report for kw in ["판결", "특허"]):
        return 5
    if "배당결정" in report:
        return 4
    if "자사주취득결정" in report:
        return 4
    if "MOU" in report:
        return 3
    if "주요주주" in report:
        return 3
    return 1


def _dart_to_theme(report_nm: str, stock_nm: str) -> str:
    if "수주" in report_nm or "공급계약" in report_nm:
        return f"{stock_nm} 수주"
    if "배당" in report_nm:
        return f"{stock_nm} 배당"
    if "자사주" in report_nm:
        return f"{stock_nm} 자사주"
    if "특허" in report_nm or "판결" in report_nm:
        return f"{stock_nm} 특허/소송"
    if "주요주주" in report_nm:
        return f"{stock_nm} 내부자매수"
    return f"{stock_nm} 공시"


def _is_positive(change_str: str) -> bool:
    if change_str in ("N/A", "", None):
        return False
    return change_str.startswith("+") or (
        not change_str.startswith("-") and
        any(c.isdigit() for c in change_str)
    )


def _judge_volatility(us: dict, price_data: dict = None) -> str:
    if price_data:
        kospi_rate  = price_data.get("kospi",  {}).get("change_rate", None)
        kosdaq_rate = price_data.get("kosdaq", {}).get("change_rate", None)
        if kospi_rate is not None and kosdaq_rate is not None:
            rate = max(abs(kospi_rate), abs(kosdaq_rate))
            if rate >= 2.0:   return "고변동"
            elif rate >= 1.0: return "중변동"
            else:             return "저변동 (순환매 에너지 낮음)"

    nasdaq = us.get("nasdaq", "N/A")
    if nasdaq == "N/A":
        return "판단불가"
    try:
        val = float(nasdaq.replace("%", "").replace("+", ""))
        return "고변동예상" if abs(val) >= 1.0 else "저변동예상"
    except Exception:
        return "판단불가"

# ══════════════════════════════════════════════════════════════
# 신호 8: 기업 이벤트 캘린더 (v10.0 Phase 4-1)
# ══════════════════════════════════════════════════════════════

def _analyze_event_impact(event_signals: list[dict], price_data: dict = None) -> list[dict]:
    """
    event_impact_analyzer 결과 → 신호8 signals 변환
    event_scores: ticker → strength (oracle_analyzer 전달용)
    """
    by_sector = price_data.get("by_sector", {}) if price_data else {}
    results   = []

    for ev in event_signals:
        if ev.get("strength", 0) < config.EVENT_SIGNAL_MIN_STRENGTH:
            continue

        ticker    = ev.get("ticker", "")
        corp_name = ev.get("corp_name", "")
        evt_type  = ev.get("event_type", "이벤트")
        days      = ev.get("days_until", -1)
        strength  = ev.get("strength", 3)
        direction = ev.get("impact_direction", "+")
        reason    = ev.get("reason", "")

        # 관련종목: 동적 조회
        related = _get_sector_top_stocks(corp_name, by_sector)
        if ticker and ticker not in related:
            related.insert(0, corp_name)

        results.append({
            "테마명":   f"{corp_name} {evt_type}",
            "발화신호": f"신호8: {evt_type} D-{days} [{corp_name}|{ev.get('event_date', '')}|{reason[:30]}]",
            "강도":     strength,
            "신뢰도":   f"event:{direction}",
            "발화단계": "1일차",
            "상태":     "신규" if direction == "+" else "경고",
            "관련종목": related[:3],
        })

    return results


def _analyze_datalab_trends(datalab: list[dict], price_data: dict = None) -> list[dict]:
    """
    네이버 DataLab 급등 키워드 → 신호2 보완 신호 변환
    ratio >= DATALAB_SPIKE_THRESHOLD 인 키워드만 포함
    """
    by_sector = price_data.get("by_sector", {}) if price_data else {}
    results   = []

    for dl in datalab:
        ratio   = dl.get("ratio", 0.0)
        theme   = dl.get("theme", "")
        keyword = dl.get("keyword", theme)

        if ratio < config.DATALAB_SPIKE_THRESHOLD:
            continue

        # 강도: ratio에 따라 분기
        strength = 4 if ratio >= 2.0 else 3
        related  = _get_sector_top_stocks(theme, by_sector)

        results.append({
            "테마명":   theme,
            "발화신호": f"DataLab: '{keyword}' 검색 {ratio:.1f}배 급등 — 개인 관심 선행 신호",
            "강도":     strength,
            "신뢰도":   f"datalab:{ratio:.1f}x",
            "발화단계": "1일차",
            "상태":     "신규",
            "관련종목": related[:3],
        })

    return results
