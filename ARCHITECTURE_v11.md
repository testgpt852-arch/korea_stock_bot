# 🇰🇷 한국주식 봇 — 아키텍처 설계 문서 v11.3

---

## ⚡ HOW TO USE (AI 필독)

> ⚠️ 이 프로젝트의 모든 코드 수정 전, 반드시 이 문서를 전체 통독할 것.
> 특히 '사용 가능한 AI 모델' 및 '절대 사용 금지' 항목은 예외 없이 준수.

1. **`## 📌 현재 기준 진실 (CURRENT TRUTH)`** 섹션이 모든 것의 최우선 기준이다.
   changelog나 하단 내용과 충돌 시 **이 섹션이 항상 이긴다.**

2. **개편·수정 작업 순서 (반드시 준수)**
   - CURRENT TRUTH 섹션 **먼저** 업데이트
   - 코드 작업 진행
   - 완료 후 아래 **문서 동기화 체크리스트** 전 항목 이행
   - changelog에 한 줄 추가

3. **`MODULE CONTRACT` 블록은 CONTRACT 먼저 수정 → 코드 맞춤** (반대 순서 금지).

4. **절대 금지 규칙(RULES.md)은 추가만 가능, 삭제·수정 금지.**

> 📎 **절대 금지 규칙 전문은 `RULES.md` 참조.** 이 파일에는 rule 번호 참조만 사용한다.

---

### 📋 문서 동기화 체크리스트 (코드 수정 시 매번 이행)

> AI는 코드 수정을 완료한 직후, 아래 체크리스트를 **빠짐없이 확인하고 해당 항목을 반드시 업데이트**한다.
> "변경 없음"이 확실한 항목만 건너뛸 수 있다. 불확실하면 업데이트한다.

```
[ ] A. CURRENT TRUTH — 버전 번호(vX.X → vX.Y) 및 수정된 내용 반영
[ ] B. 파일 구조 설명 — 파일 추가·삭제·역할 변경 시 해당 줄 갱신
[ ] C. 인터페이스 계약 — 반환값·파라미터·소스 목록 변경 시 해당 블록 갱신
[ ] D. config.py 핵심 상수 — 신규 상수 추가·기본값 변경 시 반영
[ ] E. RULES.md — 신규 규칙 필요 시 다음 번호로 추가, 변경이력 한 줄 추가
[ ] F. 변경 이력(changelog) — 버전·날짜·한 줄 요약 추가
```

> ⚠️ 특히 **C. 인터페이스 계약**은 반환값 key·소스명·URL이 바뀔 때 반드시 동기화한다.
> 계약과 코드가 불일치하면 이후 AI가 잘못된 기준으로 코드를 수정하는 연쇄 오류가 발생한다.

---

## 📌 현재 기준 진실 (CURRENT TRUTH)

> **이 섹션이 유일한 진실이다. 하단 changelog와 충돌 시 이 섹션 우선.**
> **개편 완료 후 이 섹션을 갱신하고, changelog에 한 줄만 추가하라.**

### 현재 버전: v11.3 (2026-02-28)

---

### ✅ 사용 가능한 AI 모델 (이 목록 외 모델 사용 절대 금지)

| 모델 ID | 용도 | 상태 |
|---------|------|------|
| `gemma-3-27b-it` | ai_analyzer — 장중 급등 판단, 공시·순환매 분석 | ✅ 운영 중 |
| `gemini-3-flash-preview` | geopolitics_analyzer Primary | ✅ 운영 중 |
| `gemini-2.5-flash` | geopolitics_analyzer Fallback | ✅ 지원 |
| `gemini-2.5-flash-lite` | 경량 보조 용도 | ✅ 지원 |

> **모델 유효성은 실제 API 응답으로만 판단한다.**
> 위 목록 외 모델을 추가하거나, 목록에 있는 모델을 제거·교체하는 변경은 명시적 지시 없이 금지한다.

**절대 사용 금지 (서비스 종료 확정):**
```
gemini-1.5-flash / gemini-1.5-flash-002 / gemini-1.5-pro
gemini-2.0-flash / gemini-2.0-flash-lite / gemini-2.0-flash-exp
google-generativeai (구 SDK) → google-genai (신 SDK) 로만 사용
```

---

### 현재 파일 구조 및 역할

