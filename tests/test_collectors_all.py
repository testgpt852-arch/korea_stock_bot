"""
test_collectors_all.py
Korea Stock Bot — collectors/ 전체 데이터 수집 테스트

실행 방법:
    # 프로젝트 루트(korea_stock_bot-main/)에서 실행
    pytest tests/test_collectors_all.py -v
    pytest tests/test_collectors_all.py -v -m unit       # 모의(mock) 테스트만
    pytest tests/test_collectors_all.py -v -m integration # 실제 API 호출 테스트만

테스트 구분:
    unit        — 외부 API를 Mock으로 대체, 키 없이 실행 가능
    integration — 실제 네트워크 호출 (pykrx, yfinance, RSS 등 무료 소스)
    requires_key — 유료/키 필요 API (DART, Naver, NewsAPI) — 환경변수 없으면 자동 skip

환경변수 (.env 또는 Railway Variables):
    DART_API_KEY, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, NEWSAPI_ORG_KEY
"""

import sys
import os
from dotenv import load_dotenv   # 추가
load_dotenv()                    # 추가

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

# ── 프로젝트 루트를 PYTHONPATH에 추가 ────────────────────────────
# 이 파일은 korea_stock_bot-main/tests/ 에 배치
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── 최근 거래일 계산 (주말 제외) ────────────────────────────────
def _recent_trading_day(offset_days: int = 1) -> datetime:
    """테스트용 최근 거래일 반환 (주말 제외)"""
    dt = datetime.now() - timedelta(days=offset_days)
    while dt.weekday() >= 5:  # 토(5), 일(6)
        dt -= timedelta(days=1)
    return dt

def _recent_trading_day_str(offset_days: int = 1) -> str:
    return _recent_trading_day(offset_days).strftime("%Y%m%d")

# 테스트 기준일 (어제 영업일)
TRADING_DATE     = _recent_trading_day(1)
TRADING_DATE_STR = _recent_trading_day_str(1)


# ═══════════════════════════════════════════════════════════════════
# 1. closing_strength — pykrx 기반 마감강도 분석
# ═══════════════════════════════════════════════════════════════════

class TestClosingStrength:
    """collectors/closing_strength.py — analyze()"""

    @pytest.mark.unit
    def test_returns_list(self):
        """analyze()는 항상 list를 반환해야 한다"""
        from collectors.closing_strength import analyze

        mock_df = MagicMock()
        mock_df.empty = True

        with patch("collectors.closing_strength.pykrx_stock.get_market_ohlcv_by_ticker",
                   return_value=mock_df):
            result = analyze(TRADING_DATE_STR, top_n=5)

        assert isinstance(result, list), "결과가 list가 아님"

    @pytest.mark.unit
    def test_result_structure(self):
        """반환된 항목의 필수 키 검증"""
        import pandas as pd
        from collectors.closing_strength import analyze

        # 최소한 강봉 1종목이 나오는 mock DataFrame 구성
        sample_data = {
            "시가": [1000], "고가": [1200], "저가": [900],
            "종가": [1150], "거래량": [100000], "등락률": [5.0],
        }
        mock_today = pd.DataFrame(sample_data, index=["005930"])
        mock_prev  = pd.DataFrame({"거래량": [80000]}, index=["005930"])

        with patch("collectors.closing_strength.pykrx_stock.get_market_ohlcv_by_ticker",
                   side_effect=[mock_today, mock_prev, pd.DataFrame(), pd.DataFrame()]), \
             patch("collectors.closing_strength.pykrx_stock.get_market_ticker_name",
                   return_value="삼성전자"):
            result = analyze(TRADING_DATE_STR, top_n=5)

        if result:
            item = result[0]
            required_keys = {"종목코드", "종목명", "마감강도", "등락률", "거래량증가율", "종가", "고가", "저가"}
            missing = required_keys - set(item.keys())
            assert not missing, f"필수 키 누락: {missing}"

            assert 0.0 <= item["마감강도"] <= 1.0, "마감강도는 0~1 사이여야 함"

    @pytest.mark.unit
    def test_top_n_respected(self):
        """top_n 파라미터가 결과 개수를 제한해야 한다"""
        import pandas as pd
        from collectors.closing_strength import analyze

        # 10종목 mock
        tickers = [f"{str(i).zfill(6)}" for i in range(10)]
        sample = {
            "시가":  [1000] * 10, "고가":  [1200] * 10,
            "저가":  [900]  * 10, "종가":  [1150] * 10,
            "거래량":[10000]* 10, "등락률":[3.0]  * 10,
        }
        mock_df = pd.DataFrame(sample, index=tickers)

        with patch("collectors.closing_strength.pykrx_stock.get_market_ohlcv_by_ticker",
                   return_value=mock_df), \
             patch("collectors.closing_strength.pykrx_stock.get_market_ticker_name",
                   return_value="테스트종목"):
            result = analyze(TRADING_DATE_STR, top_n=3)

        assert len(result) <= 3, f"top_n=3 인데 {len(result)}개 반환됨"

    @pytest.mark.unit
    def test_sorted_by_strength_descending(self):
        """결과가 마감강도 내림차순으로 정렬되어야 한다"""
        import pandas as pd
        from collectors.closing_strength import analyze

        sample = {
            "시가":  [1000, 1000], "고가":  [1200, 1300],
            "저가":  [900,  800],  "종가":  [1100, 1250],
            "거래량":[10000, 20000], "등락률":[3.0, 5.0],
        }
        mock_df = pd.DataFrame(sample, index=["000001", "000002"])

        with patch("collectors.closing_strength.pykrx_stock.get_market_ohlcv_by_ticker",
                   return_value=mock_df), \
             patch("collectors.closing_strength.pykrx_stock.get_market_ticker_name",
                   return_value="테스트"):
            result = analyze(TRADING_DATE_STR, top_n=10)

        if len(result) >= 2:
            strengths = [r["마감강도"] for r in result]
            assert strengths == sorted(strengths, reverse=True), "마감강도 내림차순 정렬 실패"

    @pytest.mark.integration
    def test_real_pykrx_call(self):
        """실제 pykrx 호출 — 네트워크 필요"""
        from collectors.closing_strength import analyze
        result = analyze(TRADING_DATE_STR, top_n=5)

        assert isinstance(result, list), "실제 호출 결과가 list가 아님"
        print(f"\n  [closing_strength] 실제 결과: {len(result)}종목")
        for item in result[:3]:
            print(f"    {item['종목명']}: 마감강도={item['마감강도']:.3f}, 등락률={item['등락률']:+.1f}%")


