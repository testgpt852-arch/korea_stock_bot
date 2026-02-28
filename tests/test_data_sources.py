"""
korea_stock_bot — 데이터 소스 연결 테스트
=========================================

[실행 방법]

① Railway 환경변수 포함하여 실행 (권장):
    railway run python test_data_sources.py

    Railway CLI 설치:
    npm install -g @railway/cli
    railway login
    railway link   # 프로젝트 선택
    railway run python test_data_sources.py

② 로컈 .env 파일 사용 (선택):
    .env 파일 생성 (korea_stock_bot-main 폴더에):
        DART_API_KEY=your_key
        NAVER_CLIENT_ID=your_id
        NAVER_CLIENT_SECRET=your_secret
        KIS_APP_KEY=your_key
        KIS_APP_SECRET=your_secret
        KIS_ACCOUNT_NO=your_account
        NEWSAPI_ORG_KEY=your_newsapi_key
    실행:
        python test_data_sources.py

pykrx 버전 필수조건: pip install "pykrx>=1.0.47"
"""


from dotenv import load_dotenv
load_dotenv()
import os, sys, time, json
from datetime import datetime, timedelta

# ─── 결과 집계 ────────────────────────────────────────────────
results = []

def ok(name, detail=""):
    results.append(("✅ PASS", name, detail))
    print(f"  ✅ PASS  {name}" + (f"  →  {detail}" if detail else ""))

def fail(name, detail=""):
    results.append(("❌ FAIL", name, detail))
    print(f"  ❌ FAIL  {name}" + (f"  →  {detail}" if detail else ""))

def skip(name, reason=""):
    results.append(("⏭  SKIP", name, reason))
    print(f"  ⏭  SKIP  {name}" + (f"  ({reason})" if reason else ""))

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

# ─── 환경변수 읽기 ────────────────────────────────────────────
DART_API_KEY        = os.environ.get("DART_API_KEY")
NAVER_CLIENT_ID     = os.environ.get("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET")
KIS_APP_KEY         = os.environ.get("KIS_APP_KEY")
KIS_APP_SECRET      = os.environ.get("KIS_APP_SECRET")
KIS_ACCOUNT_NO      = os.environ.get("KIS_ACCOUNT_NO")

TODAY      = datetime.today().strftime("%Y%m%d")
TODAY_KR   = datetime.today().strftime("%Y-%m-%d")
PREV_DATE  = (datetime.today() - timedelta(days=5)).strftime("%Y%m%d")

# ══════════════════════════════════════════════════════════════
#  1. pykrx — 국내 주가 / 지수 / 업종 / 공매도
# ══════════════════════════════════════════════════════════════
section("1. pykrx  (국내 주가·지수·업종·공매도)")

