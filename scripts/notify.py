"""
공시레이더 - 텔레그램 알림

1) 관심종목(watchlist.json)에 새 공시가 올라오면 AI 요약과 함께 알림
2) 평일 07:25 이후 첫 실행 때 '아침 브리핑' 한 통
   (간밤 뉴욕증시 · 국내 전일 마감 · 오늘 일정 · 관심종목 소식)

환경변수
  TELEGRAM_BOT_TOKEN  @BotFather에서 받은 봇 토큰
  TELEGRAM_CHAT_ID    알림 받을 채팅 ID (비어 있으면 로그에 찾는 방법을 출력)
보낸 기록은 data/notify_state.json에 남겨 같은 알림을 두 번 보내지 않습니다.
"""
import html
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE = DATA / "notify_state.json"
DOW = "월화수목금토일"
TYPE_LABEL = {"earn": "실적", "capital": "유상증자·CB", "buyback": "자사주", "div": "배당", "stake": "지분변동",
              "contract": "공급계약", "ma": "합병·분할", "etc": "기타"}


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def site_url() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner}.github.io/{name}/" if name.lower() != f"{owner.lower()}.github.io" else f"https://{name}/"
    return ""


def send(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token:
        return False
    if not chat:
        find_chat_id(token)
        return False
    ok = True
    for part in split(text):
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
            "chat_id": chat, "text": part, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=20)
        if not r.ok:
            print("텔레그램 전송 실패:", r.status_code, r.text[:200])
            ok = False
    return ok


def split(text: str, limit: int = 3900) -> list[str]:
    parts, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > limit and cur:
            parts.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + block
    if cur:
        parts.append(cur)
    return parts


def find_chat_id(token: str):
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20).json()
        ids = {}
        for u in r.get("result", []):
            m = u.get("message") or u.get("channel_post") or {}
            c = m.get("chat") or {}
            if c.get("id"):
                ids[c["id"]] = c.get("title") or c.get("first_name") or ""
        if ids:
            print("TELEGRAM_CHAT_ID로 등록할 값:", ", ".join(f"{k} ({v})" for k, v in ids.items()))
        else:
            print("봇에게 텔레그램으로 아무 메시지나 한 번 보낸 뒤 다시 실행하면 채팅 ID가 여기에 표시돼요.")
    except Exception as e:
        print("채팅 ID 확인 실패:", e)


def watchlist() -> set[str]:
    return {x.get("name") for x in load(ROOT / "watchlist.json", {}).get("items", []) if x.get("name")}


def day_items(date: str) -> list[dict]:
    return load(DATA / "days" / f"{date}.json", {}).get("items", [])


