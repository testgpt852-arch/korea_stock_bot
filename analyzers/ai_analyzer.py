"""
analyzers/ai_analyzer.py
Google AI (Gemma-3-27b-it) 2차 분석 전담 (5단계)

[모델]
- gemma-3-27b-it: 하루 14,400회 무료 (하루 실제 사용량 ~82회)
- 발급: https://aistudio.google.com → Get API Key

[수정이력]
- v2.0: google.generativeai(deprecated) → google.genai(신패키지) 교체
- v2.1: 대장주 선정 버그 수정 — 등락률 1위를 관련종목[0]으로 프롬프트 강화
        테마 그룹핑 정확도 개선 프롬프트 추가
- v3.5: analyze_spike() ai_context 파라미터 추가 (Phase 5 AI 학습 피드백)
        트리거 승률 / 종목 이력 / 매매 원칙을 프롬프트에 주입해 판단 정확도 향상

[ARCHITECTURE 의존성]
ai_analyzer → morning_report, closing_report, realtime_alert
수집/발송 로직 없음 — 분석 결과 dict 반환만
"""

import json
import re
from utils.logger import logger
import config

# ── Google AI SDK 초기화 (신패키지: google-genai) ────────────
try:
    from google import genai
    from google.genai import types

    if config.GOOGLE_AI_API_KEY:
        _CLIENT = genai.Client(api_key=config.GOOGLE_AI_API_KEY)
        _MODEL  = "gemma-3-27b-it"
        logger.info("[ai] Google AI (gemma-3-27b-it) 초기화 완료")
    else:
        _CLIENT = None
        _MODEL  = None
        logger.warning("[ai] GOOGLE_AI_API_KEY 없음 — AI 분석 비활성")

except ImportError:
    _CLIENT = None
    _MODEL  = None
    logger.warning("[ai] google-genai 패키지 없음 — pip install google-genai")


# ══════════════════════════════════════════════════════════════
# ① analyze_dart — 아침봇: 공시 호재/악재 판단
# ══════════════════════════════════════════════════════════════

def analyze_dart(dart_list: list[dict]) -> list[dict]:
    """
    DART 공시 리스트 → 호재/악재 점수화

    반환: list[dict]
    [{"종목명": str, "점수": int(1~10), "이유": str, "상한가확률": str}]
    """
    if not _CLIENT or not dart_list:
        return []

    top = dart_list[:5]
    prompt = _build_dart_prompt(top)

    try:
        raw = _call_api(prompt)
        return _parse_dart_result(raw, top)
    except Exception as e:
        logger.warning(f"[ai] analyze_dart 실패: {e}")
        return []


def _build_dart_prompt(dart_list: list[dict]) -> str:
    items = "\n".join(
        f"{i+1}. [{d['종목명']}] {d['공시종류']} — {d['공시시각']}"
        for i, d in enumerate(dart_list)
    )
    return f"""한국 주식 공시 분석 전문가다. 다음 공시들을 분석하라.

공시 목록:
{items}

JSON 배열만 출력. 다른 텍스트 없이:
[
  {{"번호": 1, "점수": 8, "이유": "대규모 수주로 매출 성장 기대", "상한가확률": "높음"}},
  {{"번호": 2, "점수": 4, "이유": "배당 결정, 단기 수급 긍정", "상한가확률": "낮음"}}
]

규칙:
- 점수: 1(강한악재)~10(강한호재)
- 상한가확률: 높음 또는 중간 또는 낮음
- 이유: 20자 이내"""


def _parse_dart_result(raw: str, dart_list: list[dict]) -> list[dict]:
    try:
        data = _extract_json(raw)
        if not isinstance(data, list):
            return []
        results = []
        for item in data:
            idx = int(item.get("번호", 1)) - 1
            if 0 <= idx < len(dart_list):
                results.append({
                    "종목명":     dart_list[idx]["종목명"],
                    "점수":       int(item.get("점수", 5)),
                    "이유":       item.get("이유", ""),
                    "상한가확률": item.get("상한가확률", "낮음"),
                })
        return results
    except Exception as e:
        logger.warning(f"[ai] dart 파싱 실패: {e} | raw={raw[:80]}")
        return []


