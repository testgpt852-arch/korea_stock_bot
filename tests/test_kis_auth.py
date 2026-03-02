"""
tests/test_kis_auth.py

kis/auth.py 단위 테스트
- setUp에서 _token_cache / _vts_token_cache 직접 초기화
- requests.post  → unittest.mock.patch("kis.auth.requests.post")
- config         → unittest.mock.patch("kis.auth.config")
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# 프로젝트 루트를 sys.path에 추가 (어느 디렉터리에서 실행해도 동작)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import kis.auth as auth


# ──────────────────────────────────────────────
# 공통 헬퍼
# ──────────────────────────────────────────────

def _make_post_response(token: str = "tok_abc") -> MagicMock:
    """requests.post 성공 응답 목 생성."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"access_token": token}
    return resp


def _reset_caches() -> None:
    """두 캐시를 모두 초기 상태(None)로 되돌린다."""
    auth._token_cache["access_token"] = None
    auth._token_cache["expires_at"] = None
    auth._vts_token_cache["access_token"] = None
    auth._vts_token_cache["expires_at"] = None


# ──────────────────────────────────────────────
# TestIsTokenValid
# ──────────────────────────────────────────────

class TestIsTokenValid(unittest.TestCase):

    def setUp(self) -> None:
        _reset_caches()

    def test_none_token_false(self):
        """access_token=None → False."""
        cache = {"access_token": None, "expires_at": datetime.now() + timedelta(hours=1)}
        self.assertFalse(auth._is_token_valid(cache))

    def test_none_expires_false(self):
        """expires_at=None → False."""
        cache = {"access_token": "tok", "expires_at": None}
        self.assertFalse(auth._is_token_valid(cache))

    def test_expired_false(self):
        """이미 만료된 토큰 → False."""
        cache = {
            "access_token": "tok",
            "expires_at": datetime.now() - timedelta(minutes=1),
        }
        self.assertFalse(auth._is_token_valid(cache))

    def test_within_5min_false(self):
        """만료 4분 전 → False (5분 갱신 트리거 기준)."""
        cache = {
            "access_token": "tok",
            "expires_at": datetime.now() + timedelta(minutes=4),
        }
        self.assertFalse(auth._is_token_valid(cache))

    def test_valid_true(self):
        """만료 10분 이상 남음 → True."""
        cache = {
            "access_token": "tok",
            "expires_at": datetime.now() + timedelta(minutes=10),
        }
        self.assertTrue(auth._is_token_valid(cache))


# ──────────────────────────────────────────────
# TestGetAccessToken
# ──────────────────────────────────────────────

class TestGetAccessToken(unittest.TestCase):

    def setUp(self) -> None:
        _reset_caches()

    # ── 키 미설정 ──────────────────────────────

    @patch("kis.auth.config")
    @patch("kis.auth.requests.post")
    def test_no_key_returns_none(self, mock_post, mock_config):
        """KIS_APP_KEY=None → None 반환, requests.post 호출 없음."""
        mock_config.KIS_APP_KEY = None
        mock_config.KIS_APP_SECRET = "secret"

        result = auth.get_access_token()

        self.assertIsNone(result)
        mock_post.assert_not_called()

    # ── 정상 발급 ──────────────────────────────

    @patch("kis.auth.config")
    @patch("kis.auth.requests.post")
    def test_success_returns_token(self, mock_post, mock_config):
        """API 정상 응답 → 토큰 문자열 반환."""
        mock_config.KIS_APP_KEY = "app_key"
        mock_config.KIS_APP_SECRET = "app_secret"
        mock_post.return_value = _make_post_response("tok_real")

        result = auth.get_access_token()

        self.assertEqual(result, "tok_real")
        mock_post.assert_called_once()

    # ── 캐시 히트 ──────────────────────────────

    @patch("kis.auth.config")
    @patch("kis.auth.requests.post")
    def test_cache_hit_no_refresh(self, mock_post, mock_config):
        """유효한 캐시 존재 → 두 번째 호출 시 requests.post 추가 호출 없음."""
        mock_config.KIS_APP_KEY = "app_key"
        mock_config.KIS_APP_SECRET = "app_secret"
        mock_post.return_value = _make_post_response("tok_cached")

        # 첫 호출로 캐시 채우기
        first = auth.get_access_token()
        self.assertEqual(first, "tok_cached")
        self.assertEqual(mock_post.call_count, 1)

        # 두 번째 호출 — 캐시가 유효하므로 API 재호출 없어야 함
        second = auth.get_access_token()
        self.assertEqual(second, "tok_cached")
        self.assertEqual(mock_post.call_count, 1)  # 여전히 1회

    # ── 만료 → 자동 갱신 ─────────────────────

    @patch("kis.auth.config")
    @patch("kis.auth.requests.post")
    def test_expired_triggers_refresh(self, mock_post, mock_config):
        """만료된 캐시 → 호출 시 자동 갱신 후 새 토큰 반환."""
        mock_config.KIS_APP_KEY = "app_key"
        mock_config.KIS_APP_SECRET = "app_secret"

        # 이미 만료된 캐시 주입
        auth._token_cache["access_token"] = "old_tok"
        auth._token_cache["expires_at"] = datetime.now() - timedelta(minutes=10)

        mock_post.return_value = _make_post_response("new_tok")

        result = auth.get_access_token()

        self.assertEqual(result, "new_tok")
        mock_post.assert_called_once()

    # ── 예외 처리 ──────────────────────────────

    @patch("kis.auth.config")
    @patch("kis.auth.requests.post")
    def test_requests_exception_returns_none(self, mock_post, mock_config):
        """requests.post 예외 → None 반환 (봇 안 뻗음)."""
        mock_config.KIS_APP_KEY = "app_key"
        mock_config.KIS_APP_SECRET = "app_secret"
        mock_post.side_effect = Exception("network error")

        result = auth.get_access_token()

        self.assertIsNone(result)

    # ── 빈 응답 ────────────────────────────────

    @patch("kis.auth.config")
    @patch("kis.auth.requests.post")
    def test_empty_response_returns_none(self, mock_post, mock_config):
        """access_token 없는 응답 → None 반환."""
        mock_config.KIS_APP_KEY = "app_key"
        mock_config.KIS_APP_SECRET = "app_secret"
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"error": "invalid_client"}
        mock_post.return_value = resp

        result = auth.get_access_token()

        self.assertIsNone(result)

    # ── expires_at 24h 검증 ───────────────────

    @patch("kis.auth.config")
    @patch("kis.auth.requests.post")
    def test_expires_at_is_24h(self, mock_post, mock_config):
        """발급 후 expires_at이 현재 + 24시간 근처 (±30초 허용)."""
        mock_config.KIS_APP_KEY = "app_key"
        mock_config.KIS_APP_SECRET = "app_secret"
        mock_post.return_value = _make_post_response("tok_time")

        before = datetime.now() + timedelta(hours=24)
        auth.get_access_token()
        after = datetime.now() + timedelta(hours=24)

        expires = auth._token_cache["expires_at"]
        self.assertIsNotNone(expires)
        self.assertGreaterEqual(expires, before - timedelta(seconds=30))
        self.assertLessEqual(expires, after + timedelta(seconds=30))


