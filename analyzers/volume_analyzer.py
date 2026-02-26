"""
analyzers/volume_analyzer.py
장중 급등 감지 전담 — KIS REST 실시간 기반

반환값 규격 (ARCHITECTURE.md 계약):
{
    "종목코드": str, "종목명": str, "등락률": float, "직전대비": float,
    "거래량배율": float, "순간강도": float, "조건충족": bool,
    "감지시각": str, "감지소스": str,
    "호가분석": dict | None,   # v4.0 신규 (REST 호가 분석 결과)
}

호가분석 dict 규격:
{
    "매수매도비율": float,   # 총매수잔량 / 총매도잔량 (≥1.0이면 매수우세)
    "상위3집중도":  float,   # 상위3호가 잔량 / 전체잔량 (낮을수록 벽이 두꺼움)
    "호가강도":     str,     # "강세" | "중립" | "약세"
    "매수잔량":     int,
    "매도잔량":     int,
}

[수정이력]
- v1.3: CONFIRM_CANDLES 미사용 버그 수정
- v2.8: [핵심 변경] 누적 기준 → 델타(1분 변화량) 기준으로 전환
- v2.9: 등락률 순위 API 병행 조회 추가
- v3.2: [Phase 2 / T2] 갭 상승 모멘텀 감지 추가
- v3.7: 노이즈 필터 3종 추가
- v3.8: 초기 급등 포착 & 뒷북 방지 개선
- v4.0: [호가 분석 통합]
        analyze_orderbook() 신규 — KIS 호가잔량 비율로 매수/매도 강도 분석
        poll_all_markets() / analyze_ws_tick() 에 호가 분석 결과 통합
        REST 급등 감지 → get_orderbook() 즉시 호출 → 알림에 호가 강도 추가
        WebSocket 체결 급등 감지 → REST 호가 조회 → 동일 분석 적용
- v4.1: [소스 단일화] poll_all_markets() 에서 get_volume_ranking() 제거
        → 코스피/코스닥 급등률 순위(get_rate_ranking)만 사용
        이유: 거래량 순위는 삼성전자·현대차 등 시총 대형주가 항상 상위에 포함되어
             실질적인 급등 신호가 아닌 노이즈 알림 발생
        감지소스 "volume" 배지 deprecated → 장중 REST 감지는 전부 "rate"
"""

from datetime import datetime, timezone, timedelta
from utils.logger import logger

_KST = timezone(timedelta(hours=9))  # [v4.1] Railway UTC 서버 KST 보정

def _now_kst() -> str:
    """KST(한국 표준시) 현재 시각 → HH:MM:SS 문자열"""
    return datetime.now(_KST).strftime("%H:%M:%S")
import config

# ── 모듈 레벨 상태 ────────────────────────────────────────────
_prev_snapshot:      dict[str, dict] = {}
_confirm_count:      dict[str, int]  = {}
_ws_alerted_tickers: set[str]        = set()


# ── 호가 분석 (v4.0 신규) ─────────────────────────────────────

def analyze_orderbook(orderbook: dict) -> dict | None:
    """
    [v4.0 신규] KIS 호가잔량 분석 → 매수/매도 강도 판단

    입력: rest_client.get_orderbook() 또는 websocket_client._parse_orderbook() 반환값
    출력:
    {
        "매수매도비율": float,  총매수잔량 / 총매도잔량
        "상위3집중도":  float,  bids[0~2] 잔량합 / 총매수잔량
        "호가강도":     str,    "강세" | "중립" | "약세"
        "매수잔량":     int,
        "매도잔량":     int,
    }

    [판단 기준]
    강세: 매수매도비율 >= ORDERBOOK_BID_ASK_GOOD(2.0)
          또는 매수매도비율 >= ORDERBOOK_BID_ASK_MIN(1.3) AND 상위3집중도 낮음(매도벽 얕음)
    약세: 매수매도비율 < 0.8 (매도 우세)
    중립: 그 외
    """
    if not orderbook:
        return None

    total_bid = orderbook.get("총매수잔량", 0)
    total_ask = orderbook.get("총매도잔량", 0)

    if total_ask <= 0:
        return None

    bid_ask_ratio = total_bid / total_ask

    # 상위 3 매수호가 집중도 (얕은 매도벽 = 돌파 쉬움)
    bids = orderbook.get("매수호가", [])
    top3_bid_vol = sum(b["잔량"] for b in bids[:3]) if bids else 0
    top3_ratio   = (top3_bid_vol / total_bid) if total_bid > 0 else 0.0

    # 매도벽 얕음: 상위3 매도호가 집중도가 낮으면 분산 → 뚫기 어려움
    # 상위3 매도 집중도 계산
    asks = orderbook.get("매도호가", [])
    top3_ask_vol  = sum(a["잔량"] for a in asks[:3]) if asks else 0
    top3_ask_ratio = (top3_ask_vol / total_ask) if total_ask > 0 else 0.0

    # 호가 강도 판정
    if bid_ask_ratio >= config.ORDERBOOK_BID_ASK_GOOD:
        강도 = "강세"   # 매수 압도적 우세
    elif (bid_ask_ratio >= config.ORDERBOOK_BID_ASK_MIN and
          top3_ask_ratio >= config.ORDERBOOK_TOP3_RATIO_MIN):
        강도 = "강세"   # 매수 우세 + 매도벽이 상위에 집중(돌파 가능)
    elif bid_ask_ratio < 0.8:
        강도 = "약세"   # 매도 우세 → 급등 지속 어려움
    else:
        강도 = "중립"

    logger.debug(
        f"[orderbook] 호가분석: 매수매도비율={bid_ask_ratio:.2f} "
        f"상위3집중도(매도)={top3_ask_ratio:.2f} → {강도}"
    )

    return {
        "매수매도비율": round(bid_ask_ratio, 2),
        "상위3집중도":  round(top3_ask_ratio, 2),
        "호가강도":     강도,
        "매수잔량":     total_bid,
        "매도잔량":     total_ask,
    }


