"""
analyzers/theme_analyzer.py
테마 그룹핑 + 순환매 소외도 계산 전담
- signal_analyzer 결과를 받아 테마별로 정리
- 대장주/소외주 분류, 소외도 계산
- 수집/발송 로직 없음
"""

from utils.logger import logger


def analyze(signal_result: dict, price_data: dict = None) -> dict:
    """
    테마 분석 + 순환매 지도 생성
    반환: dict
    {
        "theme_map": [
            {
                "테마명": str,
                "대장주": str,
                "대장등락률": float,
                "종목들": [
                    {
                        "종목명":   str,
                        "등락률":   float or "N/A",
                        "소외도":   float or "N/A",
                        "포지션":   str,   # "이미과열", "오늘★", "내일", "모니터"
                    }
                ],
                "상태": str,
            }
        ],
        "volatility": str,
        "top_signals": list,   # 강도 상위 3개 신호
    }
    """
    signals   = signal_result.get("signals", [])
    volatility = signal_result.get("volatility", "판단불가")

    logger.info(f"[theme] 테마 분석 시작 — 신호 {len(signals)}개")

    # 신호를 테마로 그룹핑
    theme_map = _build_theme_map(signals, price_data)

    return {
        "theme_map":   theme_map,
        "volatility":  volatility,
        "top_signals": signals[:3],
    }


def _build_theme_map(signals: list[dict], price_data: dict) -> list[dict]:
    """신호 리스트 -> 테마맵 변환"""
    themes = []

    for signal in signals:
        관련종목 = signal.get("관련종목", [])
        if not 관련종목:
            continue

        종목들 = []
        대장등락률 = None

        for i, 종목명 in enumerate(관련종목):
            등락률 = _get_price_change(종목명, price_data)

            if i == 0:
                대장등락률 = 등락률
                포지션 = "이미과열" if (isinstance(등락률, float) and 등락률 >= 15) else "대장"
            else:
                if isinstance(등락률, float) and isinstance(대장등락률, float):
                    소외도 = round(대장등락률 - 등락률, 1)
                    포지션 = _judge_position(등락률, 소외도)
                else:
                    소외도 = "N/A"
                    포지션 = "모니터"

                종목들.append({
                    "종목명": 종목명,
                    "등락률": 등락률,
                    "소외도": 소외도 if i > 0 else "—",
                    "포지션": 포지션,
                })

        themes.append({
            "테마명":     signal["테마명"],
            "대장주":     관련종목[0] if 관련종목 else "N/A",
            "대장등락률": 대장등락률 if 대장등락률 else "N/A",
            "종목들":     종목들,
            "상태":       signal["상태"],
            "발화신호":   signal["발화신호"],
        })

    return themes


def _get_price_change(stock_name: str, price_data: dict) -> float | str:
    """종목명으로 등락률 조회 (price_data 없으면 N/A)"""
    if not price_data:
        return "N/A"
    return price_data.get(stock_name, {}).get("등락률", "N/A")


def _judge_position(등락률: float, 소외도: float) -> str:
    """소외도 기준 포지션 분류"""
    if 소외도 >= 20:
        return "오늘★"
    elif 소외도 >= 10:
        return "내일"
    else:
        return "모니터"
