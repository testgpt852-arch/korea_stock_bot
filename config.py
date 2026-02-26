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
# [v2.8 핵심 변경]
# 기존: 전일종가 대비 누적 등락률/거래량 조건 → 재시작 시 이미 올라간 종목 폭탄 감지
# 변경: 직전 poll 대비 1분간 변화량(델타) 조건 → "지금 막 터지는" 종목만 포착
PRICE_DELTA_MIN    = 1.5     # 1분간 최소 추가 등락률 (%) — 핵심 조건 (v3.7: 1.0→1.5 강화)
VOLUME_DELTA_MIN   = 10      # 1분간 최소 추가 거래량 (전일 거래량 대비 %) (v3.7: 5→10 강화)
CONFIRM_CANDLES    = 2       # 연속 충족 폴링 횟수 (허위신호 방지) (v3.7: 1→2 강화)
MARKET_CAP_MIN     = 30_000_000_000
# [v2.9+ 버그픽스] 전일거래량이 너무 적은 종목은 배율이 수십만배로 폭발함
# 전일거래량 최솟값: 이 미만 종목은 거래량 배율 계산 대상에서 제외
MIN_PREV_VOL       = 50_000  # 전일 최소 거래량 (주) — 5만주 미만 스킵

# [v3.7 노이즈 필터 — prism-insight 참조]
# prism: 거래대금 100억 이상 + 상승 중인 종목만 감지
# 장중 누적 거래대금 = 누적거래량 × 현재가 (근사값)
MIN_TRADE_AMOUNT   = 3_000_000_000   # 최소 장중 누적 거래대금 (30억원) — 쪽정이 제거
MIN_CHANGE_RATE    = 2.0             # 누적 등락률 최솟값 (%) — 이 미만은 급등 아님
MAX_ALERTS_PER_CYCLE = 5            # 폴링 사이클당 최대 알림 발송 수 — 폭탄 알림 방지

# v3.1: PRICE_CHANGE_MIN — WebSocket 틱 기반 감지 임계값으로 재활성
# (방법B: 워치리스트 종목의 누적 등락률이 이 값 이상이면 WS 알림)
VOLUME_SPIKE_RATIO = 10      # deprecated (v2.8): 누적 거래량 배율 — REST 미사용
PRICE_CHANGE_MIN   = 3.0     # WebSocket 감지 임계값 (누적 등락률 %, v3.1 재활성)

# ── 장중봇 REST 폴링 간격 (v2.4 추가) ───────────────────────
# pykrx REST 전 종목 조회 주기 (초)
# 60초 × CONFIRM_CANDLES(2) = 조건 충족 후 최대 2분 내 알림
# KIS WebSocket 미사용 → 차단 위험 없음, 전 종목(코스피+코스닥) 커버
POLL_INTERVAL_SEC  = 10      # v3.1: 30→10초 단축 (방법A 개선)

# ── 중복 알림 방지 ───────────────────────────────────────────
ALERT_COOLTIME_MIN = 30

# ── WebSocket 워치리스트 (v3.1 추가 / v3.2 수정) ────────────
# KIS 공식 스펙: WebSocket 동시 구독 한도 40개 (python-kis WEBSOCKET_MAX_SUBSCRIPTIONS=40 확인)
# 기존 50 → 40 수정 (50 설정 시 KIS 서버가 40개 초과분 무시 또는 에러 반환)
WS_WATCHLIST_MAX   = 40   # v3.2: 50 → 40 (KIS 스펙 준수)

# ── KIS WebSocket 재연결 (v3.2: 무한재연결 방식으로 변경) ─────
# WS_MAX_RECONNECT 제한 없음 — 네트워크 복구될 때까지 계속 재시도
# Railway 서버 간헐적 네트워크 끊김 대응
WS_RECONNECT_DELAY = 5    # v3.2: 30초 → 5초 (python-kis reconnect_interval=5 참조)

