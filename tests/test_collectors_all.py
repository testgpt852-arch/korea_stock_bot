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


# ═══════════════════════════════════════════════════════════════════════
# ★★ 보완 테스트 — 전수 조사 후 기존 테스트에서 누락된 소스 전체 커버 ★★
#
# 누락 분류:
#   A. news_global_rss  — RSS 15개 개별 / Google News 4쿼리 / GDELT 8쿼리 / NewsAPI 지정학 10쿼리 / collect() 전체
#   B. market_global    — 원자재 5종 개별 / USD/KRW 환율 / 미국 섹터ETF 8개 개별 / _collect_* 서브함수
#   C. news_naver       — DataLab 8테마 / _extract_brokerage / _extract_action / _extract_stock_name
#   D. news_newsapi     — _extract_english_stock / _extract_english_action / 쿼리 개별
#   E. filings          — _parse_number / _parse_document_text 유형별 / piicDecsn / alotMatter / page2 fallback
#   F. price_domestic   — collect_supply() 완전 누락 / _fetch_index / _fetch_sector_map / _fetch_institutional / _fetch_short_selling / _find_col
#   G. sector_etf       — ETF 11개 개별 / 정렬 순서 검증
#   H. short_interest   — _fetch_short_volume 구조 / _fetch_balance_safe / 경계값
#   I. event_calendar   — _classify_event 전체 유형 / _parse_date 유틸 / KRX KIND 소스
# ═══════════════════════════════════════════════════════════════════════


# ───────────────────────────────────────────────────────────────────────
# A. news_global_rss — 전체 소스 보완
# ───────────────────────────────────────────────────────────────────────

class TestNewsGlobalRSS_AllSources:
    """RSS 15개 개별 + Google News + GDELT + NewsAPI 지정학 + collect() 전체"""

    @pytest.mark.integration
    @pytest.mark.parametrize("source_name,url", [
        ("ap_business",    "https://apnews.com/rss/apf-business"),
        ("ap_world",       "https://apnews.com/rss/apf-topnews"),
        ("ft_markets",     "https://www.ft.com/markets?format=rss"),
        ("kr_pressrelease","https://www.korea.kr/rss/pressrelease.xml"),
        ("kr_ebriefing",   "https://www.korea.kr/rss/ebriefing.xml"),
        ("moef",           "https://www.korea.kr/rss/dept_moef.xml"),
        ("motir",          "https://www.korea.kr/rss/dept_motir.xml"),
        ("fsc",            "https://www.korea.kr/rss/dept_fsc.xml"),
        ("msit",           "https://www.korea.kr/rss/dept_msit.xml"),
        ("dapa",           "https://www.korea.kr/rss/dept_dapa.xml"),
        ("mnd",            "https://www.korea.kr/rss/dept_mnd.xml"),
        ("unikorea",       "https://www.korea.kr/rss/dept_unikorea.xml"),
        ("ftc",            "https://www.korea.kr/rss/dept_ftc.xml"),
        ("mss",            "https://www.korea.kr/rss/dept_mss.xml"),
        ("mofa",           "https://www.korea.kr/rss/dept_mofa.xml"),
    ])
    def test_each_rss_source(self, source_name, url):
        """RSS 소스 15개 개별 — 접속 가능 여부 + 항목 구조 검증"""
        from collectors.news_global_rss import _fetch_rss
        result = _fetch_rss(name=source_name, url=url, filter_keywords=[], max_items=3)

        assert isinstance(result, list), f"{source_name}: list가 아닌 타입"
        print(f"\n  [{source_name}] {len(result)}건")
        for item in result[:1]:
            required = {"title", "summary", "link", "published", "source", "raw_text"}
            missing = required - set(item.keys())
            assert not missing, f"{source_name} 항목 키 누락: {missing}"
            assert item["source"] == source_name, f"source 필드 불일치"

    @pytest.mark.integration
    def test_google_news_rss_4queries(self):
        """Google News RSS — 4개 쿼리 모두 실행"""
        from collectors.news_global_rss import _fetch_google_news_rss, _GOOGLE_NEWS_QUERIES
        result = _fetch_google_news_rss(max_per_query=2)

        assert isinstance(result, list)
        print(f"\n  [google_news] {len(result)}건 (쿼리 {len(_GOOGLE_NEWS_QUERIES)}개)")
        if result:
            assert "title"  in result[0]
            assert "source" in result[0]
            assert result[0]["source"] == "google_news"

    @pytest.mark.integration
    def test_gdelt_8queries(self):
        """GDELT API — 8개 쿼리, API키 불필요"""
        from collectors.news_global_rss import _fetch_gdelt_geopolitics, _GDELT_GEO_QUERIES
        result = _fetch_gdelt_geopolitics(max_per_query=2)

        assert isinstance(result, list)
        print(f"\n  [gdelt] {len(result)}건 (쿼리 {len(_GDELT_GEO_QUERIES)}개)")
        if result:
            required = {"title", "summary", "link", "published", "source", "raw_text"}
            missing = required - set(result[0].keys())
            assert not missing, f"GDELT 항목 키 누락: {missing}"
            assert result[0]["source"].startswith("gdelt_"), f"source 형식 오류: {result[0]['source']}"

    @pytest.mark.requires_key
    @pytest.mark.skipif(not os.environ.get("NEWSAPI_ORG_KEY"), reason="NEWSAPI_ORG_KEY 없음")
    def test_newsapi_geo_10queries(self):
        """NewsAPI 지정학 10쿼리 — NEWSAPI_ORG_KEY 필요"""
        from collectors.news_global_rss import _fetch_newsapi_geopolitics, _NEWSAPI_GEO_QUERIES
        result = _fetch_newsapi_geopolitics(max_per_query=2)

        assert isinstance(result, list)
        print(f"\n  [newsapi_geo] {len(result)}건 (쿼리 {len(_NEWSAPI_GEO_QUERIES)}개)")
        if result:
            assert result[0]["source"].startswith("newsapi_")

    @pytest.mark.integration
    def test_collect_geopolitics_enabled_full(self):
        """collect() — GEOPOLITICS_ENABLED=True 전체 실행 + 중복 제거 검증"""
        import config
        from collectors.news_global_rss import collect

        with patch.object(config, "GEOPOLITICS_ENABLED", True), \
             patch.object(config, "NEWSAPI_ENABLED", False):
            result = collect()

        assert isinstance(result, list)
        links = [r["link"] for r in result if r.get("link")]
        assert len(links) == len(set(links)), "collect() 결과에 중복 URL 있음"
        print(f"\n  [collect/전체] {len(result)}건, 중복 없음 확인")

    @pytest.mark.unit
    def test_parse_published_fallback(self):
        """_parse_published() — published_parsed 없을 때 현재시각 fallback"""
        from collectors.news_global_rss import _parse_published
        entry = MagicMock(spec=[])  # 아무 속성도 없는 mock
        result = _parse_published(entry)
        assert isinstance(result, str) and len(result) > 0, "fallback이 빈 문자열"

    @pytest.mark.unit
    def test_fetch_rss_filter_keywords(self):
        """_fetch_rss() — filter_keywords 있으면 미포함 항목 제외"""
        from collectors.news_global_rss import _fetch_rss

        mock_entries = []
        for title in ["Korea tariff news", "unrelated sports story", "NATO Korea defense"]:
            e = MagicMock()
            e.title   = title
            e.summary = ""
            e.link    = f"http://example.com/{title[:5]}"
            e.get     = lambda k, default="": default
            mock_entries.append(e)

        mock_feed = MagicMock()
        mock_feed.entries = mock_entries
        mock_feed.bozo    = False

        with patch("collectors.news_global_rss.requests.get") as mock_get, \
             patch("collectors.news_global_rss.feedparser.parse", return_value=mock_feed):
            mock_get.return_value.content = b""
            mock_get.return_value.raise_for_status = MagicMock()
            result = _fetch_rss("test", "http://x.com", filter_keywords=["Korea", "NATO"], max_items=10)

        titles = [r["title"] for r in result]
        assert "unrelated sports story" not in titles, "필터 키워드 없는 항목이 포함됨"
        assert "Korea tariff news" in titles or "NATO Korea defense" in titles, "필터 키워드 있는 항목이 제외됨"


