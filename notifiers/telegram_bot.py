"""
notifiers/telegram_bot.py
텔레그램 메시지 포맷 + 발송 전담
- 분석 로직 없음, 포맷 + 발송만

[수정이력]
- v1.1: 빈 줄 제거, 원자재 단위 추가
- v1.2: 가독성 개선, summary 짤림 제거
- v1.3: AI 공시 분석 섹션 추가 (ai_dart_results), 마감봇 포맷 개선
- v2.1: 아침봇 포맷 개선
        - 전날 코스피/코스닥 지수 섹션 추가 (prev_kospi/prev_kosdaq)
        - 미국 섹터 연동 신호 표시 (market_summary.sectors)
        - 순환매 지도: 마감봇 의존 메시지 제거 (이제 아침봇 자체 생성)
- v2.2: 아침봇 — 전날 기관/외인 순매수 섹션 추가 (prev_institutional)
- v2.8: format_realtime_alert/ai — 직전대비(1분 추가 상승률) 표시 추가
- v2.9: format_realtime_alert/ai — 감지소스 배지 추가 (거래량포착/등락률포착)
- v3.1: format_realtime_alert/ai — "websocket" 소스 배지 추가 (🎯 워치리스트)
        섹터 표시 임계값 1.5% → 1.0% (config.US_SECTOR_SIGNAL_MIN과 일관성)
- v3.2: format_realtime_alert — "gap_up" 소스 배지 추가 (⚡ 갭상승)
        format_closing_report — T5 마감강도/T6 횡보급증/T3 시총자금유입 섹션 추가
- v3.4: Phase 4 — 자동매매 알림 포맷 추가
        format_trade_executed() — 모의/실전 매수 체결 알림
        format_trade_closed()   — 포지션 청산 알림 (익절/손절/강제청산)
"""

import asyncio
from telegram import Bot
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
        loop = asyncio.get_event_loop()
        loop.run_until_complete(_send(text))


async def send_async(text: str) -> None:
    await _send(text)


# ══════════════════════════════════════════════════════════════
# 아침봇 보고서 포맷
# ══════════════════════════════════════════════════════════════

