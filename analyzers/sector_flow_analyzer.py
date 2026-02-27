"""
analyzers/sector_flow_analyzer.py
섹터 자금흐름 + 공매도 잔고 → 방향성 점수화 전담

[ARCHITECTURE rule #92 — 절대 금지]
- pykrx 직접 호출 금지. 입력 데이터(sector_etf_collector, short_interest_collector 반환값)만 사용.
- KIS API 호출 금지 (rule #91 계열)
- 텔레그램 발송 금지
- DB 기록 금지
- 수집 로직 금지 — 순수 분석·점수화만 담당

[v10.0 Phase 3 신규]

입력:
  etf_data:        sector_etf_collector.collect() 반환값
  short_data:      short_interest_collector.collect() 반환값
  price_by_sector: price_collector.collect_daily() 반환값["by_sector"]

출력 형식 (신호7):
  {
    "signals":        list[dict],  # 섹터 방향성 신호 목록 (signal_analyzer 신호7로 주입)
    "sector_scores":  dict,        # {섹터명: score} — oracle_analyzer에 전달
    "top_sectors":    list[str],   # 상위 섹터명 목록 (최대 3개)
    "short_clusters": list[dict],  # 공매도 잔고 급감 섹터 클러스터
    "summary":        str,         # 한 줄 요약
  }

신호 발생 조건 (설계문서 §4.2):
  섹터 ETF 거래량 이상: Z-스코어 ≥ 2.0 (섹터 관심 급증)
  외국인 섹터별 순매수: 연속 2일 이상 해당 섹터 순매수
  공매도 잔고 급감:     급감 종목 3개 이상이 동일 섹터
  프로그램 매수 이상:   섹터 ETF 거래대금 전일 대비 3배 이상
"""

import statistics
import config
from utils.logger import logger


# ── 섹터명 정규화 매핑 ────────────────────────────────────────────
# sector_etf_collector 섹터명 ↔ price_collector 업종명 교차 매핑
_SECTOR_NORMALIZE: dict[str, list[str]] = {
    "반도체/IT":      ["반도체", "전기전자", "전자", "IT", "디스플레이"],
    "자동차/부품":    ["자동차", "운수장비", "기계"],
    "철강/비철금속":  ["철강", "비철금속", "금속", "제철"],
    "건설/부동산":    ["건설업", "건설", "건축"],
    "에너지/화학":    ["화학", "에너지", "정유", "석유", "고무"],
    "해운/조선/운송": ["운수창고", "해운", "조선", "운송"],
    "IT/소프트웨어":  ["서비스업", "소프트웨어", "IT서비스", "통신"],
    "금융/은행":      ["금융업", "은행", "증권", "보험"],
    "방산/항공":      ["방위산업", "항공", "방산"],
}

# Z-스코어 임계값
_ZSCORE_THRESHOLD = 2.0

# 거래대금 이상 배율 (전일 대비 3배)
_VALUE_SURGE_RATIO = 3.0

# 공매도 클러스터 최소 종목 수
_SHORT_CLUSTER_MIN = 3


def analyze(
    etf_data:        list[dict],
    short_data:      list[dict],
    price_by_sector: dict = None,
) -> dict:
    """
    섹터 ETF 자금흐름 + 공매도 잔고 데이터를 종합하여 신호7 생성.

    rule #92: 입력 파라미터만 사용. pykrx·KIS API 직접 호출 금지.

    Args:
        etf_data:        sector_etf_collector.collect() 반환값
        short_data:      short_interest_collector.collect() 반환값
        price_by_sector: price_collector "by_sector" dict (업종→종목 맵)

    Returns:
        dict — 신호7 분석 결과 (신호 목록 + 섹터 점수)
    """
    if not etf_data and not short_data:
        logger.info("[sector_flow] 입력 데이터 없음 — 신호7 생략")
        return _empty_result()

    price_by_sector = price_by_sector or {}

    # ── 1. ETF 거래량 Z-스코어 계산 ───────────────────────────────
    etf_signals     = _analyze_etf_volume(etf_data)

    # ── 2. 공매도 잔고 클러스터 분석 ──────────────────────────────
    short_clusters  = _analyze_short_clusters(short_data, price_by_sector)

    # ── 3. 섹터 방향성 점수 종합 ──────────────────────────────────
    sector_scores   = _calc_sector_scores(etf_data, etf_signals, short_clusters)

    # ── 4. 신호 목록 구성 ─────────────────────────────────────────
    signals         = _build_signals(etf_signals, short_clusters, sector_scores, price_by_sector)

    # 상위 섹터 추출
    top_sectors = sorted(sector_scores, key=sector_scores.get, reverse=True)[:3]

    summary = _build_summary(etf_signals, short_clusters, top_sectors)

    logger.info(
        f"[sector_flow] 신호7 분석 완료 — "
        f"ETF이상 {len(etf_signals)}개 / 공매도클러스터 {len(short_clusters)}개 / "
        f"상위섹터: {top_sectors}"
    )

    return {
        "signals":        signals,
        "sector_scores":  sector_scores,
        "top_sectors":    top_sectors,
        "short_clusters": short_clusters,
        "summary":        summary,
    }


