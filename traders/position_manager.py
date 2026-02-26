"""
traders/position_manager.py
오픈 포지션 관리 전담 (Phase 4, v3.4 신규 / v4.2 Phase 2 확장 / v4.4 Phase 4 포트폴리오 인텔리전스)

[역할]
모의투자(VTS) / 실전(REAL) 포지션의 진입·청산·조건 검사.
positions 테이블 기반으로 현재 보유 종목 추적.
trading_history 에 매매 이력 기록.

[주요 함수]
  can_buy(ticker, ai_result)   ← realtime_alert._send_ai_followup 에서 호출
                                  [v4.2] ai_result 추가 — R/R 필터 적용
                                  [v4.4] 섹터 집중 체크 + 동적 POSITION_MAX + 강화된 손실 한도
  open_position(...)           ← realtime_alert._send_ai_followup 에서 매수 체결 후 호출
                                  [v4.2] stop_loss_price / market_env 파라미터 추가
                                  [v4.4] sector 파라미터 추가
  check_exit()                 ← realtime_alert._poll_loop / _ws_loop 폴링 사이클마다 호출
                                  [v4.2] Trailing Stop 포함
  update_trailing_stops()      ← [v4.2 신규] performance_tracker.run_batch() 종료 후 호출
  close_position(...)          ← check_exit 내부에서 청산 조건 충족 시 호출
  force_close_all()            ← main.py 14:50 스케줄에서 호출
                                  [v4.4] AI 분석 기반 선택적 청산으로 업그레이드
  get_effective_position_max() ← [v4.4 신규] 시장 환경별 동적 POSITION_MAX 반환

[포지션 관리 규칙 — v4.4 업데이트]
  · 동시 보유 한도: get_effective_position_max(market_env) (강세 5 / 횡보 3 / 약세 2)
  · 섹터 집중 한도: SECTOR_CONCENTRATION_MAX (기본 2종목) — 동일 섹터 초과 시 진입 차단
  · 당일 손실 한도: DAILY_LOSS_LIMIT (기본 -3%) — 실현+미실현 손익 포함 강화 체크
  · 이미 보유 종목 재진입 금지
  · R/R 필터: [v4.2] 강세장 1.2+, 약세장 2.0+ (can_buy에서 검사)
  · 1차 익절: TAKE_PROFIT_1(+5%) — 전량 매도
  · 2차 익절: TAKE_PROFIT_2(+10%) — 전량 매도
  · 손절: [v4.2] AI 제공 stop_loss_price 우선 / 없으면 config.STOP_LOSS(-3%) — 전량 매도
  · Trailing Stop: [v4.2]
      - 강세장: peak_price × 0.92 (-8%)
      - 약세장/횡보: peak_price × 0.95 (-5%)
      - 손절가 상향만 허용 (하향 금지)
      - peak_price는 폴링 사이클마다 갱신
  · 강제청산: FORCE_CLOSE_TIME(14:50) — [v4.4] AI 선택적 청산 (수익 중 유망 종목 유지)

[Trailing Stop 흐름]
  매수 진입 → open_position(stop_loss_price=AI값, market_env=강세장, sector=반도체)
            → positions.peak_price = buy_price, positions.stop_loss = AI 손절가
  폴링 사이클 → _check_single_exit()
              → 현재가 > peak_price면 peak_price 갱신 + trailing stop 상향
              → 현재가 <= stop_loss면 "trailing_stop" 청산
  18:45 배치 → update_trailing_stops() — 종가 기준 peak_price / stop_loss 일괄 갱신

[ARCHITECTURE 의존성]
position_manager → tracking/db_schema    (get_conn)
position_manager → kis/order_client      (buy, sell, get_current_price)
position_manager → notifiers/telegram_bot (format_trade_executed, format_trade_closed)
position_manager ← reports/realtime_alert (can_buy, open_position, check_exit)
position_manager ← main.py               (force_close_all)
position_manager ← tracking/performance_tracker (update_trailing_stops)  ← v4.2 추가

[절대 금지 규칙 — ARCHITECTURE #23]
이 파일은 포지션 관리·주문 연동만 담당. 급등 감지·AI 분석·알림 포맷 생성 금지.
모든 함수는 동기(sync) — asyncio.run() 금지.
"""

