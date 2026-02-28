"""
collectors/news_collector.py
증권사 리포트 + 정책·시황 뉴스 + 네이버 DataLab 트렌드 수집 전담

[수정이력]
- v1.0: 네이버 HTML 크롤링 (차단 문제)
- v1.1: 네이버 공식 검색 API 교체
- v1.2: 종목명 파싱 패턴 대폭 개선 ("종목미상" 문제 해결)
- v10.0 Phase 4-1: 네이버 DataLab 검색어 트렌드 API 추가
  _collect_datalab_trends() — 주요 투자 키워드 검색량 급등 감지
  DATALAB_ENABLED=false(기본) — 활성화 시 datalab_trends 반환값에 포함
  NAVER_DATALAB_CLIENT_ID / NAVER_DATALAB_CLIENT_SECRET 환경변수 필요
  (NAVER_CLIENT_ID와 동일 앱키 사용 가능 — DataLab API 권한 필요)
- v11.0: NewsAPI.org 연동 추가
  _collect_newsapi_reports()      — 영문 증권사 리포트·애널리스트 의견
  _collect_newsapi_global_market() — 글로벌 시황·정책 뉴스 (한국 영향권)
  NEWSAPI_ENABLED=true(키 있으면 자동) — 키 없어도 기존 네이버 단독으로 동작
"""

import re
import requests
from datetime import datetime
import config
from utils.logger import logger
from utils.date_utils import get_today, fmt_kr

BLACKLIST_KEYWORDS = ["주가전망", "총정리", "배당금 계산", "tistory"]

BROKERAGES = [
    "키움", "NH투자", "미래에셋", "삼성증권", "한국투자",
    "KB증권", "신한투자", "하나증권", "대신증권", "메리츠",
    "유안타", "교보증권", "IBK투자", "이베스트", "하이투자",
    "현대차증권", "SK증권", "부국증권", "신영증권",
]

ACTIONS = {
    "목표가상향": ["목표가 상향", "목표주가 상향", "TP 상향", "목표가↑", "목표주가↑"],
    "신규매수":   ["신규 매수", "커버리지 개시", "투자의견 매수", "Buy 개시", "신규편입"],
    "매수유지":   ["매수 유지", "Buy 유지", "매수의견 유지", "투자의견 유지"],
    "목표가하향": ["목표가 하향", "목표주가 하향", "TP 하향", "목표가↓"],
    "목표가상향(단순)": ["목표주가"],   # 상향/하향 없이 목표주가만 언급
}


def collect(target_date: datetime = None) -> dict:
    """
    반환: dict
    {
        "reports": [
            {"증권사": str, "종목명": str, "액션": str,
             "내용": str, "신뢰도": str, "출처": str}
        ],
        "policy_news": [
            {"제목": str, "내용": str, "출처": str}
        ],
        "datalab_trends": [
            {"keyword": str, "ratio": float, "theme": str}
        ]
    }
    """
    if target_date is None:
        target_date = get_today()

    date_kr = fmt_kr(target_date)
    logger.info(f"[news] {date_kr} 리포트·정책뉴스 수집 시작")

    if not config.NAVER_CLIENT_ID or not config.NAVER_CLIENT_SECRET:
        logger.warning("[news] 네이버 API 키 없음 — 수집 건너뜀")
        logger.warning("[news] NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수 추가 필요")
        return {"reports": [], "policy_news": [], "datalab_trends": []}

    reports        = _collect_reports(date_kr)
    policy_news    = _collect_policy_news(date_kr)
    datalab_trends = _collect_datalab_trends() if config.DATALAB_ENABLED else []

    # ── v11.0: NewsAPI.org 영문 뉴스 보강 ────────────────────
    if config.NEWSAPI_ENABLED:
        try:
            newsapi_reports = _collect_newsapi_reports()
            reports = reports + newsapi_reports   # 네이버 결과 뒤에 append
            logger.info(f"[news] NewsAPI.org 리포트 {len(newsapi_reports)}건 추가")
        except Exception as e:
            logger.warning(f"[news] NewsAPI.org 리포트 수집 실패 (무시): {e}")
        try:
            newsapi_global = _collect_newsapi_global_market()
            policy_news = policy_news + newsapi_global
            logger.info(f"[news] NewsAPI.org 글로벌 시황 {len(newsapi_global)}건 추가")
        except Exception as e:
            logger.warning(f"[news] NewsAPI.org 글로벌 시황 수집 실패 (무시): {e}")

    logger.info(f"[news] 리포트 {len(reports)}건, 정책뉴스 {len(policy_news)}건, "
                f"DataLab 트렌드 {len(datalab_trends)}건")
    return {"reports": reports, "policy_news": policy_news, "datalab_trends": datalab_trends}


