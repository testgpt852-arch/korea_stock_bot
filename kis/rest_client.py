"""
kis/rest_client.py
KIS REST API 호출 전담

[수정이력]
- v2.5:   get_volume_ranking() 추가
- v2.5.1: URL 수정 → /uapi/domestic-stock/v1/quotations/volume-rank
- v2.5.2: logger.debug → logger.info (Railway 로그 가시성 확보)
          응답 rt_cd/msg_cd 진단 로그 추가
- v2.7:   [버그수정] get_volume_ranking() KIS API 파라미터 오류 수정
          FID_COND_MRKT_DIV_CODE: "J"/"Q" → 항상 "J" 고정
          FID_INPUT_ISCD: "0000" → "0001"(코스피) / "1001"(코스닥) 으로 시장 구분
- v2.9:   get_rate_ranking() 신규 추가 (tr_id: FHPST01700000)
          등락률 순위 TOP 30 조회 — 거래량 적어도 급등하는 소형주 포착
- v3.0:   [get_rate_ranking 전면 개편]
          종목코드 필드 버그 수정: mksc_shrn_iscd → stck_shrn_iscd
          코스닥(Q): 모든 노이즈 제외 (관리/경고/위험/우선주/스팩/ETF/ETN, 1111111)
          코스피(J): 중형+소형 2회 호출 후 합산 → 대형주 사실상 제외
          등락률 범위 0~10% (FID_RSFL_RATE2="10") — 초기 급등 조기 포착
          FID_COND_MRKT_DIV_CODE 항상 "J" 고정 (rate API도 J 통일)
          내부 헬퍼 _fetch_rate_once() 분리
- v3.2:   [P1-2] KIS API Rate Limiter 적용 (python-kis 스펙: 실전 초당 19회)
          모든 API 호출 전 kis_rate_limiter.acquire() 호출
          get_stock_price() 응답에 시가(stck_oprc) 필드 추가 (T2 갭 상승 지원)
- v4.0:   [소~중형주 필터 + 호가 분석]
          get_volume_ranking() 코스피: FID_BLNG_CLS_CODE 중형(2)+소형(3) 2회 호출
            → 대형주 제외 (get_rate_ranking()과 동일 방식으로 통일)
          get_orderbook() 신규 (tr_id: FHKST01010200)
            → 매도/매수 호가 10개 + 잔량 조회 → volume_analyzer.analyze_orderbook() 전달용
"""

import requests
from utils.logger import logger
from utils.rate_limiter import kis_rate_limiter
from kis.auth import get_access_token
import config

_BASE_URL = "https://openapi.koreainvestment.com:9443"

# volume-rank API: FID_COND_MRKT_DIV_CODE는 항상 "J"
# 코스피/코스닥 구분은 FID_INPUT_ISCD로 처리
_MARKET_INPUT_ISCD = {
    "J": "0001",   # 코스피
    "Q": "1001",   # 코스닥
}


def get_stock_price(ticker: str) -> dict:
    """
    단일 종목 현재가 조회
    v3.2: rate_limiter 적용 + 시가(stck_oprc) 추가 (T2 갭 상승 감지용)
    """
    token = get_access_token()
    if not token:
        return {}
    kis_rate_limiter.acquire()
    url = f"{_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {
        "Authorization":  f"Bearer {token}",
        "appkey":         config.KIS_APP_KEY,
        "appsecret":      config.KIS_APP_SECRET,
        "tr_id":          "FHKST01010100",
        "Content-Type":   "application/json; charset=utf-8",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD":         ticker,
    }
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=5)
        resp.raise_for_status()
        out = resp.json().get("output", {})
        return {
            "종목명": out.get("hts_kor_isnm", ""),   # [v8.0 신규] 종목명 추가 (telegram_interactive /report, /evaluate 사용)
            "현재가": int(out.get("stck_prpr", 0)),
            "시가":   int(out.get("stck_oprc", 0)),
            "등락률": float(out.get("prdy_ctrt", 0)),
            "거래량": int(out.get("acml_vol", 0)),
        }
    except Exception as e:
        logger.warning(f"[rest] {ticker} 현재가 조회 실패: {e}")
        return {}


