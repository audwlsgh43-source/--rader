"""
공시레이더 - 매일 시황 수집 스크립트 → data/market.json

데이터 출처
  - 코스피·코스닥·코스피200 지수, 전 종목 시세: FinanceDataReader의 KRX 일별 캐시
    (github.com/FinanceData/fdr_krx_data_cache, 장 마감 후 갱신)
  - 뉴욕증시(지수·업종 ETF·주요 종목), 환율·금리·원자재: Yahoo Finance (50분에 한 번)
  - 증권사 리포트 목록: 네이버 금융 리서치 (제목·증권사·날짜·링크만, 본문은 가져오지 않음)
  - AI 시황 코멘트(선택): ANTHROPIC_API_KEY가 있으면 국내·뉴욕 각각 거래일마다 한 번 생성

각 항목은 실패해도 다른 항목과 공시 수집에 영향을 주지 않습니다.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import requests

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "market.json"
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


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).replace("&amp;", "&").strip()


def report_list() -> list[dict]:
    """네이버 금융 리서치 목록에서 증권사·제목·날짜·링크만 수집 (본문은 가져오지 않음)"""
    out = []
    for cat, url, read_path in REPORT_LISTS:
        try:
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            html = r.content.decode("euc-kr", errors="ignore")
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
                m = re.search(r'href="([^"]*%s\?nid=(\d+)[^"]*)"[^>]*>(.*?)</a>' % re.escape(read_path), row, re.S)
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
                out.append({"cat": cat, "title": title[:80], "broker": broker[:20],
                            "date": ("20" + date.replace(".", "-")) if date else "",
                            "url": link, "pdf": pdf.group(1) if pdf else ""})
        except Exception as e:
            print(f"리포트 목록 실패 {cat}: {e}")
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:30]


COMMENT_PROMPT = """아래는 {date} 한국 증시 마감 데이터와 해외 시장 데이터, 그날 주요 공시 요약입니다.
이 숫자와 사실만 사용해서 개인 투자자용 '오늘의 시황'을 써 주세요.
- 전망, 추천, 매수·매도 의견, 원인에 대한 추측은 쓰지 마세요. 데이터에 없는 뉴스나 사건도 쓰지 마세요.
- 결과는 JSON 하나로만 답하세요.

{{"headline": "한 줄 제목(30자 이내)", "summary": "전체 흐름 2~3문장", "points": ["눈에 띄는 사실 3~5개, 각 50자 이내"]}}

데이터:
{data}"""


US_PROMPT = """아래는 뉴욕증시 {date} 마감 데이터입니다. 이 숫자만 사용해서 한국 투자자가 아침에 읽을 '간밤 뉴욕증시' 요약을 써 주세요.
- 전망, 추천, 매수·매도 의견, 원인에 대한 추측은 쓰지 마세요. 데이터에 없는 뉴스나 사건도 쓰지 마세요.
- 결과는 JSON 하나로만 답하세요.

{{"headline": "한 줄 제목(30자 이내)", "summary": "전체 흐름 2~3문장", "points": ["지수·업종·종목·금리·환율에서 눈에 띄는 사실 3~5개, 각 50자 이내"]}}

데이터:
{data}"""


def call_claude(prompt: str) -> dict | None:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    model = os.environ.get("SUMMARY_MODEL", "claude-haiku-5-5")
    r = requests.post("https://api.anthropic.com/v1/messages", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json",
    }, json={"model": model, "max_tokens": 3000, "messages": [{"role": "user", "content": prompt}]}, timeout=120)
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    text = re.sub(r"```(?:json)?", "", text)
    s, e = text.find("{"), text.rfind("}")
    j = json.loads(text[s:e + 1])
    return {"headline": str(j.get("headline", "")), "summary": str(j.get("summary", "")),
            "points": [str(p) for p in j.get("points", [])][:5]}


def us_comment(us: dict, macro: list) -> dict | None:
    f = lambda x: f'{x["name"]} {x["close"]} ({x["pct"]:+.2f}%)'
    brief = {"지수": [f(x) for x in us.get("indices", [])],
             "업종ETF(등락순)": [f'{x["name"]} {x["pct"]:+.2f}%' for x in us.get("sectors", [])],
             "주요종목": [f(x) for x in us.get("stocks", [])],
             "금리·환율·원자재": [f(x) for x in macro]}
    c = call_claude(US_PROMPT.format(date=us["date"], data=json.dumps(brief, ensure_ascii=False, indent=1)))
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
    c = call_claude(prompt)
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