```
korea_stock_bot/
├── ARCHITECTURE_v11.md              ← 이 파일 (개편마다 첨부)
├── RULES.md                         ← 절대 금지 규칙 전문 (규칙 추가 시만 첨부)
├── main.py                          ← 스케줄러 + 전역 캐시 (_geopolitics_cache, _event_calendar_cache)
├── config.py                        ← 모든 상수/환경변수 단일 관리
├── requirements.txt                 ← vulture>=2.11 포함 (배포 전 dead code 감지용)
│
├── tests/                           ← 단위 테스트 (외부 API 없이 독립 실행)
│   ├── test_signal_analyzer.py
│   ├── test_position_manager.py
│   ├── test_ai_context.py
│   ├── test_watchlist_state.py
│   └── test_db_schema.py
│   [실행] python -m unittest discover tests -v
│
├── collectors/                      ← 수집 전담 (AI/DB/텔레그램 금지)
│   ├── dart_collector.py
│   ├── event_calendar_collector.py  ← EVENT_CALENDAR_ENABLED=false 기본; KRX KIND 비활성(v14.0)
│   ├── geopolitics_collector.py     ← RSS 파싱 + URL 수집만
│   ├── market_collector.py          ← yfinance + 원자재(TIO=F, ALI=F)
│   ├── news_collector.py            ← datalab_trends 포함 (DATALAB_ENABLED=false 기본)
│   ├── price_collector.py
│   ├── sector_etf_collector.py      ← 마감봇 전용 (rule #15)
│   └── short_interest_collector.py  ← SHORT_INTEREST_ENABLED=false 기본
│
├── analyzers/                       ← 분석 전담 (수집/발송/DB 금지)
│   ├── ai_analyzer.py               ← gemma-3-27b-it: 장중 급등 판단, 공시·순환매 분석
│   ├── closing_strength.py          ← T5 마감 강도 (마감봇 전용)
│   ├── event_impact_analyzer.py     ← 기업이벤트 → 수급 모멘텀 (신호8)
│   ├── fund_inflow_analyzer.py      ← T3 시총 자금유입 (마감봇 전용)
│   ├── geopolitics_analyzer.py      ← gemini-3-flash-preview Primary / gemini-2.5-flash Fallback
│   ├── oracle_analyzer.py           ← 쪽집게 픽 엔진 (_verify_integration 내장)
│   ├── sector_flow_analyzer.py      ← 섹터ETF Z-스코어 + 공매도 클러스터 (신호7)
│   ├── signal_analyzer.py           ← 신호1~8 통합
│   ├── theme_analyzer.py
│   ├── volume_analyzer.py
│   └── volume_flat.py               ← T6 횡보 거래량 급증 (마감봇 전용)
│
├── reports/                         ← 보고서 조립 전담
│   ├── morning_report.py            ← 08:30 (07:30 preview)
│   ├── closing_report.py            ← 18:30
│   ├── realtime_alert.py            ← 장중 실시간
│   └── weekly_report.py             ← 매주 월요일 08:45
│
├── notifiers/
│   ├── telegram_bot.py              ← 포맷·발송 전담
│   ├── telegram_interactive.py      ← /status /holdings /report /evaluate 대화형 명령
│   └── chart_generator.py           ← 차트 이미지 생성 전담
│
├── tracking/                        ← DB 기록 전담
│   ├── accuracy_tracker.py          ← 예측 정확도 누적 + signal_weights 자동 조정
│   ├── ai_context.py
│   ├── db_schema.py                 ← 마이그레이션 단일 관리
│   ├── memory_compressor.py
│   ├── performance_tracker.py
│   ├── principles_extractor.py
│   ├── theme_history.py             ← 이벤트→급등 이력
│   └── trading_journal.py
│
├── traders/
│   └── position_manager.py
│
├── kis/                             ← KIS API 전담 (websocket + REST + order)
│   ├── auth.py
│   ├── order_client.py
│   ├── rest_client.py
│   └── websocket_client.py
│
└── utils/
    ├── date_utils.py
    ├── geopolitics_map.py           ← 이벤트 키워드 → 섹터 매핑 사전
    ├── logger.py
    ├── rate_limiter.py
    ├── state_manager.py
    └── watchlist_state.py           ← 시장 환경 상태 관리
```

---

### 현재 스케줄 (main.py 기준)