from datetime import datetime, timezone, timedelta
from utils.logger import logger
import tracking.db_schema as db_schema
import config

KST = timezone(timedelta(hours=9))

# Trailing Stop 비율 상수
_TS_RATIO_BULL  = 0.92   # 강세장: 고점 대비 -8%
_TS_RATIO_BEAR  = 0.95   # 약세장/횡보: 고점 대비 -5%


# ── [v4.4] 동적 POSITION_MAX ──────────────────────────────────

def get_effective_position_max(market_env: str = "") -> int:
    """
    [v4.4 Phase 4] 시장 환경에 따른 동적 POSITION_MAX 반환.
    강세장에서는 더 많은 종목 보유 허용, 약세장에서는 집중 관리.

    Args:
        market_env: 시장 환경 ("강세장" / "약세장/횡보" / "횡보" / "")

    Returns:
        동적 최대 보유 종목 수
        강세장 → POSITION_MAX_BULL (기본 5)
        횡보   → POSITION_MAX_NEUTRAL (기본 3)
        약세장 → POSITION_MAX_BEAR (기본 2)
        미지정 → POSITION_MAX (환경변수 설정값, 기본 3)
    """
    if "강세장" in market_env:
        return config.POSITION_MAX_BULL
    elif "약세장" in market_env:
        return config.POSITION_MAX_BEAR
    elif "횡보" in market_env:
        return config.POSITION_MAX_NEUTRAL
    else:
        return config.POSITION_MAX  # 하위 호환 (미지정 시)


# ── 공개 API ──────────────────────────────────────────────────

def can_buy(ticker: str, ai_result: dict | None = None,
            market_env: str = "") -> tuple[bool, str]:
    """
    매수 가능 여부 판단.

    [v4.2] ai_result 파라미터 추가 — R/R 필터 적용
    - 강세장: R/R < 1.2 → 진입 보류
    - 약세장/횡보: R/R < 2.0 → 진입 보류
    - ai_result 없거나 R/R 없으면 필터 미적용 (하위 호환)

    [v4.4] 추가 필터:
    - 동적 POSITION_MAX: 시장 환경에 따라 2~5종목으로 동적 조정
    - 섹터 집중 체크: 동일 섹터 SECTOR_CONCENTRATION_MAX(2)종목 이상 시 차단
    - 강화된 손실 한도: 실현 + 미실현 손익 합산으로 정밀 체크

    Args:
        ticker:     종목코드
        ai_result:  ai_analyzer.analyze_spike() 반환값 (선택)
        market_env: 시장 환경 문자열 (선택, watchlist_state에서 주입)

    Returns:
        (가능 여부, 사유 메시지)
    """
    if not config.AUTO_TRADE_ENABLED:
        return False, "AUTO_TRADE_ENABLED=false"

    # ── [v4.2] R/R 필터 ──────────────────────────────────────
    if ai_result:
        rr = ai_result.get("risk_reward_ratio")
        if rr is not None:
            if "강세장" in market_env:
                min_rr = 1.2
            elif "약세장" in market_env or "횡보" in market_env:
                min_rr = 2.0
            else:
                min_rr = 1.5   # 환경 미지정 시 중간값
            if rr < min_rr:
                return False, (
                    f"R/R 기준 미달 ({rr:.1f} < {min_rr} "
                    f"[{market_env or '환경미지정'}])"
                )

    # ── [v4.4] 동적 POSITION_MAX ────────────────────────────
    effective_max = get_effective_position_max(market_env)

    conn = db_schema.get_conn()
    try:
        c = conn.cursor()

        # ① 이미 보유 중인지 확인
        c.execute("SELECT id FROM positions WHERE ticker = ? AND mode = ?",
                  (ticker, config.TRADING_MODE))
        if c.fetchone():
            return False, f"{ticker} 이미 보유 중"

        # ② 동시 보유 한도 초과 여부 (v4.4: 동적 한도)
        c.execute("SELECT COUNT(*) FROM positions WHERE mode = ?", (config.TRADING_MODE,))
        current_count = c.fetchone()[0]
        if current_count >= effective_max:
            return False, (
                f"동시 보유 한도 초과 ({current_count}/{effective_max} "
                f"[{market_env or '환경미지정'}])"
            )

        # ── [v4.4] 섹터 집중 체크 ───────────────────────────
        import utils.watchlist_state as wls
        new_sector = wls.get_sector(ticker)
        if new_sector:
            c.execute("""
                SELECT COUNT(*) FROM positions
                WHERE mode = ? AND sector = ?
            """, (config.TRADING_MODE, new_sector))
            sector_count = c.fetchone()[0]
            if sector_count >= config.SECTOR_CONCENTRATION_MAX:
                return False, (
                    f"섹터 집중 한도 초과 ({new_sector}: "
                    f"{sector_count}/{config.SECTOR_CONCENTRATION_MAX}종목)"
                )

        # ③ 당일 손실 한도 초과 여부 (v4.4: 미실현 손익 포함 강화)
        today_str = datetime.now(KST).strftime("%Y-%m-%d")

        # 실현 손익
        c.execute("""
            SELECT SUM(profit_amount) FROM trading_history
            WHERE DATE(buy_time) = ? AND mode = ? AND sell_time IS NOT NULL
        """, (today_str, config.TRADING_MODE))
        row = c.fetchone()
        realized_pnl = row[0] or 0

        # [v4.4] 미실현 손익 (오픈 포지션의 현재 가치 - 매수가)
        unrealized_pnl = _calc_unrealized_pnl()

        total_pnl     = realized_pnl + unrealized_pnl
        invested_base = config.POSITION_BUY_AMOUNT * effective_max
        total_pnl_pct = (total_pnl / invested_base * 100) if invested_base > 0 else 0

        if total_pnl_pct <= config.DAILY_LOSS_LIMIT:
            return False, (
                f"당일 손실 한도 초과 "
                f"(실현 {realized_pnl:+,}원 + 미실현 {unrealized_pnl:+,}원 "
                f"= {total_pnl_pct:.1f}% <= {config.DAILY_LOSS_LIMIT}%)"
            )

        return True, "OK"

    except Exception as e:
        logger.warning(f"[position] can_buy 검사 오류 ({ticker}): {e}")
        return False, f"검사 오류: {e}"
    finally:
        conn.close()


