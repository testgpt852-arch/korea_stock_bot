"""
kis/order_client.py
KIS 주문 API 전담 (Phase 4, v3.4 신규)

[역할]
모의투자(VTS) / 실전(REAL) 매수·매도·잔고 조회.
config.TRADING_MODE 단일 상수로 VTS/REAL 자동 분기.
모든 함수는 동기(sync) — realtime_alert 에서 run_in_executor 경유 호출.

[API 엔드포인트]
  매수:     POST /uapi/domestic-stock/v1/trading/order-cash
            VTS: VTTC0012U  /  REAL: TTTC0012U
  매도:     POST /uapi/domestic-stock/v1/trading/order-cash
            VTS: VTTC0011U  /  REAL: TTTC0011U
  잔고조회: GET  /uapi/domestic-stock/v1/trading/inquire-balance
            VTS: VTTC8434R  /  REAL: TTTC8434R
  현재가:   GET  /uapi/domestic-stock/v1/quotations/inquire-price
            공통: FHKST01010100 (rest_client 와 동일)

[URL]
  VTS  Base URL: https://openapivts.koreainvestment.com:29443
  REAL Base URL: https://openapi.koreainvestment.com:9443

[ARCHITECTURE 의존성]
order_client → kis/auth  (get_access_token / get_vts_access_token)
order_client → utils/rate_limiter  (kis_rate_limiter.acquire)
order_client → config  (TRADING_MODE, KIS_* 상수)
order_client ← traders/position_manager  (buy / sell / get_balance 호출)

[절대 금지 규칙 — ARCHITECTURE #22 추가]
이 파일은 주문·잔고 조회만 담당. 포지션 관리·알림 발송·DB 기록 금지.
모든 order 함수는 동기 함수 — asyncio.run() 금지.
"""

import math
import requests
from utils.logger import logger
from utils.rate_limiter import kis_rate_limiter
import config

# ── Base URL / TR ID 분기 테이블 ──────────────────────────────
_REAL_BASE_URL = "https://openapi.koreainvestment.com:9443"
_VTS_BASE_URL  = "https://openapivts.koreainvestment.com:29443"

_TR = {
    "buy":     {"VTS": "VTTC0012U", "REAL": "TTTC0012U"},
    "sell":    {"VTS": "VTTC0011U", "REAL": "TTTC0011U"},
    "balance": {"VTS": "VTTC8434R", "REAL": "TTTC8434R"},
}


def _get_base_url() -> str:
    return _VTS_BASE_URL if config.TRADING_MODE == "VTS" else _REAL_BASE_URL


def _get_token() -> str | None:
    """TRADING_MODE에 따라 VTS 또는 REAL 토큰 반환"""
    from kis.auth import get_vts_access_token, get_access_token
    if config.TRADING_MODE == "VTS":
        return get_vts_access_token()
    return get_access_token()


def _get_account() -> tuple[str, str]:
    """(계좌번호, 상품코드) 반환"""
    if config.TRADING_MODE == "VTS":
        return config.KIS_VTS_ACCOUNT_NO or "", config.KIS_VTS_ACCOUNT_CODE or "01"
    return config.KIS_ACCOUNT_NO or "", config.KIS_ACCOUNT_CODE or "01"


def _base_headers(tr_id: str, token: str) -> dict:
    app_key    = config.KIS_VTS_APP_KEY if config.TRADING_MODE == "VTS" else config.KIS_APP_KEY
    app_secret = config.KIS_VTS_APP_SECRET if config.TRADING_MODE == "VTS" else config.KIS_APP_SECRET
    return {
        "Authorization": f"Bearer {token}",
        "appkey":        app_key,
        "appsecret":     app_secret,
        "tr_id":         tr_id,
        "Content-Type":  "application/json; charset=utf-8",
    }


# ── 공개 API ──────────────────────────────────────────────────

def get_current_price(ticker: str) -> dict:
    """
    단일 종목 현재가 조회 (order_client 내부용 — 매수 수량 계산에 사용)
    반환: {"현재가": int, "등락률": float}  /  실패 시 {}
    """
    token = _get_token()
    if not token:
        logger.warning(f"[order] 토큰 없음 — {ticker} 현재가 조회 불가")
        return {}

    kis_rate_limiter.acquire()
    url = f"{_get_base_url()}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = _base_headers("FHKST01010100", token)
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD":         ticker,
    }
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=5)
        resp.raise_for_status()
        out = resp.json().get("output", {})
        return {
            "현재가": int(out.get("stck_prpr", 0)),
            "등락률": float(out.get("prdy_ctrt", 0)),
        }
    except Exception as e:
        logger.warning(f"[order] {ticker} 현재가 조회 실패: {e}")
        return {}


