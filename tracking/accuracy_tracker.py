"""
tracking/accuracy_tracker.py
테마 예측 정확도 학습 DB — Phase 4-2 신규

[⚠️ v13.0 고아(Orphan) 경고]
이 파일은 현재 어디서도 호출되지 않습니다.
_build_signals() 삭제(v13.0)와 함께 signals 키 참조가 무효화됐고,
accuracy_tracker를 호출하는 모듈이 없습니다.

[역할]
- 아침봇/마감봇 oracle_analyzer 예측 테마 기록
- 다음날 마감봇 실제 급등 테마와 비교
- 신호 유형별 정확도 누적 → signal_weights 자동 조정
- oracle_analyzer에서 가중치 로드 가능

[절대 금지 규칙 — rule #100]
- 저장·조회·가중치 계산만 담당
- AI 분석·텔레그램 발송·KIS API 호출·수집 로직 절대 금지

[DB 의존성]
- theme_accuracy  테이블 (db_schema.py Phase 4-2 추가)
- signal_weights  테이블 (db_schema.py Phase 4-2 추가)

[수정이력]
- v10.6 Phase 4-2: 신규 생성
"""

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from utils.logger import logger
from tracking.db_schema import get_conn

_KST = timezone(timedelta(hours=9))

# 신호 유형 목록 (signal_analyzer.py의 발화신호 분류와 일치)
_SIGNAL_TYPES = [
    "신호1",   # 공시
    "신호2",   # 철강ETF/원자재
    "신호3",   # 증권사 리포트
    "신호4",   # 순환매(AI 그룹핑)
    "신호5",   # 정책뉴스
    "신호6",   # 지정학
    "신호7",   # 섹터 수급 (ETF+공매도)
    "신호8",   # 기업 이벤트 캘린더
]

# 기본 가중치 (미누적 상태)
_DEFAULT_WEIGHT = 1.0

# 정확도 → 가중치 변환 테이블
# 승률 70% 이상이면 가중치 ↑, 40% 이하면 가중치 ↓
_ACCURACY_TO_WEIGHT = [
    (0.80, 1.5),
    (0.70, 1.3),
    (0.60, 1.1),
    (0.50, 1.0),
    (0.40, 0.8),
    (0.30, 0.6),
    (0.0,  0.5),
]


# ══════════════════════════════════════════════════════════════
# 공개 API
# ══════════════════════════════════════════════════════════════