# ── REST 폴링 전 종목 분석 ──────────────────────────────────

def poll_all_markets() -> list[dict]:
    """
    KIS REST 등락률 순위 API로 코스피·코스닥 조회 후
    직전 poll 대비 1분간 변화량(델타)으로 급등 조건 판단.

    [v4.1 소스 단일화] get_volume_ranking() 제거 — get_rate_ranking()만 사용
    이유: 거래량 순위는 삼성전자·현대차 등 시총 대형주가 항상 상위에 포함되어
         실질적 급등 신호가 아닌 노이즈 알림이 발생함.
         등락률 순위는 코스피 중형+소형, 코스닥 전체(스팩·ETF 제외) 기준이므로
         대형주 필터가 이미 적용되어 있음.

    [v4.0] 조건 충족 종목: get_orderbook() 즉시 호출 → 호가 분석 결과 포함
    소~중형주 필터는 rest_client.get_rate_ranking()에서 처리

    [v3.8 필터 체인 순서]
    1. 누적 등락률 < MIN_CHANGE_RATE(3%) → 스킵
    2. 누적 등락률 > MAX_CATCH_RATE(12%) → 스킵 (뒷북 방지)
    3. 장중 거래대금 < MIN_TRADE_AMOUNT(30억) → 스킵
    4. 전일거래량 < MIN_PREV_VOL(5만) → 스킵
    5. 누적RVOL < MIN_VOL_RATIO_ACML(30%) → 스킵
    6. 순간Δ등락률 ≥ PRICE_DELTA_MIN AND 순간강도 ≥ VOLUME_DELTA_MIN → 카운터 증가
    7. CONFIRM_CANDLES회 연속 → 알림 발송 + 카운터 초기화
    8. [v4.0] 알림 대상 종목 → REST 호가 조회 → analyze_orderbook() 주입
    """
    global _prev_snapshot
    from kis.rest_client import get_rate_ranking, get_orderbook

    current_snapshot: dict[str, dict] = {}
    alerted:          list[dict]      = []
    is_warmup = not _prev_snapshot

    for market_code in ["J", "Q"]:
        market_name = "코스피" if market_code == "J" else "코스닥"

        # [v4.1] 등락률 순위만 사용 — 거래량 순위 제거 (대형주 노이즈 차단)
        rate_rows = get_rate_ranking(market_code)
        rows = [{**row, "_source": "rate"} for row in rate_rows]

        if not rows:
            logger.debug(f"[volume] {market_name} 순위 없음")
            continue
        logger.info(
            f"[volume] {market_name} 등락률 순위: {len(rows)}종목 (소~중형 필터 적용)"
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

            change_rate = row["등락률"]
            if change_rate < config.MIN_CHANGE_RATE:
                continue
            if change_rate > config.MAX_CATCH_RATE:
                continue

            trade_amount = row["누적거래량"] * curr_price
            if trade_amount < config.MIN_TRADE_AMOUNT:
                continue

            prdy_vol = row["전일거래량"]
            if prdy_vol < config.MIN_PREV_VOL:
                continue

            acml_vol  = row["누적거래량"]
            acml_rvol = (acml_vol / prdy_vol * 100) if prdy_vol > 0 else 0.0
            if acml_rvol < config.MIN_VOL_RATIO_ACML:
                continue

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
                _confirm_count[ticker] = 0

                # [v4.0] 호가 분석 — 조건 충족 종목에 한해 REST 1회 호출
                호가분석 = None
                if config.ORDERBOOK_ENABLED:
                    ob_data = get_orderbook(ticker)
                    호가분석 = analyze_orderbook(ob_data)
                    if 호가분석:
                        logger.info(
                            f"[volume] {row['종목명']} 호가강도={호가분석['호가강도']} "
                            f"매수매도비율={호가분석['매수매도비율']:.2f}"
                        )

                alerted.append({
                    "종목코드":   ticker,
                    "종목명":     row["종목명"],
                    "등락률":     change_rate,
                    "직전대비":   round(delta_rate, 2),
                    "거래량배율": round(acml_rvol / 100, 2),
                    "순간강도":   round(순간강도, 1),
                    "조건충족":   True,
                    "감지시각":   _now_kst(),
                    "감지소스":   row.get("_source", "volume"),
                    "호가분석":   호가분석,    # v4.0 신규
                })

    # ── [T2] 갭 상승 모멘텀 감지 (v3.2) ─────────────────────────
    gap_alerted = _detect_gap_up(current_snapshot, alerted)
    alerted.extend(gap_alerted)

    _prev_snapshot = current_snapshot

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

_gap_alerted: set[str] = set()


def _detect_gap_up(snapshot: dict[str, dict], already_alerted: list[dict]) -> list[dict]:
    """
    [T2] 갭 상승 모멘텀 감지
    - change_rate > GAP_UP_MIN × 2
    - [v3.8] change_rate ≤ MAX_CATCH_RATE
    - [v4.0] 호가 분석 포함 (ORDERBOOK_ENABLED=True 시)
    """
    from kis.rest_client import get_orderbook

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
        if change_rate > config.MAX_CATCH_RATE:
            continue

        prdy_vol = row.get("전일거래량", 1)
        if prdy_vol < config.MIN_PREV_VOL:
            continue

        trade_amount = row.get("누적거래량", 0) * curr_price
        if trade_amount < config.MIN_TRADE_AMOUNT:
            continue

        _gap_alerted.add(ticker)
        acml_vol  = row.get("누적거래량", 0)
        acml_rvol = (acml_vol / prdy_vol) if prdy_vol > 0 else 0.0

        # [v4.0] 호가 분석
        호가분석 = None
        if config.ORDERBOOK_ENABLED:
            ob_data = get_orderbook(ticker)
            호가분석 = analyze_orderbook(ob_data)

        results.append({
            "종목코드":   ticker,
            "종목명":     row.get("종목명", ticker),
            "등락률":     change_rate,
            "직전대비":   0.0,
            "거래량배율": round(acml_rvol, 2),
            "순간강도":   0.0,
            "조건충족":   True,
            "감지시각":   _now_kst(),
            "감지소스":   "gap_up",
            "호가분석":   호가분석,    # v4.0 신규
        })
        logger.info(
            f"[volume] T2 갭상승 감지: {row.get('종목명', ticker)} "
            f"+{change_rate:.1f}% (갭업추정, RVOL {acml_rvol:.1f}x)"
        )

    return results


# ── WebSocket 체결 틱 기반 분석 (v3.1 신규 — 방법 B) ──────────

def analyze_ws_tick(tick: dict, prdy_vol: int) -> dict | None:
    """
    KIS WebSocket 실시간 체결 틱 → 급등 조건 판단 (v3.1)

    [v4.0] 호가 분석은 REST 호출이 필요하므로 여기서 직접 수행하지 않음.
    realtime_alert._ws_loop()의 on_tick() 콜백에서 호가 분석을 추가로 수행.
    → 이 함수의 반환값에 "호가분석": None 포함, realtime_alert에서 채움.
    """
    rate = tick.get("등락률", 0.0)

    if rate < config.PRICE_CHANGE_MIN:
        return None
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
        "거래량배율": round(acml_rvol, 2),
        "순간강도":   0.0,
        "조건충족":   True,
        "감지시각":   체결시각 or _now_kst(),
        "감지소스":   "websocket",
        "호가분석":   None,   # v4.0: realtime_alert.on_tick()에서 REST 호가 조회 후 채움
    }


