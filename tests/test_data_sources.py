"""
korea_stock_bot — 외부 데이터 소스 연결 테스트 (핵심 3개)
===========================================================
테스트 대상: DART 공시 / GDELT 영문 뉴스 / RSS 피드

[실행]
    python test_data_sources.py
"""

import os, sys, time, json
from pathlib import Path
from datetime import datetime, timedelta

# ─── .env 로드 ────────────────────────────────────────────────
from dotenv import load_dotenv

_env_path = os.environ.get("ENV_PATH")
if _env_path:
    load_dotenv(dotenv_path=_env_path)
else:
    for _candidate in [Path(__file__).parent / ".env",
                        Path(__file__).parent.parent / ".env",
                        Path.cwd() / ".env"]:
        if _candidate.exists():
            load_dotenv(dotenv_path=str(_candidate))
            break
    else:
        load_dotenv()

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

def section(title, description=""):
    print(f"\n{'='*62}")
    print(f"  {title}")
    if description:
        print(f"  💬 {description}")
    print(f"{'='*62}")

# ─── 환경변수 ─────────────────────────────────────────────────
DART_API_KEY = os.environ.get("DART_API_KEY")
NEWSAPI_KEY  = os.environ.get("NEWSAPI_ORG_KEY") or os.environ.get("GOOGLE_NEWS_API_KEY", "")

TODAY     = datetime.today().strftime("%Y%m%d")
TODAY_KR  = datetime.today().strftime("%Y-%m-%d")

print(f"\n{'='*62}")
print("  🔑 환경변수 현황")
print(f"{'='*62}")
for _var, _val, _desc in [
    ("DART_API_KEY",   DART_API_KEY, "금융감독원 공시"),
    ("NEWSAPI_ORG_KEY", NEWSAPI_KEY, "영문 뉴스 (보조)"),
]:
    _st = "✅ 있음" if _val else "❌ 없음"
    _mk = ("*"*(len(_val)-4)+_val[-4:]) if _val and len(_val)>4 else ("-" if not _val else "설정됨")
    print(f"  {_st}  {_var:<26} ({_desc})  {_mk}")


# ══════════════════════════════════════════════════════════════
#  1. DART API — 공시 / 이벤트 캘린더
# ══════════════════════════════════════════════════════════════
section("1. DART API  (공시·IR·주주총회·실적)",
        "금융감독원 OpenDART. 수주/배당/자사주 등 주가에 영향 주는 공시를 수집.")

if not DART_API_KEY:
    skip("DART API 전체", "DART_API_KEY 환경변수 미설정")
else:
    import requests

    # 1-1. 공시목록 API (최근 5일)
    try:
        bgn_5d = (datetime.today() - timedelta(days=5)).strftime("%Y%m%d")
        r = requests.get("https://opendart.fss.or.kr/api/list.json", params={
            "crtfc_key":  DART_API_KEY,
            "bgn_de":     bgn_5d,
            "end_de":     TODAY,
            "page_count": 10,
        }, timeout=10)
        data = r.json()
        if data.get("status") == "000":
            ok("DART 공시목록 API", f"최근5일={data.get('total_count',0)}건")
        else:
            fail("DART 공시목록 API", f"status={data.get('status')} msg={data.get('message')}")
    except Exception as e:
        fail("DART 공시목록 API", str(e))

    # 1-2. 이벤트 캘린더
    # ── 왜 이벤트 0건이 나왔는가? ──────────────────────────────
    # DART list.json 은 공시 "접수일" 기준으로 검색한다.
    # 기존 코드에서 bgn_de=TODAY (오늘=주말) 로 설정해서
    # "오늘 이후 접수된 공시"를 조회했는데 → 주말엔 공시 자체가 없어서 4건 뿐
    # 그 4건도 주주총회·IR 키워드가 없는 일반 공시라 이벤트 0건으로 나온 것.
    #
    # ✅ 수정: bgn_de를 과거 30일로 설정
    #   → "최근 30일 내 접수된 이벤트 관련 공시" 조회
    #   → 주주총회·IR 공시는 보통 수 주 전에 미리 접수됨
    #   → 이 방식이 실제 event_calendar_collector.py 의 동작 방식과 동일
    try:
        bgn_30d = (datetime.today() - timedelta(days=30)).strftime("%Y%m%d")
        r2 = requests.get("https://opendart.fss.or.kr/api/list.json", params={
            "crtfc_key":  DART_API_KEY,
            "bgn_de":     bgn_30d,   # 과거 30일 — 접수된 이벤트 공시 확인
            "end_de":     TODAY,
            "page_count": 100,
            "sort":       "date",
            "sort_mthd":  "desc",
        }, timeout=10)
        data2 = r2.json()
        status2 = data2.get("status", "")

        if status2 == "000":
            _EVENT_KW = [
                "기업설명회", "IR ", "NDR", "기업탐방",
                "주주총회", "임시주주총회", "정기주주총회",
                "실적발표", "영업실적", "잠정실적", "분기실적",
                "현금배당", "중간배당", "특별배당", "배당결정",
            ]
            all_items = data2.get("list", [])
            events = [
                item for item in all_items
                if any(kw in item.get("report_nm", "") for kw in _EVENT_KW)
            ]

            if events:
                _sample = " | ".join(
                    f"{e.get('corp_name','')} [{e.get('report_nm','')[:14]}]"
                    for e in events[:3]
                )
                ok("DART 이벤트캘린더 (최근30일)",
                   f"이벤트공시={len(events)}건  예시={_sample}")
            else:
                # 이벤트 공시가 진짜 없으면 — 전체 공시 샘플 출력해서 확인 가능하게
                _all_sample = " | ".join(
                    f"[{i.get('report_nm','')[:12]}]" for i in all_items[:5]
                )
                fail("DART 이벤트캘린더 (최근30일)",
                     f"이벤트 키워드 매칭 0건 (전체={data2.get('total_count',0)}건)  "
                     f"공시샘플={_all_sample}")

        elif status2 == "013":
            ok("DART 이벤트캘린더", "조회 데이터 없음 (013 정상)")
        else:
            fail("DART 이벤트캘린더", f"status={status2} msg={data2.get('message')}")
    except Exception as e:
        fail("DART 이벤트캘린더", str(e))


