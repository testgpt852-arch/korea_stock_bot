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
    import re
    m = re.search(
        r"\b(Samsung|SK Hynix|LG|Hyundai|POSCO|Kakao|Naver|Kia|Lotte)\b[^\s,]* ?[A-Z][a-z]*",
        title,
    )
    if m:
        return m.group(0).strip()
    m = re.search(
        r"\b([A-Z]{2,6})\b(?! ?(?:ETF|Index|KOSPI|KOSDAQ|FOMC|Fed|GDP|CPI|USD|EUR))",
        title,
    )
    if m:
        return m.group(1)
    return "글로벌종목"


def _extract_english_action(text: str) -> str:
    text_lower = text.lower()
    if any(kw in text_lower for kw in ["upgrade", "raised target", "price target raise", "buy rating"]):
        return "목표가상향"
    if any(kw in text_lower for kw in ["initiate", "initiates coverage", "new buy", "outperform"]):
        return "신규매수"
    if any(kw in text_lower for kw in ["downgrade", "sell", "underperform", "lower target"]):
        return "목표가하향"
    if any(kw in text_lower for kw in ["maintain", "reiterate", "hold rating"]):
        return "매수유지"
    return "언급"
