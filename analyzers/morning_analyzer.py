"""
analyzers/morning_analyzer.py
아침 분석 통합 모듈 (v12.0 Step 6 신규 / Step 8 완성)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[통합 내용 — v12.0 Step 6~8]
  ① geopolitics_analyzer     → _analyze_geopolitics() 완전 흡수
  ② theme_analyzer           → _analyze_theme()        내부 함수로 통합
  ③ oracle_analyzer          → _pick_stocks()          내부 함수로 통합 (명칭 변경)
  ④ sector_flow_analyzer     → data_collector._build_signals()로 완전 이전 (Step 8)
                                [이 파일에서 신호7 생성 로직 제거 — ARCHITECTURE #9]
  ⑤ event_impact_analyzer   → _analyze_event_impact()  내부 함수로 통합
  ⑥ ai_analyzer.analyze_dart()    → Gemini 2.5 Flash 재작성
  ⑦ ai_analyzer.analyze_closing() → Gemini 2.5 Flash 재작성

[signal_analyzer 처리 — v12.0 Step 8]
  signal_analyzer → data_collector 내부로 이전 (이 파일 아님)
  data_collector._build_signals() 가 신호1~8 생성 + 캐시에 저장
  이 파일은 data_collector 캐시의 signals 를 받아 Gemini 분석만 수행

[analyze() 실행 순서]
  ① _analyze_geopolitics()        지정학 사전매칭 + Gemini 보완
  ② _analyze_event_impact()       기업이벤트 신호8 (이벤트 점수 계산)
  ③ prebuilt_signals 수신         신호1~8 (data_collector 캐시)
  ④ _analyze_dart_with_gemini()   Gemini 공시 분석
  ⑤ _analyze_closing_with_gemini() Gemini 테마 그룹핑 (신호4 교체)
  ⑥ _analyze_theme()              테마 지도 + 소외도 계산
  ⑦ _pick_stocks()                컨플루언스 스코어링 → 쪽집게 픽

[AI 모델]
  Primary  : gemini-2.5-flash  (google-genai SDK)
  기존 Gemma-3-27b-it / gemini-1.5-flash / gemini-2.0-flash 사용 중단

[PUBLIC API]
  analyze() — morning_report.py가 이 함수 하나만 호출

[ARCHITECTURE 의존성]
  morning_analyzer ← morning_report.py (단순화된 호출)
  morning_analyzer ← data_collector 캐시 (signals, score_summary 포함)
  morning_analyzer → geopolitics_map (utils — 사전 매칭)

[절대 금지 — ARCHITECTURE 준수]
  이 파일에서 KIS API 직접 호출 금지
  이 파일에서 텔레그램 발송 금지
  이 파일에서 DB 기록 금지
  이 파일에서 신호1~8 생성 로직 구현 금지 (data_collector 담당)
  분석·Gemini 호출만 담당

[변경 이력]
  v12.0 Step 6: 신규 생성 (geopolitics/theme/oracle/event_impact 흡수)
  v12.0 Step 8: signal_analyzer → data_collector 이전. analyze() ③ 블록 캐시 수신으로 교체
                sector_flow_analyzer 로직 → data_collector._build_signals()로 완전 이전
                _analyze_sector_flow() 블록 analyze()에서 제거 (dead code 정리)
"""

import json
import re
import statistics
from datetime import datetime, timezone, timedelta
from utils.logger import logger
import config

KST = timezone(timedelta(hours=9))

# ── Gemini API 초기화 ────────────────────────────────────────
_GEMINI_MODEL = "gemini-2.5-flash"   # 지원 모델 (2025 기준)

try:
    from google import genai as _genai_mod
    from google.genai import types as _genai_types

    if config.GOOGLE_AI_API_KEY:
        _CLIENT = _genai_mod.Client(api_key=config.GOOGLE_AI_API_KEY)
        logger.info(f"[morning_analyzer] Gemini ({_GEMINI_MODEL}) 초기화 완료")
    else:
        _CLIENT = None
        logger.warning("[morning_analyzer] GOOGLE_AI_API_KEY 없음 — Gemini 분석 비활성")
except ImportError:
    _CLIENT = None
    logger.warning("[morning_analyzer] google-genai 패키지 없음 — pip install google-genai")


# ══════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════