# ═══════════════════════════════════════════════════════════════════
# 2. volume_surge — pykrx 기반 거래량급증 분석
# ═══════════════════════════════════════════════════════════════════

class TestVolumeSurge:
    """collectors/volume_surge.py — analyze()"""

    @pytest.mark.unit
    def test_returns_list(self):
        """결과가 list여야 한다"""
        from collectors.volume_surge import analyze

        with patch("collectors.volume_surge.pykrx_stock.get_market_ohlcv_by_ticker",
                   return_value=MagicMock(empty=True)):
            result = analyze(TRADING_DATE_STR, top_n=5)

        assert isinstance(result, list)

    @pytest.mark.unit
    def test_result_structure(self):
        """반환 항목의 필수 키 검증"""
        import pandas as pd
        from collectors.volume_surge import analyze

        today_data = {
            "등락률": [2.0], "거래량": [200000], "종가": [10000],
        }
        prev_data = {"거래량": [100000]}

        mock_today = pd.DataFrame(today_data, index=["005930"])
        mock_prev  = pd.DataFrame(prev_data,  index=["005930"])

        with patch("collectors.volume_surge.pykrx_stock.get_market_ohlcv_by_ticker",
                   side_effect=[mock_today, mock_prev, pd.DataFrame(), pd.DataFrame()]), \
             patch("collectors.volume_surge.pykrx_stock.get_market_ticker_name",
                   return_value="삼성전자"):
            result = analyze(TRADING_DATE_STR, top_n=5)

        if result:
            item = result[0]
            required_keys = {"종목코드", "종목명", "등락률", "거래량증가율", "거래량", "전일거래량", "종가"}
            missing = required_keys - set(item.keys())
            assert not missing, f"필수 키 누락: {missing}"

    @pytest.mark.unit
    def test_flat_price_filter(self):
        """등락률이 VOLUME_FLAT_CHANGE_MAX 초과하는 종목은 제외되어야 한다"""
        import pandas as pd
        import config
        from collectors.volume_surge import analyze

        # 등락률 20% (VOLUME_FLAT_CHANGE_MAX=5.0 초과) → 제외
        today_data = {"등락률": [20.0], "거래량": [300000], "종가": [10000]}
        prev_data  = {"거래량": [100000]}

        mock_today = pd.DataFrame(today_data, index=["000001"])
        mock_prev  = pd.DataFrame(prev_data,  index=["000001"])

        with patch("collectors.volume_surge.pykrx_stock.get_market_ohlcv_by_ticker",
                   side_effect=[mock_today, mock_prev, pd.DataFrame(), pd.DataFrame()]), \
             patch("collectors.volume_surge.pykrx_stock.get_market_ticker_name",
                   return_value="급등주"):
            result = analyze(TRADING_DATE_STR, top_n=10)

        # 급등주(20%)는 횡보 조건 위반이므로 결과에 없어야 함
        codes = [r["종목코드"] for r in result]
        assert "000001" not in codes, "등락률 초과 종목이 결과에 포함됨"

    @pytest.mark.integration
    def test_real_pykrx_call(self):
        """실제 pykrx 호출"""
        from collectors.volume_surge import analyze
        result = analyze(TRADING_DATE_STR, top_n=5)

        assert isinstance(result, list)
        print(f"\n  [volume_surge] 실제 결과: {len(result)}종목")
        for item in result[:3]:
            print(f"    {item['종목명']}: 거래량증가율={item['거래량증가율']:.1f}%, 등락률={item['등락률']:+.1f}%")


# ═══════════════════════════════════════════════════════════════════
# 3. fund_concentration — pykrx 기반 자금집중 분석
# ═══════════════════════════════════════════════════════════════════

class TestFundConcentration:
    """collectors/fund_concentration.py — analyze()"""

    @pytest.mark.unit
    def test_returns_list(self):
        with patch("collectors.fund_concentration.pykrx_stock.get_market_ohlcv_by_ticker",
                   return_value=MagicMock(empty=True)):
            from collectors.fund_concentration import analyze
            result = analyze(TRADING_DATE_STR, top_n=5)
        assert isinstance(result, list)

    @pytest.mark.unit
    def test_result_structure(self):
        """반환 항목의 필수 키 검증"""
        import pandas as pd
        from collectors.fund_concentration import analyze

        ohlcv_data = {"종가": [10000], "거래량": [50000], "거래대금": [500_000_000], "등락률": [3.0]}
        cap_data   = {"시가총액": [100_000_000_000]}  # 1000억

        mock_ohlcv = pd.DataFrame(ohlcv_data, index=["005930"])
        mock_cap   = pd.DataFrame(cap_data,   index=["005930"])

        with patch("collectors.fund_concentration.pykrx_stock.get_market_ohlcv_by_ticker",
                   return_value=mock_ohlcv), \
             patch("collectors.fund_concentration.pykrx_stock.get_market_cap_by_ticker",
                   return_value=mock_cap), \
             patch("collectors.fund_concentration.pykrx_stock.get_market_ticker_name",
                   return_value="삼성전자"):
            result = analyze(TRADING_DATE_STR, top_n=5)

        if result:
            item = result[0]
            required_keys = {"종목코드", "종목명", "등락률", "자금유입비율", "거래대금", "시가총액", "종가"}
            missing = required_keys - set(item.keys())
            assert not missing, f"필수 키 누락: {missing}"

    @pytest.mark.unit
    def test_sorted_by_ratio_descending(self):
        """자금유입비율 내림차순 정렬 확인"""
        import pandas as pd
        from collectors.fund_concentration import analyze

        ohlcv_data = {
            "종가": [10000, 5000], "거래량": [100000, 200000],
            "거래대금": [1_000_000_000, 2_000_000_000], "등락률": [2.0, 3.0],
        }
        cap_data = {"시가총액": [50_000_000_000, 30_000_000_000]}

        mock_ohlcv = pd.DataFrame(ohlcv_data, index=["000001", "000002"])
        mock_cap   = pd.DataFrame(cap_data,   index=["000001", "000002"])

        with patch("collectors.fund_concentration.pykrx_stock.get_market_ohlcv_by_ticker",
                   return_value=mock_ohlcv), \
             patch("collectors.fund_concentration.pykrx_stock.get_market_cap_by_ticker",
                   return_value=mock_cap), \
             patch("collectors.fund_concentration.pykrx_stock.get_market_ticker_name",
                   return_value="테스트"):
            result = analyze(TRADING_DATE_STR, top_n=10)

        if len(result) >= 2:
            ratios = [r["자금유입비율"] for r in result]
            assert ratios == sorted(ratios, reverse=True), "자금유입비율 내림차순 정렬 실패"

    @pytest.mark.integration
    def test_real_pykrx_call(self):
        from collectors.fund_concentration import analyze
        result = analyze(TRADING_DATE_STR, top_n=5)
        assert isinstance(result, list)
        print(f"\n  [fund_concentration] 실제 결과: {len(result)}종목")
        for item in result[:3]:
            print(f"    {item['종목명']}: 자금유입비율={item['자금유입비율']:.3f}%")


