"""
tracking/trading_journal.py
거래 완료 시 AI 회고 분석 + 패턴 태그 자동 추출 (Phase 3, v4.3 신규)

Prism의 trading_journal_agent 기능을 우리 구조에 맞게 경량화 구현.

[역할]
position_manager.close_position() 직후 호출되어:
  1. 매수 당시 컨텍스트 vs 매도 시점 비교 분석 (Prism 벤치마킹 핵심)
  2. Gemma AI로 회고 분석 → situation_analysis / judgment_evaluation / lessons 추출
  3. 패턴 태그 부여 (rule-based + AI 혼합)
  4. trading_journal 테이블에 INSERT
  5. 추출된 lessons를 principles_extractor로 전달 (원칙 DB 품질 향상)

[Prism 대비 차이]
  - mcp_agent 대신 google-genai(Gemma) 직접 호출 (우리 AI 엔진 동일)
  - async 대신 sync (position_manager 규칙 준수)
  - API 실패 시 rule-based fallback 보장 (비치명적)

[패턴 태그 목록]
  시장 관련:
    강세장진입       — 강세장에서 진입 (bull_market_entry)
    약세장진입       — 약세장/횡보에서 진입 (위험 신호)
  진입 관련:
    조기포착         — 초기 급등 조기 포착 (delta_rate 낮을 때 진입)
    추격매수         — 고점 근접 뒤늦은 진입
    갭상승성공       — 갭상승 트리거 성공
    워치리스트조기   — WebSocket 워치리스트 조기 감지
  청산 관련:
    원칙준수익절     — 목표가 달성 후 깔끔한 익절
    트레일링스탑작동 — Trailing Stop 정상 작동
    손절지연         — stop_loss 청산인데 손실이 큼 (지연 손절)
    강제청산         — 14:50 강제청산
  결과 관련:
    큰수익           — profit_rate >= 8%
    큰손실           — profit_rate <= -5%

[실행 시점]
  traders/position_manager.close_position() 내부 → 동기(sync) 함수

[ARCHITECTURE 의존성]
trading_journal → tracking/db_schema    (get_conn)
trading_journal ← traders/position_manager  (record_journal 호출)
trading_journal → tracking/principles_extractor  (강화된 lessons 전달)
trading_journal ← tracking/ai_context   (get_journal_context 조회)
trading_journal ← reports/weekly_report (get_weekly_patterns 조회)

[절대 금지 규칙 — ARCHITECTURE Phase3 #45~49]
이 파일은 DB 기록 + AI 회고 분석만 담당.
텔레그램 발송·KIS API 호출·매수 로직 절대 금지.
모든 함수는 동기(sync).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from utils.logger import logger
import tracking.db_schema as db_schema

KST = timezone(timedelta(hours=9))

# ── Google AI SDK (ai_analyzer와 동일 패턴) ──────────────────
try:
    from google import genai
    from google.genai import types as _gtypes
    import config as _cfg
    if _cfg.GOOGLE_AI_API_KEY:
        _CLIENT = genai.Client(api_key=_cfg.GOOGLE_AI_API_KEY)
        _MODEL  = "gemma-3-27b-it"
    else:
        _CLIENT = None
        _MODEL  = None
except Exception:
    _CLIENT = None
    _MODEL  = None


# ── 공개 API ──────────────────────────────────────────────────

def record_journal(
    trading_id: int,
    ticker: str,
    name: str,
    buy_time: str,
    sell_time: str,
    buy_price: int,
    sell_price: int,
    profit_rate: float,
    trigger_source: str,
    close_reason: str,
    market_env: str = "",
) -> bool:
    """
    거래 완료 시 AI 회고 분석 후 trading_journal에 INSERT.
    position_manager.close_position() 에서만 호출.

    Args:
        trading_id:     trading_history.id
        ticker:         종목코드
        name:           종목명
        buy_time:       매수 시각 (ISO 8601 KST)
        sell_time:      매도 시각 (ISO 8601 KST)
        buy_price:      매수가 (원)
        sell_price:     매도가 (원)
        profit_rate:    수익률 (%)
        trigger_source: 진입 트리거 (rate / websocket / gap_up 등)
        close_reason:   청산 사유 (take_profit_1 / stop_loss / trailing_stop 등)
        market_env:     시장 환경 (강세장 / 약세장/횡보 / "")

    Returns:
        True(성공) / False(실패, 비치명적)
    """
    try:
        # ① rule-based 패턴 태그 (빠르고 안정적)
        rule_tags = _extract_rule_tags(profit_rate, trigger_source, close_reason, market_env)

        # ② AI 회고 분석 (API 가용 시, 실패해도 계속)
        situation_analysis, judgment_eval, lessons, ai_tags, one_line_summary = \
            _ai_retrospective(
                ticker=ticker, name=name,
                buy_time=buy_time, sell_time=sell_time,
                buy_price=buy_price, sell_price=sell_price,
                profit_rate=profit_rate,
                trigger_source=trigger_source,
                close_reason=close_reason,
                market_env=market_env,
            )

        # rule-based + AI 태그 병합 (중복 제거, 순서 보존)
        merged_tags = list(dict.fromkeys(rule_tags + ai_tags))

        # ③ trading_journal INSERT
        conn = db_schema.get_conn()
        try:
            c = conn.cursor()
            c.execute("""
                INSERT INTO trading_journal
                    (trading_id, ticker, name, buy_time, sell_time,
                     buy_price, sell_price, profit_rate,
                     trigger_source, close_reason, market_env,
                     situation_analysis, judgment_evaluation,
                     lessons, pattern_tags, one_line_summary,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trading_id, ticker, name, buy_time, sell_time,
                buy_price, sell_price, profit_rate,
                trigger_source, close_reason, market_env,
                json.dumps(situation_analysis, ensure_ascii=False),
                json.dumps(judgment_eval, ensure_ascii=False),
                json.dumps(lessons, ensure_ascii=False),
                json.dumps(merged_tags, ensure_ascii=False),
                one_line_summary,
                datetime.now(KST).isoformat(timespec="seconds"),
            ))
            journal_id = c.lastrowid
            conn.commit()
        finally:
            conn.close()

        logger.info(
            f"[journal] 일지 기록 완료 ✅  {name}({ticker})  "
            f"수익률 {profit_rate:+.2f}%  태그: {merged_tags}  "
            f"한줄요약: {one_line_summary[:30] if one_line_summary else '없음'}"
        )

        # ④ AI가 추출한 lessons → principles_extractor 로 전달 (원칙 DB 강화)
        if lessons and journal_id:
            _push_lessons_to_principles(lessons, journal_id, trigger_source)

        return True

    except Exception as e:
        logger.error(f"[journal] 일지 기록 실패 ({ticker}): {e}", exc_info=True)
        return False