try:
    from pykrx import stock as pykrx_stock
    import pykrx
    _pykrx_ver = getattr(pykrx, "__version__", "unknown")
    print(f"  pykrx 버전: {_pykrx_ver}")

    # ── 버전별 함수명 자동 탐색 ────────────────────────────────
    # 공매도 거래량: 1.0.46- / 1.0.47+ 함수명 분기
    _short_vol_fn = (
        getattr(pykrx_stock, "get_shorting_volume_by_ticker", None) or
        getattr(pykrx_stock, "get_market_short_selling_volume_by_ticker", None)
    )
    # 공매도 잔고: 1.0.46- / 1.0.47+ / 1.2.x 함수명 분기
    _short_ohlcv_fn = (
        getattr(pykrx_stock, "get_shorting_ohlcv_by_date",     None) or
        getattr(pykrx_stock, "get_market_short_ohlcv_by_date", None) or
        getattr(pykrx_stock, "get_shorting_balance_by_date",   None)
    )

    def _col(df, *candidates):
        """컬럼/인덱스에서 후보 중 첫 번째 존재하는 항목 반환"""
        col_set = set(df.columns)
        for c in candidates:
            if c in col_set:
                return c
        return None

    def _flatten_multiindex(df):
        """pykrx 1.2.x MultiIndex DataFrame을 단일 인덱스로 평탄화"""
        if df is not None and hasattr(df.index, "levels") and len(df.index.levels) > 1:
            return df.reset_index(level=1, drop=True)
        return df

    # 1-1. 코스피 지수 OHLCV
    # pykrx 1.2.x에서 get_index_ohlcv_by_date 내부 '지수명' KeyError 발생 시
    # KODEX 200 ETF(069500)로 폴백 — 등락률 계산 방식 동일
    try:
        _idx_ok = False
        _idx_err = ""
        try:
            df = pykrx_stock.get_index_ohlcv_by_date(PREV_DATE, TODAY, "1001")
            df = _flatten_multiindex(df)
            if df is not None and not df.empty:
                close_col = _col(df, "종가", "Close", "close")
                if close_col:
                    ok("pykrx 코스피 지수 OHLCV",
                       f"종가={float(df.iloc[-1][close_col]):,.0f}  컬럼={list(df.columns)}")
                    _idx_ok = True
                else:
                    _idx_err = f"종가 컬럼 없음 — 실제: {list(df.columns)}"
        except Exception as e:
            _idx_err = str(e)

        if not _idx_ok:
            # ETF 프록시 폴백 (KODEX 200)
            df = pykrx_stock.get_market_ohlcv(PREV_DATE, TODAY, "069500")
            if df is not None and not df.empty:
                close_col = _col(df, "종가", "Close", "close")
                if close_col:
                    ok("pykrx 코스피 지수 OHLCV (ETF프록시)",
                       f"KODEX200 종가={float(df.iloc[-1][close_col]):,.0f}"
                       f"  원인={_idx_err[:60]}")
                else:
                    fail("pykrx 코스피 지수 OHLCV", f"ETF프록시도 컬럼 없음  원인={_idx_err}")
            else:
                fail("pykrx 코스피 지수 OHLCV", _idx_err)
    except Exception as e:
        fail("pykrx 코스피 지수 OHLCV", str(e))

    # 1-2. 전종목 OHLCV
    # pykrx 1.2.x에서 get_market_ohlcv_by_ticker 내부 컬럼 오류 발생 시
    # 단일 종목(삼성전자) get_market_ohlcv로 폴백 — 컬럼명 검증용
    _ref_date = PREV_DATE
    try:
        _all_ok = False
        _all_err = ""
        try:
            df = pykrx_stock.get_market_ohlcv_by_ticker(PREV_DATE, market="KOSPI")
            if df is not None and not df.empty:
                close_col = _col(df, "종가", "Close", "close")
                chg_col   = _col(df, "등락률", "Change", "change", "Returns")
                ok("pykrx 코스피 전종목 OHLCV",
                   f"종목수={len(df)}  종가={close_col}  등락률={chg_col}  기준일={_ref_date}")
                _all_ok = True
        except Exception as e:
            _all_err = str(e)

        if not _all_ok:
            # 폴백: 단일 종목으로 컬럼명 구조 확인 (삼성전자)
            df = pykrx_stock.get_market_ohlcv(PREV_DATE, TODAY, "005930")
            if df is not None and not df.empty:
                close_col = _col(df, "종가", "Close", "close")
                chg_col   = _col(df, "등락률", "Change", "change")
                ok("pykrx 전종목 OHLCV (단일종목폴백)",
                   f"get_market_ohlcv 정상동작  종가={close_col}  등락률={chg_col}"
                   f"  원인={_all_err[:60]}")
            else:
                fail("pykrx 코스피 전종목 OHLCV", _all_err)
    except Exception as e:
        fail("pykrx 코스피 전종목 OHLCV", str(e))

    # 1-3. 업종 분류
    # pykrx 1.2.x에서 MultiIndex 또는 컬럼명 변경 가능
    try:
        df = pykrx_stock.get_market_sector_classifications(_ref_date, market="KOSPI")
        if df is None or df.empty:
            fail("pykrx 업종 분류", "빈 DataFrame")
        else:
            df = _flatten_multiindex(df)
            # 종목코드가 인덱스일 경우 컬럼으로 내리기
            if df.index.name in ("종목코드", "Code", "code", "ticker"):
                df = df.reset_index()
            code_col   = _col(df, "종목코드", "Code", "code", "ticker")
            sector_col = _col(df, "업종명", "sector", "Sector", "industry", "Industry")
            ok("pykrx 업종 분류",
               f"종목수={len(df)}  코드컬럼={code_col}  업종컬럼={sector_col}"
               f"  전체컬럼={list(df.columns)}")
    except Exception as e:
        fail("pykrx 업종 분류", str(e))

    # 1-4. 기관/외인 (삼성전자)
    try:
        df = pykrx_stock.get_market_trading_value_by_date(PREV_DATE, TODAY, "005930", detail=True)
        if df is not None and not df.empty:
            inst_col = next((c for c in df.columns if "기관" in str(c) or "Institution" in str(c)), None)
            frgn_col = next((c for c in df.columns if "외국인" in str(c) or "Foreign" in str(c)), None)
            ok("pykrx 기관/외인 수급 (삼성전자)",
               f"행수={len(df)}  기관={inst_col}  외인={frgn_col}")
        else:
            fail("pykrx 기관/외인 수급", f"빈 DataFrame (주말/공휴일이면 정상) — 컬럼: {list(df.columns) if df is not None else None}")
    except Exception as e:
        fail("pykrx 기관/외인 수급", str(e))

    # 1-5. 공매도 거래량
    # 버전별 함수명 자동 탐색 결과 사용
    if _short_vol_fn is None:
        fail("pykrx 공매도 거래량", "지원 함수 없음 — pykrx 버전 확인 필요")
    else:
        try:
            df = _short_vol_fn(PREV_DATE, market="KOSPI")
            if df is not None and not df.empty:
                ok("pykrx 공매도 거래량",
                   f"종목수={len(df)}  fn={_short_vol_fn.__name__}  컬럼={list(df.columns)}")
            else:
                fail("pykrx 공매도 거래량",
                     f"빈 DataFrame (주말/공휴일 정상)  fn={_short_vol_fn.__name__}")
        except Exception as e:
            fail("pykrx 공매도 거래량", f"[{_short_vol_fn.__name__}] {e}")

    # 1-6. 공매도 잔고 (삼성전자)
    if _short_ohlcv_fn is None:
        fail("pykrx 공매도 잔고", "지원 함수 없음 — pykrx 버전 확인 필요")
    else:
        try:
            df = _short_ohlcv_fn(PREV_DATE, TODAY, "005930")
            if df is not None and not df.empty:
                ok("pykrx 공매도 잔고 (삼성전자)",
                   f"행수={len(df)}  fn={_short_ohlcv_fn.__name__}  컬럼={list(df.columns)}")
            else:
                fail("pykrx 공매도 잔고",
                     f"빈 DataFrame (주말/공휴일 정상)  fn={_short_ohlcv_fn.__name__}")
        except Exception as e:
            fail("pykrx 공매도 잔고", f"[{_short_ohlcv_fn.__name__}] {e}")

    # 1-7. 섹터 ETF OHLCV (KODEX 반도체 266410)
    try:
        df = pykrx_stock.get_market_ohlcv(PREV_DATE, TODAY, "266410")
        if df is not None and not df.empty:
            ok("pykrx 섹터ETF OHLCV (KODEX반도체)", f"행수={len(df)}")
        else:
            fail("pykrx 섹터ETF OHLCV", "빈 DataFrame")
    except Exception as e:
        fail("pykrx 섹터ETF OHLCV", str(e))