def get_volume_ranking(market_code: str) -> list[dict]:
    """
    KIS 거래량 순위 조회
    tr_id: FHPST01710000
    URL:   /uapi/domestic-stock/v1/quotations/volume-rank

    market_code: "J" = 코스피, "Q" = 코스닥  (volume_analyzer 인터페이스 유지)

    [v4.0 소~중형주 필터 추가]
    - 코스피(J): 중형(FID_BLNG_CLS_CODE="2") + 소형("3") 각각 호출 후 합산
      → 대형주 사실상 제외 (get_rate_ranking과 동일 방식으로 통일)
    - 코스닥(Q): 시장 특성상 소/중형 위주이므로 전체("0") 유지
      단, 스팩/ETF/ETN/우선주 제외 (FID_TRGT_EXLS_CLS_CODE)
    """
    token = get_access_token()
    if not token:
        logger.warning("[rest] 토큰 없음 — 거래량 순위 조회 불가")
        return []

    market_name = "코스피" if market_code == "J" else "코스닥"
    input_iscd  = _MARKET_INPUT_ISCD.get(market_code, "0001")
    logger.info(f"[rest] {market_name} 거래량 순위 조회 시작 (소~중형 필터 적용)...")

    if market_code == "J":
        # [v4.0] 코스피: 중형 + 소형 각각 조회 → 대형주 제외
        mid   = _fetch_volume_once(token, market_name + "[중형]", input_iscd, blng_cls="2",
                                   exls_cls="000111")   # 우선주/스팩/ETF 제외
        small = _fetch_volume_once(token, market_name + "[소형]", input_iscd, blng_cls="3",
                                   exls_cls="000111")
        seen, rows = {}, []
        for r in mid + small:
            key = r["종목코드"] or r["종목명"]
            if key and key not in seen:
                seen[key] = True
                rows.append(r)
        result = rows
    else:
        # 코스닥: 전체 조회 (소/중형 분리 없음), 스팩/ETF/ETN/우선주 제외
        result = _fetch_volume_once(token, market_name, input_iscd, blng_cls="0",
                                    exls_cls="000111")

    logger.info(f"[rest] {market_name} 거래량 파싱 완료 — {len(result)}종목 (소~중형)")
    return result


