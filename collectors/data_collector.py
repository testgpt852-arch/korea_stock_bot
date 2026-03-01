"""
collectors/data_collector.py
데이터 수집 총괄 — 원시 데이터 수집·캐싱·텔레그램 발송 (v13.0 Step 4)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[역할]
  06:00 단일 스케줄로 모든 수집기를 asyncio.gather() 병렬 실행.
  숫자 기준 필터링만 적용 (하드코딩 키워드 매핑 전면 제거).
  필터링된 원시 데이터를 전역 캐시에 저장 후 텔레그램으로 발송.

[병렬 수집 대상 — asyncio.gather()]
  ① filings            — DART 공시 (전날, 본문요약 포함)
  ② market_global      — 미국증시 + 원자재 (전날, ±2%+ 필터)
  ③ news_naver         — 네이버 뉴스 (당일)
  ④ news_newsapi       — NewsAPI 글로벌 뉴스 (당일)
  ⑤ news_global_rss    — 글로벌 RSS 뉴스 (지정학)
  ⑥ price_domestic     — 전날 가격/기관/외인 데이터 (시총 3000억 이하)
  ⑦ sector_etf         — 섹터 ETF 자금흐름 (전날)
  ⑧ short_interest     — 공매도 잔고 (전날, 상위 20)
  ⑨ event_calendar     — 기업 이벤트 캘린더 (당일)
  ⑩ closing_strength   — 마감강도 (전날, 상위 20)
  ⑪ volume_surge       — 거래량급증 (전날, 상위 20)
  ⑫ fund_concentration — 자금집중 (전날, 상위 20)

[캐시 구조 — get_cache()]
  {
    "collected_at":              str,          # KST ISO 수집 시각
    "dart_data":                 list[dict],   # 본문요약 포함
    "market_data":               dict,         # ±2%+ 섹터ETF만
    "news_naver":                dict,         # 최신 30건
    "news_newsapi":              dict,         # 최신 20건
    "news_global_rss":           list[dict],
    "price_data":                dict | None,  # 시총 3000억 이하 필터 적용
    "sector_etf_data":           list[dict],
    "short_data":                list[dict],   # 상위 20종목
    "event_calendar":            list[dict],
    "closing_strength_result":   list[dict],   # 상위 20종목
    "volume_surge_result":       list[dict],   # 상위 20종목
    "fund_concentration_result": list[dict],   # 상위 20종목
    "success_flags":             dict[str, bool],
  }

[절대 금지 — ARCHITECTURE 준수]
  이 파일에서 AI API 호출 금지
  이 파일에서 DB 기록 금지
  수집·캐싱·원시데이터 발송만 담당

[삭제된 함수 — v13.0]
  _build_signals()         — 하드코딩 매핑 기반 신호 생성 전면 삭제
  _compute_score_summary() — 데드코드, 삭제
  _sig_us_market()         — 하드코딩 매핑 사용, 삭제
  _sig_steel_etf()         — 하드코딩 매핑 사용, 삭제
  _sig_sector_top()        — 하드코딩 매핑 사용, 삭제
  _sig_geopolitics_from_rss() — 삭제
  _sig_sector_flow()       — 삭제
  _sig_datalab_trends()    — 삭제
  _sig_prev_price()        — 삭제
  _sig_dart_strength()     — 삭제
  _sig_dart_to_theme()     — 삭제
  _sig_event_impact()      — 삭제

[수정이력]
  v12.0 Step 7: 신규 생성
  v12.0 Step 8: signal_analyzer 흡수 — _build_signals() 추가
  v13.0 Step 4: _build_signals()·_compute_score_summary() 및 하위 신호 함수 전면 삭제
                캐시 구조 단순화 (signals·market_summary 등 삭제된 키 제거)
                원시 데이터 텔레그램 발송 _send_raw_data_to_telegram() 신규 추가
"""

import asyncio
from datetime import datetime, timezone, timedelta
from utils.logger import logger
from utils.date_utils import get_today, get_prev_trading_day, fmt_ymd
import config

KST = timezone(timedelta(hours=9))

# ── 전역 캐시 ─────────────────────────────────────────────────
_cache: dict = {}


# ══════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════

