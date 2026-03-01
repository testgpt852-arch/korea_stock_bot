"""
analyzers/morning_analyzer.py
아침 분석 통합 모듈 (v13.0 Step 7 — 3단계 Gemini 구조 전면 개편)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[개편 내용 — v13.0 Step 7]
  기존 다단계 중복 Gemini 호출 구조를 버리고
  REDESIGN_v13 §5 기준 3단계 호출로 재설계.

  ① _analyze_market_env(market_data)
       입력: 미국 섹터ETF 등락(±2%+) + 원자재 + 환율
       출력: {"환경": "리스크온/오프/중립", "주도테마후보": [...], "한국시장영향": str}

  ② _analyze_materials(cache, market_env)
       입력: DART 본문 + 뉴스 + 가격(상한가/급등/시총필터) + ①결과
       출력: {"후보종목": [...], "제외근거": str}  (최대 20종목)

  ③ _pick_final(cache, candidates, rag_context)
       입력: 자금집중 + 공매도 + RAG 과거패턴 + ②결과
       출력: {"picks": [...]}  (최대 15종목)

[analyze() 실행 순서]
  cache = data_collector.get_cache()
  market_env  = _analyze_market_env(cache)
  candidates  = _analyze_materials(cache, market_env)
  picks       = _pick_final(cache, candidates, rag_context)

[PUBLIC API]
  analyze(cache) — morning_report.py 가 이 함수 하나만 호출.

[반환값]
  {
    "market_env":  dict,   # 호출① 결과
    "candidates":  dict,   # 호출② 결과
    "picks":       list,   # 호출③ 결과 picks 리스트
  }

[절대 불변 규칙 — REDESIGN_v13 §11]
  ① Gemini 호출은 이 3개 함수로만 제한
     (_analyze_market_env / _analyze_materials / _pick_final)
  ② 텔레그램 발송 금지
  ③ DB 직접 기록 금지
  ④ KIS 직접 호출 금지
  ⑤ AI 모델: gemini-2.5-flash 만 (다른 모델 절대 금지)
  ⑥ SDK: google-genai 만 (google-generativeai 금지)

[변경 이력]
  v12.0 Step 6: 신규 생성 (geopolitics/theme/oracle/event_impact 흡수)
  v12.0 Step 8: signal_analyzer → data_collector 이전
  v13.0 Step 7: 기존 구조 전면 폐기, 3단계 Gemini 구조로 재설계
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from utils.logger import logger
import config

KST = timezone(timedelta(hours=9))

# ── Gemini API 초기화 ────────────────────────────────────────
_GEMINI_MODEL = "gemini-2.5-flash"   # §11 ⑤ 고정

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

async def analyze(cache: dict) -> dict:
    """
    아침봇 전체 분석 통합 실행 (morning_report.py 가 이것만 호출).

    Args:
        cache: data_collector.get_cache() 반환값.
               개편 후 유효 키:
                 dart_data, market_data, news_naver, news_newsapi,
                 news_global_rss, price_data, sector_etf_data,
                 short_data, event_calendar,
                 closing_strength_result, volume_surge_result,
                 fund_concentration_result

    Returns:
        {
            "market_env":  dict,   # 호출① — 시장환경 판단 결과
            "candidates":  dict,   # 호출② — 후보종목 + 유효재료
            "picks":       list,   # 호출③ — 최종 픽 15종목
        }
    """
    import asyncio

    result: dict = {
        "market_env": {},
        "candidates": {"후보종목": [], "제외근거": ""},
        "picks":      [],
    }

    if not _CLIENT:
        logger.error("[morning_analyzer] Gemini 클라이언트 없음 — 분석 중단")
        return result

    loop = asyncio.get_running_loop()  # [BUG-07] deprecated fix

    # ── 호출① 시장환경 판단 ──────────────────────────────────
    try:
        market_env = await loop.run_in_executor(
            None, _analyze_market_env, cache.get("market_data", {})
        )
        result["market_env"] = market_env
        logger.info(
            f"[morning_analyzer] ①시장환경: {market_env.get('환경','?')} "
            f"/ 테마후보: {market_env.get('주도테마후보', [])}"
        )
    except Exception as e:
        logger.error(f"[morning_analyzer] ①시장환경 분석 실패: {e}")
        return result

    # ── 호출② 재료 검증 + 후보 압축 ────────────────────────
    try:
        candidates = await loop.run_in_executor(
            None, _analyze_materials, cache, result["market_env"]
        )
        result["candidates"] = candidates
        n_cand = len(candidates.get("후보종목", []))
        logger.info(f"[morning_analyzer] ②후보종목 {n_cand}개 선별 완료")
    except Exception as e:
        logger.error(f"[morning_analyzer] ②재료검증 분석 실패: {e}")
        return result

    # ── 호출③ 최종 픽 15종목 (RAG 포함) ────────────────────
    try:
        picks_result = await loop.run_in_executor(
            None, _pick_final, cache, result["candidates"]
        )
        result["picks"] = picks_result.get("picks", [])
        logger.info(f"[morning_analyzer] ③최종 픽 {len(result['picks'])}종목 완료")
    except Exception as e:
        logger.error(f"[morning_analyzer] ③최종픽 분석 실패: {e}")

    return result


# ══════════════════════════════════════════════════════════════
# 호출① — 시장환경 판단
# ══════════════════════════════════════════════════════════════

def _analyze_market_env(market_data: dict) -> dict:
    """
    미국 섹터ETF 등락(±2%+) + 원자재 + 환율 → 시장환경 판단.

    Args:
        market_data: data_collector 캐시의 "market_data" 키 값.
                     예상 구조:
                       {
                         "us_sectors":   list[dict] | dict,   # ±2%+ 필터 적용
                         "commodities":  dict,
                         "forex":        dict,
                       }

    Returns:
        {
            "환경":         "리스크온" | "리스크오프" | "중립",
            "주도테마후보": list[str],
            "한국시장영향": str,
        }
    """
    # [v13.0 버그수정] market_global.collect() 반환 구조:
    #   {"us_market": {"sectors": {...}, "nasdaq":..., "summary":...}, "commodities": {...}}
    # "us_sectors" 키는 존재하지 않음 → us_market["sectors"] 경유로 수정
    us_market   = market_data.get("us_market", {})
    us_sectors  = us_market.get("sectors", {})
    commodities = market_data.get("commodities", {})
    forex       = market_data.get("forex", {})   # market_global 미수집 — 빈 dict graceful degradation

    prompt = f"""오늘 한국 주식시장 환경을 판단해라.

