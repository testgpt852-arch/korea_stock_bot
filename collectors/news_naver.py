"""
collectors/news_naver.py  [v12.0]
네이버 API 기반 증권사 리포트 + 정책뉴스 + DataLab 트렌드 수집 전담

[분리 경위]
v12.0: news_collector.py를 3개로 분리 (Naver / NewsAPI / GlobalRSS)
  - 이 파일: NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 키 필요
  - 키 없으면 이 파일만 비활성 (다른 2개는 독립 동작)

[반환값]
collect() → dict:
  {
    "reports":        list[dict],   # 증권사 리포트
    "policy_news":    list[dict],   # 정책·시황 뉴스
    "datalab_trends": list[dict],   # DataLab 검색어 트렌드 (DATALAB_ENABLED=true 시)
  }

[수정이력]
- v11.0 (news_collector.py): 네이버 DataLab 트렌드 API 추가
- v12.0: news_collector.py에서 분리 신규 파일 생성
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
    "목표가상향(단순)": ["목표주가"],
}


def collect(target_date: datetime = None) -> dict:
    """
    네이버 API 기반 리포트·정책뉴스·DataLab 트렌드 수집.
    NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 없으면 빈 결과 반환.
    """
    if target_date is None:
        target_date = get_today()

    date_kr = fmt_kr(target_date)
    logger.info(f"[news_naver] {date_kr} 리포트·정책뉴스 수집 시작")

    if not config.NAVER_CLIENT_ID or not config.NAVER_CLIENT_SECRET:
        logger.warning("[news_naver] 네이버 API 키 없음 — 수집 건너뜀")
        return {"reports": [], "policy_news": [], "datalab_trends": []}

    reports        = _collect_reports(date_kr)
    policy_news    = _collect_policy_news(date_kr)
    datalab_trends = _collect_datalab_trends() if config.DATALAB_ENABLED else []

    logger.info(
        f"[news_naver] 리포트 {len(reports)}건, "
        f"정책뉴스 {len(policy_news)}건, "
        f"DataLab 트렌드 {len(datalab_trends)}건"
    )
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
            "title":       title,
            "description": desc,
            "link":        item.get("link", ""),
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

            results.append({
                "증권사": _extract_brokerage(text),
                "종목명": _extract_stock_name(title, text),
                "액션":   _extract_action(text),
                "내용":   title,
                "신뢰도": "스니펫",
                "출처":   article.get("link", ""),
            })
    except Exception as e:
        logger.warning(f"[news_naver] 리포트 수집 실패: {e}")
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
        logger.warning(f"[news_naver] 정책뉴스 수집 실패: {e}")
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
    m = re.match(r"^([가-힣A-Za-z0-9·&]{2,10})[,\s]", title)
    if m:
        candidate = m.group(1)
        if not any(b in candidate for b in BROKERAGES):
            return candidate

    m = re.search(r"\[([가-힣A-Za-z0-9]{2,10})\]", title)
    if m:
        return m.group(1)

    for b in BROKERAGES:
        if b in title:
            after = title.split(b)[-1]
            m = re.search(r"([가-힣]{2,8})", after)
            if m:
                candidate = m.group(1)
                skip_words = ["목표", "주가", "리포트", "매수", "분석", "투자", "상향", "하향"]
                if candidate not in skip_words:
                    return candidate

    m = re.search(r"([가-힣]{2,8})\s*\(?\d{6}\)?", full_text)
    if m:
        return m.group(1)

    return "종목미상"


# ─────────────────────────────────────────────────────────────
# 네이버 DataLab 검색어 트렌드 수집
# ─────────────────────────────────────────────────────────────

_DATALAB_KEYWORD_GROUPS = [
    {"theme": "반도체",  "keywords": ["반도체", "HBM", "AI칩"]},
    {"theme": "방산",    "keywords": ["방산주", "한화에어로", "LIG넥스원"]},
    {"theme": "철강",    "keywords": ["현대제철", "철강주", "포스코"]},
    {"theme": "바이오",  "keywords": ["바이오주", "신약", "임상시험"]},
    {"theme": "2차전지", "keywords": ["2차전지", "배터리주", "에코프로"]},
    {"theme": "AI",      "keywords": ["AI주", "인공지능주", "엔비디아"]},
    {"theme": "조선",    "keywords": ["조선주", "HD현대", "한국조선해양"]},
    {"theme": "에너지",  "keywords": ["에너지주", "태양광", "풍력"]},
]

_DATALAB_URL = "https://openapi.naver.com/v1/datalab/search"


def _collect_datalab_trends() -> list[dict]:
    """네이버 DataLab 검색어 트렌드 수집 (DATALAB_ENABLED=true 시)"""
    client_id     = getattr(config, "NAVER_DATALAB_CLIENT_ID",     None) or config.NAVER_CLIENT_ID
    client_secret = getattr(config, "NAVER_DATALAB_CLIENT_SECRET", None) or config.NAVER_CLIENT_SECRET

    if not client_id or not client_secret:
        logger.warning("[news_naver/datalab] DataLab API 키 없음 — 건너뜀")
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
                "keywordGroups": [{"groupName": group["theme"], "keywords": group["keywords"]}],
            }
            hdrs = {
                "X-Naver-Client-Id":     client_id,
                "X-Naver-Client-Secret": client_secret,
                "Content-Type":          "application/json",
            }
            resp = requests.post(_DATALAB_URL, headers=hdrs, data=json.dumps(payload), timeout=8)
            resp.raise_for_status()
            data = resp.json()

            for r in data.get("results", []):
                data_pts = r.get("data", [])
                if not data_pts:
                    continue
                ratios     = [pt.get("ratio", 0) for pt in data_pts]
                recent_avg = sum(ratios[-3:]) / 3 if len(ratios) >= 3 else 0
                total_avg  = sum(ratios) / len(ratios) if ratios else 0
                if total_avg > 0:
                    spike_ratio = recent_avg / total_avg
                    if spike_ratio >= config.DATALAB_SPIKE_THRESHOLD:
                        results.append({
                            "keyword": r.get("title", group["theme"]),
                            "ratio":   round(spike_ratio, 2),
                            "theme":   group["theme"],
                        })
        except Exception as e:
            logger.warning(f"[news_naver/datalab] {group['theme']} 트렌드 수집 실패: {e}")

    results.sort(key=lambda x: x["ratio"], reverse=True)
    return results[:10]
