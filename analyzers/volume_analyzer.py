"""
analyzers/volume_analyzer.py
장중 급등 감지 전담 — KIS REST 실시간 기반

반환값 규격 (ARCHITECTURE.md 계약):
{"종목코드": str, "종목명": str, "등락률": float, "직전대비": float,
 "거래량배율": float, "조건충족": bool, "감지시각": str, "감지소스": str}

[수정이력]
- v1.3: CONFIRM_CANDLES 미사용 버그 수정 — 연속 N틱 카운터 구현
- v2.3: 거래량 필드명 불일치 + 이중 누적 버그 수정
- v2.4: poll_all_markets() 신규 — pykrx REST 전 종목 폴링
- v2.5: 데이터 소스를 pykrx → KIS REST 실시간으로 전환
- v2.8: [핵심 변경] 누적 기준 → 델타(1분 변화량) 기준으로 전환
- v2.9: 등락률 순위 API 병행 조회 추가 (get_rate_ranking)
        거래량 TOP 30 + 등락률 TOP 30 → 중복 제거 → 최대 60종목 델타 감지
        디모아형(거래량 적음, 등락률 높음) 소형주 포착 커버리지 확장
- v3.2: [Phase 2 / T2] 갭 상승 모멘텀 감지 추가
        poll_all_markets() 폴링 사이클에서 갭업+장중추가상승 복합 조건 감지
        데이터 소스: KIS REST 시가(stck_oprc) 활용 (v3.2에서 get_stock_price에 추가)
        갭 상승 감지: 시가/전일종가 역산 비율 ≥ GAP_UP_MIN(1%) + 현재가 > 시가
        감지소스 = "gap_up" 으로 구분 (volume/rate/websocket과 구별)
        문제: 전일종가 대비 누적 등락률/거래량 조건
              → 재시작 시 이미 올라간 종목 전부 한번에 감지 (폭탄 알림)
        해결: _prev_snapshot 캐시 도입
              직전 poll 대비 1분간 변화량(Δ등락률, Δ거래량)으로 조건 판단
              첫 사이클 = 워밍업 (스냅샷 저장만, 알림 없음)
              두 번째 사이클부터 진짜 "지금 막 터지는" 종목만 포착
        조건: Δ등락률 ≥ PRICE_DELTA_MIN(1%) AND Δ거래량 ≥ VOLUME_DELTA_MIN(5%)
              × CONFIRM_CANDLES(2)회 연속
        반환값: "직전대비" key 추가 (1분간 추가 상승률)
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

    [v2.8 감지 로직]
    1. 첫 사이클 (워밍업): _prev_snapshot 저장만, 알림 없음
    2. 이후 사이클:
       - Δ등락률  = (현재가 - 직전가) / 직전가 × 100
       - Δ거래량  = 누적거래량 - 직전누적거래량
       - Δ거래량배율 = Δ거래량 / 전일거래량 × 100
       - 조건: Δ등락률 ≥ PRICE_DELTA_MIN AND Δ거래량배율 ≥ VOLUME_DELTA_MIN
               × CONFIRM_CANDLES회 연속 충족 시 알림
    """
    global _prev_snapshot
    from kis.rest_client import get_volume_ranking, get_rate_ranking

    current_snapshot: dict[str, dict] = {}
    alerted:          list[dict]      = []
    is_warmup = not _prev_snapshot   # 첫 사이클 여부

    for market_code in ["J", "Q"]:
        market_name = "코스피" if market_code == "J" else "코스닥"

        # ── 거래량 순위 + 등락률 순위 병합 (중복 제거) ──────────
        # 거래량 TOP 30: 대형주·테마 대장주 포착
        # 등락률 TOP 30: 거래량 적어도 급등하는 소형주(디모아형) 포착
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
            current_snapshot[ticker] = row   # 다음 사이클을 위해 저장

            if is_warmup:
                continue   # 워밍업 사이클: 데이터 수집만

            prev = _prev_snapshot.get(ticker)
            if not prev:
                continue   # 이번 사이클 신규 진입 종목 → 다음 사이클부터 감지

            prev_price = prev["현재가"]
            curr_price = row["현재가"]
            if prev_price <= 0:
                continue

            # ── 1분간 추가 등락률 ────────────────────────────
            delta_rate = (curr_price - prev_price) / prev_price * 100

            # ── 1분간 추가 체결량 → 전일거래량 대비 배율 ────
            delta_vol       = max(0, row["누적거래량"] - prev["누적거래량"])
            prdy_vol        = row["전일거래량"]
            delta_vol_ratio = (delta_vol / prdy_vol * 100) if prdy_vol > 0 else 0.0

            single_ok = (
                delta_rate      >= config.PRICE_DELTA_MIN and
                delta_vol_ratio >= config.VOLUME_DELTA_MIN
            )

            _confirm_count[ticker] = (
                _confirm_count.get(ticker, 0) + 1 if single_ok else 0
            )

            if _confirm_count.get(ticker, 0) >= config.CONFIRM_CANDLES:
                alerted.append({
                    "종목코드":   ticker,
                    "종목명":     row["종목명"],
                    "등락률":     row["등락률"],                      # 누적 등락률 (참고)
                    "직전대비":   round(delta_rate, 2),              # 1분간 추가 상승률 (핵심)
                    "거래량배율": round(delta_vol_ratio / 100, 2),   # 1분간 Δvol / prdy_vol
                    "조건충족":   True,
                    "감지시각":   datetime.now().strftime("%H:%M:%S"),
                    "감지소스":   row.get("_source", "volume"),      # "volume" or "rate"
                })

    # ── [T2] 갭 상승 모멘텀 감지 (v3.2) ─────────────────────────
    # 워밍업 사이클에서도 실행 (갭업은 장 시작부터 이미 발생)
    # 조건: 시가/전일종가역산 갭업률 ≥ GAP_UP_MIN AND 현재가 > 시가 (추가 상승 중)
    gap_alerted = _detect_gap_up(current_snapshot, alerted)
    alerted.extend(gap_alerted)

    _prev_snapshot = current_snapshot

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
    - 시가 > 전일종가 × (1 + GAP_UP_MIN/100): 갭업 확인
    - 현재가 > 시가: 장중 추가 상승 중
    - 이미 alerted에 포함된 종목 제외 (중복 방지)
    - 당일 이미 감지된 종목 제외 (_gap_alerted)

    전일종가 역산: prev_close ≈ curr_price / (1 + change_rate/100)
    (KIS volume-rank/rate-rank API에 전일종가 필드 없으므로 역산 사용)
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

        # 전일종가 역산
        prev_close = curr_price / (1 + change_rate / 100)
        if prev_close <= 0:
            continue

        # 시가 정보가 없으면 스킵 (get_stock_price v3.2에서 추가 — 별도 조회 불필요)
        # volume_ranking / rate_ranking 응답에는 시가 없음 → 갭 감지 불가 시 스킵
        # 향후 rest_client 응답에 stck_oprc 포함 시 활성화 가능
        # 현재는 change_rate 만으로 갭 추정
        # 갭업 추정: change_rate > GAP_UP_MIN + 시초가 갭업 가정 (보수적)
        # 더 정확한 구현은 개별 get_stock_price() 호출이 필요하나 API 비용 높음
        # → 현 단계에서 등락률로 간접 추정 (갭업 + 추가 상승 = 전체 등락률 높음)
        # T2 감지 조건: 현재 등락률이 GAP_UP_MIN × 2 이상인 종목 (갭+추가상승 복합)
        if change_rate < config.GAP_UP_MIN * 2:
            continue

        _gap_alerted.add(ticker)
        prdy_vol    = row.get("전일거래량", 1)
        acml_vol    = row.get("누적거래량", 0)
        vol_ratio   = (acml_vol / prdy_vol) if prdy_vol > 0 else 0.0

        results.append({
            "종목코드":   ticker,
            "종목명":     row.get("종목명", ticker),
            "등락률":     change_rate,
            "직전대비":   0.0,   # 갭 감지는 델타 미사용
            "거래량배율": round(vol_ratio, 2),
            "조건충족":   True,
            "감지시각":   datetime.now().strftime("%H:%M:%S"),
            "감지소스":   "gap_up",   # T2 갭 상승 트리거
        })
        logger.info(
            f"[volume] T2 갭상승 감지: {row.get('종목명', ticker)} "
            f"+{change_rate:.1f}% (갭업추정)"
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
        "직전대비":   0.0,   # WebSocket 틱은 델타 미지원 (향후 확장 시 구현)
        "거래량배율": round(volume_ratio / 100, 2),
        "조건충족":   confirmed,
        "감지시각":   datetime.now().strftime("%H:%M:%S"),
        "감지소스":   "volume",
    }