async def analyze(
    dart_data:        list[dict],
    market_data:      dict,
    news_data:        dict,
    price_data:       dict | None      = None,
    geopolitics_raw:  list[dict] | None = None,  # news_global_rss 수집 결과
    event_calendar:   list[dict] | None = None,  # event_calendar_collector 결과
    sector_etf_data:  list[dict] | None = None,  # sector_etf_collector 결과
    short_data:       list[dict] | None = None,  # short_interest_collector 결과
    # [v12.0 Step 7] data_collector 사전 수집 결과 (있으면 중복 수집 생략)
    closing_strength_result:    list[dict] | None = None,  # 마감강도 데이터
    volume_surge_result:        list[dict] | None = None,  # 거래량급증 데이터
    fund_concentration_result:  list[dict] | None = None,  # 자금집중 데이터
    # [v12.0 Step 8] data_collector가 signal_analyzer 로직으로 생성한 신호 목록
    prebuilt_signals:     list[dict] | None = None,  # data_collector._build_signals() 결과
    prebuilt_market_summary: dict | None = None,     # 미국증시 요약 (data_collector 캐시)
    prebuilt_commodities:    dict | None = None,     # 원자재 (data_collector 캐시)
    prebuilt_volatility:     str  | None = None,     # 변동성 판단 (data_collector 캐시)
    prebuilt_report_picks:   list | None = None,     # 리포트 종목 (data_collector 캐시)
    prebuilt_policy_summary: list | None = None,     # 정책 뉴스 (data_collector 캐시)
    prebuilt_sector_scores:  dict | None = None,     # 섹터 점수 (data_collector 캐시)
    prebuilt_event_scores:   dict | None = None,     # 이벤트 점수 (data_collector 캐시)
) -> dict:
    """
    아침봇 전체 분석 통합 실행 (morning_report.py가 이것만 호출)

    [v12.0 Step 8 변경]
    신호1~8 생성은 data_collector._build_signals()가 담당.
    prebuilt_signals 가 None 이면 signals 빈 목록으로 시작 (하위 호환).
    이 함수는 Gemini 분석(공시·순환매·지정학)과 테마/픽 조합만 수행.

    Args:
        dart_data:           filings.collect() 반환값
        market_data:         market_global.collect() 반환값
        news_data:           news_naver + news_newsapi 통합 결과
        price_data:          price_domestic.collect_daily() 반환값
        geopolitics_raw:     news_global_rss.collect() 반환값
        event_calendar:      event_calendar.collect() 반환값
        sector_etf_data:     sector_etf.collect() 반환값
        short_data:          short_interest.collect() 반환값
        closing_strength_result: 마감강도 데이터
        volume_surge_result:     거래량급증 데이터
        fund_concentration_result: 자금집중 데이터
        prebuilt_signals:    data_collector._build_signals() 결과 (신호1~8)
        prebuilt_*:          data_collector 캐시의 파생 데이터

    Returns: dict {
        "ai_dart_results":      list,   # Gemini 공시 분석 결과
        "signals":              list,   # 신호 1~8 통합 목록 (강도 내림차순)
        "market_summary":       dict,   # 미국증시 요약
        "commodities":          dict,   # 원자재 데이터
        "volatility":           str,    # 변동성 판단
        "report_picks":         list,   # 증권사 리포트 종목
        "policy_summary":       list,   # 정책 뉴스 요약
        "theme_result":         dict,   # 순환매 테마 지도
        "oracle_result":        dict,   # 쪽집게 종목 픽 (_pick_stocks)
        "sector_scores":        dict,   # 섹터 방향성 점수
        "event_scores":         dict,   # 기업이벤트 점수
        "geopolitics_analyzed": list,   # 지정학 분석 결과
    }
    """
    result: dict = {
        "ai_dart_results":      [],
        "signals":              [],
        "market_summary":       {},
        "commodities":          {},
        "volatility":           "판단불가",
        "report_picks":         [],
        "policy_summary":       [],
        "theme_result":         {"theme_map": [], "volatility": "판단불가", "top_signals": []},
        "oracle_result":        None,
        "sector_scores":        {},
        "event_scores":         {},
        "geopolitics_analyzed": [],
    }

    # ── ① 지정학 분석 (geopolitics_analyzer 흡수) ─────────────
    if geopolitics_raw:
        try:
            geo_analyzed = _analyze_geopolitics(geopolitics_raw)
            result["geopolitics_analyzed"] = geo_analyzed
            logger.info(f"[morning_analyzer] 지정학 분석 완료 — {len(geo_analyzed)}건")
        except Exception as e:
            logger.warning(f"[morning_analyzer] 지정학 분석 실패: {e}")

    # ── ② 기업 이벤트 분석 (event_impact_analyzer 내부 통합) ──
    event_impact_signals: list[dict] = []
    if event_calendar and config.EVENT_CALENDAR_ENABLED:
        try:
            event_impact_signals = _analyze_event_impact(event_calendar)
            for ev in event_impact_signals:
                ticker = ev.get("ticker", "")
                if ticker:
                    result["event_scores"][ticker] = max(
                        result["event_scores"].get(ticker, 0), ev.get("strength", 3)
                    )
            logger.info(f"[morning_analyzer] 이벤트 신호8 {len(event_impact_signals)}건")
        except Exception as e:
            logger.warning(f"[morning_analyzer] 이벤트 분석 실패: {e}")

    # ── ③ 신호 1~8 — data_collector 캐시에서 수신 (Step 8) ────
    # signal_analyzer 로직은 data_collector._build_signals()로 이전됨.
    # prebuilt_signals 가 있으면 그대로 사용. 없으면 빈 목록 (하위 호환).
    result["signals"]        = list(prebuilt_signals or [])
    result["market_summary"] = dict(prebuilt_market_summary or
                                    market_data.get("us_market", {}))
    result["commodities"]    = dict(prebuilt_commodities or
                                    market_data.get("commodities", {}))
    result["volatility"]     = prebuilt_volatility or "판단불가"
    result["report_picks"]   = list(prebuilt_report_picks or [])
    result["policy_summary"] = list(prebuilt_policy_summary or [])
    if prebuilt_sector_scores:
        result["sector_scores"] = dict(prebuilt_sector_scores)
    if prebuilt_event_scores:
        result["event_scores"]  = dict(prebuilt_event_scores)
    logger.info(f"[morning_analyzer] 신호 수신 — {len(result['signals'])}개 (data_collector 캐시)")

    # ── ④ Gemini 공시 분석 (ai_analyzer.analyze_dart 대체) ───
    if dart_data:
        try:
            ai_dart = _analyze_dart_with_gemini(dart_data)
            result["ai_dart_results"] = ai_dart
            if ai_dart:
                _enrich_signals_with_dart(result["signals"], ai_dart)
                logger.info(f"[morning_analyzer] Gemini 공시 분석 {len(ai_dart)}건")
        except Exception as e:
            logger.warning(f"[morning_analyzer] Gemini 공시 분석 실패: {e}")

    # ── ⑤ Gemini 순환매 테마 그룹핑 (ai_analyzer.analyze_closing 대체) ─
    if price_data:
        try:
            ai_closing = _analyze_closing_with_gemini(price_data)
            if ai_closing:
                non_signal4 = [s for s in result["signals"] if "신호4" not in s.get("발화신호", "")]
                result["signals"] = non_signal4 + ai_closing
                result["signals"].sort(key=lambda x: x["강도"], reverse=True)
                logger.info(f"[morning_analyzer] Gemini 테마 그룹핑 {len(ai_closing)}개 (신호4 교체)")
        except Exception as e:
            logger.warning(f"[morning_analyzer] Gemini 테마 그룹핑 실패: {e}")

    # ── ⑥ 테마 분석 (theme_analyzer 내부 통합) ───────────────
    try:
        price_by_name = price_data.get("by_name", {}) if price_data else {}
        result["theme_result"] = _analyze_theme(
            signal_result = {"signals": result["signals"]},
            price_data    = price_by_name,
        )
    except Exception as e:
        logger.warning(f"[morning_analyzer] 테마 분석 실패: {e}")

    # ── ⑦ 쪽집게 픽 생성 (_pick_stocks — oracle_analyzer 내부 통합) ─
    try:
        import utils.watchlist_state as watchlist_state
        market_env_val = watchlist_state.get_market_env() or ""
        result["oracle_result"] = _pick_stocks(
            theme_map        = result["theme_result"].get("theme_map", []),
            price_by_name    = price_data.get("by_name", {}) if price_data else {},
            institutional    = price_data.get("institutional", []) if price_data else [],
            ai_dart_results  = result["ai_dart_results"],
            signals          = result["signals"],
            market_env       = market_env_val,
            sector_scores    = result["sector_scores"],
            event_scores     = result["event_scores"],
            # [v12.0 Step 7] data_collector 사전 수집 결과 전달
            closing_strength = closing_strength_result,
            volume_surge     = volume_surge_result,
            fund_concentration = fund_concentration_result,
        )
    except Exception as e:
        logger.warning(f"[morning_analyzer] 쪽집게 픽 실패 (비치명적): {e}")
        result["oracle_result"] = None

    return result


