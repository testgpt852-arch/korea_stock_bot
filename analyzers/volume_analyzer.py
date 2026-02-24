"""
analyzers/volume_analyzer.py
장중 급등 감지 전담 (KIS WebSocket 연동)

[버그 수정 v1.3]
- CONFIRM_CANDLES가 config에 있었으나 실제로 사용되지 않던 문제 수정
- 연속 N틱 조건 충족 시만 알림 발생 (허위 신호 감소)

반환값 규격 (ARCHITECTURE.md 계약):
{"종목코드": str, "종목명": str, "등락률": float,
 "거래량배율": float, "조건충족": bool, "감지시각": str}
"""

from datetime import datetime
from utils.logger import logger
import config

# 전일 거래량 캐시
_prev_volume:  dict[str, int] = {}   # {종목코드: 전일거래량}
_today_volume: dict[str, int] = {}   # {종목코드: 오늘누적거래량}
_ticker_names: dict[str, str] = {}   # {종목코드: 종목명}

# [수정] CONFIRM_CANDLES 구현: 연속 충족 카운터
_confirm_count: dict[str, int] = {}  # {종목코드: 연속충족횟수}


# ── 초기화 ────────────────────────────────────────────────────

def init_prev_volumes(date_str: str) -> None:
    """
    장 시작(09:00) 전 전일 거래량 로딩
    realtime_alert.start()에서 1회 호출
    """
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


# ── 핵심 분석 ──────────────────────────────────────────────────

def analyze(tick: dict) -> dict:
    """
    실시간 틱 데이터 → 급등 조건 판단

    [개선] CONFIRM_CANDLES: 연속 N틱 조건 충족 시만 조건충족=True
    → 일시적 스파이크로 인한 허위 알림 방지

    반환: dict (ARCHITECTURE.md 계약)
    """
    ticker = tick.get("종목코드", "")
    rate   = tick.get("등락률",   0.0)
    volume = tick.get("거래량",   0)

    # 오늘 누적 거래량 업데이트
    _today_volume[ticker] = _today_volume.get(ticker, 0) + volume

    prev_vol     = _prev_volume.get(ticker, 0)
    today_vol    = _today_volume[ticker]
    volume_ratio = (today_vol / prev_vol * 100) if prev_vol > 0 else 0.0

    # 단일 틱 조건 충족 여부
    single_ok = (
        rate         >= config.PRICE_CHANGE_MIN and
        volume_ratio >= config.VOLUME_SPIKE_RATIO
    )

    # [수정] 연속 충족 카운터 업데이트
    if single_ok:
        _confirm_count[ticker] = _confirm_count.get(ticker, 0) + 1
    else:
        # 조건 미충족 시 카운터 초기화
        _confirm_count[ticker] = 0

    # CONFIRM_CANDLES번 연속 충족해야 최종 True
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
