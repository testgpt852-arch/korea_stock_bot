"""
analyzers/intraday_analyzer.py
장중봇 — 모닝봇 픽 15종목 전담 감시 (AI 없음, 숫자 조건만)

[v13.0 개편 — REDESIGN_v13.md §6]
- poll_all_markets() 전 종목 KIS REST 스캔 로직 완전 제거
- set_watchlist(picks) 신규: morning_report.py 에서 픽 15종목 등록
- poll_all_markets() 재구현: 픽 15종목만 REST 개별 조회 감시
  ① 호가 잔량 변화 (매수벽 쌓이는지)
  ② 체결강도 (등락률 모멘텀)
  ③ 모닝봇 근거 기준 가격대 도달 알림
  ④ 이상 거래량 (픽 종목 내에서만)
- _detect_gap_up() 제거 (전 종목 스캔 로직 일부였음)
- analyze_orderbook() / analyze_ws_tick() / analyze_ws_orderbook_tick() 보존

반환값 규격 (ARCHITECTURE.md 계약):
{
    "종목코드": str, "종목명": str, "등락률": float, "직전대비": float,
    "거래량배율": float, "순간강도": float, "조건충족": bool,
    "감지시각": str, "감지소스": str,
    "호가분석": dict | None,
    "픽근거": str | None,   # [v13.0 신규] 모닝봇 근거 텍스트
    "알림유형": str | None,  # [v13.0 신규] "가격도달_목표"|"가격도달_손절"|"매수벽"|"급등모멘텀"
}

호가분석 dict 규격:
{
    "매수매도비율": float,
    "상위3집중도":  float,
    "호가강도":     str,    # "강세" | "중립" | "약세"
    "매수잔량":     int,
    "매도잔량":     int,
}

[수정이력]
- v1.0 ~ v9.0: 전 종목 KIS REST 스캔 기반
- v13.0: [REDESIGN_v13.md §6] 픽 15종목 전담 감시로 전면 전환
         poll_all_markets() 전 종목 스캔 제거 → 픽 15종목 REST 개별 조회
         set_watchlist(picks) 신규 추가
         _detect_gap_up() 제거 (전 종목 스캔 의존)
         픽근거 / 알림유형 반환 필드 추가
"""

import re
from datetime import datetime, timezone, timedelta
from utils.logger import logger
import config

_KST = timezone(timedelta(hours=9))


def _now_kst() -> str:
    """KST 현재 시각 → HH:MM:SS 문자열"""
    return datetime.now(_KST).strftime("%H:%M:%S")


# ── 모듈 레벨 상태 ────────────────────────────────────────────

# [v13.0] 모닝봇 픽 15종목 워치리스트
# morning_report.py → set_watchlist() 호출 시 등록됨
_watchlist_picks: list[dict] = []

# REST 폴링 직전 스냅샷 {종목코드: {"등락률": float, "거래량": int}}
_prev_snapshot:      dict[str, dict] = {}
_confirm_count:      dict[str, int]  = {}
_ws_alerted_tickers: set[str]        = set()

# 가격 도달 알림 중복 방지 (당일 종목별 1회)
_price_alerted: set[str] = set()


# ── [v13.0] 워치리스트 등록 ──────────────────────────────────

def set_watchlist(picks: list[dict]) -> None:
    """
    [v13.0 신규] 모닝봇 픽 15종목을 REST/WebSocket 감시 대상으로 등록.

    morning_report.py 에서 morning_analyzer.analyze() 완료·발송 직후 호출.
    이 함수 호출 이후부터 poll_all_markets() 가 픽 종목만 감시함.

    Args:
        picks: morning_analyzer.analyze() 반환값["picks"] 리스트
               각 항목 예시:
               {
                 "순위": 1,
                 "종목코드": "123456",
                 "종목명": "예시기업",
                 "근거": "DART 수주 320억(자기자본 28%)",
                 "목표등락률": "20%",
                 "손절기준": "-5%",
                 "테마여부": True,
                 "매수시점": "09:00~09:30 분봉 확인 후"
               }

    호출 규칙 (REDESIGN_v13.md §10):
        유일한 호출자 = morning_report.py
        다른 모듈 직접 호출 금지
    """
    global _watchlist_picks, _prev_snapshot, _confirm_count, _price_alerted, _ws_alerted_tickers
    _watchlist_picks     = picks or []
    _prev_snapshot       = {}
    _confirm_count       = {}
    _price_alerted       = set()
    _ws_alerted_tickers.clear()   # [v13.0] 재등록 시 이전 WS 알림 상태 초기화
    codes = [p.get("종목코드", "?") for p in _watchlist_picks]
    logger.info(
        f"[intraday] set_watchlist 완료 — {len(_watchlist_picks)}종목 등록: {codes}"
    )