# ── KIS API Rate Limiter (v3.2: python-kis 참조 추가) ─────────
# 실전: 초당 19회 / 모의: 초당 2회 (python-kis REAL_API_REQUEST_PER_SECOND = 20-1)
KIS_RATE_LIMIT_REAL    = 19   # 초당 최대 호출 횟수 (실전)
KIS_RATE_LIMIT_VIRTUAL = 2    # 초당 최대 호출 횟수 (모의)

# ── Phase 2 트리거 임계값 (v3.2 신규) ─────────────────────────
# T2: 갭 상승 모멘텀
GAP_UP_MIN         = 2.5    # 갭업 최소 비율 (%) — 시가/전일종가 기준 (v3.7: 1.0→2.5 강화)
# T5: 마감 강도
CLOSING_STRENGTH_MIN  = 0.75   # 마감 강도 최소값 (0~1, 1=고가=종가)
CLOSING_STRENGTH_TOP_N = 7     # 마감 강도 상위 N 종목
# T6: 횡보 거래량 급증
VOLUME_FLAT_CHANGE_MAX = 5.0   # 횡보 인정 등락률 절대값 상한 (%)
VOLUME_FLAT_SURGE_MIN  = 50.0  # 전일 대비 거래량 급증 최소 비율 (%)
VOLUME_FLAT_TOP_N      = 7     # 횡보 거래량 상위 N 종목
# T3: 시총 대비 자금 유입
FUND_INFLOW_CAP_MIN    = 100_000_000_000  # 최소 시가총액 (1000억원, 극소형주 제외)
FUND_INFLOW_TOP_N      = 7     # 시총 대비 자금유입 상위 N 종목

# ── Phase 3: SQLite DB 경로 (v3.3 신규) ─────────────────────
# Railway 배포 환경에서 재시작 시에도 유지되려면 /data 마운트 권장.
# Railway Volume 미사용 시 /tmp/bot_data (재시작 시 초기화 주의).
DB_PATH = os.environ.get("DB_PATH", "/data/bot_db.sqlite")

# ── Phase 4: 자동매매(모의투자) 설정 (v3.4 신규) ─────────────
# 매매 모드: "VTS"=모의투자 / "REAL"=실전 (REAL 사용 시 극도로 주의)
TRADING_MODE = os.environ.get("TRADING_MODE", "VTS")

# 자동매매 활성화 플래그 (기본 False — 명시적으로 "true" 설정 시에만 활성)
# Railway Variables: AUTO_TRADE_ENABLED=true (소문자 필수)
AUTO_TRADE_ENABLED = os.environ.get("AUTO_TRADE_ENABLED", "false").lower() == "true"

# VTS 모의투자 전용 KIS 앱키/계좌 (실전 키와 분리 권장)
# 미설정 시 실전 키(KIS_APP_KEY/SECRET)를 VTS 토큰 발급에도 사용
# (KIS 모의투자 앱키가 실전 앱키와 다른 경우 반드시 별도 설정)
KIS_VTS_APP_KEY     = os.environ.get("KIS_VTS_APP_KEY",    os.environ.get("KIS_APP_KEY"))
KIS_VTS_APP_SECRET  = os.environ.get("KIS_VTS_APP_SECRET", os.environ.get("KIS_APP_SECRET"))
KIS_VTS_ACCOUNT_NO  = os.environ.get("KIS_VTS_ACCOUNT_NO", os.environ.get("KIS_ACCOUNT_NO"))
KIS_VTS_ACCOUNT_CODE = os.environ.get("KIS_VTS_ACCOUNT_CODE", "01")

# 포지션 관리 파라미터
POSITION_MAX        = int(os.environ.get("POSITION_MAX", "3"))          # 동시 보유 한도 (종목 수)
POSITION_BUY_AMOUNT = int(os.environ.get("POSITION_BUY_AMOUNT", "1000000"))  # 1회 매수 금액 (원)

