"""
collectors/filings.py
DART 공시 수집 전담
- 전날 마감 후 공시 수집
- 키워드 필터링 (수주, 배당, 자사주, 특허, 내부자거래, 소송, 임상 등)
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

[방법B — v13.0 신규: DART document API 본문 수집]
  list.json의 rcept_no 활용
  GET https://opendart.fss.or.kr/api/document.json?crtfc_key=...&rcept_no=...
  응답: zip → 압축 해제 → XML/HTML 파싱 → 핵심 수치 추출
  추출 대상: 계약금액, 매출대비비율, 소송금액, 승패여부, 청구금액

[수정이력]
- v1.0: 기본 구조
- v2.1: 방법A 규모 필터 추가
        corp_code 수집 (list.json → 상세 API 연결용)
        반환값에 "규모" 필드 추가
- v13.0: DART_KEYWORDS 확장 (소송/임상/FDA/무상증자 등 10개 신규)
         임계값 상향 (DART_CONTRACT_MIN_RATIO 10→20, MIN_BILLION 50→100, DIVIDEND 3→5)
         DART document API 본문 수집 추가 (_fetch_document_summary)
         반환값에 "본문요약", "rcept_no" 필드 추가
"""

import io
import re
import time
import zipfile
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
        "핵심내용":   str,       # 공시 제목 원문 (report_nm)
        "공시시각":   str,
        "신뢰도":     str,       # "원본" or "검색"
        "내부자여부": bool,      # 주요주주 공시 여부
        "규모":       str,       # v2.1: "25.3%" / "150억" / "N/A"
        "본문요약":   str,       # v13.0: 핵심 수치 추출 텍스트 (없으면 "")
        "rcept_no":   str,       # v13.0: DART 본문 API 연결용 접수번호
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
    """
    DART 웹사이트 공시 목록 fetch (API 실패 시 백업)

    ⚠️ [v13.0 주의] 이 함수는 공식 OpenAPI가 아닌 비공식 엔드포인트를 사용.
    dart.fss.or.kr/api/search.json 은 실제 DART 웹 내부 Ajax 엔드포인트로
    파라미터 구조·응답 형식·가용성이 opendart.fss.or.kr/api/list.json 과 다름.
    실패 시 빈 리스트 반환이므로 비치명적이나 안정성 보장 불가.
    """
    url = "https://dart.fss.or.kr/api/search.json"
    params = {
        "key":        config.DART_API_KEY,   # ⚠️ 공식 OpenAPI의 crtfc_key와 다름
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

        # 키워드 필터 (v13.0: config.DART_KEYWORDS 확장됨)
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
        rcept_no   = item.get("rcept_no",   "")   # v13.0: 본문 API 연결용
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

        # v13.0: DART document API 본문 요약 수집
        body_summary = ""
        if rcept_no:
            body_summary = _fetch_document_summary(rcept_no, report_nm)

        results.append({
            "종목명":     item.get("corp_name", ""),
            "종목코드":   stock_code,
            "공시종류":   report_nm,
            "핵심내용":   report_nm,
            "공시시각":   time_str,
            "신뢰도":     "원본",
            "내부자여부": is_insider,
            "규모":       size_str,
            "본문요약":   body_summary,   # v13.0 신규
            "rcept_no":   rcept_no,       # v13.0 신규
        })

    logger.info(f"[dart] 규모 필터 후 최종 {len(results)}건")
    return results


# ── v13.0: DART document API 본문 수집 ───────────────────────

def _fetch_document_summary(rcept_no: str, report_nm: str) -> str:
    """
    DART document API (document.json) 호출 → zip 압축 해제 → 핵심 수치 추출

    파라미터:
      rcept_no  : list.json 응답의 rcept_no 필드 (접수번호)
      report_nm : 공시 보고서명 (추출 전략 분기용)

    반환: 핵심 수치 텍스트 (실패 시 "")
      예: "계약금액 320억, 자기자본대비 25.8%"
          "승소, 청구금액 85억"
          "임상 3상, FDA"

    DART API 스펙:
      GET https://opendart.fss.or.kr/api/document.json
      파라미터: crtfc_key, rcept_no
      응답: application/zip → 압축 해제 → XML/HTML 파일 포함
    """
    if not rcept_no or not config.DART_API_KEY:
        return ""

    try:
        url = "https://opendart.fss.or.kr/api/document.json"
        params = {
            "crtfc_key": config.DART_API_KEY,
            "rcept_no":  rcept_no,
        }
        resp = requests.get(url, params=params, timeout=15)

        if resp.status_code != 200:
            logger.debug(f"[dart] document API HTTP {resp.status_code}: {rcept_no}")
            return ""

        # Content-Type json → DART 에러 응답
        content_type = resp.headers.get("Content-Type", "")
        if "json" in content_type:
            try:
                err = resp.json()
                logger.debug(
                    f"[dart] document API 오류: {err.get('message')} "
                    f"(rcept_no={rcept_no})"
                )
            except Exception:
                pass
            return ""

        # zip 압축 해제 → 텍스트 추출
        raw_text = _extract_zip_text(resp.content)
        if not raw_text:
            return ""

        return _parse_document_text(raw_text, report_nm)

    except Exception as e:
        logger.debug(f"[dart] document API 실패 ({rcept_no}): {e}")
        return ""

    finally:
        time.sleep(0.3)   # DART API 과부하 방지


def _extract_zip_text(zip_bytes: bytes) -> str:
    """
    zip 바이너리 → 텍스트 추출
    DART zip 내 파일: .xml 또는 .htm / .html
    """
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                if name.lower().endswith((".xml", ".htm", ".html")):
                    raw = zf.read(name)
                    for enc in ("utf-8", "euc-kr", "cp949"):
                        try:
                            return raw.decode(enc)
                        except UnicodeDecodeError:
                            continue
    except zipfile.BadZipFile:
        logger.debug("[dart] BadZipFile — document API 응답이 zip이 아님")
    except Exception as e:
        logger.debug(f"[dart] zip 압축 해제 실패: {e}")
    return ""


def _parse_document_text(text: str, report_nm: str) -> str:
    """
    공시 본문 텍스트에서 핵심 수치 추출 (HTML/XML 태그 제거 후 파싱)

    공시 유형별 추출 전략:
    - 계약/수주/MOU : 계약금액, 매출·자기자본대비비율
    - 소송/판결/중재: 승패여부, 청구금액
    - 임상/허가/FDA : 임상 단계, 허가기관, 적응증
    - 배당결정       : 시가배당률, 배당금
    - 무상/유상증자  : 신주 수, 증자비율
    - 기타           : 억원 단위 금액 추출
    """
    # HTML/XML 태그·엔티티 제거
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"&[a-zA-Z]+;", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    clean = clean[:8000]   # 처리 속도 보장용 제한

    extracted = []

    # ─ 계약/수주/MOU ──────────────────────────────────────────
    if any(kw in report_nm for kw in ["단일판매공급계약", "수주", "MOU"]):
        m = re.search(r"계약\s*금액\s*[:\s]*([\d,]+)\s*(원|백만원|억원?)?", clean)
        if m:
            val_str = m.group(1).replace(",", "")
            unit    = m.group(2) or "원"
            try:
                val = int(val_str)
                if "백만" in unit:
                    val_억 = val / 100
                elif "억" in unit:
                    val_억 = val
                else:
                    val_억 = val / 100_000_000
                extracted.append(f"계약금액 {val_억:.0f}억")
            except ValueError:
                extracted.append(f"계약금액 {m.group(1)}{m.group(2) or ''}")

        m2 = re.search(r"(매출|자기자본)\s*대비\s*[:\s]*([\d.]+)\s*%", clean)
        if m2:
            extracted.append(f"{m2.group(1)}대비 {m2.group(2)}%")

    # ─ 소송/판결/중재/화해 ────────────────────────────────────
    elif any(kw in report_nm for kw in ["소송", "판결", "중재", "화해"]):
        if re.search(r"(승소|인용|승리|원고\s*승)", clean):
            extracted.append("승소")
        elif re.search(r"(패소|기각|패배|원고\s*패)", clean):
            extracted.append("패소")
        elif "화해" in clean or "조정" in clean:
            extracted.append("화해/조정")

        m = re.search(r"청구\s*금액\s*[:\s]*([\d,]+)\s*(원|백만원|억원?)?", clean)
        if m:
            val_str = m.group(1).replace(",", "")
            unit    = m.group(2) or "원"
            try:
                val = int(val_str)
                if "백만" in unit:
                    val_억 = val / 100
                elif "억" in unit:
                    val_억 = val
                else:
                    val_억 = val / 100_000_000
                extracted.append(f"청구금액 {val_억:.0f}억")
            except ValueError:
                extracted.append(f"청구금액 {m.group(1)}{m.group(2) or ''}")

    # ─ 임상/허가/FDA/식약처 ──────────────────────────────────
    elif any(kw in report_nm for kw in ["임상", "허가", "FDA", "식약처"]):
        m = re.search(r"임상\s*([1-3]|I{1,3})\s*상", clean)
        if m:
            extracted.append(f"임상 {m.group(1)}상")

        for agency in ["FDA", "식약처", "EMA"]:
            if agency in clean:
                extracted.append(agency)
                break

        m2 = re.search(r"적응증\s*[:\s]*([가-힣a-zA-Z\s]{2,20})", clean)
        if m2:
            extracted.append(f"적응증: {m2.group(1).strip()}")

    # ─ 배당결정 ──────────────────────────────────────────────
    elif "배당" in report_nm:
        m = re.search(r"시가\s*배당률\s*[:\s]*([\d.]+)\s*%", clean)
        if m:
            extracted.append(f"시가배당률 {m.group(1)}%")
        m2 = re.search(r"배당\s*금\s*[:\s]*([\d,]+)\s*(원|억원?)?", clean)
        if m2:
            extracted.append(f"배당금 {m2.group(1)}{m2.group(2) or '원'}")

    # ─ 무상증자/유상증자 ─────────────────────────────────────
    elif any(kw in report_nm for kw in ["무상증자", "유상증자"]):
        m = re.search(r"신주\s*발행\s*주식\s*수\s*[:\s]*([\d,]+)\s*주?", clean)
        if m:
            extracted.append(f"신주 {m.group(1)}주")
        m2 = re.search(r"(증자\s*비율|무상\s*비율)\s*[:\s]*([\d.]+)\s*%?", clean)
        if m2:
            extracted.append(f"증자비율 {m2.group(2)}%")

    # ─ 기타 ──────────────────────────────────────────────────
    else:
        amounts = re.findall(r"([\d,]+)\s*억\s*원?", clean[:2000])
        if amounts:
            extracted.append(f"{amounts[0]}억원")

    if not extracted:
        return ""

    return ", ".join(extracted)


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

    # 자사주, MOU, 특허, 판결, 주요주주, 소송, 임상 등 → 규모 필터 없이 통과
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
