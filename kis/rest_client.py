"""
kis/rest_client.py
KIS REST API 호출 전담

[ARCHITECTURE 의존성]
rest_client → auth.py

[수정이력]
- v2.5: get_volume_ranking() 추가
- v2.5.1: get_volume_ranking() API URL 경로 수정
          기존: /uapi/domestic-stock/v1/ranking/volume        → 404
          수정: /uapi/domestic-stock/v1/quotations/volume-rank → 정상
          Content-Type 헤더 추가 (KIS API 필수)
"""

import requests
from utils.logger import logger
from kis.auth import get_access_token
import config

_BASE_URL = "https://openapi.koreainvestment.com:9443"


def get_stock_price(ticker: str) -> dict:
    """
    단일 종목 현재가 조회

    반환: {"현재가": int, "등락률": float, "거래량": int} or {}
    """
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
    KIS 거래량 순위 조회 (v2.5)
    tr_id: FHPST01710000
    URL:   /uapi/domestic-stock/v1/quotations/volume-rank  ← v2.5.1 수정

    Args:
        market_code: "J" = 코스피, "Q" = 코스닥

    반환: list[dict]
        [{"종목코드": str, "종목명": str, "등락률": float,
          "누적거래량": int, "전일거래량": int, "현재가": int}, ...]
    """
    token = get_access_token()
    if not token:
        logger.warning("[rest] 토큰 없음 — 거래량 순위 조회 불가")
        return []

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
        "FID_COND_MRKT_DIV_CODE":   market_code,   # J=코스피, Q=코스닥
        "FID_COND_SCR_DIV_CODE":    "20171",
        "FID_INPUT_ISCD":           "0000",         # 전 종목
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
        raw_list = resp.json().get("output1", [])

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

        logger.debug(f"[rest] 거래량 순위 {market_code} — {len(result)}종목 수신")
        return result

    except Exception as e:
        logger.warning(f"[rest] 거래량 순위 조회 실패 ({market_code}): {e}")
        return []
