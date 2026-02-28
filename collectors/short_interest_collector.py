"""
collectors/short_interest_collector.py
공매도 잔고 급감 종목 수집 전담 — KIS REST + pykrx 이중 구조

[ARCHITECTURE rule #17 — 절대 금지]
- 모든 KIS 호출은 rate_limiter.py 내부 함수 경유 필수
  (rest_client.py의 get_* 함수 사용 또는 rate_limiter.acquire() 직접 호출)

[ARCHITECTURE rule #90 계열 — 절대 금지]
- AI 분석 호출 금지 (sector_flow_analyzer.py에서 처리)
- 텔레그램 발송 금지
- DB 기록 금지
- 비치명적 처리: 데이터 부재/API 실패 시 빈 리스트 반환

[v10.0 Phase 3 신규]
수집 전략:
  1순위: pykrx get_market_short_selling_volume_by_ticker() — 공매도 거래량
  2순위: pykrx get_shorting_balance_by_date() — 공매도 잔고 (일부 종목)
  - SHORT_INTEREST_ENABLED=false(기본): 비활성화 (KIS 권한 필요)
  - 잔고 데이터 없는 종목은 'N/A' 처리 후 제외

출력 형식:
  list[dict] = [
    {
      "ticker":          str,   # 종목코드
      "name":            str,   # 종목명
      "short_volume":    int,   # 당일 공매도 거래량
      "short_volume_prev": int, # 전일 공매도 거래량
      "short_ratio":     float, # 공매도 비율 (전체 거래량 대비 %)
      "balance":         int,   # 공매도 잔고 주수 (없으면 0)
      "balance_prev":    int,   # 전주 공매도 잔고 (없으면 0)
      "balance_chg_pct": float, # 잔고 변화율 % (감소 시 음수)
      "signal":          str,   # "쇼트커버링_예고" | "공매도급감" | ""
      "신뢰도":          str,
    }
  ]

  쇼트커버링_예고 조건: balance_chg_pct <= -30% 이고 동일 섹터 3종목 이상
  공매도급감 조건:      short_volume_prev > 0 이고 당일 공매도 거래량 -50% 이상
"""

from datetime import datetime, timedelta
import config
from utils.logger import logger
from utils.date_utils import get_today, get_prev_trading_day, fmt_kr

try:
    from pykrx import stock as pykrx_stock
    _PYKRX_AVAILABLE = True
except ImportError:
    _PYKRX_AVAILABLE = False
    logger.warning("[short_interest] pykrx 미설치 — short_interest_collector 비활성화")


# 공매도 잔고 변화 임계값
_BALANCE_DROP_THRESHOLD = -30.0   # % — 이 이하면 쇼트커버링 예고
_VOLUME_DROP_THRESHOLD  = -50.0   # % — 당일 공매도 거래량 급감 기준
_SHORT_CLUSTER_MIN      = 3       # 동일 섹터 급감 종목 최소 수


def collect(target_date: datetime = None, top_n: int = 30) -> list[dict]:
    """
    공매도 거래량 급감 종목 수집.

    pykrx 데이터 기반으로 수집.
    SHORT_INTEREST_ENABLED=false이면 빈 리스트 반환.

    Args:
        target_date: 기준일 (None이면 당일)
        top_n:       수집 대상 거래량 상위 종목 수

    Returns:
        list[dict] — 공매도 급감 신호 종목 (비치명적: 실패 시 빈 리스트)
    """
    if not config.SHORT_INTEREST_ENABLED:
        logger.info("[short_interest] SHORT_INTEREST_ENABLED=false — 수집 건너뜀")
        return []

    if not _PYKRX_AVAILABLE:
        logger.warning("[short_interest] pykrx 미설치 — 빈 리스트 반환")
        return []

    if target_date is None:
        target_date = get_today()

    today_str = target_date.strftime("%Y%m%d")
    prev_date = get_prev_trading_day(target_date)
    prev_str  = prev_date.strftime("%Y%m%d") if prev_date else today_str

    # 전주 잔고 기준일 (7일 전 거래일 근사)
    week_ago = target_date - timedelta(days=7)
    week_ago_str = week_ago.strftime("%Y%m%d")

    logger.info(f"[short_interest] {fmt_kr(target_date)} 공매도 잔고 수집 시작")

    results: list[dict] = []

    try:
        results = _fetch_short_volume(today_str, prev_str, week_ago_str, top_n)
    except Exception as e:
        logger.warning(f"[short_interest] 공매도 수집 실패 (비치명적): {e}")
        return []

    # 신호 분류
    for item in results:
        item["signal"] = _classify_signal(item)

    # 신호 있는 종목만 반환, 없으면 전체 반환 (빈 리스트 방지)
    signal_items = [it for it in results if it["signal"]]
    if signal_items:
        logger.info(f"[short_interest] 공매도 급감 신호 종목: {len(signal_items)}개")
        return signal_items

    logger.info(f"[short_interest] 공매도 급감 신호 종목 없음 (전체 {len(results)}개 수집)")
    return []


