# 🇰🇷 한국주식 봇 — 아키텍처 설계 문서 v2.5

> **이 문서의 목적**: AI에게 유지보수 요청 시 반드시 이 문서를 첨부할 것.
> AI가 전체 구조를 파악하고 엉뚱한 파일을 건드리는 할루시네이션을 방지한다.

---

## 🚨 KIS WebSocket 운영 규칙 (위반 시 IP·앱키 차단)

> **출처**: 한국투자증권 Open API 공식 공지  
> **위반 결과**: IP 및 앱키 일시 차단 → 봇 전체 중단

### ✅ 정상 사용 흐름 (반드시 이 순서 준수)

```
연결 → 종목 구독 → 데이터 수신 → 불필요 종목 구독해제 → 연결 종료
```

### ❌ 절대 금지 패턴 (차단 대상)

```
비정상 케이스 1: 웹소켓 연결 후 종료를 바로 반복
   → websocket_client.py는 장 시작(09:00)에 한 번만 연결
     장 마감(15:30)에 한 번만 종료

비정상 케이스 2: 구독 후 수신 검증 없이 무한 등록/해제 반복
   → 종목 구독 후 반드시 ack 수신 확인 절차 포함
   → 구독/해제를 루프로 반복하는 코드 절대 금지
```

### kis/websocket_client.py 필수 구현 규칙

```python
class KISWebSocketClient:
    def __init__(self):
        self.connected = False
        self.subscribed_tickers = set()   # 구독 중인 종목 추적

    async def connect(self):
        if self.connected:   # 이미 연결 시 즉시 return
            return

    async def subscribe(self, ticker):
        if ticker in self.subscribed_tickers:   # 중복 구독 금지
            return
        await self._wait_for_ack(ticker)        # ack 대기 필수
        self.subscribed_tickers.add(ticker)

    async def disconnect(self):
        for ticker in list(self.subscribed_tickers):   # 전체 해제 후 종료
            await self.unsubscribe(ticker)

# ❌ 절대 금지
while True: await ws.connect(); await ws.disconnect()   # 연결/종료 루프
while True: await ws.subscribe("005930"); await ws.unsubscribe("005930")  # 구독/해제 루프
```

> **v2.5 참고**: 장중봇(realtime_alert.py)은 KIS WebSocket을 사용하지 않음.
> KIS REST 거래량 순위 API 폴링으로 전환. websocket_client.py는 향후 확장용 보존.

---

## 📁 전체 파일 구조

```
korea_stock_bot/
│
├── ARCHITECTURE.md          ← 이 문서 (AI 유지보수 시 필수 첨부)
├── .env                     ← API 키 모음 (절대 공유 금지)
├── main.py                  ← AsyncIOScheduler 진입점만 (로직 없음)
├── config.py                ← 모든 설정값 상수 (임계값, 시간, 종목 힌트 등)
├── requirements.txt
│
├── collectors/              ← 데이터 수집 전담 (분석 로직 절대 금지)
│   ├── dart_collector.py    ← DART 공시 수집 (내부자거래 포함)
│   ├── price_collector.py   ← pykrx 가격·거래량·기관/외인·공매도 수집 (일별 확정치)
│   ├── market_collector.py  ← 미국증시(yfinance), 원자재 수집
│   └── news_collector.py    ← 증권사 리포트·뉴스 (네이버 검색 API)
│
├── analyzers/               ← 분석 전담 (수집·발송 로직 절대 금지)
│   ├── volume_analyzer.py   ← 장중 급등 감지 (KIS REST 거래량 순위 — 실시간)
│   ├── theme_analyzer.py    ← 테마 그룹핑, 순환매 소외도 계산
│   ├── signal_analyzer.py   ← v6.0 프롬프트 신호 1~5 통합 판단
│   └── ai_analyzer.py       ← Google Gemma-3-27b-it 2차 분석
│
├── notifiers/               ← 발송 전담 (분석 로직 절대 금지)
│   └── telegram_bot.py      ← 텔레그램 메시지 포맷 + 발송
│
├── reports/                 ← 보고서 조립 전담
│   ├── morning_report.py    ← 아침봇 08:30
│   ├── closing_report.py    ← 마감봇 18:30 (순환매 지도)
│   └── realtime_alert.py    ← 장중봇 (KIS REST 폴링 — 60초, 실시간)
│
├── kis/                     ← KIS API 전담 폴더
│   ├── auth.py              ← 토큰 발급·갱신
│   ├── websocket_client.py  ← 실시간 체결 수신 (연결 규칙 엄수 — 현재 장중봇 미사용)
│   └── rest_client.py       ← REST API 호출 (현재가, 거래량 순위 등)
│
└── utils/
    ├── logger.py            ← 로그 기록
    ├── date_utils.py        ← 날짜 계산 + is_market_open()
    └── state_manager.py     ← 중복 알림 방지 (쿨타임 관리)
```

