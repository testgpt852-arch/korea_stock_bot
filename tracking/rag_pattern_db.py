"""
tracking/rag_pattern_db.py
신호 → 픽 → 실제결과 매핑 저장 + 유사패턴 검색 (v13.0 Step 5 신규)

[역할]
- 매일 모닝봇 픽 결과를 DB에 누적 저장 (RAG 학습 데이터)
- 오늘 신호와 유사한 과거 패턴을 검색해 Gemini 프롬프트용 텍스트 반환

[테이블]
  rag_patterns ← tracking/db_schema.py 에서 DDL 정의 + 초기화

[호출 규칙 — REDESIGN_v13 §10]
  save()                ← performance_tracker.run_batch() 직후만
  get_similar_patterns() ← morning_analyzer._pick_final() 내부만

[절대 금지]
  이 모듈에서 AI 호출, 텔레그램 발송, KIS 호출 금지
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from utils.logger import logger
import tracking.db_schema as db_schema

KST = timezone(timedelta(hours=9))


# ── 공개 API ──────────────────────────────────────────────────

def save(date: str, picks: list[dict], results: list[dict]) -> None:
    """
    당일 픽 결과를 rag_patterns 테이블에 저장.
    performance_tracker.run_batch() 직후 호출.

    Args:
        date:    기준일 (YYYYMMDD)
        picks:   morning_analyzer 반환 picks 리스트
                 각 원소 예시:
                 {
                   "순위": 1, "종목명": "삼성전자", "종목코드": "005930",
                   "근거": "...", "목표등락률": "20%",
                   "유형": "공시/테마/순환매",
                   "cap_tier": "소형_300억미만"  (optional)
                 }
        results: performance_tracker 가 수집한 당일 실제 결과 리스트
                 각 원소 예시:
                 {
                   "종목코드": "005930", "종목명": "삼성전자",
                   "max_return": 22.5,        # 당일 최고 등락률 (%)
                   "hit_20pct": True,
                   "hit_upper": False,
                   "signal_type": "DART_수주",  # 신호 유형 (optional)
                   "pattern_memo": "..."        # AI 생성 메모 (optional)
                 }
    """
    if not picks and not results:
        logger.info("[rag] 저장할 데이터 없음 — skip")
        return

    # results를 종목코드 키로 인덱싱
    result_map: dict[str, dict] = {r.get("종목코드", ""): r for r in results}

    created_at = datetime.now(KST).isoformat()
    rows_to_insert: list[tuple] = []

    # ── picks 기반 저장 (픽된 종목) ──────────────────────────
    picked_codes: set[str] = set()
    for pick in picks:
        code = pick.get("종목코드", "")
        name = pick.get("종목명", "")
        rank = pick.get("순위")
        signal_type = _infer_signal_type(pick, result_map.get(code, {}))
        cap_tier = pick.get("cap_tier") or _infer_cap_tier(pick)

        res = result_map.get(code, {})
        max_return  = res.get("max_return")
        hit_20pct   = bool(res.get("hit_20pct", False))
        hit_upper   = bool(res.get("hit_upper", False))
        pattern_memo = res.get("pattern_memo") or pick.get("근거", "")

        rows_to_insert.append((
            date, signal_type, name, code, cap_tier,
            True, rank,
            max_return, hit_20pct, hit_upper,
            pattern_memo, created_at,
        ))
        picked_codes.add(code)

    # ── results 중 픽 외 종목도 저장 (비교 학습용) ──────────
    for res in results:
        code = res.get("종목코드", "")
        if code in picked_codes:
            continue  # 이미 위에서 처리
        name = res.get("종목명", "")
        signal_type = res.get("signal_type", "미분류")
        cap_tier = res.get("cap_tier") or "미분류"
        max_return  = res.get("max_return")
        hit_20pct   = bool(res.get("hit_20pct", False))
        hit_upper   = bool(res.get("hit_upper", False))
        pattern_memo = res.get("pattern_memo", "")

        rows_to_insert.append((
            date, signal_type, name, code, cap_tier,
            False, None,
            max_return, hit_20pct, hit_upper,
            pattern_memo, created_at,
        ))

    if not rows_to_insert:
        logger.info("[rag] 삽입할 행 없음 — skip")
        return

    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        c.executemany("""
            INSERT INTO rag_patterns
                (date, signal_type, stock_name, stock_code, cap_tier,
                 was_picked, pick_rank,
                 max_return, hit_20pct, hit_upper,
                 pattern_memo, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows_to_insert)
        conn.commit()
        logger.info(f"[rag] 패턴 저장 완료 — {len(rows_to_insert)}건 (기준일: {date})")
    except Exception as e:
        logger.error(f"[rag] 패턴 저장 실패: {e}")
    finally:
        conn.close()