| 시각 | 함수 | 설명 |
|------|------|------|
| 06:00 | `run_geopolitics_collect()` | 지정학 뉴스 수집 → `_geopolitics_cache` |
| 06:30 | `run_event_calendar_collect()` | 기업이벤트 수집 → `_event_calendar_cache` |
| 07:00 | KIS 토큰 갱신 | |
| 07:30 | `run_morning_bot()` | 아침봇 preview |
| 08:30 | `run_morning_bot()` | 아침봇 본 실행 |
| 08:45 | `run_weekly_report()` | **매주 월요일만** |
| 09:00~15:30 | 장중봇 | WebSocket + REST 폴링 (10초 간격) |
| 14:50 | `run_force_close()` | 선택적 강제 청산 (AUTO_TRADE=true 시) |
| 15:20 | `run_final_close()` | 최종 청산 (AUTO_TRADE=true 시) |
| 18:30 | `run_closing_bot()` | 마감봇 |
| 18:45 | `perf_batch()` | 수익률 추적 + Trailing Stop 일괄 갱신 |
| 일요일 03:00 | `run_principles_extraction()` | |
| 일요일 03:30 | `run_memory_compression()` | |

---

### 현재 주요 파이프라인

```
[아침봇]
_geopolitics_cache ──┐
_event_calendar_cache ┼→ morning_report.run()
                      │    ├→ dart / market / news / price 수집
                      │    ├→ signal_analyzer(geopolitics_data, event_impact_data)
                      │    │    └→ signals, sector_scores, event_scores
                      │    ├→ determine_and_set_market_env()  ← oracle 전 필수
                      │    └→ oracle_analyzer.analyze(
                      │           price_by_name, signals, market_env,
                      │           sector_scores, event_scores)
                      │         ├→ _verify_integration() 자동 검증
                      │         └→ accuracy_tracker.record_prediction()

[마감봇]
closing_report.run()
    ├→ price_collector + T5/T6/T3 수집
    ├→ sector_etf_collector (마감봇 전용 — rule #15)
    ├→ signal_analyzer(sector_flow_data, event_scores)
    ├→ oracle_analyzer.analyze(..., sector_scores, event_scores)
    │    └→ _verify_integration() 자동 검증
    ├→ accuracy_tracker.record_actual()
    ├→ theme_history.record_closing()
    └→ determine_and_set_market_env()  ← 다음날 기준 재설정
```

---

## 🛡️ 3계층 버그 방어 시스템

### 계층1: 파이프라인 연결 자동 검증 (_verify_integration)

| 모듈 | 검증 항목 |
|------|-----------|
| `oracle_analyzer.py` | price_by_name 타입, signals 타입, market_env 유효값 |

파이프라인 연결 누락 시 `IntegrationError` 발생 → 즉시 식별.

### 계층2: 신규 모듈/기능 추가 시 의무 체크리스트

```
A. 호출 연결 검증
   [ ] 이 함수/모듈을 실제로 호출하는 곳이 존재하는가?
   [ ] main.py 또는 스케줄러에 등록되어 있는가?
   [ ] 초기화 함수(init_table 등)가 앱 시작 시 호출되는가?

B. 데이터 파이프라인 검증
   [ ] 필요한 파라미터가 모두 전달되고 있는가?
   [ ] 캐시 데이터 주입이 필요한 경우 주입 코드가 있는가?
   [ ] 반환값을 받아서 사용하는 곳이 있는가?

C. 의존성 검증
   [ ] 사용하는 외부 패키지가 requirements.txt에 있는가?
   [ ] google-generativeai(구 SDK) import가 없는가?
   [ ] AI 모델 ID가 CURRENT TRUTH '검증된 모델 목록'에 있는가?

D. 문서 동기화
   [ ] ARCHITECTURE의 CURRENT TRUTH 섹션을 업데이트했는가?
   [ ] 스케줄 시간이 문서와 코드에서 동일한가?
   [ ] 새 모듈에 MODULE CONTRACT 블록이 있는가?
   [ ] 신규 규칙이 있으면 RULES.md에 추가했는가?
```

### 계층3: 배포 전 Dead Code 자동 감지

```bash
vulture . --min-confidence 80
# 경고 함수 발견 시 체크리스트 B 항목 재확인
```

---

## 📋 MODULE CONTRACT 규격 (신규 모듈 작성 시 필수)

```python
"""
analyzers/모듈명.py
한줄 설명

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MODULE CONTRACT (파이프라인 연결 검증용 — 수정 금지)
  CALLED BY : 이 모듈을 호출하는 파일 → 함수명()
  INPUT     : 파라미터명: 타입  ← 어디서 오는지 출처 명시
  OUTPUT    : 반환값 타입 → 어디로 전달되는지 목적지 명시
  CALLS     : 이 모듈이 의존하는 외부 서비스/모듈
  AI MODEL  : 사용하는 AI 모델 (해당 시)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
```

