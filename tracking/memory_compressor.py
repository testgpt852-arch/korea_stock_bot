"""
tracking/memory_compressor.py
[v6.0 5번/P1 신규] trading_journal 3계층 기억 압축 배치

Prism CompressionManager 경량화 이식 (비동기 MCP 대신 동기 Gemma 직접 호출)

[역할]
trading_journal 테이블의 오래된 항목을 계층적으로 압축해
AI 프롬프트 토큰 증가 문제를 방지하고 장기 운영 성능을 유지.

[압축 전략]
  Layer 1 (0~7일):  원문 전체 보존 (기본 compression_layer=1)
  Layer 2 (8~30일): AI가 상황분석+판단평가+교훈을 한 문단으로 요약
                    → summary_text 저장, 상세 JSON 필드 압축
  Layer 3 (31일+):  핵심 인사이트 한 줄만 (one_line_summary 우선 활용)
                    → summary_text 최소화, 상세 필드 초기화

[토큰 절감 효과]
  Layer 1: ~200자/항목 (원문 상세)
  Layer 2: ~80자/항목 (AI 요약)
  Layer 3: ~30자/항목 (핵심 한 줄)

[실행 시점]
  main.py 매주 일요일 03:30 → run_compression() 동기 호출

[ARCHITECTURE 의존성]
memory_compressor → tracking/db_schema    (get_conn)
memory_compressor ← main.py              (run_compression 호출)

[절대 금지 규칙]
이 파일은 DB 읽기 + 압축 UPDATE만 담당.
텔레그램 발송·KIS API 호출·매수 로직 절대 금지.
모든 함수는 동기(sync) — main.py에서 run_in_executor 경유 호출.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from utils.logger import logger
import tracking.db_schema as db_schema
import config

KST = timezone(timedelta(hours=9))

# Google AI SDK (ai_analyzer, trading_journal과 동일 패턴)
try:
    from google import genai
    from google.genai import types as _gtypes
    if config.GOOGLE_AI_API_KEY:
        _CLIENT = genai.Client(api_key=config.GOOGLE_AI_API_KEY)
        logger.info("[memory_compressor] AI 클라이언트 초기화 완료 (폴백 모델 적용)")
    else:
        _CLIENT = None
except Exception:
    _CLIENT = None


# ── 공개 API ──────────────────────────────────────────────────

# [v7.0 Priority3] KOSPI 레벨 범위 구간 크기 (200포인트 단위)
_KOSPI_BUCKET_SIZE = 200


def update_index_stats() -> dict:
    """
    [v7.0 Priority3] KOSPI/KOSDAQ 지수 레벨별 매매 승률 통계 집계 배치.

    Prism memory_compressor_agent의 'KOSPI 심리적 지지/저항선, 변곡점 구간별 승률' 기능 구현.
    → 'KOSPI 2400~2600 진입 시 승률 72%' 같은 통계를 자동 추출.

    [동작]
    trading_history 테이블에서 청산된 모든 거래를 읽어
    buy_market_context (매수 당시 KOSPI 레벨) 기준으로 레벨별 승률을 집계.
    → kospi_index_stats 테이블에 UPSERT.

    buy_market_context 가 없는 거래는 건너뜀 (하위 호환).

    [호출 시점]
    run_compression() 에서 자동 호출 (매주 일요일 03:30).

    Returns:
        {
          "buckets_updated": int,  # 업데이트된 레벨 구간 수
          "trades_analyzed": int,  # 분석된 거래 수
        }
    """
    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        # trading_history에서 청산된 거래 + 매수 당시 KOSPI 레벨 조회
        # buy_market_context 컬럼이 없는 구버전 DB는 graceful 처리
        try:
            c.execute("""
                SELECT profit_rate, buy_market_context
                FROM trading_history
                WHERE sell_time IS NOT NULL
                  AND buy_market_context IS NOT NULL
                  AND buy_market_context != ''
            """)
            rows = c.fetchall()
        except Exception:
            # buy_market_context 컬럼 없음 → 통계 집계 불가
            logger.info("[compressor] kospi_index_stats: buy_market_context 컬럼 없음 — 건너뜀")
            return {"buckets_updated": 0, "trades_analyzed": 0}
    finally:
        conn.close()

    if not rows:
        logger.info("[compressor] kospi_index_stats: 분석 가능한 거래 없음")
        return {"buckets_updated": 0, "trades_analyzed": 0}

    # {kospi_range: {"wins": int, "total": int, "profits": list[float]}}
    buckets: dict[str, dict] = {}
    trades_analyzed = 0

    for profit_rate, buy_ctx in rows:
        kospi_level = _extract_kospi_level(buy_ctx)
        if kospi_level is None:
            continue

        bucket_key  = _get_kospi_bucket(kospi_level)
        bucket_low  = (kospi_level // _KOSPI_BUCKET_SIZE) * _KOSPI_BUCKET_SIZE
        bucket_high = bucket_low + _KOSPI_BUCKET_SIZE
        kospi_range = f"{bucket_low}~{bucket_high}"

        if kospi_range not in buckets:
            buckets[kospi_range] = {
                "kospi_level": bucket_low + _KOSPI_BUCKET_SIZE // 2,
                "wins": 0, "total": 0, "profits": []
            }

        buckets[kospi_range]["total"] += 1
        if profit_rate is not None and profit_rate > 0:
            buckets[kospi_range]["wins"] += 1
        if profit_rate is not None:
            buckets[kospi_range]["profits"].append(profit_rate)
        trades_analyzed += 1

    # kospi_index_stats 테이블 UPSERT
    buckets_updated = 0
    now_kst = datetime.now(KST).isoformat(timespec="seconds")
    today_str = datetime.now(KST).strftime("%Y-%m-%d")

    for kospi_range, data in buckets.items():
        total   = data["total"]
        wins    = data["wins"]
        profits = data["profits"]
        win_rate = round(wins / total * 100, 1) if total > 0 else 0.0
        avg_profit = round(sum(profits) / len(profits), 2) if profits else 0.0

        conn2 = db_schema.get_conn()
        try:
            c2 = conn2.cursor()
            c2.execute("""
                INSERT INTO kospi_index_stats
                    (trade_date, kospi_level, kospi_range,
                     win_count, total_count, win_rate, avg_profit_rate, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(kospi_range) DO UPDATE SET
                    trade_date      = excluded.trade_date,
                    win_count       = excluded.win_count,
                    total_count     = excluded.total_count,
                    win_rate        = excluded.win_rate,
                    avg_profit_rate = excluded.avg_profit_rate,
                    last_updated    = excluded.last_updated
            """, (
                today_str,
                data["kospi_level"],
                kospi_range,
                wins, total, win_rate, avg_profit,
                now_kst,
            ))
            conn2.commit()
            buckets_updated += 1
        except Exception as e:
            logger.warning(f"[compressor] kospi_index_stats UPSERT 실패 ({kospi_range}): {e}")
        finally:
            conn2.close()

    logger.info(
        f"[compressor] KOSPI 지수 레벨 통계 업데이트 완료 — "
        f"{buckets_updated}개 구간 / {trades_analyzed}건 분석"
    )
    return {"buckets_updated": buckets_updated, "trades_analyzed": trades_analyzed}


def get_index_context(current_kospi: float | None = None) -> str:
    """
    [v7.0 Priority3] 현재 KOSPI 레벨 기반 과거 승률 컨텍스트 반환.
    ai_context.build_spike_context()에서 AI 프롬프트 주입용으로 호출.

    current_kospi가 None이면 전체 레벨 Top3 (거래 수 많은 순) 반환.
    current_kospi가 있으면 해당 레벨 구간 + 인접 구간 승률 반환.

    Returns:
        AI 프롬프트 주입용 문자열 (데이터 없으면 "")
    """
    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        try:
            if current_kospi is not None:
                # 현재 레벨과 인접 ±2 구간 조회
                bucket_low  = (int(current_kospi) // _KOSPI_BUCKET_SIZE) * _KOSPI_BUCKET_SIZE
                nearby_lows = [
                    bucket_low - _KOSPI_BUCKET_SIZE * 2,
                    bucket_low - _KOSPI_BUCKET_SIZE,
                    bucket_low,
                    bucket_low + _KOSPI_BUCKET_SIZE,
                    bucket_low + _KOSPI_BUCKET_SIZE * 2,
                ]
                placeholders = ",".join("?" * len(nearby_lows))
                nearby_ranges = [
                    f"{low}~{low + _KOSPI_BUCKET_SIZE}" for low in nearby_lows
                ]
                c.execute(f"""
                    SELECT kospi_range, win_rate, avg_profit_rate, total_count
                    FROM kospi_index_stats
                    WHERE kospi_range IN ({placeholders})
                      AND total_count >= 3
                    ORDER BY kospi_level ASC
                """, nearby_ranges)
            else:
                # 전체에서 거래 수 많은 Top3
                c.execute("""
                    SELECT kospi_range, win_rate, avg_profit_rate, total_count
                    FROM kospi_index_stats
                    WHERE total_count >= 3
                    ORDER BY total_count DESC
                    LIMIT 3
                """)

            rows = c.fetchall()
        except Exception:
            return ""
    finally:
        conn.close()

    if not rows:
        return ""

    lines = ["📊 KOSPI 레벨별 과거 승률:"]
    for kospi_range, win_rate, avg_profit, total in rows:
        lines.append(
            f"  {kospi_range}: 승률 {win_rate:.0f}%  평균수익 {avg_profit:+.1f}%  (n={total})"
        )
    return "\n".join(lines)


def _extract_kospi_level(buy_market_context: str) -> int | None:
    """
    buy_market_context 문자열에서 KOSPI 지수값 추출.
    예: "강세장 KOSPI2547" → 2547
        "KOSPI:2547.3" → 2547
        "횡보 kospi=2100" → 2100

    Returns:
        KOSPI 정수값 / 추출 실패 시 None
    """
    if not buy_market_context:
        return None
    # 숫자 4자리 패턴 (KOSPI는 보통 1000~5000)
    matches = re.findall(r"[Kk][Oo][Ss][Pp][Ii][:\s=]?\s*(\d{4,5}(?:\.\d+)?)", buy_market_context)
    if matches:
        try:
            return int(float(matches[0]))
        except ValueError:
            pass
    # fallback: 4~5자리 숫자 추출
    numbers = re.findall(r"\b(\d{4,5})\b", buy_market_context)
    for n in numbers:
        val = int(n)
        if 500 <= val <= 10000:  # KOSPI 유효 범위
            return val
    return None


def _get_kospi_bucket(kospi_level: int) -> str:
    """
    KOSPI 레벨을 _KOSPI_BUCKET_SIZE 단위 구간 문자열로 변환.
    예: 2547 → "2400~2600" (bucket_size=200)
    """
    bucket_low  = (kospi_level // _KOSPI_BUCKET_SIZE) * _KOSPI_BUCKET_SIZE
    bucket_high = bucket_low + _KOSPI_BUCKET_SIZE
    return f"{bucket_low}~{bucket_high}"


def run_compression() -> dict:
    """
    3계층 기억 압축 배치 실행.
    main.py 매주 일요일 03:30에서만 호출.

    Returns:
        {
          "compressed_l1": int,  # Layer1 → Layer2 압축 건수
          "compressed_l2": int,  # Layer2 → Layer3 압축 건수
          "cleaned":        int,  # Layer3 90일 이상 정리 건수
          "skipped":        int,  # AI 없어서 건너뜀
        }
    """
    if not config.MEMORY_COMPRESS_ENABLED:
        logger.info("[compressor] 기억 압축 비활성 (MEMORY_COMPRESS_ENABLED=false)")
        return {"compressed_l1": 0, "compressed_l2": 0, "cleaned": 0, "skipped": 0}

    layer1_cutoff = (
        datetime.now(KST) - timedelta(days=config.MEMORY_COMPRESS_LAYER1_DAYS)
    ).strftime("%Y-%m-%d")

    layer2_cutoff = (
        datetime.now(KST) - timedelta(days=config.MEMORY_COMPRESS_LAYER2_DAYS)
    ).strftime("%Y-%m-%d")

    archive_cutoff = (
        datetime.now(KST) - timedelta(days=90)
    ).strftime("%Y-%m-%d")

    logger.info(
        f"[compressor] 기억 압축 시작 — "
        f"Layer1→2 기준: {layer1_cutoff} / "
        f"Layer2→3 기준: {layer2_cutoff}"
    )

    result = {"compressed_l1": 0, "compressed_l2": 0, "cleaned": 0, "skipped": 0}

    # Step 1: Layer1 → Layer2 (7~30일 항목 AI 요약)
    result["compressed_l1"], result["skipped"] = _compress_layer1_to_2(layer1_cutoff)

    # Step 2: Layer2 → Layer3 (30일+ 항목 핵심만 보존)
    result["compressed_l2"] += _compress_layer2_to_3(layer2_cutoff)

    # Step 3: Layer3 90일+ 항목 정리 (summary_text만 남기고 상세 초기화)
    result["cleaned"] = _clean_old_layer3(archive_cutoff)

    # Step 4: [v7.0 Priority3] KOSPI 지수 레벨별 승률 통계 업데이트
    index_result = update_index_stats()
    result["index_buckets_updated"] = index_result.get("buckets_updated", 0)
    result["index_trades_analyzed"] = index_result.get("trades_analyzed", 0)

    logger.info(
        f"[compressor] 기억 압축 완료 — "
        f"Layer1→2: {result['compressed_l1']}건 / "
        f"Layer2→3: {result['compressed_l2']}건 / "
        f"정리: {result['cleaned']}건 / "
        f"AI 없어 스킵: {result['skipped']}건"
    )
    return result


# ── 내부 함수 ─────────────────────────────────────────────────

def _compress_layer1_to_2(cutoff_date: str) -> tuple[int, int]:
    """
    Layer 1 항목 중 cutoff_date보다 오래된 것을 Layer 2로 압축.
    AI가 situation_analysis + judgment_evaluation + lessons를 한 문단으로 요약.
    AI 없으면 rule-based 요약 사용 (비치명적).

    Returns:
        (압축 건수, AI 없어 스킵된 건수)
    """
    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT id, ticker, name, profit_rate, close_reason,
                   situation_analysis, judgment_evaluation, lessons,
                   one_line_summary, created_at
            FROM trading_journal
            WHERE compression_layer = 1
              AND DATE(created_at) < ?
            ORDER BY created_at ASC
        """, (cutoff_date,))
        rows = c.fetchall()
    except Exception as e:
        logger.warning(f"[compressor] Layer1 조회 실패: {e}")
        return 0, 0
    finally:
        conn.close()

    if not rows:
        logger.info("[compressor] Layer1→2 압축 대상 없음")
        return 0, 0

    compressed = 0
    skipped = 0
    now_kst = datetime.now(KST).isoformat(timespec="seconds")

    for row in rows:
        (jid, ticker, name, profit_rate, close_reason,
         raw_situation, raw_judgment, raw_lessons,
         one_line_summary, created_at) = row

        # AI 요약 시도
        summary_text = _ai_summarize_to_layer2(
            ticker=ticker, name=name,
            profit_rate=profit_rate, close_reason=close_reason,
            raw_situation=raw_situation, raw_judgment=raw_judgment,
            raw_lessons=raw_lessons,
            one_line_summary=one_line_summary,
        )

        if not summary_text:
            # AI 없으면 rule-based 요약
            summary_text = _rule_based_summary(
                profit_rate, close_reason, raw_lessons, one_line_summary
            )
            if not _CLIENT:
                skipped += 1

        if not summary_text:
            continue

        conn2 = db_schema.get_conn()
        try:
            c2 = conn2.cursor()
            c2.execute("""
                UPDATE trading_journal
                SET compression_layer = 2,
                    summary_text = ?,
                    compressed_at = ?
                WHERE id = ?
            """, (summary_text, now_kst, jid))
            conn2.commit()
            compressed += 1
        except Exception as e:
            logger.warning(f"[compressor] Layer1→2 UPDATE 실패 (id={jid}): {e}")
        finally:
            conn2.close()

    logger.info(f"[compressor] Layer1→2 압축 완료: {compressed}건 / 스킵: {skipped}건")
    return compressed, skipped


