"""
analyzers/volume_analyzer.py
장중 급등 감지 전담 — KIS REST 실시간 기반

반환값 규격 (ARCHITECTURE.md 계약):
{"종목코드": str, "종목명": str, "등락률": float, "직전대비": float,
 "거래량배율": float, "순간강도": float, "조건충족": bool,
 "감지시각": str, "감지소스": str}

[수정이력]
- v1.3: CONFIRM_CANDLES 미사용 버그 수정 — 연속 N틱 카운터 구현
- v2.3: 거래량 필드명 불일치 + 이중 누적 버그 수정
- v2.4: poll_all_markets() 신규 — pykrx REST 전 종목 폴링
- v2.5: 데이터 소스를 pykrx → KIS REST 실시간으로 전환
- v2.8: [핵심 변경] 누적 기준 → 델타(1분 변화량) 기준으로 전환
- v2.9: 등락률 순위 API 병행 조회 추가 (get_rate_ranking)
        거래량 TOP 30 + 등락률 TOP 30 → 중복 제거 → 최대 60종목 델타 감지
- v3.2: [Phase 2 / T2] 갭 상승 모멘텀 감지 추가
- v3.7: [노이즈 필터 3종 추가]
        ① 누적 등락률 < MIN_CHANGE_RATE(2%) 스킵
        ② 장중 거래대금 < MIN_TRADE_AMOUNT(30억) 스킵
        ③ 사이클당 MAX_ALERTS_PER_CYCLE(5) 초과 알림 차단
- v3.8: [초기 급등 포착 & 뒷북 방지 — 핵심 개선]
        ① MAX_CATCH_RATE(12%) 상한 필터 추가
           → 이미 급등 끝난 종목(21.8% 등) 알림 완전 차단 (뒷북 제거)
           → volume_ranking에는 등락률 제한이 없으므로 직접 필터링 필수
        ② MIN_CHANGE_RATE 2.0→3.0% 상향
           → 바닥 꼬물거리는 가짜 상승 제거
        ③ MIN_VOL_RATIO_ACML(30%) 누적 RVOL 필터 추가
           → 누적거래량/전일거래량 × 100 ≥ 30% 검증
           → 호가만 비어서 오르는 쪽박주(거래량 없는 허상 급등) 제거
           → 진짜 RVOL 개념 도입 (기존 거래량배율=순간 Δvol은 RVOL 아님)
        ④ 거래량배율 출력 의미 변경: 순간 Δvol → 누적 RVOL (실제 RVOL 표시)
           + 순간강도(순간 Δvol 비율) 별도 필드로 분리 → 알림 정보 풍부화
        ⑤ MAX_ALERTS_PER_CYCLE 정렬 기준: 등락률→직전대비(순간 가속도)
           → 이미 많이 오른 순이 아닌, 지금 막 폭발하는 종목 우선 발송
        ⑥ 알림 발송 후 _confirm_count 초기화 (재트리거 방지)
        ⑦ gap_up 감지에도 MAX_CATCH_RATE 상한 적용
"""

from datetime import datetime
from utils.logger import logger
import config

# ── 모듈 레벨 상태 ────────────────────────────────────────────
_prev_snapshot:  dict[str, dict] = {}  # {종목코드: row} 직전 poll 스냅샷 (REST용)
_confirm_count:  dict[str, int]  = {}  # {종목코드: 연속충족횟수} (REST용)
# v3.1: WebSocket 전용 상태 — REST _confirm_count와 분리
_ws_alerted_tickers: set[str] = set()  # 이미 WS 알림 발송된 종목 (쿨타임은 state_manager에 위임)


# ── REST 폴링 전 종목 분석 ──────────────────────────────────