# ══════════════════════════════════════════════════════════════
# ① 지정학 분석 — geopolitics_analyzer 완전 흡수
# ══════════════════════════════════════════════════════════════

def _analyze_geopolitics(raw_news: list[dict]) -> list[dict]:
    """
    지정학 뉴스 → 영향 섹터 매핑 + Gemini 검증.
    (geopolitics_analyzer.analyze() 대체)

    Step 1: geopolitics_map 사전 기반 패턴 매칭
    Step 2: Gemini 2.5 Flash 배치 분석 (보완)
    Step 3: 신뢰도 필터링
    """
    if not raw_news:
        return []

    from utils.geopolitics_map import lookup as map_lookup

    # Step 1: 사전 매칭
    event_agg: dict[str, dict] = {}
    for article in raw_news:
        text    = article.get("raw_text", "")
        title   = article.get("title", "")
        matches = map_lookup(text + " " + title)
        for match in matches:
            key = match["key"]
            if key not in event_agg:
                event_agg[key] = {"map_entry": match, "articles": [], "hit_count": 0}
            event_agg[key]["articles"].append(article)
            event_agg[key]["hit_count"] += 1

    map_results: list[dict] = []
    for key, agg in event_agg.items():
        entry     = agg["map_entry"]
        hit_count = agg["hit_count"]
        base_conf = entry.get("confidence_base", 0.6)
        confidence = min(base_conf + (hit_count - 1) * 0.05, 0.95)

        articles = sorted(agg["articles"], key=lambda a: a.get("published", ""), reverse=True)
        rep      = articles[0] if articles else {}

        map_results.append({
            "event_type":       key,
            "affected_sectors": entry.get("sectors", []),
            "impact_direction": entry.get("impact", "+"),
            "confidence":       round(confidence, 3),
            "source_url":       rep.get("link", ""),
            "event_summary_kr": rep.get("title", entry.get("description", key)),
        })

    logger.info(f"[morning_analyzer] 지정학 사전 매칭: {len(map_results)}건")

    # Step 2: Gemini 보완 (AI 사용 가능 시)
    results = map_results
    if _CLIENT and map_results:
        try:
            results = _enhance_geopolitics_with_gemini(map_results, raw_news)
        except Exception as e:
            logger.warning(f"[morning_analyzer] 지정학 Gemini 보완 실패: {e}")

    # Step 3: 신뢰도 필터링
    min_conf = config.GEOPOLITICS_CONFIDENCE_MIN
    filtered = [r for r in results if r.get("confidence", 0) >= min_conf]
    filtered.sort(key=lambda x: x.get("confidence", 0), reverse=True)

    logger.info(f"[morning_analyzer] 지정학 최종 {len(filtered)}건 (신뢰도≥{min_conf})")
    return filtered


