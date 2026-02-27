"""
utils/geopolitics_map.py
지정학·정책 이벤트 키워드 → 국내 영향 섹터 매핑 사전

[ARCHITECTURE rule #93 — 절대 금지]
- 이 파일은 키워드→섹터 매핑 사전 파일 전용
- 분석 로직·API 호출 절대 금지
- 신규 이벤트 패턴 추가 시 이 파일만 수정

[v10.0 Phase 1 신규]
- geopolitics_analyzer.py에서 참조
- 이벤트 뉴스 텍스트에서 패턴 매칭 후 영향 섹터 결정
- AI 보완용 사전: AI 분석이 주도, 이 사전이 교차 검증

[데이터 구조]
GEOPOLITICS_SECTOR_MAP: dict[str, dict]
  key   = 이벤트 패턴 (소문자 키워드 문자열)
  value = {
    "sectors":          list[str],  # 영향받는 국내 섹터명 (config.US_SECTOR_KR_INDUSTRY key 우선)
    "impact":           str,        # "+" 상승 / "-" 하락 / "mixed" 혼재
    "confidence_base":  float,      # 기본 신뢰도 (0~1), AI 분석으로 보정됨
    "description":      str,        # 이벤트→섹터 연결 근거 (설명용)
  }

[패턴 매칭 규칙]
- 키워드는 소문자 기준 (geopolitics_analyzer에서 lower() 처리 후 in 연산)
- 더 구체적인 키워드를 앞에 배치 (우선 매칭)
- 하나의 이벤트에 여러 키워드가 매칭되면 가중치 합산 방식으로 처리
"""

# ══════════════════════════════════════════════════════════════
# 핵심 이벤트 → 섹터 매핑 사전
# ══════════════════════════════════════════════════════════════