# ═══════════════════════════════════════════════════════════════════
# 4. market_global — yfinance 기반 미국증시·원자재
# ═══════════════════════════════════════════════════════════════════

class TestMarketGlobal:
    """collectors/market_global.py — collect()"""

    @pytest.mark.unit
    def test_returns_dict_with_required_keys(self):
        """collect() 반환 dict의 최상위 키 검증"""
        import pandas as pd
        from collectors.market_global import collect

        mock_hist = pd.DataFrame({"Close": [100.0, 102.0]})

        with patch("collectors.market_global.yf.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = mock_hist
            result = collect(TRADING_DATE)

        assert isinstance(result, dict), "결과가 dict가 아님"
        assert "us_market"   in result, "us_market 키 없음"
        assert "commodities" in result, "commodities 키 없음"
        assert "forex"       in result, "forex 키 없음"

    @pytest.mark.unit
    def test_us_market_structure(self):
        """us_market 내부 구조 검증"""
        import pandas as pd
        from collectors.market_global import collect

        mock_hist = pd.DataFrame({"Close": [100.0, 102.0]})

        with patch("collectors.market_global.yf.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = mock_hist
            result = collect(TRADING_DATE)

        us = result["us_market"]
        assert "nasdaq" in us, "nasdaq 키 없음"
        assert "sp500"  in us, "sp500 키 없음"
        assert "dow"    in us, "dow 키 없음"
        assert "sectors" in us, "sectors 키 없음"

    @pytest.mark.unit
    def test_empty_history_returns_na(self):
        """yfinance 데이터 없을 때 N/A 처리"""
        import pandas as pd
        from collectors.market_global import collect

        with patch("collectors.market_global.yf.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = pd.DataFrame()
            result = collect(TRADING_DATE)

        assert result["us_market"]["nasdaq"] == "N/A"
        assert result["us_market"]["sp500"]  == "N/A"

    @pytest.mark.unit
    def test_change_format(self):
        """등락률 포맷이 '+숫자%' 또는 '-숫자%' 형식이어야 한다"""
        import pandas as pd
        import re
        from collectors.market_global import collect

        mock_hist = pd.DataFrame({"Close": [1000.0, 1020.0]})

        with patch("collectors.market_global.yf.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = mock_hist
            result = collect(TRADING_DATE)

        nasdaq = result["us_market"]["nasdaq"]
        if nasdaq != "N/A":
            assert re.match(r"^[+-]\d+\.\d+%$", nasdaq), f"포맷 불일치: {nasdaq}"

    @pytest.mark.integration
    def test_real_yfinance_call(self):
        """실제 yfinance 호출"""
        from collectors.market_global import collect
        result = collect(TRADING_DATE)

        assert isinstance(result, dict)
        print(f"\n  [market_global] 나스닥: {result['us_market']['nasdaq']}")
        print(f"  [market_global] S&P500: {result['us_market']['sp500']}")
        print(f"  [market_global] USD/KRW: {result.get('forex', {}).get('USD/KRW', 'N/A')}")
        print(f"  [market_global] 필터된 섹터 수: {len(result['us_market'].get('sectors', {}))}")


# ═══════════════════════════════════════════════════════════════════
# 5. news_global_rss — RSS/GDELT/NewsAPI 글로벌 뉴스
# ═══════════════════════════════════════════════════════════════════

class TestNewsGlobalRSS:
    """collectors/news_global_rss.py — collect()"""

    @pytest.mark.unit
    def test_returns_list_when_disabled(self):
        """GEOPOLITICS_ENABLED=false 이면 빈 리스트 반환"""
        import config
        from collectors.news_global_rss import collect

        with patch.object(config, "GEOPOLITICS_ENABLED", False):
            result = collect()
        assert result == [], "비활성 시 빈 리스트가 아님"

    @pytest.mark.unit
    def test_returns_list_type(self):
        """collect() 반환값이 list여야 한다"""
        import config
        from collectors.news_global_rss import collect

        mock_feed = MagicMock()
        mock_feed.entries = []
        mock_feed.bozo = False

        with patch.object(config, "GEOPOLITICS_ENABLED", True), \
             patch.object(config, "NEWSAPI_ENABLED", False), \
             patch("collectors.news_global_rss.requests.get") as mock_get, \
             patch("collectors.news_global_rss.feedparser.parse", return_value=mock_feed):
            mock_get.return_value.status_code = 200
            mock_get.return_value.content = b""
            result = collect()

        assert isinstance(result, list)

    @pytest.mark.unit
    def test_item_structure(self):
        """RSS 항목의 필수 키 검증"""
        import config
        from collectors.news_global_rss import collect

        entry = MagicMock()
        entry.title   = "Test News"
        entry.summary = "Test summary"
        entry.link    = "https://example.com/news/1"

        mock_feed = MagicMock()
        mock_feed.entries = [entry]
        mock_feed.bozo = False

        with patch.object(config, "GEOPOLITICS_ENABLED", True), \
             patch.object(config, "NEWSAPI_ENABLED", False), \
             patch("collectors.news_global_rss.requests.get") as mock_get, \
             patch("collectors.news_global_rss.feedparser.parse", return_value=mock_feed):
            mock_get.return_value.status_code = 200
            mock_get.return_value.content = b"<rss/>"
            result = collect()

        if result:
            item = result[0]
            required_keys = {"title", "summary", "link", "published", "source", "raw_text"}
            missing = required_keys - set(item.keys())
            assert not missing, f"필수 키 누락: {missing}"

    @pytest.mark.unit
    def test_duplicate_links_removed(self):
        """중복 URL은 제거되어야 한다"""
        import config
        from collectors.news_global_rss import collect

        entry = MagicMock()
        entry.title   = "Duplicate"
        entry.summary = ""
        entry.link    = "https://example.com/same"

        mock_feed = MagicMock()
        mock_feed.entries = [entry, entry]  # 같은 URL 2번
        mock_feed.bozo = False

        with patch.object(config, "GEOPOLITICS_ENABLED", True), \
             patch.object(config, "NEWSAPI_ENABLED", False), \
             patch("collectors.news_global_rss.requests.get") as mock_get, \
             patch("collectors.news_global_rss.feedparser.parse", return_value=mock_feed):
            mock_get.return_value.status_code = 200
            mock_get.return_value.content = b"<rss/>"
            result = collect()

        links = [r["link"] for r in result]
        assert len(links) == len(set(links)), "중복 URL이 제거되지 않음"

    @pytest.mark.integration
    def test_real_rss_call(self):
        """실제 RSS 호출 (AP News — 무료, 키 불필요)"""
        import config
        from collectors.news_global_rss import _fetch_rss

        result = _fetch_rss(
            name="ap_business",
            url="https://apnews.com/rss/apf-business",
            filter_keywords=[],
            max_items=5,
        )

        assert isinstance(result, list), "RSS 결과가 list가 아님"
        print(f"\n  [news_global_rss/AP] 수집: {len(result)}건")
        for item in result[:2]:
            print(f"    {item['title'][:60]}")


# ═══════════════════════════════════════════════════════════════════
# 6. news_naver — 네이버 API 기반 뉴스
# ═══════════════════════════════════════════════════════════════════

class TestNewsNaver:
    """collectors/news_naver.py — collect()"""

    @pytest.mark.unit
    def test_returns_dict_when_no_key(self):
        """네이버 API 키 없으면 빈 dict 구조 반환"""
        import config
        from collectors.news_naver import collect

        with patch.object(config, "NAVER_CLIENT_ID", None), \
             patch.object(config, "NAVER_CLIENT_SECRET", None):
            result = collect(TRADING_DATE)

        assert isinstance(result, dict)
        assert "reports"     in result
        assert "policy_news" in result
        assert result["reports"]     == []
        assert result["policy_news"] == []

    @pytest.mark.unit
    def test_returns_correct_structure(self):
        """네이버 API 정상 응답 시 구조 검증"""
        import config
        from collectors.news_naver import collect

        mock_articles = [
            {
                "title": "[키움증권] 삼성전자 목표가 상향 Buy 유지",
                "description": "목표주가 10만원 → 12만원",
                "link": "https://news.naver.com/1",
            }
        ]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"items": mock_articles}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(config, "NAVER_CLIENT_ID", "test_id"), \
             patch.object(config, "NAVER_CLIENT_SECRET", "test_secret"), \
             patch.object(config, "DATALAB_ENABLED", False), \
             patch("collectors.news_naver.requests.get", return_value=mock_resp):
            result = collect(TRADING_DATE)

        assert isinstance(result, dict)
        assert "reports"     in result
        assert "policy_news" in result
        assert isinstance(result["reports"],     list)
        assert isinstance(result["policy_news"], list)

    @pytest.mark.unit
    def test_report_structure(self):
        """리포트 항목 키 검증"""
        import config
        from collectors.news_naver import collect

        mock_articles = [{
            "title": "[NH투자증권] LG에너지솔루션 Buy 매수 유지 목표주가 상향",
            "description": "목표주가 60만원 상향. 실적 호조.",
            "link": "https://news.naver.com/1",
        }]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"items": mock_articles}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(config, "NAVER_CLIENT_ID", "test"), \
             patch.object(config, "NAVER_CLIENT_SECRET", "test"), \
             patch.object(config, "DATALAB_ENABLED", False), \
             patch("collectors.news_naver.requests.get", return_value=mock_resp):
            result = collect(TRADING_DATE)

        if result.get("reports"):
            item = result["reports"][0]
            assert "증권사" in item
            assert "종목명" in item
            assert "액션"   in item
            assert "내용"   in item

    @pytest.mark.requires_key
    @pytest.mark.skipif(
        not os.environ.get("NAVER_CLIENT_ID"),
        reason="NAVER_CLIENT_ID 환경변수 없음",
    )
    def test_real_naver_api(self):
        """실제 네이버 API 호출"""
        from collectors.news_naver import collect
        result = collect(TRADING_DATE)

        assert isinstance(result, dict)
        print(f"\n  [news_naver] 리포트: {len(result['reports'])}건")
        print(f"  [news_naver] 정책뉴스: {len(result['policy_news'])}건")


# ═══════════════════════════════════════════════════════════════════
# 7. news_newsapi — NewsAPI.org 기반 뉴스
# ═══════════════════════════════════════════════════════════════════

class TestNewsNewsAPI:
    """collectors/news_newsapi.py — collect()"""

    @pytest.mark.unit
    def test_returns_dict_when_disabled(self):
        """NEWSAPI_ENABLED=false 이면 빈 dict 반환"""
        import config
        from collectors.news_newsapi import collect

        with patch.object(config, "NEWSAPI_ENABLED", False):
            result = collect(TRADING_DATE)

        assert result == {"reports": [], "policy_news": []}

    @pytest.mark.unit
    def test_returns_correct_structure(self):
        """정상 응답 시 dict 구조 검증"""
        import config
        from collectors.news_newsapi import collect

        mock_articles = [{
            "title": "Samsung analyst target raised Buy",
            "description": "Analyst upgrades Samsung to Buy.",
            "url": "https://example.com/article/1",
            "source": {"name": "Reuters"},
            "publishedAt": "2024-01-10T09:00:00Z",
        }]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "articles": mock_articles}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(config, "NEWSAPI_ENABLED", True), \
             patch.object(config, "NEWSAPI_ORG_KEY", "test_key"), \
             patch("collectors.news_newsapi.requests.get", return_value=mock_resp):
            result = collect(TRADING_DATE)

        assert isinstance(result, dict)
        assert "reports"     in result
        assert "policy_news" in result

    @pytest.mark.unit
    def test_api_error_returns_empty(self):
        """NewsAPI 오류 응답 시 graceful 처리"""
        import config
        from collectors.news_newsapi import collect

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "error", "message": "Invalid API key"}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(config, "NEWSAPI_ENABLED", True), \
             patch.object(config, "NEWSAPI_ORG_KEY", "bad_key"), \
             patch("collectors.news_newsapi.requests.get", return_value=mock_resp):
            result = collect(TRADING_DATE)

        # 예외 없이 빈 결과 반환되어야 함
        assert isinstance(result, dict)

    @pytest.mark.requires_key
    @pytest.mark.skipif(
        not os.environ.get("NEWSAPI_ORG_KEY"),
        reason="NEWSAPI_ORG_KEY 환경변수 없음",
    )
    def test_real_newsapi_call(self):
        """실제 NewsAPI 호출"""
        from collectors.news_newsapi import collect
        result = collect(TRADING_DATE)
        assert isinstance(result, dict)
        print(f"\n  [news_newsapi] 리포트: {len(result['reports'])}건")
        print(f"  [news_newsapi] 정책뉴스: {len(result['policy_news'])}건")


