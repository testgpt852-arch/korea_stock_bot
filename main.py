"""
main.py
봇 진입점 — 스케줄러 설정만 담당
로직 없음. 각 봇은 reports/ 폴더의 파일에서 실행.

실행: python main.py

[수정이력]
- v2.5: 장중봇 로그 메시지 수정, 휴장일 체크 추가
- v2.6: 컨테이너 재시작 시 장중이면 즉시 start() 호출
        기존: 09:00 스케줄만 존재 → 장중 재배포 시 당일 장중봇 미실행
        수정: 시작 시 _maybe_start_now() 호출
              → 현재 시각이 09:00~15:30 사이면 즉시 start_realtime_bot()
              → 이미 실행 중 방지: _realtime_started 플래그 관리
"""

import asyncio
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from utils.logger import logger
from utils.date_utils import is_market_open, get_today
import config

# 장중봇 중복 실행 방지 플래그
_realtime_started = False


async def run_morning_bot():
    """08:30 아침봇"""
    if not is_market_open(get_today()):
        logger.info("[main] 휴장일 — 아침봇 건너뜀")
        return
    from reports.morning_report import run
    await run()


async def run_closing_bot():
    """18:30 마감봇"""
    if not is_market_open(get_today()):
        logger.info("[main] 휴장일 — 마감봇 건너뜀")
        return
    from reports.closing_report import run
    await run()


async def start_realtime_bot():
    """09:00 장중봇 시작 — KIS REST 폴링"""
    global _realtime_started
    if _realtime_started:
        logger.info("[main] 장중봇 이미 실행 중 — 중복 시작 건너뜀")
        return
    if not is_market_open(get_today()):
        logger.info("[main] 휴장일 — 장중봇 건너뜀")
        return
    _realtime_started = True
    from reports.realtime_alert import start
    await start()


async def stop_realtime_bot():
    """15:30 장중봇 종료"""
    global _realtime_started
    _realtime_started = False
    from reports.realtime_alert import stop
    await stop()


async def _maybe_start_now():
    """
    컨테이너 시작 시 현재 시각이 장중(09:00~15:30)이면 즉시 장중봇 실행

    [v2.6 추가 이유]
    Railway 재배포 or 컨테이너 재시작이 장중(09:00~15:30)에 발생하면
    09:00 cron 스케줄은 이미 지나버려 당일 장중봇이 실행되지 않는 문제 해결.
    """
    if not is_market_open(get_today()):
        return

    now = datetime.now()
    market_open  = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)

    if market_open <= now < market_close:
        logger.info(f"[main] 장중 재시작 감지 ({now.strftime('%H:%M')}) — 장중봇 즉시 시작")
        await start_realtime_bot()
    else:
        logger.info(f"[main] 장외 시간 ({now.strftime('%H:%M')}) — 장중봇 대기 중")


async def main():
    config.validate_env()
    logger.info("=" * 40)
    logger.info("한국주식 봇 시작")
    logger.info("=" * 40)

    scheduler = AsyncIOScheduler(timezone="Asia/Seoul")

    # 아침봇
    scheduler.add_job(
        run_morning_bot, "cron",
        hour=7, minute=59,
        id="morning_bot_1"
    )
    scheduler.add_job(
        run_morning_bot, "cron",
        hour=8, minute=40,
        id="morning_bot_2"
    )

    # 장중봇 시작/종료
    scheduler.add_job(start_realtime_bot, "cron", hour=9,  minute=0,  id="rt_start")
    scheduler.add_job(stop_realtime_bot,  "cron", hour=15, minute=30, id="rt_stop")

    # 마감봇
    scheduler.add_job(
        run_closing_bot, "cron",
        hour=18, minute=30,
        id="closing_bot"
    )

    scheduler.start()
    logger.info("스케줄 등록 완료")
    logger.info("  아침봇: 매일 08:30 / 07:59")
    logger.info("  장중봇: 매일 09:00~15:30 (KIS REST 폴링)")
    logger.info("  마감봇: 매일 18:30")

    # 장중 재시작 감지 → 즉시 실행
    await _maybe_start_now()

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("봇 종료")
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