---

## 🔗 파일 의존성 지도

> **유지보수 핵심 규칙**: 한 파일 수정 시 아래 지도에서 영향받는 파일을 먼저 확인한다.

```
파일명                     → 영향받는 파일
─────────────────────────────────────────────────────────────
config.py                 → 모든 파일 (설정값 변경 시 전체 영향)
                            특히 POLL_INTERVAL_SEC → realtime_alert
                            VOLUME_SPIKE_RATIO    → volume_analyzer
                            PRICE_CHANGE_MIN      → volume_analyzer
                            CONFIRM_CANDLES       → volume_analyzer
date_utils.py             → dart_collector, morning_report, closing_report, main
state_manager.py          → realtime_alert
dart_collector.py         → morning_report, signal_analyzer
price_collector.py        → closing_report, morning_report, signal_analyzer
                            ※ 장중봇과 무관 (일별 확정 데이터 전용)
market_collector.py       → morning_report, signal_analyzer
news_collector.py         → morning_report, signal_analyzer
volume_analyzer.py        → realtime_alert
theme_analyzer.py         → closing_report, morning_report
signal_analyzer.py        → morning_report
ai_analyzer.py            → morning_report(공시판단), closing_report(테마그룹핑),
                            realtime_alert(급등판단)
telegram_bot.py           → morning_report, closing_report, realtime_alert
kis/auth.py               → kis/websocket_client, kis/rest_client
kis/rest_client.py        → volume_analyzer (v2.5: 거래량 순위 제공)
kis/websocket_client.py   → (향후 확장용 보존 — 현재 미사용)
```

---

## 🗺️ 시스템 흐름도

```mermaid
graph TD
    subgraph "⏰ main.py — AsyncIOScheduler"
        S1["08:30 아침봇"]
        S2["18:30 마감봇"]
        S3["09:00 장중봇 시작 / 15:30 종료"]
    end

    subgraph "📥 collectors/"
        DC["dart_collector\nDART공시+내부자거래"]
        PC["price_collector\npykrx 일별 확정 데이터\n가격·거래량·기관·공매도"]
        MC["market_collector\n미국증시(yfinance)·원자재"]
        NC["news_collector\n리포트·뉴스(네이버API)"]
    end

    subgraph "🧠 analyzers/"
        VA["volume_analyzer\n급등감지+CONFIRM_CANDLES\nKIS REST 거래량순위 실시간"]
        TA["theme_analyzer\n테마·순환매소외도"]
        SA["signal_analyzer\n신호1~5"]
        AI["ai_analyzer\nGemma-3-27b-it\n하루14400회무료"]
    end

    subgraph "📊 reports/"
        MR["morning_report"]
        CR["closing_report"]
        RA["realtime_alert\nKIS REST 폴링\n60초 간격 실시간"]
    end

    subgraph "🔌 kis/"
        KA["auth.py 토큰"]
        KR["rest_client\n현재가·거래량순위\n(v2.5 핵심 활용)"]
        KW["websocket_client\n향후 확장용 보존"]
    end

    SM["state_manager\n쿨타임·중복방지"]
    TB["telegram_bot"]

    S1 --> MR
    S2 --> CR
    S3 --> RA

    MR --> DC & MC & NC & PC & SA & AI
    CR --> PC & TA & AI
    RA --> VA & SM & AI

    VA --> KR
    SA --> TA
    KA --> KR & KW

    MR & CR & RA --> TB
```

