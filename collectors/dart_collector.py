"""
collectors/dart_collector.py
DART 공시 수집 전담
- 전날 마감 후 공시 수집
- 키워드 필터링 (수주, 배당, 자사주, 특허, 내부자거래 등)
- 중복 공시 제거 (접수번호 기준)
- 반환값만 사용 — 분석 로직 없음
"""

import requests
from datetime import datetime
import config
from utils.logger import logger
from utils.date_utils import get_prev_trading_day, fmt_num, get_today


def collect(target_date: datetime = None) -> list[dict]:
    """
    DART 공시 수집
    반환: list[dict]
    {
        "종목명": str,
        "종목코드": str,
        "공시종류": str,
        "핵심내용": str,
        "공시시각": str,
        "신뢰도": str,       # "원본" or "검색"
        "내부자여부": bool
    }
    """
    if target_date is None:
        target_date = get_prev_trading_day(get_today())

    if target_date is None:
        logger.warning("[dart] 전 거래일 없음 (주말) — 수집 건너뜀")
        return []

    date_str         = fmt_num(target_date)           # YYYY-MM-DD
    date_str_nodash  = date_str.replace("-", "")      # YYYYMMDD

    logger.info(f"[dart] {date_str} 공시 수집 시작")

    results = []

    # 1순위: DART OpenAPI 직접 호출
    try:
        results = _fetch_dart_api(date_str_nodash)
        if results:
            logger.info(f"[dart] API 수집 완료 — {len(results)}건")
            return results
    except Exception as e:
        logger.warning(f"[dart] API 호출 실패 ({e}) — 웹 fetch 시도")

    # 2순위: DART 웹사이트 직접 fetch
    try:
        results = _fetch_dart_web(date_str_nodash)
        if results:
            logger.info(f"[dart] 웹 수집 완료 — {len(results)}건")
            return results
    except Exception as e:
        logger.warning(f"[dart] 웹 fetch 실패 ({e})")

    logger.error("[dart] 공시 수집 전체 실패")
    return []


def _fetch_dart_api(date_str: str) -> list[dict]:
    """DART OpenAPI bgn_de/end_de 기간 검색"""
    url = "https://opendart.fss.or.kr/api/list.json"
    params = {
        "crtfc_key": config.DART_API_KEY,
        "bgn_de":    date_str,
        "end_de":    date_str,
        "page_count": 100,
        "sort":      "date",
        "sort_mth":  "desc",
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "000":
        raise ValueError(f"DART API 응답 오류: {data.get('message')}")

    items = data.get("list", [])
    return _filter_and_format(items)


def _fetch_dart_web(date_str: str) -> list[dict]:
    """DART 메인 페이지 공시 목록 fetch (API 실패 시 백업)"""
    url = "https://dart.fss.or.kr/api/search.json"
    params = {
        "key":        config.DART_API_KEY,
        "ds":         date_str,
        "de":         date_str,
        "page_count": 100,
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("list", [])
    return _filter_and_format(items)


def _filter_and_format(items: list) -> list[dict]:
    """키워드 필터 + 중복 제거 + 반환값 포맷 통일"""
    results   = []
    seen_keys = set()   # 중복 제거용: (종목코드 + 공시종류) 기준

    for item in items:
        report_nm = item.get("report_nm", "")

        # 키워드 필터
        matched = next(
            (kw for kw in config.DART_KEYWORDS if kw in report_nm), None
        )
        if not matched:
            continue

        # 중복 제거: 같은 종목의 같은 공시 유형 중복 방지
        stock_code = item.get("stock_code", "")
        dedup_key  = f"{stock_code}_{report_nm}"
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        # 내부자거래 여부
        is_insider = "주요주주" in report_nm

        # 공시 시각 파싱
        rcept_dt = item.get("rcept_dt", "")   # YYYYMMDDHHMMSS
        try:
            time_str = f"{rcept_dt[8:10]}:{rcept_dt[10:12]}"
        except Exception:
            time_str = "N/A"

        results.append({
            "종목명":     item.get("corp_name", ""),
            "종목코드":   stock_code,
            "공시종류":   report_nm,
            "핵심내용":   report_nm,
            "공시시각":   time_str,
            "신뢰도":     "원본",
            "내부자여부": is_insider,
        })

    logger.info(f"[dart] 키워드 필터 + 중복제거 후 {len(results)}건")
    return results
