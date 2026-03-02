"""
reports/realtime_alert.py
장중봇 실행 전담 (09:00 시작 / 15:30 종료)

[v12.0 개편]
- AI 판단(analyze_spike) 완전 제거 — 숫자 조건 필터만 사용
- volume_analyzer → intraday_analyzer 모듈명 변경
- 2차 AI 팔로업 알림 제거 (1차 알림만 발송)
- 자동매매: AI 판단 없이 등락률·호가강도·can_buy() 규칙으로 직접 진입

[v3.1 방법 B+A 하이브리드]
- 방법 B: WebSocket 고정 구독 — 아침봇 워치리스트(최대 40종목) 실시간 체결 감시
- 방법 A: REST 폴링 간격 10초 — 워치리스트 外 신규 테마 종목 커버

[v4.0 호가 분석 통합]
- WS_ORDERBOOK_ENABLED=false(기본): 체결(H0STCNT0) 40종목 전체 구독 (기존 동작 유지)
  체결 감지 후 REST get_orderbook() 1회 호출 → 호가 분석 → 알림 포함
- WS_ORDERBOOK_ENABLED=true: 체결 20종목 + 호가(H0STASP0) 20종목 (합계 40, 한도 준수)
  → on_orderbook() 콜백: WS 호가 틱으로 REST 호출 없이 즉시 호가 분석
  ⚠️ true 설정 시 체결 커버리지 20종목으로 감소 — 신중히 설정
"""

import asyncio
from utils.logger import logger
from utils.state_manager import can_alert, mark_alerted, reset as reset_alerts
import utils.watchlist_state    as watchlist_state
import analyzers.intraday_analyzer as intraday_analyzer
import telegram.sender    as telegram_bot
from kis.websocket_client import ws_client
import tracking.alert_recorder   as alert_recorder  # [v13.0 버그수정] trading_journal 잘못 alias된 것 원복 — trading_journal.record_alert() 존재하지 않음
import config

_poll_task: asyncio.Task | None = None
_ws_task:   asyncio.Task | None = None


async def start() -> None:
    global _poll_task, _ws_task
    logger.info("[realtime] 장중봇 시작 — 방법B+A 하이브리드 (v12.0 AI없음, 숫자조건만)")

    _poll_task = asyncio.create_task(_poll_loop())
    logger.info(
        f"[realtime] REST 폴링 시작 ✅  "
        f"간격: {config.POLL_INTERVAL_SEC}초 / "
        f"호가분석: {'활성' if config.ORDERBOOK_ENABLED else '비활성'}"
    )

    watchlist = watchlist_state.get_watchlist()
    if not watchlist:
        logger.warning(
            "[realtime] WebSocket 워치리스트 없음 — "
            "아침봇(08:30)이 실행됐는지 확인. REST 폴링만 사용."
        )
    else:
        ob_mode = "체결+호가(WS)" if config.WS_ORDERBOOK_ENABLED else "체결만"
        _ws_task = asyncio.create_task(_ws_loop(watchlist))
        logger.info(
            f"[realtime] WebSocket 구독 시작 ✅  "
            f"워치리스트: {len(watchlist)}종목 / 모드: {ob_mode}"
        )

    # [v4.2] 시장 환경 로깅 (아침봇에서 설정된 값)
    market_env = watchlist_state.get_market_env()
    logger.info(f"[realtime] 오늘 시장 환경: {market_env or '(아침봇 미실행 — 미지정)'}")


async def stop() -> None:
    global _poll_task, _ws_task
    logger.info("[realtime] 장중봇 종료 시작")

    if _poll_task and not _poll_task.done():
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass
    _poll_task = None

    if _ws_task and not _ws_task.done():
        _ws_task.cancel()
        try:
            await _ws_task
        except asyncio.CancelledError:
            pass
    _ws_task = None
    await ws_client.disconnect()

    intraday_analyzer.reset()
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

            # poll_all_markets() 내부에서 급등 종목에 한해 호가 분석 자동 수행 (v4.0)
            results = await asyncio.get_running_loop().run_in_executor(  # [BUG-07] deprecated fix
                None, intraday_analyzer.poll_all_markets
            )

            logger.info(f"[realtime] 폴링 사이클 #{cycle} 완료 — 조건충족 {len(results)}종목")

            for analysis in results:
                ticker = analysis["종목코드"]
                if not can_alert(ticker):
                    continue
                mark_alerted(ticker)
                await _dispatch_alerts(analysis)

            if config.AUTO_TRADE_ENABLED:
                await _check_positions()

        except asyncio.CancelledError:
            logger.info("[realtime] REST 폴링 루프 종료 (CancelledError)")
            break
        except Exception as e:
            logger.warning(f"[realtime] REST 폴링 오류: {e}")

        await asyncio.sleep(config.POLL_INTERVAL_SEC)


# ── WebSocket 루프 (방법 B) ────────────────────────────────────

