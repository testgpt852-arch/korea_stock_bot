"""
collectors/market_global.py
미국증시 + 원자재 + 섹터 ETF 데이터 수집 전담

[수정이력]
- v1.0: 네이버 크롤링 (차단 문제)
- v1.1: yfinance 교체
- v1.2: 다우 티커 fallback 추가(^DJI→DIA), summary 길이제한 제거
- v2.1: 섹터 ETF 수집 추가 (XLK/XLE/XLB/XLI/XLV/XLF)
        → signal_analyzer에서 국내 연동 테마 신호로 변환됨
- v10.0: 철강/비철 원자재 수집 추가
        COMMODITY_TICKERS에 TIO=F(철광석), ALI=F(알루미늄) 추가
        US_SECTOR_TICKERS 확장은 config.py 참조 (XME, SLX)
"""

import yfinance as yf
import requests
from datetime import datetime
import config
from utils.logger import logger
from utils.date_utils import get_prev_trading_day, fmt_kr, get_today


US_TICKERS = {
    "nasdaq": ["^IXIC"],
    "sp500":  ["^GSPC"],
    "dow":    ["^DJI", "DIA"],
}
COMMODITY_TICKERS = {
    "copper":    "HG=F",
    "silver":    "SI=F",
    "gas":       "NG=F",
    # v10.0 Phase 1 추가 — 철강/비철 선행지표
    "steel":     "TIO=F",     # 철광석 선물 (DCE 연동, CME 표준)
    "aluminum":  "ALI=F",     # LME 알루미늄 선물
}
COMMODITY_UNITS = {
    "copper":    "$/lb",
    "silver":    "$/oz",
    "gas":       "$/MMBtu",
    "steel":     "$/MT",      # 메트릭톤
    "aluminum":  "$/MT",
}


def collect(target_date: datetime = None) -> dict:
    """
    반환: dict
    {
        "us_market": {
            "nasdaq": str, "sp500": str, "dow": str,
            "summary": str, "신뢰도": str,
            "sectors": {                          ← v2.1 추가
                "기술/반도체": {"change": str, "신뢰도": str},
                "에너지/정유":  {"change": str, "신뢰도": str},
                ...
            }
        },
        "commodities": {
            "copper": {"price": str, "change": str, "unit": str, "신뢰도": str},
            "silver": {"price": str, "change": str, "unit": str, "신뢰도": str},
            "gas":    {"price": str, "change": str, "unit": str, "신뢰도": str},
        }
    }
    """
    if target_date is None:
        target_date = get_prev_trading_day(get_today())
    if target_date is None:
        return _empty_result()

    date_kr = fmt_kr(target_date)
    logger.info(f"[market] {date_kr} 미국증시·원자재·섹터 수집 시작 (yfinance)")

    us          = _collect_us_market()
    commodities = _collect_commodities()
    us["sectors"] = _collect_sectors()          # v2.1 추가
    us["summary"] = _collect_summary(date_kr)

    return {"us_market": us, "commodities": commodities}


def _fetch_change(tickers: list) -> str:
    """티커 목록 순서대로 시도, 성공 시 등락률 반환"""
    for ticker in tickers:
        try:
            data = yf.Ticker(ticker).history(period="5d")
            if len(data) < 2:
                continue
            prev = data["Close"].iloc[-2]
            last = data["Close"].iloc[-1]
            if prev == 0:
                continue
            pct  = (last - prev) / prev * 100
            sign = "+" if pct >= 0 else ""
            return f"{sign}{pct:.2f}%"
        except Exception as e:
            logger.warning(f"[market] {ticker} 실패: {e}")
    return "N/A"


def _collect_us_market() -> dict:
    result = {"nasdaq": "N/A", "sp500": "N/A", "dow": "N/A",
              "summary": "", "신뢰도": "N/A", "sectors": {}}
    try:
        for key, tickers in US_TICKERS.items():
            result[key] = _fetch_change(tickers)
        result["신뢰도"] = "yfinance"
        logger.info(
            f"[market] 미국증시 — 나스닥:{result['nasdaq']} "
            f"S&P:{result['sp500']} 다우:{result['dow']}"
        )
    except Exception as e:
        logger.warning(f"[market] 미국증시 수집 실패: {e}")
    return result