def _compress_layer2_to_3(cutoff_date: str) -> int:
    """
    Layer 2 항목 중 cutoff_date보다 오래된 것을 Layer 3으로 압축.
    summary_text를 30자 이내로 단축 + 상세 JSON 필드 초기화.
    AI 없이도 동작 (단순 truncate).
    """
    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT id, summary_text, one_line_summary
            FROM trading_journal
            WHERE compression_layer = 2
              AND DATE(created_at) < ?
        """, (cutoff_date,))
        rows = c.fetchall()
    except Exception as e:
        logger.warning(f"[compressor] Layer2 조회 실패: {e}")
        return 0
    finally:
        conn.close()

    if not rows:
        return 0

    compressed = 0
    now_kst = datetime.now(KST).isoformat(timespec="seconds")

    for jid, summary_text, one_line_summary in rows:
        # Layer 3: 가장 짧은 핵심 텍스트만 유지
        core = one_line_summary or summary_text or ""
        core_short = core[:50] if core else ""

        conn2 = db_schema.get_conn()
        try:
            c2 = conn2.cursor()
            c2.execute("""
                UPDATE trading_journal
                SET compression_layer  = 3,
                    summary_text       = ?,
                    compressed_at      = ?,
                    situation_analysis = '{}',
                    judgment_evaluation = '{}',
                    lessons            = '[]'
                WHERE id = ?
            """, (core_short, now_kst, jid))
            conn2.commit()
            compressed += 1
        except Exception as e:
            logger.warning(f"[compressor] Layer2→3 UPDATE 실패 (id={jid}): {e}")
        finally:
            conn2.close()

    logger.info(f"[compressor] Layer2→3 압축 완료: {compressed}건")
    return compressed


def _clean_old_layer3(cutoff_date: str) -> int:
    """
    Layer 3 항목 중 90일 이상 된 것은 pattern_tags와 one_line_summary만 남기고
    나머지 텍스트 필드 초기화. DB 크기 무한 증가 방지.
    """
    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            UPDATE trading_journal
            SET summary_text = SUBSTR(COALESCE(summary_text, one_line_summary, ''), 1, 30),
                situation_analysis = '{}',
                judgment_evaluation = '{}'
            WHERE compression_layer = 3
              AND DATE(created_at) < ?
        """, (cutoff_date,))
        cleaned = c.rowcount
        conn.commit()
    except Exception as e:
        logger.warning(f"[compressor] Layer3 정리 실패: {e}")
        cleaned = 0
    finally:
        conn.close()

    if cleaned > 0:
        logger.info(f"[compressor] Layer3 오래된 항목 정리: {cleaned}건")
    return cleaned


