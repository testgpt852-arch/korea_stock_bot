# 한국주식 봇 — RULES v12.0

> v12.0 Steps 1~8 기준. 구버전(v11) 규칙 전면 정리.
> 삭제된 규칙: #15(sector_flow_analyzer), #36~39(ai_analyzer/analyze_spike)
> 변경된 규칙: #53 (18:45→15:45)
> 신규 규칙: #58~61

---

## [데이터 파이프라인 — data_collector]

```
#1  수집기는 collectors/ 전담 — 분석 로직 금지, 외부 API 호출만
#2  data_collector.run()은 main.py 06:00 스케줄에서만 호출
    morning_report, realtime_alert에서 직접 run() 호출 금지 (캐시 경유 필수)
#3  data_collector._safe_collect() 래퍼로 모든 수집기 실행 — 개별 실패는 비치명적
    수집 실패 시 None 반환 → 기본값 보정 → 전체 파이프라인 계속 진행
#4  data_collector에서 AI API 호출 금지 (수집·캐싱·점수화·신호생성만)
    signal_analyzer 로직이 흡수됐더라도 Gemini/Gemma 호출 추가 금지
#5  data_collector 캐시 유효 시간: is_fresh(max_age_minutes=180) 기본값 유지
    캐시 없거나 오래됐을 때 morning_report가 직접 수집하는 fallback 보존 필수
```

## [아침봇 — morning_analyzer]

```
#6  morning_analyzer.analyze()는 morning_report.py에서만 호출
    다른 모듈(realtime_alert, commands 등)에서 직접 호출 금지
#7  morning_analyzer에서 Gemini 호출은 3개 함수로만 제한:
      _analyze_dart_with_gemini()
      _analyze_closing_with_gemini()
      _enhance_geopolitics_with_gemini()
    다른 내부 함수에서 _call_gemini() 직접 호출 추가 금지
#8  morning_analyzer에서 텔레그램 발송 금지
    morning_analyzer에서 DB 기록 금지
    morning_analyzer에서 KIS API 직접 호출 금지
#9  신호1~8 생성 로직은 data_collector._build_signals() 담당
    morning_analyzer에서 신호 생성 로직 구현 금지 (prebuilt_signals 수신만)
#10 _pick_stocks() 실패 시 None 반환 (비치명적)
    oracle_result None이어도 전체 리포트 발송 차단 금지 (morning_report 흐름 계속)
```

## [장중봇 — intraday_analyzer]

```
#11 intraday_analyzer에서 AI 판단 로직 추가 금지
    등락률·거래량 숫자 조건 필터만 (진짜급등/작전주의심 판단 제거된 상태 유지)
#12 장중(09:00~15:30) pykrx 호출 금지 — 15~20분 지연 (일별 확정치 전용)
#13 [v12.0 갱신] 마감강도·거래량급증·자금집중은 collectors/ 전담
    data_collector가 06:00 일괄 수집, 장중 별도 수집 금지
#14 T2(갭상승): 장중봇 전용 — KIS REST 실시간만
```

## [KIS API]

```
#16 rate_limiter.acquire()는 kis/rest_client.py 내부에서만 호출 (외부 중복 호출 금지)
```

## [종목명·설정]

```
#17 config.py에 종목명 직접 하드코딩 금지 (업종명 키워드만)
#18 대장주는 data_collector._build_signals()가 by_sector에서 동적 결정
#19 반환값 key 변경 시 의존성 확인 후 연결 파일 동시 수정
#20 config.py 변수명 변경 시 전체 영향 주의
```

## [자동매매]

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
```

## [AI 모델 — Gemini]

```
#40 /evaluate 대화 타임아웃 EVALUATE_CONV_TIMEOUT_SEC(기본 120초) 반드시 적용
#41 Gemini 호출은 반드시 try/except 래핑 — 실패 시 빈 목록/None 반환 (비치명적)
    Gemini API 장애로 morning_analyzer 전체 중단 금지
