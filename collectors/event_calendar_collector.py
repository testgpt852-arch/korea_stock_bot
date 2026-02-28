"""
collectors/event_calendar_collector.py
기업 이벤트 캘린더 수집 전담

[수집 전략 — 2소스 병행]
1순위: DART 공시목록 API (list.json) — pblntf_ty 없이 키워드 검색
       (pblntf_ty 분류 코드 사용 시 status=013 권한 오류 발생 → 제거)
2순위: KRX KIND 공시시스템 (data.krx.co.kr) — 무료 REST API, 별도 인증 불필요
       IR·실적발표·주주총회·배당 공시 수집

[설계 원칙 — rule #90 계열 준수]
- 수집만 담당: AI 분석·텔레그램 발송·DB 기록 절대 금지
- 비치명적: 실패 시 빈 리스트 반환 (아침봇 blocking 금지)
- 분석 로직: event_impact_analyzer.py 에서 담당

[출력 형식]
[
    {
        "event_type":  str,   # "IR" | "주주총회" | "실적발표" | "배당"
        "corp_name":   str,   # 기업명
        "ticker":      str,   # 종목코드
        "event_date":  str,   # 예정일 (YYYY-MM-DD 또는 "" )
        "days_until":  int,   # 오늘부터 며칠 후 (0=당일, -1=불명확)
        "title":       str,   # 공시 제목
        "rcept_no":    str,   # 접수번호 (URL 조합용)
        "source":      str,   # "dart" | "krx"
    }
]

[v12.0 수정 — 2025-02]
- DART pblntf_ty 분류 코드 제거 → 키워드 전문 검색 방식으로 전환
  (status=013 권한 오류 해결)
- KRX KIND REST API 2순위 소스 추가 (별도 인증 불필요)
"""

import re
import requests
from datetime import datetime, timedelta

import config
from utils.logger import logger
from utils.date_utils import get_today


# ── 이벤트 키워드 분류 ─────────────────────────────────────────
_EVENT_TYPES = {
    "IR":     ["기업설명회", "IR ", "기업 설명회", "NDR", "기업탐방"],
    "주주총회":  ["주주총회", "임시주주총회", "정기주주총회"],
    "실적발표":  ["실적발표", "영업실적", "잠정실적", "사업실적", "분기실적"],
    "배당":    ["현금배당", "중간배당", "특별배당", "배당결정"],
}

# DART 공시목록 API (list.json — 전체 공시, 분류 코드 없이 조회)
_DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"

# KRX KIND 공시 조회 API (별도 인증 불필요 — AJAX 엔드포인트)
_KRX_KIND_URL = "https://kind.krx.co.kr/disclosure/todaydisclosure.do"

_TOTAL_DAYS = 14    # 수집 범위: 오늘 ~ 14일 후


def collect(target_date: datetime = None) -> list[dict]:
    """
    기업 이벤트 캘린더 수집
    반환: list[dict] — event_type / corp_name / ticker / event_date / days_until / title / rcept_no / source
    실패 시 빈 리스트 반환 (비치명적 — rule #90 계열)
    """
    if not config.EVENT_CALENDAR_ENABLED:
        return []

    if target_date is None:
        target_date = get_today()

    logger.info(f"[event_calendar] 이벤트 캘린더 수집 시작 ({target_date.strftime('%Y-%m-%d')} 기준)")

    events: list[dict] = []

    # 1순위: DART 키워드 검색 (API 키 있을 때만)
    if config.DART_API_KEY:
        try:
            dart_events = _collect_dart_keyword(target_date)
            events.extend(dart_events)
            logger.info(f"[event_calendar] DART 수집: {len(dart_events)}건")
        except Exception as e:
            logger.warning(f"[event_calendar] DART 수집 실패 (비치명적): {e}")
    else:
        logger.info("[event_calendar] DART_API_KEY 없음 — DART 건너뜀, KRX 시도")

    # 2순위: KRX KIND (DART 결과 부족 시 보완)
    if len(events) < 5:
        try:
            krx_events = _collect_krx_kind(target_date)
            events.extend(krx_events)
            logger.info(f"[event_calendar] KRX KIND 수집: {len(krx_events)}건")
        except Exception as e:
            logger.warning(f"[event_calendar] KRX KIND 수집 실패 (비치명적): {e}")

    # 중복 제거 (rcept_no 또는 title 기준)
    seen = set()
    unique = []
    for ev in events:
        key = ev.get("rcept_no") or ev.get("title", "")[:40]
        if key not in seen:
            seen.add(key)
            unique.append(ev)

    logger.info(f"[event_calendar] 수집 완료 — {len(unique)}건 (DART+KRX 합산)")
    return unique


# ── 1순위: DART 키워드 검색 ────────────────────────────────────

