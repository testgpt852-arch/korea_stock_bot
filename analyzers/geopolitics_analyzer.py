"""
analyzers/geopolitics_analyzer.py
지정학 이벤트 → 영향 섹터 맵핑 + AI 분석 전담

[ARCHITECTURE rule #91 — 절대 금지]
- KIS API 호출 금지
- pykrx 호출 금지
- 텔레그램 발송 금지
- DB 기록 금지
- 입력 파라미터 + geopolitics_map 사전 + AI만 사용

[rule #94 준수]
- 분석 결과는 signal_analyzer.analyze()의 geopolitics_data 파라미터로 전달
- oracle_analyzer에 직접 전달 금지 — signal_analyzer 경유 필수

[v10.0 Phase 2 신규]
입력: geopolitics_collector.collect() 반환값 (raw 뉴스 목록)
출력: signal_analyzer.analyze()의 geopolitics_data 파라미터 형식
  list[dict] = [
    {
      event_type:        str,   # 이벤트 유형 (geopolitics_map.py key)
      affected_sectors:  list,  # 영향 섹터 리스트
      impact_direction:  str,   # '+' 또는 '-' 또는 'mixed'
      confidence:        float, # 0~1 최종 신뢰도
      source_url:        str,   # 기사 URL
      event_summary_kr:  str,   # 한국어 요약 (AI 생성 또는 제목 fallback)
    }
  ]

[처리 순서]
1. geopolitics_map.lookup() 로 패턴 매칭 (규칙 사전 우선)
2. 매칭된 이벤트를 배치로 묶어 Gemini에 확인 요청 (AI 보완)
3. 신뢰도 미달 항목 필터링 후 반환
"""

import json
import config
from utils.logger import logger
from utils.geopolitics_map import lookup as map_lookup


def analyze(raw_news: list[dict]) -> list[dict]:
    """
    raw 뉴스 목록 → 지정학 이벤트 분석 결과 반환.

    rule #91: geopolitics_map 사전 + AI만 사용. KIS/pykrx 호출 없음.
    rule #94: 반환값은 signal_analyzer.geopolitics_data 파라미터로 전달.

    Args:
        raw_news: geopolitics_collector.collect() 반환값

    Returns:
        list[dict] — 신뢰도 필터링된 이벤트 분석 결과
    """
    if not raw_news:
        return []

    results: list[dict] = []

    # Step 1: geopolitics_map 사전 기반 패턴 매칭
    map_matched = _match_with_map(raw_news)
    logger.info(f"[geopolitics_analyzer] 사전 매칭: {len(map_matched)}건")

    # Step 2: AI 배치 분석 (선택적 — AI 다운 시 사전 결과만 사용)
    if config.GOOGLE_AI_API_KEY and map_matched:
        try:
            ai_enhanced = _enhance_with_ai(map_matched, raw_news)
            results = ai_enhanced
            logger.info(f"[geopolitics_analyzer] AI 보완 완료: {len(results)}건")
        except Exception as e:
            logger.warning(f"[geopolitics_analyzer] AI 분석 실패 — 사전 결과 사용: {e}")
            results = map_matched
    else:
        results = map_matched

    # Step 3: 신뢰도 필터링
    min_conf = config.GEOPOLITICS_CONFIDENCE_MIN
    filtered = [r for r in results if r.get("confidence", 0) >= min_conf]

    # 신뢰도 내림차순 정렬
    filtered.sort(key=lambda x: x.get("confidence", 0), reverse=True)

    logger.info(
        f"[geopolitics_analyzer] 최종 {len(filtered)}건 "
        f"(신뢰도≥{min_conf} 필터 후)"
    )
    return filtered


def _match_with_map(raw_news: list[dict]) -> list[dict]:
    """
    geopolitics_map.lookup()으로 패턴 매칭.
    동일 이벤트 키가 여러 기사에서 감지되면 신뢰도 상향 (corroboration).
    """
    # key → (entry, matched_articles) 집계
    event_aggregator: dict[str, dict] = {}

    for article in raw_news:
        raw_text = article.get("raw_text", "")
        title    = article.get("title", "")
        matches  = map_lookup(raw_text + " " + title)

        for match in matches:
            key = match["key"]
            if key not in event_aggregator:
                event_aggregator[key] = {
                    "map_entry":  match,
                    "articles":   [],
                    "hit_count":  0,
                }
            event_aggregator[key]["articles"].append(article)
            event_aggregator[key]["hit_count"] += 1

    results = []
    for key, agg in event_aggregator.items():
        entry      = agg["map_entry"]
        hit_count  = agg["hit_count"]
        base_conf  = entry.get("confidence_base", 0.6)

        # 복수 기사에서 감지 시 신뢰도 상향 (최대 0.95)
        confidence = min(base_conf + (hit_count - 1) * 0.05, 0.95)

        # 대표 기사 (가장 최근)
        articles = sorted(
            agg["articles"],
            key=lambda a: a.get("published", ""),
            reverse=True,
        )
        rep_article = articles[0] if articles else {}

        results.append({
            "event_type":       key,
            "affected_sectors": entry.get("sectors", []),
            "impact_direction": entry.get("impact", "+"),
            "confidence":       round(confidence, 3),
            "source_url":       rep_article.get("link", ""),
            "event_summary_kr": rep_article.get("title", entry.get("description", key)),
            "_hit_count":       hit_count,   # 내부용 (signal_analyzer에 불필요)
        })

    return results


