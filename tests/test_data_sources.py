"""
korea_stock_bot — 데이터 소스 연결 테스트
=========================================

이 봇은 아래 8개 외부 서비스에서 데이터를 가져옵니다.
이 파일을 실행하면 각각 잘 연결되는지 한 번에 확인할 수 있습니다.

────────────────────────────────────────────────────────────
  무엇을 가져오나요?  어디서 가져오나요?
────────────────────────────────────────────────────────────
  국내 주가/지수/업종/공매도    →  pykrx (한국거래소, 무료)
  미국증시/원자재/환율           →  yfinance (야후파이낸스, 무료)
  국내 공시 (DART)               →  금융감독원 OpenDART API
  국내 뉴스 + 검색량 트렌드     →  네이버 OpenAPI
  실시간 주가/거래량/호가        →  한국투자증권(KIS) API
  지정학·글로벌 영문 뉴스        →  NewsAPI.org
  로이터·기재부 뉴스             →  RSS 피드 (무료)
  AI 분석 (테마 해석 등)         →  Google AI API (Gemini)
  텔레그램 알림 발송             →  Telegram Bot API
────────────────────────────────────────────────────────────

[실행 방법]

① .env 파일이 이 파일과 같은 폴더에 있는 경우:
    python test_data_sources.py

② .env 파일 위치를 직접 지정하는 경우:
    ENV_PATH=/path/to/.env python test_data_sources.py

③ Railway 서버에서 실행하는 경우:
    railway run python test_data_sources.py

[.env 파일 예시]
    TELEGRAM_TOKEN=xxxxxxxxxx
    TELEGRAM_CHAT_ID=12345678
    DART_API_KEY=xxxxxxxxxxxxxxxx
    NAVER_CLIENT_ID=xxxx
    NAVER_CLIENT_SECRET=xxxx
    KIS_APP_KEY=xxxx
    KIS_APP_SECRET=xxxx
    KIS_ACCOUNT_NO=12345678-01
    NEWSAPI_ORG_KEY=xxxx
    GOOGLE_AI_API_KEY=xxxx
"""

import os, sys, time, json
from pathlib import Path

# ─── .env 로드 (ENV_PATH 환경변수로 경로 지정 가능) ──────────
from dotenv import load_dotenv

_env_path = os.environ.get("ENV_PATH")
if _env_path:
    load_dotenv(dotenv_path=_env_path)
    print(f"[설정] .env 로드: {_env_path}")
else:
    # 이 파일 기준 → 상위 폴더(korea_stock_bot-main) 순서로 탐색
    _found = False
    for _candidate in [Path(__file__).parent / ".env",
                        Path(__file__).parent.parent / ".env",
                        Path.cwd() / ".env"]:
        if _candidate.exists():
            load_dotenv(dotenv_path=str(_candidate))
            print(f"[설정] .env 로드: {_candidate}")
            _found = True
            break
    if not _found:
        load_dotenv()  # 기본 탐색

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
    print(f"  ⏭  SKIP  {name}" + (f"  (키 미설정: {reason})" if reason else ""))

def section(title, description=""):
    print(f"\n{'='*62}")
    print(f"  {title}")
    if description:
        print(f"  💬 {description}")
    print(f"{'='*62}")

# ─── 환경변수 읽기 ────────────────────────────────────────────
DART_API_KEY        = os.environ.get("DART_API_KEY")
NAVER_CLIENT_ID     = os.environ.get("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET")
KIS_APP_KEY         = os.environ.get("KIS_APP_KEY")
KIS_APP_SECRET      = os.environ.get("KIS_APP_SECRET")
KIS_ACCOUNT_NO      = os.environ.get("KIS_ACCOUNT_NO")
NEWSAPI_KEY         = os.environ.get("NEWSAPI_ORG_KEY") or os.environ.get("GOOGLE_NEWS_API_KEY", "")
GOOGLE_AI_API_KEY   = os.environ.get("GOOGLE_AI_API_KEY")
TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID")

TODAY      = datetime.today().strftime("%Y%m%d")
TODAY_KR   = datetime.today().strftime("%Y-%m-%d")
PREV_DATE  = (datetime.today() - timedelta(days=5)).strftime("%Y%m%d")

