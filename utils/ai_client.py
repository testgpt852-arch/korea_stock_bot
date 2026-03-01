"""
utils/ai_client.py
Gemini 모델 폴백 유틸리티

모델 우선순위 (ARCHITECTURE.md §6):
  1순위(주력): gemini-3-flash-preview
  2순위(풀백): gemini-2.5-flash
  3순위(풀백): gemini-2.5-flash-lite
  4순위(풀백): gemma-3-27b-it

[사용법]
    from utils.ai_client import call_ai

    result = call_ai(client, prompt, max_tokens=1000, temperature=0.2)
    # 성공 시 str 반환, 전 모델 실패 시 RuntimeError 발생
"""

from utils.logger import logger

# ── 모델 우선순위 ────────────────────────────────────────────
AI_MODELS = [
    "gemini-3-flash-preview",   # 1순위 주력
    "gemini-2.5-flash",             # 2순위 풀백
    "gemini-2.5-flash-lite",        # 3순위 풀백
    "gemma-3-27b-it",              # 4순위 풀백
]


def call_ai(
    client,
    prompt: str,
    max_tokens: int = 1500,
    temperature: float = 0.2,
    caller: str = "",
) -> str:
    """
    AI 모델 폴백 호출.
    AI_MODELS 순서대로 시도하고, 성공한 첫 번째 결과를 반환.
    전 모델 실패 시 RuntimeError.

    Args:
        client:      google.genai.Client 인스턴스
        prompt:      프롬프트 문자열
        max_tokens:  최대 출력 토큰
        temperature: 온도 (기본 0.2)
        caller:      로그용 호출자 식별자 (예: "morning_analyzer")
    """
    if client is None:
        raise RuntimeError(f"[ai_client] {caller} AI 클라이언트 미초기화")

    try:
        from google.genai import types as _gtypes
    except ImportError as e:
        raise RuntimeError(f"[ai_client] google-genai 패키지 없음: {e}")

    last_exc: Exception | None = None

    for model in AI_MODELS:
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=_gtypes.GenerateContentConfig(
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                ),
            )
            text = response.text or ""
            logger.debug(f"[ai_client] {caller} 모델={model} 성공 ({len(text)}자)")
            return text
        except Exception as e:
            logger.warning(f"[ai_client] {caller} 모델={model} 실패: {e}")
            last_exc = e

    raise RuntimeError(
        f"[ai_client] {caller} 전 모델 실패. 마지막 오류: {last_exc}"
    )
