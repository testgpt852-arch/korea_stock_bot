"""
collectors/price_collector.py
당일 주가·거래량·기관/외인·공매도 수집 전담 (pykrx 기반)
- 분석 로직 없음, 수집만 (ARCHITECTURE 규칙)

[사용 pykrx 함수 — 실존 확인된 것만]
  get_index_ohlcv_by_date(fromdate, todate, index_code)
  get_market_ohlcv_by_ticker(date, market=market)
  get_market_trading_value_by_date(fromdate, todate, ticker)  ← 기관/외인 (개별종목)
  get_market_short_ohlcv_by_date(fromdate, todate, ticker)   ← 공매도 (개별종목)
"""

from datetime import datetime, timedelta
from pykrx import stock as pykrx_stock
from utils.logger import logger
from utils.date_utils import fmt_ymd, get_today
import config


# ── 공개 인터페이스 ───────────────────────────────────────────

def collect_daily(target_date: datetime = None) -> dict:
    """
    당일 전종목 데이터 수집 (마감봇 메인 수집 함수)

    반환: dict {
        "date":         str,
        "kospi":        dict,   {"close": float, "change_rate": float}
        "kosdaq":       dict,
        "upper_limit":  list,   상한가 종목
        "top_gainers":  list,   급등(7%↑) 상위 20개
        "top_losers":   list,   급락(-7%↓) 상위 10개
        "institutional":list,   기관/외인 순매수 상위
        "short_selling":list,   공매도 상위
        "by_name":      dict,   {종목명: entry}  ← theme_analyzer용
        "by_code":      dict,   {종목코드: entry}
    }
    """
    if target_date is None:
        target_date = get_today()

    date_str = fmt_ymd(target_date)
    logger.info(f"[price] {date_str} 가격·수급 데이터 수집 시작")

    result = {
        "date":          date_str,
        "kospi":         {},
        "kosdaq":        {},
        "upper_limit":   [],
        "top_gainers":   [],
        "top_losers":    [],
        "institutional": [],
        "short_selling": [],
        "by_name":       {},
        "by_code":       {},
    }

    # ── 1. 지수 수집 ──────────────────────────────────────────
    result["kospi"]  = _fetch_index(date_str, "1001", "KOSPI")
    result["kosdaq"] = _fetch_index(date_str, "2001", "KOSDAQ")

    # ── 2. 전종목 등락률 수집 ─────────────────────────────────
    all_stocks = _fetch_all_stocks(date_str)
    result["by_code"]     = all_stocks
    result["by_name"]     = {v["종목명"]: v for v in all_stocks.values() if v["종목명"]}
    result["upper_limit"] = sorted(
        [v for v in all_stocks.values() if v["등락률"] >= 29.0],
        key=lambda x: x["등락률"], reverse=True
    )
    result["top_gainers"] = sorted(
        [v for v in all_stocks.values() if 7.0 <= v["등락률"] < 29.0],
        key=lambda x: x["등락률"], reverse=True
    )[:20]
    result["top_losers"]  = sorted(
        [v for v in all_stocks.values() if v["등락률"] <= -7.0],
        key=lambda x: x["등락률"]
    )[:10]

    logger.info(
        f"[price] 전종목 완료 — "
        f"상한가:{len(result['upper_limit'])}개  "
        f"급등:{len(result['top_gainers'])}개  "
        f"급락:{len(result['top_losers'])}개"
    )

    # ── 3. 기관/외인 순매수 (상위 급등 종목 개별 조회) ─────────
    # pykrx에 시장 전체 투자자별 ticker 조회 함수가 버전에 따라 없음
    # → 상한가 + 급등 상위 종목(최대 30개)에 대해 개별 조회로 대체
    top_tickers = [s["종목코드"] for s in result["upper_limit"][:15]] + \
                  [s["종목코드"] for s in result["top_gainers"][:15]]
    result["institutional"] = _fetch_institutional_by_tickers(
        date_str, top_tickers
    )

    # ── 4. 공매도 (상위 급등 종목 개별 조회) ───────────────────
    result["short_selling"] = _fetch_short_selling(date_str, top_tickers)

    return result


def collect_supply(ticker: str, target_date: datetime = None) -> dict:
    """
    개별 종목 수급 데이터 (장중봇 4단계에서 호출)

    반환: dict {
        "종목코드":        str,
        "기관_5일순매수":  int,
        "외국인_5일순매수":int,
        "공매도잔고율":    float,
        "대차잔고":        int,
    }
    """
    if target_date is None:
        target_date = get_today()

    date_str  = fmt_ymd(target_date)
    start_str = fmt_ymd(target_date - timedelta(days=config.INSTITUTION_DAYS * 2))

    result = {
        "종목코드":         ticker,
        "기관_5일순매수":   0,
        "외국인_5일순매수": 0,
        "공매도잔고율":     0.0,
        "대차잔고":         0,
    }

    try:
        df = pykrx_stock.get_market_trading_value_by_date(
            start_str, date_str, ticker
        )
        if not df.empty:
            inst_col = _find_col(df, ["기관합계", "기관"])
            frgn_col = _find_col(df, ["외국인합계", "외국인"])
            tail = df.tail(config.INSTITUTION_DAYS)
            if inst_col:
                result["기관_5일순매수"]   = int(tail[inst_col].sum())
            if frgn_col:
                result["외국인_5일순매수"] = int(tail[frgn_col].sum())
    except Exception as e:
        logger.warning(f"[price] {ticker} 기관/외인 수급 실패: {e}")

    try:
        df = pykrx_stock.get_market_short_ohlcv_by_date(start_str, date_str, ticker)
        if not df.empty:
            ratio_col = _find_col(df, ["공매도비중", "비중"])
            bal_col   = _find_col(df, ["대차잔고", "잔고"])
            last = df.iloc[-1]
            if ratio_col:
                result["공매도잔고율"] = float(last[ratio_col])
            if bal_col:
                result["대차잔고"] = int(last[bal_col])
    except Exception as e:
        logger.warning(f"[price] {ticker} 공매도 수집 실패: {e}")

    return result


