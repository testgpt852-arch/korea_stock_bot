"""
config.py — 모든 설정값 중앙 관리
API 키는 절대 이 파일에 직접 입력하지 않는다.
Railway: 서버 Variables에 입력
로컬:    .env 파일에 입력 (git 업로드 금지)
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
# 발급: https://aistudio.google.com → Get API Key → 무료
# 하루 14,400회 가능
GOOGLE_AI_API_KEY = os.environ.get("GOOGLE_AI_API_KEY")

# 네이버 검색 API (리포트·뉴스·시황 요약)
# 발급: https://developers.naver.com → Application 등록 → 검색 API → 무료
NAVER_CLIENT_ID     = os.environ.get("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET")


# ── 시작 시 키 누락 여부 체크 ─────────────────────────────────
def validate_env():
    # 필수 키
    required = {
        "TELEGRAM_TOKEN":   TELEGRAM_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "DART_API_KEY":     DART_API_KEY,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise EnvironmentError(f"[config] 필수 환경변수 누락: {missing}")

    # 선택 키 안내
    if not GOOGLE_AI_API_KEY:
        print("[config] GOOGLE_AI_API_KEY 없음 — AI 분석 비활성 (aistudio.google.com 무료 발급)")

    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        print("[config] 네이버 API 미설정 — 리포트·뉴스 수집 비활성 (developers.naver.com 무료 발급)")

    if not KIS_APP_KEY or not KIS_APP_SECRET or not KIS_ACCOUNT_NO:
        print(f"[config] KIS 미설정 — 장중봇 비활성 (4단계)")


# ── 장중봇 급등 감지 임계값 ──────────────────────────────────
VOLUME_SPIKE_RATIO = 10     # 전일 총거래량 대비 누적거래량 (%)
PRICE_CHANGE_MIN   = 3.0    # 급등 감지 최소 등락률 (%)
CONFIRM_CANDLES    = 2      # 연속 충족 횟수 (연속 2틱 확인)
MARKET_CAP_MIN     = 30_000_000_000   # 시총 최소 300억

# ── 중복 알림 방지 ───────────────────────────────────────────
ALERT_COOLTIME_MIN = 30     # 동일 종목 재알림 최소 간격 (분)

# ── KIS WebSocket ────────────────────────────────────────────
WS_MAX_RECONNECT   = 3      # 최대 재연결 횟수 (에러 시만)
WS_RECONNECT_DELAY = 30     # 재연결 대기 시간 (초)

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
    "주요주주",         # 내부자거래 감지
]

# ── 수급 데이터 조회 기간 ────────────────────────────────────
INSTITUTION_DAYS = 5        # 기관/외인 수급 조회 기간 (일)