def _naver_news_search(query: str, display: int = 10) -> list[dict]:
    url  = "https://openapi.naver.com/v1/search/news.json"
    hdrs = {
        "X-Naver-Client-Id":     config.NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": config.NAVER_CLIENT_SECRET,
    }
    resp = requests.get(url, headers=hdrs,
                        params={"query": query, "sort": "date", "display": display},
                        timeout=8)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    results = []
    for item in items:
        title = re.sub(r"<[^>]+>", "", item.get("title", ""))
        desc  = re.sub(r"<[^>]+>", "", item.get("description", ""))
        results.append({
            "title":   title,
            "description": desc,
            "link":    item.get("link", ""),
        })
    return results


def _collect_reports(date_kr: str) -> list[dict]:
    query   = f"{date_kr} 증권사 리포트 목표주가 상향 매수"
    results = []

    try:
        articles = _naver_news_search(query, display=20)
        for article in articles:
            title = article["title"]
            desc  = article["description"]
            text  = title + " " + desc

            if any(kw in title for kw in BLACKLIST_KEYWORDS):
                continue

            brokerage = _extract_brokerage(text)
            action    = _extract_action(text)
            stock     = _extract_stock_name(title, text)

            results.append({
                "증권사": brokerage,
                "종목명": stock,
                "액션":   action,
                "내용":   title,
                "신뢰도": "스니펫",
                "출처":   article.get("link", ""),
            })
    except Exception as e:
        logger.warning(f"[news] 리포트 수집 실패: {e}")

    return results[:10]


def _collect_policy_news(date_kr: str) -> list[dict]:
    query   = f"{date_kr} 정부정책 법안 수혜 외국인 기관 수급 장전시황"
    results = []
    try:
        articles = _naver_news_search(query, display=10)
        for article in articles:
            results.append({
                "제목": article["title"],
                "내용": article["description"][:150],
                "출처": article.get("link", ""),
            })
    except Exception as e:
        logger.warning(f"[news] 정책뉴스 수집 실패: {e}")
    return results[:5]


def _extract_brokerage(text: str) -> str:
    for b in BROKERAGES:
        if b in text:
            return b
    return "증권사미상"


def _extract_action(text: str) -> str:
    for action, keywords in ACTIONS.items():
        if any(kw in text for kw in keywords):
            return action
    return "언급"


def _extract_stock_name(title: str, full_text: str) -> str:
    """
    뉴스 제목에서 종목명 추출
    다양한 패턴 순서대로 시도:
    1. "삼성전자, KB증권 목표가..." 형태
    2. "[삼성전자] KB증권..." 형태
    3. "KB증권 삼성전자 목표가..." — 증권사 뒤에 오는 종목명
    4. 종목명으로 보이는 한글 2~5글자
    """
    # 패턴 1: 맨 앞 한글/영문 이름 (쉼표 또는 공백 전)
    m = re.match(r"^([가-힣A-Za-z0-9·&]{2,10})[,\s]", title)
    if m:
        candidate = m.group(1)
        # 증권사 이름이면 제외
        if not any(b in candidate for b in BROKERAGES):
            return candidate

    # 패턴 2: 대괄호 안 종목명 [삼성전자]
    m = re.search(r"\[([가-힣A-Za-z0-9]{2,10})\]", title)
    if m:
        return m.group(1)

    # 패턴 3: 증권사명 뒤에 오는 한글 단어
    for b in BROKERAGES:
        if b in title:
            after = title.split(b)[-1]
            m = re.search(r"([가-힣]{2,8})", after)
            if m:
                candidate = m.group(1)
                # "목표", "주가", "리포트" 같은 일반 단어 제외
                skip_words = ["목표", "주가", "리포트", "매수", "분석", "투자", "상향", "하향"]
                if candidate not in skip_words:
                    return candidate

    # 패턴 4: 전체 텍스트에서 종목코드 패턴 근처 한글
    m = re.search(r"([가-힣]{2,8})\s*\(?\d{6}\)?", full_text)
    if m:
        return m.group(1)

    return "종목미상"


# ─────────────────────────────────────────────────────────────
# v10.0 Phase 4-1: 네이버 DataLab 검색어 트렌드 수집
# ─────────────────────────────────────────────────────────────