def poll_all_markets() -> list[dict]:
    """
    KIS REST 거래량 순위 API로 코스피·코스닥 조회 후
    직전 poll 대비 1분간 변화량(델타)으로 급등 조건 판단.

    반환값 규격: ARCHITECTURE.md 계약 준수

    [v3.8 필터 체인 순서]
    1. 누적 등락률 < MIN_CHANGE_RATE(3%) → 스킵 (바닥 꼬물이)
    2. 누적 등락률 > MAX_CATCH_RATE(12%) → 스킵 (이미 급등 끝, 뒷북 방지)
    3. 장중 거래대금 < MIN_TRADE_AMOUNT(30억) → 스킵 (쪽정이)
    4. 전일거래량 < MIN_PREV_VOL(5만) → 스킵 (배율 왜곡)
    5. 누적RVOL < MIN_VOL_RATIO_ACML(30%) → 스킵 (허상 급등)
    6. 순간Δ등락률 ≥ PRICE_DELTA_MIN AND 순간강도 ≥ VOLUME_DELTA_MIN → 카운터 증가
    7. CONFIRM_CANDLES회 연속 → 알림 발송 + 카운터 초기화
    """
    global _prev_snapshot
    from kis.rest_client import get_volume_ranking, get_rate_ranking

    current_snapshot: dict[str, dict] = {}
    alerted:          list[dict]      = []
    is_warmup = not _prev_snapshot   # 첫 사이클 여부

    for market_code in ["J", "Q"]:
        market_name = "코스피" if market_code == "J" else "코스닥"

        vol_rows  = get_volume_ranking(market_code)
        rate_rows = get_rate_ranking(market_code)

        # 종목코드 기준 중복 제거 (거래량 순위 우선)
        seen: dict[str, dict] = {}
        for row in vol_rows:
            seen[row["종목코드"]] = {**row, "_source": "volume"}
        for row in rate_rows:
            if row["종목코드"] not in seen:
                seen[row["종목코드"]] = {**row, "_source": "rate"}

        rows = list(seen.values())
        if not rows:
            logger.debug(f"[volume] {market_name} 순위 없음")
            continue
        logger.info(
            f"[volume] {market_name} 합산: 거래량{len(vol_rows)}+등락률{len(rate_rows)}"
            f"→ 중복제거 후 {len(rows)}종목"
        )

        for row in rows:
            ticker = row["종목코드"]
            current_snapshot[ticker] = row

            if is_warmup:
                continue

            prev = _prev_snapshot.get(ticker)
            if not prev:
                continue

            prev_price = prev["현재가"]
            curr_price = row["현재가"]
            if prev_price <= 0:
                continue

            # ── 필터 1: 누적 등락률 하한 ─────────────────────────
            change_rate = row["등락률"]
            if change_rate < config.MIN_CHANGE_RATE:
                logger.debug(
                    f"[volume] {row['종목명']} 등락률 {change_rate:.1f}% < "
                    f"하한 {config.MIN_CHANGE_RATE:.1f}% → 스킵"
                )
                continue

            # ── 필터 2: 누적 등락률 상한 (v3.8 신규 — 뒷북 방지 핵심) ─
            # volume_ranking은 등락률 무제한 → 21.8% 같은 급등 끝 종목 차단
            if change_rate > config.MAX_CATCH_RATE:
                logger.debug(
                    f"[volume] {row['종목명']} 등락률 {change_rate:.1f}% > "
                    f"상한 {config.MAX_CATCH_RATE:.1f}% → 이미 급등 끝, 스킵"
                )
                continue

            # ── 필터 3: 장중 거래대금 ────────────────────────────
            trade_amount = row["누적거래량"] * curr_price
            if trade_amount < config.MIN_TRADE_AMOUNT:
                logger.debug(
                    f"[volume] {row['종목명']} 거래대금 {trade_amount/1e8:.0f}억원 < "
                    f"최솟값 {config.MIN_TRADE_AMOUNT/1e8:.0f}억원 → 스킵"
                )
                continue

            # ── 필터 4: 전일거래량 최솟값 ───────────────────────
            prdy_vol = row["전일거래량"]
            if prdy_vol < config.MIN_PREV_VOL:
                logger.debug(
                    f"[volume] {row['종목명']} 전일거래량 {prdy_vol:,}주 < "
                    f"최솟값 {config.MIN_PREV_VOL:,}주 → 스킵"
                )
                continue

            # ── 필터 5: 누적 RVOL (v3.8 신규 — 허상 급등 방지) ──
            # 누적거래량 / 전일거래량 × 100
            # 장 초반인데도 전일의 30% 이상 거래됨 = 진짜 돈이 몰리는 종목
            acml_vol  = row["누적거래량"]
            acml_rvol = (acml_vol / prdy_vol * 100) if prdy_vol > 0 else 0.0
            if acml_rvol < config.MIN_VOL_RATIO_ACML:
                logger.debug(
                    f"[volume] {row['종목명']} 누적RVOL {acml_rvol:.0f}% < "
                    f"최솟값 {config.MIN_VOL_RATIO_ACML:.0f}% → 거래량 부족, 스킵"
                )
                continue

            # ── 순간 가속도 계산 ──────────────────────────────────
            delta_rate = (curr_price - prev_price) / prev_price * 100
            delta_vol  = max(0, acml_vol - prev["누적거래량"])
            순간강도    = (delta_vol / prdy_vol * 100) if prdy_vol > 0 else 0.0

            single_ok = (
                delta_rate >= config.PRICE_DELTA_MIN and
                순간강도   >= config.VOLUME_DELTA_MIN
            )

            _confirm_count[ticker] = (
                _confirm_count.get(ticker, 0) + 1 if single_ok else 0
            )

            if _confirm_count.get(ticker, 0) >= config.CONFIRM_CANDLES:
                # [v3.8] 알림 발송 후 카운터 초기화 — 재트리거 방지
                _confirm_count[ticker] = 0

                alerted.append({
                    "종목코드":   ticker,
                    "종목명":     row["종목명"],
                    "등락률":     change_rate,                  # 현재 누적 등락률 (3~12% 구간)
                    "직전대비":   round(delta_rate, 2),         # 순간 추가 상승률 (핵심)
                    "거래량배율": round(acml_rvol / 100, 2),    # [v3.8] 누적RVOL (acml/prdy 배수)
                    "순간강도":   round(순간강도, 1),            # [v3.8] 순간 Δvol 강도 (%)
                    "조건충족":   True,
                    "감지시각":   datetime.now().strftime("%H:%M:%S"),
                    "감지소스":   row.get("_source", "volume"),
                })

    # ── [T2] 갭 상승 모멘텀 감지 (v3.2) ─────────────────────────
    gap_alerted = _detect_gap_up(current_snapshot, alerted)
    alerted.extend(gap_alerted)

    _prev_snapshot = current_snapshot

    # ── [v3.8] 사이클당 최대 알림 수 제한 + 정렬 기준 변경 ───────────
    # 기존: 등락률 내림차순 (이미 많이 오른 종목 우선 → 오히려 뒷북)
    # 변경: 직전대비(순간 가속도) 내림차순 → "지금 막 폭발하는" 종목 우선
    if len(alerted) > config.MAX_ALERTS_PER_CYCLE:
        alerted.sort(key=lambda x: x["직전대비"], reverse=True)
        suppressed = len(alerted) - config.MAX_ALERTS_PER_CYCLE
        alerted = alerted[:config.MAX_ALERTS_PER_CYCLE]
        logger.info(
            f"[volume] 사이클 최대 알림 수 초과 — 순간가속도 상위 {config.MAX_ALERTS_PER_CYCLE}개만 발송 "
            f"({suppressed}개 억제)"
        )

    if is_warmup:
        logger.info(
            f"[volume] 워밍업 완료 — {len(current_snapshot)}종목 스냅샷 저장 "
            f"/ 다음 사이클부터 실시간 감지 시작"
        )
    if alerted:
        logger.info(f"[volume] 조건충족 {len(alerted)}종목 (갭상승포함)")

    return alerted