# ═══════════════════════════════════════════════════════════════════
# 8. filings — DART 공시 수집
# ═══════════════════════════════════════════════════════════════════

class TestFilings:
    """collectors/filings.py — collect()"""

    @pytest.mark.unit
    def test_returns_list(self):
        """collect() 반환값이 list여야 한다"""
        import config
        from collectors.filings import collect

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "000",
            "list": [],
        }
        mock_resp.raise_for_status = MagicMock()

        with patch.object(config, "DART_API_KEY", "test_key"), \
             patch("collectors.filings.requests.get", return_value=mock_resp):
            result = collect(TRADING_DATE)

        assert isinstance(result, list)

    @pytest.mark.unit
    def test_result_structure(self):
        """반환 항목의 필수 키 검증"""
        import config
        from collectors.filings import collect

        dart_item = {
            "corp_name":  "테스트기업",
            "stock_code": "005930",
            "report_nm":  "단일판매공급계약체결",
            "corp_code":  "00123456",
            "rcept_no":   "20240110000001",
            "rcept_dt":   "20240110091500",
        }
        # 목록 응답
        mock_list = MagicMock()
        mock_list.json.return_value = {"status": "000", "list": [dart_item]}
        mock_list.raise_for_status = MagicMock()

        # 상세 API (piicDecsn) 응답 — 규모 필터 통과
        mock_detail = MagicMock()
        mock_detail.json.return_value = {
            "status": "000",
            "list": [{"selfCptlRatio": "25.5"}],
        }
        mock_detail.raise_for_status = MagicMock()

        # document API — 본문 요약
        mock_doc = MagicMock()
        mock_doc.status_code = 200
        mock_doc.headers = {"Content-Type": "application/json"}
        mock_doc.json.return_value = {"status": "013", "message": "no data"}

        with patch.object(config, "DART_API_KEY", "test_key"), \
             patch("collectors.filings.requests.get",
                   side_effect=[mock_list, mock_detail, mock_doc]):
            result = collect(TRADING_DATE)

        if result:
            item = result[0]
            required_keys = {"종목명", "종목코드", "공시종류", "핵심내용", "공시시각", "신뢰도", "규모", "본문요약", "rcept_no"}
            missing = required_keys - set(item.keys())
            assert not missing, f"필수 키 누락: {missing}"

    @pytest.mark.unit
    def test_keyword_filter_works(self):
        """DART_KEYWORDS에 없는 공시는 필터링되어야 한다"""
        import config
        from collectors.filings import collect

        # 필터에 없는 키워드
        dart_item = {
            "corp_name":  "테스트기업",
            "stock_code": "005930",
            "report_nm":  "임원변경",  # DART_KEYWORDS에 없음
            "corp_code":  "00123456",
            "rcept_no":   "20240110000001",
            "rcept_dt":   "20240110091500",
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "000", "list": [dart_item]}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(config, "DART_API_KEY", "test_key"), \
             patch("collectors.filings.requests.get", return_value=mock_resp):
            result = collect(TRADING_DATE)

        assert result == [], f"필터링 안 된 항목 있음: {result}"

    @pytest.mark.requires_key
    @pytest.mark.skipif(
        not os.environ.get("DART_API_KEY"),
        reason="DART_API_KEY 환경변수 없음",
    )
    def test_real_dart_api(self):
        """실제 DART API 호출"""
        from collectors.filings import collect
        result = collect(TRADING_DATE)
        assert isinstance(result, list)
        print(f"\n  [filings] DART 공시: {len(result)}건")
        for item in result[:3]:
            print(f"    {item['종목명']}: {item['공시종류']} ({item['규모']})")


