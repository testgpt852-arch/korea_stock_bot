# 한국주식 봇 — ARCHITECTURE v13.0

> **AI 필독 규칙:** 이 파일이 유일한 진실이다.  
> 코드 수정 전 전체를 읽고, 수정 후 변경된 섹션을 반드시 이 파일에도 반영하라.

---

## 1. 실행 파이프라인

```
[06:00] data_collector.run()                ← main.py 스케줄에서만 호출
         12개 수집기 asyncio.gather() 병렬
         숫자 기준 필터링만 적용 (하드코딩 매핑 전면 없음)
         → 필터링된 원시 데이터 캐시 저장
         → 텔레그램 원시 데이터 요약 발송  ← [v13.0] Gemini 장애 대비

[08:30] morning_report.run(cache=dc)        ← main.py → await run(cache=dc) 단일 호출
         ↓
         morning_analyzer.analyze(cache)    ← analyze(cache: dict) 단일 인수
         ┌─ run_in_executor(_analyze_market_env)  ← Gemini 호출 ① (비동기 처리)
         │   입력: 미국 섹터ETF(±2%+) + 원자재 + 환율
         │   출력: 리스크온/오프 + 주도테마후보
         │
         ├─ run_in_executor(_analyze_materials)   ← Gemini 호출 ② (비동기 처리)
         │   입력: DART본문 + 뉴스 + 가격 + 호출①결과
         │   출력: 후보 20종목 이내 + 재료강도 + cap_tier
         │
         └─ run_in_executor(_pick_final)          ← Gemini 호출 ③ (비동기 처리)
             입력: 자금집중 + 공매도 + RAG패턴 + 호출②결과
             출력: picks 15종목 [근거/목표가/손절가/테마여부/매수우선순위]
                          ↓
             daily_picks 테이블 INSERT     ← [v13.0] DB 저장 (RAG 연결용)
                          ↓
           morning_report.py 텔레그램 발송 (시장환경 + 픽 15종목 포맷)
                          ↓
           intraday_analyzer.set_watchlist(picks)
                          ↓
                 WebSocket/REST 15종목 감시 시작

[09:00~15:30] intraday_analyzer.py          ← AI 없음, 숫자 조건만
         모닝봇 픽 15종목만 REST 개별 조회 감시:
         ① 가격 도달 알림 (목표/손절)
         ② 급등 모멘텀 (Δ등락률 + 체결강도)
         ③ 매수벽 감지 (호가잔량비율)
         전 종목 스캔 완전 없음

[14:50] run_force_close()
[15:20] run_final_close()
[15:45] performance_tracker.run_batch()     ← trailing stop 갱신
         → daily_picks SELECT              ← [v13.0] DB에서 당일 픽 조회
         → rag_pattern_db.save()           ← [v13.0] RAG 패턴 자동 저장

[일요일 03:00] run_principles_extraction()
[일요일 03:30] run_memory_compression()
```

---

## 2. 파일 구조