def _enhance_geopolitics_with_gemini(
    map_results: list[dict],
    raw_news:    list[dict],
) -> list[dict]:
    """Gemini로 지정학 이벤트 배치 분석 및 신뢰도 보정."""
    news_texts = "\n".join(
        f"{i+1}. [{a.get('source','')}] {a.get('title','')}"
        for i, a in enumerate(raw_news[:10])
    )
    matched_keys = [r["event_type"] for r in map_results]

    prompt = f"""당신은 한국 주식 시장 전문가입니다.
아래 뉴스를 분석하여 한국 주식 시장에 영향을 줄 지정학·정책 이벤트를 식별하세요.

[뉴스 목록]
{news_texts}

[이미 감지된 이벤트]
{matched_keys}

다음 형식의 JSON 배열만 출력 (다른 텍스트 없음):
[
  {{
    "event_type": "이벤트 유형 (한국어)",
    "affected_sectors": ["섹터1", "섹터2"],
    "impact_direction": "+" 또는 "-" 또는 "mixed",
    "confidence": 0.0~1.0,
    "event_summary_kr": "50자 이내 한국어 요약"
  }}
]

규칙:
- 이미 감지된 이벤트는 신뢰도 조정 포함
- 새로운 이벤트도 추가
- 한국 주식 시장과 무관한 이벤트는 제외
- 섹터명: 철강/비철금속, 산업재/방산, 기술/반도체, 에너지/정유, 소재/화학, 바이오/헬스케어, 금융, 조선, 배터리, 자동차부품"""

    raw = _call_gemini(prompt)

    # JSON 추출
    clean = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
    match = re.search(r"\[", clean)
    if not match:
        return map_results
    json_str = clean[match.start():]
    end = json_str.rfind("]")
    if end == -1:
        return map_results
    ai_results = json.loads(json_str[:end + 1])

    # 사전 + AI 병합
    merged: dict[str, dict] = {r["event_type"]: r for r in map_results}
    for ai in ai_results:
        if not isinstance(ai, dict):
            continue
        etype = ai.get("event_type", "")
        if not etype:
            continue
        if etype in merged:
            exist = merged[etype]
            exist["confidence"] = round(
                min(exist["confidence"] * 0.6 + float(ai.get("confidence", 0)) * 0.4, 0.95), 3
            )
        else:
            merged[etype] = {
                "event_type":       etype,
                "affected_sectors": ai.get("affected_sectors", []),
                "impact_direction": ai.get("impact_direction", "+"),
                "confidence":       round(float(ai.get("confidence", 0.5)), 3),
                "source_url":       "",
                "event_summary_kr": ai.get("event_summary_kr", etype),
            }

    return list(merged.values())


# ══════════════════════════════════════════════════════════════
# ② Gemini 공시 분석 — ai_analyzer.analyze_dart() 대체
# ══════════════════════════════════════════════════════════════

def _analyze_dart_with_gemini(dart_list: list[dict]) -> list[dict]:
    """
    DART 공시 리스트 → Gemini 2.5 Flash로 호재/악재 점수화.
    (ai_analyzer.analyze_dart() 대체)

    Returns:
        [{"종목명": str, "점수": int(1~10), "이유": str, "상한가확률": str}]
    """
    if not _CLIENT or not dart_list:
        return []

    top   = dart_list[:5]
    items = "\n".join(
        f"{i+1}. [{d['종목명']}] {d['공시종류']} — {d['공시시각']}"
        for i, d in enumerate(top)
    )

    time_ctx = _get_market_time_context()

    prompt = f"""한국 주식 공시 분석 전문가다. 다음 공시들을 분석하라.

## 시간 컨텍스트
{time_ctx}

## DART 공시 유형별 판단 기준
- 수주/계약: 매출 대비 규모 중요. 시총 대비 10% 이상이면 점수 7+
- 배당결정: 단기 수급 긍정, 성장 기대치 낮음. 점수 5~6
- 자사주 취득: 단기 수급 방어. 규모 대비 점수 조정
- 유상증자: 주가 희석 → 악재. 점수 1~3
- 대규모 내부자 매도: 강한 악재 신호. 점수 1~2
- 특허/기술이전: 기술 가치 인정. 점수 6~8

## 공시 목록
{items}

JSON 배열만 출력. 다른 텍스트 없이:
[
  {{"번호": 1, "점수": 8, "이유": "대규모 수주로 매출 성장 기대", "상한가확률": "높음"}},
  {{"번호": 2, "점수": 4, "이유": "배당 결정, 단기 수급 긍정", "상한가확률": "낮음"}}
]

규칙:
- 점수: 1(강한악재)~10(강한호재), 5는 중립
- 상한가확률: 높음 또는 중간 또는 낮음
- 이유: 20자 이내"""

    try:
        raw  = _call_gemini(prompt)
        data = _extract_json(raw)
        if not isinstance(data, list):
            return []

        results = []
        for item in data:
            idx = int(item.get("번호", 1)) - 1
            if 0 <= idx < len(top):
                results.append({
                    "종목명":     top[idx]["종목명"],
                    "점수":       int(item.get("점수", 5)),
                    "이유":       item.get("이유", ""),
                    "상한가확률": item.get("상한가확률", "낮음"),
                })
        return results

    except Exception as e:
        logger.warning(f"[morning_analyzer] Gemini 공시 분석 실패: {e}")
        return []


# ══════════════════════════════════════════════════════════════
# ③ Gemini 테마 그룹핑 — ai_analyzer.analyze_closing() 대체
# ══════════════════════════════════════════════════════════════

