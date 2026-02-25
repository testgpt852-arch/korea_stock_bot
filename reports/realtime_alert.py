"""
reports/realtime_alert.py
장중봇 실행 전담 (09:00 시작 / 15:30 종료)

[v3.1 방법 B+A 하이브리드]
- 방법 B: WebSocket 고정 구독 — 아침봇 워치리스트(최대 50종목) 실시간 체결 감시
  → 0초 지연, 구독 목록 동적 변경 없음 (KIS 차단 위험 0)
- 방법 A: REST 폴링 간격 10초로 단축 — 워치리스트 外 신규 테마 종목 커버
  → 워치리스트에 없는 당일 신규 종목 놓치지 않음

[알림 흐름]
WebSocket 감지: 등락률 >= PRICE_CHANGE_MIN(3%) → 1차 즉시 + 2차 AI
REST 감지:      Δ등락률 >= PRICE_DELTA_MIN(1%) AND Δ거래량 >= VOLUME_DELTA_MIN(5%)
                × CONFIRM_CANDLES 연속 → 1차 즉시 + 2차 AI
중복 방지: state_manager.can_alert() — 동일 종목 30분 쿨타임 (WS/REST 공유)

[수정이력]
- v2.5:   KIS REST 폴링 방식 (init_prev_volumes 제거)
- v2.5.2: 폴링 사이클 시작/완료 로그 추가
- v2.8:   폴링 조건 로그 업데이트
- v3.1:   WebSocket 루프 추가 (_ws_loop)
          아침봇 워치리스트(watchlist_state) 기반 고정 구독
          REST 폴링 간격 30초 → 10초 (config.POLL_INTERVAL_SEC)
"""

import asyncio
from utils.logger import logger
from utils.state_manager import can_alert, mark_alerted, reset as reset_alerts
import utils.watchlist_state    as watchlist_state
import analyzers.volume_analyzer as volume_analyzer
import analyzers.ai_analyzer     as ai_analyzer
import notifiers.telegram_bot    as telegram_bot
from kis.websocket_client import ws_client
import config

_poll_task: asyncio.Task | None = None
_ws_task:   asyncio.Task | None = None


async def start() -> None:
    global _poll_task, _ws_task
    logger.info("[realtime] 장중봇 시작 — 방법B+A 하이브리드")

    # ── REST 폴링 시작 (방법 A) ───────────────────────────────
    _poll_task = asyncio.create_task(_poll_loop())
    logger.info(
        f"[realtime] REST 폴링 시작 ✅  "
        f"간격: {config.POLL_INTERVAL_SEC}초 / "
        f"조건: 1분+{config.PRICE_DELTA_MIN}% & 거래량{config.VOLUME_DELTA_MIN}%"
    )

    # ── WebSocket 구독 시작 (방법 B) ──────────────────────────
    watchlist = watchlist_state.get_watchlist()
    if not watchlist:
        logger.warning(
            "[realtime] WebSocket 워치리스트 없음 — "
            "아침봇(08:30)이 실행됐는지 확인. REST 폴링만 사용."
        )
    else:
        _ws_task = asyncio.create_task(_ws_loop(watchlist))
        logger.info(
            f"[realtime] WebSocket 구독 시작 ✅  "
            f"워치리스트: {len(watchlist)}종목 / "
            f"조건: 등락률 >= {config.PRICE_CHANGE_MIN}%"
        )


async def stop() -> None:
    global _poll_task, _ws_task
    logger.info("[realtime] 장중봇 종료 시작")

    # REST 폴링 종료
    if _poll_task and not _poll_task.done():
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass
    _poll_task = None

    # WebSocket 종료
    if _ws_task and not _ws_task.done():
        _ws_task.cancel()
        try:
            await _ws_task
        except asyncio.CancelledError:
            pass
    _ws_task = None
    await ws_client.disconnect()

    volume_analyzer.reset()
    reset_alerts()
    watchlist_state.clear()
    logger.info("[realtime] 장중봇 종료 완료 ✅")


# ── REST 폴링 루프 (방법 A) ────────────────────────────────────

async def _poll_loop() -> None:
    logger.info("[realtime] REST 폴링 루프 진입")
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
            logger.info("[realtime] REST 폴링 루프 종료 (CancelledError)")
            break
        except Exception as e:
            logger.warning(f"[realtime] REST 폴링 오류: {e}")

        await asyncio.sleep(config.POLL_INTERVAL_SEC)


# ── WebSocket 루프 (방법 B) ────────────────────────────────────

async def _ws_loop(watchlist: dict) -> None:
    """
    아침봇 워치리스트 고정 구독 → 실시간 체결 틱 감시

    [KIS 차단 정책 준수]
    - 장 시작(09:00) 1회 연결 + 전종목 구독
    - 장중 구독/해제 반복 없음 (watchlist 고정)
    - 장 마감(15:30) stop()에서 전체 해제 후 종료
    """
    try:
        await ws_client.connect()
        if not ws_client.connected:
            logger.error("[realtime] WebSocket 연결 실패 — REST 폴링으로 대체")
            return

        for ticker in watchlist:
            await ws_client.subscribe(ticker)
        logger.info(
            f"[realtime] WebSocket 구독 완료 — "
            f"{len(ws_client.subscribed_tickers)}/{len(watchlist)}종목"
        )

        async def on_tick(tick: dict) -> None:
            ticker = tick.get("종목코드", "")
            info   = watchlist.get(ticker)
            if not info:
                return   # 워치리스트 외 종목 틱 (이론상 없지만 방어)

            # 종목명 보강 (tick에 없음)
            tick["종목명"] = info["종목명"]

            result = volume_analyzer.analyze_ws_tick(tick, info["전일거래량"])
            if not result:
                return

            if not can_alert(ticker):
                return
            mark_alerted(ticker)
            logger.info(
                f"[realtime] WS 감지: {info['종목명']} "
                f"+{tick.get('등락률', 0):.1f}%  {tick.get('체결시각', '')}"
            )
            await _dispatch_alerts(result)

        await ws_client.receive_loop(on_tick)

    except asyncio.CancelledError:
        logger.info("[realtime] WebSocket 루프 종료 (CancelledError)")
    except Exception as e:
        logger.error(f"[realtime] WebSocket 루프 오류: {e}")


# ── 알림 발송 (WS/REST 공통) ──────────────────────────────────

async def _dispatch_alerts(analysis: dict) -> None:
    msg_1st = telegram_bot.format_realtime_alert(analysis)
    await telegram_bot.send_async(msg_1st)
    logger.info(
        f"[realtime] 1차 알림: {analysis['종목명']}  "
        f"+{analysis['등락률']:.1f}%  소스:{analysis.get('감지소스','?')}"
    )
    asyncio.create_task(_send_ai_followup(analysis))


async def _send_ai_followup(analysis: dict) -> None:
    try:
        ai_result = ai_analyzer.analyze_spike(analysis)
        msg_2nd   = telegram_bot.format_realtime_alert_ai(analysis, ai_result)
        await telegram_bot.send_async(msg_2nd)
        logger.info(
            f"[realtime] 2차 AI 알림: {analysis['종목명']} "
            f"→ {ai_result.get('판단', 'N/A')}"
        )
    except Exception as e:
        logger.warning(f"[realtime] 2차 AI 알림 실패: {e}")