# ══════════════════════════════════════════════════════════════
# ② analyze_spike — 장중봇: 급등 진짜/작전 판단
# ══════════════════════════════════════════════════════════════

def analyze_spike(
    analysis: dict,
    news_context: str = "",
    ai_context: str = "",
) -> dict:
    """
    급등 종목 → 진짜급등 / 작전주 판단

    Args:
        analysis:     급등 분석 데이터 dict (종목명, 등락률, 거래량배율 등)
        news_context: 관련 뉴스 텍스트 (선택)
        ai_context:   [v3.5 Phase 5] DB 기반 컨텍스트 문자열
                      tracking/ai_context.build_spike_context() 반환값.
                      트리거 승률 / 종목 이력 / 매매 원칙이 담긴다.
                      빈 문자열이면 기존 방식대로 판단.

    반환: dict {"판단": str, "이유": str}
    판단: "진짜급등" | "작전주의심" | "판단불가"
    """
    if not _CLIENT:
        return {"판단": "판단불가", "이유": "AI 미설정"}

    news_line    = f"\n관련뉴스: {news_context}" if news_context else ""
    context_line = f"\n\n[과거 데이터 참고]\n{ai_context}" if ai_context else ""

    prompt = f"""한국 주식 급등 분석 전문가다.

종목: {analysis.get('종목명','N/A')} ({analysis.get('종목코드','N/A')})
등락률: +{analysis.get('등락률',0):.1f}%
거래량: 전일 대비 {analysis.get('거래량배율',0):.1f}배
감지시각: {analysis.get('감지시각','N/A')}{news_line}{context_line}

진짜급등: 실적/공시/리포트 근거, 거래량 자연스러운 증가
작전주의심: 이유 없음, 거래량 폭발적 단기 급등
판단불가: 정보 부족

과거 데이터가 있다면 트리거 승률, 종목 이력, 매매 원칙을 판단에 참고하라.

JSON만 출력:
{{"판단": "진짜급등", "이유": "20자 이내 이유"}}"""

    try:
        raw  = _call_api(prompt)
        data = _extract_json(raw)
        if not isinstance(data, dict):
            return {"판단": "판단불가", "이유": "파싱 실패"}
        판단 = data.get("판단", "판단불가")
        if 판단 not in ("진짜급등", "작전주의심", "판단불가"):
            판단 = "판단불가"
        return {"판단": 판단, "이유": data.get("이유", "")}
    except Exception as e:
        logger.warning(f"[ai] analyze_spike 실패: {e}")
        return {"판단": "판단불가", "이유": str(e)[:30]}


# ══════════════════════════════════════════════════════════════
# ③ analyze_closing — 마감봇: 테마 그룹핑 + 소외주 식별
# ══════════════════════════════════════════════════════════════

def analyze_closing(price_result: dict) -> list[dict]:
    """
    마감봇 핵심: 상한가+급등 → 테마별 그룹핑 + 소외주 식별
    v6.0 프롬프트 웹작업 #5·#6 자동화

    반환: list[dict] — signal_result["signals"] 형식
    """
    if not _CLIENT:
        logger.warning("[ai] GOOGLE_AI_API_KEY 없음 — 테마 분석 건너뜀")
        return []

    upper      = price_result.get("upper_limit", [])
    gainers    = price_result.get("top_gainers", [])
    all_stocks = price_result.get("by_name", {})

    if not upper and not gainers:
        return []

    # 5%↑ 종목 + 등락률 정보 포함 (대장주 선정에 사용)
    all_movers = {
        name: info["등락률"]
        for name, info in all_stocks.items()
        if isinstance(info.get("등락률"), float) and info["등락률"] >= 5.0
    }

    prompt = _build_closing_prompt(upper, gainers, all_movers)

    try:
        raw    = _call_api(prompt)
        parsed = _parse_closing_result(raw)
        logger.info(f"[ai] 테마 그룹핑 완료 — {len(parsed)}개 테마")
        return parsed
    except Exception as e:
        logger.warning(f"[ai] analyze_closing 실패: {e}")
        return []