def _collect_dart_keyword(target_date: datetime) -> list[dict]:
    """
    DART list.json — pblntf_ty 없이 전체 공시에서 키워드로 필터링.
    pblntf_ty 분류 코드(F/D/A)는 계정 권한이 필요(status=013) → 제거.
    """
    start_dt = target_date
    end_dt   = target_date + timedelta(days=_TOTAL_DAYS)
    bgn_de   = start_dt.strftime("%Y%m%d")
    end_de   = end_dt.strftime("%Y%m%d")

    params = {
        "crtfc_key":  config.DART_API_KEY,
        "bgn_de":     bgn_de,
        "end_de":     end_de,
        "page_count": 100,
        "sort":       "date",
        "sort_mthd":  "asc",
        # pblntf_ty 제거 — 전체 공시 조회 후 키워드로 필터
    }
    resp = requests.get(_DART_LIST_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    status = data.get("status", "")
    if status not in ("000", "013"):
        # 000 = 정상, 013 = 조회된 데이터 없음 (기간 내 공시 없음) — 둘 다 비치명적
        raise RuntimeError(f"DART API 응답 오류: status={status} msg={data.get('message')}")

    items = data.get("list", [])
    events = []
    for item in items:
        ev = _parse_dart_item(item, target_date)
        if ev:
            events.append(ev)
    return events


def _parse_dart_item(item: dict, today: datetime) -> dict | None:
    """DART 공시 항목 → 이벤트 dict 변환. 키워드 미매칭 시 None."""
    report_nm  = item.get("report_nm", "")
    corp_name  = item.get("corp_name", "")
    rcept_dt   = item.get("rcept_dt", "")
    rcept_no   = item.get("rcept_no", "")
    stock_code = item.get("stock_code", "")

    event_type = _classify_event(report_nm)
    if event_type is None:
        return None

    event_date, days_until = _parse_date(rcept_dt, today)
    if days_until < 0:
        return None   # 이미 지난 공시 제외

    return {
        "event_type": event_type,
        "corp_name":  corp_name,
        "ticker":     stock_code,
        "event_date": event_date,
        "days_until": days_until,
        "title":      report_nm,
        "rcept_no":   rcept_no,
        "source":     "dart",
    }


# ── 2순위: KRX KIND ────────────────────────────────────────────

def _collect_krx_kind(target_date: datetime) -> list[dict]:
    """
    KRX KIND 공시시스템 — 별도 인증 불필요.
    https://kind.krx.co.kr 에서 오늘 ~ _TOTAL_DAYS 범위 이벤트 수집.
    """
    events = []
    for day_offset in range(_TOTAL_DAYS + 1):
        target = target_date + timedelta(days=day_offset)
        try:
            day_events = _fetch_krx_kind_day(target, target_date)
            events.extend(day_events)
        except Exception as e:
            logger.debug(f"[event_calendar/krx] {target.strftime('%Y-%m-%d')} 조회 실패 (무시): {e}")
    return events


def _fetch_krx_kind_day(day: datetime, today: datetime) -> list[dict]:
    """KRX KIND 단일 날짜 공시 조회"""
    date_str = day.strftime("%Y%m%d")
    params = {
        "method":         "searchTodayDisclosureSub",
        "currentPageSize": "100",
        "currentPage":    "1",
        "searchDate":     date_str,
        "forward":        "todaydisclosure_sub",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer":    "https://kind.krx.co.kr/",
    }
    resp = requests.get(_KRX_KIND_URL, params=params, headers=headers, timeout=8)
    resp.raise_for_status()

    # KRX KIND는 HTML 테이블 반환 → 간단 파싱
    text = resp.text
    events = []

    # 공시 제목 패턴 추출 (테이블 td 내 a 태그)
    pattern = re.compile(
        r'<a[^>]+href="([^"]+)"[^>]*>\s*([^<]{5,120})\s*</a>.*?'
        r'<td[^>]*>\s*([\w가-힣(주)·\s]{1,30})\s*</td>.*?'
        r'<td[^>]*>\s*(\d{6})\s*</td>',
        re.DOTALL,
    )
    for m in pattern.finditer(text):
        title      = m.group(2).strip()
        corp_name  = m.group(3).strip()
        stock_code = m.group(4).strip()

        event_type = _classify_event(title)
        if not event_type:
            continue

        event_date, days_until = _parse_date(date_str, today)

        events.append({
            "event_type": event_type,
            "corp_name":  corp_name,
            "ticker":     stock_code,
            "event_date": event_date,
            "days_until": days_until,
            "title":      title,
            "rcept_no":   "",
            "source":     "krx",
        })

    return events


# ── 공용 유틸 ──────────────────────────────────────────────────

def _classify_event(report_nm: str) -> str | None:
    """공시 제목 → 이벤트 유형 분류. 미매칭 시 None."""
    for event_type, keywords in _EVENT_TYPES.items():
        for kw in keywords:
            if kw in report_nm:
                return event_type
    return None


def _parse_date(date_str: str, today: datetime) -> tuple[str, int]:
    """YYYYMMDD 문자열 → (YYYY-MM-DD, days_until). 실패 시 ("", -1)."""
    if not date_str:
        return "", -1
    try:
        dt = datetime.strptime(date_str, "%Y%m%d")
        return dt.strftime("%Y-%m-%d"), (dt - today).days
    except ValueError:
        return "", -1
