"""
notifiers/telegram_interactive.py
[v5.0 Phase 5 신규] 텔레그램 인터랙티브 명령어 처리

[지원 명령어]
- /status    — 봇 현재 상태 (오늘 알림 수, 포지션 수, 시장 환경)
- /holdings  — 현재 보유 종목 (AUTO_TRADE_ENABLED=true 시 KIS 잔고 조회)
- /principles — 주요 매매 원칙 Top5 (confidence='high' 기준)

[아키텍처]
- python-telegram-bot Application + CommandHandler 기반 롱폴링
- main.py에서 asyncio.create_task()로 백그라운드 실행
- DB 조회 + 포맷만 담당 — 분석/수집/주문 로직 금지
- 명령어 처리 실패 시 "❌ 오류 발생" 응답 + 로그만 남김 (비치명적)

[의존성]
telegram_interactive → tracking/db_schema (get_conn)
telegram_interactive → utils/watchlist_state (get_market_env)
telegram_interactive → kis/order_client (get_balance — AUTO_TRADE=true 시만)
telegram_interactive ← main.py (start_interactive_handler 호출)

[규칙]
- CommandHandler는 이 파일에만 위치 — telegram_bot.py에 추가 금지
- KIS API 호출은 AUTO_TRADE_ENABLED=true 시에만 시도, 실패 시 DB 폴백
- run_in_executor 불필요 — Application은 독자 이벤트 루프 없이 asyncio 통합

[수정이력]
- v5.0: Phase 5 신규
"""

import asyncio
from utils.logger import logger
import config


# ══════════════════════════════════════════════════════════════
# 명령어 핸들러
# ══════════════════════════════════════════════════════════════

