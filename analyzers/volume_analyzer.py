"""
analyzers/volume_analyzer.py
장중 급등 감지 전담 (pykrx REST 폴링 방식 — 전 종목 커버)

반환값 규격 (ARCHITECTURE.md 계약):
{"종목코드": str, "종목명": str, "등락률": float,
 "거래량배율": float, "조건충족": bool, "감지시각": str}

[수정이력]
- v1.3: CONFIRM_CANDLES 미사용 버그 수정 — 연속 N틱 카운터 구현
- v2.3: 거래량 필드명 불일치 + 이중 누적 버그 수정
        기존: tick.get("거래량") → 항상 0 (_parse_tick은 "누적거래량" 반환)
             _today_volume += volume → 누적거래량을 또 더해서 2배 부풀려짐
        수정: tick["누적거래량"] 직접 사용 (KIS가 이미 오늘 합산값으로 줌)
             _today_volume[ticker] = 누적거래량 (대입, 누적 아님)
        get_top_tickers() 신규 추가
- v2.4: 전 종목 커버를 위한 구조 전환
        기존: KIS WebSocket 구독 방식 (최대 100종목 한계)
        수정: poll_all_markets() 추가 — pykrx REST 폴링으로 전 종목(코스피+코스닥) 조회
             realtime_alert._poll_loop()에서 POLL_INTERVAL_SEC마다 호출
             get_top_tickers()는 호환성 유지 목적으로 존재하나 장중봇에서는 미사용
"""

from datetime import datetime
from utils.logger import logger
import config

_prev_volume:   dict[str, int] = {}
_today_volume:  dict[str, int] = {}
_ticker_names:  dict[str, str] = {}
_confirm_count: dict[str, int] = {}


# ── 초기화 ────────────────────────────────────────────────────

def init_prev_volumes(date_str: str) -> None:
    """장 시작 전 전일 거래량 로딩 (realtime_alert.start()에서 1회 호출)"""
    global _prev_volume, _ticker_names
    try:
        from pykrx import stock as pykrx_stock
        for market in ["KOSPI", "KOSDAQ"]:
            df = pykrx_stock.get_market_ohlcv_by_ticker(date_str, market=market)
            if df.empty:
                continue
            for ticker in df.index:
                vol = int(df.loc[ticker].get("거래량", 0))
                if vol > 0:
                    name = pykrx_stock.get_market_ticker_name(ticker)
                    _prev_volume[ticker]  = vol
                    _ticker_names[ticker] = name or ticker
        logger.info(f"[volume] 전일 거래량 로딩 완료 — {len(_prev_volume)}종목")
    except Exception as e:
        logger.warning(f"[volume] 전일 거래량 로딩 실패: {e}")


def get_top_tickers(n: int) -> list[str]:
    """
    전일 거래량 상위 n개 종목코드 반환 (호환성 유지용 — 장중봇에서 미사용)
    v2.4 이후 장중봇은 poll_all_markets()로 전 종목 직접 스캔
    """
    if not _prev_volume:
        return []
    sorted_tickers = sorted(_prev_volume, key=lambda t: _prev_volume[t], reverse=True)
    return sorted_tickers[:n]


# ── REST 폴링 전 종목 분석 (v2.4 핵심 추가) ──────────────────

def poll_all_markets() -> list[dict]:
    """
    pykrx REST로 KOSPI·KOSDAQ 전 종목 현재 시세 일괄 조회
    CONFIRM_CANDLES 연속 충족 종목만 list[dict]로 반환

    반환값 규격: analyze()와 동일 (ARCHITECTURE.md 계약 준수)

    [설계 근거 — v2.4]
    KIS WebSocket은 동시 구독 한도(~100종목)로 코스피+코스닥 전체 커버 불가.
    pykrx REST는 전 종목을 단 2회 API 호출로 조회 가능.
    POLL_INTERVAL_SEC(60초) 간격으로 호출 → KIS WebSocket 규칙과 무관.
    데이터 지연: pykrx 기준 약 1~2분 (장중 체결 데이터 집계 주기)
    """
    from pykrx import stock as pykrx_stock
    today_str = datetime.now().strftime("%Y%m%d")
    alerted: list[dict] = []

    for market in ["KOSPI", "KOSDAQ"]:
        try:
            df = pykrx_stock.get_market_ohlcv_by_ticker(today_str, market=market)
            if df.empty:
                logger.debug(f"[volume] {market} 조회 결과 없음 (장 시작 전 또는 휴장)")
                continue

            for ticker in df.index:
                row       = df.loc[ticker]
                cum_vol   = int(row.get("거래량",  0))
                rate      = float(row.get("등락률", 0.0))

                # 오늘 누적거래량 업데이트 (대입 — pykrx가 이미 당일 합산값 제공)
                if cum_vol > 0:
                    _today_volume[ticker] = cum_vol

                # 종목명 캐싱 (전일 거래량 로딩 시 미수록 종목 대응)
                if ticker not in _ticker_names:
                    try:
                        name = pykrx_stock.get_market_ticker_name(ticker)
                        _ticker_names[ticker] = name or ticker
                    except Exception:
                        _ticker_names[ticker] = ticker

                prev_vol     = _prev_volume.get(ticker, 0)
                today_vol    = _today_volume.get(ticker, 0)
                volume_ratio = (today_vol / prev_vol * 100) if prev_vol > 0 else 0.0

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
                        "종목명":     _ticker_names.get(ticker, ticker),
                        "등락률":     rate,
                        "거래량배율": round(volume_ratio / 100, 2),
                        "조건충족":   True,
                        "감지시각":   datetime.now().strftime("%H:%M:%S"),
                    })

        except Exception as e:
            logger.warning(f"[volume] {market} 폴링 실패: {e}")

    if alerted:
        logger.info(f"[volume] 이번 폴링 조건충족 — {len(alerted)}종목")
    return alerted


# ── WebSocket 틱 기반 분석 (KIS WebSocket 연동 시 사용 — 현재 미사용) ───

def analyze(tick: dict) -> dict:
    """
    실시간 틱 → 급등 조건 판단 (KIS WebSocket 연동용)

    v2.4: 장중봇은 poll_all_markets()로 전환.
          이 함수는 향후 WebSocket 재활성화 시를 위해 유지.

    [v2.3 수정]
    - 거래량 키: "거래량" → "누적거래량"
    - 누적 방식: += 제거 → 대입
    """
    ticker   = tick.get("종목코드", "")
    rate     = tick.get("등락률",   0.0)
    cum_vol  = tick.get("누적거래량", 0)

    if cum_vol > 0:
        _today_volume[ticker] = cum_vol

    prev_vol     = _prev_volume.get(ticker, 0)
    today_vol    = _today_volume.get(ticker, 0)
    volume_ratio = (today_vol / prev_vol * 100) if prev_vol > 0 else 0.0

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
        "종목명":     _ticker_names.get(ticker, ticker),
        "등락률":     rate,
        "거래량배율": round(volume_ratio / 100, 2),
        "조건충족":   confirmed,
        "감지시각":   datetime.now().strftime("%H:%M:%S"),
    }


def reset() -> None:
    """장 마감(15:30) 후 오늘 거래량·카운터 초기화"""
    _today_volume.clear()
    _confirm_count.clear()
    logger.info("[volume] 오늘 거래량·확인카운터 초기화 완료")