# ═══════════════════════════════════════════════════════════════════
# 9. event_calendar — 기업 이벤트 캘린더
# ═══════════════════════════════════════════════════════════════════

class TestEventCalendar:
    """collectors/event_calendar.py — collect()"""

    @pytest.mark.unit
    def test_returns_list_when_disabled(self):
        """EVENT_CALENDAR_ENABLED=false 이면 빈 리스트"""
        import config
        from collectors.event_calendar import collect

        with patch.object(config, "EVENT_CALENDAR_ENABLED", False):
            result = collect(TRADING_DATE)

        assert result == []

    @pytest.mark.unit
    def test_returns_list_type(self):
        """collect() 반환값이 list여야 한다"""
        import config
        from collectors.event_calendar import collect

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "000", "list": []}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(config, "EVENT_CALENDAR_ENABLED", True), \
             patch.object(config, "DART_API_KEY", "test_key"), \
             patch("collectors.event_calendar.requests.get", return_value=mock_resp):
            result = collect(TRADING_DATE)

        assert isinstance(result, list)

    @pytest.mark.unit
    def test_event_structure(self):
        """이벤트 항목의 필수 키 검증"""
        import config
        from collectors.event_calendar import collect

        dart_item = {
            "corp_name":  "삼성전자",
            "stock_code": "005930",
            "report_nm":  "기업설명회(IR) 개최",
            "corp_code":  "00126380",
            "rcept_no":   "20240115000001",
            "rcept_dt":   str((TRADING_DATE + timedelta(days=3)).strftime("%Y%m%d")),
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "000", "list": [dart_item]}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(config, "EVENT_CALENDAR_ENABLED", True), \
             patch.object(config, "DART_API_KEY", "test_key"), \
             patch("collectors.event_calendar.requests.get", return_value=mock_resp):
            result = collect(TRADING_DATE)

        if result:
            item = result[0]
            required_keys = {"event_type", "corp_name", "ticker", "event_date", "days_until", "title", "rcept_no", "source"}
            missing = required_keys - set(item.keys())
            assert not missing, f"필수 키 누락: {missing}"

    @pytest.mark.unit
    def test_keyword_event_classification(self):
        """이벤트 유형이 올바르게 분류되어야 한다"""
        from collectors.event_calendar import _classify_event

        assert _classify_event("기업설명회(IR) 개최") == "IR"
        assert _classify_event("정기주주총회 소집 공고") == "주주총회"
        assert _classify_event("잠정실적 발표") == "실적발표"
        assert _classify_event("현금배당 결정") == "배당"
        assert _classify_event("임원 변경") is None

    @pytest.mark.requires_key
    @pytest.mark.skipif(
        not os.environ.get("DART_API_KEY"),
        reason="DART_API_KEY 환경변수 없음",
    )
    def test_real_dart_event_calendar(self):
        """실제 DART 이벤트 캘린더"""
        import config
        with patch.object(config, "EVENT_CALENDAR_ENABLED", True):
            from collectors.event_calendar import collect
            result = collect(TRADING_DATE)
        assert isinstance(result, list)
        print(f"\n  [event_calendar] 이벤트: {len(result)}건")


