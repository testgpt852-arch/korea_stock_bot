"""
collectors/geopolitics_collector.py
지정학·정책 이벤트 수집 전담 — RSS 파싱 + NewsAPI.org

[ARCHITECTURE rule #90 — 절대 금지]
- AI 분석 호출 금지 (geopolitics_analyzer.py에서 처리)
- 텔레그램 발송 금지
- DB 기록 금지
- 비치명적 처리: 소스 실패 시 빈 리스트 반환 (아침봇 blocking 금지)

[v10.0 Phase 2 신규]
수집 대상:
  - Reuters RSS (한국 관련 필터)
  - Bloomberg RSS (무료 티어)
  - 기재부/방사청 보도자료 RSS
  - Google News RSS (API키 없이 무료)

[v11.0 업그레이드]
  - NewsAPI.org 실시간 영문 뉴스 추가 (NEWSAPI_ENABLED=true 시 자동 활성)
  - RSS보다 실시간성 우수, 주요 금융매체(Reuters·Bloomberg·FT) 직접 수집
  - 쿼리: 한반도·관세·방산·반도체·중국·NATO 등 지정학 키워드

스케줄 (main.py):
  - 06:00 아침봇 전 수집 (아침봇 08:30 컨텍스트 제공)
  - 장중 GEOPOLITICS_POLL_MIN 분 간격 (긴급 이벤트 대응)

출력 형식 (raw — 분석 전):
  list[dict] = [
    {
      "title":      str,   # 뉴스 제목 (원문)
      "summary":    str,   # 본문 요약 (feedparser summary, 없으면 "")
      "link":       str,   # 기사 URL
      "published":  str,   # 발행 시각 (ISO 형식)
      "source":     str,   # 소스명 (reuters / bloomberg / moef / dapa / google / newsapi)
      "raw_text":   str,   # title + summary 합산 (키워드 매칭용)
    }
  ]
"""

import feedparser
import requests
import config
from utils.logger import logger
from datetime import datetime, timezone


# ── RSS 피드 소스 목록 ──────────────────────────────────────
# rule #90: 수집 URL만 정의. 분석·발송·DB 로직 없음.
# [v12.0] Reuters RSS 완전 폐지됨 → 제거. NewsAPI.org로 대체.
#         기재부/방사청은 비표준 XML → requests로 직접 fetch 후 feedparser 처리.
_RSS_SOURCES = [
    {
        "name": "moef",   # 기획재정부 보도자료
        "url":  "https://www.moef.go.kr/sty/rss/moefRss.do",
        "filter_keywords": [],   # 전체 수집 (기재부 = 한국 정책 전용)
    },
    {
        "name": "dapa",   # 방위사업청 보도자료
        "url":  "https://www.dapa.go.kr/dapa/rss/rssService.do",
        "filter_keywords": [],   # 전체 수집
    },
]

# Google News RSS (키워드 기반, API키 없이 무료 사용)
_GOOGLE_NEWS_QUERIES = [
    "NATO 방위비 한국",
    "트럼프 관세 한국 수출",
    "중국 부양책 철강",
    "한국 반도체 수출규제",
]