def buy(ticker: str, name: str = "", amount: int | None = None) -> dict:
    """
    시장가 매수 (TRADING_MODE 자동 분기)

    Args:
        ticker:  종목코드 (6자리)
        name:    종목명 (로그용)
        amount:  매수 금액 (원, 기본 config.POSITION_BUY_AMOUNT)

    Returns:
        {
            "success":    bool,
            "order_no":   str | None,
            "ticker":     str,
            "name":       str,
            "qty":        int,         # 주문 수량
            "buy_price":  int,         # 매수 시점 현재가
            "total_amt":  int,         # 총 매수 금액 (추정)
            "mode":       str,         # "VTS" / "REAL"
            "message":    str,
        }
    """
    result = _empty_result(ticker, name)
    buy_amt = amount or config.POSITION_BUY_AMOUNT

    token = _get_token()
    if not token:
        result["message"] = "토큰 없음"
        return result

    # ① 현재가 조회 → 수량 계산
    price_info = get_current_price(ticker)
    current_price = price_info.get("현재가", 0)
    if current_price <= 0:
        result["message"] = "현재가 조회 실패"
        return result

    qty = math.floor(buy_amt / current_price)
    if qty <= 0:
        result["message"] = f"매수 수량 0 (현재가 {current_price:,}원 > 매수금액 {buy_amt:,}원)"
        logger.warning(f"[order] {name}({ticker}) 매수 수량 0 — 건너뜀")
        return result

    # ② 주문 실행
    kis_rate_limiter.acquire()
    url     = f"{_get_base_url()}/uapi/domestic-stock/v1/trading/order-cash"
    tr_id   = _TR["buy"][config.TRADING_MODE]
    acct_no, acct_cd = _get_account()
    headers = _base_headers(tr_id, token)
    headers["custtype"] = "P"

    body = {
        "CANO":          acct_no,
        "ACNT_PRDT_CD":  acct_cd,
        "PDNO":          ticker,
        "ORD_DVSN":      "01",       # 01: 시장가
        "ORD_QTY":       str(qty),
        "ORD_UNPR":      "0",        # 시장가는 0
    }

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("rt_cd") == "0":
            order_no = data.get("output", {}).get("ODNO", "") or data.get("output", {}).get("odno", "")
            result.update({
                "success":   True,
                "order_no":  order_no,
                "qty":       qty,
                "buy_price": current_price,
                "total_amt": qty * current_price,
                "message":   f"매수 체결 완료 — {qty}주 × {current_price:,}원",
            })
            logger.info(
                f"[order] {config.TRADING_MODE} 매수 ✅  "
                f"{name}({ticker})  {qty}주 × {current_price:,}원  "
                f"총 {qty * current_price:,}원  주문번호:{order_no}"
            )
        else:
            msg = data.get("msg1", data.get("msg_cd", "알 수 없는 오류"))
            result["message"] = f"매수 거부: {msg}"
            logger.warning(f"[order] {config.TRADING_MODE} 매수 실패 — {name}({ticker}): {msg}")

    except Exception as e:
        result["message"] = f"매수 오류: {e}"
        logger.error(f"[order] {ticker} 매수 예외: {e}")

    return result


