"""
tests/test_main_schedule.py
ARCHITECTURE §1 — 스케줄 계약 + 각 봇 함수 단위 테스트

실행: python -m pytest tests/test_main_schedule.py -v
"""

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch, call

# tests/ 디렉터리 안에서 실행해도 프로젝트 루트(main.py 위치)를 찾을 수 있도록
# sys.path 맨 앞에 삽입한다.
_ROOT = Path(__file__).resolve().parent.parent  # tests/ → 프로젝트 루트
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ──────────────────────────────────────────────────────────────
# 최소 stub — main.py import 시 실제 외부 의존성 차단
# ──────────────────────────────────────────────────────────────

def _make_stub(name: str, **attrs):
    """가짜 모듈 생성 헬퍼"""
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def _patch_sys_modules():
    """main.py 최상위 import가 실패하지 않도록 stub 주입"""
    stubs = {
        "apscheduler": _make_stub("apscheduler"),
        "apscheduler.schedulers": _make_stub("apscheduler.schedulers"),
        "apscheduler.schedulers.asyncio": _make_stub(
            "apscheduler.schedulers.asyncio",
            AsyncIOScheduler=MagicMock,
        ),
        "utils.logger": _make_stub("utils.logger", logger=MagicMock()),
        "utils.date_utils": _make_stub(
            "utils.date_utils",
            is_market_open=MagicMock(return_value=True),
            get_today=MagicMock(return_value="2025-01-02"),
        ),
        "config": _make_stub(
            "config",
            AUTO_TRADE_ENABLED=True,
            TRADING_MODE="VTS",
            REAL_MODE_CONFIRM_ENABLED=False,
            REAL_MODE_CONFIRM_DELAY_SEC=300,
            KIS_ACCOUNT_NO="",
            validate_env=MagicMock(),
        ),
        # 지연 import용 stub (함수 내부 import)
        "reports.morning_report": _make_stub("reports.morning_report", run=AsyncMock()),
        "collectors.data_collector": _make_stub(
            "collectors.data_collector",
            run=AsyncMock(return_value={"success_flags": {}}),
            get_cache=MagicMock(return_value={"dart_data": []}),
            is_fresh=MagicMock(return_value=True),
        ),
        "tracking.performance_tracker": _make_stub(
            "tracking.performance_tracker", run_batch=MagicMock()
        ),
        "traders.position_manager": _make_stub(
            "traders.position_manager",
            force_close_all=MagicMock(return_value=[]),
            final_close_all=MagicMock(return_value=[]),
        ),
        "telegram.sender": _make_stub(
            "telegram.sender",
            send_async=AsyncMock(),
            format_trade_closed=MagicMock(side_effect=lambda c: f"closed:{c}"),
        ),
        "telegram": _make_stub("telegram"),
        "tracking.db_schema": _make_stub("tracking.db_schema", init_db=MagicMock()),
        "reports.realtime_alert": _make_stub(
            "reports.realtime_alert", start=AsyncMock(), stop=AsyncMock()
        ),
        "reports.weekly_report": _make_stub("reports.weekly_report", run=AsyncMock()),
        "tracking.principles_extractor": _make_stub(
            "tracking.principles_extractor",
            run_weekly_extraction=MagicMock(
                return_value={"inserted": 0, "updated": 0, "total_principles": 0}
            ),
        ),
        "tracking.memory_compressor": _make_stub(
            "tracking.memory_compressor", run_compression=MagicMock(return_value={})
        ),
        "telegram.commands": _make_stub(
            "telegram.commands", start_interactive_handler=AsyncMock()
        ),
    }
    for name, mod in stubs.items():
        sys.modules.setdefault(name, mod)


_patch_sys_modules()

# stub 주입 후 main import
import main  # noqa: E402


# ──────────────────────────────────────────────────────────────
# 공통 패치 경로
# ──────────────────────────────────────────────────────────────
IS_MARKET_OPEN = "main.is_market_open"
GET_TODAY = "main.get_today"
MORNING_REPORT_RUN = "reports.morning_report.run"
DC_GET_CACHE = "collectors.data_collector.get_cache"
DC_IS_FRESH = "collectors.data_collector.is_fresh"
PERF_RUN_BATCH = "tracking.performance_tracker.run_batch"
PM_FORCE = "traders.position_manager.force_close_all"
PM_FINAL = "traders.position_manager.final_close_all"
TG_SEND = "telegram.sender.send_async"
TG_FORMAT = "telegram.sender.format_trade_closed"
CONFIG_ATE = "main.config"


# ══════════════════════════════════════════════════════════════
# [TestRunMorningBot]
# ══════════════════════════════════════════════════════════════

