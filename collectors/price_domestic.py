"""
collectors/price_domestic.py
당일 주가·거래량·기관/외인·공매도 수집 전담 (pykrx 기반)
- 분석 로직 없음, 수집만 (ARCHITECTURE 규칙)

[수정이력]
- v2.2: _fetch_index 단일날짜 조회 버그 수정
- v2.3: 업종 분류 수집 추가, by_sector 반환 추가
- v2.5: 등락률 직접 계산으로 전환, 20일 범위 확장
- v11.0: [pykrx 1.2.x 호환 전면 개선]
        pykrx 1.2.x에서 내부 KRX API 응답 처리 변경으로 다수 함수 KeyError 발생
        모든 주요 함수에 폴백 전략 추가:
          _fetch_index:      ETF 프록시 폴백 (KODEX200 / KODEX코스닥150)
          _fetch_all_stocks: ThreadPoolExecutor 병렬 개별 조회 폴백
          _fetch_sector_map: MultiIndex 처리 + 컬럼명 후보 확장
          공매도:             함수명 후보 3개로 확장 (버전별 자동 탐색)
        _find_col: 컬럼 탐색을 set 기반으로 개선
"""

from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        "top_gainers":  list,   급등(7%이상) 상위 20개
        "top_losers":   list,   급락(-7%이하) 상위 10개
        "institutional":list,   기관/외인 순매수 상위
        "short_selling":list,   공매도 상위
        "by_name":      dict,   {종목명: entry}
        "by_code":      dict,   {종목코드: entry}
        "by_sector":    dict,   {업종명: [entry...]} 등락률 내림차순
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
        "by_sector":     {},
    }

    # ── 1. 지수 수집 ──────────────────────────────────────────
    result["kospi"]  = _fetch_index(target_date, "1001", "KOSPI",  etf_proxy="069500")
    result["kosdaq"] = _fetch_index(target_date, "2001", "KOSDAQ", etf_proxy="229200")

    # ── 2. 전종목 등락률 수집 ─────────────────────────────────
    all_stocks = _fetch_all_stocks(date_str, target_date)
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

    # ── 3. 업종 분류 수집 ────────────────────────────────────
    result["by_sector"] = _fetch_sector_map(date_str, all_stocks)

    # ── 4. 기관/외인 순매수 (상위 급등 종목) ──────────────────
    top_tickers = [s["종목코드"] for s in result["upper_limit"][:15]] + \
                  [s["종목코드"] for s in result["top_gainers"][:15]]
    result["institutional"] = _fetch_institutional_by_tickers(date_str, top_tickers)

    # ── 5. 공매도 (상위 급등 종목) ────────────────────────────
    result["short_selling"] = _fetch_short_selling(date_str, top_tickers)

    return result


def collect_supply(ticker: str, target_date: datetime = None) -> dict:
    """개별 종목 수급 데이터 (장중봇 4단계에서 호출)"""
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
        df = pykrx_stock.get_market_trading_value_by_date(start_str, date_str, ticker)
        if not df.empty:
            inst_col = _find_col(df.columns, ["기관합계", "기관"])
            frgn_col = _find_col(df.columns, ["외국인합계", "외국인"])
            tail = df.tail(config.INSTITUTION_DAYS)
            if inst_col:
                result["기관_5일순매수"]   = int(tail[inst_col].sum())
            if frgn_col:
                result["외국인_5일순매수"] = int(tail[frgn_col].sum())
    except Exception as e:
        logger.warning(f"[price] {ticker} 기관/외인 수급 실패: {e}")

    try:
        _short_fn = _resolve_short_ohlcv_fn()
        if _short_fn is None:
            raise AttributeError("공매도 OHLCV 함수 없음")
        df = _short_fn(start_str, date_str, ticker)
        if not df.empty:
            ratio_col = _find_col(df.columns, ["공매도비중", "비중", "ShortRatio"])
            bal_col   = _find_col(df.columns, ["대차잔고", "잔고", "Balance"])
            last = df.iloc[-1]
            if ratio_col:
                result["공매도잔고율"] = float(last[ratio_col])
            if bal_col:
                result["대차잔고"] = int(last[bal_col])
    except Exception as e:
        logger.warning(f"[price] {ticker} 공매도 수집 실패: {e}")

    return result


# ── 내부 헬퍼 ─────────────────────────────────────────────────