def _analyze_closing_with_gemini(price_result: dict) -> list[dict]:
    """
    전날 상한가+급등 → Gemini 2.5 Flash로 테마별 그룹핑 + 소외주 식별.
    (ai_analyzer.analyze_closing() 대체)

    Returns:
        list[dict] — signal_result["signals"] 형식 (테마 신호 목록)
    """
    if not _CLIENT:
        return []

    upper   = price_result.get("upper_limit", [])
    gainers = price_result.get("top_gainers", [])
    all_stocks = price_result.get("by_name", {})

    if not upper and not gainers:
        return []

    # 5%↑ 종목 + 등락률 (대장주 선정용)
    all_movers = {
        name: info["등락률"]
        for name, info in all_stocks.items()
        if isinstance(info.get("등락률"), float) and info["등락률"] >= 5.0
    }

    upper_str = "\n".join(
        f"  - {s['종목명']} +{s['등락률']:.1f}% ({s['시장']})" for s in upper[:15]
    )
    gainers_str = "\n".join(
        f"  - {s['종목명']} +{s['등락률']:.1f}% ({s['시장']})" for s in gainers[:15]
    )
    movers_sorted = sorted(all_movers.items(), key=lambda x: -x[1])
    movers_str = "\n".join(
        f"  {name}: +{rate:.1f}%"
        for name, rate in movers_sorted[:50]
    )

    time_ctx = _get_market_time_context()

    prompt = f"""한국 주식시장 전날 마감 데이터 분석 — 내일 순환매 지도 작성

## 시간 컨텍스트
{time_ctx}

=== 전날 상한가 ===
{upper_str if upper_str else '없음'}

=== 전날 급등(7%↑) ===
{gainers_str if gainers_str else '없음'}

=== 전날 5%↑ 전체 (등락률 높은 순) ===
{movers_str if movers_str else '없음'}

**목표**: 같은 테마(섹터)끼리 묶고 대장주와 소외주를 식별하라.

**핵심 규칙**:
1. 테마명: 실제 시장 통용 명칭 (바이오신약, 전선구리, AI반도체, 방산, 2차전지 등)
2. 관련종목[0] = 해당 테마에서 등락률이 가장 높은 종목 (대장주)
3. 관련종목[1],[2]... = 같은 테마인데 등락률이 낮은 소외주
4. 소외주는 반드시 위 5%↑ 전체 목록에 있는 종목만 포함
5. 테마가 다른 종목끼리 억지로 묶지 말 것
6. 최대 5개 테마, 강도 높은 순
7. JSON 배열만 출력, 설명 없이

[
  {{
    "테마명": "바이오신약",
    "강도": 5,
    "관련종목": ["에이프로젠", "나노엔텍", "케스피온"],
    "ai_memo": "에이프로젠 주도 상한가, 나노엔텍 소외"
  }}
]"""

    try:
        raw    = _call_gemini(prompt)
        parsed = _extract_json(raw)
        if not isinstance(parsed, list):
            return []

        signals = []
        for item in parsed:
            관련종목 = item.get("관련종목", [])
            if not 관련종목:
                continue
            강도 = max(1, min(5, int(item.get("강도", 3))))
            signals.append({
                "테마명":   item.get("테마명", "기타"),
                "발화신호": f"신호4(AI): {item.get('ai_memo', '')[:50]}",
                "강도":     강도,
                "신뢰도":   "Gemini",
                "발화단계": "오늘",
                "상태":     "신규",
                "관련종목": 관련종목,
                "ai_memo":  item.get("ai_memo", ""),
            })

        signals.sort(key=lambda x: x["강도"], reverse=True)
        return signals

    except Exception as e:
        logger.warning(f"[morning_analyzer] Gemini 테마 그룹핑 실패: {e}")
        return []


# ══════════════════════════════════════════════════════════════
# ④ 테마 분석 — theme_analyzer 내부 통합
# ══════════════════════════════════════════════════════════════

def _analyze_theme(signal_result: dict, price_data: dict) -> dict:
    """
    테마 그룹핑 + 순환매 소외도 계산.
    (theme_analyzer.analyze() 내부 통합)
    """
    signals    = signal_result.get("signals", [])
    volatility = signal_result.get("volatility", "판단불가")

    theme_map = _build_theme_map(signals, price_data)

    return {
        "theme_map":   theme_map,
        "volatility":  volatility,
        "top_signals": signals[:3],
    }


def _build_theme_map(signals: list[dict], price_data: dict) -> list[dict]:
    themes = []
    for signal in signals:
        관련종목 = signal.get("관련종목", [])
        if not 관련종목:
            continue

        종목들 = []
        대장등락률 = None

        for i, 종목명 in enumerate(관련종목):
            등락률 = price_data.get(종목명, {}).get("등락률", "N/A") if price_data else "N/A"

            if i == 0:
                대장등락률 = 등락률
                포지션 = "이미과열" if (isinstance(등락률, float) and 등락률 >= 15) else "대장"
            else:
                if isinstance(등락률, float) and isinstance(대장등락률, float):
                    소외도 = round(대장등락률 - 등락률, 1)
                    if 소외도 >= 20:
                        포지션 = "오늘★"
                    elif 소외도 >= 10:
                        포지션 = "내일"
                    else:
                        포지션 = "모니터"
                else:
                    소외도 = "N/A"
                    포지션 = "모니터"

                종목들.append({
                    "종목명": 종목명,
                    "등락률": 등락률,
                    "소외도": 소외도,
                    "포지션": 포지션,
                })

        themes.append({
            "테마명":     signal["테마명"],
            "대장주":     관련종목[0] if 관련종목 else "N/A",
            "대장등락률": 대장등락률 if 대장등락률 is not None else "N/A",
            "종목들":     종목들,
            "상태":       signal["상태"],
            "발화신호":   signal["발화신호"],
        })

    return themes


# ══════════════════════════════════════════════════════════════
# ⑤ 기업 이벤트 분석 — event_impact_analyzer 내부 통합
# ══════════════════════════════════════════════════════════════

