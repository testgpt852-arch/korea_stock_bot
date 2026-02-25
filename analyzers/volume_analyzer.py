"""
analyzers/volume_analyzer.py
장중 급등 감지 전담 — KIS REST 실시간 기반

반환값 규격 (ARCHITECTURE.md 계약):
{"종목코드": str, "종목명": str, "등락률": float,
 "거래량배율": float, "조건충족": bool, "감지시각": str}

[수정이력]
- v1.3: CONFIRM_CANDLES 미사용 버그 수정 — 연속 N틱 카운터 구현
- v2.3: 거래량 필드명 불일치 + 이중 누적 버그 수정
- v2.4: poll_all_markets() 신규 — pykrx REST 전 종목 폴링
- v2.5: 데이터 소스를 pykrx → KIS REST 실시간으로 전환
        기존: pykrx get_market_ohlcv_by_ticker → 15~20분 지연, 비공식 스크래핑
        수정: rest_client.get_volume_ranking() → 실시간, KIS 공식 API
        전일 거래량 사전 로딩(init_prev_volumes) 불필요
             → KIS 응답에 prdy_vol(전일거래량) 포함 → 배율 즉시 계산
        init_prev_volumes / get_top_tickers 제거 (pykrx 의존성 완전 제거)
"""

from datetime import datetime
from utils.logger import logger
import config

_confirm_count: dict[str, int] = {}


# ── REST 폴링 전 종목 분석 ──────────────────────────────────

def poll_all_markets() -> list[dict]:
    """
    KIS REST 거래량 순위 API로 코스피·코스닥 상위 종목 조회
    CONFIRM_CANDLES 연속 충족 종목만 list[dict]로 반환

    반환값 규격: ARCHITECTURE.md 계약 준수

    [v2.5 변경]
    - pykrx 완전 제거 → KIS REST 실시간 데이터
    - KIS 응답의 prdy_vol(전일거래량) 직접 사용 → 배율 즉시 계산
    - 데이터 지연 없음 (체결 기준 실시간)
    - 코스피/코스닥 각 상위 100종목 내 급등 조건 필터링
    """
    from kis.rest_client import get_volume_ranking

    alerted: list[dict] = []

    for market_code in ["J", "Q"]:   # J=코스피, Q=코스닥
        rows = get_volume_ranking(market_code)
        if not rows:
            logger.debug(f"[volume] {'코스피' if market_code == 'J' else '코스닥'} 순위 없음")
            continue

        for row in rows:
            ticker   = row["종목코드"]
            rate     = row["등락률"]
            acml_vol = row["누적거래량"]
            prdy_vol = row["전일거래량"]

            # 거래량 배율 계산 (전일 총거래량 대비 오늘 누적거래량 %)
            volume_ratio = (acml_vol / prdy_vol * 100) if prdy_vol > 0 else 0.0

            single_ok = (
                rate         >= config.PRICE_CHANGE_MIN and
                volume_ratio >= config.VOLUME_SPIKE_RATIO
            )

            if single_ok:
                _confirm_count[ticker] = _confirm_count.get(ticker, 0) + 1
            else:
                _confirm_count[ticker] = 0

            if _confirm_count.get(ticker, 0) >= config.CONFIRM_CANDLES:
                alerted.append({
                    "종목코드":   ticker,
                    "종목명":     row["종목명"],
                    "등락률":     rate,
                    "거래량배율": round(volume_ratio / 100, 2),
                    "조건충족":   True,
                    "감지시각":   datetime.now().strftime("%H:%M:%S"),
                })

    if alerted:
        logger.info(f"[volume] 조건충족 {len(alerted)}종목")

    return alerted


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
        rate         >= config.PRICE_CHANGE_MIN and
        volume_ratio >= config.VOLUME_SPIKE_RATIO
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
        "거래량배율": round(volume_ratio / 100, 2),
        "조건충족":   confirmed,
        "감지시각":   datetime.now().strftime("%H:%M:%S"),
    }


def reset() -> None:
    """장 마감(15:30) 후 확인 카운터 초기화"""
    _confirm_count.clear()
    logger.info("[volume] 확인카운터 초기화 완료")