async def run() -> dict:
    """
    06:00 스케줄에서 호출 — 모든 수집기 병렬 실행 후 캐시 저장 및 텔레그램 발송.

    Returns:
        cache dict (get_cache()와 동일한 구조)
    """
    global _cache

    today = get_today()
    prev  = get_prev_trading_day(today)
    today_str = fmt_ymd(today)
    prev_str  = fmt_ymd(prev) if prev else None

    logger.info(f"[data_collector] 병렬 수집 시작 — 기준일: {prev_str or 'N/A'}")
    start_ts = datetime.now(KST)

    # ── 병렬 수집 실행 ─────────────────────────────────────────
    (
        dart_data,
        market_data,
        naver_data,
        newsapi_data,
        global_rss_data,
        price_data,
        sector_etf_data,
        short_data,
        event_calendar_data,
        closing_strength_result,
        volume_surge_result,
        fund_concentration_result,
    ) = await asyncio.gather(
        _safe_collect("filings",            _collect_filings,           prev),
        _safe_collect("market_global",      _collect_market_global,     prev),
        _safe_collect("news_naver",         _collect_news_naver,        today),
        _safe_collect("news_newsapi",       _collect_news_newsapi,      today),
        _safe_collect("news_global_rss",    _collect_global_rss),
        _safe_collect("price_domestic",     _collect_price_domestic,    prev),
        _safe_collect("sector_etf",         _collect_sector_etf,        prev),
        _safe_collect("short_interest",     _collect_short_interest,    prev),
        _safe_collect("event_calendar",     _collect_event_calendar,    today),
        _safe_collect("closing_strength",   _collect_closing_strength,  prev_str),
        _safe_collect("volume_surge",       _collect_volume_surge,      prev_str),
        _safe_collect("fund_concentration", _collect_fund_concentration,prev_str),
    )

    elapsed = (datetime.now(KST) - start_ts).total_seconds()
    logger.info(f"[data_collector] 병렬 수집 완료 — {elapsed:.1f}초")

    # ── 기본값 보정 ────────────────────────────────────────────
    dart_data                 = dart_data                 or []
    market_data               = market_data               or {}
    naver_data                = naver_data                or {}
    newsapi_data              = newsapi_data              or {}
    global_rss_data           = global_rss_data           or []
    price_data                = price_data                or None
    sector_etf_data           = sector_etf_data           or []
    short_data                = short_data                or []
    event_calendar_data       = event_calendar_data       or []
    closing_strength_result   = closing_strength_result   or []
    volume_surge_result       = volume_surge_result       or []
    fund_concentration_result = fund_concentration_result or []

    # ── 성공 플래그 기록 ───────────────────────────────────────
    success_flags = {
        "filings":            bool(dart_data),
        "market_global":      bool(market_data),
        "news_naver":         bool(naver_data),
        "news_newsapi":       bool(newsapi_data),
        "news_global_rss":    bool(global_rss_data),
        "price_domestic":     price_data is not None,
        "sector_etf":         bool(sector_etf_data),
        "short_interest":     bool(short_data),
        "event_calendar":     bool(event_calendar_data),
        "closing_strength":   bool(closing_strength_result),
        "volume_surge":       bool(volume_surge_result),
        "fund_concentration": bool(fund_concentration_result),
    }
    ok_count   = sum(success_flags.values())
    fail_count = len(success_flags) - ok_count
    logger.info(f"[data_collector] 수집 결과 — 성공:{ok_count} 실패:{fail_count}")
    for name, ok in success_flags.items():
        if not ok:
            logger.warning(f"[data_collector]   ❌ {name} 수집 실패 (비치명적)")

    # ── 캐시 저장 ──────────────────────────────────────────────
    _cache = {
        "collected_at":              datetime.now(KST).isoformat(),
        "dart_data":                 dart_data,
        "market_data":               market_data,
        "news_naver":                naver_data,
        "news_newsapi":              newsapi_data,
        "news_global_rss":           global_rss_data,
        "price_data":                price_data,
        "sector_etf_data":           sector_etf_data,
        "short_data":                short_data,
        "event_calendar":            event_calendar_data,
        "closing_strength_result":   closing_strength_result,
        "volume_surge_result":       volume_surge_result,
        "fund_concentration_result": fund_concentration_result,
        "success_flags":             success_flags,
    }

    logger.info("[data_collector] 캐시 저장 완료 ✅")

    # ── 원시 데이터 텔레그램 발송 ──────────────────────────────
    try:
        _send_raw_data_to_telegram(_cache)
    except Exception as e:
        logger.warning(f"[data_collector] 원시 데이터 텔레그램 발송 실패 (비치명적): {e}")

    return _cache


def get_cache() -> dict:
    """저장된 캐시 반환. run() 미호출 시 빈 dict."""
    return _cache