# ══════════════════════════════════════════════════════════════
#  2. GDELT + NewsAPI — 지정학·글로벌 영문 뉴스
# ══════════════════════════════════════════════════════════════
#  GDELT: 완전무료, API키 불필요, 15분 단위 업데이트
#  주의: 동일 IP 연속 요청 시 빈 body(rate limit) 반환
#        → 단일 통합쿼리로 1회만 호출
# ══════════════════════════════════════════════════════════════
section("2. GDELT + NewsAPI  (지정학·글로벌 영문 뉴스)",
        "GDELT=완전무료·API키불필요. 관세/방산/반도체 등 지정학 이벤트 실시간 감지.")

import requests as _req2

# ── 2-1. GDELT ────────────────────────────────────────────────
_GDELT_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"

def _gdelt_freshness(seendate_str: str) -> str:
    try:
        from datetime import timezone
        dt = datetime.strptime(seendate_str[:14], "%Y%m%d%H%M%S")
        dt = dt.replace(tzinfo=timezone.utc)
        h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        return f"{int(h*60)}분 전" if h < 1 else (f"약 {int(h)}시간 전" if h < 24 else f"약 {int(h/24)}일 전")
    except Exception:
        return seendate_str[:10] if seendate_str else "?"

_GDELT_QUERY = (
    '"South Korea" OR "KOSPI" OR "Korea tariff" OR '
    '"Korea defense" OR "Korea semiconductor" OR "Korea trade"'
)

try:
    _gr = _req2.get(_GDELT_BASE, params={
        "query":      _GDELT_QUERY,
        "mode":       "artlist",
        "maxrecords": 10,
        "timespan":   "3d",
        "sort":       "DateDesc",
        "format":     "json",
        "sourcelang": "english",
    }, timeout=25)

    # ── 응답 검증 (rate limit 시 빈 body 또는 HTML 에러 페이지 반환) ──
    _raw_text = _gr.text.strip() if _gr.text else ""
    _is_json  = _raw_text.startswith("{") or _raw_text.startswith("[")

    if not _raw_text:
        fail("GDELT 연결 테스트",
             "빈 응답 — rate limit. 잠시 후 단독 실행 시 정상 동작함. "
             "봇 실운영(30분 간격)에서는 rate limit 없음")
    elif not _is_json:
        fail("GDELT 연결 테스트",
             f"JSON 아닌 응답 반환 (HTML 에러 등) — 첫 50자: {_raw_text[:50]}")
    else:
        _gdata = _gr.json()
        _garts = _gdata.get("articles", [])
        if _garts:
            _fresh = _gdelt_freshness(_garts[0].get("seendate", ""))
            _dom   = _garts[0].get("domain", "?")
            _title = (_garts[0].get("title") or "")[:45]
            ok("GDELT 연결 테스트 (한국 관련 통합쿼리)",
               f"기사수={len(_garts)}건  최신기사={_fresh}  소스={_dom}: {_title}")

            # 최신성 검증: 24시간 이내 기사 비율
            from datetime import timezone as _tz
            _fresh_cnt = sum(
                1 for a in _garts
                if (lambda sd: (
                    (datetime.now(_tz.utc) - datetime.strptime(sd[:14], "%Y%m%d%H%M%S")
                     .replace(tzinfo=_tz.utc)).total_seconds() / 3600 <= 24
                ) if sd else False)(a.get("seendate", ""))
            )
            if _fresh_cnt > 0:
                ok("GDELT 최신성", f"24시간 이내 기사 {_fresh_cnt}/{len(_garts)}건 — 실시간 수집 정상")
            else:
                fail("GDELT 최신성",
                     f"24시간 이내 기사 0건 (가장 최근: {_fresh}) — 쿼리 또는 서버 상태 확인")
        else:
            fail("GDELT 연결 테스트", "기사 0건 (응답은 정상 JSON)")

