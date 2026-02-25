"""
utils/date_utils.py
날짜 계산 전담
- PREV(전 거래일) 계산
- 휴장일 판단
- 날짜 포맷 변환

[수정이력]
- v1.0: 기본 구조
- v1.1: get_today() — KST 타임존 명시 적용 (Railway UTC 서버 대응)
        Railway 서버는 UTC 기준이라 datetime.now()가 UTC를 반환.
        한국 시간 07:30 KST = UTC 22:30 전날 → today가 하루 전으로 잡히는 버그 수정.
        해결: timezone(timedelta(hours=9)) 적용 → 항상 KST 기준 반환.
"""

from datetime import datetime, timedelta, timezone
from utils.logger import logger

KST = timezone(timedelta(hours=9))


def get_today() -> datetime:
    """현재 시각 KST로 반환. Railway는 UTC 서버이므로 반드시 KST 명시."""
    return datetime.now(KST)


def get_prev_trading_day(today: datetime = None) -> datetime:
    """
    전 거래일 반환
    월요일 -> 금요일(-3일)
    화~금  -> 전날(-1일)
    토/일  -> None (장 없음)
    공휴일은 별도 처리 없음 (추후 추가 가능)
    """
    if today is None:
        today = get_today()

    weekday = today.weekday()  # 0=월 ... 6=일

    if weekday == 0:   # 월요일
        return today - timedelta(days=3)
    elif 1 <= weekday <= 4:  # 화~금
        return today - timedelta(days=1)
    else:  # 토/일
        return None


def is_market_open(today: datetime = None) -> bool:
    """
    장 운영일 여부 반환
    주말이면 False
    공휴일은 pykrx로 추가 검증 (실패 시 True로 fallback)
    """
    if today is None:
        today = get_today()

    weekday = today.weekday()
    if weekday >= 5:  # 토=5, 일=6
        logger.info(f"[date_utils] 오늘은 주말({today.strftime('%A')}) — 봇 미실행")
        return False

    # pykrx로 공휴일 확인 (실패해도 진행)
    try:
        from pykrx import stock
        date_str = today.strftime("%Y%m%d")
        tickers = stock.get_market_ticker_list(date_str, market="KOSPI")
        if not tickers:
            logger.warning(f"[date_utils] {date_str} 공휴일 감지 — 봇 미실행")
            return False
    except Exception as e:
        logger.warning(f"[date_utils] pykrx 공휴일 확인 실패 ({e}) — 장 열린 것으로 간주")

    return True


def fmt_kr(dt: datetime) -> str:
    """datetime -> 'M월 DD일' 형식"""
    return f"{dt.month}월 {dt.day:02d}일"


def fmt_num(dt: datetime) -> str:
    """datetime -> 'YYYY-MM-DD' 형식"""
    return dt.strftime("%Y-%m-%d")


def fmt_ymd(dt: datetime) -> str:
    """datetime -> 'YYYYMMDD' 형식 (pykrx용)"""
    return dt.strftime("%Y%m%d")