_EVENT_CONFIG = {
    "실적발표": {"direction": "+", "base_strength": 4,
                "reason_template": "{corp} 실적발표 D-{days} — 기관 사전 포지셔닝 예상",
                "lookahead_days": 2},
    "IR":       {"direction": "+", "base_strength": 3,
                "reason_template": "{corp} 기업설명회 D-{days} — 기관/외인 관심 선행 유입",
                "lookahead_days": 2},
    "주주총회": {"direction": "mixed", "base_strength": 3,
                "reason_template": "{corp} 주주총회 D-{days} — 소액주주 이슈·배당 확정 예상",
                "lookahead_days": 5},
    "배당":     {"direction": "+", "base_strength": 4,
                "reason_template": "{corp} 배당 공시 D-{days} — 배당락 전 매수 수급 증가",
                "lookahead_days": 3},
}


def _analyze_event_impact(events: list[dict]) -> list[dict]:
    """기업 이벤트 → 신호8. (event_impact_analyzer.analyze() 내부 통합)"""
    if not events:
        return []
    signals = []
    for ev in events:
        sig = _process_event(ev)
        if sig is not None:
            signals.append(sig)
    signals.sort(key=lambda x: (-x["strength"], x["days_until"]))
    logger.info(f"[morning_analyzer] 이벤트 신호8 {len(signals)}건")
    return signals


def _process_event(ev: dict) -> dict | None:
    event_type = ev.get("event_type", "")
    days_until = ev.get("days_until", -1)
    corp_name  = ev.get("corp_name", "")
    ticker     = ev.get("ticker", "")
    cfg = _EVENT_CONFIG.get(event_type)
    if cfg is None:
        return None
    if days_until < 0 or days_until > cfg["lookahead_days"]:
        return None
    strength = cfg["base_strength"]
    if days_until <= 1:
        strength = min(5, strength + 1)
    return {
        "event_type":       event_type,
        "corp_name":        corp_name,
        "ticker":           ticker,
        "event_date":       ev.get("event_date", ""),
        "days_until":       days_until,
        "impact_direction": cfg["direction"],
        "strength":         strength,
        "reason":           cfg["reason_template"].format(corp=corp_name, days=days_until),
    }


# ══════════════════════════════════════════════════════════════
# ⑥ 쪽집게 픽 — oracle_analyzer 내부 통합 (_pick_stocks)
# ══════════════════════════════════════════════════════════════

_TARGET_PCT = {"오늘★": 0.15, "내일": 0.12, "모니터": 0.10, "대장": 0.08, "": 0.10}
_STOP_PCT   = -0.07
_RR_THRESHOLD = {"강세장": 1.2, "약세장": 2.0, "약세장/횡보": 2.0, "횡보": 2.0, "": 1.5}


def _pick_stocks(
    theme_map:       list[dict],
    price_by_name:   dict,
    institutional:   list[dict],
    ai_dart_results: list[dict],
    signals:         list[dict],
    market_env:      str  = "",
    closing_strength: list | None = None,
    volume_surge:      list | None = None,
    fund_concentration: list | None = None,
    sector_scores:   dict | None = None,
    event_scores:    dict | None = None,
) -> dict:
    """
    컨플루언스 스코어링 → 내일 주도 테마 + 종목 픽.
    (oracle_analyzer.analyze() 내부 통합 완료 — 명칭: oracle_analyzer → _pick_stocks)
    """
    _empty = {
        "picks": [], "top_themes": [],
        "market_env": market_env,
        "rr_threshold": _RR_THRESHOLD.get(market_env, 1.5),
        "one_line": f"[{market_env or '장세미정'}] 분석 데이터 부족",
        "has_data": False,
    }

    if not isinstance(price_by_name, dict):
        logger.warning("[morning_analyzer._pick_stocks] price_by_name이 dict가 아님")
        return _empty

    if not theme_map and not signals:
        return _empty

    try:
        # 보조 데이터 인덱싱
        inst_map   = {s.get("종목명", ""): s for s in institutional if s.get("종목명")}
        dart_map   = {r.get("종목명", ""): r for r in ai_dart_results if r.get("종목명")}
        cs_set     = {s.get("종목코드", "") for s in (closing_strength or []) if s.get("종목코드")}
        vf_set     = {s.get("종목코드", "") for s in (volume_surge or [])      if s.get("종목코드")}
        fi_set     = {s.get("종목코드", "") for s in (fund_concentration or [])      if s.get("종목코드")}
        sector_map = sector_scores or {}
        event_map  = event_scores  or {}

        # 신호 맵
        sig_map: dict[str, dict] = {}
        for s in signals:
            theme = s.get("테마명", "")
            if theme:
                sig_map[theme] = s
            for name in s.get("관련종목", []):
                if name:
                    sig_map[name] = s

        rr_threshold = _RR_THRESHOLD.get(market_env, 1.5)

        # 테마 스코어링
        scored = []
        for theme in theme_map:
            score, factors = _score_theme_internal(
                theme, price_by_name, inst_map, dart_map,
                cs_set, vf_set, fi_set, sig_map, sector_map, event_map,
            )
            if score > 0:
                scored.append({
                    "theme": theme.get("테마명", ""),
                    "score": score, "factors": factors,
                    "leader": theme.get("대장주", ""),
                    "leader_change": theme.get("대장등락률", 0.0),
                    "_theme_obj": theme,
                })
        scored.sort(key=lambda x: x["score"], reverse=True)
        top_themes = [{k: v for k, v in t.items() if k != "_theme_obj"} for t in scored[:3]]

        # 픽 추출
        picks = []
        seen  = set()
        for t_entry in scored[:3]:
            t_obj  = t_entry["_theme_obj"]
            t_name = t_entry["theme"]
            for stock in t_obj.get("종목들", []):
                name = stock.get("종목명", "")
                if not name or name in seen:
                    continue
                info  = price_by_name.get(name, {})
                code  = info.get("종목코드", "")
                price = info.get("종가", 0) or info.get("현재가", 0)
                if price <= 0:
                    continue
                pos_type = stock.get("포지션", "")
                pick = _build_pick_entry(
                    name, code, t_name, price, pos_type, t_entry["score"],
                    inst_map, dart_map, cs_set, vf_set, fi_set, rr_threshold,
                )
                if pick:
                    seen.add(name)
                    picks.append(pick)
                if len(picks) >= 5:
                    break
            if len(picks) >= 5:
                break

        for i, p in enumerate(picks, 1):
            p["rank"] = i

        one_line = (
            f"[{market_env or '장세미정'}] 조건 충족 픽 없음 (R/R {rr_threshold:.1f}x 미달)"
            if not picks
            else (
                f"[{market_env or '장세미정'}] 주도테마: "
                + " · ".join(t["theme"] for t in top_themes[:2])
                + f" | 최선픽: {picks[0]['name']} "
                + f"(진입{picks[0]['entry_price']:,} → 목표{picks[0]['target_price']:,} "
                + f"/ 손절{picks[0]['stop_price']:,}  R/R {picks[0]['rr_ratio']:.1f})"
            )
        )

        return {
            "picks": picks, "top_themes": top_themes,
            "market_env": market_env, "rr_threshold": rr_threshold,
            "one_line": one_line, "has_data": bool(picks),
        }

    except Exception as e:
        logger.warning(f"[morning_analyzer._pick_stocks] 실패: {e}", exc_info=True)
        return _empty