def _ai_summarize_to_layer2(
    ticker: str, name: str,
    profit_rate: float, close_reason: str,
    raw_situation: str, raw_judgment: str, raw_lessons: str,
    one_line_summary: str,
) -> str:
    """
    Gemma AI로 거래 일지 상세 내용을 한 문단(80자 이내)으로 요약.
    API 없거나 실패 시 "" 반환 (호출부에서 rule-based fallback).
    """
    if not _CLIENT:
        return ""

    # 원본 데이터 파싱
    try:
        situation = json.loads(raw_situation or "{}")
        judgment  = json.loads(raw_judgment or "{}")
        lessons   = json.loads(raw_lessons or "[]")
    except Exception:
        situation, judgment, lessons = {}, {}, []

    buy_ctx  = situation.get("buy_context_summary", "")
    sell_ctx = situation.get("sell_context_summary", "")
    buy_q    = judgment.get("buy_quality", "")
    sell_q   = judgment.get("sell_quality", "")

    lesson_str = ""
    if lessons and isinstance(lessons, list):
        first = lessons[0] if lessons else {}
        if isinstance(first, dict):
            lesson_str = first.get("action", "")

    prompt = f"""다음 거래 복기를 80자 이내 한 문장으로 압축 요약하세요. 핵심 교훈 중심. 설명 없이 요약만:

종목: {name}({ticker}) 수익률: {profit_rate:+.1f}% 청산: {close_reason}
매수상황: {buy_ctx} / 매도상황: {sell_ctx}
매수판단: {buy_q} / 매도판단: {sell_q}
핵심교훈: {lesson_str}
한줄요약: {one_line_summary}

요약:"""

    try:
        from utils.ai_client import call_ai
        result = call_ai(_CLIENT, prompt, max_tokens=100, temperature=0.1,
                         caller="memory_compressor").strip()
        # 마크다운·불필요한 텍스트 제거
        result = re.sub(r"^(요약:?\s*|Summary:?\s*)", "", result, flags=re.IGNORECASE)
        result = result[:100]  # 최대 100자
        return result
    except Exception as e:
        logger.debug(f"[compressor] AI 요약 실패 ({ticker}): {e}")
        return ""


def _rule_based_summary(
    profit_rate: float,
    close_reason: str,
    raw_lessons: str,
    one_line_summary: str,
) -> str:
    """
    AI 없을 때 rule-based 요약 생성.
    one_line_summary가 있으면 그대로 사용, 없으면 수익률+청산사유 조합.
    """
    if one_line_summary:
        return one_line_summary[:80]

    reason_map = {
        "take_profit_1": "1차익절",
        "take_profit_2": "2차익절",
        "stop_loss":     "손절",
        "trailing_stop": "트레일링스탑",
        "force_close":   "강제청산",
        "final_close":   "최종청산",
    }
    reason_kr = reason_map.get(close_reason, close_reason or "청산")

    lesson_short = ""
    try:
        lessons = json.loads(raw_lessons or "[]")
        if lessons and isinstance(lessons[0], dict):
            lesson_short = lessons[0].get("action", "")[:30]
    except Exception:
        pass

    base = f"{profit_rate:+.1f}% {reason_kr}"
    return f"{base} | {lesson_short}" if lesson_short else base
