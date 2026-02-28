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
         매주 월요일 아침봇 직후 주간 성과 리포트(weekly_report) 발송 스케줄 추가
- v12.0: 마감봇(18:30) 폐지 — closing_report.py 삭제
         수익률배치 18:45 → 15:45 (장 마감 직후)로 이동
- v3.4:  Phase 4 — 자동매매 강제청산 스케줄 추가
         14:50 run_force_close() — 미청산 포지션 전부 시장가 매도
         AUTO_TRADE_ENABLED=false 시 스케줄 등록 자체를 건너뜀
- v6.0:  [이슈④] TRADING_MODE=REAL 전환 안전장치 — 시작 시 감지 + 텔레그램 확인 + 5분 딜레이
         [5번/P1] 기억 압축 배치 — 매주 일요일 03:30 스케줄 추가
- v10.0: [Phase 2] 지정학 뉴스 수집 배치 추가
- v12.0 Step 7: data_collector.run() 도입
         06:00 data_collector.run() — 모든 수집기 asyncio.gather() 병렬 실행
         기존 run_geopolitics_collect() / run_event_calendar_collect() 제거
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

# v12.0 Step 7: data_collector가 모든 캐시를 관리
# _geopolitics_cache / _event_calendar_cache는 data_collector.get_cache() 경유로 접근
# 하위 호환용 별칭 (data_collector.run() 전 빈 값)
_geopolitics_cache:    list[dict] = []
_event_calendar_cache: list[dict] = []


async def run_morning_bot():
    """07:30 / 08:30 아침봇"""
    if not is_market_open(get_today()):
        logger.info("[main] 휴장일 — 아침봇 건너뜀")
        return
    from reports.morning_report import run
    from collectors.data_collector import get_cache, is_fresh

    # [v12.0 Step 7] data_collector 캐시 활용
    # 06:00 data_collector.run() 완료 후 캐시 신선도 확인
    dc = get_cache()
    if not is_fresh(max_age_minutes=180):
        logger.warning("[main] data_collector 캐시 없음 또는 오래됨 — 아침봇이 직접 수집")
        dc = {}

    await run(
        geopolitics_raw  = dc.get("news_global_rss",           []),
        event_cache      = dc.get("event_calendar",             []),
        sector_etf_data  = dc.get("sector_etf_data",           []) or None,
        short_data       = dc.get("short_data",                 []) or None,
        # [v12.0 Step 7] 마감강도·거래량급증·자금집중을 morning에도 전달
        # (data_collector가 06:00에 수집한 전날 데이터 재활용)
        closing_strength_result   = dc.get("closing_strength_result",   []) or None,
        volume_surge_result       = dc.get("volume_surge_result",       []) or None,
        fund_concentration_result = dc.get("fund_concentration_result", []) or None,
    )



async def run_performance_batch():
    """15:45 수익률 추적 배치 — 장 마감 직후 (Phase 3, v3.3 / v12.0: 18:45→15:45 이동)"""
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
        from telegram import sender as telegram_bot
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
    import telegram.sender as telegram_bot

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
    import telegram.sender as telegram_bot

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