---

## ⏱️ 봇별 실행 타임라인

```
07:00  KIS 토큰 자동 갱신 (auth.py) — REST 방식, WebSocket 아님

08:30  ─── 아침봇 ────────────────────────────────────────────
       ① is_market_open()    → 휴장일이면 전체 중단
       ② dart_collector      → 전날 공시 + 내부자거래 수집
       ③ market_collector    → 미국증시(yfinance), 원자재
       ④ news_collector      → 오늘 리포트 (네이버 검색 API)
       ⑤ price_collector     → 전날 가격·기관/외인 수집 (pykrx 일별 확정치)
       ⑥ signal_analyzer     → 신호 1~5 (v6.0 로직)
       ⑦ ai_analyzer.analyze_dart()  → 공시 호재/악재 점수화 (Gemma)
       ⑧ theme_analyzer      → 순환매 지도
       ⑨ morning_report      → 보고서 조립 + AI점수 신호강도 반영
       ⑩ telegram_bot        → 발송

09:00  ─── 장중봇 시작 (v2.5: KIS REST 폴링) ────────────────
       asyncio.create_task(_poll_loop()) → 폴링 루프 백그라운드 시작
       ↓ POLL_INTERVAL_SEC(60초)마다 반복:
           volume_analyzer.poll_all_markets()
               → KIS REST get_volume_ranking("J") — 코스피 상위 100종목
               → KIS REST get_volume_ranking("Q") — 코스닥 상위 100종목
               → 각 종목: 등락률 ≥ PRICE_CHANGE_MIN(3%)
                          AND 거래량배율 ≥ VOLUME_SPIKE_RATIO(10%)
                          AND CONFIRM_CANDLES(2)회 연속 충족 종목만 반환
           ① state_manager.can_alert() 확인 (쿨타임 30분)
           ② realtime_alert → 1차 알림 즉시 발송
           ③ ai_analyzer.analyze_spike() 비동기 호출
           ④ 2차 알림 (AI 판단 포함: 진짜급등/작전주의심/판단불가)

       [v2.5 KIS REST 방식 근거]
       pykrx: 비공식 KRX 스크래핑 → 장중 15~20분 지연, 언제든 차단 가능
       KIS REST 거래량 순위: 공식 API → 실시간 체결 기준, 지연 없음
       KIS 응답에 prdy_vol(전일거래량) 포함 → 사전 로딩 불필요
       2회 호출(코스피+코스닥)로 주요 급등 종목 모두 커버

       [커버 범위]
       거래량 기준 상위 100종목(코스피) + 100종목(코스닥) 실시간 모니터링
       상한가 후보는 대부분 거래량 급증 → 사실상 전 급등 종목 포착 가능

15:30  ─── 장중봇 종료 ──────────────────────────────────────
       _poll_task.cancel() → 폴링 루프 종료
       volume_analyzer.reset() → 확인카운터 초기화
       state_manager.reset()

18:30  ─── 마감봇 ────────────────────────────────────────────
       ① price_collector     → 전종목 등락률 + 기관/공매도 수집 (pykrx 확정치)
       ② ai_analyzer.analyze_closing() → 테마 그룹핑 + 소외주 식별 (Gemma)
       ③ theme_analyzer      → 소외도 수치 계산
       ④ closing_report      → 내일 순환매 지도 조립
       ⑤ telegram_bot        → 발송
```

---

## 📦 파일별 핵심 규격

### config.py 상수 목록

