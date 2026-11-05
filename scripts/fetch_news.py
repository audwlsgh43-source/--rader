"""
공시레이더 - 데일리 뉴스 → data/news/latest.json

  - 출처: 네이버 증권(Npay 증권) 뉴스 목록 API
      주요뉴스 · 많이 본 뉴스 · 속보 · 시황·전망 · 기업·종목분석 · 해외증시 · 환율 · 해외뉴스
      + 관심종목별 뉴스 검색
  - 저장하는 것: 제목, 언론사, 시각, 원문 링크, AI가 새로 쓴 한 줄 요약
    (기사 본문은 저장하지 않아요. 목록에 딸린 짧은 설명은 요약 재료로만 쓰고 버려요)
  - AI(선택): 새 기사 한 줄 요약(여러 건을 한 번에), 2시간마다 '오늘의 뉴스 브리핑'
"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = DATA / "news" / "latest.json"
API = "https://stock.naver.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
           "Accept": "application/json, text/plain, */*", "Referer": "https://stock.naver.com/"}
KEEP_HOURS = 48
MAX_ITEMS = 400


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def pick(d: dict, keys, exact_only: bool = False) -> str:
    """keys 순서대로 우선: 먼저 키 이름이 정확히 같은 것, 다음(exact_only가 아니면) 키 이름에 포함된 것"""
    low = {k.lower(): v for k, v in d.items() if isinstance(v, (str, int)) and str(v).strip()}
    for key in keys:
        if key in low:
            return str(low[key]).strip()
    if exact_only:
        return ""
    for key in keys:
        for k, v in low.items():
            if key in k:
                return str(v).strip()
    return ""


def clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
    return re.sub(r"\s+", " ", s).strip()


def norm_time(v: str) -> str:
    v = str(v or "")
    m = re.search(r"(20\d{2})[-./]?(\d{2})[-./]?(\d{2})[ T]?(\d{2})?:?(\d{2})?", v)
    if not m:
        return ""
    y, mo, d, h, mi = m.groups()
    return f"{y}-{mo}-{d} {h or '00'}:{mi or '00'}"


def to_item(d: dict, cat: str) -> dict | None:
    title = clean(pick(d, ("title", "articletitle", "subject", "headline"), exact_only=True))
    if len(title) < 6:
        return None
    oid = pick(d, ("officeid", "oid"))
    aid = pick(d, ("articleid", "aid"))
    url = pick(d, ("linkurl", "mobilelinkurl", "articleurl", "mobileurl", "url", "link"), exact_only=True)
    if re.search(r"\.(jpe?g|png|gif|webp)(\?|$)", url, re.I) or "imgnews" in url:
        url = ""
    if not url.startswith("http"):
        url = f"https://n.news.naver.com/mnews/article/{oid}/{aid}" if oid and aid else ""
    if not url:
        return None
    return {
        "title": title[:120],
        "press": clean(pick(d, ("officename", "officehname", "pressname", "press", "source"), exact_only=True))[:20],
        "time": norm_time(pick(d, ("datetime", "date", "time", "regdate", "created"))),
        "url": url,
        "cat": cat,
        "_snip": clean(pick(d, ("body", "summary", "content", "description", "subcontent")))[:300],
    }


def fetch(path: str, params: dict, cat: str) -> list[dict]:
    try:
        r = requests.get(API + path, params=params, headers=HEADERS, timeout=20)
        if not r.ok:
            print(f"뉴스 {cat}: HTTP {r.status_code} {r.text[:120]}")
            return []
        out = []
        for d in walk(r.json()):
            it = to_item(d, cat)
            if it:
                out.append(it)
        if not out:
            print(f"뉴스 {cat}: 항목 없음 → {r.text[:200]}")
        return out
    except Exception as e:
        print(f"뉴스 {cat} 실패: {e}")
        return []


SUM_PROMPT = """다음은 오늘 국내외 증시 뉴스 목록입니다(번호. 제목 | 짧은 설명).
각 기사를 투자자가 알아야 할 핵심만 담아 한국어 한 줄(45자 이내)로 요약하세요.
- 제목·설명에 있는 사실만 쓰고, 문장을 그대로 베끼지 말고 자기 말로 쓰세요. 추측·전망·투자의견은 쓰지 마세요.
- JSON 객체 하나로만 답하세요: {{"1": "요약", "2": "요약", ...}}

{items}"""

DIGEST_PROMPT = """다음은 오늘 증시 관련 뉴스 제목과 한 줄 요약입니다(번호 포함).
투자자가 아침·점심에 30초 만에 훑을 '오늘의 뉴스 브리핑'을 만드세요.
- 비슷한 기사는 하나의 주제로 묶어 4~6개 주제로 정리하세요. 각 주제는 한 줄(50자 이내), 근거 기사 번호를 1~3개 붙이세요.
- 기사에 있는 사실만 쓰고, 전망·추천·매수매도 의견은 쓰지 마세요.
- JSON 하나로만 답하세요: {{"headline": "오늘 한 줄 요약(30자 이내)", "topics": [{{"text": "주제 요약", "refs": [번호, ...]}}]}}