except Exception as _ge:
    fail("GDELT 연결 테스트", str(_ge))

# ── 2-2. NewsAPI top-headlines (보조) ─────────────────────────
if not NEWSAPI_KEY:
    skip("NewsAPI top-headlines (보조)", "NEWSAPI_ORG_KEY 미설정 — GDELT 단독 운용")
else:
    try:
        _rn = _req2.get("https://newsapi.org/v2/top-headlines",
                        params={"apiKey": NEWSAPI_KEY, "category": "business",
                                "language": "en", "pageSize": 5}, timeout=10)
        _dn = _rn.json()
        if _dn.get("status") == "ok":
            ok("NewsAPI top-headlines (보조)", f"총={_dn.get('totalResults',0)}건")
        elif _dn.get("code") == "rateLimited":
            fail("NewsAPI top-headlines", "Rate Limit — 무료 100req/day 초과")
        else:
            fail("NewsAPI top-headlines", _dn.get("message", "")[:60])
    except Exception as _ne:
        fail("NewsAPI top-headlines", str(_ne))


# ══════════════════════════════════════════════════════════════
#  3. RSS 피드 — BBC / FT / Google News / korea.kr
# ══════════════════════════════════════════════════════════════
section("3. RSS 피드  (BBC·FT·Google News·Korea.kr)",
        "무료. 지정학 뉴스 + Korea.kr 정책브리핑으로 정부 발표 실시간 수집.")