def _enhance_with_ai(
    map_results: list[dict],
    raw_news: list[dict],
) -> list[dict]:
    """
    Gemini Flash로 배치 분석 — 사전 매칭 결과를 교차 검증하고 누락 이벤트 보완.

    rule #91: AI 호출만. KIS/pykrx/텔레그램/DB 없음.

    배치 처리: 최대 10건을 하나의 프롬프트로 처리 (AI 호출 횟수 최소화).

    [v10.3 모델 정책] geopolitics_analyzer 전용:
      Primary  : gemini-3-flash-preview  (Google 현행 지원 모델)
      Fallback : gemini-2.5-flash        (Primary 실패 시 자동 전환)
      ※ 절대 사용 금지 (Google 서비스 종료):
        gemini-1.5-flash / gemini-1.5-flash-002 / gemini-1.5-pro
        gemini-2.0-flash / gemini-2.0-flash-lite / gemini-2.0-flash-exp
    """
    import google.generativeai as genai

    genai.configure(api_key=config.GOOGLE_AI_API_KEY)

    # 뉴스 요약 생성 (최대 10건)
    news_texts = []
    for i, article in enumerate(raw_news[:10]):
        news_texts.append(f"{i+1}. [{article.get('source','')}] {article.get('title','')}")
    news_block = "\n".join(news_texts)

    # 기존 사전 매칭 요약
    matched_keys = [r["event_type"] for r in map_results]

    prompt = f"""당신은 한국 주식 시장 전문가입니다.
아래 뉴스 목록을 분석하여 한국 주식 시장에 영향을 줄 지정학·정책 이벤트를 식별하세요.

[뉴스 목록]
{news_block}

[이미 감지된 이벤트]
{matched_keys}

다음 형식의 JSON 배열만 출력하세요 (다른 텍스트 없음):
[
  {{
    "event_type": "이벤트 유형 (한국어)",
    "affected_sectors": ["영향 섹터1", "영향 섹터2"],
    "impact_direction": "+" 또는 "-" 또는 "mixed",
    "confidence": 0.0~1.0,
    "event_summary_kr": "50자 이내 한국어 요약"
  }}
]

규칙:
- 이미 감지된 이벤트는 신뢰도만 조정하여 포함
- 새로 발견된 이벤트도 추가
- 한국 주식 시장과 무관한 이벤트는 제외
- 섹터명은 다음 중에서 선택: 철강/비철금속, 산업재/방산, 기술/반도체, 에너지/정유, 소재/화학, 바이오/헬스케어, 금융, 조선, 배터리, 자동차부품
"""

    # [v10.3] Primary: gemini-3-flash-preview → Fallback: gemini-2.5-flash
    # gemini-2.0-flash / gemini-1.5-flash 계열 전부 서비스 종료 — 사용 금지
    _MODELS = ["gemini-3-flash-preview", "gemini-2.5-flash"]

    for model_name in _MODELS:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(prompt)
            text = response.text.strip()

            # JSON 추출 (```json ... ``` 제거)
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]

            ai_results = json.loads(text.strip())

            if model_name != _MODELS[0]:
                logger.info(f"[geopolitics_analyzer] fallback 모델 사용: {model_name}")
            else:
                logger.info(f"[geopolitics_analyzer] AI 분석 완료 ({model_name})")

            # AI 결과와 사전 결과 병합
            return _merge_results(map_results, ai_results)

        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"[geopolitics_analyzer] {model_name} 실패: {e}")

    logger.warning("[geopolitics_analyzer] 모든 AI 모델 실패 — 사전 결과만 사용")
    return map_results


def _merge_results(
    map_results: list[dict],
    ai_results: list,
) -> list[dict]:
    """
    사전 매칭 결과 + AI 결과 병합.
    동일 이벤트 타입이면 신뢰도 평균, 없는 이벤트면 추가.
    """
    merged_map: dict[str, dict] = {r["event_type"]: r for r in map_results}

    for ai in ai_results:
        if not isinstance(ai, dict):
            continue
        event_type = ai.get("event_type", "")
        if not event_type:
            continue

        if event_type in merged_map:
            # 기존 항목 신뢰도 보정 (사전:AI = 6:4 가중 평균)
            existing = merged_map[event_type]
            new_conf = round(
                existing["confidence"] * 0.6 + float(ai.get("confidence", 0)) * 0.4,
                3,
            )
            existing["confidence"] = min(new_conf, 0.95)
        else:
            # AI 신규 발견 이벤트 추가
            merged_map[event_type] = {
                "event_type":       event_type,
                "affected_sectors": ai.get("affected_sectors", []),
                "impact_direction": ai.get("impact_direction", "+"),
                "confidence":       round(float(ai.get("confidence", 0.5)), 3),
                "source_url":       "",
                "event_summary_kr": ai.get("event_summary_kr", event_type),
            }

    return list(merged_map.values())
