"""
analyzers/event_impact_analyzer.py
기업 이벤트 캘린더 → 수급 모멘텀 예측 전담

[설계 원칙 — rule #91 계열 준수]
- 분석만 담당: KIS API·pykrx 호출·발송·DB 기록 절대 금지
- 입력 파라미터만으로 동작 (외부 의존성 없는 순수 계산 모듈)
- 실패 시 빈 리스트 반환 (비치명적)

[분석 로직]
- 실적발표 D-1: 관련 종목 수급 예측 신호 → 신호8
- 주주총회 D-3 이내: 소액주주 이슈·배당락 예측 신호
- IR D-1: 기관 사전 매수 패턴 신호
- 배당 공시: 배당락일 전 매수 수급 증가 예측

[신호8 출력 형식]
[
    {
        "event_type":        str,    # "IR" | "주주총회" | "실적발표" | "배당"
        "corp_name":         str,    # 기업명
        "ticker":            str,    # 종목코드
        "event_date":        str,    # 이벤트 예정일
        "days_until":        int,    # 오늘부터 며칠 후
        "impact_direction":  str,    # "+" 매수 압력 / "-" 매도 압력 / "mixed"
        "strength":          int,    # 신호 강도 3~5
        "reason":            str,    # 수급 예측 근거 (한국어 50자 이내)
    }
]

[v10.0 Phase 4-1 신규]
"""

from utils.logger import logger


# 이벤트 유형별 기본 강도 및 방향
_EVENT_CONFIG = {
    "실적발표": {
        "direction": "+",
        "base_strength": 4,
        "reason_template": "{corp} 실적발표 D-{days} — 기관 사전 포지셔닝 예상",
        "lookahead_days": 2,  # D-2 이내만 신호 발화
    },
    "IR": {
        "direction": "+",
        "base_strength": 3,
        "reason_template": "{corp} 기업설명회 D-{days} — 기관/외인 관심 선행 유입",
        "lookahead_days": 2,
    },
    "주주총회": {
        "direction": "mixed",
        "base_strength": 3,
        "reason_template": "{corp} 주주총회 D-{days} — 소액주주 이슈·배당 확정 예상",
        "lookahead_days": 5,
    },
    "배당": {
        "direction": "+",
        "base_strength": 4,
        "reason_template": "{corp} 배당 공시 D-{days} — 배당락 전 매수 수급 증가",
        "lookahead_days": 3,
    },
}


def analyze(events: list[dict]) -> list[dict]:
    """
    기업 이벤트 목록 → 신호8 예측 신호 변환

    파라미터:
        events: event_calendar_collector.collect() 반환값

    반환:
        list[dict] — impact 신호 목록 (강도 내림차순 정렬)
        실패 또는 이벤트 없을 시 빈 리스트 반환 (비치명적)
    """
    if not events:
        return []

    signals: list[dict] = []

    try:
        for ev in events:
            sig = _analyze_event(ev)
            if sig is not None:
                signals.append(sig)

        # 강도 내림차순 정렬 (같은 강도면 days_until 오름차순)
        signals.sort(key=lambda x: (-x["strength"], x["days_until"]))

        logger.info(f"[event_impact] 신호8 생성 — {len(signals)}건")

    except Exception as e:
        logger.warning(f"[event_impact] 분석 실패: {e}")

    return signals


def _analyze_event(ev: dict) -> dict | None:
    """
    단일 이벤트 → 신호 변환
    조건 미충족 시 None 반환
    """
    event_type = ev.get("event_type", "")
    days_until = ev.get("days_until", -1)
    corp_name  = ev.get("corp_name", "")
    ticker     = ev.get("ticker", "")

    cfg = _EVENT_CONFIG.get(event_type)
    if cfg is None:
        return None

    # 수집 범위 필터 (lookahead_days 초과 이벤트 제외)
    if days_until < 0 or days_until > cfg["lookahead_days"]:
        return None

    # 강도 계산: D-1이면 +1 부스팅
    strength = cfg["base_strength"]
    if days_until <= 1:
        strength = min(5, strength + 1)

    reason = cfg["reason_template"].format(
        corp=corp_name,
        days=days_until,
    )

    return {
        "event_type":       event_type,
        "corp_name":        corp_name,
        "ticker":           ticker,
        "event_date":       ev.get("event_date", ""),
        "days_until":       days_until,
        "impact_direction": cfg["direction"],
        "strength":         strength,
        "reason":           reason,
    }