def _collect_sectors() -> dict:
    """
    섹터 ETF 등락률 수집 (v2.1)
    config.US_SECTOR_TICKERS 기준으로 수집
    반환: {"섹터명": {"change": str, "신뢰도": str}, ...}
    """
    result = {}
    for ticker, sector_name in config.US_SECTOR_TICKERS.items():
        try:
            data = yf.Ticker(ticker).history(period="5d")
            if len(data) < 2:
                result[sector_name] = {"change": "N/A", "신뢰도": "N/A"}
                continue
            prev = data["Close"].iloc[-2]
            last = data["Close"].iloc[-1]
            if prev == 0:
                result[sector_name] = {"change": "N/A", "신뢰도": "N/A"}
                continue
            pct  = (last - prev) / prev * 100
            sign = "+" if pct >= 0 else ""
            change_str = f"{sign}{pct:.2f}%"
            result[sector_name] = {"change": change_str, "신뢰도": "yfinance"}
            logger.info(f"[market] 섹터 {sector_name} ({ticker}): {change_str}")
        except Exception as e:
            logger.warning(f"[market] 섹터 {ticker} 실패: {e}")
            result[sector_name] = {"change": "N/A", "신뢰도": "N/A"}

    # [v13.0] ±2%+ 필터 적용 — 기준 미달 섹터 제거
    min_pct = getattr(config, "US_SECTOR_SIGNAL_MIN", 2.0)
    filtered = {}
    for name, data in result.items():
        change = data.get("change", "N/A")
        if change == "N/A":
            continue
        try:
            pct = float(change.replace("%", "").replace("+", ""))
            if abs(pct) >= min_pct:
                filtered[name] = data
        except ValueError:
            continue
    return filtered


def _collect_commodities() -> dict:
    result = {}
    empty  = {"price": "N/A", "change": "N/A", "unit": "", "신뢰도": "N/A"}

    for key, ticker in COMMODITY_TICKERS.items():
        try:
            data = yf.Ticker(ticker).history(period="5d")
            if len(data) < 2:
                result[key] = dict(empty)
                continue
            prev = data["Close"].iloc[-2]
            last = data["Close"].iloc[-1]
            pct  = (last - prev) / prev * 100
            sign = "+" if pct >= 0 else ""
            result[key] = {
                "price":  f"{last:.3f}",
                "change": f"{sign}{pct:.2f}%",
                "unit":   COMMODITY_UNITS[key],
                "신뢰도": "yfinance",
            }
            logger.info(
                f"[market] {key} — {result[key]['price']} {result[key]['change']}"
            )
        except Exception as e:
            logger.warning(f"[market] {key} 실패: {e}")
            result[key] = dict(empty)

    # [v13.0] ±1.5%+ 필터 적용 — 기준 미달 원자재 제거
    min_pct = getattr(config, "COMMODITY_SIGNAL_MIN", 1.5)
    filtered = {}
    for key, data in result.items():
        change = data.get("change", "N/A")
        if change == "N/A":
            continue
        try:
            pct = float(change.replace("%", "").replace("+", ""))
            if abs(pct) >= min_pct:
                filtered[key] = data
        except ValueError:
            continue
    return filtered


def _collect_summary(date_kr: str) -> str:
    """네이버 뉴스 API로 시황 헤드라인"""
    if not config.NAVER_CLIENT_ID or not config.NAVER_CLIENT_SECRET:
        return ""
    try:
        import re
        url  = "https://openapi.naver.com/v1/search/news.json"
        hdrs = {
            "X-Naver-Client-Id":     config.NAVER_CLIENT_ID,
            "X-Naver-Client-Secret": config.NAVER_CLIENT_SECRET,
        }
        resp = requests.get(
            url, headers=hdrs,
            params={"query": f"{date_kr} 미국증시 마감", "sort": "date", "display": 3},
            timeout=8,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if items:
            return re.sub(r"<[^>]+>", "", items[0]["title"])
    except Exception as e:
        logger.warning(f"[market] 시황 요약 실패: {e}")
    return ""


def _empty_result() -> dict:
    ei = {"nasdaq": "N/A", "sp500": "N/A", "dow": "N/A",
          "summary": "", "신뢰도": "N/A", "sectors": {}}
    ec = {"price": "N/A", "change": "N/A", "unit": "", "신뢰도": "N/A"}
    return {
        "us_market": ei,
        "commodities": {"copper": dict(ec), "silver": dict(ec), "gas": dict(ec)},
    }