# ══════════════════════════════════════════════════════════════════
# 내부 함수
# ══════════════════════════════════════════════════════════════════

def _analyze_etf_volume(etf_data: list[dict]) -> list[dict]:
    """
    ETF 거래량 Z-스코어 계산 → 이상 거래량 감지.

    Z-스코어: (당일 volume_ratio - 평균) / 표준편차
    기준: volume_ratio 배열 (volume_ratio = 당일 거래량 / 전일 거래량)
    """
    if not etf_data:
        return []

    # 시장 전체 기준선(KODEX 200) 제외하고 계산
    sector_etfs = [e for e in etf_data if e.get("sector") != "시장전체"]
    if not sector_etfs:
        return []

    ratios = [e.get("volume_ratio", 1.0) for e in sector_etfs]

    if len(ratios) < 3:
        # 데이터 부족 시 단순 3배 이상 기준
        result = []
        for e in sector_etfs:
            if e.get("volume_ratio", 1.0) >= _VALUE_SURGE_RATIO:
                result.append({**e, "zscore": None, "is_anomaly": True})
        return result

    mean   = statistics.mean(ratios)
    stdev  = statistics.stdev(ratios) if len(ratios) >= 2 else 1.0
    if stdev == 0:
        stdev = 0.001

    result = []
    for e in sector_etfs:
        ratio  = e.get("volume_ratio", 1.0)
        zscore = (ratio - mean) / stdev
        is_anomaly = zscore >= _ZSCORE_THRESHOLD

        if is_anomaly or ratio >= _VALUE_SURGE_RATIO:
            result.append({
                **e,
                "zscore":     round(zscore, 2),
                "is_anomaly": True,
            })
            logger.info(
                f"[sector_flow] ETF 거래량 이상: {e['name']}({e['sector']}) "
                f"배율={ratio:.2f}x Z={zscore:.2f}"
            )

    return result


def _analyze_short_clusters(
    short_data: list[dict],
    price_by_sector: dict,
) -> list[dict]:
    """
    공매도 잔고 급감 종목 → 동일 섹터 클러스터 감지.

    발화 조건: 급감 종목 3개 이상이 동일 섹터.
    """
    if not short_data:
        return []

    # 종목코드 → 업종명 역매핑
    ticker_to_sector: dict[str, str] = {}
    for sector_name, stocks in price_by_sector.items():
        for st in stocks:
            ticker = st.get("종목코드", "") or st.get("ticker", "")
            if ticker:
                ticker_to_sector[ticker] = sector_name

    # 섹터별 급감 종목 집계
    sector_shorts: dict[str, list[dict]] = {}
    for item in short_data:
        if item.get("signal") not in ("쇼트커버링_예고", "공매도급감"):
            continue
        ticker = item.get("ticker", "")
        sector = ticker_to_sector.get(ticker, "기타")
        sector_shorts.setdefault(sector, []).append(item)

    # 클러스터 기준 충족 섹터 추출
    clusters = []
    for sector, items in sector_shorts.items():
        if sector == "기타":
            continue
        if len(items) >= _SHORT_CLUSTER_MIN:
            clusters.append({
                "sector":   sector,
                "count":    len(items),
                "tickers":  [it["ticker"] for it in items],
                "names":    [it["name"] for it in items],
                "signal":   items[0]["signal"],   # 대표 신호
            })
            logger.info(
                f"[sector_flow] 공매도 클러스터 감지: {sector} "
                f"{len(items)}종목 ({items[0]['signal']})"
            )

    clusters.sort(key=lambda x: x["count"], reverse=True)
    return clusters