def collect(max_per_source: int = 20) -> list[dict]:
    """
    모든 RSS 소스에서 뉴스를 수집하여 raw 목록 반환.

    rule #90: 수집·파싱만 수행. 분석·AI 호출·발송·DB 기록 없음.
    소스 실패 시 해당 소스만 건너뜀 (비치명적).

    Args:
        max_per_source: 소스당 최대 수집 건수 (기본 20)

    Returns:
        list[dict] — raw 뉴스 항목 (geopolitics_analyzer.analyze()에 전달)
    """
    if not config.GEOPOLITICS_ENABLED:
        logger.info("[geopolitics_collector] GEOPOLITICS_ENABLED=false — 수집 건너뜀")
        return []

    results: list[dict] = []

    for source in _RSS_SOURCES:
        try:
            items = _fetch_rss(
                name=source["name"],
                url=source["url"],
                filter_keywords=source["filter_keywords"],
                max_items=max_per_source,
            )
            results.extend(items)
            logger.info(f"[geopolitics_collector] {source['name']}: {len(items)}건 수집")
        except Exception as e:
            # rule #90: 비치명적 — 소스 실패해도 계속 진행
            logger.warning(f"[geopolitics_collector] {source['name']} 실패 (무시): {e}")

    # Google News RSS (선택적, API키 없이도 동작)
    google_items = _fetch_google_news_rss(max_per_query=5)
    results.extend(google_items)

    # ── v11.0: NewsAPI.org 실시간 지정학 뉴스 ────────────────
    if config.NEWSAPI_ENABLED:
        try:
            newsapi_items = _fetch_newsapi_geopolitics(max_per_query=5)
            results.extend(newsapi_items)
            logger.info(f"[geopolitics_collector] NewsAPI.org: {len(newsapi_items)}건 수집")
        except Exception as e:
            logger.warning(f"[geopolitics_collector] NewsAPI.org 실패 (무시): {e}")

    # 중복 URL 제거
    seen_links = set()
    unique = []
    for item in results:
        link = item.get("link", "")
        if link not in seen_links:
            seen_links.add(link)
            unique.append(item)

    logger.info(f"[geopolitics_collector] 총 {len(unique)}건 수집 완료 (중복 제거 후)")
    return unique