async def _cmd_status(update, context) -> None:
    """
    /status — 봇 현재 상태
    - 오늘 발송된 알림 수 (alert_history DB)
    - 현재 오픈 포지션 수 (positions DB)
    - 시장 환경 (watchlist_state)
    - AUTO_TRADE_ENABLED 여부
    """
    try:
        from tracking.db_schema import get_conn
        from utils.watchlist_state import get_market_env
        from utils.date_utils import get_today

        today = get_today().strftime("%Y-%m-%d")
        market_env = get_market_env() or "미판단"

        with get_conn() as conn:
            # 오늘 알림 수
            row = conn.execute(
                "SELECT COUNT(*) FROM alert_history WHERE DATE(sent_at) = ?",
                (today,)
            ).fetchone()
            today_alerts = row[0] if row else 0

            # 오픈 포지션 수
            row2 = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE status='open'"
            ).fetchone()
            open_positions = row2[0] if row2 else 0

            # 오늘 실현 손익 합계
            row3 = conn.execute(
                """SELECT COALESCE(SUM(profit_amount),0)
                   FROM trading_history
                   WHERE DATE(sell_time) = ?""",
                (today,)
            ).fetchone()
            today_pnl = row3[0] if row3 else 0

        trade_mode_line = (
            f"🤖 자동매매: <b>{'ON' if config.AUTO_TRADE_ENABLED else 'OFF'}</b>  "
            f"({'모의' if config.TRADING_MODE == 'VTS' else '💰 실전'})"
        )
        pnl_sign = "+" if today_pnl >= 0 else ""

        msg = (
            f"📊 <b>봇 현재 상태</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📅 날짜: {today}\n"
            f"🌡️ 시장 환경: <b>{market_env}</b>\n"
            f"📬 오늘 알림: <b>{today_alerts}건</b>\n"
            f"📁 보유 포지션: <b>{open_positions}개</b>\n"
            f"💰 오늘 실현 손익: <b>{pnl_sign}{today_pnl:,}원</b>\n"
            f"{trade_mode_line}"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    except Exception as e:
        logger.warning(f"[interactive] /status 오류: {e}")
        await update.message.reply_text("❌ 상태 조회 중 오류가 발생했습니다.")


async def _cmd_holdings(update, context) -> None:
    """
    /holdings — 현재 보유 종목
    AUTO_TRADE_ENABLED=true:  KIS 잔고 API 조회 (실시간)
    AUTO_TRADE_ENABLED=false: DB positions 테이블 조회
    """
    try:
        lines = ["📋 <b>현재 보유 종목</b>\n━━━━━━━━━━━━━━━━"]

        if config.AUTO_TRADE_ENABLED:
            # KIS 잔고 실시간 조회
            try:
                from kis.order_client import get_balance
                balance = get_balance()
                holdings = balance.get("holdings", [])
                cash     = balance.get("available_cash", 0)
                total_eval = balance.get("total_eval", 0)
                total_pnl  = balance.get("total_profit", 0.0)

                if holdings:
                    for h in holdings:
                        pnl_sign = "+" if h["profit_rate"] >= 0 else ""
                        lines.append(
                            f"  • <b>{h['name']}</b> ({h['ticker']})\n"
                            f"    {h['qty']}주  평균가:{h['avg_price']:,}  현재:{h['current_price']:,}\n"
                            f"    수익률: <b>{pnl_sign}{h['profit_rate']:.2f}%</b>"
                        )
                else:
                    lines.append("  보유 종목 없음")

                pnl_sign = "+" if total_pnl >= 0 else ""
                lines.append(f"\n💼 평가금액: {total_eval:,}원")
                lines.append(f"💰 예수금: {cash:,}원")
                lines.append(f"📈 총 수익률: <b>{pnl_sign}{total_pnl:.2f}%</b>")

            except Exception as kis_e:
                logger.debug(f"[interactive] KIS 잔고 조회 실패, DB 폴백: {kis_e}")
                await _append_db_positions(lines)
        else:
            await _append_db_positions(lines)

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.warning(f"[interactive] /holdings 오류: {e}")
        await update.message.reply_text("❌ 보유 종목 조회 중 오류가 발생했습니다.")


async def _append_db_positions(lines: list) -> None:
    """DB positions 테이블에서 오픈 포지션 조회 (보조 함수)"""
    from tracking.db_schema import get_conn

    with get_conn() as conn:
        rows = conn.execute(
            """SELECT ticker, name, qty, buy_price, market_env, sector, buy_time
               FROM positions WHERE status='open'
               ORDER BY buy_time DESC"""
        ).fetchall()

    if rows:
        for r in rows:
            ticker, name, qty, buy_price, menv, sector, buy_time = r
            sec_str = f"  [{sector}]" if sector else ""
            env_str = f"  {menv}" if menv else ""
            lines.append(
                f"  • <b>{name}</b> ({ticker}){sec_str}\n"
                f"    {qty}주  매수가:{buy_price:,}원{env_str}\n"
                f"    진입: {buy_time[:10] if buy_time else 'N/A'}"
            )
    else:
        lines.append("  보유 종목 없음 (자동매매 비활성 또는 DB 미기록)")


async def _cmd_principles(update, context) -> None:
    """
    /principles — 주요 매매 원칙 Top5
    trading_principles 테이블에서 confidence='high' 기준 상위 5개 조회
    """
    try:
        from tracking.db_schema import get_conn

        with get_conn() as conn:
            rows = conn.execute(
                """SELECT trigger_source, principle_text, win_rate, total_count
                   FROM trading_principles
                   WHERE confidence = 'high'
                     AND (is_active IS NULL OR is_active = 1)
                   ORDER BY win_rate DESC, total_count DESC
                   LIMIT 5"""
            ).fetchall()

        if not rows:
            # high가 없으면 전체에서 조회
            with get_conn() as conn:
                rows = conn.execute(
                    """SELECT trigger_source, principle_text, win_rate, total_count
                       FROM trading_principles
                       ORDER BY win_rate DESC, total_count DESC
                       LIMIT 5"""
                ).fetchall()

        source_emoji = {
            "volume":    "📊",
            "rate":      "📈",
            "websocket": "🎯",
            "gap_up":    "⚡",
        }

        lines = ["🧠 <b>주요 매매 원칙 Top5</b>\n━━━━━━━━━━━━━━━━"]
        if rows:
            for i, (src, text, win_rate, count) in enumerate(rows, 1):
                emoji = source_emoji.get(src, "•")
                win_str = f"{win_rate:.0f}%" if win_rate else "N/A"
                lines.append(
                    f"\n{i}. {emoji} <b>{src}</b>  승률:{win_str}  (n={count})\n"
                    f"   └ {text}"
                )
        else:
            lines.append("\n  아직 충분한 거래 데이터가 없습니다.")
            lines.append("  (봇 운영 2~3주 후 자동 생성됩니다)")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.warning(f"[interactive] /principles 오류: {e}")
        await update.message.reply_text("❌ 매매 원칙 조회 중 오류가 발생했습니다.")


# ══════════════════════════════════════════════════════════════
# 핸들러 시작 (main.py에서 호출)
# ══════════════════════════════════════════════════════════════

async def start_interactive_handler() -> None:
    """
    [v5.0] 텔레그램 인터랙티브 명령어 핸들러 시작.
    main.py에서 asyncio.create_task()로 호출.

    python-telegram-bot의 Application을 현재 asyncio 루프에 통합.
    별도 루프 생성 없이 기존 AsyncIOScheduler 루프에 공존.
    """
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.warning("[interactive] TELEGRAM_TOKEN/CHAT_ID 미설정 — 인터랙티브 핸들러 비활성")
        return

    try:
        from telegram.ext import ApplicationBuilder, CommandHandler as TGCommandHandler

        app = (
            ApplicationBuilder()
            .token(config.TELEGRAM_TOKEN)
            .build()
        )

        app.add_handler(TGCommandHandler("status",     _cmd_status))
        app.add_handler(TGCommandHandler("holdings",   _cmd_holdings))
        app.add_handler(TGCommandHandler("principles", _cmd_principles))

        logger.info("[interactive] 텔레그램 명령어 핸들러 시작 (/status /holdings /principles)")
        await app.initialize()
        await app.start()
        await app.updater.start_polling(
            allowed_updates=["message"],
            drop_pending_updates=True,
        )

        # 무한 대기 (main.py의 while True와 공존)
        while True:
            await asyncio.sleep(3600)

    except ImportError:
        logger.warning(
            "[interactive] python-telegram-bot Application 없음 — "
            "pip install python-telegram-bot 으로 설치 후 재시작"
        )
    except Exception as e:
        logger.error(f"[interactive] 핸들러 실행 오류: {e}")
