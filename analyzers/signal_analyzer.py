"""
analyzers/signal_analyzer.py
v6.0 프롬프트 신호 1~5 통합 판단 전담
- dart, market, news, price 수집 결과를 받아 신호별로 정리
- 테마 발화 가능성 평가표 생성
- 수집/발송 로직 없음

[수정이력]
- v1.0: 신호 1~3, 5 구현
- v2.1: 신호 4 추가 (전날 급등/상한가 → 순환매 신호)
        미국증시 섹터 연동 신호 추가 (sectors 데이터 활용)
        price_data 파라미터 추가
- v2.2: 전선/구리 하드코딩 종목 → config.COPPER_KR_STOCKS 참조로 변경
        신호4 저변동 스킵 조건 개선:
          기존: 지수 ±1% 미만이면 무조건 스킵
          수정: 실제 상한가·급등 종목이 있으면 지수가 낮아도 계속 진행
               (지수 0% = pykrx 데이터 이슈일 가능성 있으므로)
"""

import config
from utils.logger import logger


def analyze(
    dart_data:   list[dict],
    market_data: dict,
    news_data:   dict,
    price_data:  dict = None,   # v2.1: 전날 가격 데이터 (아침봇에서 직접 주입)
) -> dict:
    """
    신호 1~5 통합 분석
    반환: dict
    {
        "signals": [...],
        "market_summary": dict,
        "commodities":    dict,
        "volatility":     str,
        "report_picks":   list,
        "policy_summary": list,
    }
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
    us_signals = _analyze_us_market(market_summary, commodities)
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

    # ── 신호 4: 전날 급등/상한가 순환매 (v2.1 추가) ──────────
    # price_data가 있을 때만 실행 (아침봇에서 직접 주입)
    # 마감봇 의존 없이 pykrx로 직접 수집한 전날 데이터 활용
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

    # 강도 내림차순 정렬
    signals.sort(key=lambda x: x["강도"], reverse=True)

    logger.info(f"[signal] 총 {len(signals)}개 신호 감지")

    # 저변동 판단 (RULE 4)
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
# 신호 4: 전날 급등/상한가 순환매 분석 (v2.1 신규)
# ══════════════════════════════════════════════════════════════

def _analyze_prev_price(price_data: dict) -> list[dict]:
    """
    전날 상한가·급등 종목 → 순환매 신호 생성
    - 상한가 종목: 강도 5, 대장주 + 소외주 묶음
    - 급등(7%↑) 종목: 강도 3~4 (등락률 기준)

    [v2.2 저변동 스킵 조건 개선]
    기존: 코스피·코스닥 모두 ±1% 미만이면 무조건 스킵 (RULE 4)
    문제: pykrx 단일날짜 조회 버그로 지수 등락률이 0.00% 반환될 때
          상한가 16개·급등 20개 있어도 신호4 전체가 스킵됨
    수정: 상한가·급등 종목이 실제 존재하면 지수 등락률과 무관하게 진행
          → 진짜 저변동(종목도 없는 경우)만 스킵
    """
    signals = []

    kospi_rate  = price_data.get("kospi",  {}).get("change_rate", 0)
    kosdaq_rate = price_data.get("kosdaq", {}).get("change_rate", 0)
    upper_limit = price_data.get("upper_limit", [])
    top_gainers = price_data.get("top_gainers", [])

    # 실제 급등·상한가 종목 존재 여부
    has_movers = bool(upper_limit) or bool(top_gainers)

    # RULE 4: 저변동 장세 판단
    # ─ 지수도 낮고 개별종목도 없으면 → 진짜 저변동, 스킵
    # ─ 지수는 낮지만 개별종목 있으면 → 지수 데이터 이슈 가능성, 계속 진행
    is_low_vol = (abs(kospi_rate) < 1.0) and (abs(kosdaq_rate) < 1.0)

    if is_low_vol and not has_movers:
        logger.info(
            f"[signal] 저변동 장세 감지 — 코스피:{kospi_rate:+.2f}% "
            f"코스닥:{kosdaq_rate:+.2f}% 급등종목 없음 → 신호4 스킵"
        )
        return []

    if is_low_vol and has_movers:
        logger.info(
            f"[signal] 지수 저변동 감지 — 코스피:{kospi_rate:+.2f}% "
            f"코스닥:{kosdaq_rate:+.2f}% — 그러나 상한가:{len(upper_limit)}개 "
            f"급등:{len(top_gainers)}개 존재 → 신호4 진행 (지수 데이터 재확인 권장)"
        )

    # 상한가 그룹: 강도 5
    if upper_limit:
        # 상한가 종목들을 대장(최고등락) + 소외주 구조로 묶음
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

    # 급등 그룹: 상위 3개 테마로 개별 신호 (등락률 기준 강도 차등)
    # 상한가와 중복되는 종목 제외
    upper_names = {s["종목명"] for s in upper_limit}
    gainers_only = [s for s in top_gainers if s["종목명"] not in upper_names]

    # 급등 종목은 시장별로 분리해서 신호 생성 (KOSPI/KOSDAQ)
    for market in ["KOSPI", "KOSDAQ"]:
        market_gainers = [s for s in gainers_only if s.get("시장") == market][:10]
        if not market_gainers:
            continue

        top = market_gainers[0]  # 대장주
        관련종목 = [s["종목명"] for s in market_gainers]

        # 강도: 대장주 등락률 기준
        rate = top["등락률"]
        강도 = 5 if rate >= 20 else 4 if rate >= 10 else 3

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
            "관련종목": 관련종목,
            "ai_memo":  f"전날 대장 {top['종목명']} {rate:+.1f}% — 소외주 오늘 순환매 가능",
        })

    return signals


# ══════════════════════════════════════════════════════════════
# 신호 2: 미국증시·원자재·섹터 분석
# ══════════════════════════════════════════════════════════════

def _analyze_us_market(us: dict, commodities: dict) -> list[dict]:
    """미국증시 섹터 + 원자재 → 국내 연동 테마 시그널"""
    signals = []

    # 섹터 ETF 연동 신호 (v2.1)
    sectors = us.get("sectors", {})
    for sector_name, sector_data in sectors.items():
        change_str = sector_data.get("change", "N/A")
        if change_str == "N/A":
            continue

        try:
            pct = float(change_str.replace("%", "").replace("+", ""))
        except ValueError:
            continue

        # 임계값 이상 변동 시만 신호 발생 (config.US_SECTOR_SIGNAL_MIN)
        if abs(pct) < config.US_SECTOR_SIGNAL_MIN:
            continue

        direction = "강세↑" if pct > 0 else "약세↓"
        강도 = 4 if abs(pct) >= 3.0 else 3 if abs(pct) >= 2.0 else 2
        관련종목 = config.US_SECTOR_KR_MAP.get(sector_name, [])

        signals.append({
            "테마명":   sector_name,
            "발화신호": f"신호2: 미국 {sector_name} {direction} {change_str} [섹터ETF|전날]",
            "강도":     강도 if pct > 0 else 1,   # 약세는 강도 1
            "신뢰도":   sector_data.get("신뢰도", "yfinance"),
            "발화단계": "불명",
            "상태":     "모니터" if pct > 0 else "경고",
            "관련종목": 관련종목,
        })

    # 구리 강세 → 전선주
    # v2.2: 하드코딩 제거 → config.COPPER_KR_STOCKS 참조 (상장사만)
    copper = commodities.get("copper", {})
    if _is_positive(copper.get("change", "N/A")):
        signals.append({
            "테마명":   "전선/구리",
            "발화신호": f"신호2: 구리 {copper['change']} [LME|전날]",
            "강도":     3,
            "신뢰도":   copper.get("신뢰도", "N/A"),
            "발화단계": "불명",
            "상태":     "모니터",
            "관련종목": list(config.COPPER_KR_STOCKS),  # 상장사만, config에서 관리
        })

    # 은 강세 → 귀금속/태양광
    silver = commodities.get("silver", {})
    if _is_positive(silver.get("change", "N/A")):
        signals.append({
            "테마명":   "귀금속/태양광",
            "발화신호": f"신호2: 은 {silver['change']} [COMEX|전날]",
            "강도":     2,
            "신뢰도":   silver.get("신뢰도", "N/A"),
            "발화단계": "불명",
            "상태":     "모니터",
            "관련종목": [],
        })

    return signals


# ══════════════════════════════════════════════════════════════
# 내부 헬퍼
# ══════════════════════════════════════════════════════════════

def _dart_strength(dart: dict) -> int:
    """공시 종류별 강도 (1~5)"""
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
    """공시 종류 → 테마명 변환"""
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
    """등락률 문자열이 양수인지 확인"""
    if change_str in ("N/A", "", None):
        return False
    return change_str.startswith("+") or (
        not change_str.startswith("-") and
        any(c.isdigit() for c in change_str)
    )


def _judge_volatility(us: dict, price_data: dict = None) -> str:
    """
    변동성 판단 (RULE 4 준수)
    - price_data 있으면 실제 코스피/코스닥 등락률 기준 (정확)
    - 없으면 미국 나스닥 기준으로 예상 (아침봇 fallback)
    """
    # 실제 전날 지수 데이터 우선 (신호4에서 주입됨)
    if price_data:
        kospi_rate  = price_data.get("kospi",  {}).get("change_rate", None)
        kosdaq_rate = price_data.get("kosdaq", {}).get("change_rate", None)
        if kospi_rate is not None and kosdaq_rate is not None:
            rate = max(abs(kospi_rate), abs(kosdaq_rate))
            if rate >= 2.0:   return "고변동"
            elif rate >= 1.0: return "중변동"
            else:             return "저변동 (순환매 에너지 낮음)"

    # fallback: 미국 나스닥 기준 예상
    nasdaq = us.get("nasdaq", "N/A")
    if nasdaq == "N/A":
        return "판단불가"
    try:
        val = float(nasdaq.replace("%", "").replace("+", ""))
        return "고변동예상" if abs(val) >= 1.0 else "저변동예상"
    except Exception:
        return "판단불가"