# ── 내부 헬퍼 ─────────────────────────────────────────────────

def _fetch_index(date_str: str, index_code: str, name: str) -> dict:
    """지수 OHLCV 수집"""
    try:
        df = pykrx_stock.get_index_ohlcv_by_date(date_str, date_str, index_code)
        if df.empty:
            logger.warning(f"[price] {name} 지수 없음 (휴장 또는 데이터 없음)")
            return {}
        row         = df.iloc[-1]
        close       = float(row.get("종가", 0))
        change_rate = float(row.get("등락률", 0))
        logger.info(f"[price] {name}: {close:,.2f} ({change_rate:+.2f}%)")
        return {"close": close, "change_rate": change_rate}
    except Exception as e:
        logger.warning(f"[price] {name} 지수 수집 실패: {e}")
        return {}


def _fetch_all_stocks(date_str: str) -> dict:
    """전종목 OHLCV + 등락률 수집 → {종목코드: entry}"""
    all_stocks = {}
    for market in ["KOSPI", "KOSDAQ"]:
        try:
            df = pykrx_stock.get_market_ohlcv_by_ticker(date_str, market=market)
            if df.empty:
                continue
            for ticker in df.index:
                row  = df.loc[ticker]
                name = pykrx_stock.get_market_ticker_name(ticker)
                all_stocks[ticker] = {
                    "종목코드": ticker,
                    "종목명":   name or ticker,
                    "등락률":   float(row.get("등락률", 0)),
                    "거래량":   int(row.get("거래량", 0)),
                    "종가":     float(row.get("종가", 0)),
                    "시장":     market,
                }
        except Exception as e:
            logger.warning(f"[price] {market} 전종목 수집 실패: {e}")
    return all_stocks


def _fetch_institutional_by_tickers(
    date_str: str, tickers: list[str], top_n: int = 10
) -> list[dict]:
    """
    기관/외인 순매수 — 개별 종목 조회 방식
    pykrx 확인 함수: get_market_trading_value_by_date(fromdate, todate, ticker)
    반환 컬럼: 기관합계, 외국인합계, 개인, 기타법인 등
    """
    results = []
    for ticker in tickers:
        try:
            df = pykrx_stock.get_market_trading_value_by_date(
                date_str, date_str, ticker
            )
            if df.empty:
                continue
            row      = df.iloc[-1]
            inst_col = _find_col(df, ["기관합계", "기관"])
            frgn_col = _find_col(df, ["외국인합계", "외국인"])

            # 컬럼 없으면 최초 1회만 경고
            if not inst_col and not frgn_col:
                logger.warning(
                    f"[price] 기관/외인 컬럼 없음 "
                    f"(실제컬럼: {df.columns.tolist()}) — "
                    f"pykrx 버전 이슈 가능성"
                )
                break  # 다른 종목도 동일할 것이므로 중단

            inst_val = int(row[inst_col]) if inst_col else 0
            frgn_val = int(row[frgn_col]) if frgn_col else 0

            name = pykrx_stock.get_market_ticker_name(ticker)
            results.append({
                "종목코드":     ticker,
                "종목명":       name or ticker,
                "기관순매수":   inst_val,
                "외국인순매수": frgn_val,
            })
        except Exception as e:
            logger.debug(f"[price] {ticker} 기관/외인 실패: {e}")
            continue

    results.sort(key=lambda x: x["기관순매수"], reverse=True)
    logger.info(f"[price] 기관/외인 수집 완료 — {len(results)}종목")
    return results[:top_n]


def _fetch_short_selling(
    date_str: str, tickers: list[str], top_n: int = 10
) -> list[dict]:
    """
    공매도 비중 — 개별 종목 조회
    pykrx 확인 함수: get_market_short_ohlcv_by_date(fromdate, todate, ticker)
    """
    results = []
    for ticker in tickers:
        try:
            df = pykrx_stock.get_market_short_ohlcv_by_date(
                date_str, date_str, ticker
            )
            if df.empty:
                continue
            row       = df.iloc[-1]
            ratio_col = _find_col(df, ["공매도비중", "비중"])
            vol_col   = _find_col(df, ["공매도거래량", "거래량"])
            ratio_val = float(row[ratio_col]) if ratio_col else 0.0
            vol_val   = int(row[vol_col])     if vol_col   else 0
            if ratio_val > 0:
                name = pykrx_stock.get_market_ticker_name(ticker)
                results.append({
                    "종목코드":     ticker,
                    "종목명":       name or ticker,
                    "공매도잔고율": ratio_val,
                    "공매도거래량": vol_val,
                })
        except Exception:
            continue

    results.sort(key=lambda x: x["공매도잔고율"], reverse=True)
    logger.info(f"[price] 공매도 수집 완료 — {len(results)}종목")
    return results[:top_n]


def _find_col(df, candidates: list[str]) -> str | None:
    """DataFrame에서 후보 컬럼명 중 존재하는 것 반환"""
    for c in candidates:
        if c in df.columns:
            return c
    return None