# ── T2 갭 상승 모멘텀 내부 헬퍼 (v3.2 신규) ────────────────────

_gap_alerted: set[str] = set()   # 당일 이미 갭 알림 발송된 종목 (중복 방지)


def _detect_gap_up(snapshot: dict[str, dict], already_alerted: list[dict]) -> list[dict]:
    """
    [T2] 갭 상승 모멘텀 감지
    - change_rate > GAP_UP_MIN × 2: 갭업 + 추가 상승 복합 추정
    - [v3.8] change_rate ≤ MAX_CATCH_RATE: 이미 급등 끝난 종목 제외 (일관성)
    """
    already_codes = {a["종목코드"] for a in already_alerted}
    results = []

    for ticker, row in snapshot.items():
        if ticker in already_codes or ticker in _gap_alerted:
            continue

        curr_price  = row.get("현재가", 0)
        change_rate = row.get("등락률", 0.0)

        if curr_price <= 0 or change_rate <= 0:
            continue

        if change_rate < config.GAP_UP_MIN * 2:
            continue

        if change_rate < config.MIN_CHANGE_RATE:
            continue

        # ── [v3.8] 상한 필터 (gap_up도 동일 기준) ───────────────
        if change_rate > config.MAX_CATCH_RATE:
            logger.debug(
                f"[volume] T2 {row.get('종목명', ticker)} 등락률 {change_rate:.1f}% > "
                f"상한 {config.MAX_CATCH_RATE:.1f}% → 이미 급등 끝, 스킵"
            )
            continue

        prdy_vol = row.get("전일거래량", 1)
        if prdy_vol < config.MIN_PREV_VOL:
            logger.debug(
                f"[volume] T2 {row.get('종목명', ticker)} 전일거래량 {prdy_vol:,}주 < "
                f"최솟값 {config.MIN_PREV_VOL:,}주 → 스킵"
            )
            continue

        trade_amount = row.get("누적거래량", 0) * curr_price
        if trade_amount < config.MIN_TRADE_AMOUNT:
            logger.debug(
                f"[volume] T2 {row.get('종목명', ticker)} 거래대금 {trade_amount/1e8:.0f}억원 < "
                f"최솟값 {config.MIN_TRADE_AMOUNT/1e8:.0f}억원 → 스킵"
            )
            continue

        _gap_alerted.add(ticker)
        acml_vol  = row.get("누적거래량", 0)
        acml_rvol = (acml_vol / prdy_vol) if prdy_vol > 0 else 0.0

        results.append({
            "종목코드":   ticker,
            "종목명":     row.get("종목명", ticker),
            "등락률":     change_rate,
            "직전대비":   0.0,
            "거래량배율": round(acml_rvol, 2),   # [v3.8] 누적RVOL (acml/prdy 배수)
            "순간강도":   0.0,                    # [v3.8] 갭 감지는 순간강도 N/A
            "조건충족":   True,
            "감지시각":   datetime.now().strftime("%H:%M:%S"),
            "감지소스":   "gap_up",
        })
        logger.info(
            f"[volume] T2 갭상승 감지: {row.get('종목명', ticker)} "
            f"+{change_rate:.1f}% (갭업추정, RVOL {acml_rvol:.1f}x)"
        )

    return results