```python
# ── AI 분석 ──
GOOGLE_AI_API_KEY    = env  # Google AI Studio 무료 발급

# ── 뉴스/리포트 ──
NAVER_CLIENT_ID      = env  # 네이버 개발자센터 무료 발급
NAVER_CLIENT_SECRET  = env

# ── 장중봇 급등 감지 ──
VOLUME_SPIKE_RATIO   = 10     # 전일 총거래량 대비 누적거래량 (%)
PRICE_CHANGE_MIN     = 3.0    # 급등 감지 최소 등락률 (%)
CONFIRM_CANDLES      = 2      # 연속 충족 폴링 횟수 (허위신호 방지)
MARKET_CAP_MIN       = 30_000_000_000
POLL_INTERVAL_SEC    = 60     # REST 폴링 간격 (초)

# ── 중복 알림 방지 ──
ALERT_COOLTIME_MIN   = 30

# ── KIS WebSocket (websocket_client.py 재연결 로직용) ──
WS_MAX_RECONNECT     = 3
WS_RECONNECT_DELAY   = 30

# ── 스케줄 ──
TOKEN_REFRESH_TIME   = "07:00"
MORNING_TIME         = "08:30"
MARKET_OPEN_TIME     = "09:00"
MARKET_CLOSE_TIME    = "15:30"
CLOSING_TIME         = "18:30"
```

### 반환값 규격 (인터페이스 계약)

```python
# volume_analyzer.poll_all_markets() → list[dict]
# volume_analyzer.analyze()          → dict  (WebSocket 향후 확장용)
{"종목코드": str, "종목명": str, "등락률": float,
 "거래량배율": float, "조건충족": bool, "감지시각": str}

# rest_client.get_volume_ranking(market_code) → list[dict]
{"종목코드": str, "종목명": str, "현재가": int,
 "등락률": float, "누적거래량": int, "전일거래량": int}

# rest_client.get_stock_price(ticker) → dict
{"현재가": int, "등락률": float, "거래량": int}

# dart_collector.collect() → list[dict]
{"종목명": str, "종목코드": str, "공시종류": str,
 "핵심내용": str, "공시시각": str, "신뢰도": str, "내부자여부": bool}

# price_collector.collect_daily() → dict
{"date": str, "kospi": dict, "kosdaq": dict,
 "upper_limit": list, "top_gainers": list, "top_losers": list,
 "institutional": list, "short_selling": list,
 "by_name": dict, "by_code": dict, "by_sector": dict}

# ai_analyzer.analyze_spike() → dict
{"판단": str, "이유": str}   # 판단: 진짜급등 | 작전주의심 | 판단불가
```

### KIS REST API 목록 (rest_client.py 구현 현황)

```
tr_id            함수명                   용도
─────────────────────────────────────────────────────────────
FHKST01010100    get_stock_price()        단일 종목 현재가 조회
FHPST01710000    get_volume_ranking()     거래량 순위 (장중봇 핵심)
```

### API 소스 선택 기준

```
데이터 종류          소스      이유
─────────────────────────────────────────────────────────────
장중 실시간 시세     KIS REST  지연 없음, 공식 API, 전일거래량 포함
일별 확정 OHLCV     pykrx     마감 후 확정치, 2회 호출로 전 종목 조회 편의
기관/외인 수급       pykrx     일별 집계 데이터, 장중 의미 없음
미국증시/원자재      yfinance  KIS API 미제공 영역
공시                DART API  공식 공시 전용
```

### AI 모델 선택 근거

```
모델                    하루 무료 한도   선택
─────────────────────────────────────────
gemini-2.5-flash        20회            ❌ 부족
gemini-2.5-flash-lite   20회            ❌ 부족
gemma-3-27b-it          14,400회        ✅ 채택
```

---

## 🔧 AI 유지보수 요청 템플릿

### 단순 수정 (파일 1개)

```
[첨부]: ARCHITECTURE.md 전체 내용
[수정 대상]: config.py
[수정 내용]: ALERT_COOLTIME_MIN 30 → 60
[절대 건드리면 안 되는 파일]: 나머지 전부
[현재 파일 내용]: (config.py 전체 붙여넣기)
```

### 기능 추가 (파일 여러 개)

```
[첨부]: ARCHITECTURE.md 전체 내용
[목표]: RSI 조건 추가

[수정 파일 목록만]
1. config.py          → RSI_MIN = 60 상수 추가
2. volume_analyzer.py → RSI 계산 + 조건 추가
3. telegram_bot.py    → 알림에 RSI 값 추가

[절대 건드리면 안 되는 파일]
kis/websocket_client.py, state_manager.py 등 나머지 전부
```

---

## ⚠️ 절대 금지 규칙

