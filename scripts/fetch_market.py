"""
공시레이더 - 매일 시황 수집 스크립트 → data/market.json

데이터 출처
  - 코스피·코스닥·코스피200 지수, 전 종목 시세: FinanceDataReader의 KRX 일별 캐시
    (github.com/FinanceData/fdr_krx_data_cache, 장 마감 후 갱신)
  - 뉴욕증시(지수·업종 ETF·주요 종목), 환율·금리·원자재: Yahoo Finance (50분에 한 번)
  - 증권사 리포트 목록: 네이버 금융 리서치 (제목·증권사·날짜·링크) + PDF 앞부분을 읽어 AI 3~4줄 요약
  - AI 시황은 웹 검색으로 그날 특이사항(실적, 지표, 특징주)을 찾아 함께 정리 (검색 1회당 $0.01)
  - AI 시황 코멘트(선택): ANTHROPIC_API_KEY가 있으면 국내·뉴욕 각각 거래일마다 한 번 생성

각 항목은 실패해도 다른 항목과 공시 수집에 영향을 주지 않습니다.
"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from pathlib import Path

import requests

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT = DATA_DIR / "market.json"
DAYS_DIR = ROOT / "data" / "days"
CACHE = "https://raw.githubusercontent.com/FinanceData/fdr_krx_data_cache/refs/heads/master/data"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

KR_INDICES = [("ks11", "코스피"), ("kq11", "코스닥"), ("ks200", "코스피200")]
# 환율·금리·원자재 (시황 탭 '환율·금리·원자재')
GLOBAL = [
    ("KRW=X", "원/달러", "fx"), ("DX-Y.NYB", "달러인덱스", "fx"), ("^FVX", "미국 5년물", "rate"),
    ("^TNX", "미국 10년물", "rate"), ("^TYX", "미국 30년물", "rate"),
    ("CL=F", "WTI 유가", "cmdty"), ("GC=F", "금", "cmdty"), ("BTC-USD", "비트코인", "crypto"),
]
# 간밤 뉴욕증시
US_INDICES = [("^GSPC", "S&P 500"), ("^IXIC", "나스닥"), ("^DJI", "다우"), ("^RUT", "러셀2000"),
              ("^SOX", "필라델피아 반도체"), ("^VIX", "VIX(변동성)")]
US_SECTORS = [("XLK", "기술"), ("XLC", "커뮤니케이션"), ("XLY", "임의소비재"), ("XLF", "금융"),
              ("XLV", "헬스케어"), ("XLI", "산업재"), ("XLE", "에너지"), ("XLB", "소재"),
              ("XLP", "필수소비재"), ("XLU", "유틸리티"), ("XLRE", "부동산")]
US_STOCKS = [("NVDA", "엔비디아"), ("AAPL", "애플"), ("MSFT", "마이크로소프트"), ("AMZN", "아마존"),
             ("GOOGL", "알파벳"), ("META", "메타"), ("TSLA", "테슬라"), ("AVGO", "브로드컴"),
             ("TSM", "TSMC"), ("MU", "마이크론")]
YAHOO_EVERY_MIN = 50   # Yahoo 호출은 50분에 한 번만 (요청 제한 회피)

# 증권사 시황·투자정보 리포트 목록 (제목·링크만)
REPORT_LISTS = [("시황", "https://finance.naver.com/research/market_info_list.naver", "market_info_read.naver"),
                ("투자정보", "https://finance.naver.com/research/invest_list.naver", "invest_read.naver")]
# 2026년 개편 후 네이버(Npay 증권) 리서치 JSON API — type 'market'은 화면의 /research/daily
NAVER_RESEARCH_API = "https://stock.naver.com/api/stockSecurity/researches/v2"
# (분류, API type, 화면 경로, 가져올 개수)
REPORT_TYPES = [("기업", "company", "company", 30), ("산업", "industry", "industry", 15),
                ("시황", "market", "daily", 15), ("투자정보", "invest", "invest", 10)]
NAVER_HEADERS = {**UA, "Accept": "application/json, text/plain, */*", "Referer": "https://stock.naver.com/"}


def csv_rows(text: str) -> list[dict]:
    lines = text.lstrip("﻿").strip().splitlines()
    head = lines[0].split(",")
    return [dict(zip(head, ln.split(","))) for ln in lines[1:]]


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def kr_indices(today) -> list[dict]:
    out = []
    for sym, name in KR_INDICES:
        rows = []
        for y in sorted({today.year - 1, today.year}):
            r = requests.get(f"{CACHE}/index/year_{sym}/{y}.csv", headers=UA, timeout=30)
            if r.ok:
                rows += csv_rows(r.text)
        rows = [x for x in rows if x.get("Close")]
        if len(rows) < 2:
            continue
        last, prev = rows[-1], rows[-2]
        close = num(last["Close"])
        out.append({
            "key": sym, "name": name, "date": last["Date"], "close": close,
            "chg": round(close - num(prev["Close"]), 2),
            "pct": round((close / num(prev["Close"]) - 1) * 100, 2),
            "amount": num(last.get("Amount")),  # 거래대금(원)
            "spark": [num(x["Close"]) for x in rows[-30:]],
        })
    return out


def kr_listing(today):
    """가장 최근 거래일의 전 종목 시세 (최대 10일 전까지 거슬러 찾음)"""
    for i in range(0, 11):
        d = (today - timedelta(days=i)).isoformat()
        r = requests.get(f"{CACHE}/listing/krx/{d}.csv", headers=UA, timeout=60)
        if r.ok and len(r.text) > 1000:
            rows = csv_rows(r.text)
            # 장 시작 전 파일은 거래대금이 모두 0 → 실제 거래가 있는 날만 사용
            if sum(num(x.get("Amount")) for x in rows[:200]) > 0:
                return d, rows
    return None, []


def market_stats(rows: list[dict]) -> dict:
    stocks = []
    for x in rows:
        if x.get("Market") not in ("KOSPI", "KOSDAQ", "KOSDAQ GLOBAL"):
            continue
        name = x.get("Name", "")
        if "스팩" in name or "SPAC" in (x.get("Dept") or ""):
            continue
        stocks.append({
            "code": x.get("Code", "").zfill(6), "name": name,
            "mkt": "코스피" if x["Market"] == "KOSPI" else "코스닥",
            "close": num(x.get("Close")), "pct": num(x.get("ChagesRatio") or x.get("ChangesRatio")),
            "cc": x.get("ChangeCode", ""), "amount": num(x.get("Amount")), "marcap": num(x.get("Marcap")),
        })
    breadth = {}
    for m in ("코스피", "코스닥"):
        s = [x for x in stocks if x["mkt"] == m]
        breadth[m] = {
            "up": sum(1 for x in s if x["cc"] == "1"),
            "down": sum(1 for x in s if x["cc"] == "2"),
            "flat": sum(1 for x in s if x["cc"] == "3"),
            "limit_up": sum(1 for x in s if x["pct"] >= 29.5),
            "limit_down": sum(1 for x in s if x["pct"] <= -29.5),
        }
    pick = lambda x: {k: x[k] for k in ("code", "name", "mkt", "close", "pct", "amount", "marcap")}
    big = [x for x in stocks if x["marcap"] >= 100_000_000_000]  # 시총 1,000억 이상만 등락 순위에 사용
    return {
        "breadth": breadth,
        "top_amount": [pick(x) for x in sorted(stocks, key=lambda x: -x["amount"])[:10]],
        "gainers": [pick(x) for x in sorted(big, key=lambda x: -x["pct"])[:7]],
        "losers": [pick(x) for x in sorted(big, key=lambda x: x["pct"])[:7]],
        "marcap_top": [pick(x) for x in sorted(stocks, key=lambda x: -x["marcap"])[:10]],
    }


def yahoo(sym: str) -> dict | None:
    for host in ("query1", "query2"):
        try:
            r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(sym)}",
                             params={"range": "2mo", "interval": "1d"}, headers=UA, timeout=20)
            if not r.ok:
                continue
            res = r.json()["chart"]["result"][0]
            closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
            if len(closes) < 2:
                continue
            last, prev = closes[-1], closes[-2]
            t = res["meta"].get("regularMarketTime") or res["timestamp"][-1]
            return {"close": round(last, 4), "chg": round(last - prev, 4),
                    "pct": round((last / prev - 1) * 100, 2),
                    "date": datetime.fromtimestamp(t, KST).strftime("%m.%d %H:%M"),
                    "spark": [round(c, 4) for c in closes[-30:]]}
        except Exception as e:
            print(f"Yahoo 실패 {sym}: {e}")
    return None


def global_quotes() -> list[dict]:
    out = []
    for sym, name, kind in GLOBAL:
        q = yahoo(sym)
        if q:
            out.append({"key": sym, "name": name, "kind": kind, **q})
    return out


def ny_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=-4)))


def us_market() -> dict:
    def grab(lst, keep_spark=False):
        out = []
        for sym, name in lst:
            q = yahoo(sym)
            if q:
                if not keep_spark:
                    q.pop("spark", None)
                out.append({"key": sym, "name": name, **q})
        return out
    us = {"indices": grab(US_INDICES, True), "sectors": grab(US_SECTORS), "stocks": grab(US_STOCKS)}
    us["sectors"].sort(key=lambda x: -x["pct"])
    # 기준일: S&P 500 마지막 거래 시각(뉴욕)
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC",
                         params={"range": "5d", "interval": "1d"}, headers=UA, timeout=20)
        t = r.json()["chart"]["result"][0]["meta"]["regularMarketTime"]
        from zoneinfo import ZoneInfo
        us["date"] = datetime.fromtimestamp(t, ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        us["date"] = ny_now().strftime("%Y-%m-%d")
    nyn = ny_now()
    us["closed"] = nyn.strftime("%Y-%m-%d") > us["date"] or nyn.strftime("%H:%M") >= "16:15" or nyn.weekday() >= 5
    return us


REPORT_PROMPT = """다음은 {broker}의 리포트 「{title}」 본문 일부입니다.
아래 JSON 하나로만 답하세요.

