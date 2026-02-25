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
"""

import config
from utils.logger import logger


def analyze(
    dart_data:   list[dict],
    market_data: dict,
    news_data:   dict,
    price_data:  dict = None,
) -> dict:
    """
    신호 1~5 통합 분석
    반환: dict {signals, market_summary, commodities, volatility,
                report_picks, policy_summary}
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

    signals.sort(key=lambda x: x["강도"], reverse=True)
    logger.info(f"[signal] 총 {len(signals)}개 신호 감지")

    volatility = _judge_volatility(market_summary, price_data)

    return {
        "signals":        signals,
        "market_summary": market_summary,
        "commodities":    commodities,
        "volatility":     volatility,
        "report_picks":   reports[:5],
        "policy_summary": policy[:3],
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