"""
analyzers/signal_analyzer.py
v6.0 프롬프트 신호 1~5 통합 판단 전담
- dart, market, news 수집 결과를 받아 신호별로 정리
- 테마 발화 가능성 평가표 생성
- 수집/발송 로직 없음
"""

from utils.logger import logger


def analyze(
    dart_data:   list[dict],
    market_data: dict,
    news_data:   dict,
    theme_data:  dict = None,  # closing_report에서 넘어오는 전날 테마 (없으면 None)
) -> dict:
    """
    신호 1~5 통합 분석
    반환: dict
    {
        "signals": [
            {
                "테마명":     str,
                "발화신호":   str,
                "강도":       int,   # 1~5
                "신뢰도":     str,
                "발화단계":   str,
                "상태":       str,   # "신규", "진행", "모니터"
            }
        ],
        "market_summary": dict,    # 미국증시 요약
        "volatility":     str,     # "고변동" or "저변동"
        "report_picks":   list,    # 리포트 상위 종목
        "policy_summary": list,    # 정책 뉴스 요약
    }
    """
    logger.info("[signal] 신호 1~5 분석 시작")

    signals       = []
    market_summary = market_data.get("us_market", {})
    commodities   = market_data.get("commodities", {})

    # ── 신호 1: DART 공시 ────────────────────────────────
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

    # ── 신호 2: 미국증시 + 원자재 ────────────────────────
    us_signals = _analyze_us_market(market_summary, commodities)
    signals.extend(us_signals)

    # ── 신호 3: 증권사 리포트 ────────────────────────────
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

    # ── 신호 5: 정책·시황 ────────────────────────────────
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

    return {
        "signals":        signals,
        "market_summary": market_summary,
        "commodities":    commodities,
        "volatility":     _judge_volatility(market_summary),
        "report_picks":   reports[:5],
        "policy_summary": policy[:3],
    }


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
    """공시 종류 -> 테마명 변환"""
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


def _analyze_us_market(us: dict, commodities: dict) -> list[dict]:
    """미국증시·원자재 -> 국내 연동 테마 시그널"""
    signals = []

    # 구리 강세 -> 전선주
    copper = commodities.get("copper", {})
    if _is_positive(copper.get("change", "N/A")):
        signals.append({
            "테마명":   "전선/구리",
            "발화신호": f"신호2: 구리 {copper['change']} [LME|전날]",
            "강도":     3,
            "신뢰도":   copper["신뢰도"],
            "발화단계": "불명",
            "상태":     "모니터",
            "관련종목": ["LS전선", "대원전선", "가온전선"],
        })

    # 은 강세 -> 귀금속/태양광
    silver = commodities.get("silver", {})
    if _is_positive(silver.get("change", "N/A")):
        signals.append({
            "테마명":   "귀금속/태양광",
            "발화신호": f"신호2: 은 {silver['change']} [COMEX|전날]",
            "강도":     2,
            "신뢰도":   silver["신뢰도"],
            "발화단계": "불명",
            "상태":     "모니터",
            "관련종목": [],
        })

    return signals


def _is_positive(change_str: str) -> bool:
    """등락률 문자열이 양수인지 확인"""
    if change_str in ("N/A", "", None):
        return False
    return change_str.startswith("+") or (
        not change_str.startswith("-") and
        any(c.isdigit() for c in change_str)
    )


def _judge_volatility(us: dict) -> str:
    """
    미국증시 기준 고변동/저변동 판단
    실제 코스피/코스닥 등락률은 마감봇에서 판단
    아침봇에서는 미국 시황 기준으로 예상
    """
    nasdaq = us.get("nasdaq", "N/A")
    if nasdaq == "N/A":
        return "판단불가"
    try:
        val = float(nasdaq.replace("%", "").replace("+", ""))
        return "고변동예상" if abs(val) >= 1.0 else "저변동예상"
    except Exception:
        return "판단불가"
