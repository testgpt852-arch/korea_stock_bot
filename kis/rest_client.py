"""
kis/rest_client.py
KIS REST API 호출 전담

[ARCHITECTURE 의존성]
rest_client → auth.py
"""

import requests
from utils.logger import logger
from kis.auth import get_access_token
import config

_BASE_URL = "https://openapi.koreainvestment.com:9443"


def get_stock_price(ticker: str) -> dict:
    """
    현재가 조회 (장중봇 보조)

    반환: dict {"현재가": int, "등락률": float, "거래량": int} or {}
    """
    token = get_access_token()
    if not token:
        return {}

    url = f"{_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {
        "Authorization": f"Bearer {token}",
        "appkey":        config.KIS_APP_KEY,
        "appsecret":     config.KIS_APP_SECRET,
        "tr_id":         "FHKST01010100",
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
