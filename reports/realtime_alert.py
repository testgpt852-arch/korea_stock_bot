"""
reports/realtime_alert.py
장중봇 실행 전담 (09:00 시작 / 15:30 종료)

[ARCHITECTURE 의존성]
realtime_alert → volume_analyzer, state_manager, ai_analyzer, telegram_bot
realtime_alert → kis/websocket_client (연결 규칙 엄수)

🚨 KIS WebSocket 규칙 엄수 (ARCHITECTURE.md)
   start()  = 09:00 에 main.py에서 1회만 호출
   stop()   = 15:30 에 main.py에서 1회만 호출
   연결/종료 루프 절대 금지
"""

import asyncio
from datetime import datetime
from utils.logger import logger
from utils.date_utils import get_prev_trading_day, get_today, fmt_ymd
from utils.state_manager import can_alert, mark_alerted, reset as reset_alerts
import analyzers.volume_analyzer as volume_analyzer
import analyzers.ai_analyzer     as ai_analyzer
import notifiers.telegram_bot    as telegram_bot
from kis.websocket_client import ws_client   # 싱글톤


# ── 장중봇 시작 (09:00 — 1회만) ──────────────────────────────

async def start() -> None:
    """
    장중봇 시작
    main.py AsyncIOScheduler에서 09:00에 1회만 호출
    """
    logger.info("[realtime] 장중봇 시작 — WebSocket 연결")

    # 전일 거래량 로딩 (급등 판단 기준)
    prev = get_prev_trading_day(get_today())
    if prev:
        volume_analyzer.init_prev_volumes(fmt_ymd(prev))

    # WebSocket 연결 (1회만 — 연결 상태면 내부에서 즉시 return)
    await ws_client.connect()

    if not ws_client.connected:
        logger.error("[realtime] WebSocket 연결 실패 — 장중봇 중단")
        return

    # 수신 루프 시작 (비동기 백그라운드)
    asyncio.create_task(ws_client.receive_loop(_on_tick))
    logger.info("[realtime] 실시간 수신 루프 시작 ✅")


# ── 장중봇 종료 (15:30 — 1회만) ──────────────────────────────

async def stop() -> None:
    """
    장중봇 종료
    main.py AsyncIOScheduler에서 15:30에 1회만 호출
    구독 종목 전체 해제 → WebSocket 종료 → 상태 초기화
    """
    logger.info("[realtime] 장중봇 종료 — WebSocket 종료 시작")

    # WebSocket 종료 (내부에서 모든 구독 해제 후 close)
    await ws_client.disconnect()

    # 오늘 거래량·알림 상태 초기화
    volume_analyzer.reset()
    reset_alerts()

    logger.info("[realtime] 장중봇 종료 완료 ✅")


# ── 실시간 틱 수신 핸들러 ─────────────────────────────────────

async def _on_tick(tick: dict) -> None:
    """
    WebSocket에서 틱 수신 시 호출되는 콜백
    ws_client.receive_loop(on_data=_on_tick) 으로 등록
    """
    analysis = volume_analyzer.analyze(tick)

    if not analysis["조건충족"]:
        return

    ticker = analysis["종목코드"]

    # 쿨타임 확인 (state_manager: 30분 이내 동일 종목 재알림 방지)
    if not can_alert(ticker):
        return

    mark_alerted(ticker)

    # ── 1차 알림: 즉시 발송 ──────────────────────────────────
    msg_1st = telegram_bot.format_realtime_alert(analysis)
    await telegram_bot.send_async(msg_1st)
    logger.info(f"[realtime] 1차 알림 발송: {analysis['종목명']} +{analysis['등락률']:.1f}%")

    # ── 2차 알림: AI 분석 후 발송 (비동기, 1~3초 후) ─────────
    asyncio.create_task(_send_ai_followup(analysis))


async def _send_ai_followup(analysis: dict) -> None:
    """
    AI 2차 분석 후 추가 알림 발송
    급등 원인(진짜/작전) 판단 포함
    """
    try:
        ai_result = ai_analyzer.analyze_spike(analysis)
        msg_2nd   = telegram_bot.format_realtime_alert_ai(analysis, ai_result)
        await telegram_bot.send_async(msg_2nd)
        logger.info(
            f"[realtime] 2차 AI 알림: {analysis['종목명']} → {ai_result.get('판단','N/A')}"
        )
    except Exception as e:
        logger.warning(f"[realtime] 2차 AI 알림 실패: {e}")