def get_weekly_patterns(days: int = 7) -> list[dict]:
    """
    최근 N일 trading_journal에서 패턴 빈도 + 승률 집계.
    weekly_report에서 "이번 주 학습한 패턴" 섹션 생성에 사용.

    Returns:
        [
          {"tag": str, "count": int, "win_count": int, "win_rate": float,
           "avg_profit": float, "lesson_sample": str | None},
          ...
        ]
        count 기준 내림차순 정렬.
    """
    since = (datetime.now(KST) - timedelta(days=days)).isoformat(timespec="seconds")
    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT pattern_tags, profit_rate, lessons
            FROM trading_journal
            WHERE created_at >= ?
        """, (since,))
        rows = c.fetchall()
    except Exception as e:
        logger.warning(f"[journal] get_weekly_patterns 조회 실패: {e}")
        return []
    finally:
        conn.close()

    if not rows:
        return []

    tag_stats: dict[str, dict] = {}
    for raw_tags, profit_rate, raw_lessons in rows:
        try:
            tags = json.loads(raw_tags or "[]")
        except Exception:
            tags = []

        lesson_sample = ""
        try:
            lessons = json.loads(raw_lessons or "[]")
            if lessons and isinstance(lessons[0], dict):
                lesson_sample = lessons[0].get("action", "")
        except Exception:
            pass

        for tag in tags:
            if tag not in tag_stats:
                tag_stats[tag] = {"count": 0, "win_count": 0, "profits": [], "lessons": []}
            tag_stats[tag]["count"] += 1
            if profit_rate is not None and profit_rate > 0:
                tag_stats[tag]["win_count"] += 1
            if profit_rate is not None:
                tag_stats[tag]["profits"].append(profit_rate)
            if lesson_sample:
                tag_stats[tag]["lessons"].append(lesson_sample)

    result = []
    for tag, s in tag_stats.items():
        n = s["count"]
        w = s["win_count"]
        profits = s["profits"]
        result.append({
            "tag":          tag,
            "count":        n,
            "win_count":    w,
            "win_rate":     round(w / n * 100, 1) if n > 0 else 0.0,
            "avg_profit":   round(sum(profits) / len(profits), 2) if profits else 0.0,
            "lesson_sample": s["lessons"][-1] if s["lessons"] else None,
        })

    result.sort(key=lambda x: x["count"], reverse=True)
    return result


def get_journal_context(ticker: str) -> str:
    """
    ai_context.build_spike_context() 에서 호출 — 같은 종목 과거 일지 조회.
    현재 매수 결정에 과거 교훈을 주입 (Prism 벤치마킹).

    [v6.0 이슈② 수정] JOURNAL_MAX_CONTEXT_CHARS / JOURNAL_MAX_ITEMS 제한 적용.
    기존: 항목 수 제한 없이 쌓이면 토큰 무한 증가
    수정: config.JOURNAL_MAX_ITEMS(기본 3) 항목, config.JOURNAL_MAX_CONTEXT_CHARS(기본 2000자) 제한

    compression_layer 필드가 있으면 레이어별로 다른 포맷 사용:
    - Layer 1 (0~7일): 상세 포맷 (원문 one_line_summary 포함)
    - Layer 2 (8~30일): 요약 포맷 (situation_analysis summary만)
    - Layer 3 (31일~): 한 줄 핵심만

    Returns:
        프롬프트 주입용 문자열. 이력 없으면 "".
    """
    import config as _config
    max_items = getattr(_config, 'JOURNAL_MAX_ITEMS', 3)
    max_chars = getattr(_config, 'JOURNAL_MAX_CONTEXT_CHARS', 2000)

    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        # compression_layer 컬럼 존재 여부 확인 (v6.0 마이그레이션)
        c.execute("PRAGMA table_info(trading_journal)")
        cols = {row[1] for row in c.fetchall()}
        has_layer = "compression_layer" in cols
        has_summary_text = "summary_text" in cols

        if has_layer:
            c.execute("""
                SELECT profit_rate, close_reason, pattern_tags, one_line_summary,
                       created_at, compression_layer,
                       {} AS summary_text
                FROM trading_journal
                WHERE ticker = ?
                ORDER BY created_at DESC
                LIMIT ?
            """.format('"summary_text"' if has_summary_text else 'NULL'),
                (ticker, max_items))
        else:
            c.execute("""
                SELECT profit_rate, close_reason, pattern_tags, one_line_summary,
                       created_at, 1 AS compression_layer, NULL AS summary_text
                FROM trading_journal
                WHERE ticker = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (ticker, max_items))
        rows = c.fetchall()
    except Exception as e:
        logger.debug(f"[journal] get_journal_context 조회 실패: {e}")
        return ""
    finally:
        conn.close()

    if not rows:
        return ""

    lines = [f"[{ticker} 과거 거래 일지]"]
    for profit_rate, close_reason, raw_tags, summary, created_at, layer, summary_text in rows:
        try:
            tags = json.loads(raw_tags or "[]")
            tags_str = "/".join(tags[:3]) if tags else ""
        except Exception:
            tags_str = ""
        date_str = created_at[:10] if created_at else "?"
        profit_emoji = "✅" if (profit_rate or 0) > 0 else "❌"

        if layer == 3:
            # Layer 3: 한 줄 핵심만 (최소 토큰)
            core = summary_text or summary or ""
            lines.append(f"  {date_str} {profit_emoji} {profit_rate:+.1f}% [{core[:30]}]")
        elif layer == 2:
            # Layer 2: 요약 포맷
            core = summary_text or summary or tags_str
            lines.append(
                f"  {date_str} {profit_emoji} {profit_rate:+.1f}% [{close_reason}] {core[:40]}"
            )
        else:
            # Layer 1: 상세 포맷
            lines.append(
                f"  {date_str} {profit_emoji} {profit_rate:+.1f}% [{close_reason}] "
                f"{tags_str}"
            )
            if summary:
                lines.append(f"    └ {summary[:40]}")

    result = "\n".join(lines)

    # [v6.0 이슈②] 최대 문자 수 제한
    if len(result) > max_chars:
        result = result[:max_chars] + "...(압축됨)"

    return result