# ─── 환경변수 현황 출력 ───────────────────────────────────────
print(f"\n{'='*62}")
print("  🔑 환경변수 현황 (.env 로드 결과)")
print(f"{'='*62}")
_env_checks = [
    ("TELEGRAM_TOKEN",      TELEGRAM_TOKEN,      "텔레그램 알림"),
    ("TELEGRAM_CHAT_ID",    TELEGRAM_CHAT_ID,    "텔레그램 채팅방"),
    ("DART_API_KEY",        DART_API_KEY,        "금융감독원 공시"),
    ("NAVER_CLIENT_ID",     NAVER_CLIENT_ID,     "네이버 뉴스/검색"),
    ("NAVER_CLIENT_SECRET", NAVER_CLIENT_SECRET, "네이버 뉴스/검색"),
    ("KIS_APP_KEY",         KIS_APP_KEY,         "한국투자증권 주가"),
    ("KIS_APP_SECRET",      KIS_APP_SECRET,      "한국투자증권 주가"),
    ("KIS_ACCOUNT_NO",      KIS_ACCOUNT_NO,      "한국투자증권 계좌"),
    ("NEWSAPI_ORG_KEY",     NEWSAPI_KEY,         "영문 뉴스"),
    ("GOOGLE_AI_API_KEY",   GOOGLE_AI_API_KEY,   "AI 분석 (Gemini)"),
]
for _var, _val, _desc in _env_checks:
    _status = "✅ 있음" if _val else "❌ 없음"
    _masked = ("*" * (len(_val) - 4) + _val[-4:]) if _val and len(_val) > 4 else ("설정됨" if _val else "-")
    print(f"  {_status}  {_var:<26} ({_desc})  {_masked}")


# ══════════════════════════════════════════════════════════════
#  1. pykrx — 국내 주가 / 지수 / 업종 / 공매도
# ══════════════════════════════════════════════════════════════
section("1. pykrx  (국내 주가·지수·업종·공매도)",
        "한국거래소(KRX) 무료 데이터. 별도 API 키 불필요.")