def sell(ticker: str, name: str = "", qty: int = 0) -> dict:
    """
    시장가 매도 (TRADING_MODE 자동 분기)

    Args:
        ticker:  종목코드 (6자리)
        name:    종목명 (로그용)
        qty:     매도 수량 (0이면 보유 전량 자동 조회)

    Returns:
        {
            "success":     bool,
            "order_no":    str | None,
            "ticker":      str,
            "name":        str,
            "qty":         int,
            "sell_price":  int,        # 매도 시점 현재가 (추정)
            "mode":        str,
            "message":     str,
        }
    """
    result = _empty_result(ticker, name)

    token = _get_token()
    if not token:
        result["message"] = "토큰 없음"
        return result

    # qty=0이면 잔고 조회로 수량 확인
    sell_qty = qty
    if sell_qty <= 0:
        holdings = _get_holding_qty(ticker, token)
        sell_qty = holdings
        if sell_qty <= 0:
            result["message"] = f"{ticker} 보유 수량 없음"
            logger.warning(f"[order] {name}({ticker}) 보유 없음 — 매도 건너뜀")
            return result

    # 현재가 (추정 매도금액 계산용)
    price_info  = get_current_price(ticker)
    sell_price  = price_info.get("현재가", 0)

    # 주문 실행
    kis_rate_limiter.acquire()
    url   = f"{_get_base_url()}/uapi/domestic-stock/v1/trading/order-cash"
    tr_id = _TR["sell"][config.TRADING_MODE]
    acct_no, acct_cd = _get_account()
    headers = _base_headers(tr_id, token)
    headers["custtype"] = "P"

    body = {
        "CANO":          acct_no,
        "ACNT_PRDT_CD":  acct_cd,
        "PDNO":          ticker,
        "ORD_DVSN":      "01",       # 01: 시장가
        "ORD_QTY":       str(sell_qty),
        "ORD_UNPR":      "0",
        "SLL_TYPE":      "01",       # 01: 일반 매도
    }

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("rt_cd") == "0":
            order_no = data.get("output", {}).get("ODNO", "") or data.get("output", {}).get("odno", "")
            result.update({
                "success":    True,
                "order_no":   order_no,
                "qty":        sell_qty,
                "sell_price": sell_price,
                "message":    f"매도 체결 완료 — {sell_qty}주 (추정가 {sell_price:,}원)",
            })
            logger.info(
                f"[order] {config.TRADING_MODE} 매도 ✅  "
                f"{name}({ticker})  {sell_qty}주  "
                f"추정가 {sell_price:,}원  주문번호:{order_no}"
            )
        else:
            msg = data.get("msg1", data.get("msg_cd", "알 수 없는 오류"))
            result["message"] = f"매도 거부: {msg}"
            logger.warning(f"[order] {config.TRADING_MODE} 매도 실패 — {name}({ticker}): {msg}")

    except Exception as e:
        result["message"] = f"매도 오류: {e}"
        logger.error(f"[order] {ticker} 매도 예외: {e}")

    return result


def get_balance() -> dict:
    """
    계좌 잔고 조회 (TRADING_MODE 자동 분기)

    Returns:
        {
            "holdings": list[{ticker, name, qty, avg_price, current_price, profit_rate}],
            "available_cash": int,    # 매수 가능 금액 (원)
            "total_eval":    int,     # 총 평가금액
            "total_profit":  float,  # 총 손익률 (%)
        }
    """
    token = _get_token()
    if not token:
        logger.warning("[order] 토큰 없음 — 잔고 조회 불가")
        return {}

    kis_rate_limiter.acquire()
    url   = f"{_get_base_url()}/uapi/domestic-stock/v1/trading/inquire-balance"
    tr_id = _TR["balance"][config.TRADING_MODE]
    acct_no, acct_cd = _get_account()
    headers = _base_headers(tr_id, token)
    headers["custtype"] = "P"

    params = {
        "CANO":               acct_no,
        "ACNT_PRDT_CD":       acct_cd,
        "AFHR_FLPR_YN":       "N",
        "OFL_YN":             "",
        "INQR_DVSN":          "02",
        "UNPR_DVSN":          "01",
        "FUND_STTL_ICLD_YN":  "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN":          "00",
        "CTX_AREA_FK100":     "",
        "CTX_AREA_NK100":     "",
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("rt_cd") != "0":
            logger.warning(f"[order] 잔고 조회 실패: {data.get('msg1', '')}")
            return {}

        holdings = []
        for item in (data.get("output1") or []):
            qty = int(item.get("hldg_qty", 0))
            if qty <= 0:
                continue
            holdings.append({
                "ticker":        item.get("pdno", ""),
                "name":          item.get("prdt_name", ""),
                "qty":           qty,
                "avg_price":     float(item.get("pchs_avg_pric", 0)),
                "current_price": float(item.get("prpr", 0)),
                "profit_rate":   float(item.get("evlu_pfls_rt", 0)),
            })

        summary = (data.get("output2") or [{}])[0]
        return {
            "holdings":       holdings,
            "available_cash": int(float(summary.get("ord_psbl_cash", 0))),
            "total_eval":     int(float(summary.get("tot_evlu_amt", 0))),
            "total_profit":   float(summary.get("evlu_pfls_rt", 0)),
        }

    except Exception as e:
        logger.error(f"[order] 잔고 조회 예외: {e}")
        return {}


# ── 내부 헬퍼 ─────────────────────────────────────────────────

def _empty_result(ticker: str, name: str) -> dict:
    return {
        "success":   False,
        "order_no":  None,
        "ticker":    ticker,
        "name":      name,
        "qty":       0,
        "buy_price": 0,
        "sell_price": 0,
        "total_amt": 0,
        "mode":      config.TRADING_MODE,
        "message":   "",
    }


def _get_holding_qty(ticker: str, token: str) -> int:
    """잔고에서 특정 종목 보유 수량 조회 (sell() 내부 전용)"""
    try:
        balance = get_balance()
        for h in balance.get("holdings", []):
            if h["ticker"] == ticker:
                return h["qty"]
    except Exception:
        pass
    return 0
