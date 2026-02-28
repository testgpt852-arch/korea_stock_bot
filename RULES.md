# 🇰🇷 한국주식 봇 — 절대 금지 규칙 (RULES.md)

---

## ⚡ HOW TO USE (AI 필독)

- **이 파일은 추가만 가능, 삭제·수정 금지.**
- 신규 규칙 추가 시 다음 번호 채번 후 해당 그룹 끝에 추가하라.
- 규칙 번호는 ARCHITECTURE_v11.md 전체와 코드 주석에서 `rule #N` 형태로 참조된다.
  번호를 변경하면 참조 불일치가 발생하므로 **절대 번호 변경 금지**.
- 현재 최고 번호: **#72**. 다음 신규 규칙은 **#73**부터.

---

## ⚠️ 절대 금지 규칙

### [KIS WebSocket — 차단 위험]
```
#1  websocket_client.py 연결/종료 루프 금지 — 장 시작 1회 연결, 마감 1회 종료
#2  구독/해제 반복 루프 금지 — ack 수신 후 subscribed_tickers에 등록
#3  WS URL 상수 직접 사용 금지 — 반드시 _get_ws_url() 경유 (TRADING_MODE 자동 분기)
#4  WS_ORDERBOOK_ENABLED=true 시 체결+호가 합계 > WS_WATCHLIST_MAX(40) 금지
```

### [레이어 경계]
```
#5  collectors/ 에 분석 로직·AI 호출·DB 기록·텔레그램 발송 금지
#6  telegram_bot.py 에 분석 로직 금지 — 포맷·발송만
#7  ai_analyzer.py 에 수집·발송 로직 금지
#8  oracle_analyzer.py 에 DB·API·발송·수집 로직 금지 — 입력 파라미터만으로 동작
#9  tracking/ 모듈에 분석·발송·수집 로직 금지 — DB 기록·조회만
#10 chart_generator.py 에 텔레그램 발송·DB 기록·AI 호출 금지
    생성 실패 시 반드시 None 반환 (비치명적)
#11 telegram_interactive.py 에 KIS 매수/매도·포지션 기록·AI 분석 호출 금지
    start_interactive_handler()는 main.py 에서만 asyncio.create_task()로 실행
```

### [데이터 소스]
```
#12 장중(09:00~15:30) pykrx 호출 금지 — 15~20분 지연 (일별 확정치 전용)
#13 T5/T6/T3 분석기는 closing_report.py 에서만 호출 (morning_report 금지)
#14 T2(갭상승): 장중봇 전용 — KIS REST 실시간만
#15 sector_etf_collector / short_interest_collector pykrx 호출: 마감봇(18:30) 전용
    sector_flow_analyzer 내부에서 pykrx/KIS 직접 호출 금지 (입력 파라미터만)
#16 rate_limiter.acquire()는 kis/rest_client.py 내부에서만 호출 (외부 중복 호출 금지)
```

### [종목명·설정]
```
#17 config.py 에 종목명 직접 하드코딩 금지 (업종명 키워드만)
#18 대장주는 signal_analyzer가 by_sector에서 동적 결정
#19 반환값 key 변경 시 의존성 확인 후 연결 파일 동시 수정
#20 config.py 변수명 변경 시 전체 영향 주의
```

