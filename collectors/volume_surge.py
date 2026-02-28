"""
collectors/volume_surge.py  [v12.0]
[v12.0] 거래량급증 수집 (pykrx) (v3.2 신규)

주가는 ±{VOLUME_FLAT_CHANGE_MAX}% 이내인데 거래량이 전일 대비 50%↑ 급증하는 종목.
→ 세력이 조용히 매집하는 패턴: 주로 이튿날 급등의 전조신호
(prism-insight trigger_afternoon_volume_surge_flat 참조)

[실행 시점 — v12.0 변경]
data_collector.run() (06:00) 에서 asyncio.gather() 병렬 수집 중 호출.
기존: 마감봇(closing_report.py, 18:30) → v12.0에서 마감봇 폐지, 06:00으로 이전.
데이터 소스: pykrx 마감 확정치 (일별 OHLCV — 전날 데이터)

[반환값 규격]
list[dict]: 횡보 거래량 급증 상위 종목 목록
  {
    "종목코드":     str,
    "종목명":       str,
    "등락률":       float,   # 당일 등락률 (%)  — ±N% 이내
    "거래량증가율": float,   # 전일 대비 거래량 증가율 (%)  — 50%↑
    "거래량":       int,
    "전일거래량":   int,
    "종가":         int,
  }

[ARCHITECTURE 의존성]
collectors/volume_surge → collectors/data_collector (병렬 수집 호출)
결과: data_collector 캐시["volume_surge_result"] → morning_analyzer._pick_stocks()
"""

from datetime import datetime, timedelta
from utils.logger import logger
from utils.date_utils import get_prev_trading_day  # v3.2 수정: 영업일 보장
from pykrx import stock as pykrx_stock
import config


def analyze(date_str: str, top_n: int = None) -> list[dict]:
    """
    횡보 거래량 급증 종목 분석

    Args:
        date_str: 분석 기준일 (YYYYMMDD)
        top_n:    상위 N 종목 반환 (기본값 config.VOLUME_FLAT_TOP_N)

    Returns:
        거래량 증가율 내림차순 정렬된 횡보 급증 종목 list[dict]
    """
    if top_n is None:
        top_n = config.VOLUME_FLAT_TOP_N

    logger.info(f"[volume_surge] 거래량급증 분석 시작 — {date_str}")

    prev_date = _get_prev_date(date_str)
    results   = []

    for market in ["KOSPI", "KOSDAQ"]:
        try:
            df_today = pykrx_stock.get_market_ohlcv_by_ticker(date_str, market=market)
            if df_today.empty:
                continue

            df_prev = pykrx_stock.get_market_ohlcv_by_ticker(prev_date, market=market)
            if df_prev.empty:
                logger.warning(f"[volume_surge] {market} 전일 데이터 없음 — 전일 데이터 조회 실패")
                continue

            common = df_today.index.intersection(df_prev.index)
            for ticker in common:
                row_today = df_today.loc[ticker]
                row_prev  = df_prev.loc[ticker]

                change_rate = float(row_today.get("등락률", 0))
                vol_today   = int(row_today.get("거래량", 0))
                vol_prev    = int(row_prev.get("거래량", 0))

                if vol_prev <= 0 or vol_today <= 0:
                    continue

                # 횡보 조건: 등락률 절대값 ≤ VOLUME_FLAT_CHANGE_MAX
                if abs(change_rate) > config.VOLUME_FLAT_CHANGE_MAX:
                    continue

                # 거래량 급증 조건: 전일 대비 VOLUME_FLAT_SURGE_MIN% 이상
                vol_increase = (vol_today - vol_prev) / vol_prev * 100
                if vol_increase < config.VOLUME_FLAT_SURGE_MIN:
                    continue

                name = pykrx_stock.get_market_ticker_name(ticker)
                results.append({
                    "종목코드":     ticker,
                    "종목명":       name or ticker,
                    "등락률":       change_rate,
                    "거래량증가율": round(vol_increase, 1),
                    "거래량":       vol_today,
                    "전일거래량":   vol_prev,
                    "종가":         int(float(row_today.get("종가", 0))),
                })

        except Exception as e:
            logger.warning(f"[volume_surge] {market} 조회 실패: {e}")
            continue

    # 거래량 증가율 내림차순 정렬
    results.sort(key=lambda x: x["거래량증가율"], reverse=True)
    top = results[:top_n]

    logger.info(
        f"[volume_surge] 거래량급증 수집 완료 — "
        f"횡보(±{config.VOLUME_FLAT_CHANGE_MAX}%이내) + "
        f"거래량{config.VOLUME_FLAT_SURGE_MIN}%↑ 총 {len(results)}종목 중 "
        f"상위 {len(top)}종목 선별"
    )
    return top


def _get_prev_date(date_str: str) -> str:
    """
    YYYYMMDD 기준 직전 영업일 반환 (주말 대응)

    [v3.2 버그 수정]
    기존: timedelta(days=1) → 월요일이면 일요일 반환 → pykrx 빈 DataFrame
    → volume_surge_result: 시장 전체 스킵 발생
    수정: get_prev_trading_day()로 실제 직전 영업일 계산
    """
    dt   = datetime.strptime(date_str, "%Y%m%d")
    prev = get_prev_trading_day(dt)
    if prev is None:
        prev = dt - timedelta(days=1)
    return prev.strftime("%Y%m%d")