"""
collectors/data_collector.py
데이터 수집 총괄 + 가중치 점수화 + 신호1~8 생성 (v12.0 Step 7~8)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[역할]
  06:00 단일 스케줄로 모든 수집기를 asyncio.gather() 병렬 실행.
  결과를 전역 캐시에 저장 → morning_report.run()이 캐시를 읽어 사용.

  기존 main.py에 흩어져 있던 수집 로직을 이 파일로 완전 이전:
    ‣ run_geopolitics_collect()    → run() 내 _collect_global_rss()
    ‣ run_event_calendar_collect() → run() 내 _collect_event_calendar()
    ‣ closing_strength / volume_surge / fund_concentration → 병렬 수집

[signal_analyzer 흡수 — v12.0 Step 8]
  signal_analyzer.py 삭제 → _build_signals() 내부 함수로 완전 흡수.
  신호1(DART), 신호2(미국증시/원자재/철강ETF), 신호3(리포트),
  신호4(순환매), 신호5(정책), 신호6(지정학), 신호7(섹터수급),
  신호8(기업이벤트), DataLab 트렌드 — 모두 수집 직후 여기서 생성.
  결과는 캐시의 "signals" 키에 저장.

[병렬 수집 대상 — asyncio.gather()]
  ① filings            — DART 공시 (전날)
  ② market_global      — 미국증시 + 원자재 (전날)
  ③ news_naver         — 네이버 뉴스 (당일)
  ④ news_newsapi       — NewsAPI 글로벌 뉴스 (당일)
  ⑤ news_global_rss    — 글로벌 RSS 뉴스 (지정학)
  ⑥ price_domestic     — 전날 가격/기관/외인 데이터
  ⑦ sector_etf         — 섹터 ETF 자금흐름 (전날)
  ⑧ short_interest     — 공매도 잔고 (전날)
  ⑨ event_calendar     — 기업 이벤트 캘린더 (당일)
  ⑩ closing_strength   — 마감강도 (전날) [마감강도]
  ⑪ volume_surge       — 거래량급증 (전날) [거래량급증]
  ⑫ fund_concentration — 자금집중 (전날) [자금집중]

[가중치 점수화]
  수집 완료 직후 _compute_score_summary() 로 신호 유형별 강도 점수 계산.
  morning_analyzer.analyze() 호출 전 압축된 score_summary 제공.

[신호 목록 생성]
  수집 완료 직후 _build_signals() 로 신호1~8 상세 목록 생성.
  morning_analyzer 는 이 signals 를 그대로 받아 Gemini 분석에 활용.

[캐시 구조 — get_cache()]
  {
    "collected_at":         str,               # KST ISO 수집 시각
    "dart_data":            list[dict],
    "market_data":          dict,
    "news_naver":           dict,
    "news_newsapi":         dict,
    "news_global_rss":      list[dict],        # 지정학 raw 뉴스
    "price_data":           dict | None,
    "sector_etf_data":      list[dict],
    "short_data":           list[dict],
    "event_calendar":       list[dict],
    "closing_strength_result":   list[dict],  # 마감강도
    "volume_surge_result":       list[dict],  # 거래량급증
    "fund_concentration_result": list[dict],  # 자금집중
    "score_summary":        dict,             # 신호 유형별 강도 점수
    "signals":              list[dict],       # 신호1~8 상세 목록 (signal_analyzer 흡수)
    "market_summary":       dict,             # 미국증시 요약
    "commodities":          dict,             # 원자재 데이터
    "volatility":           str,              # 변동성 판단
    "report_picks":         list[dict],       # 증권사 리포트 종목
    "policy_summary":       list[dict],       # 정책 뉴스
    "sector_scores":        dict,             # 섹터 방향성 점수
    "event_scores":         dict,             # 이벤트 점수 {ticker: strength}
    "success_flags":        dict[str, bool],  # 수집기별 성공 여부
  }

[절대 금지 — ARCHITECTURE 준수]
  이 파일에서 AI API 호출 금지
  이 파일에서 텔레그램 발송 금지
  이 파일에서 DB 기록 금지
  수집·캐싱·점수화·신호생성만 담당

[수정이력]
  v12.0 Step 7: 신규 생성
  v12.0 Step 8: signal_analyzer 흡수 — _build_signals() 추가, 캐시에 signals 키 추가
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
    06:00 스케줄에서 호출 — 모든 수집기 병렬 실행 후 캐시 저장.

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
    dart_data              = dart_data              or []
    market_data            = market_data            or {}
    naver_data             = naver_data             or {}
    newsapi_data           = newsapi_data           or {}
    global_rss_data        = global_rss_data        or []
    price_data             = price_data             or None
    sector_etf_data        = sector_etf_data        or []
    short_data             = short_data             or []
    event_calendar_data    = event_calendar_data    or []
    closing_strength_result = closing_strength_result or []
    volume_surge_result    = volume_surge_result    or []
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

    # ── 가중치 점수화 ──────────────────────────────────────────
    score_summary = _compute_score_summary(
        dart_data       = dart_data,
        market_data     = market_data,
        naver_data      = naver_data,
        newsapi_data    = newsapi_data,
        global_rss_data = global_rss_data,
        price_data      = price_data,
        sector_etf_data = sector_etf_data,
        short_data      = short_data,
        event_calendar  = event_calendar_data,
        closing_strength= closing_strength_result,
        volume_surge    = volume_surge_result,
        fund_concentration = fund_concentration_result,
    )

    logger.info(
        f"[data_collector] 점수화 완료 — 총점: {score_summary.get('total_score', 0)} | "
        + " | ".join(
            f"{k}:{v}" for k, v in score_summary.items()
            if k != "total_score" and v > 0
        )
    )

    # ── 신호 1~8 생성 (signal_analyzer 흡수 — Step 8) ──────────
    news_data_merged = {**naver_data, **newsapi_data,
                        "reports": naver_data.get("reports", []) + newsapi_data.get("reports", []),
                        "policy_news": naver_data.get("policy_news", []) + newsapi_data.get("policy_news", [])}
    signal_bundle = _build_signals(
        dart_data        = dart_data,
        market_data      = market_data,
        news_data        = news_data_merged,
        price_data       = price_data,
        global_rss_data  = global_rss_data,
        sector_etf_data  = sector_etf_data,
        short_data       = short_data,
        event_calendar   = event_calendar_data,
    )
    logger.info(f"[data_collector] 신호 생성 완료 — {len(signal_bundle['signals'])}개")

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
        "score_summary":             score_summary,
        # [Step 8] signal_analyzer 흡수 — 신호1~8 + 파생 필드
        "signals":              signal_bundle["signals"],
        "market_summary":       signal_bundle["market_summary"],
        "commodities":          signal_bundle["commodities"],
        "volatility":           signal_bundle["volatility"],
        "report_picks":         signal_bundle["report_picks"],
        "policy_summary":       signal_bundle["policy_summary"],
        "sector_scores":        signal_bundle["sector_scores"],
        "event_scores":         signal_bundle["event_scores"],
        "success_flags":             success_flags,
    }

    logger.info("[data_collector] 캐시 저장 완료 ✅")
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
    """마감강도 [마감강도] — closing_strength.analyze()"""
    if not date_str:
        return []
    from collectors.closing_strength import analyze
    return analyze(date_str)


def _collect_volume_surge(date_str: str | None):
    """거래량급증 [거래량급증] — volume_surge.analyze()"""
    if not date_str:
        return []
    from collectors.volume_surge import analyze
    return analyze(date_str)


def _collect_fund_concentration(date_str: str | None):
    """자금집중 [자금집중] — fund_concentration.analyze()"""
    if not date_str:
        return []
    from collectors.fund_concentration import analyze
    return analyze(date_str)


# ══════════════════════════════════════════════════════════════
# 가중치 점수화
# ══════════════════════════════════════════════════════════════

def _compute_score_summary(
    dart_data:          list,
    market_data:        dict,
    naver_data:         dict,
    newsapi_data:       dict,
    global_rss_data:    list,
    price_data:         dict | None,
    sector_etf_data:    list,
    short_data:         list,
    event_calendar:     list,
    closing_strength:   list,
    volume_surge:       list,
    fund_concentration: list,
) -> dict:
    """
    수집 결과를 기반으로 신호 유형별 강도 점수 계산.

    점수 산정 기준:
      신호1(공시)      : 호재 공시 건수 × 가중치
      신호2(미국증시)  : 섹터 ETF 급등/급락 건수
      신호3(리포트)    : 리포트 건수
      신호4(순환매)    : 상한가 + 급등 종목 수
      신호5(정책)      : 정책 뉴스 건수
      신호6(지정학)    : 글로벌 RSS 뉴스 건수
      신호7(섹터수급)  : 섹터 ETF + 공매도 데이터 건수
      신호8(기업이벤트): 기업 이벤트 건수
      마감강도     : 마감강도 상위 종목 수
      거래량급증   : 거래량급증 상위 종목 수
      자금집중     : 자금집중 상위 종목 수

    Returns:
        {
          "신호1_공시": int,
          "신호2_미국": int,
          "신호3_리포트": int,
          "신호4_순환매": int,
          "신호5_정책": int,
          "신호6_지정학": int,
          "신호7_섹터수급": int,
          "신호8_기업이벤트": int,
          "마감강도": int,
          "거래량급증": int,
          "자금집중": int,
          "total_score": int,
        }
    """
    scores: dict[str, int] = {}

    # 신호1: DART 공시 — 호재 키워드 공시만 카운트
    BULLISH_TYPES = {"수주", "배당결정", "자사주취득결정", "MOU", "단일판매공급계약체결", "특허", "판결"}
    s1 = sum(
        1 for d in dart_data
        if any(kw in d.get("공시종류", "") for kw in BULLISH_TYPES)
    )
    scores["신호1_공시"] = min(s1 * 3, 15)   # 건당 3점, 최대 15점

    # 신호2: 미국증시 섹터 ETF 급등 카운트
    us_sectors = market_data.get("us_market", {}).get("sectors", {})
    s2 = sum(
        1 for v in us_sectors.values()
        if _parse_pct(v.get("change", "0")) >= 2.0
    )
    scores["신호2_미국"] = min(s2 * 5, 20)   # 섹터당 5점, 최대 20점

    # 신호3: 증권사 리포트
    reports = naver_data.get("reports", []) + newsapi_data.get("reports", [])
    s3 = sum(1 for r in reports if r.get("액션") in ("목표가상향", "신규매수"))
    scores["신호3_리포트"] = min(s3 * 4, 20)

    # 신호4: 상한가 + 급등 종목
    upper_cnt  = len(price_data.get("upper_limit", [])) if price_data else 0
    gainer_cnt = len(price_data.get("top_gainers",  [])) if price_data else 0
    scores["신호4_순환매"] = min(upper_cnt * 5 + gainer_cnt, 30)

    # 신호5: 정책 뉴스
    policy = naver_data.get("policy_news", []) + newsapi_data.get("policy_news", [])
    scores["신호5_정책"] = min(len(policy) * 2, 10)

    # 신호6: 지정학 뉴스 건수
    scores["신호6_지정학"] = min(len(global_rss_data) * 2, 20)

    # 신호7: 섹터 ETF + 공매도
    scores["신호7_섹터수급"] = min(
        len(sector_etf_data) * 2 + len(short_data), 20
    )

    # 신호8: 기업 이벤트
    scores["신호8_기업이벤트"] = min(len(event_calendar) * 3, 15)

    # 마감강도
    scores["마감강도"] = min(len(closing_strength) * 2, 20)

    # 거래량급증
    scores["거래량급증"] = min(len(volume_surge) * 2, 20)

    # 자금집중
    scores["자금집중"] = min(len(fund_concentration) * 2, 20)

    scores["total_score"] = sum(v for k, v in scores.items() if k != "total_score")

    return scores


def _parse_pct(change_str: str) -> float:
    """'+3.5%' 형식 문자열 → float. 파싱 실패 시 0.0."""
    try:
        return float(str(change_str).replace("%", "").replace("+", "").strip())
    except (ValueError, TypeError):
        return 0.0


# ══════════════════════════════════════════════════════════════
# 신호 1~8 생성 — signal_analyzer 완전 흡수 (Step 8)
# ══════════════════════════════════════════════════════════════

def _build_signals(
    dart_data:        list[dict],
    market_data:      dict,
    news_data:        dict,
    price_data:       dict | None    = None,
    global_rss_data:  list[dict]     = None,
    sector_etf_data:  list[dict]     = None,
    short_data:       list[dict]     = None,
    event_calendar:   list[dict]     = None,
) -> dict:
    """
    신호 1~8 상세 목록 생성 (signal_analyzer.analyze() 완전 흡수).

    AI 호출 없음 — 순수 규칙 기반 계산.
    morning_analyzer 는 이 결과를 받아 Gemini 분석에 활용.

    Returns:
        {
          "signals":        list[dict],  # 신호1~8 목록 (강도 내림차순)
          "market_summary": dict,
          "commodities":    dict,
          "volatility":     str,
          "report_picks":   list[dict],
          "policy_summary": list[dict],
          "sector_scores":  dict,
          "event_scores":   dict,
        }
    """
    logger.info("[data_collector] 신호 1~8 생성 시작")

    signals        = []
    market_summary = market_data.get("us_market", {})
    commodities    = market_data.get("commodities", {})
    by_sector      = price_data.get("by_sector", {}) if price_data else {}
    global_rss_data  = global_rss_data  or []
    sector_etf_data  = sector_etf_data  or []
    short_data       = short_data       or []
    event_calendar   = event_calendar   or []

    # ── 신호 1: DART 공시 ────────────────────────────────────
    for dart in dart_data:
        strength = _sig_dart_strength(dart)
        if strength == 0:
            continue
        signals.append({
            "테마명":   _sig_dart_to_theme(dart["공시종류"], dart["종목명"]),
            "발화신호": f"신호1: {dart['공시종류']} [{dart['종목명']}|{dart['공시시각']}]",
            "강도":     strength,
            "신뢰도":   dart["신뢰도"],
            "발화단계": "1일차",
            "상태":     "신규",
            "관련종목": [dart["종목명"]],
        })

    # ── 신호 2: 미국증시 + 원자재 + 섹터 연동 ────────────────
    signals.extend(_sig_us_market(market_summary, commodities, by_sector))
    signals.extend(_sig_steel_etf(market_summary.get("sectors", {}), by_sector))

    # ── 신호 3: 증권사 리포트 ────────────────────────────────
    for report in news_data.get("reports", [])[:5]:
        if report.get("액션") in ("목표가상향", "신규매수"):
            signals.append({
                "테마명":   f"{report['종목명']} ({report['증권사']})",
                "발화신호": f"신호3: {report['액션']} [{report['증권사']}|오늘]",
                "강도":     4,
                "신뢰도":   report.get("신뢰도", "뉴스"),
                "발화단계": "1일차",
                "상태":     "신규",
                "관련종목": [report["종목명"]],
            })

    # ── 신호 4: 전날 급등/상한가 순환매 ─────────────────────
    if price_data:
        price_sigs = _sig_prev_price(price_data)
        signals.extend(price_sigs)
        logger.info(f"[data_collector] 신호4 (순환매): {len(price_sigs)}개 테마 감지")

    # ── 신호 5: 정책·시황 ────────────────────────────────────
    for p in news_data.get("policy_news", [])[:3]:
        signals.append({
            "테마명":   "정책/시황",
            "발화신호": f"신호5: {p['제목'][:40]}",
            "강도":     2,
            "신뢰도":   "스니펫",
            "발화단계": "불명",
            "상태":     "모니터",
            "관련종목": [],
        })

    # ── 신호 6: 지정학 이벤트 ────────────────────────────────
    if global_rss_data:
        geo_sigs = _sig_geopolitics_from_rss(global_rss_data, by_sector)
        signals.extend(geo_sigs)
        logger.info(f"[data_collector] 신호6 (지정학): {len(geo_sigs)}개 이벤트 발화")

    # ── 신호 7: 섹터 자금흐름 + 공매도 잔고 ───────────────────
    sector_scores: dict = {}
    if sector_etf_data or short_data:
        sector_flow = _sig_sector_flow(sector_etf_data, short_data, by_sector)
        signals.extend(sector_flow["signals"])
        sector_scores = sector_flow["sector_scores"]
        logger.info(
            f"[data_collector] 신호7 (섹터수급): {len(sector_flow['signals'])}개 신호 추가"
        )

    # ── 신호 8: 기업 이벤트 캘린더 ────────────────────────────
    event_scores: dict = {}
    if event_calendar:
        ev_sigs = _sig_event_impact(event_calendar)
        signals.extend(ev_sigs)
        for ev in event_calendar:
            ticker = ev.get("ticker", "")
            if ticker:
                event_scores[ticker] = max(
                    event_scores.get(ticker, 0), ev.get("strength", 3)
                )
        logger.info(f"[data_collector] 신호8 (기업이벤트): {len(ev_sigs)}개 신호 추가")

    # ── DataLab 트렌드 신호 ────────────────────────────────────
    datalab = news_data.get("datalab_trends", [])
    if datalab:
        dl_sigs = _sig_datalab_trends(datalab, by_sector)
        signals.extend(dl_sigs)
        logger.info(f"[data_collector] DataLab 트렌드: {len(dl_sigs)}개 신호 추가")

    signals.sort(key=lambda x: x["강도"], reverse=True)
    logger.info(f"[data_collector] 신호 총 {len(signals)}개 생성 완료")

    volatility = _sig_judge_volatility(market_summary, price_data)
    reports    = news_data.get("reports", [])[:5]
    policy     = news_data.get("policy_news", [])[:3]

    return {
        "signals":        signals,
        "market_summary": market_summary,
        "commodities":    commodities,
        "volatility":     volatility,
        "report_picks":   reports,
        "policy_summary": policy,
        "sector_scores":  sector_scores,
        "event_scores":   event_scores,
    }


# ── 신호 2: 미국증시 + 원자재 ────────────────────────────────

def _sig_us_market(us: dict, commodities: dict, by_sector: dict) -> list[dict]:
    signals = []
    for sector_name, sector_data in us.get("sectors", {}).items():
        change_str = sector_data.get("change", "N/A")
        if change_str == "N/A":
            continue
        try:
            pct = float(change_str.replace("%", "").replace("+", ""))
        except ValueError:
            continue
        if abs(pct) < config.US_SECTOR_SIGNAL_MIN:
            continue
        direction = "강세↑" if pct > 0 else "약세↓"
        강도 = 4 if abs(pct) >= 3.0 else 3 if abs(pct) >= 2.0 else 2
        keywords = config.US_SECTOR_KR_INDUSTRY.get(sector_name, [])
        signals.append({
            "테마명":   sector_name,
            "발화신호": f"신호2: 미국 {sector_name} {direction} {change_str} [섹터ETF|전날]",
            "강도":     강도 if pct > 0 else 1,
            "신뢰도":   sector_data.get("신뢰도", "yfinance"),
            "발화단계": "불명",
            "상태":     "모니터" if pct > 0 else "경고",
            "관련종목": _sig_sector_top(by_sector, keywords),
        })
    copper = commodities.get("copper", {})
    if _sig_is_positive(copper.get("change", "N/A")):
        signals.append({
            "테마명":   "전선/구리",
            "발화신호": f"신호2: 구리 {copper['change']} [LME|전날]",
            "강도":     3, "신뢰도": copper.get("신뢰도", "N/A"),
            "발화단계": "불명", "상태": "모니터",
            "관련종목": _sig_sector_top(by_sector, config.COMMODITY_KR_INDUSTRY.get("copper", [])),
        })
    silver = commodities.get("silver", {})
    if _sig_is_positive(silver.get("change", "N/A")):
        signals.append({
            "테마명":   "귀금속/태양광",
            "발화신호": f"신호2: 은 {silver['change']} [COMEX|전날]",
            "강도":     2, "신뢰도": silver.get("신뢰도", "N/A"),
            "발화단계": "불명", "상태": "모니터",
            "관련종목": _sig_sector_top(by_sector, config.COMMODITY_KR_INDUSTRY.get("silver", [])),
        })
    return signals


def _sig_steel_etf(sectors: dict, by_sector: dict) -> list[dict]:
    for label in ("철강/비철금속", "철강"):
        data = sectors.get(label, {})
        try:
            pct = float(data.get("change", "0").replace("%", "").replace("+", ""))
        except ValueError:
            continue
        if pct < config.STEEL_ETF_ALERT_THRESHOLD:
            continue
        강도 = 5 if pct >= 5.0 else 4
        logger.info(f"[data_collector] 신호2(철강ETF): {label} {pct:+.1f}% — 강도{강도}")
        return [{
            "테마명":   "철강/비철금속",
            "발화신호": f"신호2: 미국 {label} ETF {data.get('change','')} 급등 — 철강 테마 선행 신호",
            "강도":     강도, "신뢰도": data.get("신뢰도", "yfinance"),
            "발화단계": "1일차", "상태": "신규",
            "관련종목": _sig_sector_top(by_sector, config.COMMODITY_KR_INDUSTRY.get("steel", [])),
        }]
    return []


# ── 신호 4: 전날 순환매 ──────────────────────────────────────

def _sig_prev_price(price_data: dict) -> list[dict]:
    signals     = []
    kospi_rate  = price_data.get("kospi",  {}).get("change_rate", 0)
    kosdaq_rate = price_data.get("kosdaq", {}).get("change_rate", 0)
    upper_limit = price_data.get("upper_limit", [])
    top_gainers = price_data.get("top_gainers", [])
    has_movers  = bool(upper_limit) or bool(top_gainers)
    is_low_vol  = abs(kospi_rate) < 1.0 and abs(kosdaq_rate) < 1.0
    if is_low_vol and not has_movers:
        logger.info("[data_collector] 저변동 + 급등종목 없음 → 신호4 스킵")
        return []
    if upper_limit:
        su = sorted(upper_limit, key=lambda x: x["등락률"], reverse=True)
        signals.append({
            "테마명":   "상한가 순환매",
            "발화신호": (f"신호4: 전날 상한가 {len(upper_limit)}종목 "
                        f"[대장:{su[0]['종목명']} {su[0]['등락률']:+.1f}%|pykrx]"),
            "강도": 5, "신뢰도": "pykrx",
            "발화단계": "2일차", "상태": "진행",
            "관련종목": [s["종목명"] for s in su[:10]],
            "ai_memo": f"전날 상한가 {len(upper_limit)}종목 — 오늘 2·3등주 순환매 주목",
        })
    upper_names  = {s["종목명"] for s in upper_limit}
    gainers_only = [s for s in top_gainers if s["종목명"] not in upper_names]
    for market in ("KOSPI", "KOSDAQ"):
        mg = [s for s in gainers_only if s.get("시장") == market][:10]
        if not mg:
            continue
        top = mg[0]
        강도 = 5 if top["등락률"] >= 20 else 4 if top["등락률"] >= 10 else 3
        signals.append({
            "테마명":   f"{market} 급등 순환매",
            "발화신호": (f"신호4: 전날 {market} 급등 {len(mg)}종목 "
                        f"[대장:{top['종목명']} {top['등락률']:+.1f}%|pykrx]"),
            "강도": 강도, "신뢰도": "pykrx",
            "발화단계": "2일차", "상태": "진행",
            "관련종목": [s["종목명"] for s in mg],
            "ai_memo": f"전날 대장 {top['종목명']} {top['등락률']:+.1f}% — 소외주 순환매 가능",
        })
    return signals


# ── 신호 6: 지정학 (RSS 기반 키워드 매칭) ───────────────────

def _sig_geopolitics_from_rss(global_rss_data: list[dict], by_sector: dict) -> list[dict]:
    """
    news_global_rss 수집 결과에서 지정학 이벤트 감지 → 신호6 생성.
    Gemini 분석 없이 키워드 기반으로만 처리 (AI 호출 금지).
    세밀한 Gemini 분석은 morning_analyzer._analyze_geopolitics()에서 수행.
    """
    from utils.geopolitics_map import lookup as map_lookup

    event_agg: dict[str, dict] = {}
    for article in global_rss_data:
        text    = article.get("raw_text", "") + " " + article.get("title", "")
        matches = map_lookup(text)
        for match in matches:
            key = match["key"]
            if key not in event_agg:
                event_agg[key] = {"match": match, "hit_count": 0, "rep": article}
            event_agg[key]["hit_count"] += 1

    signals = []
    min_conf = config.GEOPOLITICS_CONFIDENCE_MIN
    for key, agg in event_agg.items():
        match     = agg["match"]
        conf      = min(match.get("confidence_base", 0.6) + (agg["hit_count"] - 1) * 0.05, 0.95)
        if conf < min_conf:
            continue
        direction = match.get("impact", "+")
        for sector in match.get("sectors", [])[:3]:
            keywords = config.US_SECTOR_KR_INDUSTRY.get(sector, [sector])
            강도 = 5 if conf >= 0.85 else 4 if conf >= 0.70 else 3
            signals.append({
                "테마명":   sector,
                "발화신호": (f"신호6: {key} — {agg['rep'].get('title','')[:50]}"
                            f" [신뢰도:{conf:.0%}|지정학]"),
                "강도":     강도,
                "신뢰도":   f"geo:{conf:.2f}",
                "발화단계": "1일차",
                "상태":     "신규" if direction == "+" else "경고",
                "관련종목": _sig_sector_top(by_sector, keywords),
            })
    return signals


# ── 신호 7: 섹터 자금흐름 ────────────────────────────────────

_ZSCORE_THRESHOLD  = 2.0
_VALUE_SURGE_RATIO = 3.0
_SHORT_CLUSTER_MIN = 3

def _sig_sector_flow(
    etf_data: list[dict], short_data: list[dict], by_sector: dict
) -> dict:
    import statistics as _stats
    # ETF 이상 감지
    sector_etfs = [e for e in etf_data if e.get("sector") != "시장전체"]
    ratios = [e.get("volume_ratio", 1.0) for e in sector_etfs]
    etf_anomalies = []
    if len(ratios) >= 3:
        mean  = _stats.mean(ratios)
        stdev = _stats.stdev(ratios) or 0.001
        for e in sector_etfs:
            r  = e.get("volume_ratio", 1.0)
            zs = (r - mean) / stdev
            if zs >= _ZSCORE_THRESHOLD or r >= _VALUE_SURGE_RATIO:
                etf_anomalies.append({**e, "zscore": round(zs, 2)})
    else:
        etf_anomalies = [e for e in sector_etfs if e.get("volume_ratio", 1.0) >= _VALUE_SURGE_RATIO]

    # 공매도 클러스터
    ticker_to_sector = {}
    for sec_name, stocks in by_sector.items():
        for st in stocks:
            t = st.get("종목코드", "") or st.get("ticker", "")
            if t:
                ticker_to_sector[t] = sec_name
    sec_shorts: dict[str, list] = {}
    for item in short_data:
        if item.get("signal") not in ("쇼트커버링_예고", "공매도급감"):
            continue
        t = item.get("ticker", "")
        sec = ticker_to_sector.get(t, "기타")
        sec_shorts.setdefault(sec, []).append(item)
    short_clusters = [
        {"sector": sec, "count": len(items),
         "tickers": [i["ticker"] for i in items],
         "signal":  items[0]["signal"]}
        for sec, items in sec_shorts.items()
        if sec != "기타" and len(items) >= _SHORT_CLUSTER_MIN
    ]

    # 섹터 점수
    _SECTOR_MAP: dict[str, list[str]] = {
        "반도체/IT":      ["반도체", "전기전자", "전자", "IT"],
        "자동차/부품":    ["자동차", "운수장비", "기계"],
        "철강/비철금속":  ["철강", "비철금속", "금속"],
        "건설/부동산":    ["건설업", "건설"],
        "에너지/화학":    ["화학", "에너지", "정유"],
        "해운/조선/운송": ["운수창고", "해운", "조선"],
        "IT/소프트웨어":  ["서비스업", "소프트웨어"],
        "금융/은행":      ["금융업", "은행", "증권"],
        "방산/항공":      ["방위산업", "항공"],
    }
    sector_scores: dict[str, int] = {}
    for e in etf_data:
        sec = e.get("sector", "")
        if not sec or sec == "시장전체":
            continue
        s = sector_scores.get(sec, 0)
        if any(a.get("sector") == sec for a in etf_anomalies):
            s += 15
        if e.get("volume_ratio", 0) >= _VALUE_SURGE_RATIO:
            s += 10
        if e.get("change_pct", 0) > 0:
            s += 5
        sector_scores[sec] = s
    for cluster in short_clusters:
        sec = cluster["sector"]
        s   = sector_scores.get(sec, 0)
        s  += 15 if cluster["signal"] == "쇼트커버링_예고" else 10
        sector_scores[sec] = s

    # 신호 구성
    signals = []
    for e in etf_anomalies:
        sec = e.get("sector", "")
        kw  = _SECTOR_MAP.get(sec, [sec])
        zs  = f" Z={e['zscore']:.1f}" if e.get("zscore") is not None else ""
        signals.append({
            "테마명":   sec,
            "발화신호": f"신호7: {e['name']} 거래량이상 배율={e.get('volume_ratio',0):.1f}x{zs} [섹터ETF]",
            "강도":     4 if e.get("volume_ratio", 1) >= 5 else 3,
            "신뢰도":   "pykrx",
            "발화단계": "1일차", "상태": "신규",
            "관련종목": _sig_sector_top(by_sector, kw),
        })
    for cluster in short_clusters:
        sec = cluster["sector"]
        kw  = _SECTOR_MAP.get(sec, [sec])
        signals.append({
            "테마명":   sec,
            "발화신호": f"신호7: {cluster['signal']} {sec} {cluster['count']}종목 [공매도잔고]",
            "강도":     5 if cluster["signal"] == "쇼트커버링_예고" else 4,
            "신뢰도":   "pykrx",
            "발화단계": "불명", "상태": "신규",
            "관련종목": _sig_sector_top(by_sector, kw),
        })
    signals.sort(key=lambda x: x["강도"], reverse=True)
    return {"signals": signals, "sector_scores": sector_scores}


# ── 신호 8: 기업 이벤트 캘린더 ──────────────────────────────

_EVENT_CFG = {
    "실적발표": {"direction": "+", "base": 4, "lookahead": 2,
                "tmpl": "{corp} 실적발표 D-{days} — 기관 사전 포지셔닝 예상"},
    "IR":       {"direction": "+", "base": 3, "lookahead": 2,
                "tmpl": "{corp} 기업설명회 D-{days} — 기관/외인 관심 선행 유입"},
    "주주총회": {"direction": "mixed", "base": 3, "lookahead": 5,
                "tmpl": "{corp} 주주총회 D-{days} — 소액주주 이슈·배당 확정 예상"},
    "배당":     {"direction": "+", "base": 4, "lookahead": 3,
                "tmpl": "{corp} 배당 공시 D-{days} — 배당락 전 매수 수급 증가"},
}

def _sig_event_impact(events: list[dict]) -> list[dict]:
    results = []
    for ev in events:
        evt_type  = ev.get("event_type", "")
        days      = ev.get("days_until", -1)
        corp      = ev.get("corp_name", "")
        ticker    = ev.get("ticker", "")
        cfg = _EVENT_CFG.get(evt_type)
        if cfg is None or days < 0 or days > cfg["lookahead"]:
            continue
        strength = cfg["base"] + (1 if days <= 1 else 0)
        strength = min(5, strength)
        if strength < config.EVENT_SIGNAL_MIN_STRENGTH:
            continue
        reason = cfg["tmpl"].format(corp=corp, days=days)
        results.append({
            "테마명":   f"{corp} {evt_type}",
            "발화신호": f"신호8: {evt_type} D-{days} [{corp}|{ev.get('event_date','')}|{reason[:30]}]",
            "강도":     strength,
            "신뢰도":   f"event:{cfg['direction']}",
            "발화단계": "1일차",
            "상태":     "신규" if cfg["direction"] == "+" else "경고",
            "관련종목": [corp] if corp else [],
        })
    return results


# ── DataLab 트렌드 ───────────────────────────────────────────

def _sig_datalab_trends(datalab: list[dict], by_sector: dict) -> list[dict]:
    results = []
    for dl in datalab:
        ratio   = dl.get("ratio", 0.0)
        theme   = dl.get("theme", "")
        keyword = dl.get("keyword", theme)
        if ratio < config.DATALAB_SPIKE_THRESHOLD:
            continue
        results.append({
            "테마명":   theme,
            "발화신호": f"DataLab: '{keyword}' 검색 {ratio:.1f}배 급등 — 개인 관심 선행 신호",
            "강도":     4 if ratio >= 2.0 else 3,
            "신뢰도":   f"datalab:{ratio:.1f}x",
            "발화단계": "1일차", "상태": "신규",
            "관련종목": _sig_sector_top(by_sector, [theme])[:3],
        })
    return results


# ── 공통 헬퍼 ────────────────────────────────────────────────

def _sig_dart_strength(dart: dict) -> int:
    report = dart.get("공시종류", "")
    if any(kw in report for kw in ("단일판매공급계약체결", "수주")): return 5
    if any(kw in report for kw in ("판결", "특허")):               return 5
    if "배당결정"       in report: return 4
    if "자사주취득결정" in report: return 4
    if "MOU"           in report: return 3
    if "주요주주"       in report: return 3
    return 1


def _sig_dart_to_theme(report_nm: str, stock_nm: str) -> str:
    if "수주" in report_nm or "공급계약" in report_nm: return f"{stock_nm} 수주"
    if "배당"   in report_nm:                          return f"{stock_nm} 배당"
    if "자사주" in report_nm:                          return f"{stock_nm} 자사주"
    if "특허"   in report_nm or "판결" in report_nm:  return f"{stock_nm} 특허/소송"
    if "주요주주" in report_nm:                        return f"{stock_nm} 내부자매수"
    return f"{stock_nm} 공시"


def _sig_is_positive(change_str: str) -> bool:
    if not change_str or change_str == "N/A":
        return False
    return change_str.startswith("+") or (
        not change_str.startswith("-") and any(c.isdigit() for c in change_str)
    )


def _sig_judge_volatility(us: dict, price_data: dict | None) -> str:
    if price_data:
        kr = max(abs(price_data.get("kospi",  {}).get("change_rate", 0) or 0),
                 abs(price_data.get("kosdaq", {}).get("change_rate", 0) or 0))
        if kr >= 2.0:   return "고변동"
        elif kr >= 1.0: return "중변동"
        else:           return "저변동 (순환매 에너지 낮음)"
    nasdaq = us.get("nasdaq", "N/A")
    if nasdaq == "N/A":
        return "판단불가"
    try:
        val = float(nasdaq.replace("%", "").replace("+", ""))
        return "고변동예상" if abs(val) >= 1.0 else "저변동예상"
    except Exception:
        return "판단불가"


def _sig_sector_top(by_sector: dict, keywords: list[str], top_n: int = None) -> list[str]:
    if not by_sector or not keywords:
        return []
    if top_n is None:
        top_n = config.SECTOR_TOP_N
    matched: list[dict] = []
    for sec_name, entries in by_sector.items():
        if any(kw in sec_name for kw in keywords):
            matched.extend(entries)
    if not matched:
        return []
    matched.sort(key=lambda x: x.get("등락률", 0), reverse=True)
    return [e["종목명"] for e in matched[:top_n]]