def record_prediction(
    date_str: str,
    oracle_result: dict,
    signal_sources: list[dict] | None = None,
) -> None:
    """
    아침봇/마감봇 oracle_analyzer 예측 결과를 DB에 기록.

    Args:
        date_str:       기준 날짜 문자열 (예: "2026년 02월 28일")
        oracle_result:  oracle_analyzer.analyze() 반환값
        signal_sources: signal_result["signals"] — 예측에 기여한 신호 유형 추적용
    """
    if not oracle_result or not oracle_result.get("has_data"):
        return

    try:
        # 예측 테마명 목록
        predicted_themes = [
            t.get("theme", "") for t in oracle_result.get("top_themes", [])
            if t.get("theme")
        ]
        # 예측 픽 종목명 목록
        predicted_picks = [
            p.get("name", "") for p in oracle_result.get("picks", [])
            if p.get("name")
        ]
        # 기여 신호 유형 (있는 신호만)
        sig_types = _extract_signal_types(signal_sources or [])

        now_kst = datetime.now(tz=_KST).isoformat()
        conn = get_conn()
        try:
            c = conn.cursor()
            c.execute("""
                INSERT OR REPLACE INTO theme_accuracy
                    (date, predicted_themes, predicted_picks,
                     signal_sources, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                date_str,
                json.dumps(predicted_themes, ensure_ascii=False),
                json.dumps(predicted_picks,  ensure_ascii=False),
                json.dumps(sig_types,        ensure_ascii=False),
                now_kst,
            ))
            conn.commit()
            logger.info(
                f"[accuracy] 예측 기록 완료 — {date_str}: "
                f"테마 {len(predicted_themes)}개, 픽 {len(predicted_picks)}개"
            )
        finally:
            conn.close()

    except Exception as e:
        logger.warning(f"[accuracy] 예측 기록 실패 (비치명적): {e}")


def record_actual(
    date_str: str,
    actual_top_gainers: list[dict],
    actual_upper_limit: list[dict] | None = None,
) -> None:
    """
    마감봇 실제 급등 테마/종목을 기록하고 예측 정확도를 계산.

    Args:
        date_str:           기준 날짜 문자열 (aㅏ침봇과 동일한 포맷이어야 비교 가능)
        actual_top_gainers: price_collector.collect_daily()["top_gainers"]
        actual_upper_limit: price_collector.collect_daily()["upper_limit"] (선택)
    """
    try:
        # 실제 급등 종목명 집합
        actual_names: set[str] = set()
        for g in (actual_top_gainers or [])[:20]:
            n = g.get("종목명", "")
            if n:
                actual_names.add(n)
        for u in (actual_upper_limit or []):
            n = u.get("종목명", "")
            if n:
                actual_names.add(n)

        if not actual_names:
            return

        conn = get_conn()
        try:
            c = conn.cursor()
            c.execute(
                "SELECT id, predicted_themes, predicted_picks, signal_sources "
                "FROM theme_accuracy WHERE date = ?",
                (date_str,)
            )
            row = c.fetchone()
            if not row:
                logger.info(f"[accuracy] {date_str} 예측 기록 없음 — 비교 생략")
                return

            row_id, pred_themes_json, pred_picks_json, sig_sources_json = row
            predicted_themes = json.loads(pred_themes_json or "[]")
            predicted_picks  = json.loads(pred_picks_json  or "[]")
            sig_types        = json.loads(sig_sources_json or "[]")

            # 실제 급등 종목 저장
            actual_list = sorted(actual_names)

            # 픽 정확도 계산: 예측 픽 중 실제 급등한 비율
            matched_picks = [p for p in predicted_picks if p in actual_names]
            total_predicted = len(predicted_picks)
            match_count = len(matched_picks)
            accuracy_rate = (
                match_count / total_predicted if total_predicted > 0 else 0.0
            )

            now_kst = datetime.now(tz=_KST).isoformat()
            c.execute("""
                UPDATE theme_accuracy
                SET actual_themes  = ?,
                    actual_picks   = ?,
                    match_count    = ?,
                    total_predicted = ?,
                    accuracy_rate  = ?,
                    updated_at     = ?
                WHERE id = ?
            """, (
                json.dumps(actual_list,      ensure_ascii=False),
                json.dumps(list(actual_names), ensure_ascii=False),
                match_count,
                total_predicted,
                accuracy_rate,
                now_kst,
                row_id,
            ))
            conn.commit()
            logger.info(
                f"[accuracy] 실제 기록 완료 — {date_str}: "
                f"매칭 {match_count}/{total_predicted} "
                f"(정확도 {accuracy_rate:.1%})"
            )

            # 신호 가중치 업데이트
            _update_signal_weights(c, conn, sig_types, accuracy_rate)

        finally:
            conn.close()

    except Exception as e:
        logger.warning(f"[accuracy] 실제 기록 실패 (비치명적): {e}")


def get_signal_weights() -> dict[str, float]:
    """
    최신 신호 가중치 반환.

    Returns:
        {"신호1": 1.2, "신호2": 0.8, ...} — 누적 없으면 모두 1.0
    """
    weights = {s: _DEFAULT_WEIGHT for s in _SIGNAL_TYPES}
    try:
        conn = get_conn()
        try:
            c = conn.cursor()
            c.execute("SELECT signal_type, weight FROM signal_weights")
            for row in c.fetchall():
                sig_type, w = row
                if sig_type in weights:
                    weights[sig_type] = float(w)
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[accuracy] 가중치 로드 실패 (기본값 사용): {e}")
    return weights


def get_accuracy_stats(last_n: int = 14) -> dict:
    """
    최근 N일간 정확도 통계 반환 (텔레그램 리포트용).

    Returns:
        {
            "avg_accuracy": float,    # 평균 정확도 (0~1)
            "sample_count": int,      # 비교 완료 샘플 수
            "signal_weights": dict,   # 현재 가중치
            "best_signal":   str,     # 가장 정확도 높은 신호 유형
        }
    """
    stats = {
        "avg_accuracy":   0.0,
        "sample_count":   0,
        "signal_weights": get_signal_weights(),
        "best_signal":    "",
    }
    try:
        conn = get_conn()
        try:
            c = conn.cursor()
            c.execute("""
                SELECT accuracy_rate, signal_sources
                FROM theme_accuracy
                WHERE accuracy_rate IS NOT NULL
                ORDER BY created_at DESC
                LIMIT ?
            """, (last_n,))
            rows = c.fetchall()
            if rows:
                rates = [r[0] for r in rows if r[0] is not None]
                stats["avg_accuracy"] = sum(rates) / len(rates) if rates else 0.0
                stats["sample_count"] = len(rates)
        finally:
            conn.close()

        # 가장 정확도 높은 신호 찾기
        weights = stats["signal_weights"]
        if weights:
            stats["best_signal"] = max(weights, key=lambda k: weights[k])

    except Exception as e:
        logger.warning(f"[accuracy] 통계 조회 실패: {e}")

    return stats


# ══════════════════════════════════════════════════════════════
# 내부 헬퍼
# ══════════════════════════════════════════════════════════════

def _extract_signal_types(signals: list[dict]) -> list[str]:
    """signals 목록에서 발화신호 유형(신호1~8)을 추출해 중복 없는 리스트 반환."""
    found = set()
    for sig in signals:
        발화 = sig.get("발화신호", "")
        for st in _SIGNAL_TYPES:
            if st in 발화:
                found.add(st)
    return sorted(found)


def _update_signal_weights(
    cursor: sqlite3.Cursor,
    conn: sqlite3.Connection,
    sig_types: list[str],
    accuracy_rate: float,
) -> None:
    """
    이번 예측에 기여한 신호 유형들의 가중치를 정확도 기반으로 업데이트.
    기여하지 않은 신호는 변경하지 않는다.
    """
    if not sig_types:
        return

    new_weight = _accuracy_to_weight(accuracy_rate)
    now_kst = datetime.now(tz=_KST).isoformat()

    for sig_type in sig_types:
        try:
            # 기존 레코드 조회
            cursor.execute(
                "SELECT weight, sample_count, win_rate "
                "FROM signal_weights WHERE signal_type = ?",
                (sig_type,)
            )
            row = cursor.fetchone()

            if row:
                old_weight, sample_count, old_win_rate = row
                sample_count = (sample_count or 0) + 1
                win = 1 if accuracy_rate >= 0.5 else 0
                win_rate = (
                    ((old_win_rate or 0.0) * (sample_count - 1) + win)
                    / sample_count
                )
                # 가중치: 지수이동평균 방식 (새 데이터 30% 반영)
                blended = old_weight * 0.7 + new_weight * 0.3
                blended = round(max(0.4, min(2.0, blended)), 3)

                cursor.execute("""
                    UPDATE signal_weights
                    SET weight = ?, sample_count = ?, win_rate = ?, last_updated = ?
                    WHERE signal_type = ?
                """, (blended, sample_count, win_rate, now_kst, sig_type))
            else:
                # 최초 기록
                win_rate = 1.0 if accuracy_rate >= 0.5 else 0.0
                cursor.execute("""
                    INSERT INTO signal_weights
                        (signal_type, weight, sample_count, win_rate, last_updated)
                    VALUES (?, ?, ?, ?, ?)
                """, (sig_type, new_weight, 1, win_rate, now_kst))

        except Exception as e:
            logger.warning(f"[accuracy] 신호 가중치 업데이트 실패 ({sig_type}): {e}")

    try:
        conn.commit()
        logger.info(
            f"[accuracy] 신호 가중치 업데이트 — "
            f"신호 {sig_types}, 정확도 {accuracy_rate:.1%} → 가중치 {new_weight}"
        )
    except Exception as e:
        logger.warning(f"[accuracy] 가중치 commit 실패: {e}")


def _accuracy_to_weight(accuracy_rate: float) -> float:
    """정확도(0~1) → 신호 가중치 변환."""
    for threshold, weight in _ACCURACY_TO_WEIGHT:
        if accuracy_rate >= threshold:
            return weight
    return 0.5