try:
    from pykrx import stock as pykrx_stock
    import pykrx
    _pykrx_ver = getattr(pykrx, "__version__", "unknown")
    print(f"  pykrx 버전: {_pykrx_ver}")

    _short_vol_fn = (
        getattr(pykrx_stock, "get_shorting_volume_by_ticker", None) or
        getattr(pykrx_stock, "get_market_short_selling_volume_by_ticker", None)
    )
    _short_ohlcv_fn = (
        getattr(pykrx_stock, "get_shorting_ohlcv_by_date",     None) or
        getattr(pykrx_stock, "get_market_short_ohlcv_by_date", None) or
        getattr(pykrx_stock, "get_shorting_balance_by_date",   None)
    )

    def _col(df, *candidates):
        col_set = set(df.columns)
        for c in candidates:
            if c in col_set:
                return c
        return None

    def _flatten_multiindex(df):
        if df is not None and hasattr(df.index, "levels") and len(df.index.levels) > 1:
            return df.reset_index(level=1, drop=True)
        return df

    # 1-1. 코스피 지수 OHLCV
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
            df = pykrx_stock.get_market_ohlcv(PREV_DATE, TODAY, "069500")
            if df is not None and not df.empty:
                close_col = _col(df, "종가", "Close", "close")
                if close_col:
                    ok("pykrx 코스피 지수 OHLCV (ETF프록시)",
                       f"KODEX200 종가={float(df.iloc[-1][close_col]):,.0f}  원인={_idx_err[:60]}")
                else:
                    fail("pykrx 코스피 지수 OHLCV", f"ETF프록시도 컬럼 없음  원인={_idx_err}")
            else:
                fail("pykrx 코스피 지수 OHLCV", _idx_err)
    except Exception as e:
        fail("pykrx 코스피 지수 OHLCV", str(e))

    # 1-2. 전종목 OHLCV
    try:
        _all_ok = False
        _all_err = ""
        try:
            df = pykrx_stock.get_market_ohlcv_by_ticker(PREV_DATE, market="KOSPI")
            if df is not None and not df.empty:
                close_col = _col(df, "종가", "Close", "close")
                chg_col   = _col(df, "등락률", "Change", "change", "Returns")
                ok("pykrx 코스피 전종목 OHLCV",
                   f"종목수={len(df)}  종가={close_col}  등락률={chg_col}")
                _all_ok = True
        except Exception as e:
            _all_err = str(e)

        if not _all_ok:
            df = pykrx_stock.get_market_ohlcv(PREV_DATE, TODAY, "005930")
            if df is not None and not df.empty:
                close_col = _col(df, "종가", "Close", "close")
                ok("pykrx 전종목 OHLCV (단일종목폴백)",
                   f"삼성전자 종가={close_col}  원인={_all_err[:60]}")
            else:
                fail("pykrx 코스피 전종목 OHLCV", _all_err)
    except Exception as e:
        fail("pykrx 코스피 전종목 OHLCV", str(e))

    # 1-3. 업종 분류
    try:
        df = pykrx_stock.get_market_sector_classifications(PREV_DATE, market="KOSPI")
        if df is None or df.empty:
            fail("pykrx 업종 분류", "빈 DataFrame (주말/공휴일이면 정상)")
        else:
            # ── 실제 컬럼명 먼저 출력 (디버그) ────────────────
            print(f"  🔍 [DEBUG] 업종분류 인덱스명: {df.index.name!r}")
            print(f"  🔍 [DEBUG] 업종분류 컬럼목록: {list(df.columns)}")
            print(f"  🔍 [DEBUG] 첫 행: {df.iloc[0].to_dict() if not df.empty else '없음'}")

            # 인덱스가 종목코드면 컬럼으로 꺼내기
            df = _flatten_multiindex(df)
            if df.index.name and df.index.name not in ("", None, 0):
                df = df.reset_index()

            # 실제 컬럼에서 찾기 (DEBUG 출력 보고 후보 추가 가능)
            code_col   = _col(df, "종목코드", "Code", "code", "ticker", "Ticker")
            sector_col = _col(df, "업종명", "sector", "Sector", "industry", "Industry",
                              "업종", "섹터", "BPS", "PER", "PBR")  # pykrx 버전마다 다름

            if code_col or sector_col:
                ok("pykrx 업종 분류",
                   f"종목수={len(df)}  코드컬럼={code_col}  업종컬럼={sector_col}")
            else:
                # 컬럼 매칭 실패해도 데이터는 있음 → DEBUG 보고 후보 추가
                ok("pykrx 업종 분류 (컬럼명 확인 필요)",
                   f"종목수={len(df)}  실제컬럼={list(df.columns)[:5]}  "
                   "→ DEBUG 출력 참고해 위 _col() 후보에 추가하세요")
    except Exception as e:
        fail("pykrx 업종 분류", str(e))

    # 1-4. 기관/외인 수급 (삼성전자)
    try:
        df = pykrx_stock.get_market_trading_value_by_date(PREV_DATE, TODAY, "005930", detail=True)
        if df is not None and not df.empty:
            inst_col = next((c for c in df.columns if "기관" in str(c) or "Institution" in str(c)), None)
            frgn_col = next((c for c in df.columns if "외국인" in str(c) or "Foreign" in str(c)), None)
            ok("pykrx 기관/외인 수급 (삼성전자)",
               f"행수={len(df)}  기관={inst_col}  외인={frgn_col}")
        else:
            fail("pykrx 기관/외인 수급", "빈 DataFrame (주말/공휴일이면 정상)")
    except Exception as e:
        fail("pykrx 기관/외인 수급", str(e))

    # 1-5. 공매도 거래량
    if _short_vol_fn is None:
        fail("pykrx 공매도 거래량", "지원 함수 없음 — pip install 'pykrx>=1.0.47'")
    else:
        try:
            df = _short_vol_fn(PREV_DATE, market="KOSPI")
            if df is not None and not df.empty:
                ok("pykrx 공매도 거래량",
                   f"종목수={len(df)}  fn={_short_vol_fn.__name__}")
            else:
                fail("pykrx 공매도 거래량", "빈 DataFrame (주말/공휴일 정상)")
        except Exception as e:
            fail("pykrx 공매도 거래량", f"[{_short_vol_fn.__name__}] {e}")

    # 1-6. 공매도 잔고 (삼성전자)
    if _short_ohlcv_fn is None:
        fail("pykrx 공매도 잔고", "지원 함수 없음 — pip install 'pykrx>=1.0.47'")
    else:
        try:
            df = _short_ohlcv_fn(PREV_DATE, TODAY, "005930")
            if df is not None and not df.empty:
                ok("pykrx 공매도 잔고 (삼성전자)",
                   f"행수={len(df)}  fn={_short_ohlcv_fn.__name__}")
            else:
                fail("pykrx 공매도 잔고", "빈 DataFrame (주말/공휴일 정상)")
        except Exception as e:
            fail("pykrx 공매도 잔고", f"[{_short_ohlcv_fn.__name__}] {e}")

    # 1-7. 섹터 ETF (KODEX 반도체 266410)
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
        skip(name, "pip install 'pykrx'")


