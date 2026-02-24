"""
kis/auth.py
KIS API 토큰 발급·갱신 전담 (REST 방식 — WebSocket 아님)

[ARCHITECTURE 의존성]
auth.py → kis/websocket_client, kis/rest_client
"""

import requests
from datetime import datetime, timedelta
from utils.logger import logger
import config


_token_cache: dict = {
    "access_token":  None,
    "expires_at":    None,
}


def get_access_token() -> str | None:
    """
    유효한 액세스 토큰 반환
    만료 5분 전 자동 갱신, 최초 호출 시 발급
    """
    if not _is_token_valid():
        _refresh_token()
    return _token_cache.get("access_token")


def _is_token_valid() -> bool:
    """토큰 유효 여부 (만료 5분 전까지만 유효로 처리)"""
    token   = _token_cache.get("access_token")
    expires = _token_cache.get("expires_at")
    if not token or not expires:
        return False
    return datetime.now() < expires - timedelta(minutes=5)


def _refresh_token() -> None:
    """KIS OAuth 토큰 발급 (REST)"""
    if not config.KIS_APP_KEY or not config.KIS_APP_SECRET:
        logger.warning("[auth] KIS 키 미설정 — 토큰 발급 불가")
        return

    url  = "https://openapi.koreainvestment.com:9443/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey":     config.KIS_APP_KEY,
        "appsecret":  config.KIS_APP_SECRET,
    }

    try:
        resp = requests.post(url, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("access_token"):
            _token_cache["access_token"] = data["access_token"]
            # 만료 시각: 현재 + 24시간 (KIS 기본 만료)
            _token_cache["expires_at"] = datetime.now() + timedelta(hours=24)
            logger.info("[auth] KIS 토큰 발급 완료")
        else:
            logger.error(f"[auth] 토큰 발급 실패: {data}")
    except Exception as e:
        logger.error(f"[auth] 토큰 발급 오류: {e}")