```
korea_stock_bot/
├── main.py                        스케줄러 진입점 (로직 없음)
│                                    [v13.0] run_morning_bot(): await run(cache=dc) 단일 호출
├── config.py                      전역 상수·환경변수 단일 관리
│
├── collectors/
│   ├── data_collector.py          ★ 병렬수집 + 숫자필터링 + 캐시 + 원시데이터발송
│   │                                [v13.0] _build_signals/_compute_score_summary 제거
│   ├── filings.py                 DART 공시 — 본문(rcept_no API) 포함 수집
│   │                                [v13.0] DART_CONTRACT_MIN_RATIO=20%, 본문요약 추가
│   ├── market_global.py           미국증시·원자재·환율 — ±2%+ 필터만 (매핑 없음)
│   ├── news_naver.py              네이버뉴스·리포트·데이터랩
│   ├── news_newsapi.py            NewsAPI 글로벌뉴스
│   ├── news_global_rss.py         해외RSS + 지정학 통합
│   ├── price_domestic.py          국내 주가 — 시총 3000억 이하 필터 + 15%+ 급등기준
│   │                                [v13.0] 병렬폴백 시 시총=0 종목 upper/top_gainers 제외
│   ├── event_calendar.py          기업 이벤트 캘린더
│   ├── sector_etf.py              섹터 ETF 거래량 (거래량 500%+ 필터)
│   ├── short_interest.py          공매도 잔고 (상위 20종목)
│   ├── closing_strength.py        마감강도 (상위 20종목)
│   ├── volume_surge.py            거래량급증 (500%+ 기준)
│   └── fund_concentration.py      자금집중 — 거래대금/시총 비율 (상위 20종목)
│
├── analyzers/
│   ├── morning_analyzer.py        ★ 아침봇 통합분석 — Gemini 3단계 구조
│   │                                [v13.0] analyze(cache: dict) 단일 인수
│   │                                        run_in_executor로 Gemini 호출 비동기 처리
│   │                                        _analyze_materials() 반환값에 cap_tier 추가
│   │                                        _pick_final() 완료 후 daily_picks INSERT
│   │                                        _save_daily_picks() / _infer_cap_tier_from_cap() 추가
│   └── intraday_analyzer.py       ★ 장중봇 — 픽 15종목 전담 감시
│                                    [v13.0] set_watchlist()에 _ws_alerted_tickers.clear() 추가
│
├── reports/
│   ├── morning_report.py          08:30 아침봇
│   │                                [v13.0 전면 재작성]
│   │                                  run(cache: dict) 단일 인수
│   │                                  morning_analyzer.analyze(cache) 단일 호출
│   │                                  market_env/candidates/picks 구조로 추출
│   │                                  _format_market_env() / _format_picks() 신규
│   │                                  signal_result/oracle_result 등 v12 참조 전부 제거
│   ├── realtime_alert.py          장중 실시간 알림
│   └── weekly_report.py           주간 보고서
│
├── telegram/
│   ├── sender.py
│   ├── commands.py
│   └── chart_builder.py
│
├── kis/
│   ├── auth.py
│   ├── rest_client.py
│   ├── websocket_client.py
│   └── order_client.py
│
├── traders/
│   └── position_manager.py
│
├── tracking/
│   ├── db_schema.py               기동 시 1회 초기화
│   │                                [v13.0] daily_picks 테이블 추가 + _migrate_v130_picks()
│   ├── trading_journal.py
│   ├── accuracy_tracker.py
│   ├── performance_tracker.py     수익률 계산 + trailing stop
│   │                                [v13.0] _save_rag_patterns_after_batch():
│   │                                        daily_picks SELECT → rag_save(picks=실제픽)
│   ├── rag_pattern_db.py          [v13.0 신규] 신호→픽→결과 패턴 저장 + 유사패턴 검색
│   ├── principles_extractor.py
│   ├── memory_compressor.py
│   ├── theme_history.py
│   └── ai_context.py
│
└── utils/
    ├── logger.py
    ├── date_utils.py
    ├── watchlist_state.py
    ├── geopolitics_map.py         [v13.0] US_SECTOR_KR_INDUSTRY 잔존 주석 제거
    └── rate_limiter.py
```

---

## 3. data_collector 캐시 계약

> `get_cache()` 반환값의 키명은 **3개 파일**(data_collector / morning_analyzer / morning_report)에서 동일해야 한다.  
> 한 곳 변경 시 3파일 동시 수정 필수.  
> **[v13.0] 아래 "삭제된 키"는 절대 참조 금지.**

```python
# 개편 후 get_cache() 반환값
{
    "collected_at":              str,          # KST ISO — is_fresh() 기준

    # 수집 원본 (숫자 필터링 적용된 원시 데이터)
    "dart_data":                 list[dict],   # 본문요약(본문요약 필드) 포함
    "market_data":               dict,         # ±2%+ 섹터ETF만
    "news_naver":                dict,
    "news_newsapi":              dict,
    "news_global_rss":           list[dict],
    "price_data":                dict | None,  # 시총 3000억 이하 필터 적용
    "sector_etf_data":           list[dict],   # 거래량 500%+ 이상
    "short_data":                list[dict],   # 상위 20종목
    "event_calendar":            list[dict],
    "closing_strength_result":   list[dict],   # 상위 20종목
    "volume_surge_result":       list[dict],   # 상위 20종목
    "fund_concentration_result": list[dict],   # 상위 20종목

    # 메타
    "success_flags":             dict[str, bool],

    # ── 삭제된 키 (v13.0 — 절대 참조 금지) ──────────────────
    # "signals"        ← 삭제 (_build_signals() 제거)
    # "market_summary" ← 삭제
    # "score_summary"  ← 삭제 (_compute_score_summary() 제거)
    # "commodities"    ← 삭제
    # "volatility"     ← 삭제
    # "report_picks"   ← 삭제
    # "policy_summary" ← 삭제
    # "sector_scores"  ← 삭제
    # "event_scores"   ← 삭제
}
```

캐시 유효 시간: `is_fresh(max_age_minutes=180)` — 06:00 수집 → 08:30 아침봇 ≈ 150분

---

## 4. morning_analyzer 반환값 계약

`morning_analyzer.analyze()` → `morning_report.py` 전달 구조:

