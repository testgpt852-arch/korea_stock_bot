# 한국주식 봇 — ARCHITECTURE v12.0

> v12.0 Steps 1~8 완성 기준 (signal_analyzer 흡수 포함)
> 이전 파일: `ARCHITECTURE_v11.md` (보관용, 참조 금지)

---

## 1. 전체 파이프라인 (두 축 설계)

```
06:00  data_collector.run()
       └─ 12개 수집기 asyncio.gather() 병렬 실행
          │  ① filings (DART 공시)
          │  ② market_global (미국증시·원자재)
          │  ③ news_naver
          │  ④ news_newsapi
          │  ⑤ news_global_rss (지정학)
          │  ⑥ price_domestic
          │  ⑦ sector_etf
          │  ⑧ short_interest
          │  ⑨ event_calendar
          │  ⑩ closing_strength  [마감강도]
          │  ⑪ volume_surge      [거래량급증]
          │  ⑫ fund_concentration [자금집중]
          └─ _build_signals() → 신호1~8 생성
          └─ _compute_score_summary() → 가중치 점수화
          └─ _cache 에 저장 ← get_cache() / is_fresh()
                │
                ▼
08:30  morning_analyzer.analyze()          ← Gemini 전담
       data_collector 캐시 수신 (prebuilt_*)
       ① _analyze_geopolitics()            (사전매칭 + Gemini 보완)
       ② _analyze_sector_flow()            (Z-스코어 ETF이상감지)  [BUG-1 참고]
       ③ _analyze_event_impact()
       ④ prebuilt_signals 수신 (신호1~8)
       ⑤ _analyze_dart_with_gemini()       (Gemini 공시 분석)
       ⑥ _analyze_closing_with_gemini()    (Gemini 테마 그룹핑)
       ⑦ _analyze_theme()
       ⑧ _pick_stocks()                    (컨플루언스 스코어링)
                │
                ▼
       telegram/sender.py
       쪽집게 선발송 → 핵심 요약 → 상세 리포트

09:00~15:30  intraday_analyzer.py          ← AI 없음, 숫자 조건만
       KIS REST 10초 폴링 (등락률·거래량 기준)
       poll_all_markets() → alerted 목록 반환
       AI 판단 없음 (진짜급등/작전주의심 완전 제거)

14:50  run_force_close()  — 선택적 강제청산
15:20  run_final_close()  — 최종 청산
15:45  run_performance_batch()  — 수익률 추적 + trailing stop

일요일 03:00  run_principles_extraction()
일요일 03:30  run_memory_compression()
```

---

## 2. 파일 구조

