# 한국주식 봇 — ARCHITECTURE v12.0
> **AI 필독 규칙:** 이 파일이 유일한 진실이다. 코드 수정 전 전체를 읽고, 수정 후 변경된 섹션을 반드시 이 파일에도 반영하라.

---

## 1. 실행 파이프라인

```
[06:00] data_collector.run()          ← main.py 스케줄에서만 호출
         12개 수집기 asyncio.gather() 병렬
         → _build_signals()           신호1~8 생성
         → _compute_score_summary()   가중치 점수화
         → _cache 저장

[08:30] morning_analyzer.analyze()    ← morning_report.py 에서만 호출
         data_collector.get_cache() 수신
         → _analyze_geopolitics()     사전매칭 + Gemini 보완
         → _analyze_sector_flow()     Z-스코어 ETF 이상감지
         → _analyze_event_impact()
         → _analyze_dart_with_gemini()    ← Gemini 호출 ①
         → _analyze_closing_with_gemini() ← Gemini 호출 ②
         → _enhance_geopolitics_with_gemini() ← Gemini 호출 ③
         → _analyze_theme()
         → _pick_stocks()
         → telegram/sender.py 발송

[09:00~15:30] intraday_analyzer.py    ← AI 없음, 숫자 조건만
         KIS REST 10초 폴링 (등락률·거래량 기준)

[14:50] run_force_close()
[15:20] run_final_close()
[15:45] run_performance_batch()       ← trailing stop 갱신

[일요일 03:00] run_principles_extraction()
[일요일 03:30] run_memory_compression()
```

---

## 2. 파일 구조

```
korea_stock_bot/
├── main.py                        스케줄러 진입점 (로직 없음)
├── config.py                      전역 상수·환경변수 단일 관리
│
├── collectors/
│   ├── data_collector.py          ★ 병렬수집 + _build_signals() + 캐시
│   ├── filings.py                 DART 공시
│   ├── market_global.py           미국증시·원자재·환율 (yfinance)
│   ├── news_naver.py              네이버뉴스·리포트·데이터랩
│   ├── news_newsapi.py            NewsAPI 글로벌뉴스
│   ├── news_global_rss.py         해외RSS + 지정학 통합
│   ├── price_domestic.py          국내 주가·기관·외인 (pykrx)
│   ├── event_calendar.py          기업 이벤트 캘린더
│   ├── sector_etf.py              섹터 ETF 거래량
│   ├── short_interest.py          공매도 잔고
│   ├── closing_strength.py        마감강도
│   ├── volume_surge.py            거래량급증
│   └── fund_concentration.py      자금집중
│
├── analyzers/
│   ├── morning_analyzer.py        ★ 아침봇 통합분석 (Gemini 3개 함수만)
│   └── intraday_analyzer.py       장중봇 — AI 없음, 숫자조건만
│
├── reports/
│   ├── morning_report.py          08:30 아침봇 (morning_analyzer 단일 호출)
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
│   ├── rest_client.py             rate_limiter 내부 호출 전담
│   ├── websocket_client.py
│   └── order_client.py
│
├── traders/
│   └── position_manager.py        포지션 관리·청산 (전 함수 동기)
│
├── tracking/
│   ├── db_schema.py               기동 시 1회 초기화
│   ├── trading_journal.py         거래일지 (alert_recorder 흡수)
│   ├── accuracy_tracker.py        예측 정확도 기록
│   ├── performance_tracker.py     수익률 계산 + trailing stop
│   ├── principles_extractor.py    매매원칙 추출
│   ├── memory_compressor.py       기억 3계층 압축
│   ├── theme_history.py           테마 이력 DB
│   └── ai_context.py              DB 조회 + 컨텍스트 문자열 반환 (동기전용)
│
└── utils/
    ├── logger.py
    ├── date_utils.py
    ├── geopolitics_map.py         키워드→섹터 매핑 사전
    ├── watchlist_state.py         WebSocket 워치리스트 상태
    └── rate_limiter.py
```

---

## 3. data_collector 캐시 계약

> `get_cache()` 반환값의 키명은 **3개 파일**(data_collector / morning_analyzer / morning_report)에서 동일해야 한다. 한 곳 변경 시 3파일 동시 수정 필수.

```python
{
    # 수집 원본
    "collected_at":              str,          # KST ISO — is_fresh() 기준
    "dart_data":                 list[dict],
    "market_data":               dict,
    "news_naver":                dict,
    "news_newsapi":              dict,
    "news_global_rss":           list[dict],
    "price_data":                dict | None,
    "sector_etf_data":           list[dict],
    "short_data":                list[dict],
    "event_calendar":            list[dict],
    "closing_strength_result":   list[dict],
    "volume_surge_result":       list[dict],
    "fund_concentration_result": list[dict],
    # _build_signals() 결과
    "signals":                   list[dict],
    "market_summary":            dict,
    "commodities":               dict,
    "volatility":                str,
    "report_picks":              list[dict],
    "policy_summary":            list[dict],
    "sector_scores":             dict,
    "event_scores":              dict,
    # 메타
    "score_summary":             dict,
    "success_flags":             dict[str, bool],
}
```

