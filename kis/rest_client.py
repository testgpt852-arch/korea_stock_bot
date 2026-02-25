"""
kis/rest_client.py
KIS REST API 호출 전담

[수정이력]
- v2.5:   get_volume_ranking() 추가
- v2.5.1: URL 수정 → /uapi/domestic-stock/v1/quotations/volume-rank
- v2.5.2: logger.debug → logger.info (Railway 로그 가시성 확보)
          응답 rt_cd/msg_cd 진단 로그 추가
- v2.7:   [버그수정] get_volume_ranking() KIS API 파라미터 오류 수정
          FID_COND_MRKT_DIV_CODE: "J"/"Q" → 항상 "J" 고정
            (volume-rank 엔드포인트는 "Q" 미지원 → OPSQ2001 오류)
          FID_INPUT_ISCD: "0000" → "0001"(코스피) / "1001"(코스닥) 으로 시장 구분
            (기존 "0000" 전체조회 사용 시 rt_cd=0 이지만 항목=0 반환)
          호출부(volume_analyzer.py) 인터페이스 유지: "J"→코스피, "Q"→코스닥
- v2.9:   get_rate_ranking() 신규 추가 (tr_id: FHPST01700000)
          등락률 순위 TOP 30 조회 — 거래량 적어도 급등하는 소형주 포착
          반환값 규격: get_volume_ranking()과 동일 (volume_analyzer 호환)
- v3.0:   [get_rate_ranking 전면 개편]
          종목코드 필드 버그 수정: mksc_shrn_iscd → stck_shrn_iscd
          코스닥(Q): 모든 노이즈 제외 (관리/경고/위험/우선주/스팩/ETF/ETN, 1111111)
          코스피(J): 중형+소형 2회 호출 후 합산 → 대형주 사실상 제외
          등락률 범위 0~10% (FID_RSFL_RATE2="10") — 초기 급등 조기 포착
          FID_COND_MRKT_DIV_CODE 항상 "J" 고정 (rate API도 J 통일)
          내부 헬퍼 _fetch_rate_once() 분리
"""

import requests
from utils.logger import logger
from kis.auth import get_access_token
import config

_BASE_URL = "https://openapi.koreainvestment.com:9443"

# volume-rank API: FID_COND_MRKT_DIV_CODE는 항상 "J"
# 코스피/코스닥 구분은 FID_INPUT_ISCD로 처리
_MARKET_INPUT_ISCD = {
    "J": "0001",   # 코스피
    "Q": "1001",   # 코스닥
}


def get_stock_price(ticker: str) -> dict:
    token = get_access_token()
    if not token:
        return {}
    url = f"{_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {
        "Authorization":  f"Bearer {token}",
        "appkey":         config.KIS_APP_KEY,
        "appsecret":      config.KIS_APP_SECRET,
        "tr_id":          "FHKST01010100",
        "Content-Type":   "application/json; charset=utf-8",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD":         ticker,
    }
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=5)
        resp.raise_for_status()
        out = resp.json().get("output", {})
        return {
            "현재가": int(out.get("stck_prpr", 0)),
            "등락률": float(out.get("prdy_ctrt", 0)),
            "거래량": int(out.get("acml_vol", 0)),
        }
    except Exception as e:
        logger.warning(f"[rest] {ticker} 현재가 조회 실패: {e}")
        return {}