async def _ws_loop(watchlist: dict) -> None:
    """
    아침봇 워치리스트 고정 구독 → 실시간 체결·호가 틱 감시

    [v4.0 호가 구독 모드]
    WS_ORDERBOOK_ENABLED=false(기본):
      - 체결(H0STCNT0) 전체 watchlist 구독 (최대 40종목)
      - 체결 감지 후 REST get_orderbook() 1회 호출

    WS_ORDERBOOK_ENABLED=true:
      - 체결(H0STCNT0): watchlist 상위 WS_ORDERBOOK_SLOTS(20)종목
      - 호가(H0STASP0): watchlist 상위 WS_ORDERBOOK_SLOTS(20)종목
      - 합계 40종목 = KIS 한도 내
      → on_orderbook()에서 WS 호가 틱으로 즉시 분석 (REST 호출 없음)

    [KIS 차단 정책 준수]
    - 장 시작 1회 연결 + 전종목 구독 (목록 고정)
    - 장중 구독/해제 반복 없음
    - 장 마감 stop()에서 전체 해제 후 종료
    """
    watchlist_items = list(watchlist.items())

    try:
        await ws_client.connect()
        if not ws_client.connected:
            logger.error("[realtime] WebSocket 연결 실패 — REST 폴링으로 대체")
            return

        if config.WS_ORDERBOOK_ENABLED:
            slots = config.WS_ORDERBOOK_SLOTS
            tick_items = watchlist_items[:slots]
            ob_items   = watchlist_items[:slots]
            for ticker, _ in tick_items:
                await ws_client.subscribe(ticker)
            for ticker, _ in ob_items:
                await ws_client.subscribe_orderbook(ticker)
            logger.info(
                f"[realtime] WS 구독 완료 — 체결 {len(ws_client.subscribed_tickers)}종목 "
                f"/ 호가 {len(ws_client.subscribed_ob)}종목"
            )
        else:
            for ticker, _ in watchlist_items[:config.WS_WATCHLIST_MAX]:
                await ws_client.subscribe(ticker)
            logger.info(
                f"[realtime] WS 구독 완료 — 체결 {len(ws_client.subscribed_tickers)}/{len(watchlist)}종목"
            )

        _ob_cache: dict[str, dict] = {}

        async def on_tick(tick: dict) -> None:
            ticker = tick.get("종목코드", "")
            info   = watchlist.get(ticker)
            if not info:
                return

            tick["종목명"] = info["종목명"]
            result = intraday_analyzer.analyze_ws_tick(tick, info["전일거래량"])
            if not result:
                return

            if not can_alert(ticker):
                return
            mark_alerted(ticker)

            if config.WS_ORDERBOOK_ENABLED and ticker in _ob_cache:
                result = intraday_analyzer.analyze_ws_orderbook_tick(_ob_cache[ticker], result)
            elif config.ORDERBOOK_ENABLED and not config.WS_ORDERBOOK_ENABLED:
                loop = asyncio.get_running_loop()  # [BUG-07] deprecated fix
                from kis.rest_client import get_orderbook
                ob_data = await loop.run_in_executor(None, lambda: get_orderbook(ticker))
                호가분석 = intraday_analyzer.analyze_orderbook(ob_data)
                result = {**result, "호가분석": 호가분석}

            logger.info(
                f"[realtime] WS 감지: {info['종목명']} "
                f"+{tick.get('등락률', 0):.1f}%  {tick.get('체결시각', '')}  "
                f"호가강도: {result.get('호가분석', {}).get('호가강도', 'N/A') if result.get('호가분석') else 'N/A'}"
            )
            await _dispatch_alerts(result)

        async def on_orderbook(ob: dict) -> None:
            ticker = ob.get("종목코드", "")
            if ticker and ticker in watchlist:
                _ob_cache[ticker] = ob

        ob_callback = on_orderbook if config.WS_ORDERBOOK_ENABLED else None
        await ws_client.receive_loop(on_tick, on_orderbook=ob_callback)

    except asyncio.CancelledError:
        logger.info("[realtime] WebSocket 루프 종료 (CancelledError)")
    except Exception as e:
        logger.error(f"[realtime] WebSocket 루프 오류: {e}")


# ── 알림 발송 (WS/REST 공통) ──────────────────────────────────

async def _dispatch_alerts(analysis: dict) -> None:
    msg_1st = telegram_bot.format_realtime_alert(analysis)
    await telegram_bot.send_async(msg_1st)
    logger.info(
        f"[realtime] 알림: {analysis['종목명']}  "
        f"+{analysis['등락률']:.1f}%  소스:{analysis.get('감지소스','?')}  "
        f"호가:{analysis.get('호가분석', {}).get('호가강도', '-') if analysis.get('호가분석') else '-'}"
    )
    alert_recorder.record_alert(analysis)

    # [v12.0] AI 팔로업 제거 — 자동매매는 숫자 조건만으로 직접 판단
    if config.AUTO_TRADE_ENABLED:
        asyncio.create_task(_handle_trade_signal_numeric(analysis))