```

## [tracking/DB]

```
#42 ai_context.py는 DB 조회 + 문자열 반환만 (AI API 호출·발송·매수 로직 금지)
    모든 함수는 동기(sync) — realtime_alert에서 run_in_executor 경유
#43 principles_extractor.py → main.py 일요일 03:00 cron에서만 호출
#44 trading_journal.py: record_journal() → position_manager.close_position() 에서만 호출
#45 get_journal_context() → ai_context.py 에서만 호출 (realtime_alert 직접 호출 금지)
#46 get_weekly_patterns() → weekly_report.py 에서만 호출 (30일 기준 집계)
#47 _integrate_journal_patterns() → principles_extractor.run_weekly_extraction() 내부에서만
#48 trading_journal 테이블: position_manager만 INSERT, 다른 모듈은 SELECT 전용
#49 get_journal_context() 토큰 제한 필수: JOURNAL_MAX_ITEMS / JOURNAL_MAX_CONTEXT_CHARS
    무제한 전체 조회 금지 (장기 운영 토큰 증가 방지)
#50 kospi_index_stats 테이블: memory_compressor.update_index_stats()만 UPSERT
    외부 직접 INSERT/UPDATE 금지
#51 run_memory_compression() → main.py 일요일 03:30에서만 호출 (중복 실행 방지)
#52 DB 파일 경로는 config.DB_PATH 단일 상수로 관리 (하드코딩 금지)
#53 [v12.0 갱신] record_alert()는 trading_journal.py에 통합. alert_recorder.py 삭제됨
    trading_journal.record_alert() → realtime_alert._dispatch_alerts() 에서만 호출
    예외: oracle_recorder.record_oracle_pick()은 morning_report 발송 후 별도 호출 허용
#54 performance_tracker.run_batch() → main.py 15:45 cron에서만 호출
    (장중 직접 호출 금지 — pykrx 당일 미확정 데이터 방지)
    [v12.0: 18:45→15:45로 변경됨]
#55 accuracy_tracker.py에 AI 호출·발송·KIS 호출·수집 로직 금지
    저장 실패 시 logger.warning + 무시 (아침봇 blocking 금지)
```

## [오라클·신호 라우팅]

```
#56 [v12.0 갱신] _pick_stocks()는 morning_analyzer.analyze() 내부에서만 호출
    morning_report는 morning_analyzer.analyze() 단일 호출만
#57 [v12.0 갱신] closing_strength_result / volume_surge_result / fund_concentration_result
    키명은 data_collector 캐시, morning_analyzer 파라미터, morning_report.run() 파라미터
    3곳 모두 동일하게 유지 (한 곳 변경 시 3파일 동시 수정 — #19 확장)
```

## [신규 — v12.0 아키텍처 고유 규칙]

```
#58 data_collector.run()은 main.py 06:00 스케줄에서만 호출
    morning_report.py, realtime_alert.py에서 직접 run() 호출 금지
    캐시 접근은 반드시 get_cache() / is_fresh() 경유

#59 morning_analyzer.analyze()에서 AI(Gemini) 호출은
    _analyze_dart_with_gemini() / _analyze_closing_with_gemini() /
    _enhance_geopolitics_with_gemini() 세 함수로만 제한
    다른 내부 함수(_analyze_theme, _pick_stocks 등)에서 _call_gemini() 직접 호출 금지

#60 data_collector에서 AI API 호출 금지 (수집·캐싱·점수화·신호생성만)
    _build_signals()에 Gemini 호출 추가 시 이 규칙 위반

#61 intraday_analyzer는 AI 판단 없이 숫자 조건만으로 동작해야 함
    analyze_spike() 류의 AI 판단 재도입 금지
    호가 분석(analyze_orderbook)은 수학적 비율 계산이므로 허용

#62 morning_report.run()은 반드시 morning_analyzer 단일 호출 구조 유지
    geopolitics_analyzer, theme_analyzer, oracle_analyzer,
    sector_flow_analyzer, event_impact_analyzer를 직접 import·호출 금지
    (모두 morning_analyzer 내부 함수로 통합됨)
```