# ───────────────────────────────────────────────────────────────────────
# B. market_global — 원자재/환율/섹터 보완
# ───────────────────────────────────────────────────────────────────────

class TestMarketGlobal_AllSources:
    """원자재 5종 개별 / USD/KRW 환율 / 미국 섹터ETF 8개 / 서브함수 개별"""

    @pytest.mark.integration
    @pytest.mark.parametrize("key,ticker", [
        ("copper",   "HG=F"),
        ("silver",   "SI=F"),
        ("gas",      "NG=F"),
        ("steel",    "TIO=F"),
        ("aluminum", "ALI=F"),
    ])
    def test_each_commodity(self, key, ticker):
        """원자재 5종 개별 yfinance 수집"""
        import yfinance as yf
        data = yf.Ticker(ticker).history(period="5d")
        if data.empty or len(data) < 2:
            pytest.skip(f"{ticker} 데이터 없음 (선물 만기 등)")

        prev = float(data["Close"].iloc[-2])
        last = float(data["Close"].iloc[-1])
        pct  = (last - prev) / prev * 100
        print(f"\n  [{key}/{ticker}] 현재={last:.3f}, 등락={pct:+.2f}%")
        assert isinstance(pct, float)

    @pytest.mark.integration
    def test_collect_commodities_structure(self):
        """_collect_commodities() — 5종 수집 후 ±1.5% 필터 구조 검증"""
        from collectors.market_global import _collect_commodities
        result = _collect_commodities()

        assert isinstance(result, dict)
        print(f"\n  [commodities] 필터 통과: {list(result.keys())}")
        for key, data in result.items():
            assert "price"  in data, f"{key}: price 키 없음"
            assert "change" in data, f"{key}: change 키 없음"
            assert "unit"   in data, f"{key}: unit 키 없음"
            assert "신뢰도" in data, f"{key}: 신뢰도 키 없음"

    @pytest.mark.integration
    def test_collect_forex_usd_krw(self):
        """_collect_forex() — USD/KRW 환율 수집"""
        from collectors.market_global import _collect_forex
        result = _collect_forex()

        assert isinstance(result, dict)
        print(f"\n  [forex] {result}")
        if result:
            assert "USD/KRW" in result
            usd_krw = result["USD/KRW"]
            assert isinstance(usd_krw, float)
            assert 900 < usd_krw < 2000, f"USD/KRW 비현실적 값: {usd_krw}"

    @pytest.mark.integration
    @pytest.mark.parametrize("ticker,sector_name", [
        ("XLK", "기술/반도체"),
        ("XLE", "에너지/정유"),
        ("XLB", "소재/화학"),
        ("XLI", "산업재/방산"),
        ("XLV", "바이오/헬스케어"),
        ("XLF", "금융"),
        ("XME", "철강/비철금속"),
        ("SLX", "철강"),
    ])
    def test_each_us_sector_etf(self, ticker, sector_name):
        """미국 섹터ETF 8개 개별 yfinance 수집"""
        import yfinance as yf
        data = yf.Ticker(ticker).history(period="5d")
        if data.empty or len(data) < 2:
            pytest.skip(f"{ticker} 데이터 없음")
        prev = float(data["Close"].iloc[-2])
        last = float(data["Close"].iloc[-1])
        pct  = (last - prev) / prev * 100
        print(f"\n  [{ticker}/{sector_name}] 등락={pct:+.2f}%")
        assert isinstance(pct, float)

    @pytest.mark.integration
    def test_collect_sectors_structure(self):
        """_collect_sectors() — ±2% 필터 후 구조 검증"""
        from collectors.market_global import _collect_sectors
        result = _collect_sectors()

        assert isinstance(result, dict)
        print(f"\n  [sectors] 필터 통과: {list(result.keys())}")
        for name, data in result.items():
            assert "change" in data
            assert "신뢰도" in data

    @pytest.mark.unit
    def test_empty_result_structure(self):
        """_empty_result() — 모든 필수 키 포함 확인"""
        from collectors.market_global import _empty_result
        result = _empty_result()

        assert "us_market"   in result
        assert "commodities" in result
        assert "forex"       in result
        assert "copper" in result["commodities"]
        assert "silver" in result["commodities"]
        assert "gas"    in result["commodities"]

    @pytest.mark.unit
    def test_fetch_change_returns_na_on_insufficient_data(self):
        """_fetch_change() — 데이터 2개 미만이면 N/A 반환"""
        import pandas as pd
        from collectors.market_global import _fetch_change

        mock_hist = pd.DataFrame({"Close": [100.0]})  # 1개만
        with patch("collectors.market_global.yf.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = mock_hist
            result = _fetch_change(["^IXIC"])

        assert result == "N/A", f"데이터 부족 시 N/A가 아님: {result}"


# ───────────────────────────────────────────────────────────────────────
# C. news_naver — 추출 로직 + DataLab 보완
# ───────────────────────────────────────────────────────────────────────

class TestNewsNaver_ExtractLogic:
    """_extract_brokerage / _extract_action / _extract_stock_name / DataLab"""

    @pytest.mark.unit
    @pytest.mark.parametrize("text,expected", [
        ("키움증권 삼성전자 목표가 상향",    "키움"),
        ("NH투자증권 SK하이닉스 리포트",     "NH투자"),
        ("미래에셋증권 Buy 유지",            "미래에셋"),
        ("하나증권 목표주가 하향",           "하나증권"),
        ("알 수 없는 출처 기사",             "증권사미상"),
    ])
    def test_extract_brokerage(self, text, expected):
        from collectors.news_naver import _extract_brokerage
        result = _extract_brokerage(text)
        assert result == expected, f"'{text}' → 예상 '{expected}', 실제 '{result}'"

    @pytest.mark.unit
    @pytest.mark.parametrize("text,expected", [
        ("목표가 상향 조정",              "목표가상향"),
        ("목표주가 상향",                 "목표가상향"),
        ("신규 매수 의견",                "신규매수"),
        ("커버리지 개시 Buy",             "신규매수"),
        ("투자의견 매수 유지",            "매수유지"),       # 매수유지 키워드 우선 검사로 수정됨
        ("목표가 하향",                   "목표가하향"),
        ("TP 하향 조정",                  "목표가하향"),
        ("단순 언급 기사",                "언급"),
    ])
    def test_extract_action(self, text, expected):
        from collectors.news_naver import _extract_action
        result = _extract_action(text)
        assert result == expected, f"'{text}' → 예상 '{expected}', 실제 '{result}'"

    @pytest.mark.unit
    @pytest.mark.parametrize("title,bad_result", [
        ("키움증권 삼성전자 목표가 상향", "키움"),         # 증권사명이 종목명으로 추출되면 안 됨
        ("NH투자증권 LG에너지솔루션 리포트", "NH투자"),
    ])
    def test_extract_stock_name_not_brokerage(self, title, bad_result):
        """종목명 추출 시 증권사명이 종목명으로 잘못 추출되지 않아야 함"""
        from collectors.news_naver import _extract_stock_name
        result = _extract_stock_name(title, title)
        assert result != bad_result, f"증권사명 '{bad_result}'이 종목명으로 잘못 추출됨"
        print(f"\n  [stock_name] '{title}' → '{result}'")

    @pytest.mark.unit
    def test_blacklist_keyword_filter(self):
        """BLACKLIST_KEYWORDS 포함 기사는 수집에서 제외되어야 함"""
        from collectors.news_naver import BLACKLIST_KEYWORDS
        test_titles = ["삼성전자 주가전망 2025", "총정리 증권사 리포트", "배당금 계산기"]
        for title in test_titles:
            has_blacklist = any(kw in title for kw in BLACKLIST_KEYWORDS)
            assert has_blacklist, f"'{title}'이 블랙리스트에서 누락됨"

    @pytest.mark.requires_key
    @pytest.mark.skipif(not os.environ.get("NAVER_CLIENT_ID"), reason="NAVER_CLIENT_ID 없음")
    def test_datalab_trends_enabled(self):
        """DataLab 8테마 트렌드 수집 — DATALAB_ENABLED=True"""
        import config
        from collectors.news_naver import _collect_datalab_trends

        with patch.object(config, "DATALAB_ENABLED", True):
            result = _collect_datalab_trends()

        assert isinstance(result, list)
        print(f"\n  [datalab] 급등 테마: {len(result)}개")
        for item in result[:3]:
            assert "keyword" in item
            assert "ratio"   in item
            assert "theme"   in item
            print(f"    {item['theme']}: ratio={item['ratio']:.2f}")

    @pytest.mark.requires_key
    @pytest.mark.skipif(not os.environ.get("NAVER_CLIENT_ID"), reason="NAVER_CLIENT_ID 없음")
    def test_collect_reports_real_call(self):
        """_collect_reports() 실제 네이버 뉴스 검색 결과 구조"""
        from collectors.news_naver import _collect_reports
        from utils.date_utils import fmt_kr
        result = _collect_reports(fmt_kr(TRADING_DATE))

        assert isinstance(result, list)
        print(f"\n  [naver_reports] {len(result)}건")
        if result:
            assert "증권사" in result[0]
            assert "종목명" in result[0]
            assert "액션"   in result[0]

    @pytest.mark.requires_key
    @pytest.mark.skipif(not os.environ.get("NAVER_CLIENT_ID"), reason="NAVER_CLIENT_ID 없음")
    def test_collect_policy_news_real_call(self):
        """_collect_policy_news() 실제 네이버 뉴스 검색 결과 구조"""
        from collectors.news_naver import _collect_policy_news
        from utils.date_utils import fmt_kr
        result = _collect_policy_news(fmt_kr(TRADING_DATE))

        assert isinstance(result, list)
        print(f"\n  [naver_policy] {len(result)}건")
        if result:
            assert "제목" in result[0]
            assert "내용" in result[0]


# ───────────────────────────────────────────────────────────────────────
# D. news_newsapi — 추출 로직 + 쿼리 개별
# ───────────────────────────────────────────────────────────────────────

class TestNewsNewsAPI_ExtractLogic:
    """_extract_english_stock / _extract_english_action / 쿼리 개별 실행"""

    @pytest.mark.unit
    @pytest.mark.parametrize("title,expected_contains", [
        ("Samsung Electronics analyst raises target",   "Samsung"),
        ("SK Hynix buy rating upgrade forecast",        "SK Hynix"),    # 수정됨: 기존엔 "SK"만 추출
        ("KOSPI market outlook positive today",         "KOSPI"),
        ("Apple earnings beat expectations",            "글로벌종목"),
    ])
    def test_extract_english_stock(self, title, expected_contains):
        from collectors.news_newsapi import _extract_english_stock
        result = _extract_english_stock(title)
        assert expected_contains in result or result == expected_contains, \
            f"'{title}' → 예상 '{expected_contains}' 포함, 실제 '{result}'"

    @pytest.mark.unit
    @pytest.mark.parametrize("text,expected", [
        ("analyst upgrades buy raised target price",    "목표가상향"),
        ("initiates coverage outperform new buy",       "신규매수"),
        ("downgrades to sell underperform lower",       "목표가하향"),
        ("maintain buy rating reiterate hold",          "매수유지"),     # 수정됨: 기존엔 목표가상향 오분류
        ("reports quarterly earnings revenue",          "언급"),
    ])
    def test_extract_english_action(self, text, expected):
        from collectors.news_newsapi import _extract_english_action
        result = _extract_english_action(text)
        assert result == expected, f"'{text}' → 예상 '{expected}', 실제 '{result}'"

    @pytest.mark.requires_key
    @pytest.mark.skipif(not os.environ.get("NEWSAPI_ORG_KEY"), reason="NEWSAPI_ORG_KEY 없음")
    def test_newsapi_report_queries_real(self):
        """리포트 쿼리 3개 실제 NewsAPI 호출"""
        import config
        from collectors.news_newsapi import _collect_newsapi_reports
        with patch.object(config, "NEWSAPI_ENABLED", True):
            result = _collect_newsapi_reports()

        assert isinstance(result, list)
        print(f"\n  [newsapi_reports] {len(result)}건")
        if result:
            assert "증권사" in result[0]
            assert "종목명" in result[0]
            assert "액션"   in result[0]
            assert "내용"   in result[0]

    @pytest.mark.requires_key
    @pytest.mark.skipif(not os.environ.get("NEWSAPI_ORG_KEY"), reason="NEWSAPI_ORG_KEY 없음")
    def test_newsapi_market_queries_real(self):
        """시황 쿼리 4개 실제 NewsAPI 호출"""
        import config
        from collectors.news_newsapi import _collect_newsapi_global_market
        with patch.object(config, "NEWSAPI_ENABLED", True):
            result = _collect_newsapi_global_market()

        assert isinstance(result, list)
        print(f"\n  [newsapi_market] {len(result)}건")
        if result:
            assert "제목" in result[0]
            assert "내용" in result[0]
            assert "발행" in result[0]

    @pytest.mark.unit
    def test_newsapi_dedup_titles(self):
        """collect() 결과 제목 중복 제거 검증"""
        import config
        from collectors.news_newsapi import collect

        mock_articles = [{"title": "Same Title", "description": "desc", "url": f"http://{i}.com",
                          "source": {"name": "Test"}, "publishedAt": "2025-01-01T00:00:00Z"}
                         for i in range(3)]  # 같은 제목 3개

        def mock_search(q, page_size=5):
            return mock_articles

        with patch.object(config, "NEWSAPI_ENABLED", True), \
             patch.object(config, "NEWSAPI_ORG_KEY", "test_key"), \
             patch("collectors.news_newsapi._newsapi_search", side_effect=mock_search):
            result = collect()

        titles = [r.get("내용", r.get("제목", ""))[:60] for r in result.get("reports", [])]
        assert len(titles) == len(set(titles)), "reports에 중복 제목이 있음"


# ───────────────────────────────────────────────────────────────────────
# E. filings — 서브함수 및 파싱 로직 보완
# ───────────────────────────────────────────────────────────────────────

class TestFilings_SubFunctions:
    """_parse_number / _parse_document_text 유형별 / piicDecsn / alotMatter / page2 fallback"""

    @pytest.mark.unit
    @pytest.mark.parametrize("value,expected", [
        ("25.3",        25.3),
        ("25,300,000",  25300000.0),
        ("25.3%",       25.3),
        ("-",           None),
        ("",            None),
        ("N/A",         None),
        ("  100  ",     100.0),
        ("0",           0.0),
    ])
    def test_parse_number(self, value, expected):
        from collectors.filings import _parse_number
        result = _parse_number(value)
        assert result == expected, f"_parse_number('{value}') → 예상 {expected}, 실제 {result}"

    @pytest.mark.unit
    @pytest.mark.parametrize("report_nm,text,expected_substring", [
        ("단일판매공급계약체결",  "계약금액 320억원 매출대비 25.8%",       "320억"),
        ("소송",                  "원고 승소 청구금액 85억원",              "승소"),
        ("임상",                  "임상 3상 FDA 적응증 암종양",             "3상"),
        ("배당결정",              "시가배당률 5.5% 배당금 1000원",          "5.5%"),
        ("무상증자",              "신주발행주식수 1,000,000주 증자비율 100%","1,000,000주"),
        ("유상증자",              "발행주식수 500,000주 발행가액 10,000원", "500,000주"),  # TODO: filings.py에 유상증자 파싱 추가 필요
    ])
    def test_parse_document_text_by_type(self, report_nm, text, expected_substring):
        from collectors.filings import _parse_document_text
        result = _parse_document_text(text, report_nm)
        assert expected_substring in result, \
            f"[{report_nm}] '{expected_substring}' 없음. 결과: '{result}'"

    @pytest.mark.unit
    def test_fetch_dart_web_empty_list_response(self):
        """_fetch_dart_web() — API 정상 응답인데 list 비어있으면 []"""
        import config
        from collectors.filings import _fetch_dart_web

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "000", "list": []}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(config, "DART_API_KEY", "test_key"), \
             patch("collectors.filings.requests.get", return_value=mock_resp):
            result = _fetch_dart_web(TRADING_DATE_STR)

        assert result == [], f"빈 list 응답 시 [] 아님: {result}"

    @pytest.mark.unit
    def test_fetch_contract_size_passes_threshold(self):
        """_fetch_contract_size() — 임계값 초과 시 passes=True"""
        import config
        from collectors.filings import _fetch_contract_size

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "000", "list": [{"selfCptlRatio": "25.5"}]}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(config, "DART_API_KEY", "test"), \
             patch("collectors.filings.requests.get", return_value=mock_resp):
            size_str, passes = _fetch_contract_size("00126380", TRADING_DATE_STR)

        assert "25.5%" in size_str
        assert passes is True, "25.5%는 임계값 초과이므로 True여야 함"

    @pytest.mark.unit
    def test_fetch_contract_size_below_threshold(self):
        """_fetch_contract_size() — 임계값 미달 시 passes=False"""
        import config
        from collectors.filings import _fetch_contract_size

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "000", "list": [{"selfCptlRatio": "5.0"}]}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(config, "DART_API_KEY", "test"), \
             patch("collectors.filings.requests.get", return_value=mock_resp):
            _, passes = _fetch_contract_size("00126380", TRADING_DATE_STR)

        assert passes is False, "5.0%는 임계값 미달이므로 False여야 함"

    @pytest.mark.unit
    def test_fetch_dividend_size_passes_threshold(self):
        """_fetch_dividend_size() — 임계값 초과 시 passes=True"""
        import config
        from collectors.filings import _fetch_dividend_size

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "000", "list": [{"dvdnYld": "6.5"}]}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(config, "DART_API_KEY", "test"), \
             patch("collectors.filings.requests.get", return_value=mock_resp):
            size_str, passes = _fetch_dividend_size("00126380", TRADING_DATE_STR)

        assert "6.50%" in size_str
        assert passes is True, "6.5%는 임계값 초과이므로 True여야 함"

    @pytest.mark.unit
    def test_fetch_dividend_size_below_threshold(self):
        """_fetch_dividend_size() — 임계값 미달 시 passes=False"""
        import config
        from collectors.filings import _fetch_dividend_size

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "000", "list": [{"dvdnYld": "2.0"}]}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(config, "DART_API_KEY", "test"), \
             patch("collectors.filings.requests.get", return_value=mock_resp):
            _, passes = _fetch_dividend_size("00126380", TRADING_DATE_STR)

        assert passes is False, "2.0%는 임계값 미달이므로 False여야 함"

    @pytest.mark.unit
    def test_fetch_document_summary_api_error_returns_empty(self):
        """_fetch_document_summary() — DART 권한 오류 시 빈 문자열"""
        import config
        from collectors.filings import _fetch_document_summary

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.json.return_value = {"status": "020", "message": "권한 없음"}

        with patch.object(config, "DART_API_KEY", "test"), \
             patch("collectors.filings.requests.get", return_value=mock_resp):
            result = _fetch_document_summary("20240101000001", "단일판매공급계약체결")

        assert result == "", f"오류 시 빈 문자열이 아님: '{result}'"

    @pytest.mark.requires_key
    @pytest.mark.skipif(not os.environ.get("DART_API_KEY"), reason="DART_API_KEY 없음")
    def test_real_dart_both_apis(self):
        """실제 DART — _fetch_dart_api(page1) + _fetch_dart_web(page2) 모두 실행"""
        from collectors.filings import _fetch_dart_api, _fetch_dart_web

        r1 = _fetch_dart_api(TRADING_DATE_STR)
        assert isinstance(r1, list), "page1 결과가 list 아님"
        print(f"\n  [dart/page1] {len(r1)}건")

        r2 = _fetch_dart_web(TRADING_DATE_STR)
        assert isinstance(r2, list), "page2 결과가 list 아님"
        print(f"  [dart/page2] {len(r2)}건")


