"""
utils/watchlist_state.py
아침봇 → 장중봇 간 WebSocket 워치리스트 + 시장 환경 공유 상태 (v3.1 신규 / v4.2 확장)

[역할]
morning_report(08:30) 가 전날 수급·신호 데이터로 워치리스트를 생성해 저장.
realtime_alert(09:00) 시작 시 이 워치리스트로 WebSocket 구독 목록을 구성.

[v4.2 추가] 시장 환경 저장/조회 기능
morning_report 가 price_data(kospi)로 시장 환경을 판단해 저장.
realtime_alert 가 읽어 analyze_spike() market_env 파라미터와
position_manager.can_buy() R/R 필터에 활용.

[판단 기준 — _determine_market_env()]
  전날 KOSPI 등락률 기준 (단순 당일 기준, pykrx 확정치 사용):
  +1.0% 이상 → "강세장"   (R/R 1.2+ 기준)
  -1.0% 이하 → "약세장/횡보"  (R/R 2.0+ 기준)
  그 외      → "횡보"       (R/R 1.5+ 기준)

[ARCHITECTURE 의존성]
morning_report  → watchlist_state (write: set_watchlist, set_market_env)
realtime_alert  → watchlist_state (read: get_watchlist, get_market_env)
수집/분석/발송 로직 없음 — 상태 공유만
"""

from utils.logger import logger

# {종목코드: {"종목명": str, "전일거래량": int, "우선순위": int}}
_watchlist: dict[str, dict] = {}

# [v4.2] 시장 환경 — "강세장" / "약세장/횡보" / "횡보" / ""
_market_env: str = ""


# ── 워치리스트 ────────────────────────────────────────────────

def set_watchlist(stocks: dict[str, dict]) -> None:
    """아침봇에서 호출 — 워치리스트 저장 (장중봇 시작 전에 완료돼야 함)"""
    global _watchlist
    _watchlist = stocks
    logger.info(f"[watchlist] 워치리스트 저장 완료 — {len(_watchlist)}종목")


def get_watchlist() -> dict[str, dict]:
    """장중봇에서 호출 — 워치리스트 복사본 반환"""
    return _watchlist.copy()


def is_ready() -> bool:
    """아침봇이 워치리스트를 설정했는지 여부 (장중봇 시작 전 체크용)"""
    return bool(_watchlist)


def clear() -> None:
    """장 마감 후 초기화 (다음날 아침봇이 덮어쓰므로 선택적)"""
    global _watchlist, _market_env
    _watchlist  = {}
    _market_env = ""
    logger.info("[watchlist] 워치리스트 + 시장 환경 초기화")


# ── [v4.2] 시장 환경 ──────────────────────────────────────────

def set_market_env(env: str) -> None:
    """
    [v4.2] 아침봇에서 호출 — 시장 환경 저장.
    morning_report.py 가 price_data(kospi 등락률)를 기반으로 판단 후 저장.

    Args:
        env: "강세장" / "약세장/횡보" / "횡보" / ""
    """
    global _market_env
    _market_env = env
    logger.info(f"[watchlist] 시장 환경 저장: {env or '(미지정)'}")


def get_market_env() -> str:
    """
    [v4.2] 장중봇에서 호출 — 시장 환경 반환.
    realtime_alert._send_ai_followup() 에서 사용.

    Returns:
        "강세장" / "약세장/횡보" / "횡보" / "" (아침봇 미실행 시 빈 문자열)
    """
    return _market_env


def determine_and_set_market_env(price_data: dict | None) -> str:
    """
    [v4.2] 아침봇 호출용 — price_data(kospi)로 시장 환경 판단 + 저장.
    morning_report.py 에서 set_watchlist() 직후 호출.

    판단 기준: 전날 KOSPI 등락률 (pykrx 확정치, 단일 기준)
      +1.0% 이상 → 강세장
      -1.0% 이하 → 약세장/횡보
      그 외      → 횡보

    향후 개선 시 20일선 위치, 2주 누적 수익률 등 복합 기준 추가 가능.

    Args:
        price_data: price_collector.collect_daily() 반환값

    Returns:
        결정된 시장 환경 문자열
    """
    if not price_data:
        set_market_env("")
        return ""

    kospi       = price_data.get("kospi", {})
    change_rate = kospi.get("change_rate", 0) or 0  # 전날 KOSPI 등락률 (%)

    if change_rate >= 1.0:
        env = "강세장"
    elif change_rate <= -1.0:
        env = "약세장/횡보"
    else:
        env = "횡보"

    set_market_env(env)
    logger.info(
        f"[watchlist] 시장 환경 판단: KOSPI {change_rate:+.2f}% → {env}"
    )
    return env
