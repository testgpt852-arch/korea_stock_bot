"""
main.py
봇 진입점 — 스케줄러 설정만 담당
로직 없음. 각 봇은 reports/ 폴더의 파일에서 실행.

실행: python main.py

[수정이력]
- v2.5: 장중봇 로그 메시지 수정, 휴장일 체크 추가
- v2.6: _maybe_start_now() 추가 — 장중 재배포 시 즉시 실행
- v2.6.1: _maybe_start_now() 시간 비교를 KST 기준으로 수정
          기존: datetime.now() → Railway 서버 UTC 반환 → 장중 판단 오류
          수정: datetime.now(ZoneInfo("Asia/Seoul")) → KST 기준 정확한 판단
- v3.3:  Phase 3 — DB init_db() 기동 시 1회 호출
         18:45 수익률 추적 배치(performance_tracker.run_batch) 스케줄 추가
         매주 월요일 아침봇 직후 주간 성과 리포트(weekly_report) 발송 스케줄 추가
- v3.4:  Phase 4 — 자동매매 강제청산 스케줄 추가
         14:50 run_force_close() — 미청산 포지션 전부 시장가 매도
         AUTO_TRADE_ENABLED=false 시 스케줄 등록 자체를 건너뜀
"""

import asyncio
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from utils.logger import logger
from utils.date_utils import is_market_open, get_today
import config

KST = timezone(timedelta(hours=9))   # UTC+9, 외부 패키지 불필요

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


async def run_performance_batch():
    """18:45 수익률 추적 배치 (Phase 3, v3.3)"""
    if not is_market_open(get_today()):
        logger.info("[main] 휴장일 — 수익률 배치 건너뜀")
        return
    loop = asyncio.get_event_loop()
    from tracking.performance_tracker import run_batch
    await loop.run_in_executor(None, run_batch)


async def run_weekly_report():
    """매주 월요일 아침봇 직후 주간 성과 리포트 (Phase 3, v3.3)"""
    now = datetime.now(KST)
    if now.weekday() != 0:   # 0 = 월요일
        return               # 월요일 아니면 조용히 패스
    if not is_market_open(get_today()):
        logger.info("[main] 휴장일 — 주간 리포트 건너뜀")
        return
    from reports.weekly_report import run
    await run()


async def run_principles_extraction():
    """
    매주 일요일 03:00 Trading Principles 추출 배치 (Phase 5, v3.5)
    trading_history → trading_principles DB 갱신.
    """
    from tracking.principles_extractor import run_weekly_extraction
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, run_weekly_extraction
        )
        logger.info(
            f"[main] 원칙 추출 완료 — 신규:{result['inserted']} "
            f"업데이트:{result['updated']} 총:{result['total_principles']}개"
        )
        # 텔레그램 요약 알림
        from notifiers import telegram_bot
        if result["total_principles"] > 0:
            msg = (
                f"🧠 매매 원칙 DB 업데이트\n"
                f"• 총 원칙: {result['total_principles']}개\n"
                f"• 신규: {result['inserted']}개 / 업데이트: {result['updated']}개"
            )
            await telegram_bot.send_async(msg)
    except Exception as e:
        logger.error(f"[main] 원칙 추출 실패: {e}")


async def run_force_close():
    """
    14:50 선택적 강제 청산 (Phase 4, v3.4 / v4.4 AI 기반 선택적 청산으로 업그레이드)
    수익 유망 종목은 유지, 손실/중립 종목은 즉시 청산.
    유지 종목은 15:20 final_close에서 최종 청산.
    AUTO_TRADE_ENABLED=false 이면 아무것도 하지 않음.
    """
    if not config.AUTO_TRADE_ENABLED:
        return
    if not is_market_open(get_today()):
        return

    loop = asyncio.get_event_loop()
    from traders.position_manager import force_close_all
    import notifiers.telegram_bot as telegram_bot

    closed_list = await loop.run_in_executor(None, force_close_all)
    if not closed_list:
        logger.info("[main] 선택적 강제청산 — 즉시 청산 대상 없음 (또는 전종목 유지)")
        return

    for closed in closed_list:
        try:
            msg = telegram_bot.format_trade_closed(closed)
            await telegram_bot.send_async(msg)
        except Exception as e:
            logger.warning(f"[main] 강제청산 알림 발송 실패: {e}")

    logger.info(f"[main] 선택적 강제청산 완료 — 즉시청산 {len(closed_list)}종목")