{{"summary": ["핵심 내용 3~4줄. 각 60자 이내, 원문 문장을 그대로 옮기지 말고 자기 말로"],
  "valuation": ["밸류에이션 1~2줄. 각 70자 이내"],
  "method": "목표주가에 쓴 주된 방식 하나: PER, PBR, EV/EBITDA, SOTP, DCF, rNPV, PSR, RIM, NAV, DDM, PEG, EV/Sales 중 하나. 없으면 빈 문자열",
  "why": "이 종목에 이 방식을 쓴 이유 1줄(80자 이내). 리포트에 이유가 있으면 그 내용을, 없으면 업종 특성상 일반적인 이유를 쓰고 끝에 '(일반적 설명)'을 붙이세요. method가 없으면 빈 문자열"}}

- 리포트의 의견은 '{broker}는 ~로 봄'처럼 리포트의 의견임을 밝혀 주세요.
- valuation에는 목표주가 산정 방식과 근거 수치(적용 PER·PBR·EV/EBITDA 배수, 기준 EPS·BPS와 연도, 현재 주가 기준 PER·PBR, 상승여력, 동종업체 대비 수준 등)를 원문에 있는 숫자만으로 쓰세요.
- 업종·시황 리포트처럼 밸류에이션 언급이 없으면 valuation은 빈 배열로 두세요. 숫자를 지어내지 마세요.

