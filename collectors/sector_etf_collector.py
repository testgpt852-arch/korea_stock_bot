"""
collectors/sector_etf_collector.py
국내 KODEX 섹터 ETF 거래량·자금유입 수집 전담 — pykrx 기반

[ARCHITECTURE rule #92 — 절대 금지]
- pykrx 호출은 마감봇(18:30) 전용. 장중 실시간 섹터 수급 필요 시 KIS REST 사용.
- AI 분석 호출 금지 (sector_flow_analyzer.py에서 처리)
- 텔레그램 발송 금지
- DB 기록 금지
- 비치명적 처리: ETF 조회 실패 시 빈 리스트 반환

[v10.0 Phase 3 신규]
수집 대상 (KODEX 섹터 ETF 주요 종목):
  - 069500 KODEX 200 (시장 전체 기준선)
  - 091160 KODEX 반도체
  - 091180 KODEX 자동차
  - 157490 KODEX 철강
  - 102110 KODEX 건설
  - 139250 KODEX 에너지화학
  - 140710 KODEX 운송
  - 117700 KODEX 인프라
  - 139220 KODEX IT
  - 102780 KODEX 은행
  - 108460 KODEX 방어
  - 229720 KODEX 방산
  - 266390 KODEX 미국방산항공

출력 형식:
  list[dict] = [
    {
      "ticker":      str,   # ETF 종목코드
      "name":        str,   # ETF 이름
      "sector":      str,   # 매핑 섹터명
      "close":       int,   # 종가
      "volume":      int,   # 거래량
      "volume_prev": int,   # 전일 거래량
      "volume_ratio":float, # 거래량 배율 (당일/전일)
      "value":       int,   # 거래대금 (원)
      "change_pct":  float, # 등락률 (%)
      "신뢰도":      str,
    }
  ]
"""

from datetime import datetime
import config
from utils.logger import logger
from utils.date_utils import get_prev_trading_day, get_today, fmt_kr

try:
    from pykrx import stock as pykrx_stock
    _PYKRX_AVAILABLE = True
except ImportError:
    _PYKRX_AVAILABLE = False
    logger.warning("[sector_etf] pykrx 미설치 — sector_etf_collector 비활성화")


# ── 수집 대상 ETF 목록 ──────────────────────────────────────────────
# {종목코드: (ETF이름, 섹터명)}
# rule #92: 마감봇 전용 pykrx 기반. 장중 사용 금지.
_SECTOR_ETFS: dict[str, tuple[str, str]] = {
    "069500": ("KODEX 200",          "시장전체"),
    "091160": ("KODEX 반도체",       "반도체/IT"),
    "091180": ("KODEX 자동차",       "자동차/부품"),
    "157490": ("KODEX 철강",         "철강/비철금속"),
    "102110": ("KODEX 건설",         "건설/부동산"),
    "139250": ("KODEX 에너지화학",   "에너지/화학"),
    "140710": ("KODEX 운송",         "해운/조선/운송"),
    "117700": ("KODEX 인프라",       "인프라/유틸"),
    "139220": ("KODEX IT",           "IT/소프트웨어"),
    "102780": ("KODEX 은행",         "금융/은행"),
    "229720": ("KODEX 방산",         "방산/항공"),
}


def collect(target_date: datetime = None) -> list[dict]:
    """
    KODEX 섹터 ETF 거래량·등락률 수집.

    rule #92: pykrx 호출은 마감봇 전용.
              장중에서 호출 시 15~20분 지연 데이터 주의.

    Args:
        target_date: 수집 기준일 (None이면 당일 or 전 거래일)

    Returns:
        list[dict] — ETF별 거래량·자금유입 데이터
                     실패 시 빈 리스트 반환 (비치명적)
    """
    if not config.SECTOR_ETF_ENABLED:
        logger.info("[sector_etf] SECTOR_ETF_ENABLED=false — 수집 건너뜀")
        return []

    if not _PYKRX_AVAILABLE:
        logger.warning("[sector_etf] pykrx 미설치 — 빈 리스트 반환")
        return []

    if target_date is None:
        target_date = get_today()

    today_str = target_date.strftime("%Y%m%d")

    # 전 거래일 (거래량 배율 계산용)
    prev_date = get_prev_trading_day(target_date)
    prev_str  = prev_date.strftime("%Y%m%d") if prev_date else None

    logger.info(f"[sector_etf] {fmt_kr(target_date)} KODEX 섹터 ETF 수집 시작 ({len(_SECTOR_ETFS)}개)")

    results: list[dict] = []

    for ticker, (etf_name, sector) in _SECTOR_ETFS.items():
        try:
            item = _fetch_etf_data(ticker, etf_name, sector, today_str, prev_str)
            if item:
                results.append(item)
        except Exception as e:
            # rule #92: 비치명적 — ETF 실패해도 계속 진행
            logger.warning(f"[sector_etf] {etf_name}({ticker}) 실패 (무시): {e}")

    # 거래량 배율 내림차순 정렬
    results.sort(key=lambda x: x.get("volume_ratio", 0.0), reverse=True)

    logger.info(f"[sector_etf] 수집 완료 {len(results)}개 ETF")
    return results


