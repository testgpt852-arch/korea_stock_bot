"""
collectors/geopolitics_collector.py
지정학·정책 이벤트 수집 전담 — RSS 파싱 + URL 수집만 담당

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
  - Google News RSS (GOOGLE_NEWS_API_KEY 없어도 RSS는 무료)

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
      "source":     str,   # 소스명 (reuters / bloomberg / moef / dapa / google)
      "raw_text":   str,   # title + summary 합산 (키워드 매칭용)
    }
  ]
"""

import feedparser
import config
from utils.logger import logger
from datetime import datetime, timezone


# ── RSS 피드 소스 목록 ──────────────────────────────────────
# rule #90: 수집 URL만 정의. 분석·발송·DB 로직 없음.
_RSS_SOURCES = [
    {
        "name": "reuters_korea",
        "url":  "https://feeds.reuters.com/reuters/businessNews",
        "filter_keywords": ["korea", "한국", "steel", "defense", "tariff",
                            "semiconductor", "battery", "nato", "china"],
    },
    {
        "name": "reuters_world",
        "url":  "https://feeds.reuters.com/reuters/worldNews",
        "filter_keywords": ["korea", "nato", "defense", "steel", "tariff",
                            "china", "russia", "ukraine"],
    },
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
    "한국 철강 관세 site:reuters.com OR site:bloomberg.com",
    "NATO 방위비 한국",
    "트럼프 관세 한국 수출",
    "중국 부양책 철강",
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

    rule #90: 수집·파싱만. 분석 없음.
    """
    feed = feedparser.parse(url)

    if feed.bozo and not feed.entries:
        logger.warning(f"[geopolitics_collector] {name} RSS 파싱 오류: {feed.bozo_exception}")
        return []

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
