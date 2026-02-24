"""
collectors/news_collector.py
증권사 리포트 + 정책·시황 뉴스 수집 전담

[수정이력]
- v1.0: 네이버 HTML 크롤링 (차단 문제)
- v1.1: 네이버 공식 검색 API 교체
- v1.2: 종목명 파싱 패턴 대폭 개선 ("종목미상" 문제 해결)
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
        return {"reports": [], "policy_news": []}

    reports     = _collect_reports(date_kr)
    policy_news = _collect_policy_news(date_kr)

    logger.info(f"[news] 리포트 {len(reports)}건, 정책뉴스 {len(policy_news)}건")
    return {"reports": reports, "policy_news": policy_news}


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
