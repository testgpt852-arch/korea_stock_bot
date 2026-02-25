"""
traders/position_manager.py
오픈 포지션 관리 전담 (Phase 4, v3.4 신규)

[역할]
모의투자(VTS) / 실전(REAL) 포지션의 진입·청산·조건 검사.
positions 테이블 기반으로 현재 보유 종목 추적.
trading_history 에 매매 이력 기록.

[주요 함수]
  can_buy(ticker)       ← realtime_alert._send_ai_followup 에서 호출
  open_position(...)    ← realtime_alert._send_ai_followup 에서 매수 체결 후 호출
  check_exit()          ← realtime_alert._poll_loop / _ws_loop 폴링 사이클마다 호출
  close_position(...)   ← check_exit 내부에서 청산 조건 충족 시 호출
  force_close_all()     ← main.py 14:50 스케줄에서 호출

[포지션 관리 규칙]
  · 동시 보유 한도: POSITION_MAX (기본 3종목)
  · 당일 손실 한도: DAILY_LOSS_LIMIT (기본 -3%) — 초과 시 신규 매수 중단
  · 이미 보유 종목 재진입 금지
  · 1차 익절: TAKE_PROFIT_1(+5%) — 전량 매도
  · 2차 익절: TAKE_PROFIT_2(+10%) — 전량 매도
  · 손절:     STOP_LOSS(-3%) — 전량 매도
  · 강제청산: FORCE_CLOSE_TIME(14:50) — 미청산 전부 시장가 매도

[ARCHITECTURE 의존성]
position_manager → tracking/db_schema    (get_conn)
position_manager → kis/order_client      (buy, sell, get_current_price)
position_manager → notifiers/telegram_bot (format_trade_executed, format_trade_closed)
position_manager ← reports/realtime_alert (can_buy, open_position, check_exit)
position_manager ← main.py               (force_close_all)

[절대 금지 규칙 — ARCHITECTURE #23 추가]
이 파일은 포지션 관리·주문 연동만 담당. 급등 감지·AI 분석·알림 포맷 생성 금지.
모든 함수는 동기(sync) — asyncio.run() 금지.
check_exit() / force_close_all() 는 내부에서 sell() 호출 후 telegram 발송까지 처리.
"""

from datetime import datetime, timezone, timedelta
from utils.logger import logger
import tracking.db_schema as db_schema
import config

KST = timezone(timedelta(hours=9))


# ── 공개 API ──────────────────────────────────────────────────

def can_buy(ticker: str) -> tuple[bool, str]:
    """
    매수 가능 여부 판단.

    Args:
        ticker: 종목코드

    Returns:
        (가능 여부, 사유 메시지)
    """
    # 자동매매 비활성 검사
    if not config.AUTO_TRADE_ENABLED:
        return False, "AUTO_TRADE_ENABLED=false"

    conn = db_schema.get_conn()
    try:
        c = conn.cursor()

        # ① 이미 보유 중인지 확인
        c.execute("SELECT id FROM positions WHERE ticker = ? AND mode = ?",
                  (ticker, config.TRADING_MODE))
        if c.fetchone():
            return False, f"{ticker} 이미 보유 중"

        # ② 동시 보유 한도 초과 여부
        c.execute("SELECT COUNT(*) FROM positions WHERE mode = ?", (config.TRADING_MODE,))
        current_count = c.fetchone()[0]
        if current_count >= config.POSITION_MAX:
            return False, f"동시 보유 한도 초과 ({current_count}/{config.POSITION_MAX})"

        # ③ 당일 손실 한도 초과 여부
        today_str = datetime.now(KST).strftime("%Y%m%d")
        c.execute("""
            SELECT SUM(profit_amount) FROM trading_history
            WHERE DATE(buy_time) = ? AND mode = ? AND sell_time IS NOT NULL
        """, (today_str, config.TRADING_MODE))
        row = c.fetchone()
        today_pnl_amount = row[0] or 0

        # 당일 투자원금 기준 손실률 계산
        invested = config.POSITION_BUY_AMOUNT * config.POSITION_MAX
        today_pnl_pct = (today_pnl_amount / invested * 100) if invested > 0 else 0
        if today_pnl_pct <= config.DAILY_LOSS_LIMIT:
            return False, f"당일 손실 한도 초과 ({today_pnl_pct:.1f}% <= {config.DAILY_LOSS_LIMIT}%)"

        return True, "OK"

    except Exception as e:
        logger.warning(f"[position] can_buy 검사 오류 ({ticker}): {e}")
        return False, f"검사 오류: {e}"
    finally:
        conn.close()


