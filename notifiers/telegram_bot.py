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
- v4.0: format_realtime_alert/ai — 호가 분석 결과 표시 (호가강도/매수매도비율/상위3집중도)
        format_trade_executed() — 모의/실전 매수 체결 알림
        format_trade_closed()   — 포지션 청산 알림 (익절/손절/강제청산)
- v5.0: [Phase 5] 리포트 품질 & UX 강화
        send_photo_async() — 차트 이미지(BytesIO) 텔레그램 전송
        format_morning_report() — 구조 개선: 시장환경 → 주요공시 → AI추천 순 재배치
        format_morning_summary() — 300자 이내 핵심 요약 (아침봇 요약 발송용)
        format_weekly_report()  — 요약 최적화 (상세링크 구조)
- v8.1: [쪽집게봇] format_oracle_section() 추가
- v10.0: format_morning_report()에 🌍 글로벌 트리거 섹션 추가
         geopolitics_data(신호6 분석 결과)가 있으면 미국증시 섹션 앞에 삽입
         format_morning_report() 파라미터에 geopolitics_data 추가
        oracle_analyzer.analyze() 반환값 → 텔레그램 포맷
        아침봇·마감봇 최우선 선발송 (결론 먼저, 데이터는 후발송)
        픽마다 진입가·목표가·손절가·R/R + 판단 근거 배지 표시
- v10.6: [Phase 4-2] 완전 분석 리포트 포맷 추가
         format_morning_report_full() — FULL_REPORT_FORMAT=true 전용
         format_closing_report_full() — FULL_REPORT_FORMAT=true 전용
         4단계 구조: ① 글로벌 트리거 → ② 테마 강도 → ③ 쪽집게 → ④ 리스크
         format_accuracy_stats() — 예측 정확도 + 신호 가중치 현황 포맷
         기존 format_morning_report() / format_closing_report() 하위 호환 유지
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
        loop = asyncio.get_event_loop()
        loop.run_until_complete(_send(text))


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


# ══════════════════════════════════════════════════════════════
# 쪽집게봇 — 내일 전략 선발송 포맷 (v8.1 신규)
# ══════════════════════════════════════════════════════════════

def format_oracle_section(oracle_result: dict) -> str:
    """
    [v8.1] oracle_analyzer.analyze() 반환값 → 텔레그램 포맷.

    아침봇·마감봇에서 모든 리포트보다 먼저 발송되는 "결론 섹션".
    윌리엄 오닐 CAN SLIM: 모든 픽에 진입가·목표가·손절가·R/R 명시.

    Args:
        oracle_result: oracle_analyzer.analyze() 반환값

    Returns:
        HTML 포맷 텔레그램 메시지. 픽 없으면 빈 문자열("") 반환.
    """
    if not oracle_result or not oracle_result.get("has_data"):
        return ""

    picks       = oracle_result.get("picks",       [])
    top_themes  = oracle_result.get("top_themes",  [])
    market_env  = oracle_result.get("market_env",  "")
    rr_threshold= oracle_result.get("rr_threshold", 1.5)
    one_line    = oracle_result.get("one_line",    "")

    if not picks:
        return ""

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🎯 <b>쪽집게 내일 전략</b>")

    # 시장 환경 + R/R 기준
    if market_env:
        env_emoji = "🟢" if "강세" in market_env else "🔴" if "약세" in market_env else "🟡"
        lines.append(f"{env_emoji} 시장: <b>{market_env}</b>  |  R/R 기준: {rr_threshold:.1f}x 이상")
    else:
        lines.append(f"⚪ 시장 환경 미지정  |  R/R 기준: {rr_threshold:.1f}x 이상")

    # ── 상위 테마 ─────────────────────────────────────────────
    if top_themes:
        lines.append("\n📡 <b>내일 주도 테마 예상</b>")
        for i, t in enumerate(top_themes[:3], 1):
            score   = t.get("score", 0)
            # 점수 시각화 (0~100 → 10칸 바)
            filled  = round(score / 10)
            bar     = "█" * filled + "░" * (10 - filled)
            leader  = t.get("leader", "")
            lc      = t.get("leader_change", 0.0)
            lc_str  = f"{lc:+.1f}%" if isinstance(lc, float) else str(lc)
            factors = " / ".join(t.get("factors", [])[:2])

            lines.append(
                f"  {i}위 <b>{t['theme']}</b>  {bar} {score}점"
            )
            if leader:
                lines.append(f"       대장: {leader} {lc_str}  ({factors})")

    # ── 종목 픽 ───────────────────────────────────────────────
    lines.append(f"\n💊 <b>종목 픽 ({len(picks)}종목)</b>")
    for p in picks:
        rank        = p.get("rank", 0)
        name        = p.get("name", "")
        theme       = p.get("theme", "")
        entry       = p.get("entry_price", 0)
        target      = p.get("target_price", 0)
        stop        = p.get("stop_price", 0)
        target_pct  = p.get("target_pct", 0.0)
        stop_pct    = p.get("stop_pct", -7.0)
        rr          = p.get("rr_ratio", 0.0)
        badges      = p.get("badges", [])
        pos_type    = p.get("position_type", "")

        # R/R 등급 이모지
        rr_emoji = "🔥" if rr >= 2.5 else "✅" if rr >= 1.5 else "➖"
        rr_stars = "★★" if rr >= 2.5 else "★" if rr >= 1.5 else ""

        # 포지션 타입 이모지
        pos_emoji = {"오늘★": "🔴", "내일": "🟠", "모니터": "🟡", "대장": "🔵"}.get(pos_type, "⚪")

        badge_str = "  ".join(badges) if badges else ""

        lines.append(
            f"\n  {rank}. {pos_emoji} <b>{name}</b>  [{theme}]  {badge_str}"
        )
        lines.append(
            f"     진입가: {entry:,}원 → 목표: {target:,}원 (<b>+{target_pct:.1f}%</b>) | "
            f"손절: {stop:,}원 ({stop_pct:.1f}%)"
        )
        lines.append(
            f"     R/R: <b>{rr:.1f}x</b> {rr_emoji}{rr_stars}"
        )

    # ── 한 줄 요약 ────────────────────────────────────────────
    lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📌 <b>한 줄 요약:</b> {one_line}")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# 아침봇 보고서 포맷
