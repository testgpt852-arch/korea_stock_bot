"""
kis/rest_client.py
KIS REST API 호출 전담

[수정이력]
- v2.5:   get_volume_ranking() 추가
- v2.5.1: URL 수정 → /uapi/domestic-stock/v1/quotations/volume-rank
- v2.5.2: logger.debug → logger.info (Railway 로그 가시성 확보)
          응답 rt_cd/msg_cd 진단 로그 추가
"""

import requests
from utils.logger import logger
from kis.auth import get_access_token
import config

_BASE_URL = "https://openapi.koreainvestment.com:9443"


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
    """
    token = get_access_token()
    if not token:
        logger.warning("[rest] 토큰 없음 — 거래량 순위 조회 불가")
        return []

    market_name = "코스피" if market_code == "J" else "코스닥"
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
        "FID_COND_MRKT_DIV_CODE":   market_code,
        "FID_COND_SCR_DIV_CODE":    "20171",
        "FID_INPUT_ISCD":           "0000",
        "FID_DIV_CLS_CODE":         "0",
        "FID_BLNG_CLS_CODE":        "0",
        "FID_TRGT_CLS_CODE":        "111111111",
        "FID_TRGT_EXLS_CLS_CODE":   "000000",
        "FID_INPUT_PRICE_1":        "",
        "FID_INPUT_PRICE_2":        "",
        "FID_VOL_CNT":              "100",
        "FID_INPUT_DATE_1":         "",
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        body     = resp.json()
        raw_list = body.get("output1", [])
        rt_cd    = body.get("rt_cd",  "?")
        msg_cd   = body.get("msg_cd", "?")
        msg1     = body.get("msg1",   "")

        # 응답 진단 로그 (정상 확인 후 제거 가능)
        logger.info(f"[rest] {market_name} 응답: rt_cd={rt_cd} msg_cd={msg_cd} msg={msg1} 항목={len(raw_list)}")

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