> **CONTRACT 규칙**: 파이프라인 변경 시 CONTRACT 먼저 수정 → 코드 맞춤 (반대 순서 금지)

---

## 🚨 KIS WebSocket 운영 규칙 (위반 시 IP·앱키 차단)

### 정상 사용 흐름 (반드시 이 순서 준수)
```
연결 → 종목 구독 → 데이터 수신 → 불필요 종목 구독해제 → 연결 종료
```

### 절대 금지 패턴
```
❌ 비정상1: WebSocket 연결 후 종료를 바로 반복
   → 장 시작(09:00)에 한 번만 연결, 장 마감(15:30)에 한 번만 종료

❌ 비정상2: 구독 후 수신 검증 없이 무한 등록/해제 반복
   → 종목 구독 후 반드시 ack 수신 확인
   → 구독/해제를 루프로 반복하는 코드 절대 금지
```

### 필수 구현 패턴

```python
class KISWebSocketClient:
    async def connect(self):
        if self.connected: return          # 중복 연결 방지

    async def subscribe(self, ticker):
        if ticker in self.subscribed_tickers: return   # 중복 구독 금지
        await self._wait_for_ack(ticker)               # ack 대기 필수
        self.subscribed_tickers.add(ticker)

    async def disconnect(self):
        for ticker in list(self.subscribed_tickers):   # 전체 해제 후 종료
            await self.unsubscribe(ticker)
```

### WebSocket URL 분기 (rule #3)

```python
# kis/websocket_client.py — _get_ws_url() 경유 필수, 상수 직접 사용 금지
_WS_URL_REAL = "ws://ops.koreainvestment.com:21000"
_WS_URL_VTS  = "ws://ops.koreainvestment.com:31000"

def _get_ws_url() -> str:
    return _WS_URL_VTS if config.TRADING_MODE == "VTS" else _WS_URL_REAL
```

> **WS_ORDERBOOK_ENABLED=true 시**: 체결(H0STCNT0) + 호가(H0STASP0) 합계 ≤ WS_WATCHLIST_MAX(40)
> WS_ORDERBOOK_ENABLED=false(기본): REST get_orderbook()으로 호가 분석

---

## ⏱️ 봇별 실행 타임라인

