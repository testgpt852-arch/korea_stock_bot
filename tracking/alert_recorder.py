"""
tracking/alert_recorder.py
장중봇 알림 발송 직후 DB에 기록 (Phase 3, v3.3 신규)

[역할]
realtime_alert._dispatch_alerts() 가 알림 발송 후 이 함수를 호출.
alert_history 에 INSERT 하고, performance_tracker 에 추적 행을 예약(미완 상태)한다.

[설계 원칙 — ARCHITECTURE 준수]
- DB 기록만 담당. 수집·분석·발송 로직 없음.
- 동기(sync) 함수. realtime_alert 에서 asyncio 이벤트 루프 블로킹 없이 호출 가능.
  (sqlite3 단순 INSERT 는 수 ms → 이벤트 루프 영향 무시 가능)
- 실패해도 봇 알림 흐름 중단 없음 (try/except 내부 처리).

[ARCHITECTURE 의존성]
alert_recorder ← reports/realtime_alert  (유일 호출처)
alert_recorder → tracking/db_schema      (get_conn 사용)

[절대 금지 규칙 — ARCHITECTURE #19]
record_alert() 는 realtime_alert._dispatch_alerts() 에서만 호출.
다른 모듈(morning_report, closing_report 등)에서 직접 호출 금지.
"""

from datetime import datetime, timezone, timedelta
from utils.logger import logger
import tracking.db_schema as db_schema

KST = timezone(timedelta(hours=9))


def record_alert(analysis: dict) -> int | None:
    """
    장중봇 알림 1건을 alert_history + performance_tracker 에 기록.

    Args:
        analysis: volume_analyzer.poll_all_markets() 또는 analyze_ws_tick() 반환값.
                  필요 키: 종목코드, 종목명, 등락률, 직전대비(선택), 감지소스, 현재가(선택)

    Returns:
        삽입된 alert_history.id (성공) 또는 None (실패)
    """
    ticker = analysis.get("종목코드", "")
    name   = analysis.get("종목명", ticker)
    change = analysis.get("등락률", 0.0)
    delta  = analysis.get("직전대비", 0.0)
    source = analysis.get("감지소스", "unknown")
    price  = analysis.get("현재가", 0)

    now_kst    = datetime.now(KST)
    alert_time = now_kst.isoformat(timespec="seconds")
    alert_date = now_kst.strftime("%Y%m%d")

    try:
        conn = db_schema.get_conn()
        try:
            c = conn.cursor()

            # alert_history INSERT
            c.execute("""
                INSERT INTO alert_history
                    (ticker, name, alert_time, alert_date,
                     change_rate, delta_rate, source, price_at_alert)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (ticker, name, alert_time, alert_date,
                  change, delta, source, price))
            alert_id = c.lastrowid

            # performance_tracker 추적 행 예약 (수익률은 배치에서 채움)
            c.execute("""
                INSERT INTO performance_tracker
                    (alert_id, ticker, alert_date, price_at_alert)
                VALUES (?, ?, ?, ?)
            """, (alert_id, ticker, alert_date, price))

            conn.commit()
            logger.debug(
                f"[recorder] 기록 완료 — {name}({ticker}) "
                f"+{change:.1f}% [{source}] id={alert_id}"
            )
            return alert_id

        finally:
            conn.close()

    except Exception as e:
        logger.warning(f"[recorder] DB 기록 실패 ({ticker}): {e}")
        return None