def _score_theme_internal(
    theme, price_by_name, inst_map, dart_map, cs_set, vf_set, fi_set,
    sig_map, sector_scores, event_scores,
) -> tuple[int, list[str]]:
    """컨플루언스 점수 계산. (_pick_stocks 내부 함수 — oracle_analyzer._score_theme 완전 통합)"""
    score   = 0
    factors = []
    stocks  = theme.get("종목들", [])
    t_name  = theme.get("테마명", "")

    # 기관/외인 수급 (최대 30점)
    inst_c = sum(1 for st in stocks if inst_map.get(st.get("종목명",""),{}).get("기관순매수",0) > 0)
    frgn_c = sum(1 for st in stocks if inst_map.get(st.get("종목명",""),{}).get("외국인순매수",0) > 0)
    sm = inst_c + frgn_c
    if sm >= 6:   score += 30; factors.append(f"기관/외인 {sm}종목 ★★★")
    elif sm >= 4: score += 22; factors.append(f"기관/외인 {sm}종목 ★★")
    elif sm >= 2: score += 14; factors.append(f"기관/외인 {sm}종목 ★")
    elif sm >= 1: score += 7;  factors.append(f"기관/외인 {sm}종목")

    # 소외도 에너지 (최대 25점)
    total_소외 = sum(
        st.get("소외도", 0.0) for st in stocks if isinstance(st.get("소외도"), (int, float))
    )
    avg_소외 = total_소외 / len(stocks) if stocks else 0
    if avg_소외 >= 5.0:   score += 25; factors.append(f"소외도 {avg_소외:.1f} ★★★")
    elif avg_소외 >= 3.0: score += 18; factors.append(f"소외도 {avg_소외:.1f} ★★")
    elif avg_소외 >= 1.5: score += 10; factors.append(f"소외도 {avg_소외:.1f} ★")
    elif avg_소외 > 0:    score += 5;  factors.append(f"소외도 {avg_소외:.1f}")

    # 마감강도 (최대 20점)
    cs_c = sum(
        1 for st in stocks
        if price_by_name.get(st.get("종목명",""),{}).get("종목코드","") in cs_set
    )
    if cs_c >= 3:   score += 20; factors.append(f"마감강도 {cs_c}종목 ★★★")
    elif cs_c == 2: score += 14; factors.append(f"마감강도 {cs_c}종목 ★★")
    elif cs_c == 1: score += 8;  factors.append(f"마감강도 {cs_c}종목 ★")

    # 공시 AI 점수 (최대 15점)
    max_dart = max(
        (dart_map.get(st.get("종목명",""),{}).get("점수",0) for st in stocks), default=0
    )
    if max_dart >= 9:   score += 15; factors.append(f"공시AI {max_dart}/10 ★★★")
    elif max_dart >= 7: score += 10; factors.append(f"공시AI {max_dart}/10 ★★")
    elif max_dart >= 5: score += 5;  factors.append(f"공시AI {max_dart}/10 ★")

    # 자금집중/거래량급증 보조 (최대 10점)
    fi_c = sum(
        1 for st in stocks
        if price_by_name.get(st.get("종목명",""),{}).get("종목코드","") in fi_set
    )
    vf_c = sum(
        1 for st in stocks
        if price_by_name.get(st.get("종목명",""),{}).get("종목코드","") in vf_set
    )
    if fi_c >= 2:   score += 7; factors.append(f"자금집중 {fi_c}종목")
    elif fi_c == 1: score += 4; factors.append(f"자금집중 {fi_c}종목")
    if vf_c >= 1:   score += 3; factors.append(f"거래량급증 {vf_c}종목")

    # 신호 강도 보너스 (최대 5점)
    sig = sig_map.get(t_name)
    if sig:
        sig_s = sig.get("강도", 0)
        if sig_s >= 5:   score += 5; factors.append("신호강도 ★★★★★")
        elif sig_s >= 4: score += 4; factors.append("신호강도 ★★★★")
        elif sig_s >= 3: score += 2; factors.append("신호강도 ★★★")

    # 철강/방산 부스팅 (+20)
    BOOST_THEMES = {"철강/비철금속", "철강", "방산", "산업재/방산", "에너지솔루션", "자동차부품"}
    if t_name in BOOST_THEMES and t_name in sig_map:
        score += 20
        factors.append("🌍 지정학/철강ETF 부스팅 +20")

    # 섹터 수급 보너스 (+10~+20)
    if sector_scores:
        sf = sector_scores.get(t_name, 0)
        if sf >= 30:   score += 20; factors.append("📊 섹터ETF+공매도 수급 +20")
        elif sf >= 15: score += 10; factors.append("📊 섹터ETF 이상 +10")

    return score, factors