async def _check_real_mode_safety():
    """
    [v6.0 이슈④] TRADING_MODE=REAL 전환 안전장치.
    봇 시작 시 REAL 모드 감지 → 텔레그램 경고 + 5분 딜레이 후 자동매매 활성화.
    REAL_MODE_CONFIRM_ENABLED=false 이면 건너뜀.

    목적: Railway Variables에서 VTS→REAL 변경 즉시 실매매 발생하는 위험 방지.
    절차: ① 텔레그램 경고 발송 → ② REAL_MODE_CONFIRM_DELAY_SEC(기본 300초) 대기
         → ③ 대기 완료 후 "REAL 모드 활성화 완료" 알림 → 이후 자동매매 가능
    """
    if not config.AUTO_TRADE_ENABLED:
        return
    if config.TRADING_MODE != "REAL":
        return
    if not config.REAL_MODE_CONFIRM_ENABLED:
        logger.warning("[main] REAL 모드 활성화 — 안전장치 비활성(REAL_MODE_CONFIRM_ENABLED=false)")
        return

    delay = config.REAL_MODE_CONFIRM_DELAY_SEC
    from telegram import sender as telegram_bot

    warning_msg = (
        f"⚠️ <b>REAL 실전 자동매매 전환 감지</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📍 TRADING_MODE=REAL 감지됨\n"
        f"⏳ <b>{delay // 60}분 후</b> 자동매매가 활성화됩니다.\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"❌ 취소하려면 지금 바로 컨테이너를 재시작하거나\n"
        f"   TRADING_MODE=VTS 로 변경 후 재배포하세요."
    )
    try:
        await telegram_bot.send_async(warning_msg)
        logger.warning(f"[main] REAL 모드 전환 안전장치 — {delay}초({delay//60}분) 대기 시작")
    except Exception as e:
        logger.error(f"[main] REAL 모드 경고 알림 실패: {e}")

    await asyncio.sleep(delay)

    activate_msg = (
        f"✅ <b>REAL 실전 자동매매 활성화 완료</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"계좌: {config.KIS_ACCOUNT_NO or 'N/A'}\n"
        f"모드: 실전 ({delay // 60}분 대기 완료)\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⚠️ 이제부터 실제 매수/매도가 실행됩니다."
    )
    try:
        await telegram_bot.send_async(activate_msg)
    except Exception as e:
        logger.error(f"[main] REAL 모드 활성화 알림 실패: {e}")
    logger.warning("[main] REAL 모드 자동매매 활성화 완료")


async def run_memory_compression():
    """
    [v6.0 5번/P1] 매주 일요일 03:30 기억 압축 배치.
    trading_journal 3계층 압축 (Layer1: 원문 → Layer2: 요약 → Layer3: 핵심만).
    원칙 추출(03:00) 완료 후 실행 (30분 간격으로 충분한 여유).
    """
    from tracking.memory_compressor import run_compression
    try:
        result = await asyncio.get_event_loop().run_in_executor(None, run_compression)
        logger.info(
            f"[main] 기억 압축 완료 — "
            f"Layer1→2: {result.get('compressed_l1', 0)}건, "
            f"Layer2→3: {result.get('compressed_l2', 0)}건, "
            f"정리: {result.get('cleaned', 0)}건"
        )
        from telegram import sender as telegram_bot
        if result.get('compressed_l1', 0) + result.get('compressed_l2', 0) > 0:
            msg = (
                f"🗜️ 기억 압축 완료\n"
                f"• Layer1→2 (요약): {result.get('compressed_l1', 0)}건\n"
                f"• Layer2→3 (핵심): {result.get('compressed_l2', 0)}건\n"
                f"• 오래된 항목 정리: {result.get('cleaned', 0)}건"
            )
            await telegram_bot.send_async(msg)
    except Exception as e:
        logger.error(f"[main] 기억 압축 실패 (비치명적): {e}")


async def run_data_collector():
    """
    [v12.0 Step 7] 06:00 단일 실행 — 모든 수집기 병렬 실행.
    기존 run_geopolitics_collect() + run_event_calendar_collect() 대체.

    수집 결과는 data_collector._cache에 저장.
    아침봇(08:30)은 data_collector.get_cache()로 캐시를 읽어 사용.
    수집 실패 시 비치명적 — 아침봇이 직접 재수집 fallback.
    """
    try:
        from collectors.data_collector import run as dc_run
        cache = await dc_run()
        logger.info(
            f"[main] data_collector 완료 — "
            f"총점:{cache.get('score_summary',{}).get('total_score',0)} | "
            f"성공:{sum(cache.get('success_flags',{}).values())}/"
            f"{len(cache.get('success_flags',{}))}"
        )
    except Exception as e:
        logger.error(f"[main] data_collector 실패 (비치명적): {e}")