```
[컨테이너 시작 시]
    _maybe_start_now(): 09:00~15:30 AND 개장일 → start_realtime_bot() 즉시 실행

06:00  지정학 뉴스 수집 (GEOPOLITICS_ENABLED=true 시만)
       ① geopolitics_collector.collect() → raw_news
          [영문] AP News / FT Markets / Google News / GDELT RSS
          [한국 정부] 대한민국정책브리핑 korea.kr 통합 RSS:
            - 전체 보도자료 종합 (pressrelease.xml)
            - 부처 브리핑 종합 (ebriefing.xml)
            - 기재부 / 산업부 / 금융위 (★★★ 직접 영향)
            - 과기부 / 방사청 / 국방부 (★★☆ 섹터 영향)
            - 통일부 / 공정위 / 중기부 / 외교부 (★☆☆ 간접 영향)
          소스 실패 → 비치명적 (빈 리스트 반환)
       ② geopolitics_analyzer.analyze(raw_news) → 이벤트 분석
          geopolitics_map 사전 매칭 → gemini-3-flash-preview 배치 분석 → fallback: gemini-2.5-flash → 사전 결과
          신뢰도 GEOPOLITICS_CONFIDENCE_MIN(0.6) 미달 이벤트 필터링
       ③ _geopolitics_cache 전역 변수에 저장 (아침봇·마감봇 공유)
       장중 GEOPOLITICS_POLL_MIN(30분) 간격 폴링: 긴급 이벤트 갱신

06:30  기업 이벤트 캘린더 수집 (EVENT_CALENDAR_ENABLED=true 시만)
       ① event_calendar_collector.collect() → raw_events (DART API)
          pblntf_ty=F(공정공시·IR) / D(주주총회) / A(실적발표), 오늘~14일 후
       ② event_impact_analyzer.analyze(raw_events) → 신호8
          D-1~D-2 실적/IR → 강도5~4 / D-3 주총 → 강도3~4 / D-1 배당 → 강도5
       ③ _event_calendar_cache 전역 변수에 저장

07:00  KIS 토큰 갱신

08:30  아침봇
       ① is_market_open() 확인
       ② dart_collector → 전날 공시
       ③ market_collector → 미국증시·원자재 (철광석 TIO=F, 알루미늄 ALI=F)
       ④ news_collector → 리포트
       ⑤ price_collector → 전날 가격·수급 (pykrx 확정치)
       ⑥ signal_analyzer → 신호1~8
          신호2: XME/SLX ≥ STEEL_ETF_ALERT_THRESHOLD(3%) 급등 시 독립 발화
          신호6: 지정학 캐시 (신뢰도 0.85+→강도5 / 0.70+→4 / 기타→3)
          신호8: 기업 이벤트 캐시 (EVENT_CALENDAR_ENABLED=true 시)
          DataLab: ratio ≥ DATALAB_SPIKE_THRESHOLD(1.5x) 키워드 → 신호2 보완
       ⑦ ai_analyzer.analyze_dart() — 공시 호재/악재 점수화 (Gemma)
       ⑦-b ai_analyzer.analyze_closing(price_data) — 신호4 테마명 AI 교체 (Gemma)
       ⑧ theme_analyzer — 소외도 계산
       ⑧-b determine_and_set_market_env() — 시장환경 설정 (oracle 전 필수)
       ⑨ oracle_analyzer.analyze() — 수급·공시·소외도 기반 픽 (T5/T6/T3=None, rule #57)
       ⑩ accuracy_tracker.record_prediction()
       ⑪ telegram_bot 발송 (쪽집게 선발송 → 핵심 요약 → 상세 리포트)

09:00  장중봇 시작 (컨테이너가 장중이면 즉시)
       [방법B] WebSocket _ws_loop: watchlist 40종목 고정 구독
               H0STCNT0 틱 수신 → 누적 등락률 ≥ PRICE_CHANGE_MIN(3.0%) → 즉시 알림
       [방법A] REST _poll_loop: 10초 간격
               Δ등락률(가속도) ≥ PRICE_DELTA_MIN(0.5%) AND Δ거래량 ≥ VOLUME_DELTA_MIN(5%)
               신규진입(prev 없음): change_rate ≥ FIRST_ENTRY_MIN_RATE(4.0%) — v9.0 추가
       [공통] state_manager.can_alert() — WS/REST 30분 쿨타임 공유
       [AI] ai_analyzer.analyze_spike() (Gemma) → 2차 알림
       [자동매매] AUTO_TRADE=true 시: can_buy() → open_position() / check_exit()

14:50  AUTO_TRADE=true 시 position_manager.force_close_all() (AI 선택적 청산)

15:20  AUTO_TRADE=true 시 position_manager.final_close_all() (잔여 종목 최종 청산)

15:30  장중봇 종료: ws_client.disconnect(), volume_analyzer.reset(), state_manager.reset()

18:30  마감봇
       price_collector → T5(마감강도) / T6(횡보거래량) / T3(자금유입) 수집
       sector_etf_collector + short_interest_collector (rule #15: 마감봇 전용)
       signal_analyzer(sector_flow_data, event_scores) → 신호1~8 + sector_scores
       oracle_analyzer.analyze(..., T5/T6/T3, sector_scores, event_scores) → 내일 픽
       accuracy_tracker.record_actual() — 실제 급등 기록
       theme_history.record_closing()
       determine_and_set_market_env() — 다음날 기준 재설정
       telegram_bot 발송 (쪽집게 선발송 → 마감 리포트)

18:45  performance_tracker.run_batch() → 1/3/7일 수익률 추적
       → position_manager.update_trailing_stops() (pykrx 종가 기준 일괄 갱신)

매주 월요일 08:45
       performance_tracker.get_weekly_stats() + trading_journal.get_weekly_patterns()
       chart_generator.generate_weekly_performance_chart() → PNG 발송

일요일 03:00  principles_extractor.run_weekly_extraction()
일요일 03:30  memory_compressor.run_compression()
```

---

## 📦 인터페이스 계약 (반환값 규격)