```python
{
    # [v13.0] Gemini 3단계 최종 결과
    "picks": list[dict],           # 최종 픽 15종목 — intraday_analyzer.set_watchlist() 전달
    # 각 pick:
    # {
    #   "순위": int,
    #   "종목코드": str,
    #   "종목명": str,
    #   "근거": str,
    #   "목표등락률": "20%"/"상한가",
    #   "손절기준": str,
    #   "테마여부": bool,
    #   "매수시점": str,
    # }

    # 하위 호환 (v12.0 기존 키 — morning_report 보고서 조립용)
    "signals":            list[dict],
    "market_summary":     dict,
    "commodities":        dict,
    "volatility":         str,
    "report_picks":       list[dict],
    "policy_summary":     list[dict],
    "sector_scores":      dict,
    "event_scores":       dict,
    "ai_dart_results":    list[dict],
    "theme_result":       dict,
    "oracle_result":      dict | None,
    "geopolitics_analyzed": list[dict],
}
```

---

## 5. intraday_analyzer 반환값 계약

`poll_all_markets()` / `analyze_ws_tick()` 반환 dict:

```python
{
    "종목코드":   str,
    "종목명":     str,
    "현재가":     int,        # v10.7 AI target/stop 계산용
    "등락률":     float,
    "직전대비":   float,
    "거래량배율": float,
    "순간강도":   float,
    "조건충족":   bool,
    "감지시각":   str,        # HH:MM:SS
    "감지소스":   str,        # "watchlist" | "websocket"
    "호가분석":   dict | None,
    "픽근거":     str | None, # [v13.0] 모닝봇 근거 텍스트
    "알림유형":   str | None, # [v13.0] "가격도달_목표"|"가격도달_손절"|"매수벽"|"급등모멘텀"
}

# 호가분석 dict:
{
    "매수매도비율": float,
    "상위3집중도":  float,   # 상위3 매도호가 잔량 / 총매도잔량
    "호가강도":     str,     # "강세" | "중립" | "약세"
    "매수잔량":     int,
    "매도잔량":     int,
}
```

---

## 6. 모듈 호출 규칙 (의존성)

| 함수 | 유일한 호출자 |
|------|-------------|
| `data_collector.run()` | `main.py 06:00` |
| `morning_analyzer.analyze()` | `morning_report.py` |
| `morning_analyzer._analyze_market_env()` | `morning_analyzer.analyze()` 내부만 |
| `morning_analyzer._analyze_materials()` | `morning_analyzer.analyze()` 내부만 |
| `morning_analyzer._pick_final()` | `morning_analyzer.analyze()` 내부만 |
| `intraday_analyzer.set_watchlist()` | `morning_report.py` (발송 직후) |
| `intraday_analyzer.poll_all_markets()` | `realtime_alert.py` |
| `rag_pattern_db.save()` | `performance_tracker.run_batch()` 직후만 |
| `rag_pattern_db.get_similar_patterns()` | `morning_analyzer._pick_final()` 내부만 |
| `position_manager.can_buy() / open_position()` | `realtime_alert._send_ai_followup()` |
| `position_manager.force_close_all()` | `main.py 14:50` |
| `position_manager.final_close_all()` | `main.py 15:20` |
| `performance_tracker.run_batch()` | `main.py 15:45` |
| `trading_journal.record_alert()` | `realtime_alert._dispatch_alerts()` |
| `trading_journal.record_journal()` | `position_manager.close_position()` |
| `get_journal_context()` | `ai_context.py` 내부만 |
| `principles_extractor.run()` | `main.py 일요일 03:00` |
| `memory_compressor.run()` | `main.py 일요일 03:30` |

---

## 7. config.py 주요 상수 (v13.0 기준)

```python
# DART 필터 (강화)
DART_CONTRACT_MIN_RATIO   = 20     # 자기자본대비 20%+ (구: 10%)
DART_CONTRACT_MIN_BILLION = 100    # 계약금액 100억+ (구: 50억)
DART_DIVIDEND_MIN_RATE    = 5      # 시가배당률 5%+ (구: 3%)

# 미국 섹터 / 원자재 필터 (신규)
US_SECTOR_SIGNAL_MIN      = 2.0    # ±2.0%+ (구: 1.0%)
COMMODITY_SIGNAL_MIN      = 1.5    # ±1.5%+ (신규)

# 가격 필터 (신규)
PRICE_CAP_MAX             = 300_000_000_000   # 시총 3000억 이하
PRICE_GAINER_MIN_RATE     = 15.0   # 급등 기준 15%+ (구: 7%)

# 자금집중
FUND_INFLOW_CAP_MIN       = 30_000_000_000    # 시총 300억+ (구: 1000억)
FUND_INFLOW_TOP_N         = 20     # 상위 20종목 (구: 7)

# 거래량/공매도
VOLUME_SURGE_MIN_RATIO    = 5.0    # 500%+
SHORT_TOP_N               = 20     # 공매도 상위 20종목

# 아침봇
MORNING_PICK_MAX          = 15     # 최종 픽 최대 15종목 (구: 5)

# 삭제된 상수
# US_SECTOR_KR_INDUSTRY   ← 삭제 (하드코딩 매핑)
# COMMODITY_KR_INDUSTRY   ← 삭제
# SECTOR_TOP_N            ← 삭제
```