class TestRunMorningBot(IsolatedAsyncioTestCase):

    async def test_holiday_skips(self):
        """휴장일이면 morning_report.run 을 호출하지 않는다."""
        with patch(IS_MARKET_OPEN, return_value=False), \
             patch(GET_TODAY, return_value="2025-01-01"):
            mock_run = AsyncMock()
            sys.modules["reports.morning_report"].run = mock_run
            await main.run_morning_bot()
            mock_run.assert_not_called()

    async def test_fresh_cache_passed_to_morning_report(self):
        """캐시가 fresh 이면 get_cache() 호출 후 결과를 run(cache=...) 에 전달한다."""
        fake_cache = {"dart_data": [{"id": 1}], "market_data": {}}
        mock_run = AsyncMock()
        mock_get_cache = MagicMock(return_value=fake_cache)
        sys.modules["reports.morning_report"].run = mock_run
        sys.modules["collectors.data_collector"].get_cache = mock_get_cache
        sys.modules["collectors.data_collector"].is_fresh = MagicMock(return_value=True)

        with patch(IS_MARKET_OPEN, return_value=True), \
             patch(GET_TODAY, return_value="2025-01-02"):
            await main.run_morning_bot()

        # is_fresh=True → get_cache() 반드시 호출
        mock_get_cache.assert_called_once()
        mock_run.assert_awaited_once()
        _, kwargs = mock_run.call_args
        self.assertEqual(kwargs.get("cache"), fake_cache)

    async def test_stale_cache_passes_empty_dict(self):
        """캐시가 stale(오래됨/없음)이면 get_cache() 미호출, run(cache={}) 빈 dict 전달한다."""
        mock_run = AsyncMock()
        mock_get_cache = MagicMock(return_value={"dart_data": []})
        sys.modules["reports.morning_report"].run = mock_run
        sys.modules["collectors.data_collector"].get_cache = mock_get_cache
        sys.modules["collectors.data_collector"].is_fresh = MagicMock(return_value=False)

        with patch(IS_MARKET_OPEN, return_value=True), \
             patch(GET_TODAY, return_value="2025-01-02"):
            await main.run_morning_bot()

        # is_fresh=False → get_cache() 호출하지 않아야 한다 (버그3 수정 핵심)
        mock_get_cache.assert_not_called()
        mock_run.assert_awaited_once()
        _, kwargs = mock_run.call_args
        self.assertEqual(kwargs.get("cache"), {})

    async def test_calls_morning_report_run(self):
        """정상 장날에는 morning_report.run 이 정확히 1회 호출된다."""
        mock_run = AsyncMock()
        sys.modules["reports.morning_report"].run = mock_run
        sys.modules["collectors.data_collector"].is_fresh = MagicMock(return_value=True)
        sys.modules["collectors.data_collector"].get_cache = MagicMock(return_value={})

        with patch(IS_MARKET_OPEN, return_value=True), \
             patch(GET_TODAY, return_value="2025-01-02"):
            await main.run_morning_bot()

        self.assertEqual(mock_run.await_count, 1)


# ══════════════════════════════════════════════════════════════
# [TestRunForceClose]  — 14:50 강제청산
# ══════════════════════════════════════════════════════════════