# ───────────────────────────────────────────────────────────────────────
# F. price_domestic — collect_supply + 서브함수 보완
# ───────────────────────────────────────────────────────────────────────

class TestPriceDomestic_SubFunctions:
    """collect_supply() 완전 누락 / _fetch_index / _fetch_sector_map / _find_col 등"""

    @pytest.mark.unit
    @pytest.mark.parametrize("col_name,candidates,expected", [
        ("종가",   ["종가", "Close"],    "종가"),
        ("Close",  ["종가", "Close"],    "Close"),
        ("Volume", ["종가", "Close"],    None),
        ("등락률", ["등락률", "Change"], "등락률"),
    ])
    def test_find_col_utility(self, col_name, candidates, expected):
        from collectors.price_domestic import _find_col
        result = _find_col([col_name], candidates)
        assert result == expected, f"_find_col(['{col_name}'], {candidates}) → 예상 {expected}, 실제 {result}"

    @pytest.mark.unit
    def test_resolve_short_ohlcv_fn_no_error(self):
        """_resolve_short_ohlcv_fn() — 오류 없이 실행되어야 함"""
        from collectors.price_domestic import _resolve_short_ohlcv_fn
        try:
            fn = _resolve_short_ohlcv_fn()
            print(f"\n  [short_fn] 탐색 결과: {fn}")
        except Exception as e:
            pytest.fail(f"_resolve_short_ohlcv_fn() 오류 발생: {e}")

    @pytest.mark.integration
    def test_fetch_index_kospi(self):
        """_fetch_index() — KOSPI 지수 개별 수집"""
        from collectors.price_domestic import _fetch_index
        result = _fetch_index(TRADING_DATE, "1001", "KOSPI", etf_proxy="069500")

        assert isinstance(result, dict)
        print(f"\n  [KOSPI] {result}")
        if result:
            assert "close"       in result
            assert "change_rate" in result
            assert result["close"] > 0

    @pytest.mark.integration
    def test_fetch_index_kosdaq(self):
        """_fetch_index() — KOSDAQ 지수 개별 수집"""
        from collectors.price_domestic import _fetch_index
        result = _fetch_index(TRADING_DATE, "2001", "KOSDAQ", etf_proxy="229200")

        assert isinstance(result, dict)
        print(f"\n  [KOSDAQ] {result}")
        if result:
            assert result["close"] > 0

    @pytest.mark.integration
    def test_fetch_sector_map(self):
        """_fetch_sector_map() — 업종 분류 수집"""
        from collectors.price_domestic import _fetch_all_stocks, _fetch_sector_map

        all_stocks = _fetch_all_stocks(TRADING_DATE_STR, TRADING_DATE)
        result     = _fetch_sector_map(TRADING_DATE_STR, all_stocks)

        assert isinstance(result, dict)
        print(f"\n  [sector_map] {len(result)}개 업종")
        if result:
            sector = next(iter(result))
            assert isinstance(result[sector], list), f"{sector}: 종목 리스트가 아님"

    @pytest.mark.integration
    def test_fetch_institutional_by_tickers(self):
        """_fetch_institutional_by_tickers() — 기관/외인 수급"""
        from collectors.price_domestic import _fetch_institutional_by_tickers
        result = _fetch_institutional_by_tickers(TRADING_DATE_STR, ["005930", "000660"])

        assert isinstance(result, list)
        print(f"\n  [institutional] {len(result)}종목")
        if result:
            assert "종목코드"     in result[0]
            assert "기관순매수"   in result[0]
            assert "외국인순매수" in result[0]

    @pytest.mark.integration
    def test_fetch_short_selling(self):
        """_fetch_short_selling() — 공매도 수집"""
        from collectors.price_domestic import _fetch_short_selling
        result = _fetch_short_selling(TRADING_DATE_STR, ["005930", "000660"])

        assert isinstance(result, list)
        print(f"\n  [short_selling] {len(result)}종목")
        if result:
            assert "종목코드"     in result[0]
            assert "공매도잔고율" in result[0]
            assert "공매도거래량" in result[0]

    @pytest.mark.integration
    def test_collect_supply_samsung(self):
        """collect_supply() — 완전 누락됐던 개별 종목 수급 (장중봇 4단계용)"""
        from collectors.price_domestic import collect_supply
        result = collect_supply("005930", TRADING_DATE)

        assert isinstance(result, dict), "수급 결과가 dict 아님"
        required = {"종목코드", "기관_5일순매수", "외국인_5일순매수", "공매도잔고율", "대차잔고"}
        missing = required - set(result.keys())
        assert not missing, f"collect_supply 필수 키 누락: {missing}"
        assert result["종목코드"] == "005930"
        print(f"\n  [collect_supply/005930] {result}")


