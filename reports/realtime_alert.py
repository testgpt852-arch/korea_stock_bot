"""
reports/realtime_alert.py
장중봇 실행 전담 (09:00 시작 / 15:30 종료)

[ARCHITECTURE 의존성]
realtime_alert → volume_analyzer, state_manager, ai_analyzer, telegram_bot

🚨 KIS WebSocket 규칙 (ARCHITECTURE.md)
   이 파일은 KIS WebSocket을 사용하지 않음 (v2.4+)
   WebSocket 연결/구독/종료 코드 없음 → 차단 위험 없음

[수정이력]
- v2.3: subscribe() 호출 누락 버그 수정
- v2.4: pykrx REST 폴링 방식으로 전환
- v2.5: 데이터 소스 pykrx → KIS REST 실시간으로 전환
        init_prev_volumes() 호출 제거
        (KIS 응답에 전일거래량 포함 → 사전 로딩 불필요)
"""

import asyncio
from utils.logger import logger
from utils.state_manager import can_alert, mark_alerted, reset as reset_alerts
import analyzers.volume_analyzer as volume_analyzer
import analyzers.ai_analyzer     as ai_analyzer
import notifiers.telegram_bot    as telegram_bot
import config

# 폴링 태스크 핸들 (stop()에서 취소)
_poll_task: asyncio.Task | None = None


# ── 장중봇 시작 (09:00 — 1회만) ──────────────────────────────

async def start() -> None:
    """
    장중봇 시작
    main.py AsyncIOScheduler에서 09:00에 1회만 호출

    v2.5: KIS WebSocket 없음. init_prev_volumes() 없음.
          KIS REST 폴링 루프만 시작.
    """
    global _poll_task

    logger.info("[realtime] 장중봇 시작 — KIS REST 폴링 (전 종목 실시간)")

    # 폴링 루프 시작 (백그라운드 태스크)
    _poll_task = asyncio.create_task(_poll_loop())
    logger.info(
        f"[realtime] 폴링 루프 시작 ✅  "
        f"간격: {config.POLL_INTERVAL_SEC}초 / "
        f"조건: +{config.PRICE_CHANGE_MIN}% & 거래량{config.VOLUME_SPIKE_RATIO}% "
        f"× {config.CONFIRM_CANDLES}회 연속"
    )


# ── 장중봇 종료 (15:30 — 1회만) ──────────────────────────────

async def stop() -> None:
    """
    장중봇 종료
    main.py AsyncIOScheduler에서 15:30에 1회만 호출
    """
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


# ── 핵심: REST 폴링 루프 ───────────────────────────────────────

async def _poll_loop() -> None:
    """
    POLL_INTERVAL_SEC마다 KIS REST 전 종목 스캔
    """
    logger.info("[realtime] 폴링 루프 진입")

    while True:
        try:
            results = await asyncio.get_event_loop().run_in_executor(
                None, volume_analyzer.poll_all_markets
            )

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


# ── 알림 발송 ─────────────────────────────────────────────────

async def _dispatch_alerts(analysis: dict) -> None:
    msg_1st = telegram_bot.format_realtime_alert(analysis)
    await telegram_bot.send_async(msg_1st)
    logger.info(
        f"[realtime] 1차 알림: {analysis['종목명']}  "
        f"+{analysis['등락률']:.1f}%  거래량배율:{analysis['거래량배율']:.1f}배  "
        f"감지시각:{analysis['감지시각']}"
    )
    asyncio.create_task(_send_ai_followup(analysis))


async def _send_ai_followup(analysis: dict) -> None:
    try:
        ai_result = ai_analyzer.analyze_spike(analysis)
        msg_2nd   = telegram_bot.format_realtime_alert_ai(analysis, ai_result)
        await telegram_bot.send_async(msg_2nd)
        logger.info(
            f"[realtime] 2차 AI 알림: {analysis['종목명']} → {ai_result.get('판단', 'N/A')}"
        )
    except Exception as e:
        logger.warning(f"[realtime] 2차 AI 알림 실패: {e}")