async def main():
    config.validate_env()

    # Phase 3: DB 초기화 (테이블 없으면 생성)
    from tracking.db_schema import init_db
    init_db()
    logger.info("=" * 40)
    logger.info("한국주식 봇 시작")
    logger.info("=" * 40)

    scheduler = AsyncIOScheduler(timezone="Asia/Seoul")

    # ── 06:00 data_collector — 모든 수집기 병렬 실행 (v12.0 Step 7) ──────────────
    # 기존: run_geopolitics_collect(06:00) + run_event_calendar_collect(06:30) 분리
    # 변경: data_collector.run() 단일 스케줄로 통합
    scheduler.add_job(run_data_collector, "cron", hour=6, minute=0, id="data_collector")
    logger.info("[main] data_collector 스케줄 등록 — 06:00 (병렬 수집)")

    scheduler.add_job(run_morning_bot, "cron", hour=7,  minute=30, id="morning_bot_1")
    scheduler.add_job(run_morning_bot, "cron", hour=8,  minute=30, id="morning_bot_2")

    # 장중봇 시작/종료
    scheduler.add_job(start_realtime_bot, "cron", hour=9,  minute=0,  id="rt_start")
    scheduler.add_job(stop_realtime_bot,  "cron", hour=15, minute=30, id="rt_stop")

    # Phase 3: 수익률 추적 배치 — 15:45 장 마감 직후 (v12.0: 18:45→15:45 이동)
    scheduler.add_job(run_performance_batch, "cron", hour=15, minute=45, id="perf_batch")

    # Phase 3: 주간 성과 리포트 — 매주 월요일 08:45 (아침봇 완료 후) (v3.3)
    # [v10.7 이슈 #7] day_of_week='mon' 추가 — 기존에 누락되어 매일 실행됨
    scheduler.add_job(run_weekly_report, "cron", day_of_week="mon", hour=8, minute=45, id="weekly_report")

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

    # [v8.0 버그수정] 기억 압축 배치 스케줄 누락 수정
    # v6.0에서 run_memory_compression 함수는 정의됐으나 scheduler.add_job이 누락됨
    # → 매주 일요일 03:30 등록 (원칙 추출 03:00 완료 후 30분 여유)
    scheduler.add_job(
        run_memory_compression, "cron",
        day_of_week="sun", hour=3, minute=30,
        id="memory_compress"
    )

    scheduler.start()
    logger.info("스케줄 등록 완료")
    logger.info("  data_collector: 매일 06:00 (병렬 수집 — Step 7)")
    logger.info("  아침봇: 매일 07:30 / 08:30")
    logger.info("  장중봇: 매일 09:00~15:30 (KIS REST 폴링)")
    logger.info("  수익률배치: 매일 15:45 (장 마감 직후)")
    logger.info("  주간리포트: 매주 월요일 08:45")
    if config.AUTO_TRADE_ENABLED:
        logger.info(
            f"  강제청산: 매일 14:50 (Phase 4, 모드: {config.TRADING_MODE}) ✅ 활성"
        )
    else:
        logger.info("  강제청산: 매일 14:50 (Phase 4)\n  원칙추출: 매주 일요일 03:00 (Phase 5) ⏸ 비활성 (AUTO_TRADE_ENABLED=false)")

    # [v5.0 Phase 5] 텔레그램 인터랙티브 명령어 핸들러 백그라운드 시작
    # /status, /holdings, /principles 명령어 처리
    try:
        from telegram.commands import start_interactive_handler
        asyncio.create_task(start_interactive_handler())
        logger.info("  인터랙티브 핸들러: /status /holdings /principles (Phase 5) ✅")
    except Exception as e:
        logger.warning(f"  인터랙티브 핸들러 시작 실패 (비치명적): {e}")

    # [v6.0 이슈④] REAL 모드 전환 안전장치 — 감지 시 텔레그램 경고 + 딜레이 대기
    await _check_real_mode_safety()

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