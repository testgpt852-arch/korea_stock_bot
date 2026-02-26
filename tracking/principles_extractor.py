"""
tracking/principles_extractor.py
매매 이력 → Trading Principles DB 자동 추출 배치 (Phase 5, v3.5 신규 / v4.3 Phase3 업데이트)

[역할]
trading_history 에서 완료된 거래를 집계해 trading_principles 를 자동으로 갱신.
매매 패턴(트리거 × 수익/손실)을 통계적으로 분석해 "고신뢰 원칙"을 추출.

[v4.3 추가]
trading_journal.pattern_tags 도 집계에 반영:
→ AI가 추출한 패턴 태그 빈도 + 승률 → principles 품질 향상
→ run_weekly_extraction() 완료 후 _integrate_journal_patterns() 추가 호출

[실행 시점]
main.py → 매주 일요일 03:00 run_weekly_extraction() 호출 (cron)
(시장 닫혀 있고 주간 데이터가 충분히 쌓인 시점)

[추출 로직]
1. trading_history 에서 trigger_source별, close_reason별 그룹핑
2. 승률(win_rate) = take_profit_1 + take_profit_2 건수 / 전체 건수
3. 총 거래 >= MIN_SAMPLE(5건) 이상일 때만 원칙 등록
4. win_rate >= 65% → confidence='high' / 50~65% → 'medium' / < 50% → 'low'
5. 이미 존재하는 원칙은 win_count / total_count / win_rate / confidence 업데이트
6. [v4.3] trading_journal 패턴 태그 집계 → 고빈도 패턴에 원칙 보강

[반환값]
{"inserted": int, "updated": int, "total_principles": int}

[ARCHITECTURE 의존성]
principles_extractor ← tracking/db_schema  (get_conn)
principles_extractor ← traders/position_manager  (trading_history 읽기 전용)
principles_extractor → tracking/ai_context  (다음 조회 시 활용)
principles_extractor → main.py  (run_weekly_extraction 호출)

[절대 금지 규칙 — ARCHITECTURE #30]
이 파일은 trading_principles 갱신 배치만 담당.
AI API 호출·텔레그램 발송·매수 로직 절대 금지.
모든 함수는 동기(sync).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from utils.logger import logger
import tracking.db_schema as db_schema

KST         = timezone(timedelta(hours=9))
MIN_SAMPLE  = 5      # 원칙 등록 최소 거래 건수
HIGH_CONF   = 65.0   # high confidence 기준 승률 (%)
MED_CONF    = 50.0   # medium confidence 기준 승률 (%)


# ── 공개 API ──────────────────────────────────────────────────

def run_weekly_extraction() -> dict:
    """
    매주 일요일 03:00 main.py에서 호출.
    trading_history 집계 → trading_principles 갱신.

    Returns:
        {"inserted": int, "updated": int, "total_principles": int}
    """
    logger.info("[principles] 매매 원칙 추출 배치 시작")
    conn = db_schema.get_conn()
    inserted = 0
    updated  = 0

    try:
        # ① trading_history 에서 trigger_source별 집계
        groups = _aggregate_by_trigger(conn)
        if not groups:
            logger.info("[principles] 집계할 거래 데이터 없음")
            return {"inserted": 0, "updated": 0, "total_principles": 0}

        now_kst = datetime.now(KST).isoformat()

        for group in groups:
            trigger     = group["trigger_source"]
            total       = group["total"]
            wins        = group["wins"]
            win_rate    = round(wins / total * 100, 1) if total > 0 else 0.0
            confidence  = _calc_confidence(win_rate, total)

            condition   = f"트리거: {trigger}"
            action      = "buy" if win_rate >= MED_CONF else "skip"
            summary     = f"{wins}/{total} 성공"

            # 이미 존재하는지 확인
            c = conn.cursor()
            c.execute("""
                SELECT id FROM trading_principles
                WHERE trigger_source = ? AND action = ?
            """, (trigger, "buy"))
            row = c.fetchone()

            if row:
                # UPDATE
                c.execute("""
                    UPDATE trading_principles
                    SET win_count=?, total_count=?, win_rate=?,
                        result_summary=?, confidence=?, last_updated=?
                    WHERE id=?
                """, (wins, total, win_rate, summary, confidence, now_kst, row[0]))
                updated += 1
            else:
                # INSERT (샘플 수 미달이면 건너뜀)
                if total < MIN_SAMPLE:
                    logger.debug(
                        f"[principles] {trigger} 샘플 부족 ({total}건 < {MIN_SAMPLE}) — 건너뜀"
                    )
                    continue
                c.execute("""
                    INSERT INTO trading_principles
                        (created_at, condition_desc, action, result_summary,
                         win_count, total_count, win_rate, confidence, trigger_source, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    now_kst, condition, action, summary,
                    wins, total, win_rate, confidence, trigger, now_kst
                ))
                inserted += 1

        conn.commit()

        # 전체 원칙 수 집계
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM trading_principles")
        total_p = c.fetchone()[0]

        logger.info(
            f"[principles] 배치 완료 — 신규:{inserted} 업데이트:{updated} "
            f"총 원칙:{total_p}개"
        )

        # [v4.3 Phase 3] trading_journal 패턴 태그 집계 → 원칙 DB 보강
        _integrate_journal_patterns()

        return {"inserted": inserted, "updated": updated, "total_principles": total_p}

    except Exception as e:
        logger.error(f"[principles] 배치 실패: {e}", exc_info=True)
        return {"inserted": 0, "updated": 0, "total_principles": 0}
    finally:
        conn.close()