---

## 8. AI 모델

> 아래 목록 외 모델 사용 절대 금지. 명시적 지시 없이 추가·교체 금지.

| 용도 | 모델 ID |
|------|---------| 
| 아침봇 Gemini 분석 (3단계) | `gemini-2.5-flash` |
| 경량 보조 | `gemini-2.5-flash-lite` |

**SDK:** `google-genai` 만 사용. `google-generativeai` 절대 금지.

**폐기 모델:** `gemini-2.0-flash` / `gemini-1.5-flash` / `gemini-1.5-pro`

---

## 9. 🔒 절대 불변 규칙

> 명시적 지시 없이 위반 불가. 코드 작성 전 전체 확인 필수.

**데이터 파이프라인**
- `data_collector` 에서 AI(Gemini) 호출 금지 — 수집·필터링·캐싱만
- `morning_report.py` 에서 `data_collector.run()` 직접 호출 금지 — `get_cache()` / `is_fresh()` 경유 필수
- 캐시 fallback(캐시 없을 때 직접 수집) 제거 금지
- 삭제된 캐시 키(`signals`, `market_summary` 등) 참조 금지

**아침봇**
- `morning_analyzer` Gemini 호출 3개 함수로만 제한 (`_analyze_market_env` / `_analyze_materials` / `_pick_final`)
- `morning_analyzer` 에서 텔레그램 발송·DB 기록·KIS 직접 호출 금지
- 하드코딩 섹터 매핑(`US_SECTOR_KR_INDUSTRY` 등) 재도입 금지 — AI가 직접 판단

**장중봇**
- `intraday_analyzer` 에서 AI 판단 로직 추가 금지 — 숫자 조건만
- `intraday_analyzer.poll_all_markets()` 에서 전 종목 스캔 재도입 금지
- `intraday_analyzer.set_watchlist()` 호출자: `morning_report.py` 만
- 장중(09:00~15:30) `pykrx` 호출 금지 (15~20분 지연)

**RAG 패턴 DB**
- `rag_pattern_db.save()` 호출자: `performance_tracker.run_batch()` 직후만
- `rag_pattern_db.get_similar_patterns()` 호출자: `morning_analyzer._pick_final()` 내부만

**자동매매**
- Trailing Stop 손절가 상향만 허용: `stop_loss = MAX(현재_stop_loss, new_stop)`
- `TRADING_MODE=REAL` 전환 시 `_check_real_mode_safety()` 5분 대기 생략 금지
- `config.POSITION_MAX` 직접 참조 금지 → `get_effective_position_max()` 경유
- `position_manager` 모든 함수 동기(sync) — `asyncio.run()` 내부 호출 금지

**DB**
- DB 경로: `config.DB_PATH` 단일 상수 (하드코딩 금지)
- `trading_journal` 테이블: `position_manager` 만 INSERT
- `kospi_index_stats` 테이블: `memory_compressor.update_index_stats()` 만 UPSERT
- `performance_tracker.run_batch()` → `main.py 15:45` 에서만 (장중 pykrx 미확정 방지)

**공통**
- `rate_limiter.acquire()` 는 `kis/rest_client.py` 내부에서만 호출
- `config.py` 변수명·캐시 키명 변경 시 전체 영향 파일 동시 수정
- Gemini 호출은 반드시 `try/except` 래핑 — 실패 시 `None`/빈목록 반환, 전체 중단 금지
- AI 모델 ID: `gemini-2.5-flash` / `gemini-2.5-flash-lite` 만 (§8 목록)

---

## 10. 코드 수정 후 체크리스트

```
[ ] 이 파일(ARCHITECTURE.md) 에서 변경된 섹션 반영했는가?
[ ] 캐시 키명 변경 시 3파일 동시 수정? (data_collector / morning_analyzer / morning_report)
[ ] 새 모듈 호출 경로가 §6 모듈 호출 규칙을 위반하지 않는가?
[ ] §9 절대 불변 규칙 중 위반한 항목이 없는가?
[ ] AI 모델 ID가 §8 목록 내의 것인가?
[ ] intraday_analyzer 에 전 종목 스캔 로직 재도입하지 않았는가?
[ ] data_collector 삭제된 캐시 키를 morning_analyzer/morning_report 에서 참조하지 않는가?
```
