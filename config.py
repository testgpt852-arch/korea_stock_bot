"""
config.py — 모든 설정값 중앙 관리
API 키는 절대 이 파일에 직접 입력하지 않는다.
Railway: 서버 Variables에 입력
로컬:    .env 파일에 입력 (git 업로드 금지)

[수정이력]
- v2.1: DART 규모 필터 상수 추가 (DART_DIVIDEND_MIN_RATE, DART_ORDER_MIN_RATIO)
        미국증시 섹터 연동 매핑 추가 (US_SECTOR_TICKERS, US_SECTOR_KR_MAP)
- v2.2: 하드코딩 비상장 종목 수정
          LS전선(비상장) → 대한전선·대원전선·가온전선·LS전선아시아
          GS칼텍스(비상장) → GS
        COPPER_KR_STOCKS 상수 신규 추가 (signal_analyzer 참조)
        US_SECTOR_SIGNAL_MIN 1.5% → 1.0% (신호 감도 향상)
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


# ── 장중봇 급등 감지 임계값 ──────────────────────────────────
VOLUME_SPIKE_RATIO = 10
PRICE_CHANGE_MIN   = 3.0
CONFIRM_CANDLES    = 2
MARKET_CAP_MIN     = 30_000_000_000

# ── 중복 알림 방지 ───────────────────────────────────────────
ALERT_COOLTIME_MIN = 30

# ── KIS WebSocket ────────────────────────────────────────────
WS_MAX_RECONNECT   = 3
WS_RECONNECT_DELAY = 30

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

# ── DART 규모 필터 (v2.1 추가) ───────────────────────────────
# 배당결정: alotMatter.json dvdnYld (시가배당률) 이 값 미만이면 제외
DART_DIVIDEND_MIN_RATE    = 3      # % — 시가배당률 3% 미만 배당은 제외

# 단일판매공급계약·수주: piicDecsn.json 기준
# selfCptlRatio (자기자본대비%) 우선, 없으면 slCtrctAmt (계약금액) 사용
DART_CONTRACT_MIN_RATIO   = 10     # % — 자기자본대비 10% 미만 계약은 제외
DART_CONTRACT_MIN_BILLION = 50     # 억원 — 금액 기준 fallback: 50억 미만 제외

# ── 수급 데이터 조회 기간 ────────────────────────────────────
INSTITUTION_DAYS = 5

# ── 원자재 연동 국내 종목 (v2.2 신규) ────────────────────────
# 구리 강세 시 순환매 기대 종목 — 상장사만 포함
# ※ LS전선은 비상장 → 제거. 대한전선·LS전선아시아 로 교체
# theme_analyzer의 price_data["by_name"] 키와 정확히 일치해야 소외도 계산 가능
COPPER_KR_STOCKS = ["대한전선", "대원전선", "가온전선", "LS전선아시아"]

# ── 미국증시 섹터 ETF → 국내 연동 테마 매핑 (v2.1 추가) ──────
# yfinance로 섹터 ETF 등락률 수집 → 국내 테마 신호로 변환
US_SECTOR_TICKERS = {
    "XLK":  "기술/반도체",    # Technology
    "XLE":  "에너지/정유",    # Energy
    "XLB":  "소재/화학",      # Materials
    "XLI":  "산업재/방산",    # Industrials
    "XLV":  "바이오/헬스케어",# Health Care
    "XLF":  "금융",           # Financials
}

# 국내 연동 종목 힌트 (signal_analyzer에서 관련종목 제안용)
# ※ theme_analyzer가 price_data["by_name"]에서 조회하므로 상장사 정확명 필수
# v2.2: GS칼텍스(비상장) → GS 로 수정
US_SECTOR_KR_MAP = {
    "기술/반도체":    ["삼성전자", "SK하이닉스", "한미반도체"],
    "에너지/정유":    ["SK이노베이션", "S-Oil", "GS"],
    "소재/화학":      ["LG화학", "롯데케미칼", "금호석유"],
    "산업재/방산":    ["한화에어로스페이스", "현대로템", "LIG넥스원"],
    "바이오/헬스케어":["삼성바이오로직스", "셀트리온", "유한양행"],
    "금융":           ["KB금융", "신한지주", "하나금융지주"],
}

# 섹터 신호 발생 임계값 (등락률 절댓값 이상일 때만 신호 발생)
# v2.2: 1.5% → 1.0% (XLK +1.3% 수준의 의미 있는 움직임도 포착)
US_SECTOR_SIGNAL_MIN = 1.0      # % — 1.0% 이상 변동 시만 신호