def ready(x: dict, now: datetime) -> bool:
    """AI 요약이 끝났거나, 요약 대상이 아니거나, 처음 본 지 20분이 지났으면 보냄"""
    if x.get("ai") or x.get("analyzed") or x.get("read_fail"):
        return True
    if not x.get("seen"):
        return True
    try:
        seen = datetime.strptime(f'{x["date"]} {x["seen"]}', "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        return now - seen >= timedelta(minutes=20)
    except Exception:
        return True


def fmt_item(x: dict, url: str) -> str:
    lines = [f'<b>{esc(x["corp"])}</b> · {TYPE_LABEL.get(x.get("type"), "공시")}',
             esc(x["title"])]
    if x.get("ai"):
        lines.append(f'▸ {esc(x["ai"])}')
    for k, v in (x.get("numbers") or [])[:3]:
        lines.append(f'  · {esc(k)} {esc(v)}')
    for e in (x.get("events") or [])[:2]:
        lines.append(f'  · {e["date"][5:].replace("-", ".")} {esc(e["kind"])} {esc(e["desc"])}')
    lines.append(f'<a href="{esc(x["url"])}">DART 원문</a>' + (f' · <a href="{url}">공시레이더</a>' if url else ""))
    return "\n".join(lines)


def disclosure_alerts(state: dict, now: datetime, url: str):
    today = now.strftime("%Y-%m-%d")
    items = day_items(today)
    sent = set(state.get("sent", []))
    if not state.get("initialized"):
        # 처음 켤 때 오늘 쌓인 공시를 한꺼번에 보내지 않도록 이미 보낸 것으로 표시
        state["sent"] = [x["rcept_no"] for x in items]
        state["initialized"] = True
        print(f"알림 초기화: 기존 공시 {len(items)}건은 건너뜀")
        return
    watch = watchlist()
    if not watch:
        return
    new = [x for x in items if x["corp"] in watch and x["rcept_no"] not in sent and ready(x, now)]
    if not new:
        return
    new.sort(key=lambda x: x["rcept_no"])
    head = f'<b>관심종목 새 공시 {len(new)}건</b>'
    if send(head + "\n\n" + "\n\n".join(fmt_item(x, url) for x in new)):
        sent.update(x["rcept_no"] for x in new)
        print(f"관심종목 알림 {len(new)}건 전송")
    state["sent"] = sorted(sent)[-3000:]


def pct(v) -> str:
    try:
        return f"{float(v):+.2f}%"
    except Exception:
        return "-"


def morning_brief(state: dict, now: datetime, url: str):
    today = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5 or state.get("brief_date") == today:
        return
    if not ("07:25" <= now.strftime("%H:%M") < "11:00"):
        return
    mk = load(DATA / "market.json", {})
    watch = watchlist()
    out = [f'<b>공시레이더 아침 브리핑</b>\n{now.month}월 {now.day}일({DOW[now.weekday()]})']

    us, uc = mk.get("us") or {}, mk.get("us_comment") or {}
    if us.get("indices"):
        idx = " · ".join(f'{esc(i["name"])} {pct(i["pct"])}' for i in us["indices"] if i["key"] != "^VIX")
        sec = [f'<b>간밤 뉴욕증시</b> ({us.get("date", "")[5:].replace("-", ".")})', idx]
        if uc.get("date") == us.get("date"):
            sec.insert(1, f'<i>{esc(uc.get("headline"))}</i>')
            sec += [f"· {esc(i)}" for i in (uc.get("issues") or [])[:4]]
        fx = [g for g in mk.get("global", []) if g["key"] in ("KRW=X", "^TNX", "CL=F")]
        if fx:
            sec.append(" · ".join(f'{esc(g["name"])} {g["close"]:,.2f} ({pct(g["pct"])})' for g in fx))
        out.append("\n".join(sec))

    if mk.get("indices"):
        d = mk.get("date", "")
        out.append(f'<b>국내 전일 마감</b> ({d[5:].replace("-", ".")})\n' +
                   " · ".join(f'{esc(i["name"])} {i["close"]:,.2f} ({pct(i["pct"])})' for i in mk["indices"][:2]))

    evs = load(DATA / "events.json", {}).get("items", []) + load(DATA / "events_auto.json", {}).get("items", [])
    todays = [e for e in evs if e.get("d") == today]
    todays.sort(key=lambda e: (e.get("c") not in watch, e.get("c", "")))
    if todays:
        lines = [f'{"★ " if e.get("c") in watch else ""}{esc(e["c"])} — {esc(e["k"])} {esc(e.get("s", ""))}'.strip()
                 for e in todays[:12]]
        more = f"\n외 {len(todays) - 12}건" if len(todays) > 12 else ""
        out.append("<b>오늘 일정</b>\n" + "\n".join(lines) + more)

    if watch:
        idx = load(DATA / "days" / "index.json", {}).get("days", [])
        prev = next((d["date"] for d in idx if d["date"] < today and d.get("count")), None)
        wd = [x for x in (day_items(prev) if prev else []) + day_items(today)
              if x["corp"] in watch and not x.get("read_fail")]
        soon = sorted((e for e in evs if e.get("c") in watch and today < e.get("d", "") <=
                       (now + timedelta(days=7)).strftime("%Y-%m-%d")), key=lambda e: e["d"])
        lines = [f'{esc(x["corp"])} · {esc(x["title"])}' + (f'\n  ▸ {esc(x["ai"])}' if x.get("ai") else "") for x in wd[:8]]
        lines += [f'{e["d"][5:].replace("-", ".")} {esc(e["c"])} — {esc(e["k"])}' for e in soon[:6]]
        out.append("<b>관심종목</b>\n" + ("\n".join(lines) if lines else "새 공시와 다가오는 일정이 없어요."))

    if url:
        out.append(f'<a href="{url}">공시레이더 열기</a>')
    if send("\n\n".join(out)):
        state["brief_date"] = today
        print("아침 브리핑 전송")


def main():
    if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
        print("TELEGRAM_BOT_TOKEN이 없어 알림을 건너뜁니다.")
        return
    now = datetime.now(KST)
    state = load(STATE, {})
    url = site_url()
    morning_brief(state, now, url)
    disclosure_alerts(state, now, url)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
