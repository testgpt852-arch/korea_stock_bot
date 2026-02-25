"""
reports/realtime_alert.py
장중봇 실행 전담 (09:00 시작 / 15:30 종료)

[수정이력]
- v2.5:   KIS REST 폴링 방식 (init_prev_volumes 제거)
- v2.5.2: 폴링 사이클 시작/완료 로그 추가 (진단용)
- v2.8:   폴링 조건 로그 — PRICE_CHANGE_MIN/VOLUME_SPIKE_RATIO → PRICE_DELTA_MIN/VOLUME_DELTA_MIN
          1차 알림 로그 — 직전대비(1분 추가 상승률) 추가 표시
"""

import asyncio
from utils.logger import logger
from utils.state_manager import can_alert, mark_alerted, reset as reset_alerts
import analyzers.volume_analyzer as volume_analyzer
import analyzers.ai_analyzer     as ai_analyzer
import notifiers.telegram_bot    as telegram_bot
import config

_poll_task: asyncio.Task | None = None


async def start() -> None:
    global _poll_task
    logger.info("[realtime] 장중봇 시작 — KIS REST 폴링 (전 종목 실시간)")
    _poll_task = asyncio.create_task(_poll_loop())
    logger.info(
        f"[realtime] 폴링 루프 시작 ✅  "
        f"간격: {config.POLL_INTERVAL_SEC}초 / "
        f"조건: 1분+{config.PRICE_DELTA_MIN}% & 1분거래량{config.VOLUME_DELTA_MIN}% "
        f"× {config.CONFIRM_CANDLES}회 연속"
    )


async def stop() -> None:
    global _poll_task
    logger.info("[realtime] 장중봇 종료 시작")
    if _poll_task and not _poll_task.done():
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass
    _poll_task = None
    volume_analyzer.reset()
    reset_alerts()
    logger.info("[realtime] 장중봇 종료 완료 ✅")


async def _poll_loop() -> None:
    logger.info("[realtime] 폴링 루프 진입")
    cycle = 0

    while True:
        try:
            cycle += 1
            logger.info(f"[realtime] 폴링 사이클 #{cycle} 시작")

            results = await asyncio.get_event_loop().run_in_executor(
                None, volume_analyzer.poll_all_markets
            )

            logger.info(f"[realtime] 폴링 사이클 #{cycle} 완료 — 조건충족 {len(results)}종목")

            for analysis in results:
                ticker = analysis["종목코드"]
                if not can_alert(ticker):
                    continue
                mark_alerted(ticker)
                await _dispatch_alerts(analysis)

        except asyncio.CancelledError:
            logger.info("[realtime] 폴링 루프 종료 (CancelledError)")
            break
        except Exception as e:
            logger.warning(f"[realtime] 폴링 오류: {e}")

        await asyncio.sleep(config.POLL_INTERVAL_SEC)


async def _dispatch_alerts(analysis: dict) -> None:
    msg_1st = telegram_bot.format_realtime_alert(analysis)
    await telegram_bot.send_async(msg_1st)
    logger.info(
        f"[realtime] 1차 알림: {analysis['종목명']}  "
        f"+{analysis['등락률']:.1f}%(누적)  1분+{analysis['직전대비']:.1f}%  "
        f"1분거래량:{analysis['거래량배율']:.1f}배"
    )
    asyncio.create_task(_send_ai_followup(analysis))


async def _send_ai_followup(analysis: dict) -> None:
    try:
        ai_result = ai_analyzer.analyze_spike(analysis)
        msg_2nd   = telegram_bot.format_realtime_alert_ai(analysis, ai_result)
        await telegram_bot.send_async(msg_2nd)
        logger.info(f"[realtime] 2차 AI 알림: {analysis['종목명']} → {ai_result.get('판단', 'N/A')}")
    except Exception as e:
        logger.warning(f"[realtime] 2차 AI 알림 실패: {e}")