except ImportError:
    for name in ["pykrx 코스피 지수", "pykrx 전종목 OHLCV", "pykrx 업종 분류",
                 "pykrx 기관/외인", "pykrx 공매도 거래량", "pykrx 공매도 잔고", "pykrx 섹터ETF"]:
        skip(name, "pykrx 미설치 — pip install 'pykrx'")



section("2. yfinance  (미국증시·원자재·환율)")

try:
    import yfinance as yf

    TEST_TICKERS = {
        "S&P500 (^GSPC)":   "^GSPC",
        "나스닥 (^IXIC)":   "^IXIC",
        "다우 (^DJI)":      "^DJI",
        "WTI 원유 (CL=F)":  "CL=F",
        "금 (GC=F)":        "GC=F",
        "원달러 환율":      "KRW=X",
    }

    for label, ticker in TEST_TICKERS.items():
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="2d")
            if hist is not None and not hist.empty:
                close = hist["Close"].iloc[-1]
                ok(f"yfinance {label}", f"종가={close:.2f}")
            else:
                fail(f"yfinance {label}", "빈 DataFrame")
        except Exception as e:
            fail(f"yfinance {label}", str(e))
        time.sleep(0.3)

except ImportError:
    skip("yfinance 전체", "pip install yfinance")


# ══════════════════════════════════════════════════════════════
#  3. DART API — 공시 / 이벤트 캘린더
# ══════════════════════════════════════════════════════════════
section("3. DART API  (공시·IR·주주총회·실적)")