[미국 섹터 ETF 등락 (±2%+ 필터 적용, 없으면 빈 목록)]
{json.dumps(us_sectors, ensure_ascii=False, indent=2)}

[원자재]
{json.dumps(commodities, ensure_ascii=False, indent=2)}

[환율]
{json.dumps(forex, ensure_ascii=False, indent=2)}

판단 규칙:
- 기술/반도체 ETF +2%+ → 리스크온 + 반도체 테마 가중
- 에너지 ETF +2%+ → 정유/에너지 테마
- 국채금리 급등 → 리스크오프
- 달러 강세(원화 약세) → 수출주 유리
- 원자재 급등(원유/구리) → 관련 소재/에너지 테마

다음을 JSON으로만 반환 (다른 텍스트 없음):
{{
  "환경": "리스크온" | "리스크오프" | "중립",
  "주도테마후보": ["테마명1", "테마명2"],
  "한국시장영향": "한 문장 요약 (50자 이내)"
}}"""

    raw  = _call_gemini(prompt)
    data = _extract_json(raw)

    if isinstance(data, dict):
        return data

    logger.warning(f"[morning_analyzer] ①시장환경 JSON 파싱 실패, 원문: {raw[:120]}")
    return {"환경": "중립", "주도테마후보": [], "한국시장영향": "데이터 부족"}


# ══════════════════════════════════════════════════════════════
# 호출② — 재료 검증 + 후보 압축
# ══════════════════════════════════════════════════════════════

def _analyze_materials(cache: dict, market_env: dict) -> dict:
    """
    DART 본문 + 뉴스 + 가격데이터 + 호출①결과 → 후보 20종목 이내.

    Args:
        cache:      data_collector.get_cache() 전체.
        market_env: _analyze_market_env() 반환값.

    Returns:
        {
            "후보종목": [
                {
                    "종목명":     str,
                    "종목코드":   str,
                    "근거":       str,
                    "재료강도":   "상" | "중" | "하",
                    "유형":       "공시" | "테마" | "순환매" | "숏스퀴즈",
                },
                ...
            ],
            "제외근거": str,   # 제외된 종목 패턴 요약
        }
    """
    dart_data  = cache.get("dart_data",  []) or []
    news_naver = cache.get("news_naver", {}) or {}
    news_api   = cache.get("news_newsapi", {}) or {}
    price_data = cache.get("price_data") or {}

    upper_limit = price_data.get("upper_limit", []) if isinstance(price_data, dict) else []
    top_gainers = price_data.get("top_gainers", []) if isinstance(price_data, dict) else []

    # 뉴스 통합 (타입 대응: dict or list)
    news_naver_list  = _flatten_news(news_naver)
    news_api_list    = _flatten_news(news_api)

    prompt = f"""목표: 오늘 한국 주식 중 당일 20% 이상 또는 상한가 달성 가능한 종목 발굴.