# ───────────────────────────────────────────────────────────────────────
# G. sector_etf — ETF 11개 개별 + 정렬 검증
# ───────────────────────────────────────────────────────────────────────

class TestSectorETF_AllETFs:
    """KODEX ETF 11개 개별 수집 검증 + 거래량 배율 정렬 확인"""

    @pytest.mark.integration
    @pytest.mark.parametrize("ticker,etf_name,sector", [
        ("069500", "KODEX 200",        "시장전체"),
        ("091160", "KODEX 반도체",     "반도체/IT"),
        ("091180", "KODEX 자동차",     "자동차/부품"),
        ("157490", "KODEX 철강",       "철강/비철금속"),
        ("102110", "KODEX 건설",       "건설/부동산"),
        ("139250", "KODEX 에너지화학", "에너지/화학"),
        ("140710", "KODEX 운송",       "해운/조선/운송"),
        ("117700", "KODEX 인프라",     "인프라/유틸"),
        ("139220", "KODEX IT",         "IT/소프트웨어"),
        ("102780", "KODEX 은행",       "금융/은행"),
        ("229720", "KODEX 방산",       "방산/항공"),
    ])
    def test_each_etf(self, ticker, etf_name, sector):
        """ETF 11개 개별 pykrx 수집 — 구조·값 검증"""
        import config
        from collectors.sector_etf import _fetch_etf_data
        from utils.date_utils import get_prev_trading_day

        today_str = TRADING_DATE.strftime("%Y%m%d")
        prev      = get_prev_trading_day(TRADING_DATE)
        prev_str  = prev.strftime("%Y%m%d") if prev else None

        try:
            result = _fetch_etf_data(ticker, etf_name, sector, today_str, prev_str)
        except Exception as e:
            pytest.skip(f"{etf_name}({ticker}) 수집 실패: {e}")

        if result is None:
            pytest.skip(f"{etf_name}({ticker}) 데이터 없음 (휴장일 가능)")

        print(f"\n  [{etf_name}({ticker})] 종가={result['close']:,}, 배율={result['volume_ratio']:.2f}x, 등락={result['change_pct']:+.2f}%")
        assert result["ticker"]  == ticker,   "ticker 불일치"
        assert result["name"]    == etf_name, "name 불일치"
        assert result["sector"]  == sector,   "sector 불일치"
        assert result["close"]   > 0,         "종가 0 이하"
        assert result["volume"]  >= 0,        "거래량 음수"
        assert result["신뢰도"] == "pykrx",  "신뢰도 필드 오류"

    @pytest.mark.integration
    def test_collect_sorted_by_volume_ratio(self):
        """collect() 결과가 거래량 배율 내림차순 정렬인지 확인"""
        import config
        from collectors.sector_etf import collect

        with patch.object(config, "SECTOR_ETF_ENABLED", True):
            result = collect(TRADING_DATE)

        if len(result) >= 2:
            ratios = [r["volume_ratio"] for r in result]
            assert ratios == sorted(ratios, reverse=True), \
                f"거래량 배율 내림차순 정렬 실패: {ratios}"
        print(f"\n  [sector_etf/정렬] {len(result)}개 ETF 수집, 정렬 확인")


