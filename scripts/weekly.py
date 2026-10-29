"""
공시레이더 - 주간 리포트 → data/weekly/latest.json (+ data/weekly/YYYY-Www.json 보관)

금요일 18:00(KST) 이후 첫 실행 때, 그 주 월~금의
  - 중요도 높은 공시(AI 요약 포함), 관심종목 공시
  - 국내·뉴욕 시황 흐름(지수 주간 등락)
  - 다음 주 일정
을 AI가 한 장으로 정리합니다. 텔레그램이 설정돼 있으면 함께 보냅니다.
FORCE_WEEKLY=1 이면 요일·시간과 관계없이 이번 주 리포트를 다시 만듭니다.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT_DIR = DATA / "weekly"

PROMPT = """아래는 {start}~{end} 한 주 동안의 한국 증시 데이터, 주요 공시 요약, 다음 주 일정입니다.
개인 투자자가 주말에 읽을 '주간 리포트'를 써 주세요.
- 아래 데이터에 있는 사실만 쓰세요. 원인 추측, 전망, 추천, 매수·매도 의견은 쓰지 마세요.
- 결과는 JSON 하나로만 답하세요.

{{"headline": "이번 주 한 줄 제목(30자 이내)",
  "summary": "한 주 흐름 3~4문장",
  "market": ["지수·수급·업종 흐름에서 눈에 띄는 사실 3~4개, 각 60자 이내"],
  "disclosures": ["이번 주 주목할 공시 4~6개. '회사명: 내용' 형식, 각 60자 이내"],
  "watch": ["관심종목 관련 소식 0~4개, 각 60자 이내 (없으면 빈 배열)"],
  "next_week": ["다음 주 챙겨볼 일정 3~6개. '날짜 회사명 일정' 형식"]}}

데이터:
{data}"""


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def week_bounds(now):
    monday = (now - timedelta(days=now.weekday())).date()
    return monday, monday + timedelta(days=4)


def index_change(sym: str, start, end) -> dict | None:
    """FinanceDataReader KRX 캐시에서 주간 지수 등락"""
    try:
        r = requests.get(f"https://raw.githubusercontent.com/FinanceData/fdr_krx_data_cache/refs/heads/master/data/index/year_{sym}/{end.year}.csv",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        lines = r.text.lstrip("﻿").strip().splitlines()
        head = lines[0].split(",")
        rows = [dict(zip(head, ln.split(","))) for ln in lines[1:]]
        before = [x for x in rows if x["Date"] < start.isoformat()]
        within = [x for x in rows if start.isoformat() <= x["Date"] <= end.isoformat()]
        if not before or not within:
            return None
        a, b = float(before[-1]["Close"]), float(within[-1]["Close"])
        return {"close": b, "pct": round((b / a - 1) * 100, 2), "last_date": within[-1]["Date"]}
    except Exception as e:
        print("주간 지수 실패", sym, e)
        return None


def collect(start, end) -> dict:
    watch = {x.get("name") for x in load(ROOT / "watchlist.json", {}).get("items", [])}
    high, wl = [], []
    d = start
    while d <= end:
        for x in load(DATA / "days" / f"{d.isoformat()}.json", {}).get("items", []):
            if x.get("read_fail"):
                continue
            line = f'{x["date"][5:]} {x["corp"]}: {x.get("ai") or x["title"]}'
            if x["corp"] in watch:
                wl.append(line)
            if x.get("imp") == "high" and x.get("ai"):
                high.append(line)
        d += timedelta(days=1)
    nxt_s, nxt_e = end + timedelta(days=3), end + timedelta(days=7)
    evs = load(DATA / "events.json", {}).get("items", []) + load(DATA / "events_auto.json", {}).get("items", [])
    nxt = sorted((e for e in evs if nxt_s.isoformat() <= e.get("d", "") <= nxt_e.isoformat()),
                 key=lambda e: (e["c"] not in watch, e["d"]))
    mk = load(DATA / "market.json", {})
    idx = {n: index_change(s, start, end) for s, n in (("ks11", "코스피"), ("kq11", "코스닥"))}
    return {
        "지수주간등락": {k: v for k, v in idx.items() if v},
        "최근국내시황": (mk.get("comment") or {}).get("summary", ""),
        "최근뉴욕시황": (mk.get("us_comment") or {}).get("summary", ""),
        "주요공시": high[:40],
        "관심종목공시": wl[:20],
        "다음주일정": [f'{e["d"][5:]} {e["c"]} {e["k"]} {e.get("s", "")}' for e in nxt[:30]],
    }


def call_claude(prompt: str) -> dict:
    r = requests.post("https://api.anthropic.com/v1/messages", headers={
        "x-api-key": os.environ["ANTHROPIC_API_KEY"].strip(), "anthropic-version": "2023-06-01",
        "content-type": "application/json"},
        json={"model": os.environ.get("SUMMARY_MODEL", "claude-haiku-5-5"), "max_tokens": 4000,
              "messages": [{"role": "user", "content": prompt}]}, timeout=180)
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    text = re.sub(r"```(?:json)?", "", text)
    return json.loads(text[text.find("{"):text.rfind("}") + 1])


def telegram(rep: dict, url: str):
    import html
    sys_path = Path(__file__).resolve().parent
    import sys
    sys.path.insert(0, str(sys_path))
    from notify import send
    e = lambda s: html.escape(str(s), quote=False)
    parts = [f'<b>공시레이더 주간 리포트</b>\n{rep["start"][5:].replace("-", ".")}~{rep["end"][5:].replace("-", ".")}',
             f'<b>{e(rep.get("headline", ""))}</b>\n{e(rep.get("summary", ""))}']
    for key, title in (("market", "시장 흐름"), ("disclosures", "주목할 공시"), ("watch", "관심종목"), ("next_week", "다음 주 일정")):
        if rep.get(key):
            parts.append(f"<b>{title}</b>\n" + "\n".join(f"· {e(x)}" for x in rep[key]))
    if url:
        parts.append(f'<a href="{url}#market">공시레이더에서 보기</a>')
    send("\n\n".join(parts))


def main():
    now = datetime.now(KST)
    force = os.environ.get("FORCE_WEEKLY") == "1"
    if not force and not (now.weekday() == 4 and now.strftime("%H:%M") >= "18:00"):
        return
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        print("ANTHROPIC_API_KEY가 없어 주간 리포트를 건너뜁니다.")
        return
    start, end = week_bounds(now)
    week_id = f"{start.isocalendar()[0]}-W{start.isocalendar()[1]:02d}"
    latest = load(OUT_DIR / "latest.json", {})
    if latest.get("week") == week_id and not force:
        return
    data = collect(start, end)
    rep = call_claude(PROMPT.format(start=start, end=end, data=json.dumps(data, ensure_ascii=False, indent=1)))
    rep = {"week": week_id, "start": start.isoformat(), "end": end.isoformat(),
           "created_at": now.strftime("%Y-%m-%d %H:%M"), "indices": data["지수주간등락"],
           **{k: rep.get(k, "" if k in ("headline", "summary") else []) for k in
              ("headline", "summary", "market", "disclosures", "watch", "next_week")}}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for p in (OUT_DIR / "latest.json", OUT_DIR / f"{week_id}.json"):
        p.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"주간 리포트 저장 {week_id}")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    url = f"https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/" if "/" in repo else ""
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        telegram(rep, url)


if __name__ == "__main__":
    main()