```
[KIS WebSocket — 차단 위험]
1. websocket_client.py에 연결/종료 루프 금지
2. 구독/해제 반복 루프 금지
3. ack 수신 검증 없는 구독 금지
4. 장중 ws.connect() 여러 번 호출 금지

[아키텍처 준수]
5. collectors/ 파일에 분석 로직 금지
6. telegram_bot.py에 분석 로직 금지
7. 반환값 dict 키 구조 변경 시 → 의존성 지도 확인 후 연결 파일 동시 수정
8. config.py 변수명 변경 시 전체 파일 영향 주의
9. ai_analyzer.py에 수집/발송 로직 금지 (분석 결과 반환만)

[종목명 하드코딩 금지]
10. config.py에 종목명을 직접 쓰지 않는다
11. 실제 대장주는 signal_analyzer._get_sector_top_stocks()가
    price_data["by_sector"]에서 매일 동적으로 결정한다

[데이터 소스 원칙]
12. 장중 실시간 시세 → KIS REST API 사용 (pykrx 장중 사용 금지 — 15~20분 지연)
13. 일별 확정 데이터(마감 후) → pykrx 사용 가능
```

---

## 🔄 버전 관리

| 버전 | 날짜 | 변경 내용 |
|------|------|---------|
| v1.0 | 2026-02-24 | 최초 설계 |
| v1.1 | 2026-02-24 | AsyncIOScheduler, state_manager, is_market_open, ai_analyzer, 기관/공매도 추가 |
| v1.2 | 2026-02-24 | KIS WebSocket 공식 공지 반영 — 연결 규칙, 차단 방지 패턴 추가 |
| v2.0 | 2026-02-25 | AI 엔진 교체: Claude API → Google Gemma-3-27b-it |
| v2.1 | 2026-02-25 | 아침봇 독립화, 신호4 추가, 섹터 ETF 연동 |
| v2.2 | 2026-02-25 | price_collector _fetch_index 등락률 0% 버그 수정 |
| v2.3 | 2026-02-25 | 종목 하드코딩 전면 제거, by_sector 동적 조회 |
| v2.4 | 2026-02-25 | 장중봇 구조 전환: KIS WebSocket → pykrx REST 폴링 |
| v2.5 | 2026-02-25 | **데이터 소스 전환: pykrx(지연) → KIS REST(실시간)** |
|      |            | **kis/rest_client.py**: get_volume_ranking() 신규 |
|      |            |   tr_id FHPST01710000, 코스피/코스닥 각 상위 100종목 |
|      |            |   prdy_vol(전일거래량) 포함 → 사전 로딩 완전 불필요 |
|      |            | **volume_analyzer.py**: pykrx 의존성 완전 제거 |
|      |            |   init_prev_volumes / get_top_tickers 제거 |
|      |            |   poll_all_markets() → KIS REST 기반으로 교체 |
|      |            |   analyze()는 WebSocket 향후 확장용으로 보존 |
|      |            | **main.py**: 로그 메시지 수정, 휴장일 체크 추가 |
|      |            |   "장중봇: 미활성(4단계 예정)" → "KIS REST 폴링 활성" |
|      |            | **ARCHITECTURE.md**: API 소스 선택 기준 표 추가 |
|      |            |   KIS REST API 목록 섹션 추가 |
|      |            |   절대 금지 규칙 12~13 추가 (장중 pykrx 사용 금지) |

---

## 📋 환경변수 목록 (.env)

```bash
# ✅ 필수
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=
DART_API_KEY=               # https://opendart.fss.or.kr

# ✅ 강력 권장
GOOGLE_AI_API_KEY=          # https://aistudio.google.com — 무료
NAVER_CLIENT_ID=            # https://developers.naver.com — 무료
NAVER_CLIENT_SECRET=

# ✅ 장중봇 필수 (KIS REST 거래량 순위 API)
KIS_APP_KEY=
KIS_APP_SECRET=
KIS_ACCOUNT_NO=
KIS_ACCOUNT_CODE=01
```

*v2.5 | 2026-02-25 | 장중봇 데이터 소스 전환: pykrx 지연 → KIS REST 실시간*