def _fetch_short_volume(
    today_str: str,
    prev_str: str,
    week_ago_str: str,
    top_n: int,
) -> list[dict]:
    """
    pykrx로 공매도 거래량 조회.

    [v11.0 pykrx 호환]
    구버전 (1.0.46-): get_market_short_selling_volume_by_ticker
    신버전 (1.0.47+): get_shorting_volume_by_ticker
    → getattr 폴백으로 양쪽 모두 지원

    반환: list[dict] — 공매도 데이터 (신호 분류 전)
    """
    # 버전별 함수명 자동 탐색
    _vol_fn = getattr(pykrx_stock, "get_shorting_volume_by_ticker",
              getattr(pykrx_stock, "get_market_short_selling_volume_by_ticker", None))
    if _vol_fn is None:
        logger.warning("[short_interest] 공매도 거래량 함수 없음 — pykrx>=1.0.47 업그레이드 권장")
        return []

    results: list[dict] = []

    for market in ["KOSPI", "KOSDAQ"]:
        try:
            # 당일 공매도 거래량 상위 종목
            df_today = _vol_fn(today_str, market=market)
            df_prev  = _vol_fn(prev_str,  market=market)

            if df_today is None or df_today.empty:
                logger.debug(f"[short_interest] {market} 당일 공매도 데이터 없음")
                continue

            # 컬럼명 정규화
            vol_cols    = ["공매도수량", "ShortSellingVolume", "volume"]
            ratio_cols  = ["공매도거래대금비율", "ShortSellingRatio", "ratio"]
            name_cols   = ["종목명", "Name", "name"]

            def _col(df, keys):
                for k in keys:
                    if k in df.columns:
                        return k
                return None

            vol_col   = _col(df_today, vol_cols)
            ratio_col = _col(df_today, ratio_cols)
            name_col  = _col(df_today, name_cols)

            if not vol_col:
                logger.warning(f"[short_interest] {market} 공매도 컬럼 없음: {list(df_today.columns)}")
                continue

            # 거래량 내림차순 상위 top_n
            df_today_sorted = df_today.sort_values(vol_col, ascending=False).head(top_n)

            for ticker in df_today_sorted.index:
                try:
                    short_vol = int(df_today_sorted.loc[ticker, vol_col]) if vol_col else 0

                    short_ratio = 0.0
                    if ratio_col and ratio_col in df_today_sorted.columns:
                        short_ratio = float(df_today_sorted.loc[ticker, ratio_col])

                    name = ""
                    if name_col and name_col in df_today_sorted.columns:
                        name = str(df_today_sorted.loc[ticker, name_col])

                    # 전일 공매도 거래량
                    short_vol_prev = 0
                    if df_prev is not None and not df_prev.empty and ticker in df_prev.index:
                        prev_vol_col = _col(df_prev, vol_cols)
                        if prev_vol_col:
                            short_vol_prev = int(df_prev.loc[ticker, prev_vol_col])

                    # 잔고 조회 (실패 시 0)
                    balance, balance_prev = _fetch_balance_safe(
                        ticker, today_str, week_ago_str
                    )

                    # 잔고 변화율
                    if balance_prev > 0:
                        balance_chg_pct = (balance - balance_prev) / balance_prev * 100
                    else:
                        balance_chg_pct = 0.0

                    results.append({
                        "ticker":           str(ticker),
                        "name":             name,
                        "short_volume":     short_vol,
                        "short_volume_prev": short_vol_prev,
                        "short_ratio":      round(short_ratio, 2),
                        "balance":          balance,
                        "balance_prev":     balance_prev,
                        "balance_chg_pct":  round(balance_chg_pct, 2),
                        "signal":           "",
                        "신뢰도":            "pykrx",
                    })
                except Exception as e:
                    logger.debug(f"[short_interest] {ticker} 파싱 실패 (무시): {e}")

        except Exception as e:
            logger.warning(f"[short_interest] {market} 수집 실패 (무시): {e}")

    return results


def _fetch_balance_safe(
    ticker: str,
    today_str: str,
    week_ago_str: str,
) -> tuple[int, int]:
    """
    공매도 잔고 주수 조회 (실패 시 (0, 0) 반환).
    pykrx 잔고 데이터는 일부 종목만 제공되므로 비치명적.
    """
    try:
        df = pykrx_stock.get_shorting_balance_by_date(
            fromdate=week_ago_str,
            todate=today_str,
            ticker=ticker,
        )
        if df is None or df.empty or len(df) < 1:
            return 0, 0

        balance_cols = ["잔고", "Balance", "balance", "공매도잔고"]
        bal_col = None
        for c in balance_cols:
            if c in df.columns:
                bal_col = c
                break

        if not bal_col:
            return 0, 0

        balance_today = int(df.iloc[-1][bal_col])
        balance_prev  = int(df.iloc[0][bal_col]) if len(df) >= 2 else balance_today

        return balance_today, balance_prev

    except Exception:
        return 0, 0


def _classify_signal(item: dict) -> str:
    """
    단일 종목의 공매도 신호 분류.

    반환:
        "쇼트커버링_예고" — 잔고 -30% 이상 급감
        "공매도급감"      — 당일 공매도 거래량 -50% 이상 급감
        ""                — 신호 없음
    """
    # 잔고 급감: 전주 대비 -30% 이하
    if item["balance_prev"] > 0 and item["balance_chg_pct"] <= _BALANCE_DROP_THRESHOLD:
        return "쇼트커버링_예고"

    # 당일 공매도 거래량 급감: 전일 대비 -50% 이하
    if item["short_volume_prev"] > 100:   # 최소 100주 이상 조건
        if item["short_volume"] > 0:
            vol_chg_pct = (item["short_volume"] - item["short_volume_prev"]) / item["short_volume_prev"] * 100
            if vol_chg_pct <= _VOLUME_DROP_THRESHOLD:
                return "공매도급감"

    return ""
