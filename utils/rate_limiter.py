"""
utils/rate_limiter.py
KIS API 호출 유량 제한 (v3.2 신규)

[출처]
python-kis/pykis/utils/rate_limit.py 를 우리 봇 구조에 맞게 이식.
원본: threading.Lock 기반 (python-kis는 threading 기반)
우리 봇: asyncio 기반이지만 rest_client.py 함수들은 동기 함수 → executor에서 실행
→ threading.Lock 그대로 이식 가능. asyncio 이벤트 루프 블록 없음.

[KIS 공식 Rate Limit]
실전:  초당 20회 → 여유 1회 제외 → 19회  (REAL_API_REQUEST_PER_SECOND = 20 - 1)
모의:  초당  2회                              (VIRTUAL_API_REQUEST_PER_SECOND = 2)

[사용법]
from utils.rate_limiter import kis_rate_limiter
kis_rate_limiter.acquire()  # API 호출 전 반드시 호출

[ARCHITECTURE 의존성]
rate_limiter → kis/rest_client (import)
"""

import time
from threading import Lock
from utils.logger import logger
import config


class RateLimiter:
    """
    KIS API 호출 유량 제한기 (python-kis 구조 참조)
    Thread-safe Lock 기반 — asyncio executor 환경에서 안전
    """

    __slots__ = ["rate", "period", "_count", "_last", "_lock"]

    def __init__(self, rate: int, period: float = 1.0):
        """
        Args:
            rate:   기간(period)내 최대 호출 횟수
            period: 기간 (초, 기본값 1.0)
        """
        self.rate   = rate
        self.period = period
        self._count = 0
        self._last  = 0.0
        self._lock  = Lock()

    @property
    def count(self) -> int:
        """현재 기간 내 누적 호출 횟수"""
        with self._lock:
            return 0 if time.time() - self._last > self.period else self._count

    def acquire(self, blocking: bool = True) -> bool:
        """
        호출 허가 획득.
        blocking=True: 한도 초과 시 다음 기간까지 대기 후 허가 (기본값)
        blocking=False: 한도 초과 시 즉시 False 반환

        Returns:
            True  — 호출 허가
            False — 한도 초과 (blocking=False일 때만 반환)
        """
        with self._lock:
            now = time.time()
            if now - self._last > self.period:
                self._count = 0
                self._last  = now

            if self._count >= self.rate:
                if not blocking:
                    return False
                # 남은 기간만큼 대기
                wait = self.period - (time.time() - self._last) + 0.05
                logger.debug(f"[rate_limiter] 한도({self.rate}회/s) 도달 — {wait:.2f}초 대기")
                time.sleep(max(wait, 0))
                self._count = 0
                self._last  = time.time()

            self._count += 1
            return True


# ── 싱글톤 인스턴스 ───────────────────────────────────────────
# kis/rest_client.py 에서 import해서 사용
#
# [v3.2 버그 수정] 기존: 항상 REAL(19req/s) 고정
# 수정: TRADING_MODE에 따라 VTS(2req/s) / REAL(19req/s) 동적 선택
# → VTS 모의투자 모드에서 429 에러 방지
_rate = (
    config.KIS_RATE_LIMIT_VIRTUAL
    if getattr(config, "TRADING_MODE", "VTS") == "VTS"
    else config.KIS_RATE_LIMIT_REAL
)
kis_rate_limiter = RateLimiter(rate=_rate, period=1.0)
logger.debug(f"[rate_limiter] 초기화: TRADING_MODE={getattr(config, 'TRADING_MODE', 'VTS')} → {_rate}req/s")