# ══════════════════════════════════════════════════════════════

def format_morning_report(report: dict, geopolitics_data: list = None) -> str:
    """
    [v5.0 Phase 5] 아침봇 리포트 구조 개선.

    섹션 순서 재배치:
    ① 헤더 + 시장 환경 요약 (전날 지수 + 미국증시 + 원자재)
       ↑ [v10.0] geopolitics_data가 있으면 🌍 글로벌 트리거 섹션 삽입
    ② 주요 공시 AI 분석 (가장 임팩트 높은 정보 먼저)
    ③ AI 추천 테마 / 발화 신호 (테마발화 + 기관/외인 수급)
    ④ 순환매 지도 + 증권사 리포트 (보조 정보)

    Args:
        report:           아침봇 분석 결과 dict
        geopolitics_data: geopolitics_analyzer.analyze() 반환값 (None이면 섹션 생략)
    """
    today_str        = report.get("today_str", "")
    prev_str         = report.get("prev_str", "")
    signals          = report.get("signals", [])
    us               = report.get("market_summary", {})
    commodities      = report.get("commodities", {})
    theme_map        = report.get("theme_map", [])
    volatility       = report.get("volatility", "판단불가")
    reports          = report.get("report_picks", [])
    ai_dart          = report.get("ai_dart_results", [])
    prev_kospi       = report.get("prev_kospi", {})
    prev_kosdaq      = report.get("prev_kosdaq", {})
    prev_institutional = report.get("prev_institutional", [])

    lines = []

    # ══ ① 헤더 + 시장환경 요약 ══════════════════════════════
    lines.append("📡 <b>아침 테마 레이더</b>")
    lines.append(f"📅 {today_str}  |  기준: {prev_str} 마감")
    lines.append(f"📊 전날 장세: <b>{volatility}</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    # 전날 지수
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

    # ── v10.0: 🌍 글로벌 트리거 섹션 ──────────────────────────
    if geopolitics_data:
        lines.append("\n🌍 <b>글로벌 트리거 — 오늘 왜 이 테마인가?</b>")
        # 신뢰도 상위 3건만 표시
        for event in geopolitics_data[:3]:
            impact = event.get("impact_direction", "+")
            confidence = event.get("confidence", 0.0)
            sectors = event.get("affected_sectors", [])
            summary = event.get("event_summary_kr", "")
            emoji  = "📈" if impact == "+" else "📉" if impact == "-" else "🔀"
            sector_str = " · ".join(sectors[:2])
            lines.append(
                f"  {emoji} <b>{sector_str}</b> — {summary[:50]} "
                f"[신뢰도:{confidence:.0%}]"
            )
        lines.append("")   # 공백 구분

    # 미국증시
    lines.append("\n🌏 <b>미국증시 (전날 마감)</b>")
    nasdaq = us.get("nasdaq", "N/A")
    sp500  = us.get("sp500",  "N/A")
    dow    = us.get("dow",    "N/A")
    lines.append(f"  나스닥: {nasdaq}  |  S&P500: {sp500}  |  다우: {dow}")
    summary = us.get("summary", "")
    if summary:
        lines.append(f"  📌 {summary}")

    # 미국 섹터 연동
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
        if abs(pct) < config.US_SECTOR_SIGNAL_MIN:
            continue
        arrow = "↑" if pct > 0 else "↓"
        sector_lines.append(f"  {arrow} {sector_name}: {change}")
    if sector_lines:
        lines.append("\n🏭 <b>미국 섹터 → 국내 연동 예상</b>")
        lines.extend(sector_lines[:4])

    # 원자재
    lines.append("\n🪙 <b>원자재 (전날 마감)</b>")
    for name, key in [
        ("구리 (LME)", "copper"),
        ("은 (COMEX)", "silver"),
        ("천연가스", "gas"),
        # v10.0 Phase 1: 철강 선행지표 추가
        ("철광석", "steel"),
        ("알루미늄 (LME)", "aluminum"),
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

    # ══ ② 주요 공시 AI 분석 ═════════════════════════════════
    # [v5.0] 공시 AI 분석을 앞으로 이동 — 가장 임팩트 높은 정보 우선 제공
    if ai_dart:
        lines.append("\n🤖 <b>AI 공시 분석</b>  ← 오늘 주목 종목")
        for r in ai_dart[:5]:
            점수 = r.get("점수", 5)
            확률 = r.get("상한가확률", "낮음")
            이유 = r.get("이유", "")
            bar  = "■" * 점수 + "□" * (10 - 점수)
            lines.append(
                f"  <b>{r['종목명']}</b>  [{bar}] {점수}/10  상한가:{확률}\n"
                f"  └ {이유}"
            )

    # 전날 기관/외인 순매수
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

    # ══ ③ AI 추천 테마 / 발화 신호 ═════════════════════════
    lines.append("\n🔴 <b>AI 추천 테마 발화 신호</b>")
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

    # ══ ④ 순환매 지도 + 증권사 리포트 ═════════════════════
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
        if "저변동" in str(report.get("volatility", "")):
            lines.append(
                "  ⚪ 저변동 장세 — 순환매 에너지 없음\n"
                "  → 공시(신호1) 또는 리포트(신호3) 기반 개별 종목 집중 권장"
            )
        else:
            lines.append("  전날 급등 테마 없음 (상한가·급등 종목 미감지)")

    # 증권사 리포트
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


def format_morning_summary(report: dict) -> str:
    """
    [v5.0 Phase 5] 아침봇 300자 이내 핵심 요약.
    상세 리포트 발송 전 선발송하는 초간결 버전.

    구성: 장세 + 주목 공시 1개 + 추천 테마 1~2개 → 300자 이내
    """
    volatility = report.get("volatility", "판단불가")
    signals    = report.get("signals", [])
    ai_dart    = report.get("ai_dart_results", [])
    today_str  = report.get("today_str", "")

    lines = [f"⚡ <b>오늘의 핵심 요약</b>  {today_str}"]
    lines.append(f"장세: <b>{volatility}</b>")

    # 최고 점수 공시 1개
    if ai_dart:
        top = max(ai_dart, key=lambda r: r.get("점수", 0))
        if top.get("점수", 0) >= 7:
            lines.append(f"🤖 주목공시: <b>{top['종목명']}</b> — {top.get('이유','')[:30]}")

    # 최강 신호 테마 1~2개
    top_signals = sorted(signals, key=lambda s: s.get("강도", 0), reverse=True)[:2]
    for s in top_signals:
        if s.get("강도", 0) >= 3:
            lines.append(f"🔴 <b>{s['테마명']}</b>  {'★'*min(s['강도'],5)}")

    summary = "\n".join(lines)
    # 300자 초과 시 자름
    if len(summary) > 300:
        summary = summary[:297] + "..."
    return summary


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
    직전대비  = analysis.get("직전대비", 0.0)
    거래량배율 = analysis.get("거래량배율", 0.0)   # v3.8: 누적RVOL 배수
    순간강도   = analysis.get("순간강도", 0.0)      # v3.8: 순간 Δvol%
    소스배지   = (
        "⚡ 갭상승모멘텀" if analysis.get("감지소스") == "gap_up"
        else "📊 거래량포착" if analysis.get("감지소스") == "volume"
        else "🎯 워치리스트" if analysis.get("감지소스") == "websocket"
        else "📈 등락률포착"
    )
    rvol_line = f"RVOL: 전일 대비 {거래량배율:.1f}배"
    if 순간강도 > 0:
        rvol_line += f"  |  순간강도: {순간강도:.0f}%"

    # [v4.0] 호가 분석 라인
    ob = analysis.get("호가분석")
    if ob:
        강도이모지 = "🔥" if ob["호가강도"] == "강세" else "⚠️" if ob["호가강도"] == "약세" else "➖"
        ob_line = (
            f"{강도이모지} 호가: {ob['호가강도']}  "
            f"매수/매도잔량={ob['매수매도비율']:.1f}x  "
            f"매도상위3집중={ob['상위3집중도']:.0%}\n"
        )
    else:
        ob_line = ""

    return (
        f"🚨 <b>급등 감지</b>  {소스배지}\n"
        f"종목: <b>{analysis['종목명']}</b> ({analysis['종목코드']})\n"
        f"등락률: +{analysis['등락률']:.1f}%"
        + (f"  <b>(순간 +{직전대비:.1f}%)</b>" if 직전대비 > 0 else "") + "\n"
        + f"{rvol_line}\n"
        + f"{ob_line}"
        + f"감지: {analysis['감지시각']}"
    )


def format_realtime_alert_ai(analysis: dict, ai_result: dict) -> str:
    """
    [v4.2] R/R 비율 + 목표가/손절가 라인 추가
    """
    판단   = ai_result.get("판단", "판단불가")
    이모지  = {"진짜급등": "✅", "작전주의심": "⚠️", "판단불가": "❓"}.get(판단, "❓")
    직전대비  = analysis.get("직전대비", 0.0)
    거래량배율 = analysis.get("거래량배율", 0.0)
    순간강도   = analysis.get("순간강도", 0.0)
    소스배지   = (
        "⚡ 갭상승모멘텀" if analysis.get("감지소스") == "gap_up"
        else "📊 거래량포착" if analysis.get("감지소스") == "volume"
        else "🎯 워치리스트" if analysis.get("감지소스") == "websocket"
        else "📈 등락률포착"
    )
    rvol_line = f"RVOL: 전일 대비 {거래량배율:.1f}배"
    if 순간강도 > 0:
        rvol_line += f"  |  순간강도: {순간강도:.0f}%"

    # [v4.0] 호가 분석 라인
    ob = analysis.get("호가분석")
    if ob:
        강도이모지 = "🔥" if ob["호가강도"] == "강세" else "⚠️" if ob["호가강도"] == "약세" else "➖"
        ob_line = (
            f"{강도이모지} 호가: {ob['호가강도']}  "
            f"매수/매도잔량={ob['매수매도비율']:.1f}x  "
            f"매도상위3집중={ob['상위3집중도']:.0%}\n"
        )
    else:
        ob_line = ""

    # [v4.2] R/R + 목표가/손절가 라인 (AI 제공 시에만 표시)
    target = ai_result.get("target_price")
    stop   = ai_result.get("stop_loss")
    rr     = ai_result.get("risk_reward_ratio")

    if target and stop and rr:
        rr_line = (
            f"📊 R/R: <b>{rr:.1f}</b>  "
            f"목표가: {target:,}원  /  손절가: {stop:,}원\n"
        )
    elif rr:
        rr_line = f"📊 R/R: <b>{rr:.1f}</b>\n"
    else:
        rr_line = ""

    return (
        f"🚨 <b>급등 감지 + AI 분석</b>  {소스배지}\n"
        f"종목: <b>{analysis['종목명']}</b> ({analysis['종목코드']})\n"
        f"등락률: +{analysis['등락률']:.1f}%"
        + (f"  <b>(순간 +{직전대비:.1f}%)</b>" if 직전대비 > 0 else "") + "\n"
        + f"{rvol_line}\n"
        + f"{ob_line}"
        + f"{이모지} AI 판단: <b>{판단}</b>\n"
        + f"이유: {ai_result.get('이유', 'N/A')}\n"
        + f"{rr_line}"
    ).rstrip()


def format_trade_executed(
    ticker: str, name: str,
    buy_price: int, qty: int, total_amt: int,
    source: str, mode: str = "VTS",
    stop_loss_price: int | None = None,   # [v4.2] AI 제공 손절가 (원)
    market_env: str = "",                  # [v4.2] 시장 환경
) -> str:
    """
    자동매수 체결 알림 포맷 (Phase 4, v3.4 신규 / v4.2 확장)

    [v4.2] stop_loss_price / market_env 추가:
    - stop_loss_price: AI 제공 시 별도 표시. None이면 config 기본값(-3%) 표시.
    - market_env: 시장 환경 배지 표시 (강세장/약세장/횡보 구분)
    """
    import config
    mode_badge = "📋 모의투자" if mode == "VTS" else "💰 실전투자"
    source_badge = (
        "⚡ 갭상승모멘텀" if source == "gap_up"
        else "📊 거래량포착" if source == "volume"
        else "🎯 워치리스트" if source == "websocket"
        else "📈 등락률포착"
    )

    # [v4.2] 시장 환경 배지
    if "강세장" in market_env:
        env_badge = "📈 강세장 (R/R 1.2+)"
    elif "약세장" in market_env or "횡보" in market_env:
        env_badge = "📉 약세장/횡보 (R/R 2.0+)"
    else:
        env_badge = ""

    tp1 = round(buy_price * (1 + config.TAKE_PROFIT_1 / 100))
    tp2 = round(buy_price * (1 + config.TAKE_PROFIT_2 / 100))

    # [v4.2] 손절가: AI 제공값 우선, 없으면 config 기본값
    if stop_loss_price and stop_loss_price > 0:
        sl       = stop_loss_price
        sl_label = "AI 손절"
        sl_pct   = round((stop_loss_price - buy_price) / buy_price * 100, 1)
        sl_str   = f"{sl:,}원 ({sl_pct:+.1f}%) — AI 제공"
    else:
        sl       = round(buy_price * (1 + config.STOP_LOSS / 100))
        sl_label = "손절"
        sl_str   = f"{sl:,}원 ({config.STOP_LOSS:.0f}%) — 기본값"

    env_line = f"시장 환경: {env_badge}\n" if env_badge else ""

    return (
        f"📈 <b>자동매수 체결</b>  {mode_badge}\n"
        f"종목: <b>{name}</b> ({ticker})\n"
        f"체결가: {buy_price:,}원  수량: {qty}주\n"
        f"총 매수금액: {total_amt:,}원\n"
        f"감지 트리거: {source_badge}\n"
        f"{env_line}"
        f"━━━━━━━━━━━━━━━━\n"
        f"목표1: <b>{tp1:,}원</b> (+{config.TAKE_PROFIT_1:.0f}%)\n"
        f"목표2: <b>{tp2:,}원</b> (+{config.TAKE_PROFIT_2:.0f}%)\n"
        f"{sl_label}:  <b>{sl_str}</b>\n"
        f"Trailing Stop: 고점 대비 {'8%' if '강세장' in market_env else '5%'} 이탈 시 자동 청산"
    )


def format_trade_closed(closed: dict) -> str:
    """
    포지션 청산 알림 포맷 (Phase 4, v3.4 신규 / v4.2 확장)

    [v4.2] trailing_stop 청산 사유 추가:
    closed["reason"] = "trailing_stop" → 📈 Trailing Stop 표시

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
        "take_profit_1":  ("✅", "1차 익절"),
        "take_profit_2":  ("🏆", "2차 익절"),
        "stop_loss":      ("🔴", "손절"),
        "trailing_stop":  ("📈", "Trailing Stop"),   # [v4.2] 신규
        "force_close":    ("⏰", "강제청산"),
        "manual":         ("🖐", "수동청산"),
    }
    emoji, label = reason_map.get(reason, ("❓", reason))
    sign     = "+" if profit_rate   >= 0 else ""
    amt_sign = "+" if profit_amount >= 0 else ""

    # [v4.2] trailing_stop 시 추가 설명
    trailing_note = (
        "\n💡 고점 대비 임계 이탈로 자동 손절가 작동"
        if reason == "trailing_stop" else ""
    )

    return (
        f"{emoji} <b>포지션 청산</b>  {mode_badge}  [{label}]\n"
        f"종목: <b>{name}</b> ({ticker})\n"
        f"매수가: {buy_price:,}원 → 매도가: {sell_price:,}원  ({qty}주)\n"
        f"수익률: <b>{sign}{profit_rate:.2f}%</b>  "
        f"손익: <b>{amt_sign}{profit_amount:,}원</b>"
        f"{trailing_note}"
    )


def _split_message(text: str, limit: int = 4096) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks

def format_weekly_report(stats: dict, weekly_patterns: list | None = None) -> str:
    """
    주간 성과 리포트 텔레그램 메시지 포맷 (Phase 3, v3.3 / v4.3 Phase3 업데이트)

    Args:
        stats:           performance_tracker.get_weekly_stats() 반환값
        weekly_patterns: [v4.3] trading_journal.get_weekly_patterns() 반환값 (선택)
                         None 또는 빈 리스트면 패턴 섹션 생략

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

    # ── [v4.3 Phase 3] 이번 주 학습한 패턴 ──────────────────
    if weekly_patterns:
        lines.append("🧠 <b>이번 주 학습한 패턴 Top5</b>")
        tag_emoji = {
            "강세장진입":      "📈",
            "약세장진입":      "📉",
            "원칙준수익절":    "✅",
            "트레일링스탑작동": "🔄",
            "손절지연":        "⚠️",
            "갭상승성공":      "⚡",
            "갭상승실패":      "❌",
            "워치리스트조기":  "🎯",
            "큰수익":          "💰",
            "큰손실":          "🔴",
            "강제청산":        "⏰",
        }
        for p in weekly_patterns[:5]:
            tag     = p.get("tag", "?")
            count   = p.get("count", 0)
            win_r   = p.get("win_rate", 0.0)
            avg_p   = p.get("avg_profit", 0.0)
            lesson  = p.get("lesson_sample", "")
            emoji   = tag_emoji.get(tag, "•")
            avg_sign = "+" if avg_p >= 0 else ""
            line = (
                f"  {emoji} <b>{tag}</b>: {count}회 / "
                f"승률 {win_r:.0f}% / 평균 {avg_sign}{avg_p:.1f}%"
            )
            if lesson:
                line += f"\n    └ {lesson[:35]}"
            lines.append(line)
        lines.append("")

    if not trigger_stats and not top_picks:
        lines.append("📭 아직 7일치 추적 데이터가 없습니다.")
        lines.append("(봇 운영 1주일 후부터 승률 집계 시작)")

    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════
# [v10.6 Phase 4-2] 완전 분석 리포트 포맷 (FULL_REPORT_FORMAT=true)
# 4단계 구조: ① 글로벌 트리거 → ② 테마 강도 → ③ 쪽집게 → ④ 리스크
# ══════════════════════════════════════════════════════════════

def format_morning_report_full(
    report: dict,
    geopolitics_data: list = None,
) -> str:
    """
    [v10.6 Phase 4-2] FULL_REPORT_FORMAT=true 전용 아침봇 리포트.

    4단계 구조:
    ① 글로벌 트리거 — 지정학 이벤트 + 미국증시 + 원자재 (왜 오늘 이 테마인가?)
    ② 테마 강도 — 신호 강도 + 섹터 수급 + DataLab 트렌드 (무엇이 달아오르고 있는가?)
    ③ 쪽집게 — oracle 픽 + 진입조건 (어디에 들어가야 하는가?)
    ④ 리스크 — 시장 변동성 + 공시 AI 경고 + 예측 정확도 (얼마나 위험한가?)

    FULL_REPORT_FORMAT=false(기본)이면 기존 format_morning_report() 사용.
    """
    today_str     = report.get("today_str", "")
    prev_str      = report.get("prev_str", "")
    signals       = report.get("signals", [])
    us            = report.get("market_summary", {})
    commodities   = report.get("commodities", {})
    theme_map     = report.get("theme_map", [])
    volatility    = report.get("volatility", "판단불가")
    ai_dart       = report.get("ai_dart_results", [])
    prev_kospi    = report.get("prev_kospi", {})
    prev_kosdaq   = report.get("prev_kosdaq", {})
    prev_inst     = report.get("prev_institutional", [])
    oracle        = report.get("oracle", {}) or {}

    lines = []
    lines.append("📡 <b>아침 완전 분석 리포트</b>")
    lines.append(f"📅 {today_str}  |  기준: {prev_str} 마감")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    # ══ ① 글로벌 트리거 ══════════════════════════════════════
    lines.append("\n🌍 <b>① 글로벌 트리거 — 왜 오늘 이 테마인가?</b>")

    # 지정학 이벤트
    if geopolitics_data:
        for event in geopolitics_data[:3]:
            impact = event.get("impact_direction", "+")
            confidence = event.get("confidence", 0.0)
            sectors = event.get("affected_sectors", [])
            summary = event.get("event_summary_kr", "")
            emoji = "📈" if impact == "+" else "📉" if impact == "-" else "🔀"
            sector_str = " · ".join(sectors[:2])
            lines.append(
                f"  {emoji} <b>{sector_str}</b> — {summary[:50]} "
                f"[신뢰도:{confidence:.0%}]"
            )
    else:
        lines.append("  지정학 이벤트 없음 (GEOPOLITICS_ENABLED=true 시 표시)")

    # 미국증시 요약
    nasdaq = us.get("nasdaq", "N/A")
    sp500  = us.get("sp500",  "N/A")
    lines.append(f"\n  나스닥: {nasdaq}  |  S&P500: {sp500}")
    summary = us.get("summary", "")
    if summary:
        lines.append(f"  📌 {summary}")

    # 미국 섹터 → 국내 연동
    sectors = us.get("sectors", {})
    sector_lines = []
    for sname, sdata in sectors.items():
        change = sdata.get("change", "N/A")
        if change == "N/A":
            continue
        try:
            pct = float(change.replace("%", "").replace("+", ""))
        except ValueError:
            continue
        if abs(pct) < config.US_SECTOR_SIGNAL_MIN:
            continue
        arrow = "↑" if pct > 0 else "↓"
        sector_lines.append(f"  {arrow} {sname}: {change}")
    if sector_lines:
        lines.append("  <b>섹터 연동 예상:</b>")
        lines.extend(sector_lines[:3])

    # 핵심 원자재
    lines.append("")
    for name, key in [("구리", "copper"), ("철광석", "steel"), ("천연가스", "gas")]:
        c = commodities.get(key, {})
        price = c.get("price", "N/A")
        change = c.get("change", "N/A")
        unit = c.get("unit", "")
        if price != "N/A":
            lines.append(f"  {name}: {price} {unit}  {change}")

    # ══ ② 테마 강도 ═══════════════════════════════════════════
    lines.append("\n🔴 <b>② 테마 강도 — 무엇이 달아오르고 있는가?</b>")

    top_signals = [s for s in signals if s.get("강도", 0) >= 3][:6]
    if top_signals:
        for s in top_signals:
            star = "★" * min(s["강도"], 5)
            badges = []
            발화 = s.get("발화신호", "")
            for sig_label in ["신호7", "신호8", "신호6", "신호5", "신호3", "신호1"]:
                if sig_label in 발화:
                    badges.append(sig_label)
            badge_str = " ".join(f"[{b}]" for b in badges[:2])
            lines.append(
                f"\n  {star} <b>{s['테마명']}</b> {badge_str}"
            )
            lines.append(f"    └ {s['발화신호']}")
            ai_memo = s.get("ai_메모", "")
            if ai_memo:
                lines.append(f"    ✦ {ai_memo}")
    else:
        lines.append("  감지된 주요 신호 없음")

    # 순환매 지도 (소외도 상위 테마)
    valid_themes = [t for t in theme_map if t.get("종목들")]
    if valid_themes:
        lines.append("\n  <b>순환매 에너지 (소외도 상위)</b>")
        for theme in valid_themes[:3]:
            대장율 = theme.get("대장등락률", "N/A")
            대장율_str = f"{대장율:+.1f}%" if isinstance(대장율, float) else str(대장율)
            avg_소외 = _calc_avg_소외(theme)
            lines.append(
                f"  [{theme['테마명']}]  대장: {theme['대장주']} {대장율_str}"
                f"  소외도 평균: {avg_소외:.1f}"
            )

    # 기관/외인 수급
    if prev_inst:
        inst_top = sorted(prev_inst, key=lambda x: x.get("기관순매수", 0), reverse=True)[:3]
        inst_items = [
            f"{s['종목명']}({s['기관순매수'] // 100_000_000:+,}억)"
            for s in inst_top if s.get("기관순매수", 0) > 0
        ]
        if inst_items:
            lines.append(f"\n  🏦 기관 순매수: {', '.join(inst_items)}")

    # ══ ③ 쪽집게 ══════════════════════════════════════════════
    lines.append("\n🎯 <b>③ 쪽집게 — 어디에 들어가야 하는가?</b>")

    picks = oracle.get("picks", [])
    rr_thr = oracle.get("rr_threshold", 1.5)
    market_env_str = oracle.get("market_env", "")
    one_line = oracle.get("one_line", "")

    if picks:
        lines.append(
            f"  시장환경: <b>{market_env_str or '미분류'}</b>  |  최소 R/R: {rr_thr}"
        )
        for pick in picks[:5]:
            rank = pick.get("rank", "?")
            name = pick.get("name", "?")
            theme = pick.get("theme", "")
            entry = pick.get("entry_price", 0)
            target = pick.get("target_price", 0)
            stop = pick.get("stop_price", 0)
            target_pct = pick.get("target_pct", 0)
            rr = pick.get("rr_ratio", 0)
            score = pick.get("score", 0)
            badges = pick.get("badges", [])
            pos_type = pick.get("position_type", "")

            badge_str = " ".join(f"[{b}]" for b in badges[:3])
            lines.append(
                f"\n  <b>#{rank} {name}</b> [{pos_type}]  점수:{score}"
            )
            lines.append(f"    테마: {theme}")
            lines.append(
                f"    진입: {entry:,}  목표: {target:,}(+{target_pct:.0f}%)  "
                f"손절: {stop:,}(-7%)  R/R:{rr:.1f}"
            )
            if badge_str:
                lines.append(f"    {badge_str}")
        if one_line:
            lines.append(f"\n  💡 {one_line}")
    else:
        lines.append("  쪽집게 픽 없음 (데이터 부족 또는 고위험 장세)")

    # ══ ④ 리스크 ══════════════════════════════════════════════
    lines.append("\n⚠️ <b>④ 리스크 — 얼마나 위험한가?</b>")
    lines.append(f"  장세: <b>{volatility}</b>")

    # 전날 지수
    if prev_kospi:
        sign = "+" if prev_kospi.get("change_rate", 0) >= 0 else ""
        lines.append(
            f"  코스피: {prev_kospi.get('close', 'N/A'):,.2f} "
            f"({sign}{prev_kospi.get('change_rate', 0):.2f}%)"
        )
    if prev_kosdaq:
        sign = "+" if prev_kosdaq.get("change_rate", 0) >= 0 else ""
        lines.append(
            f"  코스닥: {prev_kosdaq.get('close', 'N/A'):,.2f} "
            f"({sign}{prev_kosdaq.get('change_rate', 0):.2f}%)"
        )

    # AI 공시 경고 (점수 낮은 종목)
    danger_dart = [r for r in ai_dart if r.get("점수", 10) <= 4]
    if danger_dart:
        lines.append(f"  🚨 주의 공시 종목: {', '.join(r['종목명'] for r in danger_dart[:3])}")

    # 변동성 경고
    if "고변동" in str(volatility):
        lines.append("  🔴 고변동 장세 — 포지션 크기 50% 축소 권장")
    elif "저변동" in str(volatility):
        lines.append("  ⚪ 저변동 장세 — 순환매 에너지 부족. 개별 공시주 집중 권장")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    lines.append("⚠️ 투자 판단은 본인 책임. 참고용 정보입니다.")
    return "\n".join(lines)


def format_closing_report_full(report: dict) -> str:
    """
    [v10.6 Phase 4-2] FULL_REPORT_FORMAT=true 전용 마감봇 리포트.

    4단계 구조:
    ① 글로벌 트리거 — 오늘 장을 움직인 원인 분석
    ② 테마 강도 — 오늘 실제 급등 테마 + T5/T6/T3 트리거
    ③ 쪽집게 — 내일 픽 + 진입조건 (oracle 결과)
    ④ 리스크 — 공매도 잔고 + 리스크 경고 + 예측 정확도
    """
    today_str       = report.get("today_str", "")
    target_str      = report.get("target_str", today_str)
    kospi           = report.get("kospi",         {})
    kosdaq          = report.get("kosdaq",        {})
    upper_limit     = report.get("upper_limit",   [])
    top_gainers     = report.get("top_gainers",   [])
    top_losers      = report.get("top_losers",    [])
    institutional   = report.get("institutional", [])
    short_selling   = report.get("short_selling", [])
    theme_map       = report.get("theme_map",     [])
    volatility      = report.get("volatility",    "판단불가")
    cs_result       = report.get("closing_strength", [])
    vf_result       = report.get("volume_flat",   [])
    fi_result       = report.get("fund_inflow",   [])
    oracle          = report.get("oracle", {}) or {}
    accuracy_stats  = report.get("accuracy_stats", {}) or {}

    lines = []
    lines.append("📊 <b>마감 완전 분석 리포트</b>")
    lines.append(f"📅 {today_str}  |  기준: {target_str} 마감")
    lines.append(f"📊 장세: <b>{volatility}</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    # 지수 요약
    if kospi:
        sign = "+" if kospi["change_rate"] >= 0 else ""
        lines.append(
            f"\n  코스피: {kospi['close']:,.2f} ({sign}{kospi['change_rate']:.2f}%)"
        )
    if kosdaq:
        sign = "+" if kosdaq["change_rate"] >= 0 else ""
        lines.append(
            f"  코스닥: {kosdaq['close']:,.2f} ({sign}{kosdaq['change_rate']:.2f}%)"
        )

    # ══ ① 글로벌 트리거 ══════════════════════════════════════
    lines.append("\n🌍 <b>① 오늘 장을 움직인 원인</b>")
    if upper_limit:
        lines.append(f"  🔒 상한가 {len(upper_limit)}종목: " +
                     ", ".join(s["종목명"] for s in upper_limit[:5]))
    if top_gainers:
        lines.append(f"  🚀 급등 상위: " +
                     ", ".join(
                         f"{s['종목명']}({s['등락률']:+.1f}%)"
                         for s in top_gainers[:5]
                     ))
    if top_losers:
        lines.append(f"  📉 급락 상위: " +
                     ", ".join(
                         f"{s['종목명']}({s['등락률']:+.1f}%)"
                         for s in top_losers[:3]
                     ))

    # ══ ② 테마 강도 ═══════════════════════════════════════════
    lines.append("\n🔴 <b>② 오늘 실제 급등 테마 + 트리거</b>")

    valid_themes = [t for t in theme_map if t.get("종목들")]
    if valid_themes:
        for theme in valid_themes[:5]:
            대장율 = theme.get("대장등락률", "N/A")
            대장율_str = f"{대장율:+.1f}%" if isinstance(대장율, float) else str(대장율)
            avg_소외 = _calc_avg_소외(theme)
            lines.append(
                f"\n  [{theme['테마명']}]  대장: {theme['대장주']} {대장율_str}"
                f"  소외도:{avg_소외:.1f}"
            )
            for stock in theme.get("종목들", [])[:3]:
                등락 = stock["등락률"]
                소외 = stock["소외도"]
                등락_str = f"{등락:+.1f}%" if isinstance(등락, float) else str(등락)
                소외_str = f"{소외:.1f}" if isinstance(소외, float) else str(소외)
                lines.append(
                    f"    {stock['포지션']:6s}  {stock['종목명']}"
                    f"  등락:{등락_str}  소외:{소외_str}"
                )

    # T5/T6/T3 트리거
    if cs_result:
        lines.append(f"\n  💪 T5 마감강도: " +
                     ", ".join(
                         f"{s['종목명']}(강도:{s['마감강도']:.2f})"
                         for s in cs_result[:4]
                     ))
    if vf_result:
        lines.append(f"  🔮 T6 횡보급증: " +
                     ", ".join(s["종목명"] for s in vf_result[:4]))
    if fi_result:
        lines.append(f"  💰 T3 자금유입: " +
                     ", ".join(
                         f"{s['종목명']}({s['자금유입비율']:.1f}%)"
                         for s in fi_result[:4]
                     ))

    # 기관/외인 수급
    inst_top = sorted(institutional, key=lambda x: x.get("기관순매수", 0), reverse=True)[:4]
    frgn_top = sorted(institutional, key=lambda x: x.get("외국인순매수", 0), reverse=True)[:4]
    if inst_top:
        inst_items = [
            f"{s['종목명']}({s['기관순매수'] // 100_000_000:+,}억)"
            for s in inst_top if s.get("기관순매수", 0) > 0
        ]
        if inst_items:
            lines.append(f"\n  🏦 기관: {', '.join(inst_items)}")
    if frgn_top:
        frgn_items = [
            f"{s['종목명']}({s['외국인순매수'] // 100_000_000:+,}억)"
            for s in frgn_top if s.get("외국인순매수", 0) > 0
        ]
        if frgn_items:
            lines.append(f"  🌐 외인: {', '.join(frgn_items)}")

    # ══ ③ 쪽집게 ══════════════════════════════════════════════
    lines.append("\n🎯 <b>③ 내일 쪽집게 픽</b>")

    picks = oracle.get("picks", [])
    rr_thr = oracle.get("rr_threshold", 1.5)
    market_env_str = oracle.get("market_env", "")
    one_line = oracle.get("one_line", "")

    if picks:
        lines.append(
            f"  시장환경: <b>{market_env_str or '미분류'}</b>  |  최소 R/R: {rr_thr}"
        )
        for pick in picks[:5]:
            rank = pick.get("rank", "?")
            name = pick.get("name", "?")
            theme = pick.get("theme", "")
            entry = pick.get("entry_price", 0)
            target = pick.get("target_price", 0)
            stop = pick.get("stop_price", 0)
            target_pct = pick.get("target_pct", 0)
            rr = pick.get("rr_ratio", 0)
            score = pick.get("score", 0)
            badges = pick.get("badges", [])
            pos_type = pick.get("position_type", "")

            badge_str = " ".join(f"[{b}]" for b in badges[:3])
            lines.append(f"\n  <b>#{rank} {name}</b> [{pos_type}]  점수:{score}")
            lines.append(f"    테마: {theme}")
            lines.append(
                f"    진입: {entry:,}  목표: {target:,}(+{target_pct:.0f}%)  "
                f"손절: {stop:,}(-7%)  R/R:{rr:.1f}"
            )
            if badge_str:
                lines.append(f"    {badge_str}")
        if one_line:
            lines.append(f"\n  💡 {one_line}")
    else:
        lines.append("  내일 픽 없음 (데이터 부족)")

    # ══ ④ 리스크 ══════════════════════════════════════════════
    lines.append("\n⚠️ <b>④ 리스크 현황</b>")

    # 공매도 잔고
    if short_selling:
        lines.append("  📌 공매도 잔고 상위:")
        for s in short_selling[:4]:
            lines.append(f"    • {s['종목명']}  잔고율:{s['공매도잔고율']:.1f}%")

    # 변동성 경고
    if "고변동" in str(volatility):
        lines.append("  🔴 고변동 장세 — 손절 철칙(-7%) 엄수 필수")
    elif "저변동" in str(volatility):
        lines.append("  ⚪ 저변동 — 오닐 공식 확인종목(거래량+50%) 우선")
    else:
        lines.append("  🟡 중변동 — 표준 R/R 1.5 이상 종목만 진입")

    # [v10.7 이슈 #13] 인라인 accuracy_stats 블록 → format_accuracy_stats() 호출로 교체
    # /status 명령어·주간 리포트에서도 재사용 가능한 독립 포맷 함수 활용
    acc_section = format_accuracy_stats(accuracy_stats)
    if acc_section:
        lines.append("")
        lines.extend(acc_section.splitlines())

    lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    lines.append("⚠️ 투자 판단은 본인 책임. 참고용 정보입니다.")
    return "\n".join(lines)


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