원문:
{body}"""


def pdf_text(url: str, limit: int = 11000) -> str:
    from pypdf import PdfReader
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    reader = PdfReader(BytesIO(r.content))
    text = ""
    for page in reader.pages[:6]:
        text += (page.extract_text() or "") + "\n"
        if len(text) > limit:
            break
    return re.sub(r"[ \t]+", " ", text).strip()[:limit]


def summarize_reports(reports: list, old_reports: list, budget: int) -> int:
    """새 리포트만 3~4줄 요약 (이미 요약한 건 이전 결과 재사용)"""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    prev = {r["url"]: r for r in old_reports}
    done = 0
    for rep_ in reports:  # 이전 결과(요약·투자의견·PDF) 이어받기
        p = prev.get(rep_["url"]) or {}
        for k in ("sum", "opinion", "goal", "pdf"):
            if p.get(k) and not rep_.get(k):
                rep_[k] = p[k]
        for k in ("val", "method", "why"):
            if k in p:
                rep_[k] = p[k]
    order = {"기업": 1, "산업": 2, "시황": 3, "투자정보": 4}
    for rep_ in sorted(reports, key=lambda x: (not x.get("watch"), order.get(x["cat"], 9))):
        # 요약이 있고, 기업 리포트는 밸류에이션까지 있으면 건너뜀 (예전에 요약만 만든 기업 리포트는 한 번 더)
        if rep_.get("sum") and (rep_["cat"] != "기업" or "method" in rep_):
            continue
        if not key or done >= budget:
            continue
        if (not rep_.get("pdf") or (rep_["cat"] == "기업" and "opinion" not in rep_)) and rep_.get("api"):
            try:
                d = requests.get(rep_["api"], headers=NAVER_HEADERS, timeout=20)
                if d.ok:
                    dj = d.json()
                    rep_["pdf"] = rep_.get("pdf") or _find_pdf(dj)
                    if rep_["cat"] == "기업":
                        for o in _walk(dj):
                            if "opinion" in o and isinstance(o.get("opinion"), str):
                                rep_["opinion"] = o["opinion"][:12]
                            if o.get("goalPrice") not in (None, "", "0", 0):
                                rep_["goal"] = str(o["goalPrice"])[:12]
                        rep_.setdefault("opinion", "")
            except Exception as e:
                print(f"리포트 상세 실패 {rep_['title'][:20]}: {e}")
        if not rep_.get("pdf"):
            continue
        try:
            body = pdf_text(rep_["pdf"])
            if len(body) < 200:
                continue
            r = requests.post("https://api.anthropic.com/v1/messages", headers={
                "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": os.environ.get("SUMMARY_MODEL", "claude-haiku-5-5"), "max_tokens": 1500,
                      "messages": [{"role": "user", "content": REPORT_PROMPT.format(
                          broker=rep_["broker"] or "증권사", title=rep_["title"], body=body)}]}, timeout=90)
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
            text = re.sub(r"```(?:json)?", "", text)
            lines, val = [], []
            try:
                j = json.loads(text[text.find("{"):text.rfind("}") + 1])
                lines = [str(x)[:90] for x in j.get("summary", [])][:4]
                val = [str(x)[:110] for x in j.get("valuation", [])][:2]
                rep_["method"] = str(j.get("method", "") or "").strip()[:12]
                rep_["why"] = str(j.get("why", "") or "").strip()[:120]
            except Exception:
                s_, e_ = text.find("["), text.rfind("]")
                lines = [str(x)[:90] for x in json.loads(text[s_:e_ + 1])][:4]
            if lines:
                if not rep_.get("sum"):
                    rep_["sum"] = lines
                rep_["val"] = val      # 빈 배열 = 밸류에이션 언급 없음
                done += 1
        except Exception as e:
            print(f"리포트 요약 실패 {rep_['title'][:20]}: {e}")
    return done


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).replace("&amp;", "&").strip()


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _next_payload_objects(html: str):
    """Next.js 페이지에 들어 있는 데이터(JSON)를 꺼냄: __NEXT_DATA__ 또는 self.__next_f.push 조각"""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if m:
        try:
            yield from _walk(json.loads(m.group(1)))
        except Exception:
            pass
    chunks = []
    for raw in re.findall(r'self\.__next_f\.push\(\[\d+,\s*("(?:[^"\\]|\\.)*")\]\)', html, re.S):
        try:
            chunks.append(json.loads(raw))
        except Exception:
            pass
    text = "".join(chunks)
    # RSC 스트림은 "id:JSON" 줄들로 이뤄짐 → 줄마다 JSON 부분을 파싱
    for line in text.split("\n"):
        i = line.find(":")
        body = line[i + 1:] if i >= 0 else line
        if not body or body[0] not in "[{":
            continue
        try:
            yield from _walk(json.loads(body))
        except Exception:
            continue


def _pick(d: dict, keys) -> str:
    for k, v in d.items():
        kl = k.lower()
        if any(x in kl for x in keys) and isinstance(v, (str, int)) and str(v).strip():
            return str(v).strip()
    return ""


def _norm_date(v: str) -> str:
    v = str(v)
    m = re.search(r"(20\d{2})[.\-/]?(\d{2})[.\-/]?(\d{2})", v)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"\b(\d{2})\.(\d{2})\.(\d{2})\b", v)
    return f"20{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def _flat(d: dict) -> dict:
    """한 단계 안쪽 객체의 값까지 펼침 (예: broker: {name: ...})"""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                if not isinstance(v2, (dict, list)):
                    out[f"{k}_{k2}"] = v2
        elif not isinstance(v, list):
            out[k] = v
    return out


def _find_pdf(o) -> str:
    for d in _walk(o):
        for v in d.values():
            if isinstance(v, str) and ".pdf" in v.lower() and v.startswith("http"):
                return v
    return ""


def _report_item(it: dict, cat: str, typ: str, page: str) -> dict | None:
    f = _flat(it)
    nid = _pick(f, ("nid", "researchid")) or str(it.get("id", ""))
    title = _pick(f, ("title", "subject"))
    if not nid or not title:
        return None
    rep_ = {"cat": cat, "title": title[:80],
            "broker": _pick(f, ("brokername", "broker", "firm", "source"))[:20],
            "date": _norm_date(_pick(f, ("writedate", "date", "dt", "regist", "created"))),
            "url": f"https://stock.naver.com/research/{page}/{nid}",
            "pdf": _find_pdf(it), "api": f"{NAVER_RESEARCH_API}/{typ}/{nid}"}
    if typ == "company":
        rep_["stock"] = _pick(f, ("itemname", "stockname", "corpname"))[:20]
        rep_["code"] = _pick(f, ("itemcode", "stockcode"))[:6]
    if typ == "industry":
        rep_["sector"] = _pick(f, ("industryname", "industry", "category"))[:20]
    return rep_


def report_list_api(watch_codes: list[str] | None = None) -> list[dict]:
    out, seen = [], set()

    def add(x, watch=False):
        if x and x["url"] not in seen:
            seen.add(x["url"])
            if watch:
                x["watch"] = True
            out.append(x)

    for cat, typ, page, size in REPORT_TYPES:
        try:
            r = requests.get(f"{NAVER_RESEARCH_API}/{typ}", params={"index": 0, "size": size},
                             headers=NAVER_HEADERS, timeout=20)
            if not r.ok:
                print(f"리포트 API {cat}: HTTP {r.status_code} {r.text[:150]}")
                continue
            j = r.json()
            items = j.get("items") if isinstance(j, dict) else j
            for it in items or []:
                add(_report_item(it, cat, typ, page))
            if not items:
                print(f"리포트 API {cat}: 응답에 항목이 없어요 → {str(j)[:200]}")
        except Exception as e:
            print(f"리포트 API 실패 {cat}: {e}")
    # 관심종목 기업 리포트는 따로 최근 5건씩
    for code in watch_codes or []:
        try:
            r = requests.get(f"{NAVER_RESEARCH_API}/company", params={"itemCodes": code, "index": 0, "size": 5},
                             headers=NAVER_HEADERS, timeout=20)
            if r.ok:
                j = r.json()
                for it in (j.get("items") if isinstance(j, dict) else j) or []:
                    x = _report_item(it, "기업", "company", "company")
                    if x and not x.get("code"):
                        x["code"] = code
                    add(x, watch=True)
            time.sleep(0.2)
        except Exception as e:
            print(f"관심종목 리포트 실패 {code}: {e}")
    for x in out:  # 목록에서 이미 받은 관심종목 리포트에도 표시
        if x.get("code") in (watch_codes or []):
            x["watch"] = True
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:90]


def report_list() -> list[dict]:
    """증권사 리포트 목록: 네이버 리서치 JSON API 우선, 실패하면 예전 페이지 방식"""
    watch_codes = [w.get("code") for w in json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8")).get("items", [])
                   if w.get("code")] if (ROOT / "watchlist.json").exists() else []
    api = report_list_api(watch_codes)
    if api:
        return api
    out = []
    for cat, url, read_path in REPORT_LISTS:
        try:
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            raw = r.content
            enc = "utf-8" if re.search(rb'charset=["\']?utf-8', raw[:3000], re.I) else "euc-kr"
            html = raw.decode(enc, errors="ignore")
            found = []
            # 1) 새 페이지: 데이터 속에서 PDF 링크나 nid를 가진 항목을 찾음
            for d in _next_payload_objects(html):
                vals = " ".join(str(v) for v in d.values() if isinstance(v, (str, int)))
                pdf = next((str(v) for v in d.values() if isinstance(v, str) and ".pdf" in v.lower()), "")
                nid = _pick(d, ("nid", "researchid", "reportid"))
                title = _pick(d, ("title", "subject"))
                if not title or not (pdf or nid):
                    continue
                broker = _pick(d, ("broker", "firm", "company", "brk", "source", "provider", "office"))
                date = _norm_date(_pick(d, ("date", "dt", "time", "created", "regist", "write")) or vals)
                link = _pick(d, ("url", "link", "href"))
                if not link or ".pdf" in link.lower():
                    link = f"https://finance.naver.com/research/{read_path}?nid={nid}" if nid else (pdf or url)
                found.append({"cat": cat, "title": title[:80], "broker": broker[:20], "date": date,
                              "url": link, "pdf": pdf})
            # 2) 예전 표 형식
            if not found:
                for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
                    m = re.search(r'href=["\']([^"\']*%s\?nid=(\d+)[^"\']*)["\'][^>]*>(.*?)</a>' % re.escape(read_path), row, re.S)
                    if not m:
                        continue
                    tds = [_strip(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
                    date = next((t for t in tds if re.fullmatch(r"\d{2}\.\d{2}\.\d{2}", t)), "")
                    title = _strip(m.group(3))
                    broker = next((t for t in tds[1:] if t and t != title and not re.fullmatch(r"[\d.,]+", t)), "")
                    pdf = re.search(r'href="([^"]+\.pdf)"', row, re.I)
                    link = m.group(1).replace("&amp;", "&")
                    if not link.startswith("http"):
                        link = "https://finance.naver.com/research/" + link.lstrip("/").replace("research/", "")
                    found.append({"cat": cat, "title": title[:80], "broker": broker[:20],
                                  "date": ("20" + date.replace(".", "-")) if date else "",
                                  "url": link, "pdf": pdf.group(1) if pdf else ""})
            # 중복 제거
            seen = set()
            for f in found:
                k = f["pdf"] or f["url"] or f["title"]
                if k not in seen:
                    seen.add(k)
                    out.append(f)
            if not found:
                dbg = DATA_DIR / "debug"
                dbg.mkdir(parents=True, exist_ok=True)
                (dbg / f"naver_{read_path.split('_')[0]}.html").write_text(html, encoding="utf-8")
                print(f"리포트 목록 진단 {cat}: 항목을 못 찾아 페이지를 data/debug/에 저장했어요 (길이 {len(html)})")
        except Exception as e:
            print(f"리포트 목록 실패 {cat}: {e}")
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:30]


COMMENT_PROMPT = """아래는 {date} 한국 증시 마감 데이터와 해외 시장 데이터, 그날 주요 공시 요약입니다. 개인 투자자용 '오늘의 국내 시황'을 써 주세요.
[검색] 먼저 웹 검색으로 그날 국내 증시의 특이사항(지수를 움직인 이슈, 외국인·기관 수급 동향, 특징 업종·종목과 그 이유, 주요 실적 발표)을 확인하세요.
- 수치는 아래 데이터를 우선 쓰고, 이슈는 검색이나 아래 공시로 확인된 사실만 쓰세요. 확인되지 않은 원인은 쓰지 마세요.
- 기사 문장을 그대로 옮기지 말고 짧게 자기 말로 쓰세요.
- 전망, 추천, 매수·매도 의견은 쓰지 마세요.
- 마지막 답은 JSON 하나로만 하세요.