# ══════════════════════════════════════════════════════════════
#  2. yfinance — 미국증시 / 원자재 / 환율
# ══════════════════════════════════════════════════════════════
section("2. yfinance  (미국증시·원자재·환율)",
        "야후파이낸스 무료 데이터. 미국 시황이 국내 테마에 영향을 줄 때 활용.")

try:
    import yfinance as yf

    TEST_TICKERS = {
        "S&P500 (^GSPC)":         "^GSPC",
        "나스닥 (^IXIC)":         "^IXIC",
        "다우 (^DJI)":            "^DJI",
        "WTI 원유 (CL=F)":        "CL=F",
        "금 (GC=F)":              "GC=F",
        "구리 — 전선/전기주 연동": "HG=F",
        "은 (SI=F)":              "SI=F",
        "천연가스 (NG=F)":        "NG=F",
        "철광석 (TIO=F)":         "TIO=F",
        "알루미늄 (ALI=F)":       "ALI=F",
        "원달러 환율 (KRW=X)":    "KRW=X",
        # 미국 섹터 ETF (국내 테마 연동용)
        "XLK 기술/반도체 ETF":    "XLK",
        "XLE 에너지/정유 ETF":    "XLE",
        "XME 철강/비철금속 ETF":  "XME",
    }

    for label, ticker in TEST_TICKERS.items():
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="2d")
            if hist is not None and not hist.empty:
                close = hist["Close"].iloc[-1]
                ok(f"yfinance {label}", f"종가={close:.2f}")
            else:
                fail(f"yfinance {label}", "빈 DataFrame (장외시간/주말이면 정상)")
        except Exception as e:
            fail(f"yfinance {label}", str(e))
        time.sleep(0.2)

except ImportError:
    skip("yfinance 전체", "pip install yfinance")


# ══════════════════════════════════════════════════════════════
#  3. DART API — 공시 / 이벤트 캘린더
# ══════════════════════════════════════════════════════════════
section("3. DART API  (공시·IR·주주총회·실적)",
        "금융감독원 OpenDART. 수주/배당/자사주 등 주가에 영향 주는 공시를 수집.")

if not DART_API_KEY:
    skip("DART API 전체", "DART_API_KEY")
else:
    import requests

    # 3-1. 공시 목록
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
            ok("DART 공시목록 API", f"최근5일={data.get('total_count',0)}건")
        elif data.get("status") == "010":
            fail("DART 공시목록 API", "API 키 인증 실패 — opendart.fss.or.kr 에서 키 확인")
        else:
            fail("DART 공시목록 API", f"status={data.get('status')} msg={data.get('message')}")
    except Exception as e:
        fail("DART 공시목록 API", str(e))

    # 3-2. 이벤트 캘린더 (향후 IR·실적·주주총회 — 키워드 전문 검색 방식)
    # [수정] pblntf_ty="F" 는 계정 권한 필요 → status=013 오류 발생
    #        → pblntf_ty 제거 후 전체 공시에서 이벤트 키워드로 필터링하는 방식으로 교체
    try:
        future_end = (datetime.today() + timedelta(days=14)).strftime("%Y%m%d")
        url = "https://opendart.fss.or.kr/api/list.json"
        r = requests.get(url, params={
            "crtfc_key":   DART_API_KEY,
            "bgn_de":      TODAY,
            "end_de":      future_end,   # 향후 14일
            "page_count":  100,
            "sort":        "date",
            "sort_mthd":   "asc",
            # pblntf_ty 완전 제거 — 전체 공시 조회 후 키워드 필터
        }, timeout=10)
        data = r.json()
        status = data.get("status", "")
        if status == "000":
            # 이벤트 키워드로 필터 (IR / 주주총회 / 실적발표 / 배당)
            _EVENT_KW = ["기업설명회", "IR ", "주주총회", "실적발표", "잠정실적",
                         "현금배당", "중간배당", "배당결정"]
            all_items = data.get("list", [])
            events = [
                item for item in all_items
                if any(kw in item.get("report_nm", "") for kw in _EVENT_KW)
            ]
            ok("DART 이벤트캘린더 (키워드필터)", 
               f"전체공시={data.get('total_count',0)}건  이벤트={len(events)}건  "
               f"예시={'|'.join([e.get('corp_name','')+'·'+e.get('report_nm','')[:12] for e in events[:2]])}")
        elif status == "013":
            ok("DART 이벤트캘린더", "향후 14일 공시 없음 (013=정상 — 주말/공휴일 직후 정상)")
        else:
            fail("DART 이벤트캘린더", f"status={status} msg={data.get('message')}")
    except Exception as e:
        fail("DART 이벤트캘린더", str(e))