### [자동매매]
```
#21 TRADING_MODE="REAL" 전환 시 _check_real_mode_safety() 5분 대기 완료 후 활성
    REAL_MODE_CONFIRM_ENABLED=false 시에만 우회 가능 (기본 true — 우회 금지)
#22 AUTO_TRADE_ENABLED 기본값 false — Railway Variables에서 명시적 "true" 설정 시만 활성
#23 position_manager 모든 함수는 동기(sync) — asyncio.run() 내부 호출 금지
#24 can_buy() / open_position() → realtime_alert._send_ai_followup() 에서만 호출
#25 check_exit() → realtime_alert._poll_loop() 매 사이클에서만 호출
#26 force_close_all() → main.py 14:50 cron에서만 호출
#27 final_close_all() → main.py 15:20 cron에서만 호출 (장중 직접 호출 금지)
#28 positions 테이블 peak_price/stop_loss/market_env 컬럼은 position_manager만 관리
    _update_peak() / update_trailing_stops() 경유 필수
#29 Trailing Stop 손절가는 상향만 허용, 하향 금지 — MAX(stop_loss, new_stop) 강제
#30 update_trailing_stops() → performance_tracker.run_batch() 종료 직후에만
    (pykrx 당일 미확정가 방지)
#31 _calc_unrealized_pnl() KIS 조회 실패 시 0 반환 금지
    → POSITION_BUY_AMOUNT × KIS_FAILURE_SAFE_LOSS_PCT(-1.5%) 추정 적용
#32 섹터 집중 체크(SECTOR_CONCENTRATION_MAX)는 can_buy() 에서만 수행
#33 get_effective_position_max() → can_buy() 내부에서만 사용
    config.POSITION_MAX 직접 참조 금지 (동적 조정 무효화 방지)
#34 force_close_all() AI 판단 실패 시 반드시 rule-based fallback 실행
#35 _deferred_close_list는 force_close_all() → _register_deferred_close() 경로로만 등록
#36 analyze_spike() fallback: AI_FALLBACK_ENABLED=true AND 등락률 ≥ 8% AND RVOL ≥ 200%
    AND Gemma API 응답 없음(timeout/500) 시에만 rule-based 허용
    fallback 매수 시 텔레그램 🆘 배지 표시 필수. 기본값 false.
```

### [AI 모델]
```
#37 ai_analyzer.analyze_spike() 프롬프트의 윌리엄 오닐 인격 / SYSTEM CONSTRAINTS 블록 삭제 금지
#38 analyze_spike() 반환값 target_price/stop_loss/risk_reward_ratio 가 None일 때 호출처 None 체크 필수
#39 ai_analyzer AI 호출은 run_in_executor 경유 (동기 Gemma SDK — 이벤트 루프 차단 방지)
#40 /evaluate 대화 타임아웃 EVALUATE_CONV_TIMEOUT_SEC(기본 120초) 반드시 적용
```

### [tracking/DB]
```
#41 ai_context.py 는 DB 조회 + 문자열 반환만 (AI API 호출·발송·매수 로직 금지)
    모든 함수는 동기(sync) — realtime_alert에서 run_in_executor 경유
#42 principles_extractor.py → main.py 일요일 03:00 cron에서만 호출
#43 trading_journal.py: record_journal() → position_manager.close_position() 에서만 호출
#44 get_journal_context() → ai_context.py 에서만 호출 (realtime_alert 직접 호출 금지)
#45 get_weekly_patterns() → weekly_report.py 에서만 호출 (30일 기준 집계)
#46 _integrate_journal_patterns() → principles_extractor.run_weekly_extraction() 내부에서만
#47 trading_journal 테이블: position_manager만 INSERT, 다른 모듈은 SELECT 전용
#48 get_journal_context() 토큰 제한 필수: JOURNAL_MAX_ITEMS / JOURNAL_MAX_CONTEXT_CHARS
    무제한 전체 조회 금지 (장기 운영 토큰 증가 방지)
#49 kospi_index_stats 테이블: memory_compressor.update_index_stats()만 UPSERT
    외부 직접 INSERT/UPDATE 금지
#50 run_memory_compression() → main.py 일요일 03:30에서만 호출 (중복 실행 방지)
#51 DB 파일 경로는 config.DB_PATH 단일 상수로 관리 (하드코딩 금지)
#52 alert_recorder.record_alert() → realtime_alert._dispatch_alerts() 에서만 호출 (기본)
    예외: oracle_recorder.record_oracle_pick() 은 morning/closing_report 발송 후 별도 호출 허용
#53 performance_tracker.run_batch() → main.py 18:45 cron에서만 호출
    (장중 직접 호출 금지 — pykrx 당일 미확정 데이터 방지)
#54 accuracy_tracker.py 에 AI 호출·발송·KIS 호출·수집 로직 금지
    저장 실패 시 logger.warning + 무시 (아침봇/마감봇 blocking 금지)
#55 oracle_analyzer.analyze() 실패 시 None 반환 (비치명적)
    oracle 실패가 전체 리포트 발송을 막으면 안 됨
```