def get_similar_patterns(
    signal_type: str,
    cap_tier: str,
    limit: int = 5,
) -> str:
    """
    오늘 신호와 유사한 과거 패턴 검색 → Gemini 프롬프트용 텍스트 반환.
    morning_analyzer._pick_final() 내부에서 호출.

    Args:
        signal_type: 신호 유형 (예: "DART_수주", "순환매", "테마_반도체")
        cap_tier:    시총 구간 (예: "소형_300억미만", "소형_1000억미만", "중형")
        limit:       반환할 최근 유사 패턴 수 (기본 5)

    Returns:
        Gemini 프롬프트에 삽입할 텍스트 (유사패턴 없으면 빈 문자열)
    """
    conn = db_schema.get_conn()
    try:
        c = conn.cursor()

        # ── 1. 동일 signal_type + cap_tier 전체 통계 ──────────
        c.execute("""
            SELECT
                COUNT(*)                                          AS total,
                SUM(CASE WHEN hit_20pct = 1 THEN 1 ELSE 0 END)  AS hit20,
                SUM(CASE WHEN hit_upper = 1 THEN 1 ELSE 0 END)  AS hitUp,
                AVG(max_return)                                   AS avgRet
            FROM rag_patterns
            WHERE signal_type = ? AND cap_tier = ?
        """, (signal_type, cap_tier))
        stat = c.fetchone()
        total, hit20, hit_upper_cnt, avg_ret = stat if stat else (0, 0, 0, None)

        if not total:
            # signal_type 만으로 재시도 (cap_tier 조건 완화)
            c.execute("""
                SELECT
                    COUNT(*),
                    SUM(CASE WHEN hit_20pct = 1 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN hit_upper = 1 THEN 1 ELSE 0 END),
                    AVG(max_return)
                FROM rag_patterns
                WHERE signal_type = ?
            """, (signal_type,))
            stat = c.fetchone()
            total, hit20, hit_upper_cnt, avg_ret = stat if stat else (0, 0, 0, None)
            if not total:
                return ""

        pct_20   = round(hit20 / total * 100, 1) if total else 0
        pct_up   = round(hit_upper_cnt / total * 100, 1) if total else 0
        avg_ret  = round(avg_ret, 1) if avg_ret is not None else 0

        # ── 2. 최근 N건 개별 사례 ────────────────────────────
        c.execute("""
            SELECT date, stock_name, max_return, hit_20pct, hit_upper, pattern_memo
            FROM rag_patterns
            WHERE signal_type = ? AND cap_tier = ?
            ORDER BY date DESC
            LIMIT ?
        """, (signal_type, cap_tier, limit))
        recent_rows = c.fetchall()

        if not recent_rows:
            # cap_tier 조건 완화
            c.execute("""
                SELECT date, stock_name, max_return, hit_20pct, hit_upper, pattern_memo
                FROM rag_patterns
                WHERE signal_type = ?
                ORDER BY date DESC
                LIMIT ?
            """, (signal_type, limit))
            recent_rows = c.fetchall()

        # ── 3. 텍스트 조합 ───────────────────────────────────
        lines = [
            f"[RAG 과거패턴] {signal_type} / {cap_tier}",
            f"총 {total}건: 20%+ {hit20}건({pct_20}%), 상한가 {hit_upper_cnt}건({pct_up}%), 평균최고등락 {avg_ret}%",
        ]
        if recent_rows:
            lines.append("최근 사례:")
            for row in recent_rows:
                date_str, sname, max_r, h20, hup, memo = row
                result_tag = "상한가" if hup else ("20%+" if h20 else f"{max_r or 0:.1f}%")
                lines.append(
                    f"  {date_str} {sname}: {result_tag}"
                    + (f" — {memo[:60]}" if memo else "")
                )

        return "\n".join(lines)

    except Exception as e:
        logger.warning(f"[rag] 유사패턴 검색 실패: {e}")
        return ""
    finally:
        conn.close()


# ── 내부 헬퍼 ─────────────────────────────────────────────────

def _infer_signal_type(pick: dict, res: dict) -> str:
    """
    pick 딕트에서 signal_type 추론.
    picks에 직접 명시된 경우 우선 사용, 없으면 '유형' 필드로 대체.
    """
    if res.get("signal_type"):
        return res["signal_type"]
    pick_type = pick.get("signal_type") or pick.get("유형", "미분류")
    return pick_type


def _infer_cap_tier(pick: dict) -> str:
    """
    pick 딕트의 시가총액 정보로 cap_tier 결정.
    시가총액 키가 없으면 '미분류' 반환.
    """
    cap = pick.get("시가총액") or pick.get("market_cap")
    if cap is None:
        return "미분류"
    cap = int(cap)
    if cap < 30_000_000_000:          # 300억 미만
        return "소형_300억미만"
    elif cap < 100_000_000_000:        # 1000억 미만
        return "소형_1000억미만"
    elif cap < 300_000_000_000:        # 3000억 미만
        return "소형_3000억미만"
    else:
        return "중형"