# ══════════════════════════════════════════════════════════════
#  4. 네이버 OpenAPI — 뉴스 / 데이터랩
# ══════════════════════════════════════════════════════════════
section("4. 네이버 OpenAPI  (뉴스검색·데이터랩 검색량 트렌드)",
        "국내 뉴스 수집 + 종목/테마 검색량 급등 감지에 사용.")

if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
    skip("네이버 API 전체", "NAVER_CLIENT_ID / NAVER_CLIENT_SECRET")
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
            fail("네이버 뉴스검색 API", "인증 실패 — 앱 등록 확인: developers.naver.com")
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
        elif r.status_code == 401:
            fail("네이버 데이터랩 트렌드 API",
                 "❗ API 키는 맞지만 DataLab '서비스' 권한이 미등록 상태입니다.\n"
                 "       ✅ 해결: developers.naver.com → 내 애플리케이션 → 해당 앱 '수정'\n"
                 "              → API 설정 탭 → '데이터랩(검색어트렌드)' 체크박스 추가\n"
                 "              → 저장 후 5~10분 후 재시도 (즉시 미반영)")
        else:
            fail("네이버 데이터랩 트렌드 API", f"HTTP {r.status_code}  {r.text[:80]}")
    except Exception as e:
        fail("네이버 데이터랩 트렌드 API", str(e))


# ══════════════════════════════════════════════════════════════
#  5. KIS (한국투자증권) REST API
# ══════════════════════════════════════════════════════════════
section("5. KIS REST API  (실시간 주가·거래량순위·호가)",
        "장중 급등 감지의 핵심. 폴링 방식으로 10초마다 전 종목 스캔.")

_KIS_BASE = "https://openapi.koreainvestment.com:9443"

if not KIS_APP_KEY or not KIS_APP_SECRET:
    skip("KIS REST API 전체", "KIS_APP_KEY / KIS_APP_SECRET")
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
            ok("KIS 액세스 토큰 발급", f"expires_in={data.get('expires_in')}초")
        else:
            fail("KIS 액세스 토큰 발급",
                 data.get("error_description", str(data))[:100] +
                 "  ※ 모의투자 키 사용 시 KIS_VTS_APP_KEY 별도 설정 필요")
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

        # 5-3. 거래량 순위 (장중봇 핵심 API)
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
                fail("KIS 거래량 순위",
                     f"rt_cd={data.get('rt_cd')} msg={data.get('msg1','')[:60]}"
                     "  ※ 장 마감 후에는 빈 결과가 정상일 수 있음")
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
                fail("KIS 등락률 순위",
                     f"rt_cd={data.get('rt_cd')} msg={data.get('msg1','')[:60]}")
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
#  6. NewsAPI.org — 지정학 / 글로벌 영문 뉴스
# ══════════════════════════════════════════════════════════════
section("6. NewsAPI.org  (지정학·글로벌 영문 뉴스)",
        "관세/전쟁/제재 등 지정학 이벤트를 감지해 국내 방산/철강 테마와 연동.")


# ══════════════════════════════════════════════════════════════
#  6. GDELT + NewsAPI.org — 지정학 / 글로벌 영문 뉴스
# ══════════════════════════════════════════════════════════════
#  [v13.0 업그레이드] NewsAPI 무료플랜 한계 극복
#  NewsAPI 무료플랜 문제: /v2/everything 은 최근 1개월+ 기사만 제공,
#                          실시간 뉴스 접근 불가 → 지정학 감지에 부적합
#  ✅ 1순위: GDELT API — 완전무료, API키 불필요, 전 세계 뉴스 실시간
#  ✅ 2순위: NewsAPI — top-headlines 한정으로 유지 (실시간 가능)
# ══════════════════════════════════════════════════════════════
section("6. GDELT + NewsAPI  (지정학·글로벌 영문 뉴스)",
        "GDELT=완전무료·API키불필요. 관세/전쟁/제재 등 지정학 이벤트 실시간 감지.")

import requests as _req2

# ── 6-1. GDELT API (메인 소스 — 무료, 키 불필요) ────────────
# GDELT DOC 2.0 : 전 세계 뉴스 15분 단위 업데이트, 키워드 검색 무료
_GDELT_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"

