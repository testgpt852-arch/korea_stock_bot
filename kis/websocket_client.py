"""
kis/websocket_client.py
KIS 실시간 체결·호가 WebSocket 수신 전담 (4단계)

═══════════════════════════════════════════════════════════════
🚨 KIS WebSocket 운영 규칙 — 위반 시 IP·앱키 차단 (ARCHITECTURE.md)
═══════════════════════════════════════════════════════════════
✅ 정상 흐름:  연결 → 구독 → 데이터수신 → 구독해제 → 종료
❌ 절대금지1: 연결/종료 루프 반복
❌ 절대금지2: 구독/해제 무한 반복
❌ 절대금지3: 수신검증 없는 구독
❌ 절대금지4: 장중 connect() 여러 번 호출

구현 규칙:
- connect()   → 장 시작(09:00) 1회만 호출. 이미 연결된 경우 즉시 return.
- disconnect()→ 장 마감(15:30) 1회만 호출. 모든 구독 해제 후 종료.
- subscribe() → 이미 구독 중이면 skip. 구독 후 ack 대기.
- reconnect   → 네트워크 에러 시만. 5초 간격, 회수 제한 없음 (v3.2).

[v4.0 호가 구독 추가]
- subscribe_orderbook(ticker): H0STASP0 실시간 호가 구독
  WS_ORDERBOOK_ENABLED=true 시 realtime_alert._ws_loop()에서 호출
  ⚠️ 체결(H0STCNT0)과 호가(H0STASP0) 합산 구독 수가 한도(40) 초과 금지
  → WS_ORDERBOOK_ENABLED=true 시 체결 20 + 호가 20 = 40으로 운영
- _parse_orderbook(): H0STASP0 파이프 포맷 파싱
  receive_loop에서 tr_id로 체결/호가 자동 분기

[ARCHITECTURE 의존성]
websocket_client → volume_analyzer, realtime_alert
auth.py → websocket_client
"""

import asyncio
import json
from typing import Callable, Optional
import websockets
from utils.logger import logger
from kis.auth import get_access_token
import config

# [v8.0 버그수정] TRADING_MODE에 따라 WebSocket URL 동적 분기
# 기존 단일 _WS_URL = "ws://ops.koreainvestment.com:21000" (실전 고정) → 오류
# KIS 공식 스펙:
#   실전(REAL): ws://ops.koreainvestment.com:21000
#   모의(VTS):  ws://ops.koreainvestment.com:31000
_WS_URL_REAL = "ws://ops.koreainvestment.com:21000"
_WS_URL_VTS  = "ws://ops.koreainvestment.com:31000"


def _get_ws_url() -> str:
    """TRADING_MODE에 따라 VTS 또는 REAL WebSocket URL 반환 (v8.0 신규)"""
    return _WS_URL_VTS if config.TRADING_MODE == "VTS" else _WS_URL_REAL


