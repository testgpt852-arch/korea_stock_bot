"""
main.py
봇 진입점 — 스케줄러 설정만 담당
로직 없음. 각 봇은 reports/ 폴더의 파일에서 실행.

실행: python main.py
"""

import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from utils.logger import logger
from utils.date_utils import is_market_open, get_today
import config


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
    if not is_market_open(get_today()):
        logger.info("[main] 휴장일 — 장중봇 건너뜀")
        return
    from reports.realtime_alert import start
    await start()


async def stop_realtime_bot():
    """15:30 장중봇 종료"""
    from reports.realtime_alert import stop
    await stop()


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

    # 마감봇
    scheduler.add_job(
        run_closing_bot, "cron",
        hour=18, minute=30,
        id="closing_bot"
    )

    # 장중봇 시작/종료
    scheduler.add_job(start_realtime_bot, "cron", hour=9,  minute=0,  id="rt_start")
    scheduler.add_job(stop_realtime_bot,  "cron", hour=15, minute=30, id="rt_stop")

    scheduler.start()
    logger.info("스케줄 등록 완료")
    logger.info("  아침봇: 매일 08:30 / 07:59")
    logger.info("  장중봇: 매일 09:00~15:30 (KIS REST 폴링)")
    logger.info("  마감봇: 매일 18:30")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("봇 종료")
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