# ── 내부 함수 ─────────────────────────────────────────────────

def _integrate_journal_patterns() -> None:
    """
    [v4.3 Phase 3] trading_journal.pattern_tags 집계 → trading_principles 보강.

    동작:
    1. trading_journal에서 최근 30일 pattern_tags + profit_rate 조회
    2. 태그별 빈도 + 승률 계산
    3. 이미 존재하는 원칙의 total_count 갱신 (win_count는 수익률 기준)
    4. 데이터 부족(건수 < MIN_SAMPLE) 시 건너뜀

    원칙 INSERT는 하지 않음 — 트리거 기반 집계(_aggregate_by_trigger)가 신규 INSERT 담당.
    journal 패턴은 기존 원칙의 신뢰도 보강에만 사용.
    """
    try:
        from tracking.trading_journal import get_weekly_patterns
        patterns = get_weekly_patterns(days=30)   # 30일 기준
        if not patterns:
            logger.debug("[principles] journal 패턴 없음 — 보강 건너뜀")
            return

        conn = db_schema.get_conn()
        try:
            c = conn.cursor()
            now_kst = datetime.now(KST).isoformat()
            updated = 0

            for p in patterns:
                tag      = p["tag"]
                count    = p["count"]
                wins     = p["win_count"]
                win_rate = p["win_rate"]

                if count < MIN_SAMPLE:
                    continue

                # tag 기반으로 condition_desc 에 매핑되는 원칙 찾기
                c.execute("""
                    SELECT id, total_count, win_count
                    FROM trading_principles
                    WHERE condition_desc LIKE ? OR action LIKE ?
                """, (f"%{tag}%", f"%{tag}%"))
                row = c.fetchone()

                if row:
                    pid, old_total, old_win = row
                    # 일지 데이터로 카운트 보강 (중복 방지: 기존값보다 크면 갱신)
                    new_total = max(old_total, count)
                    new_win   = max(old_win, wins)
                    new_conf  = _calc_confidence(win_rate, new_total)
                    c.execute("""
                        UPDATE trading_principles
                        SET total_count=?, win_count=?, win_rate=?,
                            confidence=?, last_updated=?
                        WHERE id=?
                    """, (new_total, new_win, win_rate, new_conf, now_kst, pid))
                    updated += 1

            conn.commit()
            if updated:
                logger.info(f"[principles] journal 패턴 보강 완료 — {updated}개 원칙 갱신")
        finally:
            conn.close()

    except Exception as e:
        logger.warning(f"[principles] journal 패턴 보강 실패 (비치명적): {e}")

def _aggregate_by_trigger(conn) -> list[dict]:
    """
    trading_history 에서 trigger_source별 총 거래 수 + 수익 거래 수 집계.
    청산 완료(sell_time NOT NULL)된 거래만 대상.
    """
    try:
        c = conn.cursor()
        c.execute("""
            SELECT
                COALESCE(trigger_source, 'unknown')            AS trigger_source,
                COUNT(*)                                        AS total,
                SUM(CASE WHEN close_reason IN
                    ('take_profit_1', 'take_profit_2') THEN 1 ELSE 0 END) AS wins
            FROM trading_history
            WHERE sell_time IS NOT NULL
            GROUP BY trigger_source
            HAVING COUNT(*) > 0
        """)
        rows = c.fetchall()
        return [
            {"trigger_source": r[0], "total": r[1], "wins": r[2]}
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[principles] 집계 실패: {e}")
        return []


def _calc_confidence(win_rate: float, total: int) -> str:
    """승률 + 샘플 수 → confidence 레벨 반환"""
    if total < MIN_SAMPLE:
        return "low"
    if win_rate >= HIGH_CONF:
        return "high"
    if win_rate >= MED_CONF:
        return "medium"
    return "low"