캐시 유효 시간: `is_fresh(max_age_minutes=180)` — 06:00 수집 → 08:30 아침봇 ≈ 150분

---

## 4. 모듈 호출 규칙 (의존성)

| 함수 | 유일한 호출자 |
|------|-------------|
| `data_collector.run()` | `main.py 06:00` |
| `morning_analyzer.analyze()` | `morning_report.py` |
| `intraday_analyzer` | `realtime_alert.py` |
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

## 5. AI 모델

> 아래 목록 외 모델 사용 절대 금지. 명시적 지시 없이 추가·교체 금지.

| 용도 | 모델 ID |
|------|---------|
| 아침봇 Gemini 분석 | `gemini-2.5-flash` |
| 경량 보조 | `gemini-2.5-flash-lite` |

**폐기 모델 — 코드에 절대 사용 금지:**
`gemini-2.0-flash` / `gemini-1.5-flash` / `gemini-1.5-pro` / `google-generativeai` SDK (→ `google-genai` SDK만 사용)

---

## 6. 🔒 절대 불변 규칙

> 이 섹션의 규칙은 명시적 지시 없이 위반 불가. 코드 작성 전 확인 필수.

**데이터 파이프라인**
- `data_collector`에서 AI(Gemini/Gemma) 호출 금지 — 수집·캐싱·점수화·신호생성만
- `morning_report.py`에서 `data_collector.run()` 직접 호출 금지 — `get_cache()` / `is_fresh()` 경유 필수
- 캐시 fallback(캐시 없을 때 morning_report가 직접 수집) 제거 금지

**아침봇**
- `morning_analyzer`의 Gemini 호출은 3개 함수로만 제한 (`_analyze_dart_with_gemini` / `_analyze_closing_with_gemini` / `_enhance_geopolitics_with_gemini`)
- `morning_analyzer`에서 텔레그램 발송·DB 기록·KIS 직접 호출 금지
- 삭제된 analyzer(`geopolitics_analyzer`, `oracle_analyzer`, `sector_flow_analyzer` 등)를 `morning_report`에서 직접 import 금지 — 모두 `morning_analyzer` 내부 함수로 통합됨

**장중봇**
- `intraday_analyzer`에서 AI 판단 로직 추가 금지 — 숫자 조건만
- 장중(09:00~15:30) `pykrx` 호출 금지 (15~20분 지연)

**자동매매**
- Trailing Stop 손절가는 상향만 허용: `stop_loss = MAX(현재_stop_loss, new_stop)`
- `TRADING_MODE=REAL` 전환 시 `_check_real_mode_safety()` 5분 대기 생략 금지
- `config.POSITION_MAX` 직접 참조 금지 → `get_effective_position_max()` 경유
- `position_manager` 모든 함수는 동기(sync) — `asyncio.run()` 내부 호출 금지

**DB**
- DB 경로: `config.DB_PATH` 단일 상수 (하드코딩 금지)
- `trading_journal` 테이블: `position_manager`만 INSERT, 다른 모듈은 SELECT 전용
- `kospi_index_stats` 테이블: `memory_compressor.update_index_stats()`만 UPSERT
- `get_journal_context()` 토큰 제한 필수 (`JOURNAL_MAX_ITEMS` / `JOURNAL_MAX_CONTEXT_CHARS`)
- `performance_tracker.run_batch()` → `main.py 15:45`에서만 (장중 pykrx 미확정 방지)

**공통**
- `rate_limiter.acquire()`는 `kis/rest_client.py` 내부에서만 호출
- `config.py` 변수명·캐시 키명 변경 시 전체 영향 파일 동시 수정
- Gemini 호출은 반드시 `try/except` 래핑 — 실패 시 `None`/빈목록 반환, 전체 중단 금지

---

## 7. 코드 수정 후 체크리스트

```
[ ] 이 파일(ARCHITECTURE.md)에서 변경된 섹션 반영했는가?
[ ] 캐시 키명 변경 시 3파일 동시 수정했는가? (data_collector / morning_analyzer / morning_report)
[ ] 새 모듈 호출 경로가 §4 모듈 호출 규칙을 위반하지 않는가?
[ ] §6 절대 불변 규칙 중 위반한 항목이 없는가?
[ ] AI 모델 ID가 §5 목록 내의 것인가?
```
