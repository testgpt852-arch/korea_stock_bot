"""
kis/auth.py
KIS API 토큰 발급·갱신 전담 (REST 방식 — WebSocket 아님)

[수정이력]
- v3.4: get_vts_access_token() 추가 — 모의투자(VTS) 전용 토큰 발급
        VTS Base URL: openapivts.koreainvestment.com:29443
        실전 토큰 캐시와 완전 분리 관리

[ARCHITECTURE 의존성]
auth.py → kis/websocket_client, kis/rest_client, kis/order_client
"""

import requests
from datetime import datetime, timedelta
from utils.logger import logger
import config


_token_cache: dict = {
    "access_token":  None,
    "expires_at":    None,
}

# v3.4: VTS 모의투자 전용 토큰 캐시 (실전과 완전 분리)
_vts_token_cache: dict = {
    "access_token":  None,
    "expires_at":    None,
}

_VTS_BASE_URL  = "https://openapivts.koreainvestment.com:29443"
_REAL_BASE_URL = "https://openapi.koreainvestment.com:9443"


def get_access_token() -> str | None:
    """
    유효한 실전 액세스 토큰 반환
    만료 5분 전 자동 갱신, 최초 호출 시 발급
    """
    if not _is_token_valid(_token_cache):
        _refresh_token(_token_cache, _REAL_BASE_URL,
                       config.KIS_APP_KEY, config.KIS_APP_SECRET)
    return _token_cache.get("access_token")


def get_vts_access_token() -> str | None:
    """
    유효한 모의투자(VTS) 액세스 토큰 반환 (v3.4 신규)
    실전 토큰과 완전 분리된 캐시 사용.
    만료 5분 전 자동 갱신, 최초 호출 시 발급.
    """
    if not _is_token_valid(_vts_token_cache):
        _refresh_token(_vts_token_cache, _VTS_BASE_URL,
                       config.KIS_VTS_APP_KEY, config.KIS_VTS_APP_SECRET)
    return _vts_token_cache.get("access_token")


# ── 내부 헬퍼 ─────────────────────────────────────────────────

def _is_token_valid(cache: dict) -> bool:
    """토큰 유효 여부 (만료 5분 전까지만 유효로 처리)"""
    token   = cache.get("access_token")
    expires = cache.get("expires_at")
    if not token or not expires:
        return False
    return datetime.now() < expires - timedelta(minutes=5)


def _refresh_token(cache: dict, base_url: str,
                   app_key: str | None, app_secret: str | None) -> None:
    """KIS OAuth 토큰 발급 (REST)"""
    if not app_key or not app_secret:
        logger.warning("[auth] KIS 키 미설정 — 토큰 발급 불가")
        return

    url  = f"{base_url}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey":     app_key,
        "appsecret":  app_secret,
    }

    label = "VTS" if "vts" in base_url else "REAL"
    try:
        resp = requests.post(url, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("access_token"):
            cache["access_token"] = data["access_token"]
            # 만료 시각: 현재 + 24시간 (KIS 기본 만료)
            cache["expires_at"] = datetime.now() + timedelta(hours=24)
            logger.info(f"[auth] KIS {label} 토큰 발급 완료")
        else:
            logger.error(f"[auth] {label} 토큰 발급 실패: {data}")
    except Exception as e:
        logger.error(f"[auth] {label} 토큰 발급 오류: {e}")