def _build_pick_entry(
    name, ticker, theme, entry_price, position_type,
    theme_score, inst_map, dart_map, cs_set, vf_set, fi_set, rr_threshold,
) -> dict | None:
    target_pct  = _TARGET_PCT.get(position_type, _TARGET_PCT[""])
    target_price = round(entry_price * (1 + target_pct))
    stop_price   = round(entry_price * (1 + _STOP_PCT))

    ret  = target_price - entry_price
    loss = entry_price  - stop_price
    if loss <= 0:
        return None

    rr_ratio = round(ret / loss, 1)
    if rr_ratio < rr_threshold:
        return None

    badges = []
    m = inst_map.get(name, {})
    if m.get("기관순매수", 0) > 0 and m.get("외국인순매수", 0) > 0:
        badges.append("기관/외인↑")
    elif m.get("기관순매수", 0) > 0:
        badges.append("기관↑")
    elif m.get("외국인순매수", 0) > 0:
        badges.append("외인↑")

    if ticker in cs_set: badges.append("마감강도↑")
    if ticker in vf_set: badges.append("거래량급증")
    if ticker in fi_set: badges.append("자금집중↑")

    dart = dart_map.get(name, {})
    if dart.get("점수", 0) >= 7:
        badges.append(f"공시AI {dart['점수']}/10")

    return {
        "rank": 0, "ticker": ticker, "name": name, "theme": theme,
        "entry_price": entry_price, "target_price": target_price, "stop_price": stop_price,
        "target_pct":  round(target_pct * 100, 1),
        "stop_pct":    round(_STOP_PCT * 100, 1),
        "rr_ratio":    rr_ratio,
        "score":       theme_score,
        "badges":      badges,
        "position_type": position_type,
    }


# ══════════════════════════════════════════════════════════════
# 공통 유틸
# ══════════════════════════════════════════════════════════════

def _get_market_time_context() -> str:
    """현재 KST 시각 기준 장중/마감후 컨텍스트."""
    now   = datetime.now(KST)
    open_ = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
    close = now.replace(hour=15, minute=20, second=0, microsecond=0)
    if open_ <= now <= close:
        return (
            f"현재 시각: {now.strftime('%H:%M')} KST (장중)\n"
            "⚠️ 장중 데이터 주의: 오늘 거래량·캔들은 미완성 형성 중.\n"
            "  - 전일 또는 최근 확정 데이터를 기준으로 분석할 것"
        )
    return (
        f"현재 시각: {now.strftime('%H:%M')} KST (마감 후)\n"
        "✅ 당일 데이터 확정: 거래량·캔들·등락률 모두 신뢰 가능."
    )


def _call_gemini(prompt: str) -> str:
    """Gemini 2.5 Flash API 호출."""
    if not _CLIENT:
        raise RuntimeError("Gemini 클라이언트 미초기화")
    response = _CLIENT.models.generate_content(
        model   = _GEMINI_MODEL,
        contents= prompt,
        config  = _genai_types.GenerateContentConfig(
            temperature      = 0.2,
            max_output_tokens= 1500,
        ),
    )
    return response.text


def _extract_json(raw: str):
    """AI 응답에서 JSON 추출 (마크다운 펜스 제거 포함)."""
    cleaned = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
    match   = re.search(r"[\[{]", cleaned)
    if not match:
        raise ValueError(f"JSON 없음: {cleaned[:80]}")
    json_str = cleaned[match.start():]
    end = json_str.rfind("]") if json_str.startswith("[") else json_str.rfind("}")
    if end == -1:
        raise ValueError("JSON 종료 토큰 없음")
    json_str = json_str[:end + 1]
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        # 개별 객체 복구 시도
        results = []
        for m in re.finditer(r"\{[^{}]+\}", json_str):
            try:
                results.append(json.loads(m.group()))
            except Exception:
                continue
        if results:
            return results
        raise


def _enrich_signals_with_dart(signals: list[dict], ai_results: list[dict]) -> None:
    """AI 공시 분석 결과로 신호 강도 보정 (in-place)."""
    ai_map = {r["종목명"]: r for r in ai_results}
    for signal in signals:
        관련종목 = signal.get("관련종목", [])
        if not 관련종목:
            continue
        ai = ai_map.get(관련종목[0], {})
        if ai.get("점수", 0) >= 8:
            signal["강도"] = min(5, signal.get("강도", 3) + 1)
            signal["ai_메모"] = f"AI: {ai['이유']} ({ai['상한가확률']})"