def _fetch_index(target_date: datetime, index_code: str, name: str, etf_proxy: str = None) -> dict:
    """
    지수 OHLCV + 등락률 수집

    [v11.0 pykrx 1.2.x 호환]
    get_index_ohlcv_by_date 내부에서 '지수명' KeyError 발생 시
    ETF 프록시(KODEX200 / KODEX코스닥150) 기반 폴백으로 자동 전환.
    등락률은 전일 종가 대비 직접 계산 (pykrx 등락률 컬럼 0% 버그 방지).
    """
    date_str  = fmt_ymd(target_date)
    from_str  = fmt_ymd(target_date - timedelta(days=20))

    # ─ 1차: get_index_ohlcv_by_date ──────────────────────────
    try:
        df = pykrx_stock.get_index_ohlcv_by_date(from_str, date_str, index_code)

        # pykrx 1.2.x MultiIndex (날짜, 지수명) 대응: 지수명 레벨 제거
        if hasattr(df.index, "levels") and len(df.index.levels) > 1:
            df = df.reset_index(level=1, drop=True)

        if not df.empty:
            return _calc_index_from_df(df, name)
    except Exception as e:
        logger.warning(f"[price] {name} get_index_ohlcv_by_date 실패: {e} → ETF 프록시 폴백")

    # ─ 2차: ETF 프록시 (KODEX200 / KODEX코스닥150) ───────────
    # get_market_ohlcv(범위, ticker)는 pykrx 1.2.x에서도 정상 동작 확인
    if etf_proxy:
        try:
            df = pykrx_stock.get_market_ohlcv(from_str, date_str, etf_proxy)
            if not df.empty:
                logger.info(f"[price] {name}: ETF 프록시({etf_proxy}) 기반 등락률 산출")
                return _calc_index_from_df(df, name)
        except Exception as e2:
            logger.warning(f"[price] {name} ETF 프록시 폴백도 실패: {e2}")

    logger.warning(f"[price] {name} 지수 수집 전체 실패")
    return {}


def _calc_index_from_df(df, name: str) -> dict:
    """DataFrame 마지막 두 행으로 종가·등락률 계산 (한글/영문 컬럼 모두 지원)"""
    close_col = _find_col(df.columns, ["종가", "Close", "close"])
    if not close_col:
        raise ValueError(f"종가 컬럼 없음 (실제: {list(df.columns)})")

    close = float(df.iloc[-1][close_col])

    if len(df) >= 2:
        prev_close  = float(df.iloc[-2][close_col])
        change_rate = ((close - prev_close) / prev_close * 100) if prev_close > 0 else 0.0
    else:
        chg_col     = _find_col(df.columns, ["등락률", "Change", "change"])
        change_rate = float(df.iloc[-1][chg_col]) if chg_col else 0.0

    logger.info(f"[price] {name}: {close:,.2f} ({change_rate:+.2f}%)")
    return {"close": close, "change_rate": change_rate}


def _fetch_all_stocks(date_str: str, target_date: datetime = None) -> dict:
    """
    전종목 OHLCV + 등락률 수집 → {종목코드: entry}

    [v11.0 pykrx 1.2.x 호환]
    get_market_ohlcv_by_ticker(date, market) 내부 오류 시
    get_market_ticker_list + ThreadPoolExecutor 병렬 개별 조회로 자동 폴백.
    """
    all_stocks = {}
    for market in ["KOSPI", "KOSDAQ"]:
        stocks = _fetch_market_stocks(date_str, market, target_date)
        all_stocks.update(stocks)
    return all_stocks


def _fetch_market_stocks(date_str: str, market: str, target_date: datetime = None) -> dict:
    """단일 시장 전종목 조회 (1차: 일괄 / 2차: 병렬 개별)"""

    # ─ 1차: get_market_ohlcv_by_ticker 일괄 조회 ─────────────
    try:
        df = pykrx_stock.get_market_ohlcv_by_ticker(date_str, market=market)
        if df is not None and not df.empty:
            result = _parse_ohlcv_all_tickers(df, market)
            if result:
                logger.info(f"[price] {market} 일괄 수집 완료: {len(result)}종목")
                return result
    except Exception as e:
        logger.warning(
            f"[price] {market} get_market_ohlcv_by_ticker 실패: {e}"
            " → 병렬 개별 조회 폴백"
        )

    # ─ 2차: 병렬 개별 조회 폴백 ──────────────────────────────
    logger.info(f"[price] {market} 병렬 개별 조회 시작 (pykrx 1.2.x 호환 모드)")
    return _fetch_market_stocks_parallel(date_str, market, target_date)


