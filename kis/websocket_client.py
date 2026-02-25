"""
kis/websocket_client.py
KIS 실시간 체결 WebSocket 수신 전담 (4단계)

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
               [이전 v3.1: MAX 3회, 30초 간격 → Railway 네트워크 끊김 대응 실패]
               [변경 v3.2: 무한 재시도, 5초 간격 (python-kis reconnect_interval=5 참조)]
═══════════════════════════════════════════════════════════════

[ARCHITECTURE 의존성]
websocket_client → volume_analyzer, realtime_alert
auth.py → websocket_client
"""

import asyncio
import json
import websockets
from utils.logger import logger
from kis.auth import get_access_token
import config

_WS_URL = "ws://ops.koreainvestment.com:21000"


class KISWebSocketClient:

    def __init__(self):
        self.connected          = False               # 연결 상태
        self.subscribed_tickers = set()               # 현재 구독 중인 종목
        self._ws                = None                # websockets 객체
        self._recv_callbacks    = []                  # 데이터 수신 콜백 목록
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
            self._ws = await websockets.connect(
                _WS_URL,
                ping_interval=20,
                ping_timeout=10,
            )
            self.connected = True
            self._reconnect_count = 0
            logger.info("[ws] KIS WebSocket 연결 완료")
        except Exception as e:
            logger.error(f"[ws] 연결 실패: {e}")
            self.connected = False

    # ── 2. 종목 구독 ─────────────────────────────────────────

    async def subscribe(self, ticker: str) -> None:
        """
        종목 실시간 체결 구독
        이미 구독 중이면 skip (중복 구독 금지)
        구독 후 ack 대기 (수신 검증 없는 구독 금지)
        """
        if not self.connected:
            logger.warning(f"[ws] {ticker} 구독 불가 — 연결 안 됨")
            return
        if ticker in self.subscribed_tickers:
            return

        msg = _build_subscribe_msg(ticker, subscribe=True)
        try:
            await self._ws.send(json.dumps(msg))
            # ack 대기: 최대 3초
            acked = await self._wait_for_ack(ticker, timeout=3)
            if acked:
                self.subscribed_tickers.add(ticker)
                logger.info(f"[ws] {ticker} 구독 완료")
            else:
                logger.warning(f"[ws] {ticker} ack 미수신 — 구독 미등록")
        except Exception as e:
            logger.warning(f"[ws] {ticker} 구독 요청 실패: {e}")

    # ── 3. 구독 해제 ─────────────────────────────────────────

    async def unsubscribe(self, ticker: str) -> None:
        """미구독 종목은 skip"""
        if ticker not in self.subscribed_tickers:
            return
        msg = _build_subscribe_msg(ticker, subscribe=False)
        try:
            await self._ws.send(json.dumps(msg))
            self.subscribed_tickers.discard(ticker)
            logger.info(f"[ws] {ticker} 구독해제 완료")
        except Exception as e:
            logger.warning(f"[ws] {ticker} 구독해제 실패: {e}")
            self.subscribed_tickers.discard(ticker)  # 오류여도 로컬에서 제거

    # ── 4. 연결 종료 (장 마감 1회) ───────────────────────────

    async def disconnect(self) -> None:
        """
        장 마감(15:30) 시 1회만 호출
        구독 중인 종목 전부 해제 후 연결 종료
        """
        if not self.connected:
            return

        # 구독 종목 전체 해제 후 종료
        for ticker in list(self.subscribed_tickers):
            await self.unsubscribe(ticker)

        if self._ws:
            await self._ws.close()
        self.connected = False
        logger.info("[ws] KIS WebSocket 연결 종료")

    # ── 5. 데이터 수신 루프 ───────────────────────────────────

    async def receive_loop(self, on_data: callable) -> None:
        """
        실시간 데이터 수신 루프
        on_data(parsed_data: dict) → 콜백으로 데이터 전달

        volume_analyzer.handle_tick() 등 콜백을 등록해서 사용
        """
        if not self.connected or not self._ws:
            logger.error("[ws] 수신 루프 시작 불가 — 연결 안 됨")
            return

        logger.info("[ws] 실시간 데이터 수신 시작")
        try:
            async for raw in self._ws:
                try:
                    data = _parse_tick(raw)
                    if data:
                        await on_data(data)
                except Exception as e:
                    logger.debug(f"[ws] 틱 파싱 오류: {e}")
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"[ws] 연결 끊김: {e}")
            self.connected = False
            # 네트워크 에러 시만 재연결 시도
            await self._reconnect_with_backoff()
        except Exception as e:
            logger.error(f"[ws] 수신 루프 오류: {e}")
            self.connected = False

    # ── 6. 에러 재연결 (네트워크 에러 시만, v3.2: 무한 재시도) ──

    async def _reconnect_with_backoff(self) -> None:
        """
        네트워크 에러로 연결이 끊겼을 때만 재연결 허용.
        의도적인 연결/종료 반복 절대 금지.

        [v3.2 변경]
        이전: MAX 3회, 30초 간격 → Railway 간헐적 끊김 시 3회 실패 후 WS 완전 사망
        변경: 회수 제한 없음, 5초 간격 (네트워크 복구될 때까지 계속 재시도)
             python-kis reconnect_interval=5 참조
        """
        attempt = 0
        while True:
            attempt += 1
            logger.info(
                f"[ws] 재연결 시도 {attempt}회 "
                f"({config.WS_RECONNECT_DELAY}초 후)..."
            )
            await asyncio.sleep(config.WS_RECONNECT_DELAY)

            try:
                # connect()는 내부에서 self.connected 체크 → 중복 연결 방지
                self.connected = False   # 강제로 False 설정해야 connect() 진행됨
                await self.connect()
                if not self.connected:
                    logger.warning(f"[ws] 재연결 실패 ({attempt}회) — 재시도 예정")
                    continue

                # 재연결 성공 → 기존 구독 종목 복원
                prev_tickers = list(self.subscribed_tickers)
                self.subscribed_tickers.clear()
                for ticker in prev_tickers:
                    await self.subscribe(ticker)
                logger.info(
                    f"[ws] 재연결 완료 ({attempt}회 시도) — "
                    f"{len(self.subscribed_tickers)}/{len(prev_tickers)}종목 재구독"
                )
                self._reconnect_count = 0
                return   # 성공 시 루프 탈출

            except asyncio.CancelledError:
                logger.info("[ws] 재연결 루프 취소 (CancelledError) — 장 마감으로 판단")
                return
            except Exception as e:
                logger.warning(f"[ws] 재연결 예외: {e} — {attempt}회 시도 후 재시도")

    # ── 7. ack 대기 (수신 검증) ──────────────────────────────

    async def _wait_for_ack(self, ticker: str, timeout: float = 3.0) -> bool:
        """구독 요청 후 ack 수신 대기"""
        try:
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=1.0)
                data = json.loads(raw) if isinstance(raw, str) else {}
                # KIS ack: header.tr_id가 구독한 종목의 tr_id와 일치하거나
                # body.msg1 = "SUBSCRIBE SUCCESS"
                if _is_ack(data, ticker):
                    return True
            return False
        except asyncio.TimeoutError:
            return False
        except Exception:
            return False


