"""
tests/test_sender.py

외부 의존성(Bot, config, asyncio.run 등)은 unittest.mock으로 전부 패치.
ARCHITECTURE §4 / §5 계약 검증 전용.
"""

import asyncio
import importlib
import inspect
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ──────────────────────────────────────────────
# 모듈 로드 전 외부 의존성 stub 주입
# ──────────────────────────────────────────────
def _inject_stubs():
    """telegram / config / utils.logger 를 sys.modules 에 stub으로 등록."""
    # telegram stub
    telegram_stub = types.ModuleType("telegram")
    telegram_stub.Bot = MagicMock()
    telegram_stub.InputFile = MagicMock()
    sys.modules.setdefault("telegram", telegram_stub)

    # config stub
    config_stub = types.ModuleType("config")
    config_stub.TELEGRAM_TOKEN = "FAKE_TOKEN"
    config_stub.TELEGRAM_CHAT_ID = "FAKE_CHAT_ID"
    sys.modules.setdefault("config", config_stub)

    # utils.logger stub
    utils_stub = types.ModuleType("utils")
    utils_logger_stub = types.ModuleType("utils.logger")
    utils_logger_stub.logger = MagicMock()
    utils_stub.logger = utils_logger_stub
    sys.modules.setdefault("utils", utils_stub)
    sys.modules.setdefault("utils.logger", utils_logger_stub)


_inject_stubs()

# sender 모듈을 경로 기반으로 로드
import importlib.util, os, pathlib

_SENDER_CANDIDATES = [
    pathlib.Path(__file__).parent.parent / "telegram" / "sender.py",
    pathlib.Path(__file__).parent / "telegram" / "sender.py",
    pathlib.Path("telegram/sender.py"),
    pathlib.Path("korea_stock_bot-main/telegram/sender.py"),
]