# ───────────────────────────────────────────────────────────────────────
# H. short_interest — 서브함수 + 경계값 보완
# ───────────────────────────────────────────────────────────────────────

class TestShortInterest_SubFunctions:
    """_fetch_short_volume 구조 / _fetch_balance_safe / 신호 경계값"""

    @pytest.mark.integration
    def test_fetch_short_volume_structure(self):
        """_fetch_short_volume() 반환 데이터 전체 구조 검증"""
        from collectors.short_interest import _fetch_short_volume
        from utils.date_utils import get_prev_trading_day

        today    = TRADING_DATE.strftime("%Y%m%d")
        prev     = get_prev_trading_day(TRADING_DATE)
        prev_str = prev.strftime("%Y%m%d") if prev else today
        week_ago = (TRADING_DATE - timedelta(days=7)).strftime("%Y%m%d")

        result = _fetch_short_volume(today, prev_str, week_ago, top_n=5)
        assert isinstance(result, list)
        print(f"\n  [short_volume] {len(result)}종목")

        if result:
            item = result[0]
            required = {"ticker","name","short_volume","short_volume_prev",
                        "short_ratio","balance","balance_prev","balance_chg_pct","signal","신뢰도"}
            missing = required - set(item.keys())
            assert not missing, f"short_volume 항목 키 누락: {missing}"
            assert item["신뢰도"] == "pykrx"

    @pytest.mark.integration
    def test_fetch_balance_safe_samsung(self):
        """_fetch_balance_safe() — 삼성전자 잔고 조회, 오류없이 (0,0) 이상 반환"""
        from collectors.short_interest import _fetch_balance_safe

        today    = TRADING_DATE.strftime("%Y%m%d")
        week_ago = (TRADING_DATE - timedelta(days=7)).strftime("%Y%m%d")
        bal_today, bal_prev = _fetch_balance_safe("005930", today, week_ago)

        assert isinstance(bal_today, int) and bal_today >= 0
        assert isinstance(bal_prev,  int) and bal_prev  >= 0
        print(f"\n  [balance_safe/005930] 당일={bal_today:,}, 전주={bal_prev:,}")

    @pytest.mark.unit
    @pytest.mark.parametrize("balance_chg,vol,vol_prev,expected", [
        (-30.0, 500,  1000, "쇼트커버링_예고"),  # 잔고 정확히 -30% → 트리거
        (-50.0, 200,  500,  "쇼트커버링_예고"),  # 잔고 -50%
        (-29.9, 400,  1000, "공매도급감"),        # 잔고 -29.9%, 거래량 -60% → 공매도급감
        (0.0,   400,  1000, "공매도급감"),        # 잔고 변화 없음, 거래량 -60%
        (0.0,   900,  1000, ""),                  # 신호 없음
        (0.0,   500,  1000, "공매도급감"),        # 거래량 정확히 -50% → 트리거
        (0.0,   510,  1000, ""),                  # 거래량 -49% → 트리거 안됨
    ])
    def test_signal_boundary_values(self, balance_chg, vol, vol_prev, expected):
        """공매도 신호 경계값 전수 검증"""
        from collectors.short_interest import _classify_signal
        item = {
            "balance": 700, "balance_prev": 1000,
            "balance_chg_pct": balance_chg,
            "short_volume": vol, "short_volume_prev": vol_prev,
        }
        result = _classify_signal(item)
        assert result == expected, \
            f"balance_chg={balance_chg}% vol={vol}/prev={vol_prev} → 예상 '{expected}', 실제 '{result}'"

    @pytest.mark.integration
    def test_collect_full_return_structure(self):
        """collect() — 반환 항목 전체 구조 검증"""
        import config
        from collectors.short_interest import collect

        with patch.object(config, "SHORT_INTEREST_ENABLED", True):
            result = collect(TRADING_DATE, top_n=5)

        assert isinstance(result, list)
        print(f"\n  [short/full] {len(result)}종목")
        for item in result:
            assert "ticker"       in item
            assert "short_ratio"  in item
            assert "signal"       in item
            assert item["signal"] in ("쇼트커버링_예고", "공매도급감", ""), \
                f"유효하지 않은 신호값: '{item['signal']}'"