# ── WebSocket 틱 기반 분석 (향후 확장용 — 현재 미사용) ───────

def analyze(tick: dict) -> dict:
    """
    KIS WebSocket 틱 → 급등 조건 판단 (향후 WebSocket 재활성화용 보존)
    현재 장중봇은 poll_all_markets() 사용
    """
    ticker   = tick.get("종목코드", "")
    rate     = tick.get("등락률",   0.0)
    prdy_vol = tick.get("전일거래량", 0)
    acml_vol = tick.get("누적거래량", 0)

    volume_ratio = (acml_vol / prdy_vol * 100) if prdy_vol > 0 else 0.0

    single_ok = (
        rate         >= config.PRICE_DELTA_MIN and
        volume_ratio >= config.VOLUME_DELTA_MIN
    )

    if single_ok:
        _confirm_count[ticker] = _confirm_count.get(ticker, 0) + 1
    else:
        _confirm_count[ticker] = 0

    confirmed = _confirm_count.get(ticker, 0) >= config.CONFIRM_CANDLES

    return {
        "종목코드":   ticker,
        "종목명":     tick.get("종목명", ticker),
        "등락률":     rate,
        "직전대비":   0.0,
        "거래량배율": round(volume_ratio / 100, 2),
        "순간강도":   0.0,
        "조건충족":   confirmed,
        "감지시각":   datetime.now().strftime("%H:%M:%S"),
        "감지소스":   "volume",
    }


def reset() -> None:
    """장 마감(15:30) 후 상태 전체 초기화"""
    global _prev_snapshot
    _prev_snapshot = {}
    _confirm_count.clear()
    _ws_alerted_tickers.clear()
    _gap_alerted.clear()
    logger.info("[volume] 스냅샷·확인카운터·WS상태·갭상승상태 초기화 완료")


# ── WebSocket 틱 기반 분석 (v3.1 신규 — 방법 B) ──────────────

def analyze_ws_tick(tick: dict, prdy_vol: int) -> dict | None:
    """
    KIS WebSocket 실시간 체결 틱 → 급등 조건 판단 (v3.1)

    [REST 폴링과의 차이]
    REST  : 직전 poll 대비 Δ등락률/Δ거래량 (변화량 기준) → 신규 종목 발굴
    WS    : 누적 등락률 >= PRICE_CHANGE_MIN(3%) (절대값 기준) → 워치리스트 즉시 감지

    [v3.8] 상한 필터(MAX_CATCH_RATE) 추가 — 워치리스트 종목도 뒷북 방지
    """
    rate = tick.get("등락률", 0.0)

    if rate < config.PRICE_CHANGE_MIN:
        return None
    # [v3.8] WebSocket도 상한 필터 적용 (일관성)
    if rate > config.MAX_CATCH_RATE:
        return None

    acml_vol  = tick.get("누적거래량", 0)
    acml_rvol = (acml_vol / prdy_vol) if prdy_vol > 0 else 0.0

    ticker = tick.get("종목코드", "")
    체결시각 = tick.get("체결시각", "")
    if len(체결시각) == 6:
        체결시각 = f"{체결시각[:2]}:{체결시각[2:4]}:{체결시각[4:]}"

    return {
        "종목코드":   ticker,
        "종목명":     tick.get("종목명", ticker),
        "등락률":     rate,
        "직전대비":   0.0,
        "거래량배율": round(acml_rvol, 2),   # [v3.8] 누적RVOL
        "순간강도":   0.0,
        "조건충족":   True,
        "감지시각":   체결시각 or datetime.now().strftime("%H:%M:%S"),
        "감지소스":   "websocket",
    }