GEOPOLITICS_SECTOR_MAP: dict[str, dict] = {

    # ─── 철강/관세 관련 ──────────────────────────────────────
    "철강 관세": {
        "sectors":         ["철강/비철금속", "자동차부품"],
        "impact":          "+",
        "confidence_base": 0.85,
        "description":     "미국 철강 수입 관세 → 국내 철강사 반사이익 기대",
    },
    "steel tariff": {
        "sectors":         ["철강/비철금속", "자동차부품"],
        "impact":          "+",
        "confidence_base": 0.85,
        "description":     "US steel tariff → Korean steel makers benefit",
    },
    "trump tariff": {
        "sectors":         ["철강/비철금속", "반도체", "배터리"],
        "impact":          "mixed",
        "confidence_base": 0.75,
        "description":     "트럼프 관세 — 철강(+) 반도체/배터리(불확실)",
    },
    "관세 발표": {
        "sectors":         ["철강/비철금속", "자동차부품"],
        "impact":          "+",
        "confidence_base": 0.70,
        "description":     "관세 발표 이벤트 — 보호무역 수혜 섹터",
    },
    "수입 규제": {
        "sectors":         ["철강/비철금속"],
        "impact":          "+",
        "confidence_base": 0.72,
        "description":     "수입 규제 강화 → 국내 생산 철강 수혜",
    },

    # ─── 방산/NATO/국방 관련 ─────────────────────────────────
    "nato 국방비": {
        "sectors":         ["산업재/방산"],
        "impact":          "+",
        "confidence_base": 0.88,
        "description":     "NATO 국방비 증액 → K방산 수출 수혜",
    },
    "defense spending": {
        "sectors":         ["산업재/방산"],
        "impact":          "+",
        "confidence_base": 0.87,
        "description":     "글로벌 국방비 증가 → 방산 수출주 수혜",
    },
    "방위산업": {
        "sectors":         ["산업재/방산"],
        "impact":          "+",
        "confidence_base": 0.80,
        "description":     "방위산업 관련 정책/계약 → 방산주 모멘텀",
    },
    "방위비 분담금": {
        "sectors":         ["산업재/방산"],
        "impact":          "+",
        "confidence_base": 0.82,
        "description":     "한미 방위비 분담금 협상 → K방산 협력사 수혜",
    },
    "k방산": {
        "sectors":         ["산업재/방산"],
        "impact":          "+",
        "confidence_base": 0.85,
        "description":     "K방산 수출 관련 뉴스",
    },
    "무기 수출": {
        "sectors":         ["산업재/방산"],
        "impact":          "+",
        "confidence_base": 0.83,
        "description":     "무기 수출 계약 → 방산 모멘텀",
    },
    "우크라이나 재건": {
        "sectors":         ["산업재/방산", "철강/비철금속", "건설"],
        "impact":          "+",
        "confidence_base": 0.78,
        "description":     "우크라이나 재건 수요 → 방산/철강/건설 수혜",
    },

    # ─── 중국 관련 ───────────────────────────────────────────
    "중국 부양책": {
        "sectors":         ["철강/비철금속", "소재/화학", "기계"],
        "impact":          "+",
        "confidence_base": 0.80,
        "description":     "중국 인프라 부양 → 철강·화학·기계 수요 증가",
    },
    "china stimulus": {
        "sectors":         ["철강/비철금속", "소재/화학"],
        "impact":          "+",
        "confidence_base": 0.80,
        "description":     "China stimulus → steel/chemical demand",
    },
    "중국 인프라": {
        "sectors":         ["철강/비철금속", "시멘트", "기계"],
        "impact":          "+",
        "confidence_base": 0.78,
        "description":     "중국 인프라 투자 → 철강/시멘트 수요",
    },
    "중국 봉쇄": {
        "sectors":         ["소재/화학", "철강/비철금속"],
        "impact":          "-",
        "confidence_base": 0.75,
        "description":     "중국 봉쇄 → 원자재 수요 감소",
    },
    "csi 철강": {
        "sectors":         ["철강/비철금속"],
        "impact":          "+",
        "confidence_base": 0.82,
        "description":     "중국 철강지수 상승 → 국내 철강주 선행",
    },

    # ─── 반도체/기술 관련 ────────────────────────────────────
    "반도체 수출규제": {
        "sectors":         ["기술/반도체"],
        "impact":          "-",
        "confidence_base": 0.85,
        "description":     "반도체 수출 규제 강화 → 장비·소재주 단기 하락",
    },
    "ira 보조금": {
        "sectors":         ["배터리", "에너지"],
        "impact":          "+",
        "confidence_base": 0.80,
        "description":     "IRA 보조금 지급 → 배터리/에너지 수혜",
    },
    "ira 삭감": {
        "sectors":         ["배터리", "에너지"],
        "impact":          "-",
        "confidence_base": 0.80,
        "description":     "IRA 삭감 우려 → 배터리/EV 주가 하락 압력",
    },
    "chips act": {
        "sectors":         ["기술/반도체"],
        "impact":          "+",
        "confidence_base": 0.82,
        "description":     "CHIPS Act 지원 → 반도체 제조 투자 확대",
    },

    # ─── 에너지/원자재 관련 ─────────────────────────────────
    "유가 급등": {
        "sectors":         ["에너지/정유"],
        "impact":          "+",
        "confidence_base": 0.85,
        "description":     "국제 유가 급등 → 정유·에너지주 수혜",
    },
    "oil price": {
        "sectors":         ["에너지/정유"],
        "impact":          "+",
        "confidence_base": 0.83,
        "description":     "Oil price surge → energy/refinery stocks",
    },
    "opec 감산": {
        "sectors":         ["에너지/정유"],
        "impact":          "+",
        "confidence_base": 0.87,
        "description":     "OPEC 감산 결정 → 유가 상승 → 정유사 수혜",
    },
    "lng 가격": {
        "sectors":         ["에너지/정유", "가스"],
        "impact":          "+",
        "confidence_base": 0.78,
        "description":     "LNG 가격 급등 → 가스·에너지 섹터 수혜",
    },
    "천연가스 급등": {
        "sectors":         ["에너지/정유"],
        "impact":          "+",
        "confidence_base": 0.78,
        "description":     "천연가스 가격 급등 → 에너지주 수혜",
    },

    # ─── 환율/거시경제 ───────────────────────────────────────
    "원화 약세": {
        "sectors":         ["기술/반도체", "철강/비철금속", "조선"],
        "impact":          "+",
        "confidence_base": 0.75,
        "description":     "원화 약세 → 수출주 전반 수혜",
    },
    "달러 강세": {
        "sectors":         ["기술/반도체", "조선"],
        "impact":          "+",
        "confidence_base": 0.73,
        "description":     "달러 강세 → 수출주 환차익",
    },
    "금리 인하": {
        "sectors":         ["금융", "부동산"],
        "impact":          "-",    # 금융주는 단기 하락 (NIM 축소)
        "confidence_base": 0.72,
        "description":     "금리 인하 → 은행 NIM 축소 우려 vs 성장주 밸류업",
    },
    "금리 인상": {
        "sectors":         ["금융"],
        "impact":          "+",
        "confidence_base": 0.72,
        "description":     "금리 인상 → 은행 NIM 확대 → 금융주 수혜",
    },

    # ─── 러시아-우크라이나 ───────────────────────────────────
    "러시아 우크라이나": {
        "sectors":         ["산업재/방산", "에너지/정유", "철강/비철금속"],
        "impact":          "mixed",
        "confidence_base": 0.70,
        "description":     "지정학 분쟁 — 방산(+), 에너지(혼재), 철강(구조조정기대)",
    },
    "종전 협상": {
        "sectors":         ["산업재/방산"],
        "impact":          "-",
        "confidence_base": 0.72,
        "description":     "종전 협상 진전 → 방산주 단기 하락 압력",
    },
    "휴전": {
        "sectors":         ["산업재/방산"],
        "impact":          "-",
        "confidence_base": 0.70,
        "description":     "휴전 논의 → 방산 모멘텀 둔화 우려",
    },

    # ─── 한국 내수 정책 ──────────────────────────────────────
    "국내 방위비": {
        "sectors":         ["산업재/방산"],
        "impact":          "+",
        "confidence_base": 0.80,
        "description":     "국내 방위비 예산 증액 → 방산 수주 기대",
    },
    "반도체 특별법": {
        "sectors":         ["기술/반도체"],
        "impact":          "+",
        "confidence_base": 0.82,
        "description":     "반도체 특별법 통과 → 반도체 투자 촉진",
    },
    "배터리 보조금": {
        "sectors":         ["배터리"],
        "impact":          "+",
        "confidence_base": 0.80,
        "description":     "배터리/EV 보조금 정책 → 관련주 수혜",
    },
    "조선 수주": {
        "sectors":         ["조선"],
        "impact":          "+",
        "confidence_base": 0.85,
        "description":     "대형 조선 수주 → 조선·기자재 수혜",
    },
}


# ══════════════════════════════════════════════════════════════
# 헬퍼 함수 — 단순 매핑 조회만 허용 (분석 로직 금지, rule #93)
# ══════════════════════════════════════════════════════════════

def lookup(text: str) -> list[dict]:
    """
    뉴스 텍스트에서 매핑 사전 패턴을 검색해 매칭된 항목 목록 반환.

    반환: [{"key": str, **entry_dict}, ...]  — 빈 리스트면 매핑 없음
    분석 로직 없음. geopolitics_analyzer에서 해석·가중치 처리.

    rule #93: 이 함수는 dict 조회만 수행. AI 호출·수집·DB 기록 절대 금지.
    """
    text_lower = text.lower()
    results = []
    for key, entry in GEOPOLITICS_SECTOR_MAP.items():
        if key.lower() in text_lower:
            results.append({"key": key, **entry})
    return results


def get_all_sectors() -> list[str]:
    """사전에 등록된 모든 섹터 목록 반환 (중복 제거)"""
    sectors = set()
    for entry in GEOPOLITICS_SECTOR_MAP.values():
        sectors.update(entry.get("sectors", []))
    return sorted(sectors)