def analyze_ws_orderbook_tick(ob: dict, existing_result: dict) -> dict:
    """
    [v4.0 신규] WebSocket 실시간 호가 틱 → 기존 분석 결과에 호가분석 보강

    WS_ORDERBOOK_ENABLED=true 시 realtime_alert._ws_loop()의 on_orderbook() 에서 호출.
    WebSocket 호가 틱을 REST get_orderbook() 반환값 형식으로 변환 후 analyze_orderbook() 통과.

    ob: websocket_client._parse_orderbook() 반환값
    existing_result: analyze_ws_tick() 반환값 (호가분석=None인 상태)
    → 호가분석 채워서 새 dict 반환
    """
    if not ob:
        return existing_result

    # WebSocket 호가 틱은 REST 호가 포맷과 동일하게 변환됨
    호가분석 = analyze_orderbook(ob)
    return {**existing_result, "호가분석": 호가분석}


# ── 레거시 보존 (향후 확장용) ────────────────────────────────

def analyze(tick: dict) -> dict:
    """KIS WebSocket 틱 → 급등 조건 판단 (향후 WebSocket 재활성화용 보존)"""
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
        "감지시각":   _now_kst(),
        "감지소스":   "volume",
        "호가분석":   None,
    }


def reset() -> None:
    """장 마감(15:30) 후 상태 전체 초기화"""
    global _prev_snapshot
    _prev_snapshot = {}
    _confirm_count.clear()
    _ws_alerted_tickers.clear()
    _gap_alerted.clear()
    logger.info("[volume] 스냅샷·확인카운터·WS상태·갭상승상태 초기화 완료")