def open_position(
    ticker: str, name: str, buy_price: int,
    qty: int, trigger_source: str,
    stop_loss_price: int | None = None,
    market_env: str = "",
    sector: str = "",
) -> int | None:
    """
    포지션 개설 — DB 기록 (매수 체결 후 호출).

    [v4.2] stop_loss_price / market_env 파라미터 추가
    - stop_loss_price: AI 제공 손절가 (원). None이면 config.STOP_LOSS 기본값 계산.
    - market_env: 시장 환경 ("강세장" / "약세장/횡보" / ""). Trailing Stop 비율 결정.

    [v4.4] sector 파라미터 추가
    - sector: 진입 종목의 섹터 (watchlist_state.get_sector()로 조회).
              섹터 분산 체크 및 AI 컨텍스트 활용에 사용.

    [v7.0] buy_market_context 자동 기록
    - watchlist_state.get_kospi_level()로 현재 KOSPI 레벨을 읽어
      trading_history.buy_market_context에 "KOSPI:6306" 형태로 자동 저장.
      memory_compressor.update_index_stats()가 이를 파싱해 레벨별 승률 집계.

    Args:
        ticker:          종목코드
        name:            종목명
        buy_price:       체결가 (원)
        qty:             체결 수량
        trigger_source:  감지 소스 (volume / rate / websocket / gap_up)
        stop_loss_price: [v4.2] AI 제공 손절가 (원). None이면 기본 -3%
        market_env:      [v4.2] 시장 환경
        sector:          [v4.4] 종목 섹터

    Returns:
        positions.id (성공) / None (실패)
    """
    now_kst  = datetime.now(KST)
    buy_time = now_kst.isoformat(timespec="seconds")

    # 손절가 결정: AI 제공값 우선, 없으면 config 기본값
    if stop_loss_price and stop_loss_price > 0:
        sl_price = stop_loss_price
        sl_source = "AI"
    else:
        sl_price = round(buy_price * (1 + config.STOP_LOSS / 100))
        sl_source = f"기본({config.STOP_LOSS}%)"

    conn = db_schema.get_conn()
    try:
        c = conn.cursor()

        # [v7.0] 매수 당시 KOSPI 레벨 기록 (kospi_index_stats 집계용)
        # watchlist_state에서 아침봇이 저장한 전날 KOSPI 종가를 읽어 컨텍스트 문자열로 저장
        try:
            import utils.watchlist_state as _wls
            _kospi = _wls.get_kospi_level()
            buy_market_context = f"KOSPI:{_kospi:.0f}" if _kospi > 0 else None
        except Exception:
            buy_market_context = None

        # trading_history 에 먼저 기록 (sell_time=NULL = 미청산)
        c.execute("""
            INSERT INTO trading_history
                (ticker, name, buy_time, buy_price, qty, trigger_source, mode, buy_market_context)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (ticker, name, buy_time, buy_price, qty, trigger_source, config.TRADING_MODE,
              buy_market_context))
        trading_id = c.lastrowid

        # positions 테이블에 오픈 포지션 등록
        # [v4.2] peak_price=buy_price (초기값), stop_loss, market_env 추가
        # [v4.4] sector 추가
        c.execute("""
            INSERT INTO positions
                (trading_id, ticker, name, buy_time, buy_price, qty,
                 trigger_source, mode, peak_price, stop_loss, market_env, sector)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trading_id, ticker, name, buy_time, buy_price, qty,
            trigger_source, config.TRADING_MODE,
            buy_price,   # peak_price 초기값 = 매수가
            sl_price,    # stop_loss
            market_env,
            sector,      # [v4.4]
        ))
        pos_id = c.lastrowid

        conn.commit()
        logger.info(
            f"[position] 포지션 개설 ✅  {name}({ticker})  "
            f"{qty}주 × {buy_price:,}원  총 {qty * buy_price:,}원  "
            f"[{trigger_source}]  손절가:{sl_price:,}원({sl_source})  "
            f"시장:{market_env or '미지정'}  섹터:{sector or '미분류'}  pos_id={pos_id}"
        )
        return pos_id

    except Exception as e:
        logger.error(f"[position] 포지션 개설 실패 ({ticker}): {e}")
        return None
    finally:
        conn.close()