# ═══════════════════════════════════════════════════════════════════
# 10. price_domestic — pykrx 기반 국내 주가 수집
# ═══════════════════════════════════════════════════════════════════

class TestPriceDomestic:
    """collectors/price_domestic.py — collect_daily()"""

    @pytest.mark.unit
    def test_returns_dict_with_required_keys(self):
        """collect_daily() 반환 dict의 최상위 키 검증"""
        import pandas as pd
        from collectors.price_domestic import collect_daily

        mock_index = MagicMock()
        mock_index.empty = True

        mock_ohlcv = pd.DataFrame(
            {"종가": [70000], "등락률": [3.5], "거래량": [100000], "시가총액": [50_000_000_000]},
            index=["005930"],
        )

        with patch("collectors.price_domestic.pykrx_stock.get_index_ohlcv_by_date",
                   return_value=MagicMock(empty=True)), \
             patch("collectors.price_domestic.pykrx_stock.get_market_ohlcv_by_ticker",
                   return_value=mock_ohlcv), \
             patch("collectors.price_domestic.pykrx_stock.get_market_ticker_name",
                   return_value="삼성전자"), \
             patch("collectors.price_domestic.pykrx_stock.get_market_sector_classifications",
                   return_value=MagicMock(empty=True)):
            result = collect_daily(TRADING_DATE)

        assert isinstance(result, dict)
        required_keys = {"date", "kospi", "kosdaq", "upper_limit", "top_gainers",
                         "top_losers", "institutional", "short_selling", "by_name",
                         "by_code", "by_sector"}
        missing = required_keys - set(result.keys())
        assert not missing, f"최상위 키 누락: {missing}"

    @pytest.mark.unit
    def test_stock_entry_structure(self):
        """개별 종목 엔트리의 필수 키 검증"""
        import pandas as pd
        from collectors.price_domestic import collect_daily

        mock_ohlcv = pd.DataFrame(
            {"종가": [70000], "등락률": [3.5], "거래량": [100000], "시가총액": [50_000_000_000]},
            index=["005930"],
        )

        with patch("collectors.price_domestic.pykrx_stock.get_index_ohlcv_by_date",
                   return_value=MagicMock(empty=True)), \
             patch("collectors.price_domestic.pykrx_stock.get_market_ohlcv_by_ticker",
                   return_value=mock_ohlcv), \
             patch("collectors.price_domestic.pykrx_stock.get_market_ticker_name",
                   return_value="삼성전자"), \
             patch("collectors.price_domestic.pykrx_stock.get_market_sector_classifications",
                   return_value=MagicMock(empty=True)), \
             patch("collectors.price_domestic.pykrx_stock.get_market_trading_value_by_date",
                   return_value=MagicMock(empty=True)):
            result = collect_daily(TRADING_DATE)

        if result["by_code"]:
            entry = next(iter(result["by_code"].values()))
            required_keys = {"종목코드", "종목명", "등락률", "거래량", "종가", "시가총액", "시장"}
            missing = required_keys - set(entry.keys())
            assert not missing, f"종목 엔트리 키 누락: {missing}"

    @pytest.mark.integration
    def test_real_pykrx_price(self):
        """실제 pykrx 가격 데이터 수집"""
        from collectors.price_domestic import collect_daily
        result = collect_daily(TRADING_DATE)

        assert isinstance(result, dict)
        total = len(result.get("by_code", {}))
        print(f"\n  [price_domestic] 총 종목: {total}개")
        print(f"  [price_domestic] 상한가: {len(result['upper_limit'])}개")
        print(f"  [price_domestic] 급등(15%+): {len(result['top_gainers'])}개")
        print(f"  [price_domestic] KOSPI: {result.get('kospi', {})}")