def _parse_ohlcv_all_tickers(df, market: str) -> dict:
    """
    get_market_ohlcv_by_ticker 결과 파싱.
    한글 컬럼(종가/등락률/거래량)과 영문 컬럼(Close/Change/Volume) 모두 지원.
    """
    close_col  = _find_col(df.columns, ["종가",  "Close",  "close"])
    change_col = _find_col(df.columns, ["등락률", "Change", "change"])
    vol_col    = _find_col(df.columns, ["거래량", "Volume", "volume"])

    if not close_col:
        logger.warning(f"[price] {market} 종가 컬럼 없음 (실제: {list(df.columns)})")
        return {}

    result = {}
    for ticker in df.index:
        try:
            row    = df.loc[ticker]
            close  = float(row[close_col])
            vol    = int(row[vol_col])      if vol_col    else 0
            change = float(row[change_col]) if change_col else 0.0
            name   = pykrx_stock.get_market_ticker_name(ticker)
            result[ticker] = {
                "종목코드": ticker,
                "종목명":   name or ticker,
                "등락률":   change,
                "거래량":   vol,
                "종가":     close,
                "시장":     market,
                "업종명":   "",
            }
        except Exception as e:
            logger.debug(f"[price] {ticker} 파싱 실패: {e}")
    return result


def _fetch_market_stocks_parallel(
    date_str: str, market: str, target_date: datetime = None,
    max_workers: int = 20,
) -> dict:
    """
    [v11.0 폴백] 개별 종목 병렬 조회.

    get_market_ohlcv_by_ticker가 pykrx 1.2.x 내부 오류로 실패할 때 사용.
    get_market_ohlcv(범위, ticker)는 1.2.x에서도 정상 동작 확인됨.

    성능: KOSPI+KOSDAQ 합산 ~2000종목, 20 workers 기준 30~60초
          마감봇(18:30) 실행 기준 허용 범위
    """
    if target_date is None:
        target_date = datetime.strptime(date_str, "%Y%m%d")

    try:
        tickers = pykrx_stock.get_market_ticker_list(date_str, market=market)
    except Exception as e:
        logger.warning(f"[price] {market} 종목 목록 조회 실패: {e}")
        return {}

    if not tickers:
        return {}

    # 5일 범위 조회: 단일 날짜보다 주말/공휴일에 안정적
    from_str = fmt_ymd(target_date - timedelta(days=5))

    def fetch_one(ticker: str) -> tuple[str, dict | None]:
        try:
            df = pykrx_stock.get_market_ohlcv(from_str, date_str, ticker)
            if df is None or df.empty:
                return ticker, None

            close_col = _find_col(df.columns, ["종가",  "Close",  "close"])
            vol_col   = _find_col(df.columns, ["거래량", "Volume", "volume"])
            if not close_col:
                return ticker, None

            close = float(df.iloc[-1][close_col])
            vol   = int(df.iloc[-1][vol_col]) if vol_col else 0

            # 등락률 직접 계산 (pykrx 0% 버그 방지)
            if len(df) >= 2:
                prev_close  = float(df.iloc[-2][close_col])
                change_rate = ((close - prev_close) / prev_close * 100) if prev_close > 0 else 0.0
            else:
                chg_col     = _find_col(df.columns, ["등락률", "Change", "change"])
                change_rate = float(df.iloc[-1][chg_col]) if chg_col else 0.0

            name = pykrx_stock.get_market_ticker_name(ticker)
            return ticker, {
                "종목코드": ticker,
                "종목명":   name or ticker,
                "등락률":   change_rate,
                "거래량":   vol,
                "종가":     close,
                "시장":     market,
                "업종명":   "",
            }
        except Exception:
            return ticker, None

    result = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_one, t): t for t in tickers}
        for future in as_completed(futures):
            ticker, data = future.result()
            if data:
                result[ticker] = data

    logger.info(f"[price] {market} 병렬 폴백 완료: {len(result)}/{len(tickers)}종목")
    return result