def check_exit() -> list[dict]:
    """
    오픈 포지션 전체를 순회하며 익절/손절/Trailing Stop 조건 확인 후 청산 실행.
    _poll_loop() 폴링 사이클마다 호출 (run_in_executor 경유).

    [v4.2] Trailing Stop 포함 — peak_price 갱신 + 동적 손절가 비교

    Returns:
        청산된 포지션 정보 리스트 (텔레그램 발송용)
    """
    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        # [v4.2] peak_price, stop_loss, market_env 추가 SELECT
        c.execute("""
            SELECT id, trading_id, ticker, name, buy_price, qty,
                   trigger_source, buy_time, peak_price, stop_loss, market_env
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
        (pos_id, trading_id, ticker, name, buy_price, qty,
         source, buy_time, peak_price, stop_loss, market_env) = row
        try:
            result = _check_single_exit(
                pos_id, trading_id, ticker, name,
                buy_price, qty, source,
                peak_price or buy_price,
                stop_loss,
                market_env or "",
            )
            if result:
                closed.append(result)
        except Exception as e:
            logger.warning(f"[position] {ticker} 청산 검사 오류: {e}")

    return closed


def update_trailing_stops() -> int:
    """
    [v4.2 신규] 오픈 포지션의 peak_price / stop_loss 일괄 갱신.
    performance_tracker.run_batch() 종료 직후 호출 (18:45 배치).
    종가 기준으로 peak_price를 갱신하고 trailing stop을 상향 조정.

    Returns:
        갱신된 포지션 수
    """
    from kis import order_client

    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT id, ticker, name, buy_price, peak_price, stop_loss, market_env
            FROM positions WHERE mode = ?
        """, (config.TRADING_MODE,))
        rows = c.fetchall()
    except Exception as e:
        logger.warning(f"[position] update_trailing_stops 조회 실패: {e}")
        return 0
    finally:
        conn.close()

    if not rows:
        return 0

    updated = 0
    for row in rows:
        pos_id, ticker, name, buy_price, peak_price, stop_loss, market_env = row
        try:
            price_info    = order_client.get_current_price(ticker)
            current_price = price_info.get("현재가", 0)
            if current_price <= 0:
                continue

            new_peak = max(peak_price or buy_price, current_price)
            new_stop = _calc_trailing_stop(new_peak, market_env or "")

            # 손절가는 상향만 허용 (하향 금지)
            current_stop = stop_loss or round(buy_price * (1 + config.STOP_LOSS / 100))
            new_stop = max(new_stop, current_stop)

            conn2 = db_schema.get_conn()
            try:
                c2 = conn2.cursor()
                c2.execute("""
                    UPDATE positions SET peak_price = ?, stop_loss = ?
                    WHERE id = ?
                """, (new_peak, new_stop, pos_id))
                conn2.commit()
                updated += 1
                logger.info(
                    f"[position] Trailing Stop 갱신 — {name}({ticker})  "
                    f"현재가:{current_price:,}  고점:{new_peak:,}  "
                    f"손절가:{new_stop:,}({market_env or '미지정'})"
                )
            finally:
                conn2.close()

        except Exception as e:
            logger.warning(f"[position] {ticker} trailing stop 갱신 실패: {e}")

    logger.info(f"[position] Trailing Stop 일괄 갱신 완료 — {updated}종목")
    return updated