def is_fresh(max_age_minutes: int = 180) -> bool:
    """
    캐시가 max_age_minutes 이내에 수집된 경우 True.
    기본 3시간 (06:00 수집 → 아침봇 08:30 사용: 약 150분 차이).
    """
    if not _cache.get("collected_at"):
        return False
    try:
        collected = datetime.fromisoformat(_cache["collected_at"])
        age_min   = (datetime.now(KST) - collected).total_seconds() / 60
        return age_min <= max_age_minutes
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════
# 개별 수집기 래퍼 (동기 → asyncio executor)
# ══════════════════════════════════════════════════════════════

async def _safe_collect(name: str, fn, *args):
    """
    단일 수집기 실행 — 실패 시 None 반환 (비치명적).
    모든 동기 수집기를 executor에서 실행해 asyncio.gather()와 호환.
    """
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, fn, *args)
        return result
    except Exception as e:
        logger.warning(f"[data_collector] {name} 수집 실패 (비치명적): {e}")
        return None


def _collect_filings(target_date):
    if target_date is None:
        return []
    from collectors.filings import collect
    return collect(target_date)


def _collect_market_global(target_date):
    if target_date is None:
        return {}
    from collectors.market_global import collect
    return collect(target_date)


def _collect_news_naver(target_date):
    from collectors.news_naver import collect
    return collect(target_date)


def _collect_news_newsapi(target_date):
    from collectors.news_newsapi import collect
    return collect(target_date)


def _collect_global_rss():
    if not config.GEOPOLITICS_ENABLED:
        return []
    from collectors.news_global_rss import collect
    return collect()


def _collect_price_domestic(target_date):
    if target_date is None:
        return None
    from collectors.price_domestic import collect_daily
    return collect_daily(target_date)


def _collect_sector_etf(target_date):
    if not config.SECTOR_ETF_ENABLED:
        return []
    if target_date is None:
        return []
    from collectors.sector_etf import collect
    return collect(target_date)


def _collect_short_interest(target_date):
    if not config.SHORT_INTEREST_ENABLED:
        return []
    if target_date is None:
        return []
    from collectors.short_interest import collect
    return collect(target_date)


def _collect_event_calendar(target_date):
    if not config.EVENT_CALENDAR_ENABLED:
        return []
    from collectors.event_calendar import collect
    return collect(target_date)


def _collect_closing_strength(date_str: str | None):
    """마감강도 — closing_strength.analyze()"""
    if not date_str:
        return []
    from collectors.closing_strength import analyze
    return analyze(date_str)


def _collect_volume_surge(date_str: str | None):
    """거래량급증 — volume_surge.analyze()"""
    if not date_str:
        return []
    from collectors.volume_surge import analyze
    return analyze(date_str)


def _collect_fund_concentration(date_str: str | None):
    """자금집중 — fund_concentration.analyze()"""
    if not date_str:
        return []
    from collectors.fund_concentration import analyze
    return analyze(date_str)


# ══════════════════════════════════════════════════════════════
# 원시 데이터 텔레그램 발송 (§8)
# ══════════════════════════════════════════════════════════════

