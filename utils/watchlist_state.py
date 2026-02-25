"""
utils/watchlist_state.py
아침봇 → 장중봇 간 WebSocket 워치리스트 공유 상태 (v3.1 신규)

[역할]
morning_report(08:30) 가 전날 수급·신호 데이터로 워치리스트를 생성해 저장.
realtime_alert(09:00) 시작 시 이 워치리스트로 WebSocket 구독 목록을 구성.

[ARCHITECTURE 의존성]
morning_report → watchlist_state (write)
realtime_alert → watchlist_state (read)
수집/분석/발송 로직 없음 — 상태 공유만
"""

from utils.logger import logger

# {종목코드: {"종목명": str, "전일거래량": int, "우선순위": int}}
_watchlist: dict[str, dict] = {}


def set_watchlist(stocks: dict[str, dict]) -> None:
    """아침봇에서 호출 — 워치리스트 저장 (장중봇 시작 전에 완료돼야 함)"""
    global _watchlist
    _watchlist = stocks
    logger.info(f"[watchlist] 워치리스트 저장 완료 — {len(_watchlist)}종목")


def get_watchlist() -> dict[str, dict]:
    """장중봇에서 호출 — 워치리스트 복사본 반환"""
    return _watchlist.copy()


def is_ready() -> bool:
    """아침봇이 워치리스트를 설정했는지 여부 (장중봇 시작 전 체크용)"""
    return bool(_watchlist)


def clear() -> None:
    """장 마감 후 초기화 (다음날 아침봇이 덮어쓰므로 선택적)"""
    global _watchlist
    _watchlist = {}
    logger.info("[watchlist] 워치리스트 초기화")