def close_position(pos_id: int, ticker: str, name: str,
                   buy_price: int, qty: int,
                   reason: str) -> dict | None:
    """
    포지션 청산 실행 — 시장가 매도 후 DB 기록.

    [v4.2] close_reason에 "trailing_stop" 추가
    [v4.3] 청산 완료 후 trading_journal.record_journal() 자동 호출 (Phase 3)

    Args:
        pos_id:    positions.id
        ticker:    종목코드
        name:      종목명
        buy_price: 매수가 (원)
        qty:       수량
        reason:    청산 사유 (take_profit_1 / take_profit_2 / stop_loss / trailing_stop / force_close)

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

    sell_price    = sell_result.get("sell_price", buy_price)
    profit_rate   = round((sell_price - buy_price) / buy_price * 100, 2) if buy_price > 0 else 0
    profit_amount = (sell_price - buy_price) * qty
    now_kst       = datetime.now(KST)
    sell_time     = now_kst.isoformat(timespec="seconds")

    # [v4.3] journal 기록용 — DELETE 전에 추가 컬럼 수집
    trading_id     = None
    trigger_source = ""
    market_env     = ""
    buy_time       = ""

    conn = db_schema.get_conn()
    try:
        c = conn.cursor()

        # [v4.3] journal에 필요한 정보 먼저 조회 (DELETE 전)
        c.execute("""
            SELECT trading_id, trigger_source, market_env, buy_time
            FROM positions WHERE id = ?
        """, (pos_id,))
        extra = c.fetchone()
        if extra:
            trading_id, trigger_source, market_env, buy_time = extra
            trigger_source = trigger_source or ""
            market_env     = market_env or ""
            buy_time       = buy_time or ""

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

    # [v4.3] 거래 일지 기록 — 비치명적, 실패해도 청산 결과 반환
    try:
        from tracking.trading_journal import record_journal
        record_journal(
            trading_id    = trading_id,
            ticker        = ticker,
            name          = name,
            buy_time      = buy_time,
            sell_time     = sell_time,
            buy_price     = buy_price,
            sell_price    = sell_price,
            profit_rate   = profit_rate,
            trigger_source= trigger_source,
            close_reason  = reason,
            market_env    = market_env,
        )
    except Exception as e:
        logger.warning(f"[position] journal 기록 실패 (비치명적): {e}")

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
    14:50 강제 청산 — [v4.4] AI 분석 기반 선택적 청산으로 업그레이드.
    수익 중이고 유망한 종목은 15:20까지 유지 허용.
    main.py 스케줄에서 호출.

    [v4.4 선택적 청산 로직]
    1. 각 포지션의 현재 수익률 계산
    2. ai_analyzer.analyze_selective_close() 로 청산/유지 AI 판단
    3. "유지" 판단된 종목: 15:20 최종 청산 스케줄로 위임 (force_close_deferred에 등록)
    4. "청산" 판단된 종목: 즉시 시장가 매도

    Returns:
        즉시 청산된 포지션 정보 리스트
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

    logger.info(f"[position] 선택적 강제청산 분석 시작 — {len(rows)}종목")

    # ── 현재가 조회 + 수익률 계산 ──────────────────────────────
    from kis import order_client as oc
    positions_with_pnl: list[dict] = []
    for row in rows:
        pos_id, trading_id, ticker, name, buy_price, qty, source = row
        try:
            cur = oc.get_current_price(ticker).get("현재가", buy_price)
        except Exception:
            cur = buy_price
        profit_rate = (cur - buy_price) / buy_price * 100 if buy_price > 0 else 0
        positions_with_pnl.append({
            "pos_id": pos_id, "trading_id": trading_id,
            "ticker": ticker, "name": name,
            "buy_price": buy_price, "qty": qty,
            "source": source, "profit_rate": profit_rate,
            "current_price": cur,
        })

    # ── AI 선택적 청산 판단 ───────────────────────────────────
    market_env = ""
    try:
        import utils.watchlist_state as wls
        market_env = wls.get_market_env()
    except Exception:
        pass

    try:
        from analyzers.ai_analyzer import analyze_selective_close
        close_verdict = analyze_selective_close(positions_with_pnl, market_env)
    except Exception as e:
        logger.warning(f"[position] 선택적청산 AI 실패 → 전량 청산: {e}")
        # AI 실패 시 기존 동작: 전량 청산
        close_verdict = {p["ticker"]: "청산" for p in positions_with_pnl}

    # ── 청산 실행 ───────────────────────────────────────────────
    closed = []
    deferred = []
    for pos in positions_with_pnl:
        ticker  = pos["ticker"]
        verdict = close_verdict.get(ticker, "청산")

        if verdict == "유지":
            logger.info(
                f"[position] 선택적청산 유지 — {pos['name']}({ticker}) "
                f"{pos['profit_rate']:+.1f}% → 15:20 최종 청산 위임"
            )
            deferred.append(pos)
        else:
            result = close_position(
                pos["pos_id"], ticker, pos["name"],
                pos["buy_price"], pos["qty"], "force_close"
            )
            if result:
                closed.append(result)

    # 유지 종목 내부 추적 (15:20 final_close_all에서 처리)
    if deferred:
        _register_deferred_close(deferred)
        logger.info(
            f"[position] 선택적청산 결과 — 즉시청산 {len(closed)}종목 / "
            f"15:20 유지 {len(deferred)}종목"
        )
    else:
        logger.info(f"[position] 강제 청산 완료 — {len(closed)}종목")

    return closed


def final_close_all() -> list[dict]:
    """
    [v4.4 신규] 15:20 최종 청산 — force_close_all()에서 '유지' 판정된 종목 청산.
    main.py 15:20 스케줄에서 호출 (AUTO_TRADE_ENABLED=true 시에만).

    Returns:
        청산된 포지션 정보 리스트
    """
    if not config.AUTO_TRADE_ENABLED:
        return []

    deferred = _pop_deferred_close()
    if not deferred:
        logger.info("[position] 최종 청산 대상 없음 (14:50 전량 청산됨)")
        return []

    logger.info(f"[position] 최종 청산 시작 — {len(deferred)}종목 (14:50 유지 종목)")
    closed = []
    for pos in deferred:
        # positions 테이블에 여전히 존재하는지 확인
        conn = db_schema.get_conn()
        try:
            c = conn.cursor()
            c.execute(
                "SELECT id, buy_price, qty FROM positions WHERE ticker = ? AND mode = ?",
                (pos["ticker"], config.TRADING_MODE)
            )
            row = c.fetchone()
        except Exception:
            row = None
        finally:
            conn.close()

        if not row:
            continue  # 이미 청산됨 (폴링에서 TS/익절로 청산됐을 수 있음)

        pos_id, buy_price, qty = row
        result = close_position(
            pos_id, pos["ticker"], pos["name"],
            buy_price, qty, "final_close"
        )
        if result:
            closed.append(result)

    logger.info(f"[position] 최종 청산 완료 — {len(closed)}종목")
    return closed


# ── 유지 종목 내부 레지스트리 ─────────────────────────────────

_deferred_close_list: list[dict] = []


def _register_deferred_close(positions: list[dict]) -> None:
    """14:50 유지 종목을 내부 레지스트리에 등록 (15:20 final_close_all 대상)"""
    global _deferred_close_list
    _deferred_close_list = positions


def _pop_deferred_close() -> list[dict]:
    """레지스트리에서 유지 종목 꺼내기 (한 번 꺼내면 비워짐)"""
    global _deferred_close_list
    result = _deferred_close_list.copy()
    _deferred_close_list = []
    return result


def get_open_positions() -> list[dict]:
    """
    현재 오픈 포지션 목록 반환 (텔레그램 상태 조회용).
    [v4.2] peak_price, stop_loss 필드 추가
    [v4.4] sector 필드 추가
    """
    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT ticker, name, buy_price, qty, buy_time, trigger_source,
                   peak_price, stop_loss, market_env, sector
            FROM positions WHERE mode = ?
            ORDER BY buy_time
        """, (config.TRADING_MODE,))
        return [
            {
                "ticker":      r[0], "name":       r[1],
                "buy_price":   r[2], "qty":         r[3],
                "buy_time":    r[4], "source":      r[5],
                "peak_price":  r[6], "stop_loss":   r[7],
                "market_env":  r[8], "sector":      r[9] or "",
            }
            for r in c.fetchall()
        ]
    except Exception as e:
        logger.warning(f"[position] 포지션 목록 조회 실패: {e}")
        return []
    finally:
        conn.close()