def open_position(ticker: str, name: str, buy_price: int,
                  qty: int, trigger_source: str) -> int | None:
    """
    포지션 개설 — DB 기록 (매수 체결 후 호출).

    Args:
        ticker:         종목코드
        name:           종목명
        buy_price:      체결가 (원)
        qty:            체결 수량
        trigger_source: 감지 소스 (volume / rate / websocket / gap_up)

    Returns:
        positions.id (성공) / None (실패)
    """
    now_kst  = datetime.now(KST)
    buy_time = now_kst.isoformat(timespec="seconds")

    conn = db_schema.get_conn()
    try:
        c = conn.cursor()

        # trading_history 에 먼저 기록 (sell_time=NULL = 미청산)
        c.execute("""
            INSERT INTO trading_history
                (ticker, name, buy_time, buy_price, qty, trigger_source, mode)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (ticker, name, buy_time, buy_price, qty, trigger_source, config.TRADING_MODE))
        trading_id = c.lastrowid

        # positions 테이블에 오픈 포지션 등록
        c.execute("""
            INSERT INTO positions
                (trading_id, ticker, name, buy_time, buy_price, qty, trigger_source, mode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (trading_id, ticker, name, buy_time, buy_price, qty, trigger_source, config.TRADING_MODE))
        pos_id = c.lastrowid

        conn.commit()
        logger.info(
            f"[position] 포지션 개설 ✅  {name}({ticker})  "
            f"{qty}주 × {buy_price:,}원  총 {qty * buy_price:,}원  "
            f"[{trigger_source}]  pos_id={pos_id}"
        )
        return pos_id

    except Exception as e:
        logger.error(f"[position] 포지션 개설 실패 ({ticker}): {e}")
        return None
    finally:
        conn.close()


def check_exit() -> list[dict]:
    """
    오픈 포지션 전체를 순회하며 익절/손절 조건 확인 후 청산 실행.
    _poll_loop() 폴링 사이클마다 호출 (run_in_executor 경유).

    Returns:
        청산된 포지션 정보 리스트 (텔레그램 발송용)
    """
    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT id, trading_id, ticker, name, buy_price, qty, trigger_source, buy_time
            FROM positions WHERE mode = ?
        """, (config.TRADING_MODE,))
        rows = c.fetchall()
    except Exception as e:
        logger.warning(f"[position] check_exit 조회 실패: {e}")
        return []
    finally:
        conn.close()

    if not rows:
        return []

    closed = []
    for row in rows:
        pos_id, trading_id, ticker, name, buy_price, qty, source, buy_time = row
        try:
            result = _check_single_exit(
                pos_id, trading_id, ticker, name, buy_price, qty, source
            )
            if result:
                closed.append(result)
        except Exception as e:
            logger.warning(f"[position] {ticker} 청산 검사 오류: {e}")

    return closed


def close_position(pos_id: int, ticker: str, name: str,
                   buy_price: int, qty: int,
                   reason: str) -> dict | None:
    """
    포지션 청산 실행 — 시장가 매도 후 DB 기록.

    Args:
        pos_id:    positions.id
        ticker:    종목코드
        name:      종목명
        buy_price: 매수가 (원)
        qty:       수량
        reason:    청산 사유 (take_profit_1 / stop_loss / force_close 등)

    Returns:
        청산 결과 dict (텔레그램 포맷에 전달) / None (실패)
    """
    from kis import order_client

    sell_result = order_client.sell(ticker, name, qty)
    if not sell_result["success"]:
        logger.warning(
            f"[position] {name}({ticker}) 매도 실패 ({reason}): {sell_result['message']}"
        )
        return None

    sell_price   = sell_result.get("sell_price", buy_price)
    profit_rate  = round((sell_price - buy_price) / buy_price * 100, 2) if buy_price > 0 else 0
    profit_amount = (sell_price - buy_price) * qty
    now_kst      = datetime.now(KST)
    sell_time    = now_kst.isoformat(timespec="seconds")

    # DB 업데이트
    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            UPDATE trading_history
            SET sell_time=?, sell_price=?, profit_rate=?, profit_amount=?, close_reason=?
            WHERE id = (SELECT trading_id FROM positions WHERE id = ?)
        """, (sell_time, sell_price, profit_rate, profit_amount, reason, pos_id))
        c.execute("DELETE FROM positions WHERE id = ?", (pos_id,))
        conn.commit()
    except Exception as e:
        logger.error(f"[position] {ticker} DB 청산 기록 실패: {e}")
    finally:
        conn.close()

    logger.info(
        f"[position] 포지션 청산 ✅  {name}({ticker})  {reason}  "
        f"매수가 {buy_price:,}원 → 매도가 {sell_price:,}원  "
        f"수익률 {profit_rate:+.2f}%  손익 {profit_amount:+,}원"
    )

    return {
        "ticker":        ticker,
        "name":          name,
        "buy_price":     buy_price,
        "sell_price":    sell_price,
        "qty":           qty,
        "profit_rate":   profit_rate,
        "profit_amount": profit_amount,
        "reason":        reason,
        "mode":          config.TRADING_MODE,
    }