# 익절/손절 기준 (%)
TAKE_PROFIT_1   = 5.0    # 1차 익절 기준 — 도달 시 절반 매도 or 전량 매도
TAKE_PROFIT_2   = 10.0   # 2차 익절 기준 — 전량 매도
STOP_LOSS       = -3.0   # 손절 기준 — 전량 매도
DAILY_LOSS_LIMIT = -3.0  # 당일 누적 손실 한도 (%) — 초과 시 신규 매수 중단

# 매수 진입 등락률 범위 (이 범위를 벗어나면 매수 안 함)
MIN_ENTRY_CHANGE = 3.0   # 최소 등락률 (%) — 이하면 진입 신호 약함
MAX_ENTRY_CHANGE = 10.0  # 최대 등락률 (%) — 초과 시 이미 늦음, 추격 금지

# 강제 청산 시각 (HH:MM, KST) — 미청산 포지션 전부 시장가 매도
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
DART_DIVIDEND_MIN_RATE    = 3      # % — 시가배당률 3% 미만 배당은 제외
DART_CONTRACT_MIN_RATIO   = 10     # % — 자기자본대비 10% 미만 계약은 제외
DART_CONTRACT_MIN_BILLION = 50     # 억원 — 금액 기준 fallback

# ── 수급 데이터 조회 기간 ────────────────────────────────────
INSTITUTION_DAYS = 5

# ══════════════════════════════════════════════════════════════
# 미국 섹터/원자재 → 국내 업종 연동 매핑 (v2.3 전면 개편)
#
# ※ 핵심 설계 원칙:
#   종목명은 절대 이 파일에 쓰지 않는다.
#   업종명 키워드만 관리한다.
#   실제 대장주 결정은 signal_analyzer가
#   price_data["by_sector"]에서 동적으로 조회한다.
#
#   업종명 키워드는 pykrx get_market_sector_classifications()가
#   반환하는 "업종명" 컬럼 값의 부분 문자열이어야 한다.
#   → 첫 실행 후 [price] 업종분류 완료 로그에서 실제 업종명 확인 권장
# ══════════════════════════════════════════════════════════════

# ── 미국증시 섹터 ETF 티커 ────────────────────────────────────
US_SECTOR_TICKERS = {
    "XLK":  "기술/반도체",
    "XLE":  "에너지/정유",
    "XLB":  "소재/화학",
    "XLI":  "산업재/방산",
    "XLV":  "바이오/헬스케어",
    "XLF":  "금융",
}

# ── 미국 섹터 신호 → 국내 KRX 업종명 키워드 (v2.3: 종목명→업종명) ─
# signal_analyzer는 이 키워드로 price_data["by_sector"]를 검색한다.
# 키워드는 부분 일치(in) 방식이므로 짧고 명확하게 작성한다.
US_SECTOR_KR_INDUSTRY = {
    "기술/반도체":    ["반도체", "IT하드웨어", "전자장비"],
    "에너지/정유":    ["에너지", "정유"],
    "소재/화학":      ["화학", "정밀화학"],
    "산업재/방산":    ["항공", "방산", "기계"],
    "바이오/헬스케어":["제약", "바이오", "의료"],
    "금융":           ["은행", "증권", "보험"],
}

# ── 원자재 신호 → 국내 KRX 업종명 키워드 (v2.3 신규) ───────────
# signal_analyzer가 구리·은 강세 시 해당 업종 실제 등락률 상위 종목을 조회
COMMODITY_KR_INDUSTRY = {
    "copper": ["전기/전선", "전선", "전기장비"],
    "silver": ["귀금속", "비철금속", "태양광"],
    "gas":    ["가스", "에너지"],
}

# ── 섹터/원자재 신호 공통 설정 ───────────────────────────────
# 업종에서 관련종목으로 담을 최대 종목 수
SECTOR_TOP_N         = 5    # 업종 등락률 상위 N개 → 관련종목으로 사용

# 신호 발생 임계값
US_SECTOR_SIGNAL_MIN = 1.0  # % — 1.0% 이상 변동 시만 신호