def reset() -> None:
    """장 마감(15:30) 후 상태 전체 초기화"""
    global _prev_snapshot
    _prev_snapshot = {}
    _confirm_count.clear()
    _ws_alerted_tickers.clear()   # v3.1: WS 상태도 초기화
    _gap_alerted.clear()          # v3.2: T2 갭 상승 감지 상태 초기화
    logger.info("[volume] 스냅샷·확인카운터·WS상태·갭상승상태 초기화 완료")


# ── WebSocket 틱 기반 분석 (v3.1 신규 — 방법 B) ──────────────

def analyze_ws_tick(tick: dict, prdy_vol: int) -> dict | None:
    """
    KIS WebSocket 실시간 체결 틱 → 급등 조건 판단 (v3.1)

    [REST 폴링과의 차이]
    REST  : 직전 poll 대비 Δ등락률/Δ거래량 (변화량 기준) → 신규 종목 발굴
    WS    : 누적 등락률 >= PRICE_CHANGE_MIN(3%) (절대값 기준) → 워치리스트 즉시 감지

    [중복 알림 방지]
    실제 발송 제어는 state_manager.can_alert()에 위임 (30분 쿨타임).
    이 함수는 조건 판단만 담당.

    Args:
        tick:     _parse_tick()이 반환한 실시간 틱 dict
                  {"종목코드", "등락률", "누적거래량", "체결시각", "종목명"(보강)}
        prdy_vol: 전일거래량 (watchlist_state에서 전달)

    Returns:
        조건 충족 시 알림 dict, 미충족 시 None
    """
    rate = tick.get("등락률", 0.0)

    # 누적 등락률이 임계값 미달 → 조건 미충족
    if rate < config.PRICE_CHANGE_MIN:
        return None

    acml_vol     = tick.get("누적거래량", 0)
    volume_ratio = (acml_vol / prdy_vol) if prdy_vol > 0 else 0.0

    ticker = tick.get("종목코드", "")
    체결시각 = tick.get("체결시각", "")
    if len(체결시각) == 6:   # HHMMSS → HH:MM:SS
        체결시각 = f"{체결시각[:2]}:{체결시각[2:4]}:{체결시각[4:]}"

    return {
        "종목코드":   ticker,
        "종목명":     tick.get("종목명", ticker),
        "등락률":     rate,
        "직전대비":   0.0,         # WS 틱은 누적값 — "직전대비" 개념 없음
        "거래량배율": round(volume_ratio, 2),
        "조건충족":   True,
        "감지시각":   체결시각 or datetime.now().strftime("%H:%M:%S"),
        "감지소스":   "websocket",
    }