async def run_final_close():
    """
    [v4.4 신규] 15:20 최종 청산 — 14:50 '유지' 판정 종목 최종 청산.
    장 마감 10분 전으로 충분한 유동성 내 청산 가능.
    AUTO_TRADE_ENABLED=false 이면 아무것도 하지 않음.
    """
    if not config.AUTO_TRADE_ENABLED:
        return
    if not is_market_open(get_today()):
        return

    loop = asyncio.get_event_loop()
    from traders.position_manager import final_close_all
    import notifiers.telegram_bot as telegram_bot

    closed_list = await loop.run_in_executor(None, final_close_all)
    if not closed_list:
        logger.info("[main] 최종 청산 — 대상 없음 (이미 청산 완료)")
        return

    for closed in closed_list:
        try:
            msg = telegram_bot.format_trade_closed(closed)
            await telegram_bot.send_async(msg)
        except Exception as e:
            logger.warning(f"[main] 최종청산 알림 발송 실패: {e}")

    logger.info(f"[main] 최종 청산 완료 — {len(closed_list)}종목")


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

    [v2.6.1 수정]
    datetime.now(KST) 사용 — Railway 서버는 UTC이므로 반드시 KST 명시
    """
    if not is_market_open(get_today()):
        return

    now = datetime.now(KST)   # ← KST 명시 (UTC 오판 방지)
    market_open  = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)

    if market_open <= now < market_close:
        logger.info(f"[main] 장중 재시작 감지 ({now.strftime('%H:%M')} KST) — 장중봇 즉시 시작")
        await start_realtime_bot()
    else:
        logger.info(f"[main] 장외 시간 ({now.strftime('%H:%M')} KST) — 장중봇 대기 중")


async def main():
    config.validate_env()

    # Phase 3: DB 초기화 (테이블 없으면 생성)
    from tracking.db_schema import init_db
    init_db()
    logger.info("=" * 40)
    logger.info("한국주식 봇 시작")
    logger.info("=" * 40)

    scheduler = AsyncIOScheduler(timezone="Asia/Seoul")

    # 아침봇
    scheduler.add_job(run_morning_bot, "cron", hour=7,  minute=30, id="morning_bot_1")
    scheduler.add_job(run_morning_bot, "cron", hour=8,  minute=30, id="morning_bot_2")

    # 장중봇 시작/종료
    scheduler.add_job(start_realtime_bot, "cron", hour=9,  minute=0,  id="rt_start")
    scheduler.add_job(stop_realtime_bot,  "cron", hour=15, minute=30, id="rt_stop")

    # 마감봇
    scheduler.add_job(run_closing_bot, "cron", hour=18, minute=30, id="closing_bot")

    # Phase 3: 수익률 추적 배치 (v3.3)
    scheduler.add_job(run_performance_batch, "cron", hour=18, minute=45, id="perf_batch")

    # Phase 3: 주간 성과 리포트 — 매주 월요일 08:45 (아침봇 완료 후) (v3.3)
    scheduler.add_job(run_weekly_report, "cron", hour=8, minute=45, id="weekly_report")

    # Phase 4: 강제 청산 — 14:50 (v3.4 / v4.4 AI 선택적 청산으로 업그레이드)
    scheduler.add_job(run_force_close, "cron", hour=14, minute=50, id="force_close")
    # Phase 4: 최종 청산 — 15:20 (v4.4 신규: 14:50 '유지' 종목 최종 청산)
    scheduler.add_job(run_final_close, "cron", hour=15, minute=20, id="final_close")
    # v3.5 Phase 5: 매주 일요일 03:00 매매 원칙 추출 배치
    scheduler.add_job(
        run_principles_extraction, "cron",
        day_of_week="sun", hour=3, minute=0,
        id="principles_extract"
    )

    scheduler.start()
    logger.info("스케줄 등록 완료")
    logger.info("  아침봇: 매일 08:30 / 07:59")
    logger.info("  장중봇: 매일 09:00~15:30 (KIS REST 폴링)")
    logger.info("  마감봇: 매일 18:30")
    logger.info("  수익률배치: 매일 18:45 (Phase 3)")
    logger.info("  주간리포트: 매주 월요일 08:45 (Phase 3)")
    if config.AUTO_TRADE_ENABLED:
        logger.info(
            f"  강제청산: 매일 14:50 (Phase 4, 모드: {config.TRADING_MODE}) ✅ 활성"
        )
    else:
        logger.info("  강제청산: 매일 14:50 (Phase 4)\n  원칙추출: 매주 일요일 03:00 (Phase 5) ⏸ 비활성 (AUTO_TRADE_ENABLED=false)")

    # 장중 재시작 감지 → 즉시 실행 (KST 기준)
    await _maybe_start_now()

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("봇 종료")
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())