{{"headline": "한 줄 제목(30자 이내)", "summary": "전체 흐름 2~3문장", "issues": ["특이사항 3~5개. 예: 'SK하이닉스, 실적 발표 후 11% 급등'. 각 60자 이내"], "points": ["지수·등락·거래대금에서 눈에 띄는 수치 2~4개, 각 50자 이내"]}}

데이터:
{data}"""


US_PROMPT = """아래는 뉴욕증시 {date}(현지) 마감 데이터입니다. 한국 투자자가 아침에 읽을 '간밤 뉴욕증시' 요약을 써 주세요.
[검색] 먼저 웹 검색으로 그날 뉴욕증시의 특이사항(주요 기업 실적 발표와 주가 반응, 경제지표, 연준 발언, 크게 움직인 종목의 이유)을 확인하세요.
- 수치는 아래 데이터를 우선 쓰고, 이슈는 검색으로 확인된 사실만 쓰세요. 확인되지 않은 원인은 쓰지 마세요.
- 기사 문장을 그대로 옮기지 말고 짧게 자기 말로 쓰세요.
- 전망, 추천, 매수·매도 의견은 쓰지 마세요.
- 마지막 답은 JSON 하나로만 하세요.

{{"headline": "한 줄 제목(30자 이내)", "summary": "전체 흐름 2~3문장", "issues": ["특이사항 3~5개. 예: '알파벳, 3분기 실적 발표 후 시간외 -5%'. 각 60자 이내"], "points": ["지수·업종·금리·환율에서 눈에 띄는 수치 2~4개, 각 50자 이내"]}}