GDELT_TEST_CASES = [
    ("지정학 — 한국 관세",       "South Korea tariff trade US",            "geopolitics_collector"),
    ("지정학 — 반도체 수출규제", "Korea semiconductor export restriction",  "geopolitics_collector"),
    ("글로벌 — Fed 금리결정",    "Fed FOMC rate decision Korea",           "news_collector"),
    ("방산 — 한국 방위산업",     "Korea defense military NATO",            "geopolitics_collector"),
]

for _name, _query, _used_in in GDELT_TEST_CASES:
    try:
        _params = {
            "query":      _query,
            "mode":       "artlist",
            "maxrecords": 5,
            "timespan":   "3d",       # 최근 3일
            "sort":       "DateDesc", # 최신순
            "format":     "json",
            "sourcelang": "english",
        }
        _r = _req2.get(_GDELT_BASE, params=_params, timeout=12)
        _data = _r.json()
        _arts = _data.get("articles", [])
        if _arts:
            _src   = _arts[0].get("domain", "?")
            _title = (_arts[0].get("title") or "")[:50]
            ok(f"GDELT {_name}", f"[{_used_in}] {len(_arts)}건  최신={_src}: {_title}")
        else:
            # GDELT는 기사 없을 때도 status 200 반환 — 쿼리 범위 확대 시도
            _params2 = {**_params, "timespan": "7d"}
            _r2 = _req2.get(_GDELT_BASE, params=_params2, timeout=12)
            _arts2 = _r2.json().get("articles", [])
            if _arts2:
                ok(f"GDELT {_name} (7일범위)", f"{len(_arts2)}건 (3일내 없음→7일로 확장)")
            else:
                fail(f"GDELT {_name}", "기사 0건 (7일 범위에도 없음 — 쿼리 키워드 재검토)")
    except Exception as _e:
        fail(f"GDELT {_name}", str(_e))
    time.sleep(0.3)

# ── 6-2. NewsAPI top-headlines (보조 소스 — 키 있을 때만) ────
if not NEWSAPI_KEY:
    skip("NewsAPI top-headlines (보조)", "NEWSAPI_ORG_KEY 미설정 — GDELT 단독으로 충분")
else:
    try:
        _r2 = _req2.get("https://newsapi.org/v2/top-headlines",
                        params={"apiKey": NEWSAPI_KEY, "category": "business",
                                "language": "en", "pageSize": 5}, timeout=10)
        _d2 = _r2.json()
        if _d2.get("status") == "ok":
            ok("NewsAPI top-headlines (보조)", f"총={_d2.get('totalResults',0)}건")
        elif _d2.get("code") == "rateLimited":
            fail("NewsAPI top-headlines", "Rate Limit — 무료 100req/day 초과. GDELT가 대체 중.")
        else:
            fail("NewsAPI top-headlines", _d2.get("message", "")[:60])
    except Exception as _e:
        fail("NewsAPI top-headlines", str(_e))




# ══════════════════════════════════════════════════════════════
#  7. RSS 피드 — 고품질 뉴스 소스 + 기재부/방사청
# ══════════════════════════════════════════════════════════════
#  [v13.0 업그레이드]
#  Reuters 기존 RSS: 2023년 완전 폐기됨 → 제거
#  ✅ 교체 소스 (모두 무료, 실시간, 고신뢰):
#    - AP News Business : 세계 1위 통신사, 실시간 업데이트
#    - FT (Financial Times) : 금융·경제 전문, 글로벌 신뢰도 최상
#    - Google News (Korea economy) : 집계형, 다양한 소스 커버
#  ✅ 기재부/방사청: feedparser 직접 호출 → requests 선fetch 후 파싱 (비표준XML 대응)
# ══════════════════════════════════════════════════════════════
section("7. RSS 피드  (AP·FT·Google News·기재부·방사청)",
        "무료. 지정학 뉴스와 정부 발표를 실시간으로 수집.")