def _calc_sector_scores(
    etf_data: list[dict],
    etf_signals: list[dict],
    short_clusters: list[dict],
) -> dict[str, int]:
    """
    섹터별 방향성 점수 계산.

    점수 기여:
      ETF 거래량 이상 (Z≥2): +15점
      ETF 거래대금 3배 이상:  +10점
      ETF 등락률 양수:        +5점
      공매도 클러스터:        +15점 (쇼트커버링_예고) / +10점 (공매도급감)
    """
    scores: dict[str, int] = {}

    # ETF 기반 점수
    for e in etf_data:
        sector = e.get("sector", "")
        if not sector or sector == "시장전체":
            continue

        s = scores.get(sector, 0)

        # Z-스코어 이상 ETF
        if any(sig.get("sector") == sector for sig in etf_signals):
            s += 15

        # 거래대금 3배 이상
        if e.get("volume_ratio", 0) >= _VALUE_SURGE_RATIO:
            s += 10

        # 등락률 양수
        if e.get("change_pct", 0) > 0:
            s += 5

        scores[sector] = s

    # 공매도 클러스터 점수
    for cluster in short_clusters:
        sector = cluster.get("sector", "")
        s = scores.get(sector, 0)
        if cluster.get("signal") == "쇼트커버링_예고":
            s += 15
        else:
            s += 10
        scores[sector] = s

    return scores


def _build_signals(
    etf_signals: list[dict],
    short_clusters: list[dict],
    sector_scores: dict,
    price_by_sector: dict,
) -> list[dict]:
    """
    신호7 목록 구성 — signal_analyzer.analyze()의 signals 리스트에 추가됨.

    rule #94 계열: 신호7 결과는 signal_analyzer → signals 경유.
                  oracle_analyzer 직접 전달 금지.
    """
    signals = []

    # ETF 거래량 이상 신호
    for e in etf_signals:
        sector = e.get("sector", "")

        # 해당 섹터 관련 종목 동적 조회
        관련종목 = _get_sector_top_stocks(price_by_sector, sector)

        zscore_str = f" Z={e['zscore']:.1f}" if e.get("zscore") is not None else ""
        signals.append({
            "테마명":   sector,
            "발화신호": (
                f"신호7: {e['name']} 거래량 이상 "
                f"배율={e.get('volume_ratio', 0):.1f}x{zscore_str} [섹터ETF|전날]"
            ),
            "강도":     4 if e.get("volume_ratio", 1) >= 5 else 3,
            "신뢰도":   e.get("신뢰도", "pykrx"),
            "발화단계": "1일차",
            "상태":     "신규",
            "관련종목": 관련종목,
        })

    # 공매도 클러스터 신호
    for cluster in short_clusters:
        sector = cluster.get("sector", "")
        관련종목 = _get_sector_top_stocks(price_by_sector, sector)

        signals.append({
            "테마명":   sector,
            "발화신호": (
                f"신호7: {cluster['signal']} {sector} "
                f"{cluster['count']}종목 급감 [공매도잔고|전주比]"
            ),
            "강도":     5 if cluster["signal"] == "쇼트커버링_예고" else 4,
            "신뢰도":   "pykrx",
            "발화단계": "불명",
            "상태":     "신규",
            "관련종목": 관련종목,
        })

    signals.sort(key=lambda x: x["강도"], reverse=True)
    return signals


def _get_sector_top_stocks(
    price_by_sector: dict,
    sector: str,
    top_n: int = 3,
) -> list[str]:
    """
    price_by_sector에서 섹터명 키워드 매핑으로 관련 종목명 반환.

    sector_etf "철강/비철금속" → price_by_sector "철강", "비철금속" 등 매핑.
    """
    keywords = _SECTOR_NORMALIZE.get(sector, [sector])
    matched_stocks: list[str] = []

    for sector_key, stocks in price_by_sector.items():
        # 키워드 매핑
        if not any(kw in sector_key for kw in keywords):
            continue
        for st in sorted(stocks, key=lambda x: abs(x.get("등락률", 0)), reverse=True)[:top_n]:
            name = st.get("종목명", "")
            if name and name not in matched_stocks:
                matched_stocks.append(name)
        if len(matched_stocks) >= top_n:
            break

    return matched_stocks[:top_n]


def _build_summary(
    etf_signals: list[dict],
    short_clusters: list[dict],
    top_sectors: list[str],
) -> str:
    """신호7 한 줄 요약 생성."""
    parts = []

    if etf_signals:
        names = [e.get("sector", e.get("name", "")) for e in etf_signals[:2]]
        parts.append(f"섹터ETF이상: {', '.join(names)}")

    if short_clusters:
        secs = [c["sector"] for c in short_clusters[:2]]
        parts.append(f"공매도급감: {', '.join(secs)}")

    if not parts and top_sectors:
        parts.append(f"관심섹터: {', '.join(top_sectors)}")

    return " | ".join(parts) if parts else "섹터 수급 신호 없음"


def _empty_result() -> dict:
    return {
        "signals":        [],
        "sector_scores":  {},
        "top_sectors":    [],
        "short_clusters": [],
        "summary":        "섹터 수급 데이터 없음",
    }