# ── 내부 헬퍼 ─────────────────────────────────────────────────

def _calc_trailing_stop(peak_price: int, market_env: str) -> int:
    """
    [v4.2] Trailing Stop 손절가 계산.
    강세장:   peak_price × 0.92 (고점 대비 -8%)
    약세장/횡보: peak_price × 0.95 (고점 대비 -5%)
    """
    ratio = _TS_RATIO_BULL if "강세장" in market_env else _TS_RATIO_BEAR
    return round(peak_price * ratio)


def _check_single_exit(
    pos_id: int, trading_id: int,
    ticker: str, name: str,
    buy_price: int, qty: int, source: str,
    peak_price: int,
    stop_loss: float | None,
    market_env: str,
) -> dict | None:
    """
    단일 포지션 청산 조건 검사 + 실행.
    [v4.2] Trailing Stop 로직 추가.

    우선순위:
      1. TAKE_PROFIT_2 (+10%) — 2차 익절
      2. TAKE_PROFIT_1 (+5%)  — 1차 익절
      3. Trailing Stop        — 고점 대비 -8%(강세) / -5%(약세) 이탈
      4. 절대 손절가          — AI 제공 stop_loss 또는 config.STOP_LOSS
    """
    from kis import order_client

    price_info    = order_client.get_current_price(ticker)
    current_price = price_info.get("현재가", 0)
    if current_price <= 0 or buy_price <= 0:
        return None

    profit_pct = (current_price - buy_price) / buy_price * 100

    # ── 익절 조건 먼저 확인 ──────────────────────────────────
    reason = None
    if profit_pct >= config.TAKE_PROFIT_2:
        reason = "take_profit_2"
    elif profit_pct >= config.TAKE_PROFIT_1:
        reason = "take_profit_1"

    if reason:
        logger.info(
            f"[position] 익절 조건 — {name}({ticker})  "
            f"현재가 {current_price:,}원  +{profit_pct:.2f}%  사유: {reason}"
        )
        return close_position(pos_id, ticker, name, buy_price, qty, reason)

    # ── [v4.2] Trailing Stop 갱신 및 검사 ───────────────────
    new_peak = max(peak_price, current_price)
    trailing_stop = _calc_trailing_stop(new_peak, market_env)

    # 절대 손절가 (AI 제공값 or config 기본값)
    abs_stop = int(stop_loss) if stop_loss else round(buy_price * (1 + config.STOP_LOSS / 100))

    # 최종 손절가 = Trailing Stop과 절대 손절가 중 더 높은 값 (상향만 허용)
    effective_stop = max(trailing_stop, abs_stop)

    # peak_price 갱신이 필요하면 DB 업데이트
    if new_peak > peak_price:
        _update_peak(pos_id, new_peak, effective_stop)
        logger.info(
            f"[position] 고점 갱신 — {name}({ticker})  "
            f"{peak_price:,} → {new_peak:,}원  "
            f"Trailing Stop: {effective_stop:,}원({market_env or '미지정'})"
        )

    # 손절 조건 확인
    if current_price <= effective_stop:
        # Trailing Stop vs 절대 손절 구분
        if current_price <= abs_stop:
            reason = "stop_loss"
        else:
            reason = "trailing_stop"
        logger.info(
            f"[position] 손절 조건 — {name}({ticker})  "
            f"현재가 {current_price:,}원  손절가 {effective_stop:,}원  "
            f"수익률 {profit_pct:+.2f}%  사유: {reason}"
        )
        return close_position(pos_id, ticker, name, buy_price, qty, reason)

    return None