```python
# rest_client.get_stock_price(ticker) → dict  [v8.0: 종목명 추가]
{"종목명": str, "현재가": int, "시가": int, "등락률": float, "거래량": int}

# rest_client.get_rate_ranking(market_code) → list[dict]
{"종목코드": str, "종목명": str, "현재가": int,
 "등락률": float, "누적거래량": int, "전일거래량": int}

# price_collector.collect_daily() → dict
{"date": str, "kospi": dict, "kosdaq": dict,
 "upper_limit": list, "top_gainers": list, "top_losers": list,
 "institutional": list, "short_selling": list,
 "by_name": dict, "by_code": dict, "by_sector": dict}

# volume_analyzer.poll_all_markets() → list[dict]
{"종목코드": str, "종목명": str, "등락률": float,
 "직전대비": float,    # 등락률 가속도 (curr등락률 - prev등락률); 신규진입은 change_rate 그대로
 "거래량배율": float,  # 누적RVOL (acml_vol / prdy_vol)
 "순간강도": float,    # 순간 Δvol / 전일거래량 (%)
 "조건충족": bool, "감지시각": str,
 "감지소스": str,      # "rate" | "gap_up" | "websocket"
 "호가분석": dict | None}

# dart_collector.collect() → list[dict]
{"종목명": str, "종목코드": str, "공시종류": str,
 "핵심내용": str, "공시시각": str, "신뢰도": str, "내부자여부": bool}

# ai_analyzer.analyze_spike() → dict
{"판단": str,               # "진짜급등" | "작전주의심" | "판단불가"
 "이유": str,               # 20자 이내
 "target_price": int|None,
 "stop_loss": int|None,
 "risk_reward_ratio": float|None}

# geopolitics_collector.collect() → list[dict]
[{"title": str, "summary": str, "url": str,
  "source":    str,  # "ap_business" | "ap_world" | "ft_markets"
               #  | "kr_pressrelease" | "kr_ebriefing"
               #  | "moef" | "motir" | "fsc" | "msit"
               #  | "dapa" | "mnd" | "unikorea" | "ftc" | "mss" | "mofa"
               #  | "google_news" | "gdelt_*" | "newsapi_*"
  "published": str}] # ISO 8601; 소스 실패 시 해당 소스 빈 리스트 (비치명적)

# geopolitics_analyzer.analyze(raw_news) → list[dict]
[{"event_type":       str,       # geopolitics_map.py 패턴 키
  "affected_sectors": list[str], # ["철강/비철금속", "방산"]
  "impact_direction": str,       # "+" | "-" | "mixed"
  "confidence":       float,     # 0.0~1.0
  "source_url":       str,
  "event_summary_kr": str}]      # 50자 이내 한국어 요약

# signal_analyzer.analyze() → dict
# 신호6 구조 (signals 리스트 내):
{"테마명":   str,
 "발화신호": str,   # "신호6: {event_type} — {summary_kr[:50]} [신뢰도:{conf:.0%}|지정학]"
 "강도":     int,   # 3~5
 "신뢰도":   str,   # "geo:{confidence:.2f}"
 "발화단계": str,
 "상태":     str,   # "+" → "신규" / "-" → "경고"
 "관련종목": list[str]}
# signals 리스트: 강도 내림차순 정렬

# oracle_analyzer.analyze() → dict | None
{"picks": [{"rank": int, "ticker": str, "name": str, "theme": str,
            "entry_price": int, "target_price": int, "stop_price": int,
            "target_pct": float, "stop_pct": float,  # stop_pct 항상 -7.0
            "rr_ratio": float, "score": int,
            "badges": list[str], "position_type": str}],
 "top_themes": [{"theme": str, "score": int, "factors": list[str],
                 "leader": str, "leader_change": float}],
 "market_env": str, "rr_threshold": float, "one_line": str, "has_data": bool}
# R/R 기준: 강세장 1.2+ / 약세장·횡보 2.0+ / 기본 1.5+
# T5/T6/T3: closing_report에서만 전달, morning_report는 None (rule #57)

# position_manager.can_buy(ticker, ai_result, market_env) → (bool, str)
# order_client.buy() → {"success": bool, "order_no": str|None, "ticker": str, "name": str,
#                        "qty": int, "buy_price": int, "total_amt": int, "mode": str, "message": str}
# order_client.sell() → {"success": bool, "order_no": str|None, "ticker": str, "name": str,
#                         "qty": int, "sell_price": int, "mode": str, "message": str}
```

---

## 📦 config.py 핵심 상수

