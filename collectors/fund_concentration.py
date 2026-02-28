"""
collectors/fund_concentration.py  [v12.0]
[v12.0] 자금집중 수집 (pykrx) (v3.2 신규)

거래대금 / 시가총액 비율 상위 종목 감지.
시총 1000억 소형주에 100억 거래 = 시총 100조 대형주에 1조 거래와 동일 의미.
→ 대형주 중심 기존 알림의 맹점인 소형주 급등 조기 포착
(prism-insight trigger_morning_value_to_cap_ratio 참조)

[실행 시점 — v12.0 변경]
data_collector.run() (06:00) 에서 asyncio.gather() 병렬 수집 중 호출.
기존: 마감봇(closing_report.py, 18:30) → v12.0에서 마감봇 폐지, 06:00으로 이전.
데이터 소스: pykrx 마감 확정치 (일별 OHLCV + 시가총액 — 전날 데이터)

[반환값 규격]
list[dict]: 시총 대비 자금유입 상위 종목
  {
    "종목코드":       str,
    "종목명":         str,
    "등락률":         float,
    "자금유입비율":   float,  # 거래대금 / 시가총액 × 100 (%)
    "거래대금":       int,    # 원
    "시가총액":       int,    # 원
    "종가":           int,
  }

[ARCHITECTURE 의존성]
collectors/fund_concentration → collectors/data_collector (병렬 수집 호출)
결과: data_collector 캐시["fund_concentration_result"] → morning_analyzer._pick_stocks()
"""

from utils.logger import logger
from pykrx import stock as pykrx_stock
import config


def analyze(date_str: str, top_n: int = None) -> list[dict]:
    """
    시총 대비 집중 자금유입 종목 분석

    Args:
        date_str: 분석 기준일 (YYYYMMDD)
        top_n:    상위 N 종목 (기본값 config.FUND_INFLOW_TOP_N)

    Returns:
        자금유입비율 내림차순 정렬 list[dict]
    """
    if top_n is None:
        top_n = config.FUND_INFLOW_TOP_N

    logger.info(f"[fund_concentration] 자금집중 분석 시작 — {date_str}")

    results = []

    for market in ["KOSPI", "KOSDAQ"]:
        try:
            # OHLCV + 시가총액 동시 조회
            df_ohlcv = pykrx_stock.get_market_ohlcv_by_ticker(date_str, market=market)
            df_cap   = pykrx_stock.get_market_cap_by_ticker(date_str, market=market)

            if df_ohlcv.empty or df_cap.empty:
                continue

            # 공통 종목코드로 merge
            common = df_ohlcv.index.intersection(df_cap.index)
            for ticker in common:
                row_ohlcv = df_ohlcv.loc[ticker]
                row_cap   = df_cap.loc[ticker]

                # 시가총액 필드 탐색 (pykrx 버전 대응)
                cap_val = _extract_cap(row_cap)
                if cap_val < config.FUND_INFLOW_CAP_MIN:
                    continue   # 극소형주 제외

                # 거래대금 필드 탐색
                amount = _extract_amount(row_ohlcv)
                if amount <= 0:
                    continue

                ratio = amount / cap_val * 100   # 거래대금/시총 비율 (%)
                change_rate = float(row_ohlcv.get("등락률", 0))

                name = pykrx_stock.get_market_ticker_name(ticker)
                results.append({
                    "종목코드":     ticker,
                    "종목명":       name or ticker,
                    "등락률":       change_rate,
                    "자금유입비율": round(ratio, 3),
                    "거래대금":     int(amount),
                    "시가총액":     int(cap_val),
                    "종가":         int(float(row_ohlcv.get("종가", 0))),
                })

        except Exception as e:
            logger.warning(f"[fund_concentration] {market} 조회 실패: {e}")
            continue

    results.sort(key=lambda x: x["자금유입비율"], reverse=True)
    top = results[:top_n]

    logger.info(
        f"[fund_concentration] 자금집중 수집 완료 — 시총 {config.FUND_INFLOW_CAP_MIN/1e8:.0f}억원↑ "
        f"전체 {len(results)}종목 중 상위 {len(top)}종목 선별"
    )
    return top


def _extract_cap(row) -> float:
    """pykrx 버전별 시가총액 컬럼명 대응"""
    for col in ["시가총액", "Marcap", "market_cap"]:
        try:
            v = row.get(col, None) if hasattr(row, "get") else getattr(row, col, None)
            if v is not None:
                return float(v)
        except Exception:
            continue
    return 0.0


def _extract_amount(row) -> float:
    """pykrx 버전별 거래대금 컬럼명 대응"""
    for col in ["거래대금", "Amount", "amount"]:
        try:
            v = row.get(col, None) if hasattr(row, "get") else getattr(row, col, None)
            if v is not None:
                return float(v)
        except Exception:
            continue
    return 0.0