# ──────────────────────────────────────────────
# TestVtsTokenSeparation
# ──────────────────────────────────────────────

class TestVtsTokenSeparation(unittest.TestCase):

    def setUp(self) -> None:
        _reset_caches()

    # ── 캐시 독립성 ────────────────────────────

    @patch("kis.auth.config")
    @patch("kis.auth.requests.post")
    def test_vts_independent_from_real(self, mock_post, mock_config):
        """실전 캐시가 만료되어도 유효한 VTS 캐시에 영향 없음."""
        mock_config.KIS_APP_KEY = "real_key"
        mock_config.KIS_APP_SECRET = "real_secret"
        mock_config.KIS_VTS_APP_KEY = "vts_key"
        mock_config.KIS_VTS_APP_SECRET = "vts_secret"

        # VTS 캐시만 유효하게 세팅
        auth._vts_token_cache["access_token"] = "vts_valid"
        auth._vts_token_cache["expires_at"] = datetime.now() + timedelta(hours=1)

        # 실전 캐시는 만료
        auth._token_cache["access_token"] = "real_old"
        auth._token_cache["expires_at"] = datetime.now() - timedelta(minutes=1)

        mock_post.return_value = _make_post_response("real_new")

        # 실전 토큰 갱신 (VTS 캐시 건드리지 않아야 함)
        auth.get_access_token()

        # VTS 캐시는 여전히 원래 값
        self.assertEqual(auth._vts_token_cache["access_token"], "vts_valid")

    # ── VTS URL 호출 확인 ─────────────────────

    @patch("kis.auth.config")
    @patch("kis.auth.requests.post")
    def test_vts_uses_vts_url(self, mock_post, mock_config):
        """VTS 토큰 발급 시 'openapivts' 도메인으로 POST 호출."""
        mock_config.KIS_VTS_APP_KEY = "vts_key"
        mock_config.KIS_VTS_APP_SECRET = "vts_secret"
        mock_post.return_value = _make_post_response("vts_tok")

        auth.get_vts_access_token()

        called_url: str = mock_post.call_args[0][0]
        self.assertIn("openapivts", called_url)

    # ── 실전 URL 호출 확인 ────────────────────

    @patch("kis.auth.config")
    @patch("kis.auth.requests.post")
    def test_real_uses_real_url(self, mock_post, mock_config):
        """실전 토큰 발급 시 'openapi.koreainvestment' 도메인으로 POST 호출."""
        mock_config.KIS_APP_KEY = "real_key"
        mock_config.KIS_APP_SECRET = "real_secret"
        mock_post.return_value = _make_post_response("real_tok")

        auth.get_access_token()

        called_url: str = mock_post.call_args[0][0]
        self.assertIn("openapi.koreainvestment", called_url)
        self.assertNotIn("vts", called_url)

    # ── VTS 키 미설정 ─────────────────────────

    @patch("kis.auth.config")
    @patch("kis.auth.requests.post")
    def test_vts_no_key_returns_none(self, mock_post, mock_config):
        """KIS_VTS_APP_KEY=None → None 반환, requests.post 호출 없음."""
        mock_config.KIS_VTS_APP_KEY = None
        mock_config.KIS_VTS_APP_SECRET = "vts_secret"

        result = auth.get_vts_access_token()

        self.assertIsNone(result)
        mock_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