# DataLab 조회할 키워드 그룹 (테마명 → 검색 키워드 목록)
_DATALAB_KEYWORD_GROUPS = [
    {"theme": "반도체",     "keywords": ["반도체", "HBM", "AI칩"]},
    {"theme": "방산",       "keywords": ["방산주", "한화에어로", "LIG넥스원"]},
    {"theme": "철강",       "keywords": ["현대제철", "철강주", "포스코"]},
    {"theme": "바이오",     "keywords": ["바이오주", "신약", "임상시험"]},
    {"theme": "2차전지",    "keywords": ["2차전지", "배터리주", "에코프로"]},
    {"theme": "AI",        "keywords": ["AI주", "인공지능주", "엔비디아"]},
    {"theme": "조선",       "keywords": ["조선주", "HD현대", "한국조선해양"]},
    {"theme": "에너지",     "keywords": ["에너지주", "태양광", "풍력"]},
]

_DATALAB_URL = "https://openapi.naver.com/v1/datalab/search"


def _collect_datalab_trends() -> list[dict]:
    """
    네이버 DataLab 검색어 트렌드 수집
    반환: list[dict] — keyword / ratio / theme
    실패 시 빈 리스트 반환 (비치명적)

    [rule #90 계열] 수집만 담당 — 분석·발송 금지
    [DataLab API] 네이버 검색어 트렌드 API (별도 DataLab 권한 필요)
    NAVER_DATALAB_CLIENT_ID / NAVER_DATALAB_CLIENT_SECRET 사용
    (없으면 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 폴백)
    """
    client_id     = getattr(config, "NAVER_DATALAB_CLIENT_ID",     None) or config.NAVER_CLIENT_ID
    client_secret = getattr(config, "NAVER_DATALAB_CLIENT_SECRET", None) or config.NAVER_CLIENT_SECRET

    if not client_id or not client_secret:
        logger.warning("[news/datalab] DataLab API 키 없음 — 트렌드 수집 건너뜀")
        return []

    from datetime import date, timedelta
    import json

    today    = date.today()
    end_date = today.strftime("%Y-%m-%d")
    bgn_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")

    results: list[dict] = []

    for group in _DATALAB_KEYWORD_GROUPS:
        try:
            payload = {
                "startDate": bgn_date,
                "endDate":   end_date,
                "timeUnit":  "date",
                "keywordGroups": [
                    {
                        "groupName": group["theme"],
                        "keywords":  group["keywords"],
                    }
                ],
            }
            hdrs = {
                "X-Naver-Client-Id":     client_id,
                "X-Naver-Client-Secret": client_secret,
                "Content-Type":          "application/json",
            }
            resp = requests.post(_DATALAB_URL, headers=hdrs, data=json.dumps(payload), timeout=8)
            resp.raise_for_status()
            data = resp.json()

            results_list = data.get("results", [])
            for r in results_list:
                data_pts = r.get("data", [])
                if not data_pts:
                    continue
                # 최근 3일 평균 vs 7일 평균으로 급등 여부 계산
                ratios = [pt.get("ratio", 0) for pt in data_pts]
                recent_avg = sum(ratios[-3:]) / 3 if len(ratios) >= 3 else 0
                total_avg  = sum(ratios) / len(ratios) if ratios else 0
                if total_avg > 0:
                    spike_ratio = recent_avg / total_avg
                    if spike_ratio >= config.DATALAB_SPIKE_THRESHOLD:
                        results.append({
                            "keyword":     r.get("title", group["theme"]),
                            "ratio":       round(spike_ratio, 2),
                            "theme":       group["theme"],
                        })

        except Exception as e:
            logger.warning(f"[news/datalab] {group['theme']} 트렌드 수집 실패: {e}")

    # 급등 비율 내림차순 정렬
    results.sort(key=lambda x: x["ratio"], reverse=True)
    return results[:10]


# ─────────────────────────────────────────────────────────────
# v11.0: NewsAPI.org 연동
# https://newsapi.org/v2/everything
# 무료 플랜: 100req/day, 영문 기사, 최근 1개월
# ─────────────────────────────────────────────────────────────

_NEWSAPI_BASE = "https://newsapi.org/v2/everything"

# 증권사 리포트·애널리스트 의견 관련 쿼리
_NEWSAPI_REPORT_QUERIES = [
    "Korea stock analyst target price upgrade",
    "KOSPI KOSDAQ buy rating brokerage",
    "Samsung SK Hynix LG analyst report",
]

# 글로벌 시황·정책 뉴스 — 한국 주식시장 영향권
_NEWSAPI_MARKET_QUERIES = [
    "Korea tariff trade US China",
    "Korea defense semiconductor export",
    "Fed interest rate FOMC Korea market",
    "China stimulus economy Korea",
]