async def _handle_trade_signal_numeric(analysis: dict) -> None:
    """
    [v12.0] AI 제거 후 숫자 조건 기반 자동매매 진입 판단.
    analyze_spike() 호출 없이 등락률·호가강도 필터만 적용.
    """
    try:
        ticker      = analysis.get("종목코드", "")
        change_rate = analysis.get("등락률", 0.0)

        if change_rate < config.MIN_ENTRY_CHANGE:
            return
        if change_rate > config.MAX_ENTRY_CHANGE:
            return

        # 호가강도가 "약세"이면 진입 보류
        호가분석 = analysis.get("호가분석")
        if 호가분석 and 호가분석.get("호가강도") == "약세":
            logger.info(
                f"[realtime] 자동매매 보류 — {analysis['종목명']} "
                f"호가강도=약세 (매도 우세, 급등 지속 불투명)"
            )
            return

        name       = analysis["종목명"]
        source     = analysis.get("감지소스", "unknown")
        market_env = watchlist_state.get_market_env()
        sector     = watchlist_state.get_sector(ticker)

        from traders import position_manager
        loop = asyncio.get_running_loop()  # [BUG-07] deprecated fix
        ok, reason = await loop.run_in_executor(
            None,
            lambda: position_manager.can_buy(ticker, market_env=market_env)
        )
        if not ok:
            logger.info(f"[realtime] 자동매매 진입 불가 — {name}: {reason}")
            return

        # [v13.1] 픽 유형 → pick_type 매핑 (단타/스윙 청산 분기)
        _DAYTRADING = {"공시", "테마"}
        pick_유형 = analysis.get("유형", "")
        pick_type = "단타" if pick_유형 in _DAYTRADING else "스윙"

        asyncio.create_task(
            _handle_trade_signal(ticker, name, source, None, market_env, sector, pick_type)
        )

    except Exception as e:
        logger.warning(f"[realtime] 숫자조건 자동매매 판단 실패: {e}")


async def _handle_trade_signal(
    ticker: str, name: str, source: str,
    stop_loss_price: int | None = None,
    market_env: str = "",
    sector: str = "",
    pick_type: str = "단타",               # [v13.1] 단타/스윙 청산 분기용
) -> None:
    """
    매수 체결 → DB 기록 → 텔레그램 알림 (v3.4)
    [v4.2] stop_loss_price / market_env → open_position() 에 전달
           → Trailing Stop peak_price 초기화 + 손절가 설정
    [v4.4] sector → open_position() 에 전달 → 섹터 분산 DB 기록
    """
    from traders import position_manager
    from kis import order_client

    loop = asyncio.get_running_loop()  # [BUG-07] deprecated fix

    try:
        buy_result = await loop.run_in_executor(
            None, lambda: order_client.buy(ticker, name)
        )

        if not buy_result["success"]:
            logger.warning(
                f"[realtime] 자동매수 실패 — {name}({ticker}): {buy_result['message']}"
            )
            return

        buy_price = buy_result["buy_price"]
        qty       = buy_result["qty"]
        total_amt = buy_result["total_amt"]

        # [v4.2] stop_loss_price + market_env 전달 → Trailing Stop 초기화
        # [v4.4] sector 전달 → 섹터 분산 체크 DB 기록
        await loop.run_in_executor(
            None,
            lambda: position_manager.open_position(
                ticker, name, buy_price, qty, source,
                stop_loss_price=stop_loss_price,
                market_env=market_env,
                sector=sector,
                pick_type=pick_type,       # [v13.1]
            )
        )

        msg = telegram_bot.format_trade_executed(
            ticker=ticker, name=name,
            buy_price=buy_price, qty=qty, total_amt=total_amt,
            source=source, mode=config.TRADING_MODE,
            stop_loss_price=stop_loss_price,   # [v4.2] 알림에 AI 손절가 표시
            market_env=market_env,
        )
        await telegram_bot.send_async(msg)
        logger.info(
            f"[realtime] 자동매수 완료 ✅  {name}({ticker})  "
            f"{qty}주 × {buy_price:,}원  총 {total_amt:,}원  "
            f"손절가:{stop_loss_price:,}원" if stop_loss_price else
            f"[realtime] 자동매수 완료 ✅  {name}({ticker})  "
            f"{qty}주 × {buy_price:,}원  총 {total_amt:,}원"
        )

    except Exception as e:
        logger.error(f"[realtime] _handle_trade_signal 오류 ({ticker}): {e}")


async def _check_positions() -> None:
    """포지션 익절/손절/Trailing Stop 검사 + 청산 처리 (v3.4 / v4.2 TS 추가)"""
    from traders import position_manager

    loop = asyncio.get_running_loop()  # [BUG-07] deprecated fix
    try:
        closed_list = await loop.run_in_executor(
            None, position_manager.check_exit
        )
        if closed_list:
            await _handle_exit_results(closed_list)
    except Exception as e:
        logger.warning(f"[realtime] 포지션 청산 검사 오류: {e}")


async def _handle_exit_results(closed_list: list[dict]) -> None:
    """청산된 포지션 텔레그램 알림 발송 (v3.4)"""
    for closed in closed_list:
        try:
            msg = telegram_bot.format_trade_closed(closed)
            await telegram_bot.send_async(msg)
        except Exception as e:
            logger.warning(f"[realtime] 청산 알림 발송 실패: {e}")
