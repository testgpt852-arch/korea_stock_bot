"""
config.py — 모든 설정값 중앙 관리
API 키는 절대 이 파일에 직접 입력하지 않는다.
Railway: 서버 Variables에 입력
로컬:    .env 파일에 입력 (git 업로드 금지)

[수정이력]
- v2.1: DART 규모 필터 상수 추가
        미국증시 섹터 연동 매핑 추가 (US_SECTOR_TICKERS, US_SECTOR_KR_MAP)
- v2.2: 비상장사 제거, COPPER_KR_STOCKS 추가, US_SECTOR_SIGNAL_MIN 1.5→1.0
- v2.3: 종목명 하드코딩 전면 제거
        US_SECTOR_KR_MAP (종목명 고정) → US_SECTOR_KR_INDUSTRY (업종명 키워드) 로 교체
        COMMODITY_KR_INDUSTRY 신규 추가
        → signal_analyzer가 price_data["by_sector"]에서 실제 등락률 상위 종목을 동적으로 조회
        ※ 이 파일에는 업종명 키워드만 관리. 종목명은 절대 직접 쓰지 않는다.
- v2.4: POLL_INTERVAL_SEC 추가 — 장중봇 REST 폴링 방식 전환 (전 종목 커버)
- v3.4: Phase 4 — 자동매매(모의투자) 설정 추가
        TRADING_MODE, AUTO_TRADE_ENABLED
        KIS VTS 인증 (실전과 앱키 분리)
        포지션 관리 상수 (POSITION_MAX, BUY_AMOUNT, TAKE_PROFIT, STOP_LOSS 등)
- v4.0: [소~중형주 필터 + 호가 분석]
        MARKET_CAP_MAX 신규 — 소~중형주 시총 상한 (volume_ranking 코스피 필터용)
        VOLUME_RANK_KOSPI_SIZE_CLS 신규 — 거래량순위 코스피 규모 필터 코드 목록
        ORDERBOOK_BID_ASK_MIN/MAX — 호가 매수/매도 잔량 비율 임계값
        ORDERBOOK_TOP3_RATIO_MIN — 상위 3호가 집중도 (얕은 벽 감지)
        ORDERBOOK_ENABLED — 호가 분석 활성화 여부 (기본 True)
        WS_ORDERBOOK_ENABLED — WebSocket H0STASP0 호가 구독 여부 (기본 False, 체결과 한도 공유)
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── API 키 (환경변수에서만 읽음) ──────────────────────────────
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID")
DART_API_KEY      = os.environ.get("DART_API_KEY")
KIS_APP_KEY       = os.environ.get("KIS_APP_KEY")
KIS_APP_SECRET    = os.environ.get("KIS_APP_SECRET")
KIS_ACCOUNT_NO    = os.environ.get("KIS_ACCOUNT_NO")
KIS_ACCOUNT_CODE  = os.environ.get("KIS_ACCOUNT_CODE", "01")

# Google AI API (ai_analyzer — Gemma-3-27b-it)
GOOGLE_AI_API_KEY = os.environ.get("GOOGLE_AI_API_KEY")

# 네이버 검색 API
NAVER_CLIENT_ID     = os.environ.get("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET")


# ── 시작 시 키 누락 여부 체크 ─────────────────────────────────
def validate_env():
    required = {
        "TELEGRAM_TOKEN":   TELEGRAM_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "DART_API_KEY":     DART_API_KEY,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise EnvironmentError(f"[config] 필수 환경변수 누락: {missing}")

    if not GOOGLE_AI_API_KEY:
        print("[config] GOOGLE_AI_API_KEY 없음 — AI 분석 비활성 (aistudio.google.com 무료 발급)")

    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        print("[config] 네이버 API 미설정 — 리포트·뉴스 수집 비활성 (developers.naver.com 무료 발급)")

    if not KIS_APP_KEY or not KIS_APP_SECRET or not KIS_ACCOUNT_NO:
        print(f"[config] KIS 미설정 — 장중봇 비활성 (4단계)")

    if AUTO_TRADE_ENABLED and TRADING_MODE == "VTS":
        if not KIS_VTS_APP_KEY or not KIS_VTS_APP_SECRET or not KIS_VTS_ACCOUNT_NO:
            print("[config] ⚠️  AUTO_TRADE_ENABLED=true이지만 KIS_VTS_* 환경변수 미설정"
                  " — 자동매매 비활성화됨")

    if AUTO_TRADE_ENABLED and TRADING_MODE == "REAL":
        print("[config] ⚠️  TRADING_MODE=REAL: 실전 자동매매 활성화됨! 신중하게 운영할 것.")


# ── 장중봇 급등 감지 임계값 (v2.8: 델타 기준으로 전환) ──────────
PRICE_DELTA_MIN    = 1.5     # 1분간 최소 추가 등락률 (%) — 핵심 조건 (v3.7: 1.0→1.5 강화)
VOLUME_DELTA_MIN   = 10      # 1분간 최소 추가 거래량 (전일 거래량 대비 %) (v3.7: 5→10 강화)
CONFIRM_CANDLES    = 2       # 연속 충족 폴링 횟수 (허위신호 방지) (v3.7: 1→2 강화)
MARKET_CAP_MIN     = 30_000_000_000   # 시총 하한: 300억 (극소형 제외)
# [v4.1 버그수정] MARKET_CAP_MAX: FID_BLNG_CLS_CODE가 volume-rank API에서 실제 미동작
# → hts_avls(KIS 응답 필드, 억원 단위) 기반 사후 필터링으로 대체
# 10조 = 100,000억: 삼성전자(~350조), LG전자(~18조), 카카오(~20조) 모두 제외
MARKET_CAP_MAX     = 100_000            # 억원 단위 (hts_avls 기준) — 10조 이상 제외

# [v4.0 신규] 소~중형주 시총 상한 — 대형주 거래량 순위에서 제외
# KIS FID_BLNG_CLS_CODE 분류 기준 (코스피):
#   1=대형(시총 상위 1~100위), 2=중형(101~300위), 3=소형(301위~)
# volume_ranking 코스피 호출 시 중형(2) + 소형(3)만 조회 → 대형주 제외
# 코스닥은 시장 특성상 소/중 분리 없이 전체 조회 (이미 중소형 위주)
VOLUME_RANK_KOSPI_SIZE_CLS = ["2", "3"]   # 중형 + 소형만 (대형 제외)

# [v2.9+ 버그픽스] 전일거래량이 너무 적은 종목은 배율이 수십만배로 폭발함
MIN_PREV_VOL       = 50_000  # 전일 최소 거래량 (주) — 5만주 미만 스킵

# [v3.7 노이즈 필터]
MIN_TRADE_AMOUNT   = 3_000_000_000   # 최소 장중 누적 거래대금 (30억원) — 쪽정이 제거
MIN_CHANGE_RATE    = 3.0             # 누적 등락률 하한 (%) — 이 미만은 급등 아님 (v3.8: 2.0→3.0)
MAX_CATCH_RATE     = 12.0            # [v3.8 신규] 누적 등락률 상한 (%) — 초과 시 이미 급등 끝 (뒷북 방지)
MAX_ALERTS_PER_CYCLE = 5            # 폴링 사이클당 최대 알림 발송 수 — 폭탄 알림 방지

# [v3.8 신규] 누적 RVOL 필터 — 진짜 주도주 판별
MIN_VOL_RATIO_ACML = 30.0            # 누적 RVOL 최솟값 (%): 전일 거래량의 30% 이상 소화

# v3.1: PRICE_CHANGE_MIN — WebSocket 틱 기반 감지 임계값으로 재활성
VOLUME_SPIKE_RATIO = 10      # deprecated (v2.8): 누적 거래량 배율 — REST 미사용
PRICE_CHANGE_MIN   = 3.0     # WebSocket 감지 임계값 (누적 등락률 %, v3.1 재활성)

# ── 장중봇 REST 폴링 간격 (v2.4 추가) ───────────────────────
POLL_INTERVAL_SEC  = 10      # v3.1: 30→10초 단축 (방법A 개선)

# ── 중복 알림 방지 ───────────────────────────────────────────
ALERT_COOLTIME_MIN = 30

# ── WebSocket 워치리스트 (v3.1 추가 / v3.2 수정) ────────────
# KIS 공식 스펙: WebSocket 동시 구독 한도 40개 (python-kis WEBSOCKET_MAX_SUBSCRIPTIONS=40 확인)
WS_WATCHLIST_MAX   = 40   # v3.2: 50 → 40 (KIS 스펙 준수)

# [v4.0 신규] WebSocket 호가(H0STASP0) 구독 여부
# True 설정 시: 체결(H0STCNT0) 20종목 + 호가(H0STASP0) 20종목 → 합계 40 (한도 준수)
# False(기본): 체결 40종목 전체 구독 (기존 동작 유지)
# ※ True 설정 시 ws_watchlist 상위 20종목만 체결 구독됨 — 커버리지 감소 주의
WS_ORDERBOOK_ENABLED = os.environ.get("WS_ORDERBOOK_ENABLED", "false").lower() == "true"
WS_ORDERBOOK_SLOTS   = 20   # WS_ORDERBOOK_ENABLED=true 시 호가 구독 종목 수 (최대)

# ── [v4.0 신규] REST 호가 분석 설정 ─────────────────────────
# 급등 감지 후 KIS REST 호가 API(FHKST01010200)로 호가잔량 분석
# → 매수벽 강도 확인 후 알림 신뢰도 점수 부여
ORDERBOOK_ENABLED      = True    # REST 호가 분석 활성화 (False → 호가 조회 생략)
ORDERBOOK_BID_ASK_MIN  = 1.3    # 매수잔량/매도잔량 비율 하한 — 이상이면 매수 우세 (강세 신호)
ORDERBOOK_BID_ASK_GOOD = 2.0    # 매수잔량/매도잔량 비율 — 이상이면 강한 매수 압력
ORDERBOOK_TOP3_RATIO_MIN = 0.4  # 상위3호가 잔량 / 전체 잔량 — 이하면 벽이 두꺼워 돌파 어려움

# ── KIS WebSocket 재연결 (v3.2: 무한재연결 방식으로 변경) ─────
WS_RECONNECT_DELAY = 5    # v3.2: 30초 → 5초 (python-kis reconnect_interval=5 참조)

# ── KIS API Rate Limiter (v3.2: python-kis 참조 추가) ─────────
KIS_RATE_LIMIT_REAL    = 19   # 초당 최대 호출 횟수 (실전)
KIS_RATE_LIMIT_VIRTUAL = 2    # 초당 최대 호출 횟수 (모의)

# ── Phase 2 트리거 임계값 (v3.2 신규) ─────────────────────────
GAP_UP_MIN         = 2.5    # 갭업 최소 비율 (%) (v3.7: 1.0→2.5 강화)
CLOSING_STRENGTH_MIN  = 0.75
CLOSING_STRENGTH_TOP_N = 7
VOLUME_FLAT_CHANGE_MAX = 5.0
VOLUME_FLAT_SURGE_MIN  = 50.0
VOLUME_FLAT_TOP_N      = 7
FUND_INFLOW_CAP_MIN    = 100_000_000_000  # 최소 시가총액 (1000억원)
FUND_INFLOW_TOP_N      = 7

# ── Phase 3: SQLite DB 경로 (v3.3 신규) ─────────────────────
DB_PATH = os.environ.get("DB_PATH", "/data/bot_db.sqlite")

# ── Phase 4: 자동매매(모의투자) 설정 (v3.4 신규) ─────────────
TRADING_MODE = os.environ.get("TRADING_MODE", "VTS")
AUTO_TRADE_ENABLED = os.environ.get("AUTO_TRADE_ENABLED", "false").lower() == "true"

KIS_VTS_APP_KEY     = os.environ.get("KIS_VTS_APP_KEY",    os.environ.get("KIS_APP_KEY"))
KIS_VTS_APP_SECRET  = os.environ.get("KIS_VTS_APP_SECRET", os.environ.get("KIS_APP_SECRET"))
KIS_VTS_ACCOUNT_NO  = os.environ.get("KIS_VTS_ACCOUNT_NO", os.environ.get("KIS_ACCOUNT_NO"))
KIS_VTS_ACCOUNT_CODE = os.environ.get("KIS_VTS_ACCOUNT_CODE", "01")

POSITION_MAX        = int(os.environ.get("POSITION_MAX", "3"))
POSITION_BUY_AMOUNT = int(os.environ.get("POSITION_BUY_AMOUNT", "1000000"))

TAKE_PROFIT_1   = 5.0
TAKE_PROFIT_2   = 10.0
STOP_LOSS       = -3.0
DAILY_LOSS_LIMIT = -3.0

MIN_ENTRY_CHANGE = 3.0
MAX_ENTRY_CHANGE = 10.0

FORCE_CLOSE_TIME = "14:50"

# ── 스케줄 시간 ──────────────────────────────────────────────
TOKEN_REFRESH_TIME = "07:00"
MORNING_TIME       = "08:30"
MARKET_OPEN_TIME   = "09:00"
MARKET_CLOSE_TIME  = "15:30"
CLOSING_TIME       = "18:30"

# ── DART 공시 필터 키워드 ────────────────────────────────────
DART_KEYWORDS = [
    "수주",
    "배당결정",
    "자사주취득결정",
    "MOU",
    "단일판매공급계약체결",
    "특허",
    "판결",
    "주요주주",
]

# ── DART 규모 필터 ───────────────────────────────────────────
DART_DIVIDEND_MIN_RATE    = 3
DART_CONTRACT_MIN_RATIO   = 10
DART_CONTRACT_MIN_BILLION = 50

# ── 수급 데이터 조회 기간 ────────────────────────────────────
INSTITUTION_DAYS = 5

# ══════════════════════════════════════════════════════════════
# 미국 섹터/원자재 → 국내 업종 연동 매핑 (v2.3 전면 개편)
# ══════════════════════════════════════════════════════════════

US_SECTOR_TICKERS = {
    "XLK":  "기술/반도체",
    "XLE":  "에너지/정유",
    "XLB":  "소재/화학",
    "XLI":  "산업재/방산",
    "XLV":  "바이오/헬스케어",
    "XLF":  "금융",
}

US_SECTOR_KR_INDUSTRY = {
    "기술/반도체":    ["반도체", "IT하드웨어", "전자장비"],
    "에너지/정유":    ["에너지", "정유"],
    "소재/화학":      ["화학", "정밀화학"],
    "산업재/방산":    ["항공", "방산", "기계"],
    "바이오/헬스케어":["제약", "바이오", "의료"],
    "금융":           ["은행", "증권", "보험"],
}

COMMODITY_KR_INDUSTRY = {
    "copper": ["전기/전선", "전선", "전기장비"],
    "silver": ["귀금속", "비철금속", "태양광"],
    "gas":    ["가스", "에너지"],
}

SECTOR_TOP_N         = 5
US_SECTOR_SIGNAL_MIN = 1.0