def get_volume_ranking(market_code: str) -> list[dict]:
    """
    KIS 거래량 순위 조회
    tr_id: FHPST01710000
    URL:   /uapi/domestic-stock/v1/quotations/volume-rank

    market_code: "J" = 코스피, "Q" = 코스닥  (volume_analyzer 인터페이스 유지)

    [v2.7 파라미터 수정]
    - FID_COND_MRKT_DIV_CODE: 항상 "J" (volume-rank는 "Q" 미지원)
    - FID_INPUT_ISCD: "0001"(코스피) / "1001"(코스닥) 으로 시장 구분
    """
    token = get_access_token()
    if not token:
        logger.warning("[rest] 토큰 없음 — 거래량 순위 조회 불가")
        return []

    market_name = "코스피" if market_code == "J" else "코스닥"
    input_iscd  = _MARKET_INPUT_ISCD.get(market_code, "0001")
    logger.info(f"[rest] {market_name} 거래량 순위 조회 시작...")

    url = f"{_BASE_URL}/uapi/domestic-stock/v1/quotations/volume-rank"
    headers = {
        "Authorization":  f"Bearer {token}",
        "appkey":         config.KIS_APP_KEY,
        "appsecret":      config.KIS_APP_SECRET,
        "tr_id":          "FHPST01710000",
        "custtype":       "P",
        "Content-Type":   "application/json; charset=utf-8",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE":   "J",           # 항상 "J" 고정 (v2.7)
        "FID_COND_SCR_DIV_CODE":    "20171",
        "FID_INPUT_ISCD":           input_iscd,     # "0001"=코스피 / "1001"=코스닥 (v2.7)
        "FID_DIV_CLS_CODE":         "0",            # 0:전체
        "FID_BLNG_CLS_CODE":        "0",            # 0:전체 (1~7 업종별)
        "FID_TRGT_CLS_CODE":        "111111111",    # 전체 종목 대상
        "FID_TRGT_EXLS_CLS_CODE":   "000000",       # 제외 없음
        "FID_INPUT_PRICE_1":        "0",            # 가격 하한 (빈값→0)
        "FID_INPUT_PRICE_2":        "0",            # 가격 상한 (빈값→0, 0=제한없음)
        "FID_VOL_CNT":              "100",
        "FID_INPUT_DATE_1":         "",
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        body   = resp.json()
        rt_cd  = body.get("rt_cd",  "?")
        msg_cd = body.get("msg_cd", "?")
        msg1   = body.get("msg1",   "")

        # output 키 자동 탐지: output1 → output → output2 순서로 시도
        raw_list = body.get("output1") or body.get("output") or body.get("output2") or []

        # 응답 진단 로그 — rt_cd=0인데 항목=0이면 응답 키 목록도 출력
        logger.info(
            f"[rest] {market_name} 응답: rt_cd={rt_cd} msg_cd={msg_cd} "
            f"msg={msg1} 항목={len(raw_list)}"
        )
        if rt_cd == "0" and len(raw_list) == 0:
            logger.warning(
                f"[rest] {market_name} 응답 키 목록: {list(body.keys())} "
                f"| FID_INPUT_ISCD={input_iscd}"
            )

        result = []
        for item in raw_list:
            try:
                prdy_vol = int(item.get("prdy_vol", 0))
                acml_vol = int(item.get("acml_vol", 0))
                if prdy_vol <= 0 or acml_vol <= 0:
                    continue
                result.append({
                    "종목코드":   item.get("mksc_shrn_iscd", ""),
                    "종목명":     item.get("hts_kor_isnm", ""),
                    "현재가":     int(item.get("stck_prpr", 0)),
                    "등락률":     float(item.get("prdy_ctrt", 0.0)),
                    "누적거래량": acml_vol,
                    "전일거래량": prdy_vol,
                })
            except (ValueError, TypeError):
                continue

        logger.info(f"[rest] {market_name} 파싱 완료 — {len(result)}종목")
        return result

    except Exception as e:
        logger.warning(f"[rest] 거래량 순위 조회 실패 ({market_code}): {e}")
        return []

def get_rate_ranking(market_code: str) -> list[dict]:
    """
    KIS 등락률 순위 조회 (v2.9 신규 / v3.0 개편)
    tr_id: FHPST01700000
    URL:   /uapi/domestic-stock/v1/ranking/fluctuation

    market_code: "J" = 코스피, "Q" = 코스닥

    [v3.0 개편 내용]
    - 종목코드 필드 버그 수정: mksc_shrn_iscd → stck_shrn_iscd
    - 코스닥 (Q): 모든 노이즈 제외 (FID_TRGT_EXLS_CLS_CODE="1111111")
      관리·경고·위험·우선주·스팩·ETF·ETN/ELW 전부 제외
    - 코스피 (J): 중형(FID_BLNG_CLS_CODE="2") + 소형("3") 각각 호출 후
      종목코드 기준 중복 제거 → 대형주 사실상 제외
    - 등락률 범위 0~10% (FID_RSFL_RATE1="0", FID_RSFL_RATE2="10")
      → 이미 폭발한 상한가 제외, 초기 급등 종목 조기 포착
    - FID_COND_MRKT_DIV_CODE: 항상 "J" 고정 (rate API도 "J" 통일)
    - 결과 등락률 내림차순 정렬 후 반환

    반환값 규격: get_volume_ranking()과 동일
    """
    token = get_access_token()
    if not token:
        logger.warning("[rest] 토큰 없음 — 등락률 순위 조회 불가")
        return []

    market_name = "코스피" if market_code == "J" else "코스닥"
    input_iscd  = _MARKET_INPUT_ISCD.get(market_code, "0001")

    if market_code == "Q":
        # 코스닥: 단일 호출, 모든 노이즈 제외
        rows = _fetch_rate_once(token, market_name, input_iscd,
                                blng_cls="0", exls_cls="1111111")
    else:
        # 코스피: 중형 + 소형 각각 호출 후 합산 → 대형주 제외 효과
        mid   = _fetch_rate_once(token, market_name + "[중형]", input_iscd,
                                 blng_cls="2", exls_cls="0001111")
        small = _fetch_rate_once(token, market_name + "[소형]", input_iscd,
                                 blng_cls="3", exls_cls="0001111")
        seen, rows = set(), []
        for r in mid + small:
            key = r["종목코드"] or r["종목명"]
            if key and key not in seen:
                seen.add(key)
                rows.append(r)

    rows.sort(key=lambda x: x["등락률"], reverse=True)
    result = rows[:30]
    logger.info(f"[rest] {market_name} 등락률 파싱 완료 — {len(result)}종목")
    return result


def _fetch_rate_once(token: str, label: str, input_iscd: str,
                     blng_cls: str, exls_cls: str) -> list[dict]:
    """
    등락률 순위 단일 호출 헬퍼 (get_rate_ranking 내부 전용)
    등락률 범위: 0~10% (초기 급등 조기 포착)
    """
    url = f"{_BASE_URL}/uapi/domestic-stock/v1/ranking/fluctuation"
    headers = {
        "Authorization":  f"Bearer {token}",
        "appkey":         config.KIS_APP_KEY,
        "appsecret":      config.KIS_APP_SECRET,
        "tr_id":          "FHPST01700000",
        "custtype":       "P",
        "Content-Type":   "application/json; charset=utf-8",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE":  "J",       # 항상 "J" 고정 (v3.0)
        "FID_COND_SCR_DIV_CODE":   "20170",
        "FID_INPUT_ISCD":          input_iscd,
        "FID_RANK_SORT_CLS_CODE":  "0",        # 상승률순
        "FID_INPUT_CNT_1":         "0",
        "FID_PRC_CLS_CODE":        "0",
        "FID_INPUT_PRICE_1":       "0",
        "FID_INPUT_PRICE_2":       "0",
        "FID_VOL_CNT":             "100",
        "FID_TRGT_CLS_CODE":       "0",
        "FID_TRGT_EXLS_CLS_CODE":  exls_cls,
        "FID_DIV_CLS_CODE":        "0",
        "FID_BLNG_CLS_CODE":       blng_cls,
        "FID_RSFL_RATE1":          "0",        # 등락률 하한 0% (v3.0)
        "FID_RSFL_RATE2":          "10",       # 등락률 상한 10% (v3.0)
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        body   = resp.json()
        rt_cd  = body.get("rt_cd",  "?")
        msg_cd = body.get("msg_cd", "?")
        msg1   = body.get("msg1",   "")
        raw_list = body.get("output1") or body.get("output") or body.get("output2") or []

        logger.info(
            f"[rest] {label} 등락률 응답: rt_cd={rt_cd} msg_cd={msg_cd} "
            f"msg={msg1} 항목={len(raw_list)}"
        )

        result = []
        for item in raw_list:
            try:
                acml_vol = int(item.get("acml_vol", 0))
                prdy_vol = int(item.get("prdy_vol", 0))
                if acml_vol <= 0:
                    continue
                result.append({
                    "종목코드":   item.get("stck_shrn_iscd", ""),   # v3.0 버그수정
                    "종목명":     item.get("hts_kor_isnm", ""),
                    "현재가":     int(item.get("stck_prpr", 0)),
                    "등락률":     float(item.get("prdy_ctrt", 0.0)),
                    "누적거래량": acml_vol,
                    "전일거래량": prdy_vol if prdy_vol > 0 else 1,  # 0 방지
                })
            except (ValueError, TypeError):
                continue

        return result

    except Exception as e:
        logger.warning(f"[rest] 등락률 순위 단일 호출 실패 ({label}): {e}")
        return []