if not DART_API_KEY:
    skip("DART API 전체", "DART_API_KEY 환경변수 미설정")
else:
    import requests

    # 3-1. opendart 공시 목록
    try:
        url = "https://opendart.fss.or.kr/api/list.json"
        r = requests.get(url, params={
            "crtfc_key": DART_API_KEY,
            "bgn_de":    PREV_DATE,
            "end_de":    TODAY,
            "page_no":   1,
            "page_count": 10,
        }, timeout=10)
        data = r.json()
        if data.get("status") == "000":
            ok("DART 공시목록 API", f"총={data.get('total_count',0)}건")
        elif data.get("status") == "010":
            fail("DART 공시목록 API", "API 키 인증 실패")
        else:
            fail("DART 공시목록 API", f"status={data.get('status')} msg={data.get('message')}")
    except Exception as e:
        fail("DART 공시목록 API", str(e))

    # 3-2. dart.fss.or.kr 검색 API (dart_web fallback)
    try:
        url = "https://dart.fss.or.kr/api/search.json"
        r = requests.get(url, params={
            "key":       DART_API_KEY,
            "startDate": PREV_DATE,
            "endDate":   TODAY,
            "pageNo":    1,
            "maxResults": 5,
        }, timeout=10)
        if r.status_code == 200:
            ok("DART 검색 API (dart.fss.or.kr)", f"status={r.status_code}")
        else:
            fail("DART 검색 API (dart.fss.or.kr)", f"HTTP {r.status_code}")
    except Exception as e:
        fail("DART 검색 API (dart.fss.or.kr)", str(e))

    # 3-3. 이벤트 캘린더 (IR 일정)
    try:
        url = "https://opendart.fss.or.kr/api/list.json"
        r = requests.get(url, params={
            "crtfc_key":   DART_API_KEY,
            "pblntf_ty":   "F",   # IR/기업설명회
            "bgn_de":      TODAY,
            "end_de":      (datetime.today() + timedelta(days=7)).strftime("%Y%m%d"),
            "page_count":  20,
        }, timeout=10)
        data = r.json()
        if data.get("status") == "000":
            ok("DART 이벤트캘린더 (IR 일정)", f"향후7일={data.get('total_count',0)}건")
        else:
            fail("DART 이벤트캘린더", f"status={data.get('status')}")
    except Exception as e:
        fail("DART 이벤트캘린더", str(e))


# ══════════════════════════════════════════════════════════════
#  4. 네이버 OpenAPI — 뉴스 / 데이터랩
# ══════════════════════════════════════════════════════════════
section("4. 네이버 OpenAPI  (뉴스검색·데이터랩)")

if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
    skip("네이버 API 전체", "NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 미설정")
