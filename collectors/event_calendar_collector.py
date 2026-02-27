"""
collectors/event_calendar_collector.py
기업 이벤트 캘린더 수집 전담 — DART 공시 채널에서 IR/실적 일정 파싱

[설계 원칙 — rule #90 계열 준수]
- 수집만 담당: AI 분석·텔레그램 발송·DB 기록 절대 금지
- 비치명적: 실패 시 빈 리스트 반환 (아침봇 blocking 금지)
- 분석 로직: event_impact_analyzer.py 에서 담당

[수집 대상]
1. DART 공시 채널 기업설명회·IR (pblntf_ty='F') — 향후 7일 이내
2. DART 공시 주주총회 (pblntf_ty='D') — 향후 14일 이내
3. DART 공시 실적발표 키워드 검색 — 향후 7일 이내

[출력 형식]
[
    {
        "event_type":  str,   # "IR" | "주주총회" | "실적발표" | "배당"
        "corp_name":   str,   # 기업명
        "ticker":      str,   # 종목코드 (DART stock_code 기반)
        "event_date":  str,   # 예정일 (YYYY-MM-DD 또는 추출 불가 시 "")
        "days_until":  int,   # 오늘부터 며칠 후 (0=당일, -1=불명확)
        "title":       str,   # 공시 제목
        "rcept_no":    str,   # DART 접수번호 (URL 조합용)
    }
]

[v10.0 Phase 4-1 신규]
"""

import re
import requests
from datetime import datetime, timedelta

import config
from utils.logger import logger
from utils.date_utils import get_today


# 수집할 이벤트 유형 → DART 공시 유형 코드
_EVENT_TYPES = {
    "IR":     ["기업설명회", "IR ", "기업 설명회"],
    "주주총회":  ["주주총회", "임시주주총회"],
    "실적발표":  ["실적발표", "영업실적", "잠정실적", "사업실적"],
    "배당":    ["현금배당", "중간배당"],
}

# DART 공시목록 API
_DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"

# 최대 수집 일수 범위
_LOOKAHEAD_DAYS = 7     # IR·실적: 7일 이내
_TOTAL_DAYS     = 14    # 주주총회: 14일 이내


def collect(target_date: datetime = None) -> list[dict]:
    """
    기업 이벤트 캘린더 수집
    반환: list[dict] — event_type / corp_name / ticker / event_date / days_until / title / rcept_no
    실패 시 빈 리스트 반환 (비치명적 — rule #90 계열)
    EVENT_CALENDAR_ENABLED=false 시 즉시 빈 리스트 반환
    """
    if not config.EVENT_CALENDAR_ENABLED:
        return []

    if not config.DART_API_KEY:
        logger.warning("[event_calendar] DART_API_KEY 없음 — 수집 건너뜀")
        return []

    if target_date is None:
        target_date = get_today()

    logger.info(f"[event_calendar] 기업 이벤트 캘린더 수집 시작 ({target_date.strftime('%Y-%m-%d')} 기준)")

    events: list[dict] = []
    try:
        events.extend(_collect_dart_events(target_date))
    except Exception as e:
        logger.warning(f"[event_calendar] DART 이벤트 수집 실패: {e}")

    # 중복 제거 (rcept_no 기준)
    seen = set()
    unique = []
    for ev in events:
        key = ev.get("rcept_no", ev.get("title", ""))
        if key not in seen:
            seen.add(key)
            unique.append(ev)

    logger.info(f"[event_calendar] 수집 완료 — {len(unique)}건")
    return unique


def _collect_dart_events(target_date: datetime) -> list[dict]:
    """DART 공시목록 API에서 기업 이벤트 수집"""
    events: list[dict] = []

    # 조회 기간: 오늘 ~ 14일 후
    start_dt = target_date
    end_dt   = target_date + timedelta(days=_TOTAL_DAYS)
    bgn_de   = start_dt.strftime("%Y%m%d")
    end_de   = end_dt.strftime("%Y%m%d")

    # 수집할 공시 유형 코드 목록
    # F = 공정공시(IR·사업설명회 포함), D = 의결권·주주총회, A = 정기공시(사업보고서·실적)
    for pblntf_ty in ["F", "D", "A"]:
        try:
            items = _fetch_dart_list(bgn_de, end_de, pblntf_ty)
            for item in items:
                ev = _parse_dart_item(item, target_date)
                if ev:
                    events.append(ev)
        except Exception as e:
            logger.warning(f"[event_calendar] DART pblntf_ty={pblntf_ty} 조회 실패: {e}")

    return events


def _fetch_dart_list(bgn_de: str, end_de: str, pblntf_ty: str) -> list[dict]:
    """DART 공시목록 API 호출"""
    params = {
        "crtfc_key": config.DART_API_KEY,
        "bgn_de":    bgn_de,
        "end_de":    end_de,
        "pblntf_ty": pblntf_ty,
        "page_count": 40,
        "sort":       "date",
        "sort_mthd":  "asc",
    }
    resp = requests.get(_DART_LIST_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "000":
        # 결과 없음 또는 오류 — 비치명적
        return []

    return data.get("list", [])


def _parse_dart_item(item: dict, today: datetime) -> dict | None:
    """
    DART 공시 항목 파싱 → 이벤트 dict 변환
    이벤트 유형에 해당하지 않으면 None 반환
    """
    report_nm  = item.get("report_nm", "")
    corp_name  = item.get("corp_name", "")
    rcept_dt   = item.get("rcept_dt", "")    # 접수일 YYYYMMDD
    rcept_no   = item.get("rcept_no", "")
    stock_code = item.get("stock_code", "")  # 종목코드 (없을 수 있음)

    # 이벤트 유형 분류
    event_type = _classify_event(report_nm)
    if event_type is None:
        return None

    # 이벤트 예정일 파싱 (접수일 기준 — 실제 일정은 공시 내부에 있으나 API 미제공)
    event_date = ""
    days_until = -1
    if rcept_dt:
        try:
            ev_dt = datetime.strptime(rcept_dt, "%Y%m%d")
            event_date = ev_dt.strftime("%Y-%m-%d")
            days_until = (ev_dt - today).days
        except ValueError:
            pass

    # 조기 이벤트만 포함 (이미 지난 공시 제외)
    if days_until < 0 and rcept_dt:
        return None

    return {
        "event_type":  event_type,
        "corp_name":   corp_name,
        "ticker":      stock_code,
        "event_date":  event_date,
        "days_until":  days_until,
        "title":       report_nm,
        "rcept_no":    rcept_no,
    }


def _classify_event(report_nm: str) -> str | None:
    """공시 제목 → 이벤트 유형 분류"""
    for event_type, keywords in _EVENT_TYPES.items():
        for kw in keywords:
            if kw in report_nm:
                return event_type
    return None