class TestRunForceClose(IsolatedAsyncioTestCase):

    async def test_auto_trade_disabled_skips(self):
        """AUTO_TRADE_ENABLED=False → force_close_all 호출 없음."""
        mock_force = MagicMock(return_value=[])
        sys.modules["traders.position_manager"].force_close_all = mock_force

        original = main.config.AUTO_TRADE_ENABLED
        main.config.AUTO_TRADE_ENABLED = False
        try:
            await main.run_force_close()
        finally:
            main.config.AUTO_TRADE_ENABLED = original

        mock_force.assert_not_called()

    async def test_holiday_skips(self):
        """휴장일이면 force_close_all 호출 없음."""
        mock_force = MagicMock(return_value=[])
        sys.modules["traders.position_manager"].force_close_all = mock_force

        main.config.AUTO_TRADE_ENABLED = True
        with patch(IS_MARKET_OPEN, return_value=False), \
             patch(GET_TODAY, return_value="2025-01-01"):
            await main.run_force_close()

        mock_force.assert_not_called()

    async def test_empty_closed_list_no_telegram(self):
        """force_close_all 이 빈 리스트 반환 → send_async 호출 없음."""
        mock_force = MagicMock(return_value=[])
        mock_send = AsyncMock()
        sys.modules["traders.position_manager"].force_close_all = mock_force
        sys.modules["telegram.sender"].send_async = mock_send

        main.config.AUTO_TRADE_ENABLED = True
        with patch(IS_MARKET_OPEN, return_value=True), \
             patch(GET_TODAY, return_value="2025-01-02"):
            await main.run_force_close()

        mock_send.assert_not_called()

    async def test_sends_telegram_per_closed(self):
        """청산 2건 → format_trade_closed 2회, send_async 2회 호출."""
        closed_items = [{"종목코드": "005930"}, {"종목코드": "000660"}]
        mock_force = MagicMock(return_value=closed_items)
        mock_send = AsyncMock()
        mock_format = MagicMock(side_effect=lambda c: f"msg:{c['종목코드']}")
        sys.modules["traders.position_manager"].force_close_all = mock_force
        sys.modules["telegram.sender"].send_async = mock_send
        sys.modules["telegram.sender"].format_trade_closed = mock_format

        main.config.AUTO_TRADE_ENABLED = True
        with patch(IS_MARKET_OPEN, return_value=True), \
             patch(GET_TODAY, return_value="2025-01-02"):
            await main.run_force_close()

        self.assertEqual(mock_format.call_count, 2)
        self.assertEqual(mock_send.await_count, 2)

    async def test_telegram_failure_does_not_crash(self):
        """send_async 예외 발생 → 나머지 청산 알림이 계속 처리된다."""
        closed_items = [{"종목코드": "005930"}, {"종목코드": "000660"}]
        mock_force = MagicMock(return_value=closed_items)
        # 첫 번째 호출만 예외, 두 번째는 정상
        mock_send = AsyncMock(side_effect=[Exception("네트워크 오류"), None])
        mock_format = MagicMock(side_effect=lambda c: f"msg:{c['종목코드']}")
        sys.modules["traders.position_manager"].force_close_all = mock_force
        sys.modules["telegram.sender"].send_async = mock_send
        sys.modules["telegram.sender"].format_trade_closed = mock_format

        main.config.AUTO_TRADE_ENABLED = True
        with patch(IS_MARKET_OPEN, return_value=True), \
             patch(GET_TODAY, return_value="2025-01-02"):
            # 예외가 전파되지 않아야 한다
            await main.run_force_close()

        # 두 번 모두 시도했음을 확인
        self.assertEqual(mock_send.await_count, 2)


# ══════════════════════════════════════════════════════════════
# [TestRunFinalClose]  — 15:20 최종청산
# ══════════════════════════════════════════════════════════════

class TestRunFinalClose(IsolatedAsyncioTestCase):

    async def test_auto_trade_disabled_skips(self):
        """AUTO_TRADE_ENABLED=False → final_close_all 호출 없음."""
        mock_final = MagicMock(return_value=[])
        sys.modules["traders.position_manager"].final_close_all = mock_final

        main.config.AUTO_TRADE_ENABLED = False
        try:
            await main.run_final_close()
        finally:
            main.config.AUTO_TRADE_ENABLED = True

        mock_final.assert_not_called()

    async def test_holiday_skips(self):
        """휴장일이면 final_close_all 호출 없음."""
        mock_final = MagicMock(return_value=[])
        sys.modules["traders.position_manager"].final_close_all = mock_final

        main.config.AUTO_TRADE_ENABLED = True
        with patch(IS_MARKET_OPEN, return_value=False), \
             patch(GET_TODAY, return_value="2025-01-01"):
            await main.run_final_close()

        mock_final.assert_not_called()

    async def test_sends_telegram_per_closed(self):
        """청산 건수만큼 send_async 가 호출된다."""
        n = 3
        closed_items = [{"종목코드": f"00{i}"} for i in range(n)]
        mock_final = MagicMock(return_value=closed_items)
        mock_send = AsyncMock()
        mock_format = MagicMock(side_effect=lambda c: f"msg:{c}")
        sys.modules["traders.position_manager"].final_close_all = mock_final
        sys.modules["telegram.sender"].send_async = mock_send
        sys.modules["telegram.sender"].format_trade_closed = mock_format

        main.config.AUTO_TRADE_ENABLED = True
        with patch(IS_MARKET_OPEN, return_value=True), \
             patch(GET_TODAY, return_value="2025-01-02"):
            await main.run_final_close()

        self.assertEqual(mock_send.await_count, n)

    async def test_telegram_failure_does_not_crash(self):
        """send_async 예외 발생 → 나머지 종목 알림 계속 처리된다."""
        closed_items = [{"종목코드": "A"}, {"종목코드": "B"}, {"종목코드": "C"}]
        mock_final = MagicMock(return_value=closed_items)
        # 첫 번째만 예외
        mock_send = AsyncMock(side_effect=[Exception("오류"), None, None])
        mock_format = MagicMock(side_effect=lambda c: str(c))
        sys.modules["traders.position_manager"].final_close_all = mock_final
        sys.modules["telegram.sender"].send_async = mock_send
        sys.modules["telegram.sender"].format_trade_closed = mock_format

        main.config.AUTO_TRADE_ENABLED = True
        with patch(IS_MARKET_OPEN, return_value=True), \
             patch(GET_TODAY, return_value="2025-01-02"):
            await main.run_final_close()  # 예외 전파 없음

        # 3건 모두 시도
        self.assertEqual(mock_send.await_count, 3)