def _send_raw_data_to_telegram(cache: dict) -> None:
    """
    수집 완료 직후 필터링된 원시 데이터 요약을 텔레그램으로 발송.

    목적:
      - Gemini 장애 시 사용자가 Claude 채팅에 붙여넣어 수동 분석 가능
      - 사용자 교차검증용 (봇이 어떤 데이터를 받았는지 확인)

    발송 형식 (§8 준수):
      📊 [06:00 수집 완료] 원시 데이터 요약
      🇺🇸 미국 섹터 (±2%+ 필터)
      📋 DART 공시 (오늘)
      📈 전날 상한가/15%+
      💰 자금집중 상위 5
      ⚠️ Gemini 장애 시 이 메시지를 Claude에게 전달하세요.
    """
    from telegram.sender import send as send_message  # [v13.0 버그수정] send_message → send (sender.py에는 send()만 존재)

    lines: list[str] = []
    lines.append("📊 [06:00 수집 완료] 원시 데이터 요약\n")

    # ── 미국 섹터 ETF (±2%+ 필터 적용된 결과만) ──────────────
    market_data   = cache.get("market_data", {})
    us_market     = market_data.get("us_market", {})
    us_sectors    = us_market.get("sectors", {})
    commodities   = market_data.get("commodities", {})

    sector_lines = []
    for sector_name, sector_info in us_sectors.items():
        change = sector_info.get("change", "N/A")
        if change != "N/A":
            sector_lines.append(f"  - {sector_name}: {change}")

    lines.append("🇺🇸 미국 섹터 (±2%+ 필터)")
    if sector_lines:
        lines.extend(sector_lines)
    else:
        lines.append("  - 해당 없음 (±2% 초과 섹터 없음)")

    # 원자재
    commodity_lines = []
    for com_key, com_info in commodities.items():
        if not isinstance(com_info, dict):
            continue
        change = com_info.get("change", "N/A")
        if change and change != "N/A":
            commodity_lines.append(f"  - {com_key}: {change}")
    if commodity_lines:
        lines.append("\n🛢 원자재")
        lines.extend(commodity_lines)

    # 환율
    forex = market_data.get("forex", {})
    usd_krw = forex.get("USD/KRW", forex.get("usd_krw", "N/A"))
    if usd_krw != "N/A":
        lines.append(f"\n💱 환율: USD/KRW {usd_krw}")

    # ── DART 공시 ─────────────────────────────────────────────
    dart_data = cache.get("dart_data", [])
    lines.append(f"\n📋 DART 공시 ({len(dart_data)}건)")
    if dart_data:
        for d in dart_data[:10]:                           # 최대 10건
            name      = d.get("종목명",  "")
            kind      = d.get("공시종류", "")
            size      = d.get("규모",    "")
            summary   = d.get("본문요약", "")
            cap       = d.get("시가총액", 0)
            cap_str   = f" 시총{cap // 100_000_000}억" if cap else ""
            detail    = summary or size
            lines.append(f"  - {name}: {kind} {detail}{cap_str}".rstrip())
    else:
        lines.append("  - 해당 없음")

    # ── 전날 상한가/15%+ 급등 ─────────────────────────────────
    price_data  = cache.get("price_data") or {}
    upper_limit = price_data.get("upper_limit", [])
    top_gainers = price_data.get("top_gainers", [])

    lines.append(f"\n📈 전날 상한가/15%+")
    all_movers = sorted(
        upper_limit + top_gainers,
        key=lambda x: x.get("등락률", 0),
        reverse=True,
    )
    if all_movers:
        for s in all_movers[:10]:
            name    = s.get("종목명", "")
            rate    = s.get("등락률", 0)
            cap     = s.get("시가총액", 0)
            cap_str = f" 시총{cap // 100_000_000}억" if cap else ""
            lines.append(f"  - {name}: {rate:+.1f}%{cap_str}")
    else:
        lines.append("  - 해당 없음")

    # ── 자금집중 상위 5 ───────────────────────────────────────
    fund_top = cache.get("fund_concentration_result", [])
    lines.append(f"\n💰 자금집중 상위 5 (거래대금/시총 비율)")
    if fund_top:
        for f in fund_top[:5]:
            name  = f.get("종목명", f.get("name", ""))
            ratio = f.get("ratio", f.get("거래대금시총비율", 0))
            lines.append(f"  - {name}: {ratio:.1f}%" if ratio else f"  - {name}")
    else:
        lines.append("  - 해당 없음")

    # ── 공매도 상위 5 ─────────────────────────────────────────
    short_top = cache.get("short_data", [])
    lines.append(f"\n🩳 공매도 상위 5")
    if short_top:
        for s in short_top[:5]:
            name   = s.get("종목명", s.get("name", ""))
            ratio  = s.get("short_ratio", s.get("공매도비율", 0))
            lines.append(f"  - {name}: {ratio:.1f}%" if ratio else f"  - {name}")
    else:
        lines.append("  - 해당 없음")

    # ── 거래량 급증 상위 5 ────────────────────────────────────
    volume_top = cache.get("volume_surge_result", [])
    lines.append(f"\n📊 거래량 급증 상위 5 (전일 대비 500%+)")
    if volume_top:
        for v in volume_top[:5]:
            name  = v.get("종목명", v.get("name", ""))
            surge = v.get("volume_ratio", v.get("거래량배율", 0))
            lines.append(f"  - {name}: {surge:.0f}x" if surge else f"  - {name}")
    else:
        lines.append("  - 해당 없음")

    # ── 성공 플래그 요약 ──────────────────────────────────────
    flags = cache.get("success_flags", {})
    failed = [k for k, v in flags.items() if not v]
    if failed:
        lines.append(f"\n⚠️ 수집 실패: {', '.join(failed)}")

    lines.append("\n─────────────────────────────")
    lines.append("⚠️ Gemini 장애 시 이 메시지를 Claude에게 전달하세요.")

    message = "\n".join(lines)

    try:
        send_message(message)
        logger.info("[data_collector] 원시 데이터 텔레그램 발송 완료 ✅")
    except Exception as e:
        logger.warning(f"[data_collector] 텔레그램 send_message 실패: {e}")
        raise