def force_close_all() -> list[dict]:
    """
    14:50 강제 청산 — 미청산 포지션 전부 시장가 매도.
    main.py 스케줄에서 호출.

    Returns:
        청산된 포지션 정보 리스트
    """
    if not config.AUTO_TRADE_ENABLED:
        return []

    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT id, trading_id, ticker, name, buy_price, qty, trigger_source
            FROM positions WHERE mode = ?
        """, (config.TRADING_MODE,))
        rows = c.fetchall()
    except Exception as e:
        logger.warning(f"[position] force_close_all 조회 실패: {e}")
        return []
    finally:
        conn.close()

    if not rows:
        logger.info("[position] 강제 청산 대상 없음")
        return []

    logger.info(f"[position] 강제 청산 시작 — {len(rows)}종목")
    closed = []
    for row in rows:
        pos_id, trading_id, ticker, name, buy_price, qty, source = row
        result = close_position(pos_id, ticker, name, buy_price, qty, "force_close")
        if result:
            closed.append(result)

    return closed


def get_open_positions() -> list[dict]:
    """
    현재 오픈 포지션 목록 반환 (텔레그램 상태 조회용).
    """
    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT ticker, name, buy_price, qty, buy_time, trigger_source
            FROM positions WHERE mode = ?
            ORDER BY buy_time
        """, (config.TRADING_MODE,))
        return [
            {
                "ticker":   r[0], "name":     r[1],
                "buy_price": r[2], "qty":     r[3],
                "buy_time":  r[4], "source":  r[5],
            }
            for r in c.fetchall()
        ]
    except Exception as e:
        logger.warning(f"[position] 포지션 목록 조회 실패: {e}")
        return []
    finally:
        conn.close()


# ── 내부 헬퍼 ─────────────────────────────────────────────────

def _check_single_exit(pos_id: int, trading_id: int,
                       ticker: str, name: str,
                       buy_price: int, qty: int, source: str) -> dict | None:
    """
    단일 포지션 청산 조건 검사 + 실행.
    현재가를 조회해 익절/손절 기준과 비교.
    """
    from kis import order_client

    price_info    = order_client.get_current_price(ticker)
    current_price = price_info.get("현재가", 0)
    if current_price <= 0 or buy_price <= 0:
        return None

    profit_pct = (current_price - buy_price) / buy_price * 100

    # 청산 조건 판단
    reason = None
    if profit_pct >= config.TAKE_PROFIT_2:
        reason = "take_profit_2"
    elif profit_pct >= config.TAKE_PROFIT_1:
        reason = "take_profit_1"
    elif profit_pct <= config.STOP_LOSS:
        reason = "stop_loss"

    if reason is None:
        return None   # 청산 조건 미충족

    logger.info(
        f"[position] 청산 조건 충족 — {name}({ticker})  "
        f"현재가 {current_price:,}원  수익률 {profit_pct:+.2f}%  사유: {reason}"
    )
    return close_position(pos_id, ticker, name, buy_price, qty, reason)
