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
- v4.2: [Phase 1 벤치마킹] 프롬프트 전면 강화
        analyze_spike():
          - 윌리엄 오닐 인격 + SYSTEM CONSTRAINTS 블록 추가
          - 손절 철칙(-7%) + 예외 조건 5개 명시
          - 강세장/약세장 분기 전략 (R/R 기준 차등 적용)
          - 장중(09:00~15:20) vs 마감 후(15:30+) 데이터 신뢰도 분기
          - market_env 파라미터 추가 (선택적, realtime_alert에서 주입)
          - JSON 응답 확장: target_price, stop_loss, risk_reward_ratio 필드 추가
        analyze_dart():
          - 장중/마감 후 시간 인식 추가
          - 공시 유형별 판단 가이드 강화
        _build_closing_prompt():
          - 장중/마감 후 컨텍스트 구분 추가

[ARCHITECTURE 의존성]
ai_analyzer → morning_report, closing_report, realtime_alert
수집/발송 로직 없음 — 분석 결과 dict 반환만

[절대 금지 — ARCHITECTURE #9]
이 파일에 수집(API 호출)·텔레그램 발송 로직 추가 금지
"""

import json
import re
from datetime import datetime, timezone, timedelta
from utils.logger import logger
import config

KST = timezone(timedelta(hours=9))

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
# 내부 유틸 — 시간 인식
# ══════════════════════════════════════════════════════════════

def _get_market_time_context() -> str:
    """
    현재 KST 시각 기준으로 데이터 신뢰도 컨텍스트 반환.

    장중(09:00~15:20): 당일 데이터는 미완성 → 전일 확정 데이터 우선
    마감 후(15:30+) : 당일 데이터 확정 → 모든 데이터 신뢰 가능

    [v4.2 신규] analyze_spike / analyze_dart 프롬프트에 공통 주입.
    """
    now = datetime.now(KST)
    market_open  = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=20, second=0, microsecond=0)

    if market_open <= now <= market_close:
        return (
            f"현재 시각: {now.strftime('%H:%M')} KST (장중)\n"
            "⚠️ 장중 데이터 주의: 오늘 거래량·캔들은 미완성 형성 중.\n"
            "  - '오늘 거래량 급감', '오늘 캔들 음봉' 등 당일 확정 판단 금지\n"
            "  - 전일 또는 최근 확정 데이터를 기준으로 분석할 것\n"
            "  - 당일 급등/급락은 '진행 중인 흐름'으로만 참고"
        )
    else:
        return (
            f"현재 시각: {now.strftime('%H:%M')} KST (마감 후)\n"
            "✅ 당일 데이터 확정: 거래량·캔들·등락률 모두 신뢰 가능.\n"
            "  - 오늘 종가·거래량·수급 데이터를 적극 분석에 활용"
        )


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

    # [v4.2] 시간 인식 컨텍스트 주입
    time_ctx = _get_market_time_context()

    return f"""한국 주식 공시 분석 전문가다. 다음 공시들을 분석하라.

## 시간 컨텍스트
{time_ctx}

## DART 공시 유형별 판단 기준
- 수주/계약: 매출 대비 규모 중요. 시총 대비 10% 이상이면 점수 7+
- 배당결정: 단기 수급 긍정이나 성장 기대치 낮음. 점수 5~6
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
    market_env: str = "",
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
        market_env:   [v4.2 Phase 1] 시장 환경 문자열 (선택)
                      "강세장" 또는 "약세장/횡보" 포함 시 R/R 기준 자동 분기.
                      빈 문자열이면 AI가 컨텍스트로 판단.

    반환: dict
    {
        "판단": str,              # "진짜급등" | "작전주의심" | "판단불가"
        "이유": str,              # 20자 이내
        "target_price": int|None, # [v4.2] 추정 목표가 (원). 판단불가 시 None
        "stop_loss": int|None,    # [v4.2] 권장 손절가 (원). 판단불가 시 None
        "risk_reward_ratio": float|None  # [v4.2] 손익비 (소수점 1자리). 판단불가 시 None
    }
    """
    if not _CLIENT:
        return {"판단": "판단불가", "이유": "AI 미설정",
                "target_price": None, "stop_loss": None, "risk_reward_ratio": None}

    prompt = _build_spike_prompt(analysis, news_context, ai_context, market_env)

    try:
        raw  = _call_api(prompt)
        data = _extract_json(raw)
        if not isinstance(data, dict):
            return {"판단": "판단불가", "이유": "파싱 실패",
                    "target_price": None, "stop_loss": None, "risk_reward_ratio": None}
        return _parse_spike_result(data, analysis)
    except Exception as e:
        logger.warning(f"[ai] analyze_spike 실패: {e}")
        return {"판단": "판단불가", "이유": str(e)[:30],
                "target_price": None, "stop_loss": None, "risk_reward_ratio": None}


def _build_spike_prompt(
    analysis: dict,
    news_context: str,
    ai_context: str,
    market_env: str,
) -> str:
    """
    [v4.2] 윌리엄 오닐 인격 + SYSTEM CONSTRAINTS + 강세장/약세장 분기 프롬프트 빌드
    """

    current_price = analysis.get("현재가", 0) or analysis.get("등락률", 0)  # 가격 정보
    종목명 = analysis.get("종목명", "N/A")
    종목코드 = analysis.get("종목코드", "N/A")
    등락률 = analysis.get("등락률", 0)
    거래량배율 = analysis.get("거래량배율", 0)
    감지시각 = analysis.get("감지시각", "N/A")

    # 선택 블록 조립
    news_line    = f"\n관련뉴스: {news_context}" if news_context else ""
    context_line = f"\n\n[과거 데이터 참고]\n{ai_context}" if ai_context else ""
    market_line  = f"\n시장 환경: {market_env}" if market_env else ""

    # 시장 환경에 따른 R/R 기준 분기
    if "강세장" in market_env:
        rr_rule = "강세장 적용: R/R 1.2 이상이면 진짜급등 우선 고려. 모멘텀 > R/R 절대 기준."
        stop_rule = "손절: -5% (R/R < 1.5) 또는 -7% (R/R >= 1.5)"
    elif "약세장" in market_env or "횡보" in market_env:
        rr_rule = "약세장/횡보 적용: R/R 2.0 이상만 진짜급등 고려. 자본 보존 최우선."
        stop_rule = "손절: -7% 이내 엄수"
    else:
        rr_rule = "시장 환경 미제공: 종목 자체 모멘텀과 거래량으로 판단. R/R 1.5 이상 권장."
        stop_rule = "손절: -7% 기본 기준"

    # 시간 인식
    time_ctx = _get_market_time_context()

    return f"""## 당신의 정체성
당신은 윌리엄 오닐(William O'Neil)입니다.
CAN SLIM 시스템 창시자. 철칙: "손실은 7~8%에서 자른다. 예외 없다."

## SYSTEM CONSTRAINTS (반드시 준수)
1. 조건부 대기 표현 절대 금지:
   - "지지 확인 후 진입", "횡보 후 재진입", "다음 기회에" → 사용 불가
2. 판단은 지금 이 순간만: 진짜급등 또는 작전주의심 또는 판단불가
3. 이 시스템은 분할 매매 불가 — 진입하면 전량, 손절하면 전량
4. 불확실하면 "판단불가" 선택. "나중에" 는 존재하지 않는다.

## 손절 철칙 (예외 조건 엄격 명시)
- 손실 -7.1% 이상: 즉시 자동 손절. 이유 불문. 논의 불가.
- 유일한 예외 허용 (아래 5개 조건 ALL 충족 시에만):
  1. 손실이 -5% ~ -7% 구간 (절대 -7.1% 이상은 불가)
  2. 당일 종가 반등 +3% 이상
  3. 당일 거래량 ≥ 20일 평균 × 2배
  4. 기관 또는 외국인 순매수
  5. 유예 기간 최대 1일 (2일차 미회복 시 무조건 손절)

## 시장 환경 & R/R 기준
{market_line if market_line else ""}
- {rr_rule}
- {stop_rule}

## 시간 인식 (데이터 신뢰도)
{time_ctx}

## 분석 대상
종목: {종목명} ({종목코드})
등락률: +{등락률:.1f}%
거래량: 전일 대비 {거래량배율:.1f}배 (RVOL)
감지시각: {감지시각}{news_line}{context_line}

## 판단 기준
진짜급등: 실적/공시/리포트 등 명확한 근거 + 거래량 자연스러운 증가 + 기관/외인 참여
작전주의심: 뚜렷한 이유 없음 + 거래량 폭발 + 단기 급등 패턴 + 개인 쏠림
판단불가: 정보 부족 또는 판단 근거 불충분

과거 데이터 제공 시: 트리거 승률·종목 이력·매매 원칙을 판단에 반영.

## 응답 형식 (JSON만, 설명 없이)
{{
  "판단": "진짜급등",
  "이유": "20자 이내 이유",
  "target_price": 12000,
  "stop_loss": 9300,
  "risk_reward_ratio": 3.0
}}

target_price / stop_loss: 현재 등락률과 지지/저항 기반 정수 추정 (판단불가 시 null)
risk_reward_ratio: (목표가 - 현재가) / (현재가 - 손절가), 소수점 1자리 (판단불가 시 null)"""


def _parse_spike_result(data: dict, analysis: dict) -> dict:
    """
    [v4.2] JSON 파싱 + 타입 안전 처리
    target_price, stop_loss, risk_reward_ratio 필드 추가
    """
    판단 = data.get("판단", "판단불가")
    if 판단 not in ("진짜급등", "작전주의심", "판단불가"):
        판단 = "판단불가"

    # 숫자 필드 안전 파싱
    def _safe_int(val):
        try:
            return int(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    def _safe_float(val):
        try:
            return round(float(val), 1) if val is not None else None
        except (TypeError, ValueError):
            return None

    target  = _safe_int(data.get("target_price"))
    stop    = _safe_int(data.get("stop_loss"))
    rr      = _safe_float(data.get("risk_reward_ratio"))

    # R/R 직접 계산 (AI 제공값 없고 target/stop 있을 때 보정)
    if rr is None and target and stop:
        current = analysis.get("현재가", 0)
        if current and current > stop:
            expected_return = (target - current) / current * 100
            expected_loss   = (current - stop)   / current * 100
            if expected_loss > 0:
                rr = round(expected_return / expected_loss, 1)

    return {
        "판단":              판단,
        "이유":              data.get("이유", ""),
        "target_price":     target,
        "stop_loss":        stop,
        "risk_reward_ratio": rr,
    }


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

    # [v4.2] 시간 컨텍스트 주입
    time_ctx = _get_market_time_context()

    return f"""한국 주식시장 마감 데이터 분석 — 내일 순환매 지도 작성

## 시간 컨텍스트
{time_ctx}

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