# ── 내부 함수 ─────────────────────────────────────────────────

def _extract_rule_tags(
    profit_rate: float,
    trigger_source: str,
    close_reason: str,
    market_env: str,
) -> list[str]:
    """
    규칙 기반 패턴 태그 추출.
    AI 없이도 항상 동작하는 안정적 fallback.
    """
    tags = []

    # 시장 환경
    if "강세장" in market_env:
        tags.append("강세장진입")
    elif "약세장" in market_env or "횡보" in market_env:
        tags.append("약세장진입")

    # 진입 트리거
    if trigger_source == "gap_up":
        tags.append("갭상승성공" if (profit_rate or 0) > 0 else "갭상승실패")
    elif trigger_source == "websocket":
        tags.append("워치리스트조기")

    # 청산 사유
    if close_reason in ("take_profit_1", "take_profit_2"):
        tags.append("원칙준수익절")
    elif close_reason == "trailing_stop":
        tags.append("트레일링스탑작동")
    elif close_reason == "stop_loss":
        tags.append("손절지연" if (profit_rate or 0) < -5.0 else "손절실행")
    elif close_reason == "force_close":
        tags.append("강제청산")

    # 수익률 결과
    if (profit_rate or 0) >= 8.0:
        tags.append("큰수익")
    elif (profit_rate or 0) <= -5.0:
        tags.append("큰손실")

    return tags