# ───────────────────────────────────────────────────────────────────────
# I. event_calendar — 서브함수 및 KRX KIND 소스 보완
# ───────────────────────────────────────────────────────────────────────

class TestEventCalendar_SubFunctions:
    """_classify_event 전체 유형 / _parse_date 유틸 / KRX KIND 소스"""

    @pytest.mark.unit
    @pytest.mark.parametrize("report_nm,expected_type", [
        ("기업설명회 개최",         "IR"),
        ("IR 컨퍼런스 참석",        "IR"),
        ("NDR 일정 안내",           "IR"),
        ("정기주주총회 소집",       "주주총회"),
        ("임시주주총회 개최",       "주주총회"),
        ("잠정실적 발표",           "실적발표"),
        ("분기실적 공시",           "실적발표"),
        ("현금배당 결정",           "배당"),
        ("중간배당 공시",           "배당"),
        ("무관한 공시 제목",        None),          # 미매칭 → None
    ])
    def test_classify_event_all_types(self, report_nm, expected_type):
        from collectors.event_calendar import _classify_event
        result = _classify_event(report_nm)
        assert result == expected_type, \
            f"'{report_nm}' → 예상 '{expected_type}', 실제 '{result}'"

    @pytest.mark.unit
    @pytest.mark.parametrize("date_str,should_succeed", [
        ("20250315", True),
        ("20251231", True),
        ("",        False),
        ("invalid", False),
        ("2025-03", False),
    ])
    def test_parse_date_utility(self, date_str, should_succeed):
        from collectors.event_calendar import _parse_date
        today = TRADING_DATE
        date_result, days_until = _parse_date(date_str, today)

        if should_succeed:
            assert date_result != "", f"'{date_str}': 유효한 날짜인데 빈 문자열 반환"
            assert isinstance(days_until, int), "days_until이 int가 아님"
        else:
            assert date_result == "", f"'{date_str}': 무효한 날짜인데 빈 문자열이 아님"
            assert days_until == -1, f"'{date_str}': 실패 시 days_until이 -1이 아님"

    @pytest.mark.integration
    def test_krx_kind_source(self):
        """KRX KIND — DART API 없이도 이벤트 수집 가능한지 확인"""
        import config
        from collectors.event_calendar import _collect_krx_kind

        with patch.object(config, "EVENT_CALENDAR_ENABLED", True):
            result = _collect_krx_kind(TRADING_DATE)

        assert isinstance(result, list)
        print(f"\n  [krx_kind] {len(result)}건")
        if result:
            item = result[0]
            required = {"event_type", "corp_name", "ticker", "event_date",
                        "days_until", "title", "rcept_no", "source"}
            missing = required - set(item.keys())
            assert not missing, f"KRX KIND 항목 키 누락: {missing}"
            assert item["source"] == "krx_kind", f"source 필드 오류: {item['source']}"

    @pytest.mark.requires_key
    @pytest.mark.skipif(not os.environ.get("DART_API_KEY"), reason="DART_API_KEY 없음")
    def test_collect_dart_keyword_source(self):
        """DART 키워드 공시 소스 — _collect_dart_keyword() 개별 실행"""
        import config
        from collectors.event_calendar import _collect_dart_keyword

        with patch.object(config, "EVENT_CALENDAR_ENABLED", True):
            result = _collect_dart_keyword(TRADING_DATE)

        assert isinstance(result, list)
        print(f"\n  [dart_keyword] {len(result)}건")
        if result:
            assert "event_type" in result[0]
            assert "source"     in result[0]
            assert result[0]["source"] == "dart"
