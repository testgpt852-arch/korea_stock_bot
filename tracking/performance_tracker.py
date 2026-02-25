"""
tracking/performance_tracker.py
알림 후 1/3/7일 실제 수익률 추적 배치 + 주간 통계 조회 (Phase 3, v3.3 신규)

[실행 시점]
main.py 스케줄러 → 매 거래일 18:45 run_batch() 호출.
(마감봇 18:30 완료 후, pykrx 마감 확정치 사용 가능 시점)

[동작 흐름]
① performance_tracker 테이블에서 미추적(done_Xd=0) 행 조회
   → 오늘 기준 캘린더 1/3/7일 전에 발송된 알림 대상
② pykrx 마감 확정치로 현재 종가 일괄 조회 (KOSPI+KOSDAQ 전종목)
③ 수익률 계산 → performance_tracker UPDATE

[get_weekly_stats]
weekly_report.py 가 호출해 지난 7일 성과 데이터를 가져간다.

[ARCHITECTURE 의존성]
performance_tracker ← main.py  (18:45 cron, run_batch)
performance_tracker ← reports/weekly_report  (get_weekly_stats)
performance_tracker → tracking/db_schema  (get_conn)
performance_tracker → pykrx  (마감 확정치 — 18:45 실행이므로 허용)

[절대 금지 규칙 — ARCHITECTURE #20]
run_batch() 는 main.py 18:45 cron 에서만 호출.
장중(09:00~15:30) 호출 금지 (pykrx 당일 미확정 데이터 방지).
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pykrx import stock as pykrx_stock
from utils.logger import logger
import tracking.db_schema as db_schema
import config

KST      = timezone(timedelta(hours=9))
TRACK_DAYS = [1, 3, 7]    # 추적 기간 (캘린더일 기준)


# ── 공개 API ──────────────────────────────────────────────────

def run_batch() -> dict:
    """
    수익률 추적 배치 실행.
    main.py 에서 매 거래일 18:45 에 호출.

    Returns:
        {"updated": int, "stats": list[dict]}
    """
    today_kst = datetime.now(KST)
    today_str = today_kst.strftime("%Y%m%d")
    logger.info(f"[perf] 수익률 추적 배치 시작 — 기준일: {today_str}")

    total_updated = 0
    for n_days in TRACK_DAYS:
        target_date  = (today_kst - timedelta(days=n_days)).strftime("%Y%m%d")
        done_col     = f"done_{n_days}d"
        price_col    = f"price_{n_days}d"
        return_col   = f"return_{n_days}d"
        date_col     = f"tracked_date_{n_days}d"

        cnt = _update_period(today_str, target_date, done_col, price_col, return_col, date_col)
        total_updated += cnt
        logger.info(f"[perf] {n_days}일 추적 업데이트: {cnt}건")

    stats = _get_trigger_stats()
    if stats:
        logger.info("[perf] === 트리거별 7일 승률 ===")
        for row in stats:
            logger.info(
                f"  [{row['trigger_type']}] "
                f"승률 {row['win_rate_7d']}% "
                f"(n={row['tracked_7d']}) "
                f"평균수익 {row['avg_return_7d']}%"
            )

    logger.info(f"[perf] 배치 완료 — 총 {total_updated}건 업데이트")
    return {"updated": total_updated, "stats": stats}


def get_weekly_stats() -> dict:
    """
    지난 7일 성과 데이터 반환. weekly_report.py 에서 호출.

    Returns:
        {
          "period":        str,           # "YYYY.MM.DD ~ YYYY.MM.DD"
          "total_alerts":  int,
          "trigger_stats": list[dict],
          "top_picks":     list[dict],    # 7일 수익률 상위 5종목
          "miss_picks":    list[dict],    # 7일 수익률 하위 5종목
        }
    """
    today_kst = datetime.now(KST)
    from_dt   = today_kst - timedelta(days=7)
    from_date = from_dt.strftime("%Y%m%d")
    to_date   = today_kst.strftime("%Y%m%d")

    def _fmt(d: str) -> str:
        return f"{d[:4]}.{d[4:6]}.{d[6:]}"

    conn = db_schema.get_conn()
    try:
        c = conn.cursor()

        c.execute("""
            SELECT COUNT(*) FROM alert_history
            WHERE alert_date BETWEEN ? AND ?
        """, (from_date, to_date))
        total_alerts = c.fetchone()[0]

        trigger_stats = _get_trigger_stats(conn=conn)

        c.execute("""
            SELECT ah.name, ah.ticker, ah.source, ah.change_rate,
                   pt.return_7d, pt.price_at_alert, pt.price_7d
            FROM performance_tracker pt
            JOIN alert_history ah ON pt.alert_id = ah.id
            WHERE pt.done_7d = 1 AND pt.return_7d IS NOT NULL
              AND ah.alert_date BETWEEN ? AND ?
            ORDER BY pt.return_7d DESC
            LIMIT 5
        """, (from_date, to_date))
        top_picks = [_row_to_dict(r) for r in c.fetchall()]

        c.execute("""
            SELECT ah.name, ah.ticker, ah.source, ah.change_rate,
                   pt.return_7d, pt.price_at_alert, pt.price_7d
            FROM performance_tracker pt
            JOIN alert_history ah ON pt.alert_id = ah.id
            WHERE pt.done_7d = 1 AND pt.return_7d IS NOT NULL
              AND ah.alert_date BETWEEN ? AND ?
            ORDER BY pt.return_7d ASC
            LIMIT 5
        """, (from_date, to_date))
        miss_picks = [_row_to_dict(r) for r in c.fetchall()]

        return {
            "period":        f"{_fmt(from_date)} ~ {_fmt(to_date)}",
            "total_alerts":  total_alerts,
            "trigger_stats": trigger_stats,
            "top_picks":     top_picks,
            "miss_picks":    miss_picks,
        }

    except Exception as e:
        logger.warning(f"[perf] 주간 통계 조회 실패: {e}")
        return {}
    finally:
        conn.close()


# ── 내부 헬퍼 ─────────────────────────────────────────────────

def _update_period(
    today_str: str,
    target_date: str,
    done_col: str,
    price_col: str,
    return_col: str,
    date_col: str,
) -> int:
    """
    target_date 에 발송된 알림 중 해당 기간 미추적 행을 pykrx 종가로 업데이트.
    """
    conn = db_schema.get_conn()
    updated = 0
    try:
        c = conn.cursor()
        c.execute(f"""
            SELECT id, ticker, price_at_alert
            FROM performance_tracker
            WHERE {done_col} = 0 AND alert_date = ?
        """, (target_date,))
        rows = c.fetchall()
        if not rows:
            return 0

        price_map = _fetch_prices_batch(today_str)

        for (row_id, ticker, price_at_alert) in rows:
            curr = price_map.get(ticker)
            if curr is None or curr <= 0:
                # 상장폐지·정지 등으로 가격 없음 → done 표시만
                c.execute(f"""
                    UPDATE performance_tracker
                    SET {done_col}=1, {date_col}=? WHERE id=?
                """, (today_str, row_id))
                conn.commit()
                updated += 1
                continue

            if not price_at_alert or price_at_alert <= 0:
                # 알림 시 가격 미기록 → done 표시만 (수익률 계산 불가)
                c.execute(f"""
                    UPDATE performance_tracker
                    SET {done_col}=1, {date_col}=? WHERE id=?
                """, (today_str, row_id))
                conn.commit()
                updated += 1
                continue

            ret = round((curr - price_at_alert) / price_at_alert * 100, 2)
            c.execute(f"""
                UPDATE performance_tracker
                SET {price_col}=?, {return_col}=?, {done_col}=1, {date_col}=?
                WHERE id=?
            """, (curr, ret, today_str, row_id))
            conn.commit()
            updated += 1

        return updated

    except Exception as e:
        logger.warning(f"[perf] 기간 업데이트 실패 ({target_date}): {e}")
        return 0
    finally:
        conn.close()


def _fetch_prices_batch(date_str: str) -> dict[str, int]:
    """
    pykrx 마감 확정치로 해당 날짜 전 종목 종가를 일괄 조회.
    {ticker: 종가(원)} 반환.
    """
    price_map: dict[str, int] = {}
    for market in ["KOSPI", "KOSDAQ"]:
        try:
            df = pykrx_stock.get_market_ohlcv_by_ticker(date_str, market=market)
            if df is None or df.empty:
                continue
            for ticker in df.index:
                close = df.loc[ticker, "종가"] if "종가" in df.columns else 0
                try:
                    close = int(float(close))
                except (ValueError, TypeError):
                    close = 0
                if close > 0:
                    price_map[ticker] = close
        except Exception as e:
            logger.warning(f"[perf] pykrx {market} 조회 실패 ({date_str}): {e}")
    return price_map


def _get_trigger_stats(conn: sqlite3.Connection | None = None) -> list[dict]:
    """trigger_stats 뷰 집계. conn 없으면 자체 생성."""
    own_conn = conn is None
    if own_conn:
        conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT trigger_type, total_alerts, tracked_7d,
                   win_7d, win_rate_7d, avg_return_7d
            FROM trigger_stats
            ORDER BY win_rate_7d DESC
        """)
        return [
            {
                "trigger_type":  r[0],
                "total_alerts":  r[1],
                "tracked_7d":    r[2],
                "win_7d":        r[3],
                "win_rate_7d":   r[4] or 0.0,
                "avg_return_7d": r[5] or 0.0,
            }
            for r in c.fetchall()
        ]
    except Exception as e:
        logger.warning(f"[perf] trigger_stats 조회 실패: {e}")
        return []
    finally:
        if own_conn:
            conn.close()


def _row_to_dict(r: tuple) -> dict:
    return {
        "name":          r[0],
        "ticker":        r[1],
        "source":        r[2],
        "change_rate":   r[3],
        "return_7d":     r[4],
        "price_at_alert": r[5],
        "price_7d":      r[6],
    }
