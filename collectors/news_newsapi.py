"""
collectors/news_newsapi.py  [v12.0]
NewsAPI.org 기반 영문 증권사 리포트 + 글로벌 시황 뉴스 수집 전담

[분리 경위]
v12.0: news_collector.py를 3개로 분리 (Naver / NewsAPI / GlobalRSS)
  - 이 파일: NEWSAPI_ORG_KEY (NewsAPI.org 키) 필요
  - 키 없으면 이 파일만 비활성 (다른 2개는 독립 동작)
  - 무료 플랜: 100req/day, 영문 기사, 최근 1개월

[반환값]
collect() → dict:
  {
    "reports":     list[dict],   # 영문 증권사 리포트·애널리스트 의견
    "policy_news": list[dict],   # 글로벌 시황·정책 뉴스 (한국 영향권)
  }

[수정이력]
- v11.0 (news_collector.py): NewsAPI.org 연동 추가, domains 파라미터 제거 (0건 버그 수정)
- v12.0: news_collector.py에서 분리 신규 파일 생성
"""

import requests
from datetime import date, timedelta
import config
from utils.logger import logger

_NEWSAPI_BASE = "https://newsapi.org/v2/everything"

_NEWSAPI_REPORT_QUERIES = [
    "Korea stock analyst target price upgrade",
    "KOSPI KOSDAQ buy rating brokerage",
    "Samsung SK Hynix LG analyst report",
]

_NEWSAPI_MARKET_QUERIES = [
    "Korea tariff trade US China",
    "Korea defense semiconductor export",
    "Fed interest rate FOMC Korea market",
    "China stimulus economy Korea",
]


def collect(target_date=None) -> dict:
    """
    NewsAPI.org 기반 영문 리포트 + 글로벌 시황 수집.
    NEWSAPI_ORG_KEY 없거나 NEWSAPI_ENABLED=false 이면 빈 결과 반환.
    """
    if not config.NEWSAPI_ENABLED:
        logger.info("[news_newsapi] NEWSAPI_ENABLED=false — 건너뜀")
        return {"reports": [], "policy_news": []}

    if not config.NEWSAPI_ORG_KEY:
        logger.warning("[news_newsapi] NEWSAPI_ORG_KEY 없음 — 건너뜀")
        return {"reports": [], "policy_news": []}

    reports     = []
    policy_news = []

    try:
        reports = _collect_newsapi_reports()
        logger.info(f"[news_newsapi] 리포트 {len(reports)}건 수집")
    except Exception as e:
        logger.warning(f"[news_newsapi] 리포트 수집 실패 (무시): {e}")

    try:
        policy_news = _collect_newsapi_global_market()
        logger.info(f"[news_newsapi] 글로벌 시황 {len(policy_news)}건 수집")
    except Exception as e:
        logger.warning(f"[news_newsapi] 글로벌 시황 수집 실패 (무시): {e}")

    return {"reports": reports, "policy_news": policy_news}


def _newsapi_search(query: str, page_size: int = 5) -> list[dict]:
    """
    NewsAPI.org /v2/everything 호출.
    [v11.0] domains 파라미터 제거 — 무료 플랜(Developer) 미지원으로 0건 버그 수정.
    """
    params = {
        "apiKey":   config.NEWSAPI_ORG_KEY,
        "q":        query,
        "language": "en",
        "sortBy":   "publishedAt",
        "pageSize": page_size,
        "from":     (date.today() - timedelta(days=2)).isoformat(),
    }
    resp = requests.get(_NEWSAPI_BASE, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"NewsAPI.org 오류: {data.get('message', data)}")
    return data.get("articles", [])


def _collect_newsapi_reports() -> list[dict]:
    results = []
    for query in _NEWSAPI_REPORT_QUERIES:
        try:
            articles = _newsapi_search(query, page_size=3)
            for art in articles:
                title  = art.get("title", "") or ""
                desc   = art.get("description", "") or ""
                source = art.get("source", {}).get("name", "NewsAPI")
                url    = art.get("url", "")
                text   = title + " " + desc
                results.append({
                    "증권사": source,
                    "종목명": _extract_english_stock(title),
                    "액션":   _extract_english_action(text),
                    "내용":   title[:120],
                    "신뢰도": "NewsAPI.org",
                    "출처":   url,
                })
        except Exception as e:
            logger.warning(f"[news_newsapi] 리포트 쿼리 실패 '{query}': {e}")

    seen, unique = set(), []
    for r in results:
        key = r["내용"][:60]
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique[:10]