def format_morning_report(report: dict) -> str:
    today_str        = report.get("today_str", "")
    prev_str         = report.get("prev_str", "")
    signals          = report.get("signals", [])
    us               = report.get("market_summary", {})
    commodities      = report.get("commodities", {})
    theme_map        = report.get("theme_map", [])
    volatility       = report.get("volatility", "판단불가")
    reports          = report.get("report_picks", [])
    ai_dart          = report.get("ai_dart_results", [])
    prev_kospi       = report.get("prev_kospi", {})         # v2.1
    prev_kosdaq      = report.get("prev_kosdaq", {})        # v2.1
    prev_institutional = report.get("prev_institutional", [])  # v2.2

    lines = []

    # ── 헤더
    lines.append("📡 <b>아침 테마 레이더</b>")
    lines.append(f"📅 {today_str}  |  기준: {prev_str} 마감")
    lines.append(f"📊 전날 장세: <b>{volatility}</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    # ── 전날 지수 (v2.1 추가) ─────────────────────────────────
    if prev_kospi or prev_kosdaq:
        lines.append(f"\n📈 <b>전날 지수 ({prev_str})</b>")
        if prev_kospi:
            sign = "+" if prev_kospi.get("change_rate", 0) >= 0 else ""
            lines.append(
                f"  코스피:  {prev_kospi.get('close', 'N/A'):,.2f}"
                f"  ({sign}{prev_kospi.get('change_rate', 0):.2f}%)"
            )
        if prev_kosdaq:
            sign = "+" if prev_kosdaq.get("change_rate", 0) >= 0 else ""
            lines.append(
                f"  코스닥:  {prev_kosdaq.get('close', 'N/A'):,.2f}"
                f"  ({sign}{prev_kosdaq.get('change_rate', 0):.2f}%)"
            )

    # ── 테마 발화 신호 (강도 3 이상만)
    lines.append("\n🔴 <b>테마 발화 신호</b>")
    top = [s for s in signals if s.get("강도", 0) >= 3][:5]
    if top:
        for s in top:
            star    = "★" * min(s["강도"], 5)
            ai_memo = f"  ✦ {s['ai_메모']}" if s.get("ai_메모") else ""
            lines.append(f"\n{star} [{s['상태']}] <b>{s['테마명']}</b>")
            lines.append(f"   └ {s['발화신호']}")
            if ai_memo:
                lines.append(f"   {ai_memo}")
    else:
        lines.append("   감지된 주요 신호 없음")

    # ── AI 공시 분석
    if ai_dart:
        lines.append("\n🤖 <b>AI 공시 분석 (Gemma)</b>")
        for r in ai_dart[:5]:
            점수 = r.get("점수", 5)
            확률 = r.get("상한가확률", "낮음")
            이유 = r.get("이유", "")
            bar  = "■" * 점수 + "□" * (10 - 점수)
            lines.append(
                f"  <b>{r['종목명']}</b>  [{bar}] {점수}/10  상한가:{확률}\n"
                f"  └ {이유}"
            )

    # ── 미국증시
    lines.append("\n🌏 <b>미국증시 (전날 마감)</b>")
    nasdaq = us.get("nasdaq", "N/A")
    sp500  = us.get("sp500",  "N/A")
    dow    = us.get("dow",    "N/A")
    lines.append(f"  나스닥: {nasdaq}  |  S&P500: {sp500}  |  다우: {dow}")
    summary = us.get("summary", "")
    if summary:
        lines.append(f"  📌 {summary}")

    # ── 미국 섹터 연동 (v2.1 추가)
    # v2.2: 표시 임계값 1.5% → 1.0% (config.US_SECTOR_SIGNAL_MIN과 일관성)
    sectors = us.get("sectors", {})
    sector_lines = []
    for sector_name, sdata in sectors.items():
        change = sdata.get("change", "N/A")
        if change == "N/A":
            continue
        try:
            pct = float(change.replace("%", "").replace("+", ""))
        except ValueError:
            continue
        if abs(pct) < config.US_SECTOR_SIGNAL_MIN:   # config 상수 사용
            continue
        arrow = "↑" if pct > 0 else "↓"
        sector_lines.append(f"  {arrow} {sector_name}: {change}")

    if sector_lines:
        lines.append("\n🏭 <b>미국 섹터 → 국내 연동 예상</b>")
        lines.extend(sector_lines[:4])  # 최대 4개

    # ── 원자재
    lines.append("\n🪙 <b>원자재 (전날 마감)</b>")
    for name, key in [
        ("구리 (LME)", "copper"),
        ("은 (COMEX)", "silver"),
        ("천연가스", "gas"),
    ]:
        c      = commodities.get(key, {})
        price  = c.get("price",  "N/A")
        change = c.get("change", "N/A")
        unit   = c.get("unit",   "")
        신뢰도  = c.get("신뢰도", "")
        if price != "N/A":
            lines.append(f"  {name}: {price} {unit}  {change}  [{신뢰도}]")
        else:
            lines.append(f"  {name}: N/A")

    # ── 전날 기관/외인 순매수 (v2.2 신규) ──────────────────────
    # 전날 기관·외인이 집중 매수한 종목 = 오늘 장에서 추가 매수 가능성 있음
    # 상한가·급등 종목 대상으로만 조회하므로 모멘텀+수급 교차 확인에 유용
    if prev_institutional:
        inst_top = sorted(
            prev_institutional,
            key=lambda x: x.get("기관순매수", 0), reverse=True
        )[:5]
        frgn_top = sorted(
            prev_institutional,
            key=lambda x: x.get("외국인순매수", 0), reverse=True
        )[:5]

        lines.append(f"\n🏦 <b>전날 기관/외인 순매수 ({prev_str})</b>")
        lines.append("  ※ 상한가·급등 종목 대상 집계")

        inst_items = [
            f"{s['종목명']}({s['기관순매수'] // 100_000_000:+,}억)"
            for s in inst_top if s.get("기관순매수", 0) > 0
        ]
        frgn_items = [
            f"{s['종목명']}({s['외국인순매수'] // 100_000_000:+,}억)"
            for s in frgn_top if s.get("외국인순매수", 0) > 0
        ]

        lines.append(f"  기관: {',  '.join(inst_items) if inst_items else 'N/A'}")
        lines.append(f"  외인: {',  '.join(frgn_items) if frgn_items else 'N/A'}")

    # ── 순환매 지도 (v2.1: 마감봇 의존 메시지 제거)
    lines.append("\n🗺️ <b>순환매 지도</b>")
    valid = [t for t in theme_map if t.get("종목들")]
    if valid:
        for theme in valid[:3]:
            대장율 = theme.get("대장등락률", "N/A")
            대장율_str = (
                f"{대장율:+.1f}%" if isinstance(대장율, float) else str(대장율)
            )
            lines.append(
                f"\n  [{theme['테마명']}]  "
                f"대장: {theme['대장주']} {대장율_str}"
            )
            for stock in theme.get("종목들", [])[:3]:
                등락 = stock["등락률"]
                소외 = stock["소외도"]
                등락_str = f"{등락:+.1f}%" if isinstance(등락, float) else str(등락)
                소외_str = f"{소외:.1f}"   if isinstance(소외, float) else str(소외)
                lines.append(
                    f"    {stock['포지션']:5s}  {stock['종목명']}"
                    f"  등락:{등락_str}  소외:{소외_str}"
                )
    else:
        # v2.1: 저변동 장세이거나 데이터 없을 때 구체적 안내
        if "저변동" in str(report.get("volatility", "")):
            lines.append(
                "  ⚪ 저변동 장세 — 순환매 에너지 없음\n"
                "  → 공시(신호1) 또는 리포트(신호3) 기반 개별 종목 집중 권장"
            )
        else:
            lines.append("  전날 급등 테마 없음 (상한가·급등 종목 미감지)")

    # ── 증권사 리포트
    lines.append("\n📋 <b>오늘 증권사 리포트</b>")
    if reports:
        for r in reports[:5]:
            종목 = r["종목명"]
            if 종목 == "종목미상":
                lines.append(f"  • {r['증권사']} | {r['내용'][:40]} | {r['액션']}")
            else:
                lines.append(f"  • {r['증권사']} | {종목} | {r['액션']}")
    else:
        lines.append("  네이버 API 키 설정 후 활성화")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    lines.append("⚠️ 투자 판단은 본인 책임. 참고용 정보입니다.")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# 마감봇 보고서 포맷 (변경 없음)
# ══════════════════════════════════════════════════════════════

def format_closing_report(report: dict) -> str:
    today_str     = report.get("today_str", "")
    target_str    = report.get("target_str", today_str)
    kospi         = report.get("kospi",         {})
    kosdaq        = report.get("kosdaq",        {})
    upper_limit   = report.get("upper_limit",   [])
    top_gainers   = report.get("top_gainers",   [])
    top_losers    = report.get("top_losers",    [])
    institutional = report.get("institutional", [])
    short_selling = report.get("short_selling", [])
    theme_map     = report.get("theme_map",     [])
    volatility    = report.get("volatility",    "판단불가")

    lines = []

    lines.append("📊 <b>마감 테마 레이더</b>")
    lines.append(f"📅 {today_str}  |  기준: {target_str} 마감")
    lines.append(f"📊 장세: <b>{volatility}</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    lines.append("\n📈 <b>오늘 지수</b>")
    if kospi:
        sign = "+" if kospi["change_rate"] >= 0 else ""
        lines.append(
            f"  코스피:  {kospi['close']:,.2f}  ({sign}{kospi['change_rate']:.2f}%)"
        )
    else:
        lines.append("  코스피:  N/A")
    if kosdaq:
        sign = "+" if kosdaq["change_rate"] >= 0 else ""
        lines.append(
            f"  코스닥:  {kosdaq['close']:,.2f}  ({sign}{kosdaq['change_rate']:.2f}%)"
        )
    else:
        lines.append("  코스닥:  N/A")

    if upper_limit:
        lines.append(f"\n🔒 <b>상한가 ({len(upper_limit)}종목)</b>")
        for s in upper_limit[:10]:
            lines.append(f"  • <b>{s['종목명']}</b> ({s['시장']})  {s['등락률']:+.1f}%")

    if top_gainers:
        lines.append(f"\n🚀 <b>급등 TOP {min(len(top_gainers),10)}</b>  (7%↑)")
        for s in top_gainers[:10]:
            lines.append(f"  • {s['종목명']}  {s['등락률']:+.1f}%  [{s['시장']}]")

    if top_losers:
        lines.append(f"\n📉 <b>급락 TOP {min(len(top_losers),5)}</b>  (-7%↓)")
        for s in top_losers[:5]:
            lines.append(f"  • {s['종목명']}  {s['등락률']:+.1f}%  [{s['시장']}]")

    lines.append("\n🏦 <b>기관/외인 순매수 상위</b>")
    inst_top = sorted(
        institutional, key=lambda x: x.get("기관순매수", 0), reverse=True
    )[:5]
    frgn_top = sorted(
        institutional, key=lambda x: x.get("외국인순매수", 0), reverse=True
    )[:5]
    if inst_top:
        items = "  ,  ".join(
            f"{s['종목명']}({s['기관순매수']//100_000_000:+,}억)"
            for s in inst_top if s.get("기관순매수", 0) > 0
        )
        lines.append(f"  기관: {items if items else 'N/A'}")
    else:
        lines.append("  기관: N/A")
    if frgn_top:
        items = "  ,  ".join(
            f"{s['종목명']}({s['외국인순매수']//100_000_000:+,}억)"
            for s in frgn_top if s.get("외국인순매수", 0) > 0
        )
        lines.append(f"  외인: {items if items else 'N/A'}")
    else:
        lines.append("  외인: N/A")

    if short_selling:
        lines.append("\n📌 <b>공매도 잔고 상위</b>")
        for s in short_selling[:5]:
            lines.append(f"  • {s['종목명']}  잔고율:{s['공매도잔고율']:.1f}%")

    lines.append("\n🗺️ <b>내일 순환매 지도</b>")
    valid = [t for t in theme_map if t.get("종목들")]
    if valid:
        for theme in valid[:5]:
            대장율 = theme.get("대장등락률", "N/A")
            대장율_str = (
                f"{대장율:+.1f}%" if isinstance(대장율, float) else str(대장율)
            )
            lines.append(
                f"\n  [{theme['테마명']}]  대장: {theme['대장주']} {대장율_str}"
            )
            for stock in theme.get("종목들", [])[:3]:
                등락 = stock["등락률"]
                소외 = stock["소외도"]
                등락_str = f"{등락:+.1f}%" if isinstance(등락, float) else str(등락)
                소외_str = f"{소외:.1f}"   if isinstance(소외, float) else str(소외)
                lines.append(
                    f"    {stock['포지션']:6s}  {stock['종목명']}"
                    f"  등락:{등락_str}  소외:{소외_str}"
                )
    else:
        lines.append("  상한가·급등 테마 데이터 없음")

    # ── [v3.2] T5 마감 강도 상위 ────────────────────────────
    closing_strength_result = report.get("closing_strength", [])
    if closing_strength_result:
        lines.append(f"\n💪 <b>마감강도 상위 (T5) — 내일 추가 상승 후보</b>")
        for s in closing_strength_result[:5]:
            vol_str = f"+{s['거래량증가율']:.0f}%거래량" if s.get("거래량증가율", 0) > 0 else ""
            lines.append(
                f"  • <b>{s['종목명']}</b>  강도:{s['마감강도']:.2f}  "
                f"{s['등락률']:+.1f}%  {vol_str}"
            )

    # ── [v3.2] T6 횡보 거래량 급증 ───────────────────────────
    volume_flat_result = report.get("volume_flat", [])
    if volume_flat_result:
        lines.append(f"\n🔮 <b>횡보 거래량 급증 (T6) — 세력 매집 의심</b>")
        for s in volume_flat_result[:5]:
            lines.append(
                f"  • <b>{s['종목명']}</b>  등락:{s['등락률']:+.1f}%  "
                f"거래량+{s['거래량증가율']:.0f}%"
            )

    # ── [v3.2] T3 시총 대비 자금 유입 ────────────────────────
    fund_inflow_result = report.get("fund_inflow", [])
    if fund_inflow_result:
        lines.append(f"\n💰 <b>시총 대비 집중 자금 유입 (T3)</b>")
        for s in fund_inflow_result[:5]:
            cap_str = f"{s['시가총액']//100_000_000:,}억"
            lines.append(
                f"  • <b>{s['종목명']}</b>  자금비율:{s['자금유입비율']:.2f}%  "
                f"시총:{cap_str}  {s['등락률']:+.1f}%"
            )

    lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    lines.append("⚠️ 투자 판단은 본인 책임. 참고용 정보입니다.")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# 장중 실시간 알림 포맷
# ══════════════════════════════════════════════════════════════

def format_realtime_alert(analysis: dict) -> str:
    직전대비 = analysis.get("직전대비", 0.0)
    거래량배율 = analysis.get("거래량배율", 0.0)   # v3.8: 누적RVOL 배수
    순간강도  = analysis.get("순간강도", 0.0)       # v3.8: 순간 Δvol%
    소스배지  = (
        "⚡ 갭상승모멘텀" if analysis.get("감지소스") == "gap_up"
        else "📊 거래량포착" if analysis.get("감지소스") == "volume"
        else "🎯 워치리스트" if analysis.get("감지소스") == "websocket"
        else "📈 등락률포착"
    )
    # v3.8: 거래량배율=누적RVOL, 순간강도=순간Δvol% 표시
    rvol_line = f"RVOL: 전일 대비 {거래량배율:.1f}배"
    if 순간강도 > 0:
        rvol_line += f"  |  순간강도: {순간강도:.0f}%"
    return (
        f"🚨 <b>급등 감지</b>  {소스배지}\n"
        f"종목: <b>{analysis['종목명']}</b> ({analysis['종목코드']})\n"
        f"등락률: +{analysis['등락률']:.1f}%  <b>(순간 +{직전대비:.1f}%)</b>\n"
        f"{rvol_line}\n"
        f"감지: {analysis['감지시각']}"
    )


def format_realtime_alert_ai(analysis: dict, ai_result: dict) -> str:
    판단  = ai_result.get("판단", "판단불가")
    이모지 = {"진짜급등": "✅", "작전주의심": "⚠️", "판단불가": "❓"}.get(판단, "❓")
    직전대비 = analysis.get("직전대비", 0.0)
    거래량배율 = analysis.get("거래량배율", 0.0)   # v3.8: 누적RVOL 배수
    순간강도  = analysis.get("순간강도", 0.0)       # v3.8: 순간 Δvol%
    소스배지  = (
        "⚡ 갭상승모멘텀" if analysis.get("감지소스") == "gap_up"
        else "📊 거래량포착" if analysis.get("감지소스") == "volume"
        else "🎯 워치리스트" if analysis.get("감지소스") == "websocket"
        else "📈 등락률포착"
    )
    rvol_line = f"RVOL: 전일 대비 {거래량배율:.1f}배"
    if 순간강도 > 0:
        rvol_line += f"  |  순간강도: {순간강도:.0f}%"
    return (
        f"🚨 <b>급등 감지 + AI 분석</b>  {소스배지}\n"
        f"종목: <b>{analysis['종목명']}</b> ({analysis['종목코드']})\n"
        f"등락률: +{analysis['등락률']:.1f}%  <b>(순간 +{직전대비:.1f}%)</b>\n"
        f"{rvol_line}\n\n"
        f"{이모지} AI 판단: <b>{판단}</b>\n"
        f"이유: {ai_result.get('이유', 'N/A')}"
    )


def format_trade_executed(
    ticker: str, name: str,
    buy_price: int, qty: int, total_amt: int,
    source: str, mode: str = "VTS"
) -> str:
    """
    자동매수 체결 알림 포맷 (Phase 4, v3.4 신규)

    Args:
        ticker:    종목코드
        name:      종목명
        buy_price: 매수가 (원)
        qty:       체결 수량
        total_amt: 총 매수 금액 (원)
        source:    감지 소스 (volume / rate / websocket / gap_up)
        mode:      "VTS"(모의) / "REAL"(실전)
    """
    import config
    mode_badge = "📋 모의투자" if mode == "VTS" else "💰 실전투자"
    source_badge = (
        "⚡ 갭상승모멘텀" if source == "gap_up"
        else "📊 거래량포착" if source == "volume"
        else "🎯 워치리스트" if source == "websocket"
        else "📈 등락률포착"
    )
    tp1 = round(buy_price * (1 + config.TAKE_PROFIT_1 / 100))
    tp2 = round(buy_price * (1 + config.TAKE_PROFIT_2 / 100))
    sl  = round(buy_price * (1 + config.STOP_LOSS / 100))

    return (
        f"📈 <b>자동매수 체결</b>  {mode_badge}\n"
        f"종목: <b>{name}</b> ({ticker})\n"
        f"체결가: {buy_price:,}원  수량: {qty}주\n"
        f"총 매수금액: {total_amt:,}원\n"
        f"감지 트리거: {source_badge}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"목표1: <b>{tp1:,}원</b> (+{config.TAKE_PROFIT_1:.0f}%)\n"
        f"목표2: <b>{tp2:,}원</b> (+{config.TAKE_PROFIT_2:.0f}%)\n"
        f"손절:  <b>{sl:,}원</b> ({config.STOP_LOSS:.0f}%)"
    )


def format_trade_closed(closed: dict) -> str:
    """
    포지션 청산 알림 포맷 (Phase 4, v3.4 신규)

    Args:
        closed: position_manager.close_position() 반환값
                {ticker, name, buy_price, sell_price, qty,
                 profit_rate, profit_amount, reason, mode}
    """
    ticker        = closed.get("ticker", "")
    name          = closed.get("name", ticker)
    buy_price     = closed.get("buy_price", 0)
    sell_price    = closed.get("sell_price", 0)
    qty           = closed.get("qty", 0)
    profit_rate   = closed.get("profit_rate", 0.0)
    profit_amount = closed.get("profit_amount", 0)
    reason        = closed.get("reason", "unknown")
    mode          = closed.get("mode", "VTS")

    mode_badge = "📋 모의투자" if mode == "VTS" else "💰 실전투자"

    reason_map = {
        "take_profit_1": ("✅", "1차 익절"),
        "take_profit_2": ("🏆", "2차 익절"),
        "stop_loss":     ("🔴", "손절"),
        "force_close":   ("⏰", "강제청산"),
        "manual":        ("🖐", "수동청산"),
    }
    emoji, label = reason_map.get(reason, ("❓", reason))
    sign = "+" if profit_rate >= 0 else ""
    amt_sign = "+" if profit_amount >= 0 else ""

    return (
        f"{emoji} <b>포지션 청산</b>  {mode_badge}  [{label}]\n"
        f"종목: <b>{name}</b> ({ticker})\n"
        f"매수가: {buy_price:,}원 → 매도가: {sell_price:,}원  ({qty}주)\n"
        f"수익률: <b>{sign}{profit_rate:.2f}%</b>  "
        f"손익: <b>{amt_sign}{profit_amount:,}원</b>"
    )


def _split_message(text: str, limit: int = 4096) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks

def format_weekly_report(stats: dict) -> str:
    """
    주간 성과 리포트 텔레그램 메시지 포맷 (Phase 3, v3.3)

    Args:
        stats: performance_tracker.get_weekly_stats() 반환값

    Returns:
        HTML 포맷 텔레그램 메시지
    """
    period        = stats.get("period", "N/A")
    total_alerts  = stats.get("total_alerts", 0)
    trigger_stats = stats.get("trigger_stats", [])
    top_picks     = stats.get("top_picks", [])
    miss_picks    = stats.get("miss_picks", [])

    lines = [
        f"📊 <b>주간 알림 성과 리포트</b>",
        f"📅 기간: {period}",
        f"📬 총 알림: {total_alerts}건",
        "",
    ]

    # ── 트리거별 승률 ─────────────────────────────────────────
    if trigger_stats:
        lines.append("🏆 <b>트리거별 7일 승률</b>")
        source_emoji = {
            "volume":    "📊 거래량급증",
            "rate":      "📈 등락률포착",
            "websocket": "🎯 워치리스트",
            "gap_up":    "⚡ 갭상승",
        }
        for t in trigger_stats:
            ttype    = t.get("trigger_type", "?")
            label    = source_emoji.get(ttype, ttype)
            n        = t.get("tracked_7d", 0)
            win_rate = t.get("win_rate_7d", 0.0)
            avg_ret  = t.get("avg_return_7d", 0.0)
            avg_sign = "+" if avg_ret >= 0 else ""
            if n == 0:
                lines.append(f"  {label}: 추적 데이터 없음")
            else:
                lines.append(
                    f"  {label}: 승률 <b>{win_rate:.0f}%</b> "
                    f"(n={n}) / 평균 {avg_sign}{avg_ret:.1f}%"
                )
        lines.append("")

    # ── 수익률 상위 종목 ──────────────────────────────────────
    if top_picks:
        lines.append("✅ <b>7일 수익률 상위</b>")
        for p in top_picks:
            ret  = p.get("return_7d", 0.0)
            name = p.get("name", p.get("ticker", "?"))
            src  = p.get("source", "?")
            lines.append(f"  {name}  <b>+{ret:.1f}%</b>  [{src}]")
        lines.append("")

    # ── 수익률 하위 종목 ──────────────────────────────────────
    if miss_picks and miss_picks[0].get("return_7d", 0) < 0:
        lines.append("⚠️ <b>7일 수익률 하위</b>")
        for p in miss_picks:
            ret  = p.get("return_7d", 0.0)
            name = p.get("name", p.get("ticker", "?"))
            src  = p.get("source", "?")
            sign = "+" if ret >= 0 else ""
            lines.append(f"  {name}  <b>{sign}{ret:.1f}%</b>  [{src}]")
        lines.append("")

    if not trigger_stats and not top_picks:
        lines.append("📭 아직 7일치 추적 데이터가 없습니다.")
        lines.append("(봇 운영 1주일 후부터 승률 집계 시작)")

    return "\n".join(lines)