```
korea_stock_bot_v12/
│
├── main.py                        # 스케줄러 진입점 (로직 없음)
├── config.py                      # 전역 설정·환경변수
├── requirements.txt
│
├── collectors/                    # 수집기 — 외부 데이터 수집만 담당
│   ├── data_collector.py          # ★ 핵심: 병렬 수집 + 가중치 점수화 + 캐시
│   │                              #   내부: _build_signals() ← signal_analyzer 흡수
│   │                              #          _compute_score_summary()
│   │                              #          _safe_collect() (비치명 래퍼)
│   ├── filings.py                 # DART 공시 수집
│   ├── market_global.py           # 미국 증시·원자재·환율 (yfinance)
│   ├── news_naver.py              # 네이버 뉴스·리포트·데이터랩
│   ├── news_newsapi.py            # NewsAPI 글로벌 뉴스
│   ├── news_global_rss.py         # 해외 RSS + 지정학 통합
│   ├── price_domestic.py          # 국내 주가·기관·외인 (pykrx)
│   ├── event_calendar.py          # 기업 이벤트 캘린더
│   ├── sector_etf.py              # 섹터 ETF 거래량
│   ├── short_interest.py          # 공매도 잔고
│   ├── closing_strength.py        # 마감강도 [closing_strength_result]
│   ├── volume_surge.py            # 거래량급증 [volume_surge_result]
│   └── fund_concentration.py      # 자금집중 [fund_concentration_result]
│
├── analyzers/                     # 분석기 — 신호·점수·픽 생성
│   ├── morning_analyzer.py        # ★ 핵심: 아침봇 통합 분석 (Gemini 2.5 Flash)
│   │                              #   내부: _analyze_geopolitics()
│   │                              #          _analyze_theme()
│   │                              #          _pick_stocks()       ← oracle 대체
│   │                              #          _analyze_sector_flow()
│   │                              #          _analyze_event_impact()
│   │                              #          _analyze_dart_with_gemini()
│   │                              #          _analyze_closing_with_gemini()
│   └── intraday_analyzer.py       # 장중봇 — 숫자 조건 필터만 (AI 완전 제거)
│
├── reports/                       # 보고서 조립
│   ├── morning_report.py          # 아침봇 08:30 (morning_analyzer 단일 호출)
│   ├── realtime_alert.py          # 장중 실시간 알림
│   └── weekly_report.py           # 주간 보고서
│
├── telegram/                      # 텔레그램 인터페이스
│   ├── sender.py                  # 메시지 포맷·발송
│   ├── commands.py                # 인터랙티브 핸들러 (/status /holdings /principles)
│   └── chart_builder.py           # 차트 이미지 생성
│
├── kis/                           # 한국투자증권 API
│   ├── auth.py
│   ├── rest_client.py
│   ├── websocket_client.py
│   └── order_client.py
│
├── traders/                       # 자동매매
│   └── position_manager.py        # 포지션 관리·청산
│
├── tracking/                      # DB·성과 추적
│   ├── db_schema.py               # DB 초기화 (기동 시 1회)
│   ├── trading_journal.py         # 거래 일지 (alert_recorder 흡수)
│   ├── accuracy_tracker.py        # 예측 정확도 기록
│   ├── performance_tracker.py     # 수익률 계산 + trailing stop
│   ├── principles_extractor.py    # 매매 원칙 추출 (일요일 03:00)
│   ├── memory_compressor.py       # 기억 3계층 압축 (일요일 03:30)
│   ├── theme_history.py           # 테마 이력 DB
│   └── ai_context.py              # DB 조회 + 컨텍스트 문자열 반환
│
├── utils/                         # 공통 유틸
│   ├── logger.py
│   ├── date_utils.py              # get_today, get_prev_trading_day, fmt_ymd 등
│   ├── geopolitics_map.py         # 지정학 사전 (키워드→섹터 매핑)
│   ├── watchlist_state.py         # WebSocket 워치리스트·시장환경 상태
│   └── rate_limiter.py
│
└── tests/
    ├── test_ai_context.py
    └── test_data_sources.py
```

### 삭제된 파일 (v11 → v12)

| 삭제 파일 | 이전 역할 | 대체 |
|-----------|-----------|------|
| `analyzers/ai_analyzer.py` | Gemma AI 분석 | `morning_analyzer._analyze_dart_with_gemini()` 등 |
| `analyzers/signal_analyzer.py` | 신호1~8 가중치 | `data_collector._build_signals()` |
| `analyzers/geopolitics_analyzer.py` | 지정학 분석 | `morning_analyzer._analyze_geopolitics()` |
| `analyzers/theme_analyzer.py` | 테마 그룹핑 | `morning_analyzer._analyze_theme()` |
| `analyzers/oracle_analyzer.py` | 쪽집게 픽 | `morning_analyzer._pick_stocks()` |
| `analyzers/sector_flow_analyzer.py` | 섹터 수급 | `morning_analyzer._analyze_sector_flow()` |
| `analyzers/event_impact_analyzer.py` | 이벤트 신호 | `morning_analyzer._analyze_event_impact()` |
| `analyzers/volume_analyzer.py` (→ 개명) | 장중 감지+Gemma | `analyzers/intraday_analyzer.py` (AI 제거) |
| `collectors/geopolitics_collector.py` | 지정학 수집 | `collectors/news_global_rss.py` 흡수 |
| `collectors/news_collector.py` (→ 3분리) | 뉴스 통합 | naver/newsapi/global_rss 3개로 분리 |
| `reports/closing_report.py` | 마감봇 (18:30) | 폐지 |
| `tracking/alert_recorder.py` | 알림 기록 | `trading_journal.record_alert()` |
| `notifiers/` (→ 폴더 개명) | 텔레그램 | `telegram/` |