def _collect_newsapi_global_market() -> list[dict]:
    results = []
    for query in _NEWSAPI_MARKET_QUERIES:
        try:
            articles = _newsapi_search(query, page_size=3)
            for art in articles:
                title  = art.get("title", "") or ""
                desc   = art.get("description", "") or ""
                source = art.get("source", {}).get("name", "NewsAPI")
                url    = art.get("url", "")
                pub    = art.get("publishedAt", "")[:16].replace("T", " ")
                results.append({
                    "제목":   f"[{source}] {title[:100]}",
                    "내용":   (desc or title)[:150],
                    "출처":   url,
                    "발행":   pub,
                    "신뢰도": "NewsAPI.org",
                })
        except Exception as e:
            logger.warning(f"[news_newsapi] 글로벌 시황 쿼리 실패 '{query}': {e}")

    seen, unique = set(), []
    for r in sorted(results, key=lambda x: x.get("발행", ""), reverse=True):
        key = r["제목"][:60]
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique[:8]


def _extract_english_stock(title: str) -> str:
    """
    영문 뉴스 제목에서 종목명 추출.

    [버그수정] 기존 정규식이 "SK Hynix" → "SK" 만 추출하는 문제 수정.
    - 원인: 패턴 뒤에 붙은 [^\\s,]* ?[A-Z][a-z]* 가 소문자로 이어지는 경우 매칭 실패
    - 수정: 회사명 목록을 구체적인 것부터 순서대로 직접 매칭 (더 긴 이름 우선)
    - 예: "SK Hynix"를 "SK"보다 먼저 검사해야 정확한 종목명 추출 가능
    """
    import re

    # 한국 주요 기업 영문명 — 구체적인 이름(긴 것)을 먼저, 짧은 것을 나중에
    # 같은 그룹(SK, Samsung, LG...)은 세부 계열사를 상위에 배치
    _KNOWN_STOCKS = [
        # SK 그룹 — 세부 계열사 먼저
        "SK Hynix", "SK Innovation", "SK Telecom", "SK Biopharmaceuticals",
        "SK Networks", "SK E&S", "SK On",
        # Samsung 그룹
        "Samsung Electronics", "Samsung SDI", "Samsung Biologics",
        "Samsung C&T", "Samsung SDS", "Samsung Life",
        # LG 그룹
        "LG Energy Solution", "LG Electronics", "LG Chem",
        "LG Display", "LG Innotek", "LG Uplus",
        # Hyundai 그룹
        "Hyundai Motor", "Hyundai Steel", "Hyundai Mobis",
        "Hyundai Glovis", "HD Hyundai", "HD Korea Shipbuilding",
        # Hanwha 그룹
        "Hanwha Aerospace", "Hanwha Solutions", "Hanwha Systems", "Hanwha Ocean",
        # 기타 대형주
        "POSCO Holdings", "POSCO",
        "Kakao Corp", "Kakao Bank", "Kakao Games", "Kakao Pay",
        "Naver Corp",
        "Celltrion Healthcare", "Celltrion",
        "LIG Nex1", "Korea Aerospace Industries",
        "Lotte Chemical", "Lotte Shopping",
        "KIA", "Kia",
        # 그룹명 단독 — 반드시 마지막에
        "SK", "Samsung", "LG", "Hyundai", "Kakao", "Naver", "Lotte",
    ]

    title_lower = title.lower()
    for name in _KNOWN_STOCKS:
        if name.lower() in title_lower:
            return name  # 정확한 회사명 반환

    # 알려진 회사명 없을 때 — 대문자 약어(티커) 탐색
    # KOSPI, KOSDAQ, FOMC, Fed 등 시장/지표 약어는 제외
    _EXCLUDE = {"ETF", "INDEX", "KOSPI", "KOSDAQ", "FOMC", "FED",
                "GDP", "CPI", "USD", "EUR", "IPO", "IMF", "WTO"}
    m = re.search(r"\b([A-Z]{2,6})\b", title)
    if m and m.group(1) not in _EXCLUDE:
        return m.group(1)

    return "글로벌종목"


def _extract_english_action(text: str) -> str:
    """
    영문 텍스트에서 투자 액션 추출.

    [버그수정] "maintain buy rating" → "목표가상향" 오분류 수정.
    - 원인: "buy rating" 키워드가 목표가상향 조건에 먼저 걸림
    - 수정: 매수유지(maintain/reiterate) 를 목표가상향보다 먼저 검사
    - 우선순위: 매수유지 > 목표가상향 > 신규매수 > 목표가하향 > 언급
    """
    text_lower = text.lower()

    # 매수유지 — 먼저 체크 (maintain buy rating 오분류 방지)
    if any(kw in text_lower for kw in ["maintain", "reiterate", "hold rating", "reaffirm"]):
        return "매수유지"
    # 목표가상향 — buy rating 은 maintain 없을 때만
    if any(kw in text_lower for kw in ["upgrade", "raised target", "price target raise", "buy rating", "raises target"]):
        return "목표가상향"
    # 신규매수
    if any(kw in text_lower for kw in ["initiate", "initiates coverage", "new buy", "outperform", "overweight"]):
        return "신규매수"
    # 목표가하향
    if any(kw in text_lower for kw in ["downgrade", "sell", "underperform", "lower target", "cuts target"]):
        return "목표가하향"
    return "언급"