# 신뢰할 수 있는 금융·뉴스 도메인 우선
# [v12.0] domains 파라미터는 _newsapi_search에서 제거 (무료 플랜 미지원 → 0건 버그)
# 아래 상수는 참고용으로만 유지
_NEWSAPI_PREFERRED_DOMAINS = (
    "reuters.com,bloomberg.com,ft.com,wsj.com,"
    "cnbc.com,marketwatch.com,investing.com"
)


def _newsapi_search(query: str, page_size: int = 5) -> list[dict]:
    """
    NewsAPI.org /v2/everything 호출.
    rule #90 계열: 수집·파싱만. 분석 없음.

    [v12.0] domains 파라미터 제거 — 무료 플랜(Developer) 미지원으로 0건 버그 수정.
    """
    from datetime import date, timedelta
    params = {
        "apiKey":     config.NEWSAPI_ORG_KEY,
        "q":          query,
        "language":   "en",
        "sortBy":     "publishedAt",       # 실시간 최신순
        "pageSize":   page_size,
        "from":       (date.today() - timedelta(days=2)).isoformat(),  # 최근 2일
        # domains 파라미터 제거 — 무료 플랜 미지원 (0건 버그)
    }
    resp = requests.get(_NEWSAPI_BASE, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "ok":
        raise RuntimeError(f"NewsAPI.org 오류: {data.get('message', data)}")

    return data.get("articles", [])


def _collect_newsapi_reports() -> list[dict]:
    """
    NewsAPI.org 기반 영문 증권사 리포트·애널리스트 의견 수집.
    반환 형식은 기존 _collect_reports()와 동일 — morning_report.py에서 통합 사용.
    """
    results = []
    for query in _NEWSAPI_REPORT_QUERIES:
        try:
            articles = _newsapi_search(query, page_size=3)
            for art in articles:
                title   = art.get("title", "") or ""
                desc    = art.get("description", "") or ""
                source  = art.get("source", {}).get("name", "NewsAPI")
                url     = art.get("url", "")
                text    = title + " " + desc

                results.append({
                    "증권사": source,
                    "종목명": _extract_english_stock(title),
                    "액션":   _extract_english_action(text),
                    "내용":   title[:120],
                    "신뢰도": "NewsAPI.org",
                    "출처":   url,
                })
        except Exception as e:
            logger.warning(f"[news/newsapi] 리포트 쿼리 실패 '{query}': {e}")

    # 중복 제목 제거
    seen = set()
    unique = []
    for r in results:
        key = r["내용"][:60]
        if key not in seen:
            seen.add(key)
            unique.append(r)

    return unique[:10]


def _collect_newsapi_global_market() -> list[dict]:
    """
    NewsAPI.org 기반 글로벌 시황·정책 뉴스 수집.
    반환 형식은 기존 _collect_policy_news()와 동일.
    """
    results = []
    for query in _NEWSAPI_MARKET_QUERIES:
        try:
            articles = _newsapi_search(query, page_size=3)
            for art in articles:
                title   = art.get("title", "") or ""
                desc    = art.get("description", "") or ""
                source  = art.get("source", {}).get("name", "NewsAPI")
                url     = art.get("url", "")
                pub     = art.get("publishedAt", "")[:16].replace("T", " ")  # 2025-01-01 09:30

                results.append({
                    "제목": f"[{source}] {title[:100]}",
                    "내용": (desc or title)[:150],
                    "출처": url,
                    "발행": pub,
                    "신뢰도": "NewsAPI.org",
                })
        except Exception as e:
            logger.warning(f"[news/newsapi] 글로벌 시황 쿼리 실패 '{query}': {e}")

    # 중복 제거 + 최신순 정렬
    seen = set()
    unique = []
    for r in sorted(results, key=lambda x: x.get("발행", ""), reverse=True):
        key = r["제목"][:60]
        if key not in seen:
            seen.add(key)
            unique.append(r)

    return unique[:8]


def _extract_english_stock(title: str) -> str:
    """영문 기사 제목에서 종목명 추출 — 대문자 회사명 패턴."""
    import re
    # "Samsung Electronics", "SK Hynix", "LG Energy" 같은 패턴
    m = re.search(r"\b(Samsung|SK Hynix|LG|Hyundai|POSCO|Kakao|Naver|Kia|Lotte)\b[^\s,]* ?[A-Z][a-z]*", title)
    if m:
        return m.group(0).strip()
    # 단독 대문자 약어 (KOSPI, KOSDAQ 제외)
    m = re.search(r"\b([A-Z]{2,6})\b(?! ?(?:ETF|Index|KOSPI|KOSDAQ|FOMC|Fed|GDP|CPI|USD|EUR))", title)
    if m:
        return m.group(1)
    return "글로벌종목"


def _extract_english_action(text: str) -> str:
    """영문 기사에서 애널리스트 액션 추출."""
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

