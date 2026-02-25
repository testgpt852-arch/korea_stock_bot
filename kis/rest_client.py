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
    KIS 등락률 순위 조회 (v2.9 신규)
    tr_id: FHPST01700000
    URL:   /uapi/domestic-stock/v1/ranking/fluctuation

    market_code: "J" = 코스피, "Q" = 코스닥

    반환값 규격: get_volume_ranking()과 동일
    → volume_analyzer가 두 리스트를 그대로 합칠 수 있음

    [추가 배경]
    거래량 순위(TOP 30)에는 대형주·테마 대장주만 잡힘.
    거래량이 적어도 등락률이 높은 소형주(디모아형) 포착을 위해 추가.
    두 API를 합산하면 실질적으로 감지 커버리지가 2배로 확장됨.
    """
    token = get_access_token()
    if not token:
        logger.warning("[rest] 토큰 없음 — 등락률 순위 조회 불가")
        return []

    market_name = "코스피" if market_code == "J" else "코스닥"
    input_iscd  = _MARKET_INPUT_ISCD.get(market_code, "0001")
    logger.info(f"[rest] {market_name} 등락률 순위 조회 시작...")

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
        "FID_COND_MRKT_DIV_CODE":  market_code,   # 등락률API는 "J"/"Q" 모두 지원
        "FID_COND_SCR_DIV_CODE":   "20170",
        "FID_INPUT_ISCD":          input_iscd,
        "FID_RANK_SORT_CLS_CODE":  "0",            # 0: 상승률 순
        "FID_INPUT_CNT_1":         "0",
        "FID_PRC_CLS_CODE":        "0",
        "FID_INPUT_PRICE_1":       "0",
        "FID_INPUT_PRICE_2":       "0",
        "FID_VOL_CNT":             "30",
        "FID_TRGT_CLS_CODE":       "0",
        "FID_TRGT_EXLS_CLS_CODE":  "0",
        "FID_DIV_CLS_CODE":        "0",
        "FID_BLNG_CLS_CODE":       "0",
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
            f"[rest] {market_name} 등락률 응답: rt_cd={rt_cd} msg_cd={msg_cd} "
            f"msg={msg1} 항목={len(raw_list)}"
        )

        result = []
        for item in raw_list:
            try:
                prdy_vol = int(item.get("prdy_vol", 0))
                acml_vol = int(item.get("acml_vol", 0))
                if acml_vol <= 0:
                    continue
                result.append({
                    "종목코드":   item.get("mksc_shrn_iscd", ""),
                    "종목명":     item.get("hts_kor_isnm", ""),
                    "현재가":     int(item.get("stck_prpr", 0)),
                    "등락률":     float(item.get("prdy_ctrt", 0.0)),
                    "누적거래량": acml_vol,
                    "전일거래량": prdy_vol if prdy_vol > 0 else 1,  # 0 방지
                })
            except (ValueError, TypeError):
                continue

        logger.info(f"[rest] {market_name} 등락률 파싱 완료 — {len(result)}종목")
        return result

    except Exception as e:
        logger.warning(f"[rest] 등락률 순위 조회 실패 ({market_code}): {e}")
        return []