def get_watchlist() -> list[dict]:
    """등록된 픽 워치리스트 복사본 반환 (읽기 전용)."""
    return _watchlist_picks.copy()


# ── 호가 분석 ─────────────────────────────────────────────────

def analyze_orderbook(orderbook: dict) -> dict | None:
    """
    KIS 호가잔량 분석 → 매수/매도 강도 판단.

    입력: rest_client.get_orderbook() 또는 websocket_client._parse_orderbook() 반환값
    출력: {"매수매도비율": float, "상위3집중도": float, "호가강도": str,
           "매수잔량": int, "매도잔량": int}

    판단 기준:
    - 강세: 매수매도비율 >= ORDERBOOK_BID_ASK_GOOD(2.0)
            또는 >= ORDERBOOK_BID_ASK_MIN(1.3) AND 매도벽 상위 집중
    - 약세: 매수매도비율 < 0.8
    - 중립: 그 외
    """
    if not orderbook:
        return None

    total_bid = orderbook.get("총매수잔량", 0)
    total_ask = orderbook.get("총매도잔량", 0)
    if total_ask <= 0:
        return None

    bid_ask_ratio = total_bid / total_ask

    bids = orderbook.get("매수호가", [])
    asks = orderbook.get("매도호가", [])
    top3_ask_vol   = sum(a["잔량"] for a in asks[:3]) if asks else 0
    top3_ask_ratio = (top3_ask_vol / total_ask) if total_ask > 0 else 0.0

    if bid_ask_ratio >= config.ORDERBOOK_BID_ASK_GOOD:
        강도 = "강세"
    elif (bid_ask_ratio >= config.ORDERBOOK_BID_ASK_MIN and
          top3_ask_ratio >= config.ORDERBOOK_TOP3_RATIO_MIN):
        강도 = "강세"
    elif bid_ask_ratio < 0.8:
        강도 = "약세"
    else:
        강도 = "중립"

    logger.debug(
        f"[orderbook] 매수매도비율={bid_ask_ratio:.2f} "
        f"매도상위3집중도={top3_ask_ratio:.2f} → {강도}"
    )
    return {
        "매수매도비율": round(bid_ask_ratio, 2),
        "상위3집중도":  round(top3_ask_ratio, 2),
        "호가강도":     강도,
        "매수잔량":     total_bid,
        "매도잔량":     total_ask,
    }


# ── [v13.0] REST 폴링 — 픽 15종목만 ──────────────────────────

def poll_all_markets() -> list[dict]:
    """
    [v13.0] 모닝봇 픽 15종목 REST 개별 조회 → 감시 조건 판단.

    [기존 동작 — 완전 제거]
    - get_rate_ranking() 전 종목 스캔
    - 코스피/코스닥 전체 등락률 순위 폴링
    - _detect_gap_up() 전 종목 갭 상승 감지

    [v13.0 신규 동작]
    픽 15종목만 get_stock_price() 개별 조회 후 아래 조건 감시:
    ① 가격 도달: 목표등락률 90% 이상 또는 손절 기준 도달 → "가격도달_목표/손절" 알림
    ② 급등 모멘텀: Δ등락률 >= PRICE_DELTA_MIN AND 체결강도 >= VOLUME_DELTA_MIN
                   CONFIRM_CANDLES회 연속 → "급등모멘텀" 알림
    ③ 매수벽: 호가 조회 후 "강세" 판정 → "매수벽" 알림

    워치리스트가 비어 있으면(set_watchlist 미호출) 빈 리스트 반환.

    Returns:
        list[dict] — 조건 충족 알림 목록 (상단 반환값 규격 참조)
    """
    global _prev_snapshot

    if not _watchlist_picks:
        logger.debug("[intraday] 워치리스트 없음 — poll 생략")
        return []

    from kis.rest_client import get_stock_price, get_orderbook

    alerted:          list[dict]       = []
    current_snapshot: dict[str, dict]  = {}
    is_warmup = not _prev_snapshot

    for pick in _watchlist_picks:
        ticker    = pick.get("종목코드", "")
        pick_name = pick.get("종목명", ticker)
        근거       = pick.get("근거", "")
        목표등락률  = pick.get("목표등락률", "")
        손절기준   = pick.get("손절기준", "")

        if not ticker or len(ticker) != 6:
            continue

        try:
            row = get_stock_price(ticker)
        except Exception as e:
            logger.warning(f"[intraday] {pick_name}({ticker}) 조회 실패: {e}")
            continue

        if not row:
            continue

        curr_price  = row.get("현재가", 0)
        change_rate = row.get("등락률", 0.0)
        acml_vol    = row.get("거래량", 0)

        if curr_price <= 0:
            continue

        current_snapshot[ticker] = {
            "현재가": curr_price,
            "등락률": change_rate,
            "거래량": acml_vol,
        }

        if is_warmup:
            continue

        prev = _prev_snapshot.get(ticker, {})

        # ── ① 가격 도달 알림 (당일 종목별 1회) ───────────────
        if ticker not in _price_alerted:
            triggered, 알림유형 = _check_price_trigger(
                ticker, curr_price, change_rate, 목표등락률, 손절기준
            )
            if triggered:
                _price_alerted.add(ticker)
                호가분석 = None
                if config.ORDERBOOK_ENABLED:
                    try:
                        호가분석 = analyze_orderbook(get_orderbook(ticker))
                    except Exception:
                        pass
                alerted.append(_build_alert(
                    ticker, pick_name, curr_price, change_rate,
                    prev, acml_vol, 호가분석, 근거, 알림유형,
                ))
                logger.info(
                    f"[intraday] {알림유형} — {pick_name} "
                    f"{change_rate:+.1f}% / 현재가={curr_price:,}원"
                )
                continue

        # ── ② 급등 모멘텀 ────────────────────────────────────
        if prev:
            delta_rate = change_rate - prev.get("등락률", change_rate)
            prev_vol   = max(prev.get("거래량", 1), 1)
            delta_vol  = max(0, acml_vol - prev_vol)
            순간강도    = (delta_vol / prev_vol * 100)

            single_ok = (
                delta_rate >= config.PRICE_DELTA_MIN and
                순간강도   >= config.VOLUME_DELTA_MIN
            )
            _confirm_count[ticker] = (
                _confirm_count.get(ticker, 0) + 1 if single_ok else 0
            )

            if _confirm_count.get(ticker, 0) >= config.CONFIRM_CANDLES:
                _confirm_count[ticker] = 0
                호가분석 = None
                if config.ORDERBOOK_ENABLED:
                    try:
                        호가분석 = analyze_orderbook(get_orderbook(ticker))
                    except Exception:
                        pass
                alert = _build_alert(
                    ticker, pick_name, curr_price, change_rate,
                    prev, acml_vol, 호가분석, 근거, "급등모멘텀",
                )
                alert["직전대비"] = round(delta_rate, 2)
                alert["순간강도"] = round(순간강도, 1)
                alert["거래량배율"] = round(acml_vol / prev_vol, 2)
                alerted.append(alert)
                logger.info(
                    f"[intraday] 급등모멘텀 — {pick_name} "
                    f"Δ등락률={delta_rate:+.2f}% 순간강도={순간강도:.1f}%"
                )
                continue

        # ── ③ 매수벽 감지 (등락률 양수인 종목 한정) ──────────
        if config.ORDERBOOK_ENABLED and change_rate >= config.MIN_CHANGE_RATE:
            # 분 단위 중복 방지: 같은 분에 이미 매수벽 알림 발송했으면 스킵
            ob_key = f"{ticker}_ob_{_now_kst()[:5]}"
            if ob_key not in _price_alerted:
                try:
                    ob = get_orderbook(ticker)
                    호가분석 = analyze_orderbook(ob)
                    if 호가분석 and 호가분석["호가강도"] == "강세":
                        _price_alerted.add(ob_key)
                        alerted.append(_build_alert(
                            ticker, pick_name, curr_price, change_rate,
                            prev, acml_vol, 호가분석, 근거, "매수벽",
                        ))
                        logger.info(
                            f"[intraday] 매수벽 — {pick_name} "
                            f"매수매도비율={호가분석['매수매도비율']:.2f}"
                        )
                except Exception as e:
                    logger.debug(f"[intraday] {pick_name} 호가 조회 실패: {e}")

    _prev_snapshot = current_snapshot

    if is_warmup:
        logger.info(
            f"[intraday] 워밍업 완료 — 픽 {len(_watchlist_picks)}종목 "
            f"스냅샷 저장 / 다음 사이클부터 감시 시작"
        )
    if alerted:
        logger.info(f"[intraday] 픽 감시 알림 {len(alerted)}건")

    return alerted


def _build_alert(
    ticker:     str,
    pick_name:  str,
    curr_price: int,
    change_rate: float,
    prev:       dict,
    acml_vol:   int,
    호가분석:    dict | None,
    근거:        str,
    알림유형:    str,
) -> dict:
    """알림 dict 공통 생성 헬퍼"""
    prev_rate = prev.get("등락률", change_rate)
    return {
        "종목코드":   ticker,
        "종목명":     pick_name,
        "현재가":     curr_price,
        "등락률":     change_rate,
        "직전대비":   round(change_rate - prev_rate, 2),
        "거래량배율": 0.0,
        "순간강도":   0.0,
        "조건충족":   True,
        "감지시각":   _now_kst(),
        "감지소스":   "watchlist",
        "호가분석":   호가분석,
        "픽근거":     근거,
        "알림유형":   알림유형,
    }


def _check_price_trigger(
    ticker:     str,
    curr_price: int,
    change_rate: float,
    목표등락률:  str,
    손절기준:   str,
) -> tuple[bool, str]:
    """
    [v13.0 내부 헬퍼] 모닝봇 근거 기준 가격 도달 조건 판단.

    Returns:
        (triggered: bool, 알림유형: str)
        알림유형: "가격도달_목표" | "가격도달_손절" | ""
    """
    # 상한가 도달 (29.5%+)
    if change_rate >= 29.5:
        return True, "가격도달_목표"

    # 목표 등락률 90% 이상 도달
    try:
        if 목표등락률 and "상한가" not in 목표등락률:
            target_pct = float(목표등락률.replace("%", "").strip())
            if target_pct > 0 and change_rate >= target_pct * 0.9:
                return True, "가격도달_목표"
    except (ValueError, AttributeError):
        pass

    # 손절 기준 도달
    try:
        if 손절기준:
            # [BUG-08 수정] 비율(%) 기준과 가격(원) 기준 두 가지 처리
            if "원" in 손절기준:
                # 가격 기준: "9,500원 하향 시", "9500원" 등
                price_str = 손절기준.split("원")[0].replace(",", "").strip()
                nums = re.findall(r"\d+", price_str)
                if nums:
                    stop_price = int(nums[-1])
                    if stop_price > 0 and curr_price <= stop_price:
                        return True, "가격도달_손절"
            else:
                # 비율 기준: "-5%" 또는 "-5"
                손절_str = 손절기준.replace("%", "").strip()
                손절_val = float(손절_str.replace(",", ""))
                if 손절_val < 0 and change_rate <= 손절_val:
                    return True, "가격도달_손절"
    except (ValueError, AttributeError):
        pass

    return False, ""


# ── WebSocket 체결 틱 기반 분석 ───────────────────────────────