```python
# 장중봇 감지 조건
PRICE_DELTA_MIN      = 0.5    # 등락률 가속도 최소값 (%) — 0.3 미만 금지 (rule #71)
VOLUME_DELTA_MIN     = 5      # 순간 거래량 증가 (전일 대비 %)
CONFIRM_CANDLES      = 1      # 연속 충족 횟수
FIRST_ENTRY_MIN_RATE = 4.0    # 신규진입 종목 단독 감지 임계값 (%) — MIN_CHANGE_RATE 이상 유지
POLL_INTERVAL_SEC    = 10     # KIS REST 폴링 간격 (초)
ALERT_COOLTIME_MIN   = 30     # 중복 알림 방지 쿨타임
GAP_UP_MIN           = 1.5    # T2 갭업 최소 비율 (%)
WS_MAX_RECONNECT     = 3
WS_RECONNECT_DELAY   = 5      # 재연결 간격 (초)
WS_WATCHLIST_MAX     = 40     # KIS 구독 한도 (체결+호가 합산)

# KIS API Rate Limit
KIS_RATE_LIMIT_REAL    = 19   # 초당 최대 호출 (실전)
KIS_RATE_LIMIT_VIRTUAL = 2    # 초당 최대 호출 (모의)

# 마감봇 트리거 임계값
CLOSING_STRENGTH_MIN   = 0.75   # T5 마감 강도 최소값
CLOSING_STRENGTH_TOP_N = 7
VOLUME_FLAT_CHANGE_MAX = 5.0    # T6 횡보 인정 등락률 절대값 상한 (%)
VOLUME_FLAT_SURGE_MIN  = 50.0   # T6 거래량 급증 최소 비율 (%)
FUND_INFLOW_CAP_MIN    = 100_000_000_000  # T3 최소 시가총액 (1000억)

# 자동매매
TRADING_MODE         = "VTS"          # "VTS"=모의 / "REAL"=실전
AUTO_TRADE_ENABLED   = False
POSITION_MAX         = 3
POSITION_BUY_AMOUNT  = 1_000_000
TAKE_PROFIT_1        = 5.0            # 1차 익절 (%)
TAKE_PROFIT_2        = 10.0           # 2차 익절 (%)
STOP_LOSS            = -3.0           # 손절 기준 (%)
DAILY_LOSS_LIMIT     = -3.0           # 당일 누적 손실 한도 (%)
FORCE_CLOSE_TIME     = "14:50"
POSITION_MAX_BULL    = 5              # 강세장
POSITION_MAX_NEUTRAL = 3              # 횡보장
POSITION_MAX_BEAR    = 2              # 약세장
SECTOR_CONCENTRATION_MAX = 2          # 동일 섹터 최대 보유 종목
KIS_FAILURE_SAFE_LOSS_PCT = -1.5      # KIS 조회 실패 시 보수적 손실 추정 (%)
REAL_MODE_CONFIRM_ENABLED = True      # REAL 모드 5분 대기 안전장치

# Trailing Stop
# 강세장 0.92 / 약세장·횡보 0.95 (_TS_RATIO_* 상수, 동시 수정 필수)

# v10.0 Phase 1 철강/비철
STEEL_ETF_ALERT_THRESHOLD = 3.0  # XME/SLX 급등 임계값 (%)

# v10.0 Phase 2 지정학
GEOPOLITICS_ENABLED        = False
GEOPOLITICS_POLL_MIN       = 30
GEOPOLITICS_CONFIDENCE_MIN = 0.6

# v10.0 Phase 3 섹터수급
SECTOR_ETF_ENABLED      = True
SHORT_INTEREST_ENABLED  = False
THEME_HISTORY_ENABLED   = True

# v10.0 Phase 4 이벤트·DataLab
EVENT_CALENDAR_ENABLED  = False
DATALAB_ENABLED         = False
DATALAB_SPIKE_THRESHOLD = 1.5
FULL_REPORT_FORMAT      = False  # true 시 4단계 완전 분석 리포트

# KIS Base URL
# REAL: https://openapi.koreainvestment.com:9443
# VTS:  https://openapivts.koreainvestment.com:29443

# 데이터 소스 선택 원칙
# 장중 실시간 시세 → KIS REST (pykrx 장중 사용 금지 — 15~20분 지연)
# 일별 확정 OHLCV  → pykrx (마감 후 전용)
# 미국증시/원자재  → yfinance
# 공시             → DART API
```

---

## 📜 변경 이력

