"""
telegram/sender.py  [v13.0]
텔레그램 메시지 포맷 + 발송 전담
- 분석 로직 없음, 포맷 + 발송만
- v12.0: 마감봇(closing_report) 폐지 → format_closing_report*() 삭제
         수익률배치 15:45로 이동 (기존 18:45)
- v13.0: [Dead Code 제거] format_pick_stocks_section / format_morning_report / format_morning_summary
         세 함수 전면 삭제 — 호출자 없음, v12 이전 캐시 키(signals, market_summary, volatility,
         report_picks) 참조, morning_report.py가 자체 _format_picks() / _format_market_env()로 교체.
"""
import asyncio
from io import BytesIO
from telegram import Bot, InputFile
import config
from utils.logger import logger


async def _send(text: str) -> None:
    bot = Bot(token=config.TELEGRAM_TOKEN)
    for chunk in _split_message(text):
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=chunk,
            parse_mode="HTML",
        )
        await asyncio.sleep(0.5)


def send(text: str) -> None:
    try:
        asyncio.run(_send(text))
    except RuntimeError:
        # 이미 실행 중인 루프가 있는 경우 (asyncio.run 실패) — BUG-07 수정
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_send(text))
        finally:
            loop.close()


def format_trade_closed(trade: dict) -> str:
    """
    [BUG-01 수정] 청산 완료 텔레그램 메시지 포맷.
    v12.0에서 마감봇 관련 코드 삭제 시 함께 제거됐던 함수 복구.
    main.py(135/165번 줄), realtime_alert.py(373번 줄)에서 호출.
    """
    name        = trade.get("name",         trade.get("종목명", ""))
    ticker      = trade.get("ticker",       trade.get("종목코드", ""))
    profit_rate = trade.get("profit_rate",  0.0)
    reason      = trade.get("close_reason", "")
    sell_price  = trade.get("sell_price",   0)
    profit_amt  = trade.get("profit_amount", 0)

    sign  = "🟢" if profit_rate >= 0 else "🔴"
    emoji = {
        "take_profit_1":  "✅",
        "take_profit_2":  "🎯",
        "stop_loss":      "🛑",
        "trailing_stop":  "📉",
        "force_close":    "⏰",
        "final_close":    "🏁",
    }.get(reason, "📌")

    return (
        f"{sign} <b>청산</b> {name}({ticker})\n"
        f"   {emoji} {reason}  수익률 <b>{profit_rate:+.2f}%</b>\n"
        f"   매도가 {sell_price:,}원  손익 {profit_amt:+,}원"
    )


async def send_async(text: str) -> None:
    await _send(text)


async def send_photo_async(photo: BytesIO, caption: str = "") -> None:
    """
    [v5.0 Phase 5] 차트 이미지(BytesIO) 텔레그램 전송.

    Args:
        photo:   BytesIO PNG — chart_generator.py 반환값
        caption: 이미지 설명 (HTML, 최대 1024자)
    """
    try:
        bot = Bot(token=config.TELEGRAM_TOKEN)
        photo.seek(0)
        await bot.send_photo(
            chat_id=config.TELEGRAM_CHAT_ID,
            photo=InputFile(photo, filename="chart.png"),
            caption=caption[:1024] if caption else None,
            parse_mode="HTML" if caption else None,
        )
    except Exception as e:
        logger.warning(f"[telegram] 이미지 전송 실패: {e}")




def format_accuracy_stats(accuracy_stats: dict) -> str:
    """
    [v10.6 Phase 4-2] 예측 정확도 + 신호 가중치 현황 포맷.
    주간 리포트 등에서 선택적 삽입 가능.
    """
    if not accuracy_stats or accuracy_stats.get("sample_count", 0) == 0:
        return ""

    lines = []
    avg_acc  = accuracy_stats.get("avg_accuracy", 0.0)
    sample   = accuracy_stats.get("sample_count", 0)
    best_sig = accuracy_stats.get("best_signal", "")
    weights  = accuracy_stats.get("signal_weights", {})

    lines.append("🧠 <b>신호 학습 현황 (테마 예측 정확도)</b>")
    lines.append(f"  최근 {sample}일 평균 픽 적중률: <b>{avg_acc:.1%}</b>")
    if best_sig:
        lines.append(f"  최우수 신호: <b>{best_sig}</b> (가중치:{weights.get(best_sig, 1.0):.2f})")

    if weights:
        high_weights = [(k, v) for k, v in weights.items() if v >= 1.2]
        low_weights  = [(k, v) for k, v in weights.items() if v <= 0.7]
        if high_weights:
            lines.append(
                "  📈 강화 신호: " +
                ", ".join(f"{k}({v:.2f})" for k, v in
                          sorted(high_weights, key=lambda x: -x[1]))
            )
        if low_weights:
            lines.append(
                "  📉 약화 신호: " +
                ", ".join(f"{k}({v:.2f})" for k, v in
                          sorted(low_weights, key=lambda x: x[1]))
            )

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# 내부 헬퍼 (full report 전용)
# ══════════════════════════════════════════════════════════════

def _calc_avg_소외(theme: dict) -> float:
    """테마 내 종목들의 소외도 평균 계산."""
    stocks = theme.get("종목들", [])
    if not stocks:
        return 0.0
    vals = [
        s.get("소외도", 0.0) for s in stocks
        if isinstance(s.get("소외도"), (int, float))
    ]
    return sum(vals) / len(vals) if vals else 0.0