class KISWebSocketClient:

    def __init__(self):
        self.connected          = False
        self.subscribed_tickers = set()   # 체결(H0STCNT0) 구독 종목
        self.subscribed_ob      = set()   # 호가(H0STASP0) 구독 종목 (v4.0 신규)
        self._ws                = None
        self._recv_callbacks    = []
        self._reconnect_count   = 0

    # ── 1. 연결 (장 시작 1회) ─────────────────────────────────

    async def connect(self) -> None:
        """
        장 시작(09:00) 시 1회만 호출
        이미 연결된 경우 즉시 return — 재연결 시도 금지
        """
        if self.connected:
            logger.info("[ws] 이미 연결됨 — connect() 무시")
            return

        token = get_access_token()
        if not token:
            logger.error("[ws] 토큰 없음 — WebSocket 연결 불가")
            return

        try:
            ws_url = _get_ws_url()   # [v8.0] VTS/REAL 동적 분기
            logger.info(f"[ws] WebSocket 연결 시도: {ws_url} (모드: {config.TRADING_MODE})")
            self._ws = await websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=10,
            )
            self.connected = True
            self._reconnect_count = 0
            logger.info("[ws] KIS WebSocket 연결 완료")
        except Exception as e:
            logger.error(f"[ws] 연결 실패: {e}")
            self.connected = False

    # ── 2. 종목 구독 (체결 H0STCNT0) ────────────────────────

    async def subscribe(self, ticker: str) -> None:
        """
        종목 실시간 체결 구독
        이미 구독 중이면 skip. 구독 후 ack 대기.
        """
        if not self.connected:
            logger.warning(f"[ws] {ticker} 구독 불가 — 연결 안 됨")
            return
        if ticker in self.subscribed_tickers:
            return

        msg = _build_subscribe_msg(ticker, tr_id="H0STCNT0", subscribe=True)
        try:
            await self._ws.send(json.dumps(msg))
            acked = await self._wait_for_ack(ticker, timeout=3)
            if acked:
                self.subscribed_tickers.add(ticker)
                logger.info(f"[ws] {ticker} 체결 구독 완료")
            else:
                logger.warning(f"[ws] {ticker} 체결 ack 미수신 — 구독 미등록")
        except Exception as e:
            logger.warning(f"[ws] {ticker} 체결 구독 요청 실패: {e}")

    # ── 3. 호가 구독 (H0STASP0) — v4.0 신규 ─────────────────

    async def subscribe_orderbook(self, ticker: str) -> None:
        """
        종목 실시간 호가(H0STASP0) 구독
        v4.0 신규. WS_ORDERBOOK_ENABLED=true 시 realtime_alert에서 호출.

        ⚠️ 한도 주의: 체결(H0STCNT0) + 호가(H0STASP0) 합계 ≤ WS_WATCHLIST_MAX(40)
           → WS_ORDERBOOK_ENABLED 설정 시 realtime_alert._ws_loop()에서
             체결 WS_ORDERBOOK_SLOTS(20) + 호가 WS_ORDERBOOK_SLOTS(20) = 40으로 분할
        """
        if not self.connected:
            logger.warning(f"[ws] {ticker} 호가 구독 불가 — 연결 안 됨")
            return
        if ticker in self.subscribed_ob:
            return

        msg = _build_subscribe_msg(ticker, tr_id="H0STASP0", subscribe=True)
        try:
            await self._ws.send(json.dumps(msg))
            acked = await self._wait_for_ack(ticker, timeout=3)
            if acked:
                self.subscribed_ob.add(ticker)
                logger.info(f"[ws] {ticker} 호가 구독 완료")
            else:
                logger.warning(f"[ws] {ticker} 호가 ack 미수신 — 구독 미등록")
        except Exception as e:
            logger.warning(f"[ws] {ticker} 호가 구독 요청 실패: {e}")

    # ── 4. 구독 해제 ─────────────────────────────────────────

    async def unsubscribe(self, ticker: str) -> None:
        """체결 구독 해제. 미구독 종목은 skip."""
        if ticker not in self.subscribed_tickers:
            return
        msg = _build_subscribe_msg(ticker, tr_id="H0STCNT0", subscribe=False)
        try:
            await self._ws.send(json.dumps(msg))
            self.subscribed_tickers.discard(ticker)
            logger.info(f"[ws] {ticker} 체결 구독해제 완료")
        except Exception as e:
            logger.warning(f"[ws] {ticker} 체결 구독해제 실패: {e}")
            self.subscribed_tickers.discard(ticker)

    async def unsubscribe_orderbook(self, ticker: str) -> None:
        """호가 구독 해제. 미구독 종목은 skip."""
        if ticker not in self.subscribed_ob:
            return
        msg = _build_subscribe_msg(ticker, tr_id="H0STASP0", subscribe=False)
        try:
            await self._ws.send(json.dumps(msg))
            self.subscribed_ob.discard(ticker)
            logger.info(f"[ws] {ticker} 호가 구독해제 완료")
        except Exception as e:
            logger.warning(f"[ws] {ticker} 호가 구독해제 실패: {e}")
            self.subscribed_ob.discard(ticker)

    # ── 5. 연결 종료 (장 마감 1회) ───────────────────────────

    async def disconnect(self) -> None:
        """
        장 마감(15:30) 시 1회만 호출
        체결·호가 모든 구독 해제 후 연결 종료
        """
        if not self.connected:
            return

        for ticker in list(self.subscribed_tickers):
            await self.unsubscribe(ticker)
        for ticker in list(self.subscribed_ob):
            await self.unsubscribe_orderbook(ticker)

        if self._ws:
            await self._ws.close()
        self.connected = False
        logger.info("[ws] KIS WebSocket 연결 종료")

    # ── 6. 데이터 수신 루프 ───────────────────────────────────

    async def receive_loop(self, on_tick: Callable,
                           on_orderbook: Optional[Callable] = None) -> None:
        """
        실시간 데이터 수신 루프
        on_tick(parsed_tick: dict)           → 체결(H0STCNT0) 콜백
        on_orderbook(parsed_ob: dict) | None → 호가(H0STASP0) 콜백 (v4.0 신규)

        tr_id로 체결/호가 자동 분기:
          0|H0STCNT0|... → on_tick 호출
          0|H0STASP0|... → on_orderbook 호출 (on_orderbook이 None이면 skip)
        """
        if not self.connected or not self._ws:
            logger.error("[ws] 수신 루프 시작 불가 — 연결 안 됨")
            return

        logger.info("[ws] 실시간 데이터 수신 시작 (체결+호가 분기)")
        try:
            async for raw in self._ws:
                try:
                    # tr_id로 분기
                    tr_id = _peek_tr_id(raw)
                    if tr_id == "H0STCNT0":
                        data = _parse_tick(raw)
                        if data:
                            await on_tick(data)
                    elif tr_id == "H0STASP0" and on_orderbook:
                        data = _parse_orderbook(raw)
                        if data:
                            await on_orderbook(data)
                except Exception as e:
                    logger.debug(f"[ws] 데이터 파싱 오류: {e}")
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"[ws] 연결 끊김: {e}")
            self.connected = False
            await self._reconnect_with_backoff(on_tick, on_orderbook)
        except Exception as e:
            logger.error(f"[ws] 수신 루프 오류: {e}")
            self.connected = False

    # ── 7. 에러 재연결 (v3.2: 무한 재시도) ───────────────────

    async def _reconnect_with_backoff(self, on_tick=None, on_orderbook=None) -> None:
        """
        네트워크 에러로 연결이 끊겼을 때만 재연결 허용.
        의도적인 연결/종료 반복 절대 금지.
        v3.2: 회수 제한 없음, 5초 간격
        v6.0 이슈③: 지수 백오프 적용으로 KIS IP 차단 위험 완화.
            - 1~3회: 5초 간격 (기존 동일)
            - 4~6회: 30초 간격 (서버 부하 감소)
            - 7회+:  120초 간격 (KIS 서버 장애 시 연결 폭탄 방지)
            - 60회(120초 간격 기준 2시간) 초과 시 장 마감으로 간주해 중단
        """
        attempt = 0
        # 지수 백오프 딜레이 단계
        _BACKOFF_STAGES = [
            (3,  config.WS_RECONNECT_DELAY),   # 1~3회: 기본 간격(5초)
            (6,  30),                           # 4~6회: 30초
            (float('inf'), 120),                # 7회+:  120초
        ]
        _MAX_ATTEMPTS = 60  # 최대 재연결 횟수 (120초 간격 기준 약 2시간)

        while True:
            attempt += 1

            # [v6.0 이슈③] 최대 횟수 초과 시 중단
            if attempt > _MAX_ATTEMPTS:
                logger.error(
                    f"[ws] 재연결 {attempt}회 초과 — KIS 서버 장애 또는 IP 차단 가능성. "
                    f"장 마감으로 간주해 재연결 중단."
                )
                return

            # 현재 단계 딜레이 결정
            delay = _BACKOFF_STAGES[-1][1]
            for threshold, stage_delay in _BACKOFF_STAGES:
                if attempt <= threshold:
                    delay = stage_delay
                    break

            logger.info(
                f"[ws] 재연결 시도 {attempt}회 "
                f"({delay}초 후)..."
            )
            await asyncio.sleep(delay)

            try:
                self.connected = False
                await self.connect()
                if not self.connected:
                    logger.warning(f"[ws] 재연결 실패 ({attempt}회) — 재시도 예정")
                    continue

                # 재연결 성공 → 기존 체결 구독 복원
                prev_tickers = list(self.subscribed_tickers)
                self.subscribed_tickers.clear()
                for ticker in prev_tickers:
                    await self.subscribe(ticker)

                # 호가 구독도 복원
                prev_ob = list(self.subscribed_ob)
                self.subscribed_ob.clear()
                for ticker in prev_ob:
                    await self.subscribe_orderbook(ticker)

                logger.info(
                    f"[ws] 재연결 완료 ({attempt}회 시도) — "
                    f"체결 {len(self.subscribed_tickers)}/{len(prev_tickers)}종목 "
                    f"/ 호가 {len(self.subscribed_ob)}/{len(prev_ob)}종목 재구독"
                )
                self._reconnect_count = 0
                return

            except asyncio.CancelledError:
                logger.info("[ws] 재연결 루프 취소 (CancelledError) — 장 마감으로 판단")
                return
            except Exception as e:
                logger.warning(f"[ws] 재연결 예외: {e} — {attempt}회 시도 후 재시도")

    # ── 8. ack 대기 (수신 검증) ──────────────────────────────

    async def _wait_for_ack(self, ticker: str, timeout: float = 3.0) -> bool:
        """구독 요청 후 ack 수신 대기"""
        try:
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=1.0)
                data = json.loads(raw) if isinstance(raw, str) else {}
                if _is_ack(data, ticker):
                    return True
            return False
        except asyncio.TimeoutError:
            return False
        except Exception:
            return False