def _update_peak(pos_id: int, new_peak: int, new_stop: float) -> None:
    """positions 테이블의 peak_price / stop_loss 갱신 (상향만)."""
    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            UPDATE positions
            SET peak_price = ?, stop_loss = MAX(stop_loss, ?)
            WHERE id = ?
        """, (new_peak, new_stop, pos_id))
        conn.commit()
    except Exception as e:
        logger.warning(f"[position] peak_price 갱신 실패 (pos_id={pos_id}): {e}")
    finally:
        conn.close()


def _calc_unrealized_pnl() -> int:
    """
    [v4.4 Phase 4] 현재 오픈 포지션의 미실현 손익 합산 (원).
    can_buy() 당일 손실 한도 체크 시 실현 손익과 합산하여 정밀 체크에 사용.

    [v6.0 이슈⑤ 수정] KIS API 장애 시 0 대신 보수적 기본값(KIS_FAILURE_SAFE_LOSS_PCT) 적용.
    기존: KIS 장애 → 0 반환 → 실제 손실 중인데도 daily_loss_limit 통과 가능
    수정: KIS 장애 → POSITION_BUY_AMOUNT × 보유종목수 × KIS_FAILURE_SAFE_LOSS_PCT% 추정값 반환
    → 보수적으로 손실이 있다고 가정해 추가 매수 과도 허용 방지
    """
    positions = get_open_positions()
    if not positions:
        return 0

    kis_available = False
    try:
        from kis import order_client
        kis_available = True
    except Exception:
        pass

    total = 0
    kis_failure_count = 0

    for pos in positions:
        if not kis_available:
            kis_failure_count += 1
            continue
        try:
            cur = order_client.get_current_price(pos["ticker"]).get("현재가", 0)
            if cur > 0 and pos["buy_price"] > 0:
                total += (cur - pos["buy_price"]) * pos["qty"]
            else:
                kis_failure_count += 1
        except Exception:
            kis_failure_count += 1

    # [v6.0] KIS 조회 실패한 포지션은 보수적 기본값으로 추정
    if kis_failure_count > 0:
        # KIS_FAILURE_SAFE_LOSS_PCT(기본 -1.5%)만큼 손실이 있다고 가정
        safe_loss_per_pos = config.POSITION_BUY_AMOUNT * (config.KIS_FAILURE_SAFE_LOSS_PCT / 100)
        total += round(safe_loss_per_pos * kis_failure_count)
        logger.debug(
            f"[position] KIS 조회 실패 {kis_failure_count}종목 → "
            f"보수적 손실 추정 {safe_loss_per_pos * kis_failure_count:,.0f}원 적용"
        )

    return total