[시장환경 — 호출① 결과]
{json.dumps(market_env, ensure_ascii=False, indent=2)}

[DART 공시 (본문 포함, 최대 20건)]
{json.dumps(dart_data[:20], ensure_ascii=False, indent=2)}

[전날 상한가 종목 (시총 3000억 이하)]
{json.dumps(upper_limit[:15], ensure_ascii=False, indent=2)}

[전날 15%+ 급등 종목 (시총 3000억 이하)]
{json.dumps(top_gainers[:15], ensure_ascii=False, indent=2)}

[주요 뉴스 — 네이버 (최대 15건)]
{json.dumps(news_naver_list[:15], ensure_ascii=False, indent=2)}

[주요 뉴스 — NewsAPI (최대 10건)]
{json.dumps(news_api_list[:10], ensure_ascii=False, indent=2)}

판단 기준:
- 소형주(시총 3000억 이하) 우선: 20%+ 달성 확률 높음
- DART 공시: 자기자본대비 비율, 실적 영향 직접 계산 → 재료강도 판단
  예) 자기자본대비 20%+ 수주 → 강재료(상), 10~20% → 중재료, 10% 미만 → 하재료
- 순환매: 전날 대장주 상한가 → 오늘 같은 테마 2등주 흐름
- 테마: 호출① 주도테마후보와 연결된 종목 우선
- 숏스퀴즈: 공매도 잔고 높은데 호재 발생

