"""
collectors/price_collector.py
당일 주가·거래량·기관/외인·공매도 수집 전담 (pykrx 기반)
- 분석 로직 없음, 수집만 (ARCHITECTURE 규칙)

[사용 pykrx 함수 — 실존 확인된 것만]
  get_index_ohlcv_by_date(fromdate, todate, index_code)
  get_market_ohlcv_by_ticker(date, market=market)
  get_market_sector_classifications(date, market)     ← 업종 분류 (v2.3 추가)
  get_market_trading_value_by_date(fromdate, todate, ticker)  ← 기관/외인
  get_market_short_ohlcv_by_date(fromdate, todate, ticker)   ← 공매도

[수정이력]
- v2.2: _fetch_index 단일날짜 조회 버그 수정 (10일 범위 조회로 변경)
- v2.3: 업종 분류 수집 추가 (_fetch_sector_map)
        by_sector 반환 추가: {업종명: [종목entry...]} 등락률 내림차순
        → signal_analyzer가 실제 시장 데이터로 섹터 대장주를 동적 판별
"""

from datetime import datetime, timedelta
from pykrx import stock as pykrx_stock
from utils.logger import logger
from utils.date_utils import fmt_ymd, get_today
import config


# ── 공개 인터페이스 ───────────────────────────────────────────