def _ai_retrospective(
    ticker: str, name: str,
    buy_time: str, sell_time: str,
    buy_price: int, sell_price: int,
    profit_rate: float,
    trigger_source: str,
    close_reason: str,
    market_env: str,
) -> tuple[dict, dict, list, list[str], str]:
    """
    Gemma AI로 매매 회고 분석 (Prism trading_journal_agent 경량화 구현).

    Returns:
        (situation_analysis, judgment_evaluation, lessons, extra_tags, one_line_summary)
        실패 시 ({}, {}, [], [], "")
    """
    if not _CLIENT:
        return {}, {}, [], [], ""

    prompt = f"""당신은 노련한 한국 주식 단타 매매 전문가입니다.
다음 완료된 거래를 복기(회고)하고 JSON으로만 응답하세요. 설명 없이 JSON만.

[거래 정보]
종목명: {name} ({ticker})
매수가: {buy_price:,}원  |  매도가: {sell_price:,}원
수익률: {profit_rate:+.2f}%
매수 시각: {buy_time}  |  매도 시각: {sell_time}
진입 트리거: {trigger_source}
청산 사유: {close_reason}
시장 환경: {market_env or "미지정"}

[분석 지시]
1. 매수/매도 당시 상황 비교 (시장·종목·재료 변화)
2. 매수/매도 판단 품질 평가 (적절/부적절/보통)
3. 실행 가능한 교훈 1~3개 추출
4. 패턴 태그 부여 (아래 목록에서 해당하는 것)

[사용 가능한 추가 패턴 태그]
조기포착, 추격매수, 급등후조정, 박스권돌파, 손절지연, 익절조급,
추세추종, 눌림목매수, 재료과신, 경고무시, 좋은손익비

[응답 형식]
{{
  "situation_analysis": {{
    "buy_context_summary": "매수 당시 상황 요약 (30자 이내)",
    "sell_context_summary": "매도 당시 상황 요약 (30자 이내)",
    "key_changes": ["변화1", "변화2"]
  }},
  "judgment_evaluation": {{
    "buy_quality": "적절/부적절/보통",
    "buy_quality_reason": "이유 (20자 이내)",
    "sell_quality": "적절/조급/지연/보통",
    "sell_quality_reason": "이유 (20자 이내)",
    "missed_signals": ["놓친 신호"]
  }},
  "lessons": [
    {{
      "condition": "이런 상황에서는",
      "action": "이렇게 해야 한다",
      "priority": "high/medium/low"
    }}
  ],
  "extra_tags": ["추가태그1", "추가태그2"],
  "one_line_summary": "한 줄 요약 (25자 이내)"
}}"""

    try:
        response = _CLIENT.models.generate_content(
            model=_MODEL,
            contents=prompt,
            config=_gtypes.GenerateContentConfig(
                temperature=0.15,
                max_output_tokens=800,
            ),
        )
        data = _parse_json(response.text or "")
        situation = data.get("situation_analysis", {})
        judgment  = data.get("judgment_evaluation", {})
        lessons   = data.get("lessons", [])
        ai_tags   = [t for t in data.get("extra_tags", []) if isinstance(t, str)]
        summary   = str(data.get("one_line_summary", ""))[:50]
        return situation, judgment, lessons, ai_tags, summary

    except Exception as e:
        logger.debug(f"[journal] AI 회고 분석 실패 ({ticker}): {e}")
        return {}, {}, [], [], ""