다음을 JSON으로만 반환 (다른 텍스트 없음):
{{
  "후보종목": [
    {{
      "종목명":   "종목명",
      "종목코드": "6자리코드 또는 빈문자열",
      "근거":     "구체적 근거 (50자 이내)",
      "재료강도": "상" | "중" | "하",
      "유형":     "공시" | "테마" | "순환매" | "숏스퀴즈"
    }}
  ],
  "제외근거": "제외된 종목 패턴 요약 (30자 이내)"
}}
최대 20종목. 재료강도 "상" 우선 정렬."""

    raw  = _call_gemini(prompt)
    data = _extract_json(raw)

    if isinstance(data, dict) and "후보종목" in data:
        # ── cap_tier 주입 (price_data 시가총액 기반) ─────────
        price_data = cache.get("price_data") or {}
        by_code: dict = price_data.get("by_code", {}) if isinstance(price_data, dict) else {}
        by_name: dict = price_data.get("by_name", {}) if isinstance(price_data, dict) else {}

        for stock in data.get("후보종목", []):
            code = stock.get("종목코드", "")
            name = stock.get("종목명", "")
            entry = by_code.get(code) or by_name.get(name) or {}
            cap = entry.get("시가총액", 0) or 0
            stock["cap_tier"] = _infer_cap_tier_from_cap(cap)

        return data

    logger.warning(f"[morning_analyzer] ②재료검증 JSON 파싱 실패, 원문: {raw[:120]}")
    return {"후보종목": [], "제외근거": "파싱 실패"}


# ══════════════════════════════════════════════════════════════
# 호출③ — 최종 픽 15종목 (RAG 포함)
# ══════════════════════════════════════════════════════════════

def _pick_final(cache: dict, candidates: dict) -> dict:
    """
    자금집중 + 공매도 + RAG 과거패턴 + 호출②결과 → 최종 픽 15종목.

    Args:
        cache:      data_collector.get_cache() 전체.
        candidates: _analyze_materials() 반환값.

    Returns:
        {
            "picks": [
                {
                    "순위":         int,        # 1~15 (1=매수 최우선)
                    "종목명":       str,
                    "종목코드":     str,
                    "근거":         str,
                    "유형":         str,        # "공시"/"테마"/"순환매"/"숏스퀴즈"
                    "목표등락률":   str,        # "20%" 또는 "상한가"
                    "손절기준":     str,        # 예: "전일 저가 하향 시"
                    "테마여부":     bool,
                    "매수시점":     str,        # 예: "시초가 매수" / "눌림목 9,200원대"
                },
                ...
            ]
        }
    """
    fund_concentration = cache.get("fund_concentration_result", []) or []
    short_data         = cache.get("short_data", []) or []
    candidate_stocks   = candidates.get("후보종목", [])

    # ── RAG: 후보종목별 유사패턴 수집 ──────────────────────
    rag_context = _build_rag_context(candidate_stocks)

    prompt = f"""한국 주식 모닝봇 최종 픽 선정 전문가.
목표: 당일 20%+ 또는 상한가 달성 가능한 최상위 종목 15개 선정.

[후보종목 — 호출② 결과 (최대 20종목)]
{json.dumps(candidate_stocks, ensure_ascii=False, indent=2)}

[자금집중 상위 20종목 (거래대금/시총 비율 높은 순)]
{json.dumps(fund_concentration[:20], ensure_ascii=False, indent=2)}

[공매도 잔고 상위 20종목]
{json.dumps(short_data[:20], ensure_ascii=False, indent=2)}

[RAG: 과거 유사패턴 및 실제 결과]
{rag_context if rag_context else "아직 축적된 패턴 데이터 없음"}

최종 선정 기준 (우선순위):
1. 재료강도 "상" + 자금집중 겹치는 종목 최우선
2. RAG에서 같은 신호유형 20%+ 성공률 높은 패턴 우대
3. 공매도 잔고 높은 종목에 호재 → 숏스퀴즈 가능성 추가 고려
4. 테마 종목은 같은 테마 내 2~3종목 이내로 분산
5. 재료 없는 단순 거래량 급증 → 낮은 순위