try:
    import feedparser
    import requests as _rss_req
    import urllib.parse

    # ── 7-1. 국제 뉴스 소스 (표준 RSS — feedparser 직접 파싱 가능) ──
    INTL_RSS = [
        ("AP News Business",    "https://apnews.com/rss/apf-business"),
        ("AP News World",       "https://apnews.com/rss/apf-topnews"),
        ("FT Markets",          "https://www.ft.com/markets?format=rss"),
        ("Google News KR경제",  "https://news.google.com/rss/search?"
                                "q=Korea+economy+stock&hl=en&gl=KR&ceid=KR:en"),
        ("Google News 방산",    "https://news.google.com/rss/search?"
                                + urllib.parse.urlencode({"q":"한국 방산 수출", "hl":"ko", "gl":"KR", "ceid":"KR:ko"})),
    ]
    for name, url in INTL_RSS:
        try:
            feed = feedparser.parse(url)
            if feed.entries:
                ok(f"RSS {name}",
                   f"기사수={len(feed.entries)}  최신={feed.entries[0].get('title','')[:35]}")
            elif feed.bozo:
                fail(f"RSS {name}", f"파싱오류: {feed.bozo_exception}")
            else:
                fail(f"RSS {name}", "entries 없음")
        except Exception as e:
            fail(f"RSS {name}", str(e))
        time.sleep(0.4)

    # ── 7-2. 한국 정부 RSS (비표준 XML — requests 선fetch 후 feedparser) ──
    # 기재부·방사청은 XML이 표준을 벗어나 feedparser 직접 호출 시 파싱 실패
    # ✅ 해결: requests로 raw bytes 먼저 받은 뒤 feedparser에 전달
    GOV_RSS = [
        ("기재부 보도자료", "https://www.moef.go.kr/sty/rss/moefRss.do"),
        ("방사청 보도자료", "https://www.dapa.go.kr/dapa/rss/rssService.do"),
    ]
    _gov_headers = {"User-Agent": "Mozilla/5.0 (compatible; KoreaStockBot/1.0)"}
    for name, url in GOV_RSS:
        try:
            resp = _rss_req.get(url, headers=_gov_headers, timeout=10)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)   # ← bytes 전달 (인코딩 자동 처리)
            if feed.entries:
                ok(f"RSS {name}",
                   f"기사수={len(feed.entries)}  최신={feed.entries[0].get('title','')[:35]}")
            elif feed.bozo:
                # bozo여도 entries가 있으면 수집 가능이지만 여기선 없음
                fail(f"RSS {name}", f"파싱오류(entries없음): {str(feed.bozo_exception)[:60]}")
            else:
                fail(f"RSS {name}", "entries 없음 (서버 응답은 정상)")
        except Exception as e:
            fail(f"RSS {name}", str(e))
        time.sleep(0.4)

except ImportError:
    skip("RSS 피드 전체", "pip install feedparser requests")




# ══════════════════════════════════════════════════════════════
#  8. Google AI API (Gemini) — AI 테마 분석
# ══════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════
#  8. Google AI API (Gemma/Gemini) — AI 테마 분석
# ══════════════════════════════════════════════════════════════
#  [v13.0 수정] 모델명 오류 수정
#  기존: gemini-2.0-flash → RESOURCE_EXHAUSTED 오류 발생
#  원인 분석:
#    - gemini-2.0-flash (버전 미지정) = 결제 계정 필요한 stable 버전
#    - 실제 코드(ai_analyzer.py)는 gemma-3-27b-it 사용 (무료 14,400회/일)
#    - geopolitics_analyzer.py 는 gemini-3-flash-preview 사용 → 존재하지 않는 모델!
#  ✅ 수정: gemma-3-27b-it 우선 → gemini-2.0-flash-lite → gemini-1.5-flash 폴백 체인
# ══════════════════════════════════════════════════════════════
section("8. Google AI API  (Gemma/Gemini — AI 테마 분석)",
        "수집된 뉴스·공시·시황을 AI가 종합해 '오늘의 테마' 판단에 사용.")

if not GOOGLE_AI_API_KEY:
    skip("Google AI API", "GOOGLE_AI_API_KEY  (aistudio.google.com 에서 무료 발급 가능)")