def _push_lessons_to_principles(
    lessons: list[dict],
    journal_id: int,
    trigger_source: str,
) -> None:
    """
    AI가 추출한 lessons → trading_principles 테이블에 반영.
    principles_extractor의 기존 배치 로직과 중복되지 않게 직접 UPDATE만.
    INSERT는 샘플 5건 미만이므로 하지 않음 — extractor 배치 역할 유지.
    기존 원칙이 있으면 supporting_trades + 1, confidence 미세 상향.
    """
    if not lessons:
        return

    conn = db_schema.get_conn()
    try:
        c = conn.cursor()
        now_kst = datetime.now(KST).isoformat()

        for lesson in lessons:
            if not isinstance(lesson, dict):
                continue
            condition = str(lesson.get("condition", "")).strip()
            action    = str(lesson.get("action", "")).strip()
            if not condition or not action:
                continue

            # 기존 원칙과 유사한 것 있으면 supporting_trades 증가
            c.execute("""
                SELECT id FROM trading_principles
                WHERE trigger_source = ? AND action = ? AND is_active = 1
            """, (trigger_source, action))
            row = c.fetchone()

            if row:
                c.execute("""
                    UPDATE trading_principles
                    SET total_count = total_count + 1,
                        last_updated = ?
                    WHERE id = ?
                """, (now_kst, row[0]))

        conn.commit()
    except Exception as e:
        logger.debug(f"[journal] principles 반영 실패: {e}")
    finally:
        conn.close()


def _parse_json(raw: str) -> dict:
    """AI 응답에서 JSON 추출 (마크다운 펜스 제거)"""
    cleaned = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
    match = re.search(r"\{", cleaned)
    if not match:
        raise ValueError(f"JSON 시작 없음: {cleaned[:60]}")
    json_str = cleaned[match.start():]
    end = json_str.rfind("}")
    if end == -1:
        raise ValueError("JSON 종료 없음")
    return json.loads(json_str[:end + 1])