def _fetch_rss(
    name: str,
    url: str,
    filter_keywords: list[str],
    max_items: int,
) -> list[dict]:
    """
    단일 RSS 피드를 파싱하여 필터링된 아이템 목록 반환.

    [v12.0] requests로 직접 fetch 후 feedparser에 text 전달.
    기재부/방사청처럼 비표준 XML(bozo)이어도 entries가 있으면 수집.
    rule #90: 수집·파싱만. 분석 없음.
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; KoreaStockBot/1.0)"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        raw_content = resp.content   # bytes 전달 — feedparser가 인코딩 자동 감지
    except Exception as e:
        logger.warning(f"[geopolitics_collector] {name} HTTP 요청 실패: {e}")
        return []

    feed = feedparser.parse(raw_content)

    # bozo여도 entries가 있으면 계속 진행 (기재부/방사청 비표준 XML 대응)
    if not feed.entries:
        if feed.bozo:
            logger.warning(f"[geopolitics_collector] {name} RSS 파싱 오류 + entries 없음: {feed.bozo_exception}")
        else:
            logger.debug(f"[geopolitics_collector] {name} entries 없음")
        return []

    if feed.bozo:
        logger.debug(f"[geopolitics_collector] {name} bozo=True이나 entries={len(feed.entries)}건 수집 진행")

    items = []
    for entry in feed.entries[:max_items * 2]:   # 필터 후 max_items 맞추기 위해 여유분
        title   = getattr(entry, "title",   "") or ""
        summary = getattr(entry, "summary", "") or ""
        link    = getattr(entry, "link",    "") or ""
        raw_text = (title + " " + summary).lower()

        # 필터 키워드가 있으면 하나라도 포함 시만 수집
        if filter_keywords:
            if not any(kw.lower() in raw_text for kw in filter_keywords):
                continue

        published = _parse_published(entry)

        items.append({
            "title":     title,
            "summary":   summary[:500],   # 500자 제한
            "link":      link,
            "published": published,
            "source":    name,
            "raw_text":  raw_text,
        })

        if len(items) >= max_items:
            break

    return items


def _fetch_google_news_rss(max_per_query: int = 5) -> list[dict]:
    """
    Google News RSS (쿼리 기반) — API키 없이 무료 사용 가능.
    rule #90: 수집만 수행.
    """
    items = []
    base_url = "https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"

    for query in _GOOGLE_NEWS_QUERIES:
        try:
            import urllib.parse
            url  = base_url.format(query=urllib.parse.quote(query))
            feed = feedparser.parse(url)

            for entry in feed.entries[:max_per_query]:
                title   = getattr(entry, "title",   "") or ""
                link    = getattr(entry, "link",    "") or ""
                raw_text = title.lower()

                items.append({
                    "title":     title,
                    "summary":   "",
                    "link":      link,
                    "published": _parse_published(entry),
                    "source":    "google_news",
                    "raw_text":  raw_text,
                })
        except Exception as e:
            logger.warning(f"[geopolitics_collector] Google News '{query}' 실패 (무시): {e}")

    return items


def _parse_published(entry) -> str:
    """feedparser entry의 발행 시각을 ISO 문자열로 변환"""
    import time
    try:
        t = entry.get("published_parsed") or entry.get("updated_parsed")
        if t:
            dt = datetime(*t[:6], tzinfo=timezone.utc)
            return dt.isoformat()
    except Exception:
        pass
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────
# v11.0: NewsAPI.org 실시간 지정학 뉴스 수집
# v12.0: domains 파라미터 제거 (무료 플랜 미지원 → 0건 버그 수정)
#        Reuters RSS 폐지 대체 쿼리 대폭 확장
# ─────────────────────────────────────────────────────────────

_NEWSAPI_BASE = "https://newsapi.org/v2/everything"

# 지정학·글로벌 매크로 쿼리 목록 (한국 주식 영향권 핵심 키워드)
# [v12.0] Reuters 대체 쿼리 추가 — Reuters/Bloomberg 기사를 NewsAPI로 직접 수집
_NEWSAPI_GEO_QUERIES = [
    "South Korea tariff trade war US",
    "Korea defense NATO military",
    "Korea semiconductor export restriction China",
    "China economic stimulus steel battery",
    "North Korea military provocation",
    "Trump Korea trade policy",
    "Fed FOMC rate decision emerging markets",
    # v12.0 추가 — Reuters RSS 대체
    "Korea stock market KOSPI outlook",
    "Korea steel shipbuilding defense export",
    "Korea US tariff trade Bloomberg Reuters",
]


def _fetch_newsapi_geopolitics(max_per_query: int = 5) -> list[dict]:
    """
    NewsAPI.org /v2/everything 로 지정학 뉴스 수집.
    rule #90: 수집·파싱만. 분석·AI 호출·발송·DB 기록 없음.

    [v12.0] domains 파라미터 제거 — 무료 플랜(Developer)에서 미지원으로 0건 버그 수정.
    반환 형식은 _fetch_rss()와 동일 — geopolitics_analyzer.analyze()에 직접 전달.
    """
    from datetime import date, timedelta

    items: list[dict] = []

    for query in _NEWSAPI_GEO_QUERIES:
        try:
            params = {
                "apiKey":   config.NEWSAPI_ORG_KEY,
                "q":        query,
                "language": "en",
                "sortBy":   "publishedAt",          # 실시간 최신순
                "pageSize": max_per_query,
                "from":     (date.today() - timedelta(days=1)).isoformat(),  # 최근 24시간
                # domains 파라미터 제거 — 무료 플랜 미지원 (0건 버그)
            }
            resp = requests.get(_NEWSAPI_BASE, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") != "ok":
                logger.warning(f"[geopolitics/newsapi] API 오류 '{query}': {data.get('message')}")
                continue

            for art in data.get("articles", []):
                title   = art.get("title", "")   or ""
                desc    = art.get("description", "") or ""
                url     = art.get("url", "")
                pub     = art.get("publishedAt", "") or datetime.now(timezone.utc).isoformat()
                source  = art.get("source", {}).get("name", "newsapi")
                raw_text = (title + " " + desc).lower()

                items.append({
                    "title":     title,
                    "summary":   desc[:500],
                    "link":      url,
                    "published": pub,
                    "source":    f"newsapi_{source.lower().replace(' ', '_')}",
                    "raw_text":  raw_text,
                })

        except Exception as e:
            # rule #90: 비치명적 — 쿼리 단위 실패는 무시
            logger.warning(f"[geopolitics/newsapi] 쿼리 실패 '{query}' (무시): {e}")

    # 중복 URL 제거
    seen_links: set[str] = set()
    unique: list[dict] = []
    for item in items:
        link = item.get("link", "")
        if link and link not in seen_links:
            seen_links.add(link)
            unique.append(item)

    return unique

