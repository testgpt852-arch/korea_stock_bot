"""
collectors/closing_strength.py  [v12.0]
[마감강도] 마감 강도 트리거 분석기 (v3.2 신규)

마감 강도 = (종가 - 저가) / (고가 - 저가)
  → 값이 1에 가까울수록 윗꼬리 없이 고가 근처에서 마감 (강봉)
  → 내일 추가 상승 확률 높음 (prism-insight trigger_afternoon_closing_strength 참조)

[실행 시점 — v12.0 변경]
data_collector.run() (06:00) 에서 asyncio.gather() 병렬 수집 중 호출.
기존: 마감봇(closing_report.py, 18:30) → v12.0에서 마감봇 폐지, 06:00으로 이전.
데이터 소스: pykrx 마감 확정치 (일별 OHLCV — 전날 데이터)
일별 확정 데이터 → pykrx 사용 허가 (ARCHITECTURE.md 규칙 준수)

[반환값 규격]
list[dict]: 마감 강도 상위 종목 목록
  {
    "종목코드":    str,
    "종목명":      str,
    "마감강도":    float,   # 0.0 ~ 1.0 (클수록 강봉)
    "등락률":      float,   # 당일 등락률 (%)
    "거래량증가율": float,  # 전일 대비 거래량 증가율 (%)
    "종가":        int,
    "고가":        int,
    "저가":        int,
  }

[ARCHITECTURE 의존성]
collectors/closing_strength → collectors/data_collector (병렬 수집 호출)
결과: data_collector 캐시["closing_strength_result"] → morning_analyzer._pick_stocks()
"""

from datetime import datetime, timedelta
from utils.logger import logger
from utils.date_utils import get_prev_trading_day  # v3.2 수정: 영업일 보장
from pykrx import stock as pykrx_stock
import config


def analyze(date_str: str, top_n: int = None) -> list[dict]:
    """
    마감 강도 상위 종목 분석

    Args:
        date_str: 분석 기준일 (YYYYMMDD, 마감 확정 후)
        top_n:    상위 N 종목 반환 (기본값 config.CLOSING_STRENGTH_TOP_N)

    Returns:
        마감 강도 상위 종목 list[dict] — 마감 강도 내림차순 정렬
    """
    if top_n is None:
        top_n = config.CLOSING_STRENGTH_TOP_N

    logger.info(f"[마감강도] 마감강도 분석 시작 — {date_str}")

    # 전일 데이터 조회 (거래량 증가율 계산용)
    prev_date = _get_prev_date(date_str)

    results = []
    for market in ["KOSPI", "KOSDAQ"]:
        try:
            df_today = pykrx_stock.get_market_ohlcv_by_ticker(date_str, market=market)
            if df_today.empty:
                continue

            # 전일 거래량 조회
            df_prev = pykrx_stock.get_market_ohlcv_by_ticker(prev_date, market=market)

            for ticker in df_today.index:
                row = df_today.loc[ticker]
                high  = float(row.get("고가", 0))
                low   = float(row.get("저가", 0))
                close = float(row.get("종가", 0))
                vol   = int(row.get("거래량", 0))

                if high <= low or close <= 0:
                    continue

                # 마감 강도 계산: (종가 - 저가) / (고가 - 저가)
                strength = (close - low) / (high - low)
                if strength < config.CLOSING_STRENGTH_MIN:
                    continue

                # 거래량 증가율
                vol_ratio = 0.0
                if not df_prev.empty and ticker in df_prev.index:
                    prev_vol = int(df_prev.loc[ticker].get("거래량", 0))
                    if prev_vol > 0:
                        vol_ratio = (vol - prev_vol) / prev_vol * 100

                # 등락률
                change_rate = float(row.get("등락률", 0))

                name = pykrx_stock.get_market_ticker_name(ticker)
                results.append({
                    "종목코드":     ticker,
                    "종목명":       name or ticker,
                    "마감강도":     round(strength, 3),
                    "등락률":       change_rate,
                    "거래량증가율": round(vol_ratio, 1),
                    "종가":         int(close),
                    "고가":         int(high),
                    "저가":         int(low),
                })

        except Exception as e:
            logger.warning(f"[closing_strength] {market} 조회 실패: {e}")
            continue

    # 마감 강도 내림차순 정렬 + 상위 N개
    results.sort(key=lambda x: x["마감강도"], reverse=True)
    top = results[:top_n]

    logger.info(
        f"[closing_strength] 마감강도 수집 완료 — "
        f"마감강도 {config.CLOSING_STRENGTH_MIN}↑ 총 {len(results)}종목 중 "
        f"상위 {len(top)}종목 선별"
    )
    return top


def _get_prev_date(date_str: str) -> str:
    """
    YYYYMMDD 기준 직전 영업일 반환 (주말 대응)

    [v3.2 버그 수정]
    기존: timedelta(days=1) → 월요일이면 일요일 반환 → pykrx 빈 DataFrame
    → 월요일 vol_ratio = 0.0 (데이터 오염) 또는 시장 전체 스킵 발생
    수정: get_prev_trading_day()로 실제 직전 영업일 계산
    """
    dt   = datetime.strptime(date_str, "%Y%m%d")
    prev = get_prev_trading_day(dt)
    if prev is None:
        # 토/일이 입력된 경우(정상 운영에선 발생 안 함) — 안전 fallback
        prev = dt - timedelta(days=1)
    return prev.strftime("%Y%m%d")