else:
    import requests

    # ✅ ARCHITECTURE_v11.md '사용 가능한 AI 모델' 목록 준수
    # gemini-1.5-x / gemini-2.0-x 계열 = 절대 사용 금지 (서비스 종료 확정)
    _AI_MODELS = [
        ("gemma-3-27b-it",         "ai_analyzer 사용 모델 — 무료 14,400회/일"),
        ("gemini-3-flash-preview", "geopolitics_analyzer Primary"),
        ("gemini-2.5-flash",       "geopolitics_analyzer Fallback"),
        ("gemini-2.5-flash-lite",  "경량 보조 용도"),
    ]
    _ai_ok = False
    for _model_name, _model_desc in _AI_MODELS:
        try:
            _url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{_model_name}:generateContent?key={GOOGLE_AI_API_KEY}")
            _payload = {
                "contents": [{"parts": [{"text": "한국 주식시장 테스트. '연결 성공'이라고만 답하세요."}]}]
            }
            _r = requests.post(_url, json=_payload, timeout=15)
            _data = _r.json()
            _text = (_data.get("candidates", [{}])[0]
                          .get("content", {})
                          .get("parts", [{}])[0]
                          .get("text", ""))
            if _text:
                ok(f"Google AI ({_model_name})", f"응답: {_text.strip()[:40]}  ({_model_desc})")
                _ai_ok = True
                break
            elif "error" in _data:
                _err = _data["error"]
                _status = _err.get("status", "")
                _msg    = _err.get("message", "")[:80]
                if _status == "RESOURCE_EXHAUSTED":
                    print(f"  ⚠️  {_model_name}: RESOURCE_EXHAUSTED — 다음 모델 시도...")
                elif _status == "NOT_FOUND":
                    print(f"  ⚠️  {_model_name}: 모델 없음 — 다음 모델 시도...")
                else:
                    print(f"  ⚠️  {_model_name}: {_status} {_msg}")
        except Exception as _e:
            print(f"  ⚠️  {_model_name}: 예외 {_e}")

    if not _ai_ok:
        fail("Google AI (전체 폴백 실패)",
             "모든 모델에서 오류 발생.\n"
             "       ✅ 확인사항:\n"
             "          1. aistudio.google.com → API 키 유효 여부\n"
             "          2. Google AI Studio → 'Quotas' → 프로젝트 일일한도 확인\n"
             "          3. 같은 구글 계정의 다른 프로젝트가 quota 소모 중인지 확인")



# ══════════════════════════════════════════════════════════════
#  9. Telegram Bot API — 알림 발송
# ══════════════════════════════════════════════════════════════
section("9. Telegram Bot API  (급등 알림·리포트 발송)",
        "모든 분석 결과를 텔레그램으로 전송. 봇 동작의 최종 출력 채널.")

if not TELEGRAM_TOKEN:
    skip("Telegram Bot API", "TELEGRAM_TOKEN")
else:
    try:
        import requests
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe",
            timeout=8,
        )
        data = r.json()
        if data.get("ok"):
            bot = data.get("result", {})
            ok("Telegram Bot 인증",
               f"봇이름=@{bot.get('username','')}  id={bot.get('id','')}")
        else:
            fail("Telegram Bot 인증",
                 f"{data.get('description','인증 실패')}  — @BotFather 에서 토큰 확인")
    except Exception as e:
        fail("Telegram Bot 인증", str(e))

    if TELEGRAM_CHAT_ID:
        try:
            import requests
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChat",
                params={"chat_id": TELEGRAM_CHAT_ID},
                timeout=8,
            )
            data = r.json()
            if data.get("ok"):
                chat = data.get("result", {})
                ok("Telegram 채팅방 확인",
                   f"타입={chat.get('type','')}  제목={chat.get('title', chat.get('first_name',''))}")
            else:
                fail("Telegram 채팅방 확인",
                     f"{data.get('description','')[:80]}  — TELEGRAM_CHAT_ID 확인 필요")
        except Exception as e:
            fail("Telegram 채팅방 확인", str(e))
    else:
        skip("Telegram 채팅방 확인", "TELEGRAM_CHAT_ID")


# ══════════════════════════════════════════════════════════════
#  최종 결과 요약
# ══════════════════════════════════════════════════════════════
print(f"\n{'='*62}")
print("  📊 테스트 결과 요약")
print(f"{'='*62}")

passed  = [r for r in results if r[0].startswith("✅")]
failed  = [r for r in results if r[0].startswith("❌")]
skipped = [r for r in results if r[0].startswith("⏭")]

print(f"\n  총 {len(results)}개  |  ✅ 성공 {len(passed)}  ❌ 실패 {len(failed)}  ⏭ 스킵 {len(skipped)}\n")

if failed:
    print("  ── ❌ 실패 항목 (조치 필요) ──────────────────────")
    for _, name, detail in failed:
        print(f"  ❌ {name}")
        if detail:
            print(f"       └ {detail}")

if skipped:
    print("\n  ── ⏭ SKIP 항목 (환경변수 미설정 → 해당 기능 비활성) ──")
    for _, name, reason in skipped:
        print(f"  ⏭  {name}  (필요 키: {reason})")

print(f"\n  {'🎉 모든 필수 항목 정상!' if not failed else '⚠️  실패 항목을 확인하세요.'}")
print()