---

## 3. data_collector 캐시 구조

```python
# data_collector.get_cache() 반환값
{
    "collected_at":              str,          # KST ISO (is_fresh() 기준)
    "dart_data":                 list[dict],
    "market_data":               dict,
    "news_naver":                dict,
    "news_newsapi":              dict,
    "news_global_rss":           list[dict],   # 지정학 raw (geopolitics_raw)
    "price_data":                dict | None,
    "sector_etf_data":           list[dict],
    "short_data":                list[dict],
    "event_calendar":            list[dict],
    "closing_strength_result":   list[dict],   # [마감강도]
    "volume_surge_result":       list[dict],   # [거래량급증]
    "fund_concentration_result": list[dict],   # [자금집중]
    # ── signal_analyzer 흡수 결과 ──
    "signals":                   list[dict],   # 신호1~8 (강도 내림차순)
    "market_summary":            dict,
    "commodities":               dict,
    "volatility":                str,
    "report_picks":              list[dict],
    "policy_summary":            list[dict],
    "sector_scores":             dict,
    "event_scores":              dict,
    # ── 메타 ──
    "score_summary":             dict,         # 유형별 강도 점수 + total_score
    "success_flags":             dict[str, bool],
}
```

캐시 유효 시간: `is_fresh(max_age_minutes=180)` (기본 3시간)
→ 06:00 수집 → 08:30 아침봇: 약 150분 차이 → 여유 있음

---

## 4. 모듈 의존성 규칙

```
data_collector
    ← main.py 06:00 cron만 run() 호출
    ← morning_report.py 는 get_cache() / is_fresh() 만 호출

morning_analyzer
    ← morning_report.py 만 analyze() 호출
    → data_collector 캐시 수신 (prebuilt_* 파라미터)
    → Gemini API 호출 (3개 함수로만 제한)
    AI/텔레그램/DB/KIS 직접 호출 금지

intraday_analyzer
    ← realtime_alert.py 만 호출
    → kis/rest_client.py (등락률 순위, 호가)
    AI 호출 금지

position_manager
    ← realtime_alert.py (매수 판단)
    ← main.py 14:50/15:20 cron (청산)
    ← performance_tracker (trailing stop)
```

---

## 5. 스케줄 목록

| 시각 | 함수 | 설명 |
|------|------|------|
| 06:00 | `run_data_collector()` | 12종 병렬 수집 + 가중치 점수화 |
| 07:30 | `run_morning_bot()` | 아침봇 preview |
| 08:30 | `run_morning_bot()` | 아침봇 본방 (Gemini 분석) |
| 08:45 | `run_weekly_report()` | 주간 리포트 (월요일만) |
| 09:00 | `start_realtime_bot()` | 장중봇 시작 |
| 14:50 | `run_force_close()` | 선택적 강제청산 |
| 15:20 | `run_final_close()` | 최종 청산 |
| 15:30 | `stop_realtime_bot()` | 장중봇 종료 |
| 15:45 | `run_performance_batch()` | 수익률 추적 + trailing stop |
| 일요일 03:00 | `run_principles_extraction()` | 매매 원칙 추출 |
| 일요일 03:30 | `run_memory_compression()` | 기억 3계층 압축 |

---

## 6. 용어 대응표

| 구버전 코드명 | v12.0 명칭 | 캐시 키 |
|--------------|------------|---------|
| T5 / closing_strength | 마감강도 | `closing_strength_result` |
| T6 / volume_flat | 거래량급증 | `volume_surge_result` |
| T3 / fund_inflow | 자금집중 | `fund_concentration_result` |
| oracle / oracle_analyzer | 쪽집게 픽 / `_pick_stocks()` | — |
| 진짜급등 / 작전주의심 | 제거됨 | — |