def _fetch_volume_once(token: str, label: str, input_iscd: str,
                       blng_cls: str, exls_cls: str) -> list[dict]:
    """
    거래량 순위 단일 호출 헬퍼 (get_volume_ranking 내부 전용)
    v4.0 신규: blng_cls(규모구분), exls_cls(제외구분) 파라미터 추가
    """
    kis_rate_limiter.acquire()
    url = f"{_BASE_URL}/uapi/domestic-stock/v1/quotations/volume-rank"
    headers = {
        "Authorization":  f"Bearer {token}",
        "appkey":         config.KIS_APP_KEY,
        "appsecret":      config.KIS_APP_SECRET,
        "tr_id":          "FHPST01710000",
        "custtype":       "P",
        "Content-Type":   "application/json; charset=utf-8",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE":   "J",        # 항상 "J" 고정
        "FID_COND_SCR_DIV_CODE":    "20171",
        "FID_INPUT_ISCD":           input_iscd,  # "0001"=코스피 / "1001"=코스닥
        "FID_DIV_CLS_CODE":         "0",         # 0:전체
        "FID_BLNG_CLS_CODE":        blng_cls,    # v4.0: 규모구분 (0=전체/2=중형/3=소형)
        "FID_TRGT_CLS_CODE":        "111111111", # 전체 종목 대상
        "FID_TRGT_EXLS_CLS_CODE":   exls_cls,   # v4.0: 제외 구분 (우선주/스팩/ETF 등)
        "FID_INPUT_PRICE_1":        "0",
        "FID_INPUT_PRICE_2":        "0",
        "FID_VOL_CNT":              "100",
        "FID_INPUT_DATE_1":         "",
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        body     = resp.json()
        rt_cd    = body.get("rt_cd",  "?")
        msg_cd   = body.get("msg_cd", "?")
        msg1     = body.get("msg1",   "")
        raw_list = body.get("output1") or body.get("output") or body.get("output2") or []

        logger.info(
            f"[rest] {label} 거래량 응답: rt_cd={rt_cd} msg_cd={msg_cd} "
            f"msg={msg1} 항목={len(raw_list)}"
        )
        if rt_cd == "0" and len(raw_list) == 0:
            logger.warning(
                f"[rest] {label} 응답 키 목록: {list(body.keys())} "
                f"| FID_INPUT_ISCD={input_iscd} FID_BLNG_CLS_CODE={blng_cls}"
            )

        result = []
        for item in raw_list:
            try:
                prdy_vol = int(item.get("prdy_vol", 0))
                acml_vol = int(item.get("acml_vol", 0))
                if prdy_vol <= 0 or acml_vol <= 0:
                    continue
                # [v4.1 버그수정] FID_BLNG_CLS_CODE가 API에서 무시됨
                # → hts_avls(억원 단위) 기반 사후 시총 필터로 대체
                hts_avls_raw = item.get("hts_avls", "0") or "0"
                try:
                    시총억원 = int(str(hts_avls_raw).replace(",", ""))
                except ValueError:
                    시총억원 = 0
                # hts_avls가 없는 경우(0) 필터 통과 (알 수 없으면 포함)
                if 시총억원 > 0 and 시총억원 > config.MARKET_CAP_MAX:
                    logger.debug(
                        f"[rest] 대형주 제외: {item.get('hts_kor_isnm','')} "
                        f"시총={시총억원:,}억 > {config.MARKET_CAP_MAX:,}억"
                    )
                    continue
                result.append({
                    "종목코드":   item.get("mksc_shrn_iscd", ""),
                    "종목명":     item.get("hts_kor_isnm", ""),
                    "현재가":     int(item.get("stck_prpr", 0)),
                    "등락률":     float(item.get("prdy_ctrt", 0.0)),
                    "누적거래량": acml_vol,
                    "전일거래량": prdy_vol,
                })
            except (ValueError, TypeError):
                continue

        logger.info(f"[rest] {label} 파싱 완료 — {len(result)}종목")
        return result

    except Exception as e:
        logger.warning(f"[rest] 거래량 순위 단일 호출 실패 ({label}): {e}")
        return []


def get_rate_ranking(market_code: str) -> list[dict]:
    """
    KIS 등락률 순위 조회 (v2.9 신규 / v3.0 개편)
    tr_id: FHPST01700000
    URL:   /uapi/domestic-stock/v1/ranking/fluctuation

    market_code: "J" = 코스피, "Q" = 코스닥

    [v3.0 개편 내용]
    - 코스닥 (Q): 모든 노이즈 제외 (FID_TRGT_EXLS_CLS_CODE="1111111")
    - 코스피 (J): 중형(FID_BLNG_CLS_CODE="2") + 소형("3") 각각 호출 후
      종목코드 기준 중복 제거 → 대형주 사실상 제외
    - 등락률 범위 0~10% (FID_RSFL_RATE1="0", FID_RSFL_RATE2="10")
    """
    token = get_access_token()
    if not token:
        logger.warning("[rest] 토큰 없음 — 등락률 순위 조회 불가")
        return []

    market_name = "코스피" if market_code == "J" else "코스닥"
    input_iscd  = _MARKET_INPUT_ISCD.get(market_code, "0001")

    if market_code == "Q":
        rows = _fetch_rate_once(token, market_name, input_iscd,
                                blng_cls="0", exls_cls="1111111")
    else:
        mid   = _fetch_rate_once(token, market_name + "[중형]", input_iscd,
                                 blng_cls="2", exls_cls="0001111")
        small = _fetch_rate_once(token, market_name + "[소형]", input_iscd,
                                 blng_cls="3", exls_cls="0001111")
        seen, rows = set(), []
        for r in mid + small:
            key = r["종목코드"] or r["종목명"]
            if key and key not in seen:
                seen.add(key)
                rows.append(r)

    rows.sort(key=lambda x: x["등락률"], reverse=True)
    result = rows[:30]
    logger.info(f"[rest] {market_name} 등락률 파싱 완료 — {len(result)}종목")
    return result


def _fetch_rate_once(token: str, label: str, input_iscd: str,
                     blng_cls: str, exls_cls: str) -> list[dict]:
    """
    등락률 순위 단일 호출 헬퍼 (get_rate_ranking 내부 전용)
    등락률 범위: 0~10% (초기 급등 조기 포착)
    v3.2: rate_limiter 적용
    """
    kis_rate_limiter.acquire()
    url = f"{_BASE_URL}/uapi/domestic-stock/v1/ranking/fluctuation"
    headers = {
        "Authorization":  f"Bearer {token}",
        "appkey":         config.KIS_APP_KEY,
        "appsecret":      config.KIS_APP_SECRET,
        "tr_id":          "FHPST01700000",
        "custtype":       "P",
        "Content-Type":   "application/json; charset=utf-8",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE":  "J",
        "FID_COND_SCR_DIV_CODE":   "20170",
        "FID_INPUT_ISCD":          input_iscd,
        "FID_RANK_SORT_CLS_CODE":  "0",
        "FID_INPUT_CNT_1":         "0",
        "FID_PRC_CLS_CODE":        "0",
        "FID_INPUT_PRICE_1":       "0",
        "FID_INPUT_PRICE_2":       "0",
        "FID_VOL_CNT":             "100",
        "FID_TRGT_CLS_CODE":       "0",
        "FID_TRGT_EXLS_CLS_CODE":  exls_cls,
        "FID_DIV_CLS_CODE":        "0",
        "FID_BLNG_CLS_CODE":       blng_cls,
        "FID_RSFL_RATE1":          "0",
        "FID_RSFL_RATE2":          "10",
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        body     = resp.json()
        rt_cd    = body.get("rt_cd",  "?")
        msg_cd   = body.get("msg_cd", "?")
        msg1     = body.get("msg1",   "")
        raw_list = body.get("output1") or body.get("output") or body.get("output2") or []

        logger.info(
            f"[rest] {label} 등락률 응답: rt_cd={rt_cd} msg_cd={msg_cd} "
            f"msg={msg1} 항목={len(raw_list)}"
        )

        result = []
        for item in raw_list:
            try:
                acml_vol = int(item.get("acml_vol", 0))
                prdy_vol = int(item.get("prdy_vol", 0))
                if acml_vol <= 0:
                    continue
                # [v4.1] 시총 사후 필터 (등락률 순위도 동일 적용)
                hts_avls_raw = item.get("hts_avls", "0") or "0"
                try:
                    시총억원 = int(str(hts_avls_raw).replace(",", ""))
                except ValueError:
                    시총억원 = 0
                if 시총억원 > 0 and 시총억원 > config.MARKET_CAP_MAX:
                    continue
                result.append({
                    "종목코드":   item.get("stck_shrn_iscd", ""),   # v3.0 버그수정
                    "종목명":     item.get("hts_kor_isnm", ""),
                    "현재가":     int(item.get("stck_prpr", 0)),
                    "등락률":     float(item.get("prdy_ctrt", 0.0)),
                    "누적거래량": acml_vol,
                    "전일거래량": prdy_vol if prdy_vol > 0 else 1,
                })
            except (ValueError, TypeError):
                continue

        return result

    except Exception as e:
        logger.warning(f"[rest] 등락률 순위 단일 호출 실패 ({label}): {e}")
        return []


def get_orderbook(ticker: str) -> dict:
    """
    [v4.0 신규] 단일 종목 호가 조회
    tr_id: FHKST01010200
    URL:   /uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn

    반환값:
    {
        "종목코드":   str,
        "매도호가":   list[{"가격": int, "잔량": int}],  # 10개, asks[0]이 최저매도가
        "매수호가":   list[{"가격": int, "잔량": int}],  # 10개, bids[0]이 최고매수가
        "총매도잔량": int,
        "총매수잔량": int,
    }
    빈 dict 반환 시 호가 조회 실패

    [사용처]
    - volume_analyzer.analyze_orderbook() 에 전달
    - 급등 감지 직후 1회 호출 → 매수벽/매도벽 강도 확인
    """
    token = get_access_token()
    if not token:
        return {}

    kis_rate_limiter.acquire()
    url = f"{_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
    headers = {
        "Authorization":  f"Bearer {token}",
        "appkey":         config.KIS_APP_KEY,
        "appsecret":      config.KIS_APP_SECRET,
        "tr_id":          "FHKST01010200",
        "Content-Type":   "application/json; charset=utf-8",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD":         ticker,
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=5)
        resp.raise_for_status()
        body = resp.json()
        out  = body.get("output1", {})
        out2 = body.get("output2", [{}])
        if isinstance(out2, list):
            out2 = out2[0] if out2 else {}

        if not out:
            logger.debug(f"[rest] {ticker} 호가 응답 비어있음 (output1 없음)")
            return {}

        # 매도호가 1~10 (asks[0] = 최저 매도가)
        asks = []
        for i in range(1, 11):
            price = int(out.get(f"askp{i}", 0) or 0)
            vol   = int(out.get(f"askp_rsqn{i}", 0) or 0)
            if price > 0:
                asks.append({"가격": price, "잔량": vol})

        # 매수호가 1~10 (bids[0] = 최고 매수가)
        bids = []
        for i in range(1, 11):
            price = int(out.get(f"bidp{i}", 0) or 0)
            vol   = int(out.get(f"bidp_rsqn{i}", 0) or 0)
            if price > 0:
                bids.append({"가격": price, "잔량": vol})

        total_ask = int(out2.get("total_askp_rsqn", 0) or 0)
        total_bid = int(out2.get("total_bidp_rsqn", 0) or 0)

        return {
            "종목코드":   ticker,
            "매도호가":   asks,
            "매수호가":   bids,
            "총매도잔량": total_ask,
            "총매수잔량": total_bid,
        }

    except Exception as e:
        logger.debug(f"[rest] {ticker} 호가 조회 실패: {e}")
        return {}