# ── 내부 유틸 ────────────────────────────────────────────────

def _build_subscribe_msg(ticker: str, tr_id: str, subscribe: bool) -> dict:
    """KIS WebSocket 구독/해제 메시지 생성"""
    return {
        "header": {
            "approval_key": get_access_token() or "",
            "custtype":     "P",
            "tr_type":      "1" if subscribe else "2",
            "content-type": "utf-8",
        },
        "body": {
            "input": {
                "tr_id":  tr_id,    # "H0STCNT0"(체결) 또는 "H0STASP0"(호가)
                "tr_key": ticker,
            }
        }
    }


def _is_ack(data: dict, ticker: str) -> bool:
    """KIS ack 메시지 판별"""
    try:
        body = data.get("body", {})
        msg  = body.get("msg1", "")
        return "SUBSCRIBE SUCCESS" in msg or ticker in str(data)
    except Exception:
        return False


def _peek_tr_id(raw: str | bytes) -> str:
    """
    파싱 없이 tr_id만 빠르게 추출
    KIS 파이프 포맷: type|tr_id|cnt|data
    """
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        parts = raw.split("|", 3)
        if len(parts) >= 2 and parts[0] == "0":
            return parts[1]
    except Exception:
        pass
    return ""


def _parse_tick(raw: str | bytes) -> dict | None:
    """
    KIS H0STCNT0 실시간 체결 데이터 파싱 (v3.1 필드 수정)

    data 필드 (^구분):
      [0]  종목코드   [1] 체결시각(HHMMSS)  [2] 현재가
      [3]  전일대비부호               [4] 전일대비(등락폭)
      [5]  전일대비율(등락률%)
      [12] 체결거래량(이 틱)
      [13] 누적거래량(당일 누적)
    """
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        parts = raw.split("|")
        if len(parts) < 4:
            return None
        if parts[0] == "0":
            fields = parts[3].split("^")
            if len(fields) < 14:
                return None

            def safe_int(v): return int(v) if v and v.lstrip("-").isdigit() else 0
            def safe_float(v):
                try: return float(v) if v else 0.0
                except: return 0.0

            return {
                "종목코드":   fields[0],
                "체결가":     safe_int(fields[2]),
                "등락률":     safe_float(fields[5]),
                "체결거래량": safe_int(fields[12]),
                "누적거래량": safe_int(fields[13]),
                "체결시각":   fields[1],
            }
    except Exception:
        pass
    return None


