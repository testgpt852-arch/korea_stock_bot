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
    today_str    = report.get("today_str", "")
    prev_str     = report.get("prev_str", "")
    signals      = report.get("signals", [])
    us           = report.get("market_summary", {})
    commodities  = report.get("commodities", {})
    theme_map    = report.get("theme_map", [])
    volatility   = report.get("volatility", "판단불가")
    reports      = report.get("report_picks", [])
    ai_dart      = report.get("ai_dart_results", [])
    prev_kospi   = report.get("prev_kospi", {})    # v2.1
    prev_kosdaq  = report.get("prev_kosdaq", {})   # v2.1

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

    # ── 미국 섹터 연동 (v2.1 추가) ───────────────────────────
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
        if abs(pct) < 1.5:  # 1.5% 미만은 표시 생략
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

    lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    lines.append("⚠️ 투자 판단은 본인 책임. 참고용 정보입니다.")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# 장중 실시간 알림 포맷
# ══════════════════════════════════════════════════════════════

def format_realtime_alert(analysis: dict) -> str:
    return (
        f"🚨 <b>급등 감지</b>\n"
        f"종목: <b>{analysis['종목명']}</b> ({analysis['종목코드']})\n"
        f"등락률: +{analysis['등락률']:.1f}%\n"
        f"거래량: 전일 대비 {analysis['거래량배율']:.1f}배\n"
        f"감지: {analysis['감지시각']}"
    )


def format_realtime_alert_ai(analysis: dict, ai_result: dict) -> str:
    판단  = ai_result.get("판단", "판단불가")
    이모지 = {"진짜급등": "✅", "작전주의심": "⚠️", "판단불가": "❓"}.get(판단, "❓")
    return (
        f"🚨 <b>급등 감지 + AI 분석</b>\n"
        f"종목: <b>{analysis['종목명']}</b> ({analysis['종목코드']})\n"
        f"등락률: +{analysis['등락률']:.1f}%\n"
        f"거래량: 전일 대비 {analysis['거래량배율']:.1f}배\n\n"
        f"{이모지} AI 판단: <b>{판단}</b>\n"
        f"이유: {ai_result.get('이유', 'N/A')}"
    )


def _split_message(text: str, limit: int = 4096) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks
