"""
reports/realtime_alert.py
장중봇 실행 전담 (09:00 시작 / 15:30 종료)

[ARCHITECTURE 의존성]
realtime_alert → volume_analyzer, state_manager, ai_analyzer, telegram_bot

🚨 KIS WebSocket 규칙 (ARCHITECTURE.md)
   이 파일은 KIS WebSocket을 사용하지 않음 (v2.4)
   WebSocket 연결/구독/종료 코드 없음 → 차단 위험 없음

[수정이력]
- v2.3: subscribe() 호출 누락 버그 수정
        receive_loop task 시작 순서 수정

- v2.4: 전 종목 커버를 위한 구조 전환
        기존: KIS WebSocket 구독 방식
              → 동시 구독 한도(~100종목)로 코스피+코스닥 전체 커버 불가
        수정: pykrx REST 폴링 방식
              → POLL_INTERVAL_SEC(60초) 마다 전 종목 일괄 조회
              → KIS WebSocket 전혀 사용 안 함 (차단 위험 제로)
              → ack 경합 문제 구조적 해소
              → 코스피+코스닥 전 종목(~2,500개) 커버

        변경 내용:
        - ws_client import 제거
        - _poll_loop() 추가: asyncio 무한 루프 기반 폴링
        - _poll_task: Task 핸들 관리 (stop()에서 취소)
        - start(): WebSocket 연결 제거 → 폴링 루프 시작
        - stop():  WebSocket 종료 제거 → 폴링 루프 취소
"""

import asyncio
from utils.logger import logger
from utils.date_utils import get_prev_trading_day, get_today, fmt_ymd
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

    v2.4: KIS WebSocket 연결 없음.
          pykrx REST 폴링 루프만 시작.
    """
    global _poll_task

    logger.info("[realtime] 장중봇 시작 — pykrx REST 폴링 방식 (전 종목)")

    # 전일 거래량 로딩 (급등 판단 기준)
    prev = get_prev_trading_day(get_today())
    if prev:
        volume_analyzer.init_prev_volumes(fmt_ymd(prev))
    else:
        logger.warning("[realtime] 전일 거래량 로딩 불가 — 거래량배율 계산 제한")

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

    # 폴링 루프 취소
    if _poll_task and not _poll_task.done():
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass
    _poll_task = None

    # 오늘 거래량·알림 상태 초기화
    volume_analyzer.reset()
    reset_alerts()

    logger.info("[realtime] 장중봇 종료 완료 ✅")


# ── 핵심: REST 폴링 루프 ───────────────────────────────────────

async def _poll_loop() -> None:
    """
    POLL_INTERVAL_SEC마다 전 종목 스캔
    volume_analyzer.poll_all_markets() → 조건충족 종목 → 알림 발송

    흐름:
    1. poll_all_markets() → pykrx REST 전 종목 조회 (코스피+코스닥)
    2. 각 종목 CONFIRM_CANDLES 연속 충족 여부 판단 (volume_analyzer 내부)
    3. 쿨타임 확인 → 1차 알림 즉시 발송
    4. AI 2차 분석 비동기 발송
    """
    logger.info("[realtime] 폴링 루프 진입")

    while True:
        try:
            results = await asyncio.get_event_loop().run_in_executor(
                None, volume_analyzer.poll_all_markets
            )

            for analysis in results:
                ticker = analysis["종목코드"]

                # 쿨타임 확인 (30분 이내 동일 종목 재알림 방지)
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
    """
    1차 알림 즉시 발송 + 2차 AI 알림 비동기 발송
    """
    # ── 1차 알림: 즉시 발송 ──────────────────────────────────
    msg_1st = telegram_bot.format_realtime_alert(analysis)
    await telegram_bot.send_async(msg_1st)
    logger.info(
        f"[realtime] 1차 알림: {analysis['종목명']}  "
        f"+{analysis['등락률']:.1f}%  거래량배율:{analysis['거래량배율']:.1f}배  "
        f"감지시각:{analysis['감지시각']}"
    )

    # ── 2차 알림: AI 분석 후 발송 (비동기) ───────────────────
    asyncio.create_task(_send_ai_followup(analysis))


async def _send_ai_followup(analysis: dict) -> None:
    """
    AI 2차 분석 후 추가 알림 발송
    급등 원인(진짜급등/작전주의심/판단불가) 판단 포함
    """
    try:
        ai_result = ai_analyzer.analyze_spike(analysis)
        msg_2nd   = telegram_bot.format_realtime_alert_ai(analysis, ai_result)
        await telegram_bot.send_async(msg_2nd)
        logger.info(
            f"[realtime] 2차 AI 알림: {analysis['종목명']} → {ai_result.get('판단', 'N/A')}"
        )
    except Exception as e:
        logger.warning(f"[realtime] 2차 AI 알림 실패: {e}")