def _parse_orderbook(raw: str | bytes) -> dict | None:
    """
    [v4.0 신규] KIS H0STASP0 실시간 호가 데이터 파싱

    data 필드 (^구분, python-kis KisDomesticRealtimeOrderbook 참조):
      [0]  종목코드 (MKSC_SHRN_ISCD)
      [1]  영업시간 (HHMMSS)
      [2]  시간구분코드
      [3~12]   매도호가 1~10 (ASKP1~10)
      [13~22]  매수호가 1~10 (BIDP1~10)
      [23~32]  매도호가잔량 1~10 (ASKP_RSQN1~10)
      [33~42]  매수호가잔량 1~10 (BIDP_RSQN1~10)
      [43] 총매도호가잔량 (TOTAL_ASKP_RSQN)
      [44] 총매수호가잔량 (TOTAL_BIDP_RSQN)
      [53] 누적거래량 (ACML_VOL)

    반환값:
    {
        "종목코드":   str,
        "체결시각":   str,
        "매도호가":   list[{"가격": int, "잔량": int}],  # asks[0]=최저매도가
        "매수호가":   list[{"가격": int, "잔량": int}],  # bids[0]=최고매수가
        "총매도잔량": int,
        "총매수잔량": int,
    }
    """
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        parts = raw.split("|")
        if len(parts) < 4 or parts[0] != "0":
            return None

        fields = parts[3].split("^")
        if len(fields) < 45:
            return None

        def safe_int(v): return int(v) if v and v.lstrip("-").isdigit() else 0

        asks = [
            {"가격": safe_int(fields[3 + i]), "잔량": safe_int(fields[23 + i])}
            for i in range(10)
            if safe_int(fields[3 + i]) > 0
        ]
        bids = [
            {"가격": safe_int(fields[13 + i]), "잔량": safe_int(fields[33 + i])}
            for i in range(10)
            if safe_int(fields[13 + i]) > 0
        ]

        return {
            "종목코드":   fields[0],
            "체결시각":   fields[1],
            "매도호가":   asks,
            "매수호가":   bids,
            "총매도잔량": safe_int(fields[43]),
            "총매수잔량": safe_int(fields[44]),
        }
    except Exception:
        pass
    return None


# ── 싱글톤 인스턴스 ──────────────────────────────────────────
ws_client = KISWebSocketClient()
