"""
reports/weekly_report.py
주간 성과 리포트 조립 + 발송 (Phase 3, v3.3 신규)

[실행 시점]
main.py 스케줄러 → 매주 월요일 08:30 아침봇 직후 run() 호출.
(월요일이 아닌 날은 main.py 에서 요일 체크 후 건너뜀)

[동작 흐름]
① performance_tracker.get_weekly_stats() — 지난 7일 성과 DB 조회
② telegram_bot.format_weekly_report(stats) — 메시지 포맷
③ telegram_bot.send_async(message) — 발송

[ARCHITECTURE 의존성]
weekly_report → tracking/performance_tracker  (DB 조회)
weekly_report → notifiers/telegram_bot        (포맷 + 발송)
weekly_report ← main.py  (월요일 08:30 cron)

[절대 금지 규칙 — ARCHITECTURE #18]
이 파일에서 pykrx / KIS REST / AI 호출 금지.
데이터 조회는 performance_tracker.get_weekly_stats() 에 위임.
"""

from utils.logger import logger
from utils.date_utils import is_market_open, get_today
import tracking.performance_tracker as performance_tracker
import notifiers.telegram_bot as telegram_bot


async def run() -> None:
    """
    주간 성과 리포트 실행 함수.
    main.py 에서 매주 월요일 아침봇 직후 호출.
    """
    today = get_today()
    if not is_market_open(today):
        logger.info("[weekly] 휴장일 — 주간 리포트 건너뜀")
        return

    logger.info("[weekly] 주간 성과 리포트 조립 중...")
    try:
        stats = performance_tracker.get_weekly_stats()
        if not stats:
            logger.warning("[weekly] 주간 통계 없음 — 발송 건너뜀 (데이터 부족)")
            return

        if stats.get("total_alerts", 0) == 0:
            logger.info("[weekly] 지난 주 알림 없음 — 발송 건너뜀")
            return

        message = telegram_bot.format_weekly_report(stats)
        await telegram_bot.send_async(message)
        logger.info(
            f"[weekly] 주간 리포트 발송 완료 — "
            f"알림 {stats['total_alerts']}건 / "
            f"트리거 {len(stats.get('trigger_stats', []))}종"
        )

    except Exception as e:
        logger.error(f"[weekly] 주간 리포트 실패: {e}")
