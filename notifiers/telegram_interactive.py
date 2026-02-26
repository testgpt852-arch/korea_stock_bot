"""
notifiers/telegram_interactive.py
[v5.0 Phase 5 신규] 텔레그램 인터랙티브 명령어 처리

[지원 명령어]
- /status    — 봇 현재 상태 (오늘 알림 수, 포지션 수, 시장 환경)
- /holdings  — 현재 보유 종목 (AUTO_TRADE_ENABLED=true 시 KIS 잔고 조회)
- /principles — 주요 매매 원칙 Top5 (confidence='high' 기준)
- /evaluate  — [v6.0 P2 신규] 보유 종목 AI 맞춤 분석 (Prism /evaluate 경량화)
               종목코드 입력 → 평균매수가 입력 → Gemma AI 분석 결과 반환
               ConversationHandler 2단계 대화 플로우 (EVAL_TICKER → EVAL_PRICE)

[아키텍처]
- python-telegram-bot Application + CommandHandler 기반 롱폴링
- main.py에서 asyncio.create_task()로 백그라운드 실행
- DB 조회 + 포맷만 담당 — 분석/수집/주문 로직 금지
- 명령어 처리 실패 시 "❌ 오류 발생" 응답 + 로그만 남김 (비치명적)

[의존성]
telegram_interactive → tracking/db_schema (get_conn)
telegram_interactive → utils/watchlist_state (get_market_env)
telegram_interactive → kis/order_client (get_balance — AUTO_TRADE=true 시만)
telegram_interactive → tracking/trading_journal (get_journal_context — /evaluate)
telegram_interactive ← main.py (start_interactive_handler 호출)

[규칙]
- CommandHandler는 이 파일에만 위치 — telegram_bot.py에 추가 금지
- KIS API 호출은 AUTO_TRADE_ENABLED=true 시에만 시도, 실패 시 DB 폴백
- /evaluate AI 호출은 run_in_executor 경유 (동기 Gemma SDK 사용)
- ConversationHandler 타임아웃: EVALUATE_CONV_TIMEOUT_SEC(기본 120초)

[수정이력]
- v5.0: Phase 5 신규
- v6.0: /evaluate 명령어 추가 (P2, Prism 경량화)
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
# /evaluate 명령어 — 보유 종목 AI 맞춤 분석 [v6.0 P2 신규]
# ══════════════════════════════════════════════════════════════

# ConversationHandler 상태값
_EVAL_TICKER = 0   # 종목코드 입력 대기
_EVAL_PRICE  = 1   # 평균매수가 입력 대기


async def _cmd_evaluate_start(update, context) -> int:
    """
    /evaluate — 1단계: 종목코드 입력 요청.
    Prism /evaluate 경량화 구현 — 보유 종목 AI 맞춤 분석.
    """
    await update.message.reply_text(
        "📊 <b>보유 종목 AI 분석</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        "분석할 종목코드를 입력해주세요.\n"
        "예: <code>005930</code> (삼성전자)\n\n"
        "❌ 취소하려면 /cancel 을 입력하세요.",
        parse_mode="HTML"
    )
    return _EVAL_TICKER


async def _cmd_evaluate_ticker(update, context) -> int:
    """
    /evaluate — 2단계: 종목코드 수신 후 평균매수가 요청.
    종목명은 KIS get_stock_price로 조회 시도, 실패 시 코드 그대로 사용.
    """
    ticker_input = update.message.text.strip().replace("-", "").upper()

    # 6자리 숫자 코드 또는 종목명 허용 (종목명은 간략 매핑 시도)
    if not ticker_input.isdigit():
        await update.message.reply_text(
            "⚠️ 6자리 종목코드를 입력해주세요.\n예: <code>005930</code>",
            parse_mode="HTML"
        )
        return _EVAL_TICKER

    ticker = ticker_input.zfill(6)

    # 종목명 조회 시도
    stock_name = ticker
    try:
        if config.AUTO_TRADE_ENABLED or config.KIS_APP_KEY:
            from kis.order_client import get_current_price
            price_info = get_current_price(ticker)
            if price_info:
                stock_name = price_info.get("종목명", ticker) or ticker
    except Exception:
        pass

    context.user_data["eval_ticker"]     = ticker
    context.user_data["eval_stock_name"] = stock_name

    await update.message.reply_text(
        f"✅ <b>{stock_name}</b> ({ticker}) 선택됨\n\n"
        f"평균 매수가를 입력해주세요. (숫자만)\n"
        f"예: <code>68500</code>",
        parse_mode="HTML"
    )
    return _EVAL_PRICE


async def _cmd_evaluate_price(update, context) -> int:
    """
    /evaluate — 3단계: 평균매수가 수신 → Gemma AI 분석 실행.
    과거 거래 일지 컨텍스트 + 매매 원칙을 주입해 맞춤 분석 반환.
    """
    try:
        avg_price = int(update.message.text.strip().replace(",", ""))
    except ValueError:
        await update.message.reply_text(
            "⚠️ 숫자만 입력해주세요. 예: <code>68500</code>",
            parse_mode="HTML"
        )
        return _EVAL_PRICE

    ticker     = context.user_data.get("eval_ticker", "")
    stock_name = context.user_data.get("eval_stock_name", ticker)

    waiting_msg = await update.message.reply_text(
        f"🔍 <b>{stock_name}</b> 분석 중...\n잠시 기다려주세요.",
        parse_mode="HTML"
    )

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            _run_evaluate_analysis,
            ticker, stock_name, avg_price
        )
        await waiting_msg.delete()
        await update.message.reply_text(result, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"[interactive] /evaluate 분석 오류: {e}")
        await waiting_msg.delete()
        await update.message.reply_text("❌ 분석 중 오류가 발생했습니다.")

    return _EVAL_CANCEL  # ConversationHandler 종료


_EVAL_CANCEL = -1  # ConversationHandler.END 역할


async def _cmd_evaluate_cancel(update, context) -> int:
    """/evaluate 대화 취소"""
    await update.message.reply_text("분석이 취소되었습니다.")
    context.user_data.clear()
    return _EVAL_CANCEL


def _run_evaluate_analysis(ticker: str, stock_name: str, avg_price: int) -> str:
    """
    동기 함수 — run_in_executor 경유 호출.
    Gemma AI로 보유 종목 맞춤 분석 수행.

    주입 컨텍스트:
    1. 현재가 + 수익률 (KIS API)
    2. 과거 거래 일지 요약 (trading_journal)
    3. 관련 매매 원칙 (trading_principles)
    4. 시장 환경 (watchlist_state)
    """
    # ① 현재가 조회
    current_price = 0
    try:
        if config.KIS_APP_KEY:
            from kis.order_client import get_current_price
            price_info = get_current_price(ticker)
            current_price = price_info.get("현재가", 0) if price_info else 0
    except Exception:
        pass

    profit_pct = (
        (current_price - avg_price) / avg_price * 100
        if avg_price > 0 and current_price > 0 else 0.0
    )

    # ② 과거 거래 일지 컨텍스트
    journal_ctx = ""
    try:
        from tracking.trading_journal import get_journal_context
        journal_ctx = get_journal_context(ticker)
    except Exception:
        pass

    # ③ 관련 매매 원칙
    principles_ctx = ""
    try:
        from tracking.db_schema import get_conn
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT condition_desc, action, win_rate
                FROM trading_principles
                WHERE confidence = 'high'
                  AND (is_active IS NULL OR is_active = 1)
                ORDER BY win_rate DESC
                LIMIT 2
            """).fetchall()
        if rows:
            items = [f"'{r[0]}' → {r[1]} (승률 {r[2]:.0f}%)" for r in rows]
            principles_ctx = "매매 원칙: " + " / ".join(items)
    except Exception:
        pass

    # ④ 시장 환경
    market_env = ""
    try:
        from utils.watchlist_state import get_market_env
        market_env = get_market_env() or ""
    except Exception:
        pass

    # ⑤ Google AI 분석
    google_client = None
    try:
        from google import genai
        from google.genai import types as _gtypes
        if config.GOOGLE_AI_API_KEY:
            google_client = genai.Client(api_key=config.GOOGLE_AI_API_KEY)
    except Exception:
        pass

    if not google_client:
        # AI 없으면 기본 수익률 보고만
        emoji = "📈" if profit_pct >= 0 else "📉"
        price_line = f"현재가: {current_price:,}원" if current_price > 0 else "현재가: 조회 불가"
        return (
            f"{emoji} <b>{stock_name}</b> ({ticker}) 분석\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"평균 매수가: {avg_price:,}원\n"
            f"{price_line}\n"
            f"현재 수익률: <b>{profit_pct:+.2f}%</b>\n\n"
            f"⚠️ AI 분석 불가 (GOOGLE_AI_API_KEY 미설정)"
        )

    price_line = f"{current_price:,}원 ({profit_pct:+.1f}%)" if current_price > 0 else "조회불가"
    prompt = f"""당신은 한국 단타 매매 전문가입니다. 보유 종목을 간결하게 분석해주세요.

[보유 종목]
종목명: {stock_name} ({ticker})
평균 매수가: {avg_price:,}원
현재가/수익률: {price_line}
시장 환경: {market_env or "미확인"}

[과거 거래 이력]
{journal_ctx or "이력 없음"}

[참고 원칙]
{principles_ctx or "없음"}

[분석 요청]
1. 현재 수익률 상황 평가 (hold/익절/손절 판단 포함)
2. 이 종목 특이사항 또는 주의점 (과거 이력 있으면 반영)
3. 단기(오늘~내일) 대응 전략 한 줄

간결하고 실용적으로 3~5문장 이내로 작성하세요."""

    try:
        response = google_client.models.generate_content(
            model="gemma-3-27b-it",
            contents=prompt,
            config=_gtypes.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=400,
            ),
        )
        analysis = (response.text or "분석 결과 없음").strip()
    except Exception as e:
        analysis = f"AI 분석 실패: {str(e)[:50]}"

    emoji = "📈" if profit_pct >= 0 else "📉"
    price_display = f"{current_price:,}원" if current_price > 0 else "조회불가"

    return (
        f"{emoji} <b>{stock_name}</b> ({ticker}) AI 분석\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"평균매수가: {avg_price:,}원 | 현재가: {price_display}\n"
        f"수익률: <b>{profit_pct:+.2f}%</b>  시장: {market_env or 'N/A'}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{analysis}"
    )


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

        # [v6.0 P2] /evaluate ConversationHandler 등록
        try:
            from telegram.ext import ConversationHandler as TGConvHandler, MessageHandler as TGMsgHandler, filters as TGFilters
            eval_timeout = getattr(config, "EVALUATE_CONV_TIMEOUT_SEC", 120)
            eval_conv = TGConvHandler(
                entry_points=[TGCommandHandler("evaluate", _cmd_evaluate_start)],
                states={
                    _EVAL_TICKER: [TGMsgHandler(TGFilters.TEXT & ~TGFilters.COMMAND, _cmd_evaluate_ticker)],
                    _EVAL_PRICE:  [TGMsgHandler(TGFilters.TEXT & ~TGFilters.COMMAND, _cmd_evaluate_price)],
                },
                fallbacks=[TGCommandHandler("cancel", _cmd_evaluate_cancel)],
                conversation_timeout=eval_timeout,
            )
            app.add_handler(eval_conv)
            logger.info(f"[interactive] /evaluate 핸들러 등록 (타임아웃 {eval_timeout}초)")
        except ImportError:
            logger.info("[interactive] ConversationHandler 없음 — /evaluate 비활성 (pip install python-telegram-bot>=20)")
        except Exception as e:
            logger.warning(f"[interactive] /evaluate 핸들러 등록 실패 (비치명적): {e}")

        logger.info("[interactive] 텔레그램 명령어 핸들러 시작 (/status /holdings /principles /evaluate)")
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
