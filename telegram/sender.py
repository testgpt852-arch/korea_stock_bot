"""
telegram/sender.py  [v12.0]
텔레그램 메시지 포맷 + 발송 전담
- 분석 로직 없음, 포맷 + 발송만
- v12.0: 마감봇(closing_report) 폐지 → format_closing_report*() 삭제
         수익률배치 15:45로 이동 (기존 18:45)
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

def format_pick_stocks_section(oracle_result: dict) -> str:
    """
    [v12.0] _pick_stocks() 반환값 → 텔레그램 포맷.

    아침봇·마감봇에서 모든 리포트보다 먼저 발송되는 "결론 섹션".
    윌리엄 오닐 CAN SLIM: 모든 픽에 진입가·목표가·손절가·R/R 명시.

    Args:
        oracle_result: _pick_stocks() 반환값

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