def _fetch_etf_data(
    ticker: str,
    etf_name: str,
    sector: str,
    date_str: str,
    prev_str: str | None,
) -> dict | None:
    """
    단일 ETF의 당일·전일 OHLCV 조회 → 거래량 배율 계산.

    rule #92: pykrx 호출만 수행. 분석 없음.
    """
    try:
        # 당일 OHLCV (최근 3일치로 조회, 당일 데이터 안정성 확보)
        df = pykrx_stock.get_market_ohlcv(
            fromdate=prev_str or date_str,
            todate=date_str,
            ticker=ticker,
        )

        if df is None or df.empty:
            logger.debug(f"[sector_etf] {etf_name}({ticker}) 데이터 없음")
            return None

        today_row = None
        prev_row  = None

        # date_str 행 찾기
        date_idx = date_str[:4] + "-" + date_str[4:6] + "-" + date_str[6:8]
        for idx in df.index:
            idx_str = str(idx)[:10]
            if idx_str == date_idx or idx_str.replace("-", "") == date_str:
                today_row = df.loc[idx]
                break

        if today_row is None and len(df) >= 1:
            today_row = df.iloc[-1]

        if today_row is None:
            return None

        # 전일 행 찾기 (거래량 배율용)
        if len(df) >= 2:
            # today_row가 마지막 행이면 그 전 행이 전일
            iloc_today = list(df.index).index(today_row.name) if hasattr(today_row, "name") else len(df) - 1
            if iloc_today > 0:
                prev_row = df.iloc[iloc_today - 1]
            elif len(df) >= 2:
                prev_row = df.iloc[-2]

        # 열 이름 정규화
        col_map = {
            "종가": ["종가", "Close", "close"],
            "거래량": ["거래량", "Volume", "volume"],
            "거래대금": ["거래대금", "Value", "value"],
            "등락률": ["등락률", "Change", "change"],
            "시가": ["시가", "Open", "open"],
        }

        def _get_col(row, keys: list):
            for k in keys:
                if k in row.index:
                    return row[k]
            return 0

        close      = int(_get_col(today_row, col_map["종가"]))
        volume     = int(_get_col(today_row, col_map["거래량"]))
        value      = int(_get_col(today_row, col_map["거래대금"]))

        # 등락률 계산 (pykrx가 이미 제공하면 사용, 없으면 계산)
        change_pct_raw = _get_col(today_row, col_map["등락률"])
        if change_pct_raw and change_pct_raw != 0:
            change_pct = float(change_pct_raw)
        else:
            open_price = int(_get_col(today_row, col_map["시가"]))
            change_pct = (close - open_price) / open_price * 100 if open_price else 0.0

        # 전일 거래량 (없으면 0)
        volume_prev = int(_get_col(prev_row, col_map["거래량"])) if prev_row is not None else 0

        # 거래량 배율
        if volume_prev > 0:
            volume_ratio = volume / volume_prev
        else:
            volume_ratio = 1.0

        logger.debug(
            f"[sector_etf] {etf_name}({ticker}): "
            f"종가={close:,} 거래량={volume:,} 배율={volume_ratio:.2f}x 등락={change_pct:+.2f}%"
        )

        return {
            "ticker":       ticker,
            "name":         etf_name,
            "sector":       sector,
            "close":        close,
            "volume":       volume,
            "volume_prev":  volume_prev,
            "volume_ratio": round(volume_ratio, 2),
            "value":        value,
            "change_pct":   round(change_pct, 2),
            "신뢰도":        "pykrx",
        }

    except Exception as e:
        raise RuntimeError(f"pykrx 조회 실패 [{ticker}]: {e}") from e