def collect_daily(target_date: datetime = None) -> dict:
    """
    당일 전종목 데이터 수집

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
        "by_sector":    dict,   {업종명: [entry...]} 등락률 내림차순  ← v2.3 추가
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
        "by_sector":     {},   # v2.3 추가
    }

    # ── 1. 지수 수집 ──────────────────────────────────────────
    result["kospi"]  = _fetch_index(target_date, "1001", "KOSPI")
    result["kosdaq"] = _fetch_index(target_date, "2001", "KOSDAQ")

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

    # ── 3. 업종 분류 수집 (v2.3 신규) ────────────────────────
    # pykrx 업종분류 + 전종목 등락률 결합 → 업종별 실제 등락률 순위
    # signal_analyzer가 "구리 강세" 신호 시 실제 전선 업종 상위 종목 동적 조회에 사용
    result["by_sector"] = _fetch_sector_map(date_str, all_stocks)

    # ── 4. 기관/외인 순매수 (상위 급등 종목 개별 조회) ─────────
    top_tickers = [s["종목코드"] for s in result["upper_limit"][:15]] + \
                  [s["종목코드"] for s in result["top_gainers"][:15]]
    result["institutional"] = _fetch_institutional_by_tickers(
        date_str, top_tickers
    )

    # ── 5. 공매도 (상위 급등 종목 개별 조회) ───────────────────
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

def _fetch_index(target_date: datetime, index_code: str, name: str) -> dict:
    """
    지수 OHLCV + 등락률 수집

    [v2.2 버그 수정]
    fromdate==todate 단일날짜 조회 시 pykrx가 등락률 0.00% 반환하는 문제
    → target_date 기준 10 캘린더일 전부터 조회 → 마지막 행 사용

    [v2.5 버그 수정 — 날짜 교체 시 0% 초기화]
    pykrx 등락률 컬럼은 범위 첫 행을 0으로 삼는 경우가 있어
    하루가 바뀌면 최근 행이 0%로 초기화되는 현상 발생.
    → 마지막 두 행의 종가로 등락률을 직접 계산하도록 변경.
    범위를 10 → 20일로 확장해 공휴일 연속 구간에서도 2개 이상 행 보장.
    """
    try:
        date_str  = fmt_ymd(target_date)
        from_date = target_date - timedelta(days=20)   # v2.5: 10 → 20일로 확장
        from_str  = fmt_ymd(from_date)

        df = pykrx_stock.get_index_ohlcv_by_date(from_str, date_str, index_code)
        if df.empty:
            logger.warning(f"[price] {name} 지수 없음 (휴장 또는 데이터 없음)")
            return {}

        close = float(df.iloc[-1].get("종가", 0))

        # v2.5: 등락률 직접 계산 (pykrx 등락률 컬럼 0% 반환 버그 회피)
        if len(df) >= 2:
            prev_close  = float(df.iloc[-2].get("종가", 0))
            change_rate = ((close - prev_close) / prev_close * 100) if prev_close > 0 else 0.0
        else:
            # 행이 1개뿐이면 컬럼 값을 fallback으로 사용
            change_rate = float(df.iloc[-1].get("등락률", 0))

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
                    "업종명":   "",   # _fetch_sector_map에서 채워짐
                }
        except Exception as e:
            logger.warning(f"[price] {market} 전종목 수집 실패: {e}")
    return all_stocks


def _fetch_sector_map(date_str: str, all_stocks: dict) -> dict:
    """
    업종 분류 수집 → {업종명: [종목entry...]} 등락률 내림차순

    pykrx.get_market_sector_classifications(date, market) 사용
    반환 컬럼 예시: 종목코드, 종목명, 업종명 (버전마다 다를 수 있음)
    → 컬럼명은 _find_col_keyword로 유연하게 탐색

    역할:
      signal_analyzer가 "구리 강세" 신호를 만들 때
      config.COMMODITY_KR_INDUSTRY["copper"] = ["전기/전선"]로 업종명 키워드 조회
      → 그날 실제 등락률 상위 종목들을 동적으로 관련종목에 넣음
    """
    sector_by_code = {}   # {종목코드: 업종명}

    for market in ["KOSPI", "KOSDAQ"]:
        try:
            df = pykrx_stock.get_market_sector_classifications(date_str, market=market)
            if df.empty:
                continue

            # 컬럼명 탐색 (pykrx 버전별 차이 대응)
            code_col   = _find_col(df, ["종목코드", "Code", "ticker"])
            sector_col = _find_col(df, ["업종명", "sector", "Sector", "SECTOR"])

            if not code_col or not sector_col:
                logger.warning(
                    f"[price] {market} 업종분류 컬럼 미확인 "
                    f"(실제컬럼: {df.columns.tolist()}) — 업종 데이터 스킵"
                )
                # 컬럼명 로그 → 첫 실행 후 확인해서 수정 가능
                continue

            for _, row in df.iterrows():
                code   = str(row[code_col]).zfill(6)
                sector = str(row[sector_col])
                sector_by_code[code] = sector

        except Exception as e:
            logger.warning(f"[price] {market} 업종분류 수집 실패: {e}")

    if not sector_by_code:
        logger.warning("[price] 업종분류 전체 실패 — by_sector 빈 상태로 진행")
        return {}

    # all_stocks에 업종명 주입 (부가 정보)
    for code, entry in all_stocks.items():
        entry["업종명"] = sector_by_code.get(code, "기타")

    # 업종별 그룹핑 → 등락률 내림차순 정렬
    by_sector: dict[str, list] = {}
    for entry in all_stocks.values():
        sector = entry.get("업종명", "기타")
        if not sector or sector == "기타":
            continue
        by_sector.setdefault(sector, []).append(entry)

    for sector in by_sector:
        by_sector[sector].sort(key=lambda x: x["등락률"], reverse=True)

    logger.info(f"[price] 업종분류 완료 — {len(by_sector)}개 업종")
    return by_sector


def _fetch_institutional_by_tickers(
    date_str: str, tickers: list[str], top_n: int = 10
) -> list[dict]:
    """기관/외인 순매수 — 개별 종목 조회"""
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

            if not inst_col and not frgn_col:
                logger.warning(
                    f"[price] 기관/외인 컬럼 없음 "
                    f"(실제컬럼: {df.columns.tolist()}) — pykrx 버전 이슈 가능성"
                )
                break

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
    """공매도 비중 — 개별 종목 조회"""
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