다음을 JSON으로만 반환 (다른 텍스트 없음):
{{
  "picks": [
    {{
      "순위":       1,
      "종목명":     "종목명",
      "종목코드":   "6자리코드 또는 빈문자열",
      "근거":       "구체적 근거 (60자 이내)",
      "유형":       "공시" | "테마" | "순환매" | "숏스퀴즈",
      "목표등락률": "20%" | "상한가",
      "손절기준":   "손절 조건 (30자 이내)",
      "테마여부":   true | false,
      "매수시점":   "매수 타이밍 (20자 이내)"
    }}
  ]
}}
1위부터 매수 우선순위 순. 최대 15종목."""

    raw  = _call_gemini(prompt, max_tokens=2500)
    data = _extract_json(raw)

    if isinstance(data, dict) and "picks" in data:
        picks = data["picks"]
        # ── [BUG-03 수정] cap_tier 역매핑 ──────────────────────────
        # Gemini 출력 JSON 스키마에 cap_tier 없음 → candidates에서 주입
        cap_map      = {s.get("종목명",   ""): s.get("cap_tier", "미분류") for s in candidate_stocks}
        code_cap_map = {s.get("종목코드", ""): s.get("cap_tier", "미분류") for s in candidate_stocks}
        for p in picks:
            p["cap_tier"] = (
                cap_map.get(p.get("종목명",   ""))
                or code_cap_map.get(p.get("종목코드", ""))
                or "미분류"
            )
        # 순위 정규화
        for i, p in enumerate(picks, 1):
            p.setdefault("순위", i)
        final_picks = picks[:15]

        # ── daily_picks DB 저장 ────────────────────────────
        _save_daily_picks(final_picks)

        return {"picks": final_picks}

    logger.warning(f"[morning_analyzer] ③최종픽 JSON 파싱 실패, 원문: {raw[:120]}")
    return {"picks": []}


# ══════════════════════════════════════════════════════════════
# RAG 헬퍼
# ══════════════════════════════════════════════════════════════

def _save_daily_picks(picks: list[dict]) -> None:
    """
    [v13.0] _pick_final() 완료 직후 daily_picks 테이블에 당일 픽 저장.
    performance_tracker._save_rag_patterns_after_batch() 에서 이 데이터를 읽어 rag_save() 호출.

    §11 규칙: 이 함수는 _pick_final() 내부에서만 호출.
    """
    if not picks:
        return
    try:
        import tracking.db_schema as db_schema
        today_str = datetime.now(KST).strftime("%Y%m%d")
        created_at = datetime.now(KST).isoformat()
        rows = []
        for p in picks:
            rows.append((
                today_str,
                p.get("순위"),
                p.get("종목코드", ""),
                p.get("종목명", ""),
                _map_type_to_signal(p.get("유형", "미분류")),  # [BUG-10] 변환 후 저장
                p.get("cap_tier", "미분류"),
                p.get("근거", ""),
                p.get("목표등락률", ""),
                p.get("손절기준", ""),
                created_at,
            ))
        conn = db_schema.get_conn()
        try:
            c = conn.cursor()
            # 당일 기존 픽 삭제 후 재삽입 (08:30 재실행 대비)
            c.execute("DELETE FROM daily_picks WHERE date = ?", (today_str,))
            c.executemany("""
                INSERT INTO daily_picks
                    (date, rank, stock_code, stock_name, signal_type, cap_tier,
                     reason, target_rate, stop_loss, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            conn.commit()
            logger.info(f"[morning_analyzer] daily_picks 저장 완료 — {len(rows)}건")
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[morning_analyzer] daily_picks 저장 실패 (비치명적): {e}")

def _build_rag_context(candidate_stocks: list[dict]) -> str:
    """
    후보종목 목록에서 signal_type / cap_tier 를 추출해
    rag_pattern_db.get_similar_patterns() 를 호출하고
    결과 텍스트를 합쳐 반환.

    §10 규칙: rag_pattern_db.get_similar_patterns()는 _pick_final() 내부에서만 호출.
    실제로는 이 함수가 _pick_final() 안에서 사용되므로 규칙 준수.
    """
    try:
        from tracking import rag_pattern_db
    except ImportError:
        logger.warning("[morning_analyzer] rag_pattern_db 임포트 실패 — RAG 건너뜀")
        return ""

    if not candidate_stocks:
        return ""

    # 유형별로 대표 cap_tier 추정 (price_data 없으면 "미분류" 사용)
    seen: set[tuple[str, str]] = set()
    rag_lines: list[str] = []

    for stock in candidate_stocks:
        signal_type = _map_type_to_signal(stock.get("유형", "미분류"))
        cap_tier    = stock.get("cap_tier", "미분류")

        key = (signal_type, cap_tier)
        if key in seen:
            continue
        seen.add(key)

        text = rag_pattern_db.get_similar_patterns(signal_type, cap_tier)
        if text:
            rag_lines.append(text)

    return "\n\n".join(rag_lines)


def _infer_cap_tier_from_cap(cap: int) -> str:
    """
    [BUG-02 수정] 시가총액(원) → cap_tier 문자열 변환.
    rag_pattern_db._infer_cap_tier() 와 명칭 완전 통일.
    기존: 소형_극소 / 소형 / 중형이상  (rag_pattern_db와 불일치 → RAG 영구 빈 결과)
    수정: 소형_300억미만 / 소형_1000억미만 / 소형_3000억미만 / 중형  (통일)
    """
    if not cap or cap <= 0:
        return "미분류"
    if cap < 30_000_000_000:           # 300억 미만
        return "소형_300억미만"
    elif cap < 100_000_000_000:         # 1000억 미만
        return "소형_1000억미만"
    elif cap < 300_000_000_000:         # 3000억 미만
        return "소형_3000억미만"
    else:
        return "중형"


def _map_type_to_signal(유형: str) -> str:
    """후보종목 '유형' → rag_pattern_db signal_type 변환."""
    mapping = {
        "공시":    "DART_공시",
        "테마":    "테마",
        "순환매":  "순환매",
        "숏스퀴즈": "숏스퀴즈",
    }
    return mapping.get(유형, 유형 or "미분류")


# ══════════════════════════════════════════════════════════════
# 공통 유틸
# ══════════════════════════════════════════════════════════════

def _call_gemini(prompt: str, max_tokens: int = 1500) -> str:
    """Gemini 2.5 Flash API 호출 (§11 ⑤ — 이 모델만)."""
    if not _CLIENT:
        raise RuntimeError("[morning_analyzer] Gemini 클라이언트 미초기화")
    response = _CLIENT.models.generate_content(
        model    = _GEMINI_MODEL,
        contents = prompt,
        config   = _genai_types.GenerateContentConfig(
            temperature       = 0.2,
            max_output_tokens = max_tokens,
        ),
    )
    return response.text


def _extract_json(raw: str):
    """
    [BUG-09 수정] AI 응답에서 JSON 추출 (마크다운 펜스 제거 포함).
    기존: r"\\{[^{}]+\\}" 패턴 → 중첩 {} 있으면 매칭 실패
    수정: json.loads 실패 시 끝에서부터 한 문자씩 잘라내며 재시도 (후위 잘림 대응)
    """
    cleaned = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
    match   = re.search(r"[\[{]", cleaned)
    if not match:
        raise ValueError(f"JSON 없음: {cleaned[:80]}")
    json_str = cleaned[match.start():]
    # 닫는 괄호 탐색
    end = json_str.rfind("]") if json_str.startswith("[") else json_str.rfind("}")
    if end != -1:
        json_str = json_str[:end + 1]
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        # 후위 잘림 대응: 마지막 완전한 JSON 경계까지 잘라내며 재시도
        for i in range(len(json_str) - 1, -1, -1):
            if json_str[i] not in ('}', ']'):
                continue
            try:
                return json.loads(json_str[:i + 1])
            except json.JSONDecodeError:
                continue
        raise


def _flatten_news(news: dict | list) -> list:
    """
    뉴스 데이터가 dict(카테고리별) 또는 list 형태 모두 대응.
    list[dict] 형태로 평탄화해 반환.
    """
    if isinstance(news, list):
        return news
    if isinstance(news, dict):
        out = []
        for v in news.values():
            if isinstance(v, list):
                out.extend(v)
            elif isinstance(v, dict):
                out.append(v)
        return out
    return []
