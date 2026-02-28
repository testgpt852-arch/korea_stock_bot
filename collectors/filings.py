"""
collectors/filings.py
DART 공시 수집 전담
- 전날 마감 후 공시 수집
- 키워드 필터링 (수주, 배당, 자사주, 특허, 내부자거래 등)
- 규모 필터링 — DART 상세 API 호출로 본문 수치 직접 파싱 (방법A)
- 중복 공시 제거
- 반환값만 사용 — 분석 로직 없음

[방법A 상세]
  list.json → 키워드 필터 → 공시 유형별 상세 API 호출 → 규모 필터
  ┌ 단일판매공급계약·수주 → piicDecsn.json → selfCptlRatio (자기자본대비%)
  └ 배당결정             → alotMatter.json → dvdnYld (시가배당률%)
  - 상세 API 실패 시 보수적으로 통과 (필터 미적용)
  - DART API 하루 한도 1,000회 — 최대 15건 × 2회 = 30회 추가 (여유 충분)
  - 건당 0.3초 대기 → 최대 추가 소요 15초

[수정이력]
- v1.0: 기본 구조
- v2.1: 방법A 규모 필터 추가
        corp_code 수집 (list.json → 상세 API 연결용)
        반환값에 "규모" 필드 추가
"""

import time
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
        "종목명":     str,
        "종목코드":   str,
        "공시종류":   str,
        "핵심내용":   str,
        "공시시각":   str,
        "신뢰도":     str,       # "원본" or "검색"
        "내부자여부": bool,
        "규모":       str,       # v2.1: "25.3%" / "150억" / "N/A"
    }
    """
    if target_date is None:
        target_date = get_prev_trading_day(get_today())

    if target_date is None:
        logger.warning("[dart] 전 거래일 없음 (주말) — 수집 건너뜀")
        return []

    date_str        = fmt_num(target_date)
    date_str_nodash = date_str.replace("-", "")

    logger.info(f"[dart] {date_str} 공시 수집 시작")

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


# ── 목록 수집 ─────────────────────────────────────────────────

def _fetch_dart_api(date_str: str) -> list[dict]:
    """DART OpenAPI bgn_de/end_de 기간 검색"""
    url = "https://opendart.fss.or.kr/api/list.json"
    params = {
        "crtfc_key":  config.DART_API_KEY,
        "bgn_de":     date_str,
        "end_de":     date_str,
        "page_count": 100,
        "sort":       "date",
        "sort_mth":   "desc",
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "000":
        raise ValueError(f"DART API 응답 오류: {data.get('message')}")

    items = data.get("list", [])
    return _filter_and_format(items, date_str)


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
    return _filter_and_format(items, date_str)


# ── 필터 + 포맷 ───────────────────────────────────────────────

def _filter_and_format(items: list, date_str: str) -> list[dict]:
    """
    키워드 필터 → 규모 상세조회 → 규모 필터 → 중복 제거 → 포맷
    date_str: YYYYMMDD (상세 API bgn_de/end_de용)
    """
    candidates = []
    seen_keys  = set()

    for item in items:
        report_nm  = item.get("report_nm", "")
        stock_code = item.get("stock_code", "")

        # 키워드 필터
        matched = next(
            (kw for kw in config.DART_KEYWORDS if kw in report_nm), None
        )
        if not matched:
            continue

        # 중복 제거
        dedup_key = f"{stock_code}_{report_nm}"
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        candidates.append(item)

    logger.info(f"[dart] 키워드 필터 후 {len(candidates)}건 — 규모 상세조회 시작")

    results = []
    for item in candidates:
        report_nm  = item.get("report_nm", "")
        corp_code  = item.get("corp_code",  "")   # list.json에 포함됨
        stock_code = item.get("stock_code", "")
        is_insider = "주요주주" in report_nm

        # 공시 시각 파싱
        rcept_dt = item.get("rcept_dt", "")
        try:
            time_str = f"{rcept_dt[8:10]}:{rcept_dt[10:12]}"
        except Exception:
            time_str = "N/A"

        # 규모 상세조회 (v2.1)
        size_str  = "N/A"
        size_pass = True

        if corp_code:
            size_str, size_pass = _fetch_and_filter_size(
                report_nm, corp_code, date_str
            )

        if not size_pass:
            logger.info(
                f"[dart] 규모 필터 제외: {item.get('corp_name')} "
                f"— {report_nm} ({size_str})"
            )
            continue

        results.append({
            "종목명":     item.get("corp_name", ""),
            "종목코드":   stock_code,
            "공시종류":   report_nm,
            "핵심내용":   report_nm,
            "공시시각":   time_str,
            "신뢰도":     "원본",
            "내부자여부": is_insider,
            "규모":       size_str,
        })

    logger.info(f"[dart] 규모 필터 후 최종 {len(results)}건")
    return results


# ── 규모 상세조회 (방법A 핵심) ────────────────────────────────

def _fetch_and_filter_size(
    report_nm: str, corp_code: str, date_str: str
) -> tuple[str, bool]:
    """
    공시 유형별 DART 상세 API 호출 → (규모문자열, 통과여부) 반환

    규칙:
    - API 실패 또는 수치 파싱 실패 → ("N/A", True)  보수적 통과
    - 수치 파싱 성공 → 임계값 비교
    """
    if any(kw in report_nm for kw in ["단일판매공급계약", "수주"]):
        return _fetch_contract_size(corp_code, date_str)

    if "배당결정" in report_nm:
        return _fetch_dividend_size(corp_code, date_str)

    # 자사주, MOU, 특허, 판결, 주요주주 → 규모 필터 없이 통과
    return ("N/A", True)


def _fetch_contract_size(corp_code: str, date_str: str) -> tuple[str, bool]:
    """
    단일판매공급계약·수주 상세 조회
    DART API: piicDecsn.json
    주요 필드:
      selfCptlRatio : 자기자본대비 (%)  ← 우선 사용
      slCtrctAmt    : 계약금액 (원)     ← fallback
    """
    try:
        url = "https://opendart.fss.or.kr/api/piicDecsn.json"
        params = {
            "crtfc_key": config.DART_API_KEY,
            "corp_code": corp_code,
            "bgn_de":    date_str,
            "end_de":    date_str,
        }
        resp = requests.get(url, params=params, timeout=8)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "000":
            logger.debug(f"[dart] piicDecsn 오류: {data.get('message')}")
            return ("N/A", True)

        items = data.get("list", [])
        if not items:
            return ("N/A", True)

        row = items[0]
        # 첫 실행 시 실제 필드명 확인용 (안정화 후 제거 가능)
        logger.debug(f"[dart] piicDecsn 필드목록: {list(row.keys())}")

        # 자기자본대비 비율 우선
        ratio = _parse_number(row.get("selfCptlRatio", ""))
        if ratio is not None:
            size_str = f"{ratio:.1f}%"
            passes   = ratio >= config.DART_CONTRACT_MIN_RATIO
            return (size_str, passes)

        # 계약금액(원) fallback
        amount = _parse_number(row.get("slCtrctAmt", ""))
        if amount is not None:
            amount_억 = amount / 100_000_000
            size_str  = f"{amount_억:.0f}억"
            passes    = amount_억 >= config.DART_CONTRACT_MIN_BILLION
            return (size_str, passes)

        return ("N/A", True)

    except Exception as e:
        logger.debug(f"[dart] piicDecsn 호출 실패 ({corp_code}): {e}")
        return ("N/A", True)

    finally:
        time.sleep(0.3)   # DART API 과부하 방지


def _fetch_dividend_size(corp_code: str, date_str: str) -> tuple[str, bool]:
    """
    배당결정 상세 조회
    DART API: alotMatter.json
    주요 필드:
      dvdnYld : 시가배당률 (%)
    """
    try:
        url = "https://opendart.fss.or.kr/api/alotMatter.json"
        params = {
            "crtfc_key": config.DART_API_KEY,
            "corp_code": corp_code,
            "bgn_de":    date_str,
            "end_de":    date_str,
        }
        resp = requests.get(url, params=params, timeout=8)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "000":
            logger.debug(f"[dart] alotMatter 오류: {data.get('message')}")
            return ("N/A", True)

        items = data.get("list", [])
        if not items:
            return ("N/A", True)

        row = items[0]
        logger.debug(f"[dart] alotMatter 필드목록: {list(row.keys())}")

        yld = _parse_number(row.get("dvdnYld", ""))
        if yld is not None:
            size_str = f"{yld:.2f}%"
            passes   = yld >= config.DART_DIVIDEND_MIN_RATE
            return (size_str, passes)

        return ("N/A", True)

    except Exception as e:
        logger.debug(f"[dart] alotMatter 호출 실패 ({corp_code}): {e}")
        return ("N/A", True)

    finally:
        time.sleep(0.3)


# ── 유틸 ──────────────────────────────────────────────────────

def _parse_number(value: str) -> float | None:
    """
    숫자 문자열 → float 변환 (쉼표·공백·% 제거)
    예: "25.3" → 25.3 / "25,300,000" → 25300000.0 / "-" → None
    """
    if not value or str(value).strip() in ("-", "", "N/A"):
        return None
    try:
        cleaned = str(value).replace(",", "").replace(" ", "").replace("%", "")
        return float(cleaned)
    except ValueError:
        return None