def _load_sender():
    for p in _SENDER_CANDIDATES:
        if p.exists():
            spec = importlib.util.spec_from_file_location("telegram.sender", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    # 마지막 수단: importlib
    return importlib.import_module("telegram.sender")


sender = _load_sender()


# ════════════════════════════════════════════════════════════════
# 공통 픽스처 — 최소한의 trade dict
# ════════════════════════════════════════════════════════════════
def _base_trade(**kwargs) -> dict:
    base = {
        "name": "테스트주식",
        "ticker": "005930",
        "profit_rate": 1.5,
        "close_reason": "take_profit_1",
        "sell_price": 75000,
        "profit_amount": 15000,
    }
    base.update(kwargs)
    return base


# ════════════════════════════════════════════════════════════════
# [TestFormatTradeClosedExists]
# ARCHITECTURE §5 BUG 재발 방지 — 함수 존재 여부
# ════════════════════════════════════════════════════════════════
class TestFormatTradeClosedExists(unittest.TestCase):
    def test_function_exists(self):
        """format_trade_closed 함수가 sender.py 에 반드시 존재해야 한다."""
        self.assertTrue(
            hasattr(sender, "format_trade_closed"),
            "sender.py 에 format_trade_closed 함수가 없음 — ARCHITECTURE §5 BUG-01 재발",
        )
        self.assertTrue(
            inspect.isfunction(sender.format_trade_closed),
            "format_trade_closed 가 함수가 아님",
        )


# ════════════════════════════════════════════════════════════════
# [TestFormatTradeClosedSign]
# ════════════════════════════════════════════════════════════════
class TestFormatTradeClosedSign(unittest.TestCase):
    def test_profit_green(self):
        """profit_rate >= 0 이면 메시지에 🟢 포함."""
        msg = sender.format_trade_closed(_base_trade(profit_rate=3.5))
        self.assertIn("🟢", msg)

    def test_loss_red(self):
        """profit_rate < 0 이면 메시지에 🔴 포함."""
        msg = sender.format_trade_closed(_base_trade(profit_rate=-2.1))
        self.assertIn("🔴", msg)

    def test_zero_profit_is_green(self):
        """profit_rate == 0.0 이면 🟢 (손익 없음은 손실 아님)."""
        msg = sender.format_trade_closed(_base_trade(profit_rate=0.0))
        self.assertIn("🟢", msg)


# ════════════════════════════════════════════════════════════════
# [TestFormatTradeClosedEmoji]
# ARCHITECTURE §4 close_reason 6개 표준 열거값 + 비표준 fallback
# ════════════════════════════════════════════════════════════════
class TestFormatTradeClosedEmoji(unittest.TestCase):
    def _msg(self, reason: str) -> str:
        return sender.format_trade_closed(_base_trade(close_reason=reason))

    def test_take_profit_1_emoji(self):
        self.assertIn("✅", self._msg("take_profit_1"))

    def test_take_profit_2_emoji(self):
        self.assertIn("🎯", self._msg("take_profit_2"))

    def test_stop_loss_emoji(self):
        self.assertIn("🛑", self._msg("stop_loss"))

    def test_trailing_stop_emoji(self):
        self.assertIn("📉", self._msg("trailing_stop"))

    def test_force_close_emoji(self):
        self.assertIn("⏰", self._msg("force_close"))

    def test_final_close_emoji(self):
        self.assertIn("🏁", self._msg("final_close"))

    def test_unknown_reason_default_emoji(self):
        """표준 외 reason 은 기본 이모지 📌 를 반환해야 한다."""
        self.assertIn("📌", self._msg("NONEXISTENT_REASON_XYZ"))


# ════════════════════════════════════════════════════════════════
# [TestFormatTradeClosedKeyFallback]
# ════════════════════════════════════════════════════════════════
class TestFormatTradeClosedKeyFallback(unittest.TestCase):
    def test_name_key_fallback(self):
        """`name` 키 없어도 `종목명` 으로 fallback 해야 한다."""
        trade = {
            "종목명": "삼성전자",
            "ticker": "005930",
            "profit_rate": 1.0,
            "close_reason": "take_profit_1",
            "sell_price": 75000,
            "profit_amount": 10000,
        }
        msg = sender.format_trade_closed(trade)
        self.assertIn("삼성전자", msg)

    def test_ticker_key_fallback(self):
        """`ticker` 키 없어도 `종목코드` 로 fallback 해야 한다."""
        trade = {
            "name": "삼성전자",
            "종목코드": "005930",
            "profit_rate": 1.0,
            "close_reason": "take_profit_1",
            "sell_price": 75000,
            "profit_amount": 10000,
        }
        msg = sender.format_trade_closed(trade)
        self.assertIn("005930", msg)

    def test_sell_price_formatted(self):
        """sell_price 는 천단위 콤마 포맷이 포함되어야 한다."""
        msg = sender.format_trade_closed(_base_trade(sell_price=1234567))
        self.assertIn("1,234,567", msg)

    def test_profit_rate_sign_format(self):
        """profit_rate 는 +/- 부호 포함 포맷이어야 한다 (예: +3.50% / -2.00%)."""
        msg_pos = sender.format_trade_closed(_base_trade(profit_rate=3.5))
        msg_neg = sender.format_trade_closed(_base_trade(profit_rate=-2.0))
        self.assertIn("+3.50%", msg_pos)
        self.assertIn("-2.00%", msg_neg)


# ════════════════════════════════════════════════════════════════
# [TestSendFallback]
# ARCHITECTURE §5 BUG-07 — asyncio.run RuntimeError → new_event_loop 경로
# ════════════════════════════════════════════════════════════════
class TestSendFallback(unittest.TestCase):
    """
    send() 의 fallback 경로를 검증한다.
    asyncio.run 이 RuntimeError 를 올리면 new_event_loop() 를 사용해야 하고,
    이후 예외가 발생해도 loop.close() 가 반드시 호출되어야 한다.
    """

    def _make_mock_loop(self, *, raise_on_run=False):
        loop = MagicMock()
        if raise_on_run:
            loop.run_until_complete.side_effect = Exception("inner error")
        else:
            loop.run_until_complete.return_value = None
        loop.close = MagicMock()
        return loop

    def test_runtime_error_triggers_new_loop(self):
        """asyncio.run 이 RuntimeError 를 올리면 new_event_loop() 경로를 사용해야 한다."""
        mock_loop = self._make_mock_loop()

        with patch.object(sender, "_send", new=AsyncMock()):
            with patch("asyncio.run", side_effect=RuntimeError("already running")):
                with patch("asyncio.new_event_loop", return_value=mock_loop) as mock_new_loop:
                    sender.send("hello")

        mock_new_loop.assert_called_once()
        mock_loop.run_until_complete.assert_called_once()

    def test_loop_closed_even_on_exception(self):
        """new_event_loop 경로에서 예외가 발생해도 loop.close() 는 반드시 호출되어야 한다."""
        mock_loop = self._make_mock_loop(raise_on_run=True)

        with patch.object(sender, "_send", new=AsyncMock()):
            with patch("asyncio.run", side_effect=RuntimeError("already running")):
                with patch("asyncio.new_event_loop", return_value=mock_loop):
                    # inner exception 은 send() 밖으로 전파됨
                    with self.assertRaises(Exception):
                        sender.send("hello")

        mock_loop.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