else:
    import requests
    hdrs = {
        "X-Naver-Client-Id":     NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }

    # 4-1. 뉴스 검색
    try:
        r = requests.get(
            "https://openapi.naver.com/v1/search/news.json",
            headers=hdrs,
            params={"query": "코스피", "display": 5, "sort": "date"},
            timeout=8,
        )
        if r.status_code == 200:
            items = r.json().get("items", [])
            ok("네이버 뉴스검색 API", f"기사수={len(items)}")
        elif r.status_code == 401:
            fail("네이버 뉴스검색 API", "인증 실패 (API 키 확인)")
        else:
            fail("네이버 뉴스검색 API", f"HTTP {r.status_code}")
    except Exception as e:
        fail("네이버 뉴스검색 API", str(e))

    # 4-2. 데이터랩 트렌드
    try:
        payload = {
            "startDate": (datetime.today() - timedelta(days=7)).strftime("%Y-%m-%d"),
            "endDate":   TODAY_KR,
            "timeUnit":  "date",
            "keywordGroups": [{"groupName": "반도체", "keywords": ["반도체", "삼성전자"]}],
        }
        r = requests.post(
            "https://openapi.naver.com/v1/datalab/search",
            headers={**hdrs, "Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=8,
        )
        if r.status_code == 200:
            ok("네이버 데이터랩 트렌드 API", "검색량 지수 수신 완료")
        else:
            fail("네이버 데이터랩 트렌드 API", f"HTTP {r.status_code}  {r.text[:80]}")
    except Exception as e:
        fail("네이버 데이터랩 트렌드 API", str(e))


# ══════════════════════════════════════════════════════════════
#  5. KIS (한국투자증권) REST API
# ══════════════════════════════════════════════════════════════
section("5. KIS REST API  (주가·거래량·호가)")

_KIS_BASE = "https://openapi.koreainvestment.com:9443"

if not KIS_APP_KEY or not KIS_APP_SECRET:
    skip("KIS REST API 전체", "KIS_APP_KEY / KIS_APP_SECRET 미설정")
else:
    import requests

    # 5-1. 토큰 발급
    access_token = None
    try:
        r = requests.post(
            f"{_KIS_BASE}/oauth2/tokenP",
            json={
                "grant_type":   "client_credentials",
                "appkey":       KIS_APP_KEY,
                "appsecret":    KIS_APP_SECRET,
            },
            timeout=10,
        )
        data = r.json()
        access_token = data.get("access_token")
        if access_token:
            ok("KIS 액세스 토큰 발급", f"expires_in={data.get('expires_in')}")
        else:
            fail("KIS 액세스 토큰 발급", data.get("error_description", str(data))[:100])
    except Exception as e:
        fail("KIS 액세스 토큰 발급", str(e))

    if access_token:
        kis_hdrs = {
            "authorization": f"Bearer {access_token}",
            "appkey":        KIS_APP_KEY,
            "appsecret":     KIS_APP_SECRET,
            "Content-Type":  "application/json; charset=utf-8",
        }

        # 5-2. 현재가 조회 (삼성전자)
        try:
            r = requests.get(
                f"{_KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
                headers={**kis_hdrs, "tr_id": "FHKST01010100"},
                params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": "005930"},
                timeout=8,
            )
            data = r.json()
            price = data.get("output", {}).get("stck_prpr")
            if price:
                ok("KIS 현재가 (삼성전자)", f"현재가={int(price):,}원")
            else:
                fail("KIS 현재가", f"rt_cd={data.get('rt_cd')} msg={data.get('msg1','')[:60]}")
        except Exception as e:
            fail("KIS 현재가 조회", str(e))

        # 5-3. 거래량 순위
        try:
            r = requests.get(
                f"{_KIS_BASE}/uapi/domestic-stock/v1/quotations/volume-rank",
                headers={**kis_hdrs, "tr_id": "FHPST01710000"},
                params={
                    "fid_cond_mrkt_div_code": "J",
                    "fid_cond_scr_div_code":  "20171",
                    "fid_input_iscd":         "0000",
                    "fid_div_cls_code":       "0",
                    "fid_blng_cls_code":      "0",
                    "fid_trgt_cls_code":      "111111111",
                    "fid_trgt_exls_cls_code": "000000",
                    "fid_input_price_1":      "",
                    "fid_input_price_2":      "",
                    "fid_vol_cnt":            "",
                    "fid_input_date_1":       "",
                },
                timeout=8,
            )
            data = r.json()
            items = data.get("output", [])
            if items:
                ok("KIS 거래량 순위", f"종목수={len(items)}  1위={items[0].get('hts_kor_isnm','')}")
            else:
                fail("KIS 거래량 순위", f"rt_cd={data.get('rt_cd')} msg={data.get('msg1','')[:60]}")
        except Exception as e:
            fail("KIS 거래량 순위", str(e))

        # 5-4. 등락률 순위
        try:
            r = requests.get(
                f"{_KIS_BASE}/uapi/domestic-stock/v1/ranking/fluctuation",
                headers={**kis_hdrs, "tr_id": "FHPST01700000"},
                params={
                    "fid_cond_mrkt_div_code": "J",
                    "fid_cond_scr_div_code":  "20170",
                    "fid_input_iscd":         "0000",
                    "fid_rank_sort_cls_code": "0",
                    "fid_input_cnt_1":        "0",
                    "fid_prc_cls_code":       "0",
                    "fid_input_price_1":      "",
                    "fid_input_price_2":      "",
                    "fid_vol_cnt":            "",
                    "fid_trgt_cls_code":      "0",
                    "fid_trgt_exls_cls_code": "0",
                    "fid_div_cls_code":       "0",
                    "fid_rsfl_rate1":         "",
                    "fid_rsfl_rate2":         "",
                },
                timeout=8,
            )
            data = r.json()
            items = data.get("output", [])
            if items:
                ok("KIS 등락률 순위", f"종목수={len(items)}")
            else:
                fail("KIS 등락률 순위", f"rt_cd={data.get('rt_cd')} msg={data.get('msg1','')[:60]}")
        except Exception as e:
            fail("KIS 등락률 순위", str(e))

        # 5-5. 호가 잔량 (삼성전자)
        try:
            r = requests.get(
                f"{_KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
                headers={**kis_hdrs, "tr_id": "FHKST01010200"},
                params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": "005930"},
                timeout=8,
            )
            data = r.json()
            askp = data.get("output1", {}).get("askp1")
            if askp:
                ok("KIS 호가 잔량 (삼성전자)", f"매도1호가={int(askp):,}원")
            else:
                fail("KIS 호가 잔량", f"rt_cd={data.get('rt_cd')} msg={data.get('msg1','')[:60]}")
        except Exception as e:
            fail("KIS 호가 잔량", str(e))




# ══════════════════════════════════════════════════════════════
#  5-b. NewsAPI.org — 지정학·글로벌 시황·리포트
# ══════════════════════════════════════════════════════════════
section("5-b. NewsAPI.org  (지정학·글로벌 시황·영문 리포트)")

NEWSAPI_KEY = os.environ.get("NEWSAPI_ORG_KEY") or os.environ.get("GOOGLE_NEWS_API_KEY", "")

if not NEWSAPI_KEY:
    skip("NewsAPI.org 전체", "NEWSAPI_ORG_KEY 또는 GOOGLE_NEWS_API_KEY 미설정")
else:
    import requests as _req2
    _NEWSAPI_BASE = "https://newsapi.org/v2/everything"
    from datetime import date as _date, timedelta as _td

    TEST_CASES = [
        ("지정학 — 한국 관세",      "South Korea tariff trade US",            "geopolitics_collector"),
        ("지정학 — 반도체 수출규제", "Korea semiconductor export restriction",  "geopolitics_collector"),
        ("리포트 — 한국주식 애널",  "Korea stock analyst target price",        "news_collector"),
        ("글로벌 — Fed 금리결정",   "Fed FOMC rate decision emerging markets", "news_collector"),
    ]

    for _name, _query, _used_in in TEST_CASES:
        try:
            _params = {
                "apiKey":   NEWSAPI_KEY,
                "q":        _query,
                "language": "en",
                "sortBy":   "publishedAt",
                "pageSize": 3,
                "from":     (_date.today() - _td(days=2)).isoformat(),
            }
            _r = _req2.get(_NEWSAPI_BASE, params=_params, timeout=10)
            _data = _r.json()
            if _data.get("status") == "ok":
                _arts = _data.get("articles", [])
                if _arts:
                    _src   = _arts[0].get("source", {}).get("name", "?")
                    _title = (_arts[0].get("title") or "")[:50]
                    ok(f"NewsAPI.org {_name}", f"[{_used_in}] {len(_arts)}건  최신={_src}: {_title}")
                else:
                    fail(f"NewsAPI.org {_name}", "기사 0건")
            elif _data.get("code") == "apiKeyInvalid":
                fail(f"NewsAPI.org {_name}", "API 키 무효")
            elif _data.get("code") == "rateLimited":
                fail(f"NewsAPI.org {_name}", "Rate Limit 초과 (무료 100req/day)")
            else:
                fail(f"NewsAPI.org {_name}", f"{_data.get(chr(39)+'status'+chr(39),'?')} {str(_data)[:60]}")
        except Exception as _e:
            fail(f"NewsAPI.org {_name}", str(_e))
        time.sleep(0.5)

    # top-headlines 엔드포인트 확인
    try:
        _r2 = _req2.get("https://newsapi.org/v2/top-headlines",
                        params={"apiKey": NEWSAPI_KEY, "category": "business",
                                "language": "en", "pageSize": 3}, timeout=10)
        _d2 = _r2.json()
        if _d2.get("status") == "ok":
            ok("NewsAPI.org top-headlines", f"총={_d2.get(chr(39)+'totalResults'+chr(39),0)}건")
        else:
            fail("NewsAPI.org top-headlines", _d2.get("message", "")[:60])
    except Exception as _e:
        fail("NewsAPI.org top-headlines", str(_e))


# ══════════════════════════════════════════════════════════════
#  6. RSS 피드 — 지정학 뉴스
# ══════════════════════════════════════════════════════════════
section("6. RSS 피드  (로이터·기재부·방사청)")

try:
    import feedparser

    RSS_SOURCES = [
        ("Reuters Business",  "https://feeds.reuters.com/reuters/businessNews"),
        ("Reuters World",     "https://feeds.reuters.com/reuters/worldNews"),
        ("기재부 보도자료",   "https://www.moef.go.kr/sty/rss/moefRss.do"),
        ("방사청 보도자료",   "https://www.dapa.go.kr/dapa/rss/rssService.do"),
    ]

    for name, url in RSS_SOURCES:
        try:
            feed = feedparser.parse(url)
            if feed.entries:
                ok(f"RSS {name}", f"기사수={len(feed.entries)}  최신={feed.entries[0].get('title','')[:30]}")
            elif feed.bozo:
                fail(f"RSS {name}", f"파싱오류: {feed.bozo_exception}")
            else:
                fail(f"RSS {name}", "entries 없음")
        except Exception as e:
            fail(f"RSS {name}", str(e))
        time.sleep(0.5)

except ImportError:
    skip("RSS 피드 전체", "pip install feedparser")


# ══════════════════════════════════════════════════════════════
#  7. 최종 결과 요약
# ══════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print("  테스트 결과 요약")
print(f"{'='*60}")

passed = [r for r in results if r[0].startswith("✅")]
failed = [r for r in results if r[0].startswith("❌")]
skipped = [r for r in results if r[0].startswith("⏭")]

print(f"\n  총 {len(results)}개  |  ✅ {len(passed)}  ❌ {len(failed)}  ⏭ {len(skipped)}\n")

if failed:
    print("  ── 실패 항목 ──────────────────────────────")
    for _, name, detail in failed:
        print(f"  ❌ {name}")
        if detail:
            print(f"       └ {detail}")

if skipped:
    print("\n  ── SKIP 항목 (환경변수 미설정) ────────────")
    for _, name, reason in skipped:
        print(f"  ⏭  {name}  ({reason})")

print()
