"""
utils/state_manager.py
중복 알림 방지 전담 (장중봇용 쿨타임 관리)
- can_alert(): 알림 가능 여부 확인
- mark_alerted(): 알림 발송 후 기록
- reset(): 장 마감 후 초기화
"""

from datetime import datetime, timedelta
import config

_alerted: dict[str, datetime] = {}  # {종목코드: 알림 발송 시각}


def can_alert(ticker: str) -> bool:
    """True면 알림 가능, False면 쿨타임 중"""
    if ticker not in _alerted:
        return True
    elapsed = datetime.now() - _alerted[ticker]
    return elapsed > timedelta(minutes=config.ALERT_COOLTIME_MIN)


def mark_alerted(ticker: str) -> None:
    """알림 발송 직후 반드시 호출"""
    _alerted[ticker] = datetime.now()


def reset() -> None:
    """장 마감(15:30) 후 초기화"""
    _alerted.clear()