### [오라클·신호 라우팅]
```
#56 oracle_analyzer.analyze() → closing_report / morning_report 에서만 호출
    장중봇(realtime_alert) 직접 호출 금지
#57 oracle에 T5/T6/T3 파라미터: closing_report에서만 전달, morning_report는 None 전달 필수
#58 신호6(지정학) / 신호7(섹터수급) / 신호8(기업이벤트): 반드시 signal_analyzer.analyze() 경유
    oracle_analyzer에 직접 전달 금지
#59 format_oracle_section() → telegram_bot.py 에만 위치 (report 모듈에서 직접 포맷 금지)
#60 determine_and_set_market_env() → morning_report.py 에서만 호출 (원칙)
    예외: KOSPI 장중 등락률 ±2.0% 초과 시 realtime_alert._emergency_env_update() 1회 허용
    (텔레그램 경고 알림 필수, 재재설정 금지 — _env_emergency_used 플래그 관리)
```

### [v10.0 신규 모듈 역할 경계]
```
#61 geopolitics_collector.py: RSS 파싱 + URL 수집만
    AI 분석·텔레그램 발송·DB 기록 금지; 소스 실패 시 빈 리스트 반환
#62 geopolitics_analyzer.py: 지정학 이벤트 → 섹터 맵핑 + Gemini 분석만
    KIS API·pykrx 호출 금지; AI 실패 시 사전(geopolitics_map) 결과로 fallback
#63 utils/geopolitics_map.py: 키워드→섹터 사전 전용
    분석 로직·API 호출 금지; 신규 패턴 추가 시 이 파일만 수정
#64 event_calendar_collector.py: DART API 파싱 + 이벤트 목록 반환만
    AI 분석·발송·DB 기록 금지; 실패 시 빈 리스트 반환
#65 event_impact_analyzer.py: 기업 이벤트 → 수급 모멘텀 예측만
    KIS API·pykrx·AI 호출 금지 (입력 파라미터만으로 동작)
#66 theme_history.py: 이벤트→급등 이력 DB 저장·조회만
    분석·발송·AI 호출 금지
#67 news_collector._collect_datalab_trends(): 네이버 DataLab API 호출만
    분석·발송·DB 기록 금지; DATALAB_ENABLED=false 시 즉시 빈 리스트 반환
```

### [장중봇 감지 로직]
```
#68 poll_all_markets(): prev 없는 신규진입 종목 → first-entry 블록 처리
    `if not prev: continue` 패턴 복귀 금지 (신규진입 알림 0건 버그 재발 방지)
    first-entry 조건: FIRST_ENTRY_MIN_RATE + MIN_VOL_RATIO_ACML 동시 준수
#69 FIRST_ENTRY_MIN_RATE는 MIN_CHANGE_RATE 이상으로만 설정
#70 _detect_gap_up()에 MIN_VOL_RATIO_ACML 필터 반드시 포함 (노이즈 방지)
#71 PRICE_DELTA_MIN은 0.3 미만으로 낮추지 말 것 (MAX_ALERTS_PER_CYCLE 포화 위험)
```

### [테스트]
```
#72 tests/ 에서 실제 외부 API 호출 금지 — 순수 로직 + 임시 SQLite DB만 허용
    새 모듈 추가 시 tests/ 에 대응 테스트 파일 생성 권장
```

---

## 📜 변경 이력

| 날짜 | 추가 규칙 | 배경 |
|------|-----------|------|
| 2026-02-28 | #1~#72 | ARCHITECTURE_v11.md에서 분리 신설 |