# ══════════════════════════════════════════════════════════════
# [TestRunPerformanceBatch]  — 15:45 수익률 배치
# ══════════════════════════════════════════════════════════════

class TestRunPerformanceBatch(IsolatedAsyncioTestCase):

    async def test_holiday_skips(self):
        """휴장일이면 run_batch 를 호출하지 않는다."""
        mock_rb = MagicMock()
        sys.modules["tracking.performance_tracker"].run_batch = mock_rb

        with patch(IS_MARKET_OPEN, return_value=False), \
             patch(GET_TODAY, return_value="2025-01-01"):
            await main.run_performance_batch()

        mock_rb.assert_not_called()

    async def test_weekday_calls_run_batch(self):
        """정상 장날에는 run_batch 가 정확히 1회 호출된다."""
        mock_rb = MagicMock()
        sys.modules["tracking.performance_tracker"].run_batch = mock_rb

        with patch(IS_MARKET_OPEN, return_value=True), \
             patch(GET_TODAY, return_value="2025-01-02"):
            await main.run_performance_batch()

        mock_rb.assert_called_once()


# ══════════════════════════════════════════════════════════════
# [TestScheduleRegistration]  — ARCHITECTURE §1 스케줄 계약 검증
# ══════════════════════════════════════════════════════════════

class TestScheduleRegistration(unittest.TestCase):
    """
    main() 내 scheduler.add_job 호출을 가로채어
    ARCHITECTURE §1 에서 요구하는 7개 job id 가 모두 등록되는지 검증한다.
    """

    def test_all_jobs_registered(self):
        required_jobs = {
            "data_collector",  # 06:00
            "morning_bot",     # 07:30
            "rt_start",        # 09:00
            "rt_stop",         # 15:30
            "perf_batch",      # 15:45
            "force_close",     # 14:50
            "final_close",     # 15:20
        }

        registered_ids: set[str] = set()

        class FakeScheduler:
            def add_job(self, func, trigger, id=None, **kwargs):
                if id:
                    registered_ids.add(id)

            def start(self):
                pass

            def shutdown(self):
                pass

        fake_scheduler = FakeScheduler()

        # main.py 는 `from apscheduler.schedulers.asyncio import AsyncIOScheduler`
        # 로 직접 바인딩하므로 반드시 "main.AsyncIOScheduler" 를 패치해야 한다.
        with patch("main.AsyncIOScheduler", return_value=fake_scheduler), \
             patch("main.config.validate_env"), \
             patch("tracking.db_schema.init_db"), \
             patch("main._check_real_mode_safety", new=AsyncMock()), \
             patch("main._maybe_start_now", new=AsyncMock()), \
             patch("main.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)), \
             patch("main.asyncio.create_task", side_effect=lambda coro: coro.close() or MagicMock()):
            try:
                asyncio.run(main.main())
            except (asyncio.CancelledError, Exception):
                pass  # sleep CancelledError 로 루프 종료

        missing = required_jobs - registered_ids
        self.assertFalse(
            missing,
            f"스케줄러에 등록되지 않은 job id: {missing}\n"
            f"등록된 job id: {registered_ids}",
        )



# ══════════════════════════════════════════════════════════════
# [TestRunWeeklyReport]  — 버그4 수정: weekday() 이중 체크 제거 검증
# ══════════════════════════════════════════════════════════════

class TestRunWeeklyReport(IsolatedAsyncioTestCase):

    async def test_holiday_skips(self):
        """휴장일이면 weekly_report.run 을 호출하지 않는다."""
        mock_run = AsyncMock()
        sys.modules["reports.weekly_report"].run = mock_run

        with patch(IS_MARKET_OPEN, return_value=False), \
             patch(GET_TODAY, return_value="2025-01-06"):  # 월요일
            await main.run_weekly_report()

        mock_run.assert_not_called()

    async def test_market_day_calls_run(self):
        """장날이면 weekday() 조건 없이 weekly_report.run 을 1회 호출한다."""
        mock_run = AsyncMock()
        sys.modules["reports.weekly_report"].run = mock_run

        with patch(IS_MARKET_OPEN, return_value=True), \
             patch(GET_TODAY, return_value="2025-01-06"):
            await main.run_weekly_report()

        mock_run.assert_awaited_once()

    def test_no_weekday_check_in_source(self):
        """run_weekly_report 함수 본문에 weekday() 이중 체크가 없어야 한다. (버그4 수정 확인)"""
        import inspect
        src = inspect.getsource(main.run_weekly_report)
        self.assertNotIn(
            "weekday()",
            src,
            "run_weekly_report 내부에 weekday() 이중 체크가 남아 있음 — 버그4 미수정"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