# ═══════════════════════════════════════════════════════════════════
# 11. sector_etf — KODEX 섹터 ETF 수집
# ═══════════════════════════════════════════════════════════════════

class TestSectorETF:
    """collectors/sector_etf.py — collect()"""

    @pytest.mark.unit
    def test_returns_list_when_disabled(self):
        """SECTOR_ETF_ENABLED=false 이면 빈 리스트"""
        import config
        from collectors.sector_etf import collect

        with patch.object(config, "SECTOR_ETF_ENABLED", False):
            result = collect(TRADING_DATE)

        assert result == []

    @pytest.mark.unit
    def test_returns_list_type(self):
        """collect() 반환값이 list여야 한다"""
        import config, pandas as pd
        from collectors.sector_etf import collect

        mock_df = pd.DataFrame(
            {"종가": [29000], "거래량": [1000000], "거래대금": [29_000_000_000], "등락률": [1.5]},
            index=[pd.Timestamp(TRADING_DATE.strftime("%Y-%m-%d"))],
        )

        with patch.object(config, "SECTOR_ETF_ENABLED", True), \
             patch("collectors.sector_etf.pykrx_stock.get_market_ohlcv",
                   return_value=mock_df):
            result = collect(TRADING_DATE)

        assert isinstance(result, list)

    @pytest.mark.unit
    def test_etf_item_structure(self):
        """ETF 항목의 필수 키 검증"""
        import config, pandas as pd
        from collectors.sector_etf import collect

        dates = [
            pd.Timestamp((TRADING_DATE - timedelta(days=1)).strftime("%Y-%m-%d")),
            pd.Timestamp(TRADING_DATE.strftime("%Y-%m-%d")),
        ]
        mock_df = pd.DataFrame(
            {
                "종가":    [28000, 29000],
                "거래량":  [900000, 1100000],
                "거래대금":[25_000_000_000, 31_000_000_000],
                "등락률":  [0.5, 1.5],
            },
            index=dates,
        )

        with patch.object(config, "SECTOR_ETF_ENABLED", True), \
             patch("collectors.sector_etf.pykrx_stock.get_market_ohlcv",
                   return_value=mock_df):
            result = collect(TRADING_DATE)

        if result:
            item = result[0]
            required_keys = {"ticker", "name", "sector", "close", "volume", "volume_ratio", "value", "change_pct", "신뢰도"}
            missing = required_keys - set(item.keys())
            assert not missing, f"ETF 항목 키 누락: {missing}"

    @pytest.mark.integration
    def test_real_sector_etf(self):
        """실제 pykrx 섹터 ETF 수집"""
        from collectors.sector_etf import collect
        result = collect(TRADING_DATE)
        assert isinstance(result, list)
        print(f"\n  [sector_etf] 수집: {len(result)}개 ETF")
        for item in result[:3]:
            print(f"    {item['name']}: 등락={item['change_pct']:+.2f}%, 거래량배율={item['volume_ratio']:.2f}x")


# ═══════════════════════════════════════════════════════════════════
# 12. short_interest — 공매도 잔고 수집
# ═══════════════════════════════════════════════════════════════════

class TestShortInterest:
    """collectors/short_interest.py — collect()"""

    @pytest.mark.unit
    def test_returns_list_when_disabled(self):
        """SHORT_INTEREST_ENABLED=false 이면 빈 리스트"""
        import config
        from collectors.short_interest import collect

        with patch.object(config, "SHORT_INTEREST_ENABLED", False):
            result = collect(TRADING_DATE)

        assert result == []

    @pytest.mark.unit
    def test_signal_classification(self):
        """공매도 신호 분류 로직 검증"""
        from collectors.short_interest import _classify_signal

        # 쇼트커버링 예고: 잔고 -30% 이상 감소
        item_covering = {
            "balance": 700, "balance_prev": 1000, "balance_chg_pct": -30.0,
            "short_volume": 5000, "short_volume_prev": 6000,
        }
        assert _classify_signal(item_covering) == "쇼트커버링_예고"

        # 공매도 급감: 전일 대비 -50% 이상
        item_decline = {
            "balance": 1000, "balance_prev": 0, "balance_chg_pct": 0.0,
            "short_volume": 200, "short_volume_prev": 600,  # -66%
        }
        assert _classify_signal(item_decline) == "공매도급감"

        # 신호 없음
        item_none = {
            "balance": 1000, "balance_prev": 1000, "balance_chg_pct": 0.0,
            "short_volume": 1000, "short_volume_prev": 900,
        }
        assert _classify_signal(item_none) == ""

    @pytest.mark.integration
    def test_real_short_interest_enabled(self):
        """SHORT_INTEREST_ENABLED=true 로 실제 pykrx 호출"""
        import config
        from collectors.short_interest import collect

        with patch.object(config, "SHORT_INTEREST_ENABLED", True):
            result = collect(TRADING_DATE, top_n=10)

        assert isinstance(result, list)
        print(f"\n  [short_interest] 공매도 급감 신호: {len(result)}종목")
        for item in result[:3]:
            print(f"    ticker={item['ticker']}, 신호={item['signal']}, 비율={item['short_ratio']:.2f}%")


# ═══════════════════════════════════════════════════════════════════
# 13. data_collector — 수집 총괄
# ═══════════════════════════════════════════════════════════════════