데이터:
{data}"""


def call_claude(prompt: str, search: bool = False) -> dict | None:
    """Claude 호출. search=True면 웹 검색 도구로 그날 이슈를 찾고, 인용한 기사 링크를 sources로 돌려줌"""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    model = os.environ.get("SUMMARY_MODEL", "claude-haiku-5-5")
    body = {"model": model, "max_tokens": 4000, "messages": [{"role": "user", "content": prompt}]}
    if search:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    blocks = []
    for _ in range(4):  # 검색이 길어지면 pause_turn으로 끊겨 오므로 이어서 요청
        r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=180)
        if r.status_code == 400 and search and "web search" in r.text.lower():
            print("웹 검색이 꺼져 있어 검색 없이 작성합니다:", r.text[:200])
            return call_claude(re.sub(r"\[검색\][^\n]*\n", "", prompt), search=False)
        r.raise_for_status()
        rj = r.json()
        blocks += rj.get("content", [])
        if rj.get("stop_reason") != "pause_turn":
            break
        body["messages"] = body["messages"][:1] + [{"role": "assistant", "content": rj["content"]}]
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    sources, seen = [], set()
    for b in blocks:
        for c in b.get("citations") or []:
            u = c.get("url")
            if u and u not in seen:
                seen.add(u)
                sources.append({"title": (c.get("title") or u)[:80], "url": u})
    text = re.sub(r"```(?:json)?", "", text)
    s_, e_ = text.find("{"), text.rfind("}")
    j = json.loads(text[s_:e_ + 1])
    return {"headline": str(j.get("headline", "")), "summary": str(j.get("summary", "")),
            "issues": [str(p) for p in j.get("issues", [])][:5],
            "points": [str(p) for p in j.get("points", [])][:5], "sources": sources[:5]}


def us_comment(us: dict, macro: list) -> dict | None:
    f = lambda x: f'{x["name"]} {x["close"]} ({x["pct"]:+.2f}%)'
    brief = {"지수": [f(x) for x in us.get("indices", [])],
             "업종ETF(등락순)": [f'{x["name"]} {x["pct"]:+.2f}%' for x in us.get("sectors", [])],
             "주요종목": [f(x) for x in us.get("stocks", [])],
             "금리·환율·원자재": [f(x) for x in macro]}
    c = call_claude(US_PROMPT.format(date=us["date"], data=json.dumps(brief, ensure_ascii=False, indent=1)), search=True)
    return {"date": us["date"], **c} if c else None


def ai_comment(data: dict, date: str) -> dict | None:
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return None
    brief = {
        "국내지수": [{k: i[k] for k in ("name", "close", "chg", "pct")} | {"거래대금(조원)": round(i["amount"] / 1e12, 1)}
                  for i in data.get("indices", [])],
        "등락종목수": data.get("breadth"),
        "거래대금상위": [f'{x["name"]} {x["pct"]:+.2f}%' for x in data.get("top_amount", [])[:8]],
        "상승률상위(시총1천억↑)": [f'{x["name"]} {x["pct"]:+.2f}%' for x in data.get("gainers", [])[:5]],
        "하락률상위(시총1천억↑)": [f'{x["name"]} {x["pct"]:+.2f}%' for x in data.get("losers", [])[:5]],
        "해외": [f'{g["name"]} {g["close"]} ({g["pct"]:+.2f}%, {g["date"]})' for g in data.get("global", [])],
    }
    day = DAYS_DIR / f"{date}.json"
    if day.exists():
        items = json.loads(day.read_text(encoding="utf-8")).get("items", [])
        top = [x for x in items if x.get("imp") == "high" and x.get("ai")][:8]
        brief["주요공시"] = [f'{x["corp"]}: {x["ai"]}' for x in top]
    prompt = COMMENT_PROMPT.format(date=date, data=json.dumps(brief, ensure_ascii=False, indent=1))
    c = call_claude(prompt, search=True)
    return {"date": date, **c} if c else None


def main():
    now = datetime.now(KST)
    old = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    data = {"updated_at": now.strftime("%Y-%m-%d %H:%M")}
    errors = []

    try:
        data["indices"] = kr_indices(now.date())
    except Exception as e:
        errors.append(f"국내 지수: {e}")
    try:
        d, rows = kr_listing(now.date())
        if rows:
            data["date"] = d
            data.update(market_stats(rows))
    except Exception as e:
        errors.append(f"종목 시세: {e}")
    last_y = old.get("yahoo_at", "")
    yahoo_due = (not last_y or not old.get("us") or
                 (now - datetime.strptime(last_y, "%Y-%m-%d %H:%M").replace(tzinfo=KST)) >= timedelta(minutes=YAHOO_EVERY_MIN))
    if yahoo_due or os.environ.get("FORCE_MARKET") == "1":
        try:
            data["global"] = global_quotes()
        except Exception as e:
            errors.append(f"환율·금리·원자재: {e}")
        try:
            us = us_market()
            if us.get("indices"):
                data["us"] = us
        except Exception as e:
            errors.append(f"뉴욕증시: {e}")
        if data.get("global") or data.get("us"):
            data["yahoo_at"] = data["updated_at"]
    try:
        reps = report_list()
        if reps:
            n = summarize_reports(reps, old.get("reports") or [], int(os.environ.get("REPORT_SUMMARIES", "20")))
            print(f"리포트 요약 {n}건")
            data["reports"] = reps
        else:
            errors.append("리포트 목록: 0건 (페이지 구조가 바뀌었을 수 있어요)")
    except Exception as e:
        errors.append(f"리포트 목록: {e}")

    # 실패했거나 이번에 갱신하지 않은 항목은 이전 값을 유지
    for k in ("indices", "date", "breadth", "top_amount", "gainers", "losers", "marcap_top",
              "global", "us", "yahoo_at", "reports", "us_comment"):
        if not data.get(k) and old.get(k):
            data[k] = old[k]

    us = data.get("us")
    if us and us.get("closed") and (not data.get("us_comment") or data["us_comment"].get("date") != us["date"]):
        try:
            c = us_comment(us, data.get("global", []))
            if c:
                data["us_comment"] = c
        except Exception as e:
            errors.append(f"AI 뉴욕 시황: {e}")

    date = data.get("date") or (data.get("indices") or [{}])[0].get("date")
    comment = old.get("comment")
    if date and (not comment or comment.get("date") != date):
        try:
            c = ai_comment(data, date)
            if c:
                comment = c
        except Exception as e:
            errors.append(f"AI 시황: {e}")
    if comment:
        data["comment"] = comment
    data["errors"] = errors

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"시황 저장: 기준일 {date}, 국내지수 {len(data.get('indices', []))}, 뉴욕 {len((data.get('us') or {}).get('indices', []))}, "
          f"환율등 {len(data.get('global', []))}, 리포트 {len(data.get('reports', []))}, 오류 {errors}")


if __name__ == "__main__":
    main()