def _fetch_sector_map(date_str: str, all_stocks: dict) -> dict:
    """
    업종 분류 수집 → {업종명: [종목entry...]} 등락률 내림차순

    [v11.0 pykrx 1.2.x 호환]
    - MultiIndex 반환 대응: reset_index로 평탄화
    - 컬럼명 후보 대폭 확장 (한글/영문/대소문자 혼용)
    - 종목코드가 index에 있는 경우도 처리
    """
    sector_by_code: dict[str, str] = {}

    for market in ["KOSPI", "KOSDAQ"]:
        try:
            df = pykrx_stock.get_market_sector_classifications(date_str, market=market)
            if df is None or df.empty:
                continue

            # [v12.0] pykrx 1.2.x 버그 방어: OHLCV 컬럼(종가/Close 등)이 있으면
            # 업종 분류가 아닌 가격 데이터가 잘못 반환된 것 → 건너뜀
            ohlcv_cols = {"종가", "Close", "close", "시가", "고가", "저가", "거래량"}
            if ohlcv_cols & set(df.columns):
                logger.warning(
                    f"[price] {market} get_market_sector_classifications가 OHLCV 반환 "
                    f"(pykrx 버그) — 업종분류 건너뜀. 실제컬럼: {df.columns.tolist()}"
                )
                continue

            # MultiIndex 또는 종목코드가 index인 경우 평탄화
            if hasattr(df.index, "levels"):
                df = df.reset_index()
            elif df.index.name in ("종목코드", "Code", "code", "ticker", "Ticker"):
                df = df.reset_index()

            # 컬럼명 탐색
            code_col   = _find_col(df.columns, [
                "종목코드", "Code", "code", "ticker", "Ticker", "TICKER", "symbol",
            ])
            sector_col = _find_col(df.columns, [
                "업종명", "sector", "Sector", "SECTOR", "industry", "Industry", "분류",
            ])

            if not code_col or not sector_col:
                logger.warning(
                    f"[price] {market} 업종분류 컬럼 미확인 "
                    f"(실제: {df.columns.tolist()})"
                )
                continue

            for _, row in df.iterrows():
                code   = str(row[code_col]).zfill(6)
                sector = str(row[sector_col])
                if code and sector and sector not in ("nan", ""):
                    sector_by_code[code] = sector

        except Exception as e:
            logger.warning(f"[price] {market} 업종분류 수집 실패: {e}")

    if not sector_by_code:
        logger.warning("[price] 업종분류 전체 실패 — by_sector 빈 상태로 진행")
        return {}

    # all_stocks에 업종명 주입
    for code, entry in all_stocks.items():
        entry["업종명"] = sector_by_code.get(code, "기타")

    # 업종별 그룹핑 → 등락률 내림차순
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
            inst_col = _find_col(df.columns, ["기관합계", "기관"])
            frgn_col = _find_col(df.columns, ["외국인합계", "외국인"])

            if not inst_col and not frgn_col:
                logger.warning(
                    f"[price] 기관/외인 컬럼 없음 (실제: {df.columns.tolist()})"
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

    results.sort(key=lambda x: x["기관순매수"], reverse=True)
    logger.info(f"[price] 기관/외인 수집 완료 — {len(results)}종목")
    return results[:top_n]


def _fetch_short_selling(
    date_str: str, tickers: list[str], top_n: int = 10
) -> list[dict]:
    """
    공매도 비중 — 개별 종목 조회

    [v11.0 pykrx 1.2.x 호환]
    함수명 후보 3개로 확장:
      1. get_shorting_ohlcv_by_date      (pykrx 1.0.47+)
      2. get_market_short_ohlcv_by_date  (pykrx 1.0.46-)
      3. get_shorting_balance_by_date    (pykrx 1.2.x)
    """
    _short_fn = _resolve_short_ohlcv_fn()
    if _short_fn is None:
        logger.warning("[price] 공매도 OHLCV 함수 없음 — pykrx 버전 확인 권장")
        return []

    results = []
    for ticker in tickers:
        try:
            df = _short_fn(date_str, date_str, ticker)
            if df is None or df.empty:
                continue
            row       = df.iloc[-1]
            ratio_col = _find_col(df.columns, ["공매도비중", "비중", "ShortRatio", "ratio"])
            vol_col   = _find_col(df.columns, ["공매도거래량", "거래량", "ShortVolume", "volume"])
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


# ── 공용 유틸 ──────────────────────────────────────────────────

def _find_col(columns, candidates: list[str]) -> str | None:
    """
    DataFrame 컬럼 목록에서 후보 중 존재하는 첫 번째 항목 반환.
    set 기반 O(1) 탐색. 한글/영문 컬럼명 혼용 환경 대응.
    """
    col_set = set(columns)
    for c in candidates:
        if c in col_set:
            return c
    return None


def _resolve_short_ohlcv_fn():
    """
    pykrx 버전별 공매도 OHLCV 함수 자동 탐색.

    탐색 순서:
      1. get_shorting_ohlcv_by_date      (pykrx 1.0.47+)
      2. get_market_short_ohlcv_by_date  (pykrx 1.0.46-)
      3. get_shorting_balance_by_date    (pykrx 1.2.x 이후)
    """
    for fn_name in [
        "get_shorting_ohlcv_by_date",
        "get_market_short_ohlcv_by_date",
        "get_shorting_balance_by_date",
    ]:
        fn = getattr(pykrx_stock, fn_name, None)
        if fn is not None:
            return fn
    return None