| 버전 | 날짜 | 요약 |
|------|------|------|
| v11.3 | 2026-02-28 | geopolitics_collector RSS 소스 전면 교체: moef.go.kr/dapa.go.kr(접속불가) → korea.kr 통합 RSS 14개 소스. event_calendar_collector KRX KIND 비활성화(kind.krx.co.kr 서버 점검 상태). 06:00 타임라인·인터페이스 계약 동기화 |
| v11.2 | 2026-02-28 | 문서 분리: 절대 금지 규칙 → RULES.md 이관, HOW TO USE에 작업 순서 지시 추가, 계층2 체크리스트 D항목에 RULES.md 동기화 추가 |
| v11.1 | 2026-02-28 | 규칙 번호 불일치 수정, CURRENT TRUTH 강조 문구 복원, 아침봇 파이프라인에 accuracy_tracker 추가 |
| v11.0 | 2026-02-28 | 아키텍처 문서 정리: 중복 파일구조 제거, 모델 정보 교정(ai_analyzer=gemma-3-27b-it), 규칙 통합, changelog 압축 |
| v10.8 | 2026-02-28 | 3계층 버그 방어 시스템 + MODULE CONTRACT 도입, CURRENT TRUTH 섹션 신설 |
| v10.7 | 2026-02-28 | 버그 13건 전수 수정: SDK 교체, 현재가 키, oracle 파라미터 연결, 캐시 주입, theme_history DDL 이관 등 |
| v10.6 | 2026-02-28 | Phase 4-2: accuracy_tracker, 완전 분석 리포트 포맷, 신호 가중치 학습 |
| v10.5 | 2026-02-28 | Phase 4-1: 기업 이벤트 캘린더, DataLab 트렌드, 신호8 |
| v10.4 | 2026-02-27 | Phase 3: 섹터ETF 수급, 공매도 잔고, theme_history, 신호7 |
| v10.3 | 2026-02-27 | Gemini 모델 정책 교정: gemini-3-flash-preview Primary 확정 |
| v10.2 | 2026-02-27 | 아키텍처 감사: geopolitics 파이프라인 단절 수정 |
| v10.1 | 2026-02-27 | run_geopolitics_collect 누락 추가, 모델 교체 |
| v10.0 | 2026-02-27 | Phase 1·2: 지정학/섹터ETF, 신호6, 신규 모듈 다수 |
| v9.1  | 2026-02-27 | 전수 감사: 할루시네이션 1, 자기모순 3, 퇴행규칙 3 교정 |
| v9.0  | 2026-02-28 | 신규진입 감지 버그 수정 (FIRST_ENTRY_MIN_RATE), 델타 기준 완화 |
| v8.2  | 2026-02-27 | 장중봇 델타 계산: 가격 변화율 → 등락률 가속도로 전환 |
| v8.1  | 2026-02-27 | 쪽집게봇(oracle_analyzer) 통합 — 아침봇·마감봇 픽 + 진입조건 |
| v8.0  | 2026-02-27 | WebSocket URL 분기, 종목명 반환값 추가, memory_compressor 스케줄 등록 |
| v7.0  | 2026-02-27 | tests/ 신규, /report 명령어, KOSPI 지수 레벨 학습 |
| v6.0  | 2026-02-27 | /evaluate 명령어, memory_compressor, REAL 모드 안전장치 |
| v5.0  | 2026-02-26 | chart_generator, telegram_interactive, 주간 성과 차트 |
| v4.4  | 2026-02-26 | 포트폴리오 인텔리전스: 섹터 분산, 동적 POSITION_MAX, 선택적 청산 |
| v4.3  | 2026-02-26 | 거래 일지(trading_journal), 패턴 학습, principles_extractor 연동 |
| v4.2  | 2026-02-26 | Trailing Stop, R/R 필터, AI 프롬프트 강화(윌리엄 오닐) |
| v4.0  | 2026-02-26 | 소~중형주 필터, WebSocket 호가 분석 통합 |
| v3.8  | 2026-02-26 | 초기 급등 포착 강화, 뒷북 방지 |
| v3.6  | 2026-02-26 | 버그 6종: 등락률 0% 버그, T5/T6/T3 dead code 복원, DATE 포맷, rate limit |
| v3.4  | 2026-02-26 | 자동매매(position_manager, order_client) 신규 |
| v3.3  | 2026-02-26 | DB/성과 추적(performance_tracker), 주간 리포트 |
| v3.2  | 2026-02-26 | WebSocket 방법B, T5/T6/T3/T2 트리거, rate_limiter |
| v1.0  | 2026-02-24 | 최초 설계 |