class TestDataCollector:
    """collectors/data_collector.py — run(), get_cache(), is_fresh()"""

    @pytest.mark.unit
    def test_get_cache_returns_dict(self):
        """run() 전 get_cache()는 빈 dict여야 한다"""
        import importlib
        import collectors.data_collector as dc
        importlib.reload(dc)  # _cache 초기화

        result = dc.get_cache()
        assert isinstance(result, dict)

    @pytest.mark.unit
    def test_is_fresh_false_when_no_cache(self):
        """캐시 없을 때 is_fresh()는 False"""
        import importlib
        import collectors.data_collector as dc
        importlib.reload(dc)

        assert dc.is_fresh() is False

    @pytest.mark.unit
    def test_cache_structure_after_run(self):
        """run() 후 캐시의 최상위 키 검증"""
        import asyncio
        import collectors.data_collector as dc

        # 모든 수집기를 None 반환으로 mock
        async def mock_safe_collect(name, fn, *args):
            return None

        with patch.object(dc, "_safe_collect", side_effect=mock_safe_collect), \
             patch("collectors.data_collector._send_raw_data_to_telegram"):
            cache = asyncio.run(dc.run())

        required_keys = {
            "collected_at", "dart_data", "market_data", "news_naver",
            "news_newsapi", "news_global_rss", "price_data", "sector_etf_data",
            "short_data", "event_calendar", "closing_strength_result",
            "volume_surge_result", "fund_concentration_result", "success_flags",
        }
        missing = required_keys - set(cache.keys())
        assert not missing, f"캐시 키 누락: {missing}"

    @pytest.mark.unit
    def test_success_flags_all_false_when_all_fail(self):
        """모든 수집기 실패 시 success_flags가 모두 False여야 한다"""
        import asyncio
        import collectors.data_collector as dc

        async def mock_safe_collect(name, fn, *args):
            return None

        with patch.object(dc, "_safe_collect", side_effect=mock_safe_collect), \
             patch("collectors.data_collector._send_raw_data_to_telegram"):
            cache = asyncio.run(dc.run())

        flags = cache["success_flags"]
        assert isinstance(flags, dict)
        # price_data가 None이면 False, 나머지 빈 리스트/dict도 False
        false_flags = [k for k, v in flags.items() if not v]
        print(f"\n  [data_collector] 실패 플래그: {false_flags}")
        assert len(false_flags) == len(flags), f"일부 플래그가 True임: {[k for k,v in flags.items() if v]}"

    @pytest.mark.unit
    def test_is_fresh_true_after_run(self):
        """run() 직후 is_fresh()는 True여야 한다"""
        import asyncio
        import collectors.data_collector as dc

        async def mock_safe_collect(name, fn, *args):
            return None

        with patch.object(dc, "_safe_collect", side_effect=mock_safe_collect), \
             patch("collectors.data_collector._send_raw_data_to_telegram"):
            asyncio.run(dc.run())

        assert dc.is_fresh(max_age_minutes=180) is True


# ═══════════════════════════════════════════════════════════════════
# 통합 실행 테스트 — 전체 파이프라인 smoke test
# ═══════════════════════════════════════════════════════════════════

class TestCollectorsPipeline:
    """모든 수집기를 한 번에 smoke-test"""

    @pytest.mark.unit
    def test_all_collectors_import_without_error(self):
        """모든 수집기 모듈이 import 오류 없어야 한다"""
        modules = [
            "collectors.closing_strength",
            "collectors.data_collector",
            "collectors.event_calendar",
            "collectors.filings",
            "collectors.fund_concentration",
            "collectors.market_global",
            "collectors.news_global_rss",
            "collectors.news_naver",
            "collectors.news_newsapi",
            "collectors.price_domestic",
            "collectors.sector_etf",
            "collectors.short_interest",
            "collectors.volume_surge",
        ]
        for mod in modules:
            try:
                __import__(mod)
            except ImportError as e:
                pytest.fail(f"{mod} import 실패: {e}")

    @pytest.mark.integration
    def test_free_sources_complete_pipeline(self):
        """
        API 키 불필요한 무료 소스 전체 실행
        - pykrx: closing_strength, volume_surge, fund_concentration, sector_etf
        - yfinance: market_global (US markets)
        - RSS: news_global_rss (AP News)
        """
        from collectors.closing_strength  import analyze as cs_analyze
        from collectors.volume_surge      import analyze as vs_analyze
        from collectors.fund_concentration import analyze as fc_analyze
        from collectors.market_global     import collect as mg_collect
        from collectors.sector_etf        import collect as etf_collect

        print(f"\n  === 무료 소스 통합 테스트 (기준일: {TRADING_DATE_STR}) ===")

        results = {}

        # closing_strength
        cs = cs_analyze(TRADING_DATE_STR, top_n=5)
        results["closing_strength"] = len(cs)
        print(f"  closing_strength:   {len(cs)}종목")

        # volume_surge
        vs = vs_analyze(TRADING_DATE_STR, top_n=5)
        results["volume_surge"] = len(vs)
        print(f"  volume_surge:       {len(vs)}종목")

        # fund_concentration
        fc = fc_analyze(TRADING_DATE_STR, top_n=5)
        results["fund_concentration"] = len(fc)
        print(f"  fund_concentration: {len(fc)}종목")

        # market_global (yfinance)
        mg = mg_collect(TRADING_DATE)
        results["market_global"] = mg["us_market"]["nasdaq"] != "N/A"
        print(f"  market_global:      나스닥={mg['us_market']['nasdaq']}")

        # sector_etf
        etf = etf_collect(TRADING_DATE)
        results["sector_etf"] = len(etf)
        print(f"  sector_etf:         {len(etf)}개 ETF")

        # 최소 하나라도 데이터가 있어야 pass
        assert any([
            results["closing_strength"] > 0,
            results["volume_surge"] > 0,
            results["fund_concentration"] > 0,
            results["market_global"],
            results["sector_etf"] > 0,
        ]), "모든 무료 소스에서 데이터 수집 실패"