def _build_closing_prompt(
    upper: list[dict],
    gainers: list[dict],
    all_movers: dict,
) -> str:
    upper_str = "\n".join(
        f"  - {s['종목명']} +{s['등락률']:.1f}% ({s['시장']})"
        for s in upper[:15]
    )
    gainers_str = "\n".join(
        f"  - {s['종목명']} +{s['등락률']:.1f}% ({s['시장']})"
        for s in gainers[:15]
    )
    # 등락률 내림차순으로 정렬해서 넘김 (대장주 선정 힌트)
    movers_sorted = sorted(all_movers.items(), key=lambda x: -x[1])
    movers_str = "\n".join(
        f"  {name}: +{rate:.1f}%"
        for name, rate in movers_sorted[:50]
    )

    return f"""한국 주식시장 마감 데이터 분석 — 내일 순환매 지도 작성

=== 오늘 상한가 ===
{upper_str if upper_str else "없음"}

=== 오늘 급등(7%↑) ===
{gainers_str if gainers_str else "없음"}

=== 오늘 5%↑ 전체 (등락률 높은 순) ===
{movers_str if movers_str else "없음"}

**목표**: 같은 테마(섹터)끼리 묶고 대장주와 소외주를 식별하라.

**핵심 규칙**:
1. 테마명: 실제 시장 통용 명칭 (바이오신약, 전선구리, AI반도체, 방산, 2차전지 등)
2. 관련종목[0] = 반드시 해당 테마에서 등락률이 가장 높은 종목 (대장주)
3. 관련종목[1],[2]... = 같은 테마인데 등락률이 낮은 소외주
4. **소외주는 반드시 위 5%↑ 전체 목록에 있는 종목만** 포함할 것
5. 테마가 다른 종목끼리 억지로 묶지 말 것 (KEC는 반도체소켓, 서울바이오시스는 LED — 다른 테마)
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


def _parse_closing_result(raw: str) -> list[dict]:
    """AI 응답 → signal_result 형식"""
    data = _extract_json(raw)
    if not isinstance(data, list):
        return []

    signals = []
    for item in data:
        관련종목 = item.get("관련종목", [])
        if not 관련종목:
            continue
        강도 = max(1, min(5, int(item.get("강도", 3))))
        signals.append({
            "테마명":   item.get("테마명", "기타"),
            "발화신호": f"AI분석: {item.get('ai_memo', '')[:50]}",
            "강도":     강도,
            "신뢰도":   "AI(Gemma)",
            "발화단계": "오늘",
            "상태":     "신규",
            "관련종목": 관련종목,
            "ai_memo":  item.get("ai_memo", ""),
        })

    signals.sort(key=lambda x: x["강도"], reverse=True)
    return signals


# ══════════════════════════════════════════════════════════════
# 내부 유틸
# ══════════════════════════════════════════════════════════════

def _call_api(prompt: str) -> str:
    """Google AI API 호출 (google-genai 신패키지)"""
    response = _CLIENT.models.generate_content(
        model=_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.2,          # 낮을수록 일관된 JSON
            max_output_tokens=1500,
        ),
    )
    return response.text


def _extract_json(raw: str):
    """
    AI 응답에서 JSON 추출
    Gemma는 마크다운 펜스·설명을 붙이는 경향이 있어 robust하게 파싱
    """
    # 1) 마크다운 펜스 제거
    cleaned = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()

    # 2) 첫 번째 [ 또는 { 위치
    match = re.search(r"[\[{]", cleaned)
    if not match:
        raise ValueError(f"JSON 없음: {cleaned[:80]}")

    json_str = cleaned[match.start():]

    # 3) 대응하는 닫는 괄호 위치로 잘라내기
    if json_str.startswith("["):
        end = json_str.rfind("]")
    else:
        end = json_str.rfind("}")

    if end == -1:
        raise ValueError("JSON 종료 토큰 없음")

    json_str = json_str[:end + 1]

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.warning(f"[ai] JSON 파싱 실패 ({e}) — 객체 개별 추출 시도")
        return _recover_json(json_str)


def _recover_json(broken: str):
    """불완전 JSON 복구 — 완전한 객체만 추출"""
    results = []
    for m in re.finditer(r"\{[^{}]+\}", broken):
        try:
            results.append(json.loads(m.group()))
        except Exception:
            continue
    if results:
        return results
    raise ValueError("JSON 복구 실패")