# ── 내부 유틸 ────────────────────────────────────────────────

def _build_subscribe_msg(ticker: str, subscribe: bool) -> dict:
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
                "tr_id":      "H0STCNT0",   # 국내주식 실시간 체결
                "tr_key":     ticker,
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


def _parse_tick(raw: str | bytes) -> dict | None:
    """
    KIS H0STCNT0 실시간 체결 데이터 파싱 (v3.1 필드 수정)

    KIS 파이프 형식: type|tr_id|cnt|data
    data 필드 (^구분):
      [0]  종목코드   [1] 체결시각(HHMMSS)  [2] 현재가
      [3]  전일대비부호               [4] 전일대비(등락폭)
      [5]  전일대비율(등락률%)        ← v3.1: 기존 [12] 오류 → [5] 정정
      [6]  가중평균가  [7] 시가  [8] 고가  [9] 저가
      [10] 매도호가1  [11] 매수호가1
      [12] 체결거래량(이 틱)
      [13] 누적거래량(당일 누적)      ← v3.1 신규: analyze_ws_tick RVOL용
      [14] 누적거래대금
    """
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        parts = raw.split("|")
        if len(parts) < 4:
            return None
        if parts[0] == "0":   # 실시간 데이터
            fields = parts[3].split("^")
            if len(fields) < 14:
                return None

            def safe_int(v: str) -> int:
                return int(v) if v and v.lstrip("-").isdigit() else 0

            def safe_float(v: str) -> float:
                try:    return float(v) if v else 0.0
                except: return 0.0

            return {
                "종목코드":   fields[0],
                "체결가":     safe_int(fields[2]),
                "등락률":     safe_float(fields[5]),    # v3.1 정정: 전일대비율
                "체결거래량": safe_int(fields[12]),     # 이 틱 거래량
                "누적거래량": safe_int(fields[13]),     # v3.1 신규: 당일 누적
                "체결시각":   fields[1],                # HHMMSS
            }
    except Exception:
        pass
    return None


# ── 싱글톤 인스턴스 (realtime_alert에서 import해서 사용) ──────
ws_client = KISWebSocketClient()