try:
    import feedparser
    import requests as _rss_req
    import urllib.parse
    import email.utils as _eu
    from datetime import timezone as _tzr

    def _rss_freshness(entry) -> str:
        """RSS entry published → '약 N시간 전'"""
        for field in ("published", "updated"):
            val = entry.get(field, "")
            if not val:
                continue
            try:
                parsed = _eu.parsedate(val)
                if parsed:
                    dt = datetime(*parsed[:6], tzinfo=_tzr.utc)
                    h = (datetime.now(_tzr.utc) - dt).total_seconds() / 3600
                    if h < 1:   return f"{int(h*60)}분 전"
                    if h < 24:  return f"약 {int(h)}시간 전"
                    return f"약 {int(h/24)}일 전"
            except Exception:
                pass
        return "시각미상"

    # ── 3-1. 표준 RSS (feedparser 직접 파싱) ──────────────────
    _SIMPLE_RSS = [
        ("BBC Business",      "https://feeds.bbci.co.uk/news/business/rss.xml"),
        ("BBC World",         "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("Google News KR경제", "https://news.google.com/rss/search?"
                               "q=Korea+economy+stock&hl=en&gl=KR&ceid=KR:en"),
        ("Google News 방산",  "https://news.google.com/rss/search?"
                               + urllib.parse.urlencode({
                                   "q":"한국 방산 수출", "hl":"ko",
                                   "gl":"KR", "ceid":"KR:ko"})),
    ]
    for _name, _url in _SIMPLE_RSS:
        try:
            _feed = feedparser.parse(_url)
            if _feed.entries:
                _e0 = _feed.entries[0]
                ok(f"RSS {_name}",
                   f"기사수={len(_feed.entries)}건  최신={_rss_freshness(_e0)}"
                   f"  제목={_e0.get('title','')[:30]}")
            elif _feed.bozo:
                fail(f"RSS {_name}", f"파싱오류: {_feed.bozo_exception}")
            else:
                fail(f"RSS {_name}", "entries 없음")
        except Exception as e:
            fail(f"RSS {_name}", str(e))
        time.sleep(0.4)

    # ── 3-2. requests 선fetch 소스 (SSL 우회 / 비표준 인코딩) ──
    _FETCH_RSS = [
        # FT: Windows 환경 SSL 인증서 오류 → verify=False
        ("FT Markets", "https://www.ft.com/markets?format=rss", False),
    ]
    for _name, _url, _verify in _FETCH_RSS:
        try:
            _hdrs = {"User-Agent": "Mozilla/5.0 (compatible; KoreaStockBot/1.0)"}
            _resp = _rss_req.get(_url, headers=_hdrs, timeout=10, verify=_verify)
            _resp.raise_for_status()
            _feed = feedparser.parse(_resp.content)
            if _feed.entries:
                _e0 = _feed.entries[0]
                ok(f"RSS {_name}",
                   f"기사수={len(_feed.entries)}건  최신={_rss_freshness(_e0)}"
                   f"  제목={_e0.get('title','')[:30]}")
            elif _feed.bozo:
                fail(f"RSS {_name}", f"파싱오류: {str(_feed.bozo_exception)[:60]}")
            else:
                fail(f"RSS {_name}", "entries 없음")
        except Exception as e:
            fail(f"RSS {_name}", str(e))
        time.sleep(0.4)

    # ── 3-3. 한국 정부 RSS ─────────────────────────────────────
    # 기재부(moef.go.kr): 서버가 깨진 XML 반환 → 수정 불가, skip
    # 방사청(dapa.go.kr): 404 서비스 폐지 → skip
    skip("RSS 기재부 (moef.go.kr)", "서버가 깨진 XML 반환 — 서버 측 문제로 수정 불가")
    skip("RSS 방사청 (dapa.go.kr)", "404 — 서비스 폐지됨")

    # ── korea.kr 정책브리핑 RSS (정부 통합 보도자료) ───────────
    # 기재부·행안부·과기부 등 전 부처 통합
    # 비표준 XML: invalid token 포함 → bozo여도 entries가 있으면 수집 가능
    _KR_GOV_HEADERS = {
        "User-Agent": "Mozilla/5.0 (compatible; KoreaStockBot/1.0)",
        "Accept":     "application/rss+xml, text/xml, */*",
        "Referer":    "https://www.korea.kr/",
    }
    try:
        _kr = _rss_req.get("https://www.korea.kr/rss/policyNewsAll.do",
                           headers=_KR_GOV_HEADERS, timeout=12)
        _kr.raise_for_status()

        # ── XML 전처리: invalid token 제거 ──────────────────────
        # 오류 위치(line 290, col 30)에 HTML 엔티티(&nbsp; 등) 또는
        # 제어문자가 포함돼 있어 파싱 실패
        # 전략: EUC-KR 디코딩 → 제어문자 제거 → UTF-8 재인코딩
        import re as _re
        _raw_bytes  = _kr.content
        _ktext_raw  = _raw_bytes.decode("euc-kr", errors="replace")
        # 0x00~0x1F 범위 제어문자 제거 (탭·개행·CR 제외)
        _ktext_clean = _re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', _ktext_raw)
        # &nbsp; → 공백, &amp; 중복 → &amp;
        _ktext_clean = _ktext_clean.replace("&nbsp;", " ").replace("&copy;", "©")
        _kfeed = feedparser.parse(_ktext_clean.encode("utf-8"))

        if _kfeed.entries:
            _ke0   = _kfeed.entries[0]
            _ktitle = _ke0.get("title", "")[:35]
            _kfresh = _rss_freshness(_ke0)
            ok("RSS korea.kr 정책브리핑 (기재부 대체)",
               f"기사수={len(_kfeed.entries)}건  최신={_kfresh}  제목={_ktitle}")
        elif _kfeed.bozo:
            # bozo지만 partial entries 있을 수 있음 — 실제 봇 코드는 entries 있으면 수집함
            fail("RSS korea.kr 정책브리핑",
                 f"XML 비표준 파싱실패: {str(_kfeed.bozo_exception)[:60]}\n"
                 "       ℹ️  geopolitics_collector.py는 bozo여도 entries 있으면 수집하므로\n"
                 "            실제 봇 운영에는 영향 없을 수 있음. 별도 확인 권장")
        else:
            fail("RSS korea.kr 정책브리핑", "entries 없음")
    except Exception as _ke:
        fail("RSS korea.kr 정책브리핑", str(_ke))

except ImportError:
    skip("RSS 피드 전체", "pip install feedparser requests")


# ══════════════════════════════════════════════════════════════
#  📊 결과 요약
# ══════════════════════════════════════════════════════════════
print(f"\n{'='*62}")
print("  📊 테스트 결과 요약")
print(f"{'='*62}")

passed  = [r for r in results if r[0].startswith("✅")]
failed  = [r for r in results if r[0].startswith("❌")]
skipped = [r for r in results if r[0].startswith("⏭")]

print(f"\n  총 {len(results)}개  |  ✅ 성공 {len(passed)}  ❌ 실패 {len(failed)}  ⏭ 스킵 {len(skipped)}\n")

if failed:
    print("  ── ❌ 실패 항목 ──────────────────────────────────────")
    for _, name, detail in failed:
        print(f"  ❌ {name}")
        if detail:
            print(f"       └ {detail}")

if skipped:
    print("\n  ── ⏭ SKIP 항목 ──────────────────────────────────────")
    for _, name, reason in skipped:
        print(f"  ⏭  {name}  ({reason})")

print(f"\n  {'🎉 모든 필수 항목 정상!' if not failed else '⚠️  실패 항목을 확인하세요.'}\n")