def analyze_ws_tick(tick: dict, prdy_vol: int) -> dict | None:
    """
    KIS WebSocket 실시간 체결 틱 → 픽 종목 급등 조건 판단.

    [v13.0] 픽 워치리스트에 포함된 종목만 처리 (그 외 None 반환).
    realtime_alert._ws_loop() on_tick() 콜백에서 호출.
    호가분석은 realtime_alert 에서 REST 조회 후 채움.
    """
    ticker = tick.get("종목코드", "")

    # 픽 워치리스트 외 종목 무시
    pick_codes = {p.get("종목코드", "") for p in _watchlist_picks}
    if ticker not in pick_codes:
        return None

    rate = tick.get("등락률", 0.0)
    if rate < config.PRICE_CHANGE_MIN:
        return None
    if rate > config.MAX_CATCH_RATE:
        return None

    acml_vol  = tick.get("누적거래량", 0)
    acml_rvol = (acml_vol / prdy_vol) if prdy_vol > 0 else 0.0

    체결시각 = tick.get("체결시각", "")
    if len(체결시각) == 6:
        체결시각 = f"{체결시각[:2]}:{체결시각[2:4]}:{체결시각[4:]}"

    pick_info = next((p for p in _watchlist_picks if p.get("종목코드") == ticker), {})

    return {
        "종목코드":   ticker,
        "종목명":     tick.get("종목명", ticker),
        "등락률":     rate,
        "직전대비":   0.0,
        "거래량배율": round(acml_rvol, 2),
        "순간강도":   0.0,
        "조건충족":   True,
        "감지시각":   체결시각 or _now_kst(),
        "감지소스":   "websocket",
        "호가분석":   None,   # realtime_alert.on_tick() 에서 채움
        "픽근거":     pick_info.get("근거", ""),
        "알림유형":   "급등모멘텀",
    }


def analyze_ws_orderbook_tick(ob: dict, existing_result: dict) -> dict:
    """
    [v4.0 보존] WebSocket 실시간 호가 틱 → 기존 분석 결과에 호가분석 보강.

    ob: websocket_client._parse_orderbook() 반환값
    existing_result: analyze_ws_tick() 반환값 (호가분석=None 상태)
    """
    if not ob:
        return existing_result
    return {**existing_result, "호가분석": analyze_orderbook(ob)}


# ── 레거시 보존 ───────────────────────────────────────────────

def analyze(tick: dict) -> dict:
    """KIS WebSocket 틱 → 급등 조건 판단 (향후 WebSocket 재활성화용 보존)"""
    ticker   = tick.get("종목코드", "")
    rate     = tick.get("등락률",   0.0)
    prdy_vol = tick.get("전일거래량", 0)
    acml_vol = tick.get("누적거래량", 0)
    volume_ratio = (acml_vol / prdy_vol * 100) if prdy_vol > 0 else 0.0

    single_ok = (
        rate         >= config.PRICE_DELTA_MIN and
        volume_ratio >= config.VOLUME_DELTA_MIN
    )
    _confirm_count[ticker] = (
        _confirm_count.get(ticker, 0) + 1 if single_ok else 0
    )
    confirmed = _confirm_count.get(ticker, 0) >= config.CONFIRM_CANDLES
    pick_info = next((p for p in _watchlist_picks if p.get("종목코드") == ticker), {})

    return {
        "종목코드":   ticker,
        "종목명":     tick.get("종목명", ticker),
        "등락률":     rate,
        "직전대비":   0.0,
        "거래량배율": round(volume_ratio / 100, 2),
        "순간강도":   0.0,
        "조건충족":   confirmed,
        "감지시각":   _now_kst(),
        "감지소스":   "volume",
        "호가분석":   None,
        "픽근거":     pick_info.get("근거", ""),
        "알림유형":   None,
    }


def reset() -> None:
    """장 마감(15:30) 후 상태 전체 초기화"""
    global _watchlist_picks, _prev_snapshot
    _watchlist_picks = []
    _prev_snapshot   = {}
    _confirm_count.clear()
    _ws_alerted_tickers.clear()
    _price_alerted.clear()
    logger.info("[intraday] 워치리스트·스냅샷·카운터·WS상태 초기화 완료")
