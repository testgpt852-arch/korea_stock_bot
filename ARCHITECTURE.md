# ARCHITECTURE v13.0 — 계약서

> AI에게: 이 파일의 모든 줄은 동등하게 중요하다. 수정 전 전체 통독.
> 수정 후엔 바뀐 계약만 diff로 제시할 것. 이 파일 전체 재작성 금지.

---

## 1. 호출 규칙 — 유일한 호출자만 허용

| 함수 | 유일한 호출자 |
|------|-------------|
| `data_collector.run()` | `main.py 06:00` |
| `morning_analyzer.analyze(cache)` | `morning_report.run()` |
| `intraday_analyzer.set_watchlist(picks)` | `morning_report.run()` — analyze 직후 |
| `intraday_analyzer.poll_all_markets()` | `realtime_alert.py` |
| `rag_pattern_db.save()` | `performance_tracker.run_batch()` 직후 |
| `rag_pattern_db.get_similar_patterns()` | `morning_analyzer._pick_final()` 내부 |
| `performance_tracker.run_batch()` | `main.py 15:45` — 장중 호출 금지 |
| `position_manager.force_close_all()` | `main.py 14:50` |
| `position_manager.final_close_all()` | `main.py 15:20` |
| `trading_journal` INSERT | `position_manager` 단독 |

---

## 2. 캐시 키 계약 — 3파일 동시 적용 (data_collector / morning_analyzer / morning_report)

```python
get_cache() = {
    "dart_data":                 list[dict],
    "market_data":               dict,          # ← us_market / commodities / forex 포함
    "news_naver":                dict,
    "news_newsapi":              dict,
    "news_global_rss":           list[dict],
    "price_data":                dict | None,   # ← upper_limit / top_gainers / by_code / by_name
    "sector_etf_data":           list[dict],
    "short_data":                list[dict],
    "event_calendar":            list[dict],
    "closing_strength_result":   list[dict],
    "volume_surge_result":       list[dict],
    "fund_concentration_result": list[dict],
    "success_flags":             dict[str, bool],
    "collected_at":              str,
}
# 삭제된 키 — 참조 금지: signals / market_summary / score_summary / report_picks / volatility
```

---

## 3. 모듈 간 반환값 계약

### market_global.collect()
```python
{
    "us_market":   {"sectors": dict, "nasdaq": str, ...},
    "commodities": dict,
    "forex":       {"USD/KRW": float},  # 실패 시 {} — 키 자체는 반드시 존재
}
# morning_analyzer 접근: market_data["us_market"]["sectors"] / ["commodities"] / ["forex"]
```

### morning_analyzer.analyze()
```python
{
    "market_env": {"환경": str, "주도테마후보": list, "한국시장영향": str},
    "candidates": {"후보종목": list[dict], "제외근거": str},
    "picks":      list[dict],   # 최대 15종목 — 아래 picks 계약 참고
}
```

### picks 각 원소
```python
{
    "순위": int, "종목코드": str, "종목명": str,
    "유형": str,          # "공시" / "테마" / "순환매" / "숏스퀴즈"
    "근거": str, "목표등락률": str, "손절기준": str,
    "테마여부": bool, "매수시점": str,
    "cap_tier": str,      # 아래 표준값 참고 — Gemini 미반환 시 역매핑 주입 필수
}
```

### fund_concentration_result 각 원소
```python
{"종목명": str, "자금유입비율": float}
# 키 이름: "자금유입비율" — "ratio" / "거래대금시총비율" 아님
```

---

## 4. 표준 열거값 — 임의 변경 금지

### cap_tier (morning_analyzer ↔ rag_pattern_db 반드시 동일)
```
"소형_300억미만" / "소형_1000억미만" / "소형_3000억미만" / "중형" / "미분류"
# 금지: "소형_극소" / "소형" / "중형이상"
```

### signal_type — 반드시 _map_type_to_signal() 통과 후 저장
```
"공시" → "DART_공시" / "테마" → "테마" / "순환매" → "순환매" / "숏스퀴즈" → "숏스퀴즈"
# "공시" 원문 그대로 저장 금지 → RAG 검색 불일치
```

### close_reason (format_trade_closed 입력)
```
"take_profit_1" / "take_profit_2" / "stop_loss" / "trailing_stop" / "force_close" / "final_close"
```

---

## 5. 함정 목록 — 과거 버그 재발 방지

```python
# ❌ asyncio.get_event_loop()  →  ✅ asyncio.get_running_loop()  (async 컨텍스트)
#                               ✅ asyncio.new_event_loop() + loop.close()  (sync 폴백)

# ❌ WHERE pt.alert_date = today  →  ✅ WHERE pt.tracked_date_1d = today  (RAG 쿼리)

# ❌ fund["ratio"]  →  ✅ fund["자금유입비율"]

# ❌ cap_tier = "소형"  →  ✅ cap_tier = "소형_1000억미만"  (등)

# ❌ daily_picks에 유형 원문 저장  →  ✅ _map_type_to_signal() 변환 후 저장

# ❌ picks에 cap_tier 없음  →  ✅ Gemini 반환 후 candidates에서 역매핑 주입

# ❌ 손절기준 "원" 형식 무시  →  ✅ curr_price <= stop_price 분기 필수

# ❌ format_trade_closed 함수 삭제  →  ✅ sender.py에 반드시 존재 (14:50/15:20 청산)
```

---

## 6. 절대 금지

```
data_collector    → AI 호출 금지
morning_analyzer  → 텔레그램 발송 / KIS 호출 금지  (daily_picks INSERT만 예외)
intraday_analyzer → AI 추가 금지 / 전 종목 스캔 재도입 금지
pykrx             → 장중(09:00~15:30) 호출 금지
Trailing Stop     → stop_loss = MAX(현재, 신규)  하향 금지
POSITION_MAX      → get_effective_position_max() 경유 필수
Gemini 모델       → gemini-2.5-flash 만  /  SDK → google-genai 만
하드코딩 섹터매핑 → US_SECTOR_KR_INDUSTRY / COMMODITY_KR_INDUSTRY 재도입 금지
```