{items}"""


def claude(prompt: str, max_tokens: int = 3000) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    r = requests.post("https://api.anthropic.com/v1/messages", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": os.environ.get("SUMMARY_MODEL", "claude-haiku-5-5"), "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": prompt}]}, timeout=120)
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    text = re.sub(r"```(?:json)?", "", text)
    return json.loads(text[text.find("{"):text.rfind("}") + 1])


def main():
    now = datetime.now(KST)
    today = now.strftime("%Y%m%d")
    old = load(OUT, {})
    prev = {x["url"]: x for x in old.get("items", [])}

    got = []
    got += fetch("/domestic/news/list", {"category": "MAINNEWS", "page": 1, "pageSize": 20}, "주요")
    got += fetch("/domestic/news/list", {"category": "RANKNEWS", "page": 1, "pageSize": 10}, "많이 본")
    got += fetch("/domestic/news/list", {"category": "FLASHNEWS", "page": 1, "pageSize": 20}, "속보")
    for sid, cat, n in ((401, "시황·전망", 15), (402, "기업·종목", 15), (403, "해외증시", 10), (429, "환율", 5)):
        got += fetch("/domestic/news/focus", {"sid": sid, "page": 1, "pageSize": n, "date": today, "enableFallback": "true"}, cat)
    got += fetch("/foreign/news/worldNews", {"page": 1, "pageSize": 10, "date": today}, "해외뉴스")

    watch = [w for w in load(ROOT / "watchlist.json", {}).get("items", []) if w.get("name")]
    for w in watch[:30]:
        for it in fetch("/domestic/news/search", {"query": w["name"], "page": 1, "pageSize": 5}, "관심종목"):
            it["stock"] = w["name"]
            got.append(it)
        time.sleep(0.2)

    # 합치기: 같은 기사(링크)는 하나로, 분류는 여러 개 유지
    items = {}
    for it in got:
        u = it["url"]
        if u in items:
            if it["cat"] not in items[u]["cats"]:
                items[u]["cats"].append(it["cat"])
            if it.get("stock"):
                items[u].setdefault("stocks", [])
                if it["stock"] not in items[u]["stocks"]:
                    items[u]["stocks"].append(it["stock"])
            continue
        p = prev.get(u, {})
        x = {**{k: v for k, v in it.items() if k not in ("cat", "stock")},
             "cats": list(dict.fromkeys((p.get("cats") or []) + [it["cat"]])),
             "stocks": list(dict.fromkeys((p.get("stocks") or []) + ([it["stock"]] if it.get("stock") else []))),
             "ai": p.get("ai", ""), "first_seen": p.get("first_seen") or now.strftime("%Y-%m-%d %H:%M")}
        if not x["time"]:
            x["time"] = p.get("time") or x["first_seen"]
        items[u] = x
    # 이전에 모은 기사 중 48시간 안의 것은 유지
    cutoff = (now - timedelta(hours=KEEP_HOURS)).strftime("%Y-%m-%d %H:%M")
    for u, p in prev.items():
        if u not in items and (p.get("time") or "") >= cutoff:
            items[u] = p

    # 제목에 관심종목·시총 상위 종목 이름이 있으면 태그
    mk = load(DATA / "market.json", {})
    names = {w["name"]: w.get("code", "") for w in watch}
    for x in (mk.get("marcap_top") or []) + (mk.get("top_amount") or []):
        names.setdefault(x["name"], x.get("code", ""))
    for x in items.values():
        tags = set(x.get("stocks") or [])
        tags.update(n for n in names if len(n) >= 2 and n in x["title"])
        x["stocks"] = sorted(tags)[:4]
    codes = {n: c for n, c in names.items() if c}

    lst = sorted(items.values(), key=lambda x: x.get("time") or "", reverse=True)[:MAX_ITEMS]

    # AI 한 줄 요약: 새 기사만, 25건씩 묶어서
    # 뉴스 AI 요약·브리핑은 기본으로 꺼 둠 (비용 절감). 켜려면 워크플로에 NEWS_AI: "1"
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip() if os.environ.get("NEWS_AI") == "1" else ""
    errors = []
    if key:
        todo = [x for x in lst if not x.get("ai")][:100]
        for i in range(0, len(todo), 25):
            batch = todo[i:i + 25]
            body = "\n".join(f'{n + 1}. {x["title"]} | {x.get("_snip", "")[:200]}' for n, x in enumerate(batch))
            try:
                res = claude(SUM_PROMPT.format(items=body))
                for n, x in enumerate(batch):
                    s = str(res.get(str(n + 1), "")).strip()
                    if s:
                        x["ai"] = s[:80]
            except Exception as e:
                errors.append(f"요약: {e}")
        # 브리핑: 2시간마다 (또는 처음)
        dg = old.get("digest") or {}
        try:
            due = not dg or now - datetime.strptime(dg.get("at", "2000-01-01 00:00"), "%Y-%m-%d %H:%M").replace(tzinfo=KST) >= timedelta(hours=2)
        except Exception:
            due = True
        recent = [x for x in lst if (x.get("time") or "") >= (now - timedelta(hours=18)).strftime("%Y-%m-%d %H:%M")][:60]
        if due and len(recent) >= 8:
            body = "\n".join(f'{n + 1}. {x["title"]} — {x.get("ai", "")}' for n, x in enumerate(recent))
            try:
                res = claude(DIGEST_PROMPT.format(items=body))
                topics = []
                for t in res.get("topics", [])[:6]:
                    refs = [recent[r - 1]["url"] for r in t.get("refs", []) if isinstance(r, int) and 1 <= r <= len(recent)][:3]
                    topics.append({"text": str(t.get("text", ""))[:80], "refs": refs})
                dg = {"at": now.strftime("%Y-%m-%d %H:%M"), "headline": str(res.get("headline", ""))[:40], "topics": topics}
            except Exception as e:
                errors.append(f"브리핑: {e}")
    else:
        dg = None
        for x in lst:
            x["ai"] = ""

    for x in lst:
        x.pop("_snip", None)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"updated_at": now.strftime("%Y-%m-%d %H:%M"), "digest": dg, "codes": codes,
                               "items": lst, "errors": errors}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"뉴스 저장 {len(lst)}건 (이번에 받은 {len(got)}건), 오류 {errors}")


if __name__ == "__main__":
    main()
