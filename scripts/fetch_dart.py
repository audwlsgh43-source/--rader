"""
공시레이더 - DART 공시 수집 스크립트

오늘(한국시간) 접수된 코스피·코스닥 공시를 OpenDART에서 받아
유형·중요도를 붙여 data/latest.json 으로 저장합니다.
ANTHROPIC_API_KEY가 있으면 주요 공시 원문을 AI가 읽고
  - 한 줄 요약, 핵심 포인트, 핵심 수치  → latest.json
  - 앞으로 예정된 일정(주총일, 배당기준일, 신주상장일 등) → data/events_auto.json
을 만듭니다.

환경변수
  DART_API_KEY       (필수) OpenDART 인증키 40자리
  ANTHROPIC_API_KEY  (선택) AI 요약·일정 추출
  SUMMARY_MODEL      (선택) 기본 claude-haiku-5-5
  MAX_SUMMARIES      (선택) 1회 실행당 새로 분석할 최대 건수, 기본 25
"""
import io
import json
import os
import re
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "latest.json"
EVENTS_OUT = ROOT / "data" / "events_auto.json"
ARCHIVE_DIR = ROOT / "data" / "archive"

DART_LIST = "https://opendart.fss.or.kr/api/list.json"
DART_DOC = "https://opendart.fss.or.kr/api/document.xml"
MARKETS = {"Y": "코스피", "K": "코스닥"}

# 공시 제목 키워드 → 유형 (위에서부터 먼저 맞는 것 적용)
TYPE_RULES = [
    ("capital", ["유상증자", "전환사채", "신주인수권부사채", "교환사채", "무상증자", "감자"]),
    ("buyback", ["자기주식", "주식소각"]),
    ("div", ["배당"]),
    ("contract", ["공급계약", "판매ㆍ공급"]),
    ("ma", ["합병", "분할", "영업양수", "영업양도", "주식교환", "주식이전", "타법인주식"]),
    ("earn", ["영업(잠정)실적", "잠정실적", "매출액또는손익구조", "결산실적", "분기보고서", "반기보고서", "사업보고서"]),
    ("stake", ["대량보유", "임원ㆍ주요주주", "최대주주", "주식등의"]),
]
HIGH = ["유상증자", "전환사채", "신주인수권부사채", "교환사채", "감자", "합병", "분할", "공급계약",
        "영업(잠정)실적", "잠정실적", "최대주주변경", "거래정지", "상장폐지", "관리종목", "불성실공시",
        "회생절차", "부도", "횡령", "배임", "임상"]
MID = ["자기주식", "배당", "대량보유", "매출액또는손익구조", "무상증자", "타법인주식", "소송"]
# 중요도와 별개로, 일정이 들어 있을 가능성이 높아 AI 분석 대상에 넣는 공시
EVENT_HINTS = ["주주총회소집", "기업설명회", "IR", "배당", "상장", "청약", "권리락", "명의개서정지", "기준일"]
# AI 분석에서 제외 (원문이 길고 일정·요약 가치가 낮음)
SKIP_AI = ["분기보고서", "반기보고서", "사업보고서", "임원ㆍ주요주주", "감사보고서", "증권신고서", "투자설명서"]

EVENT_KINDS = {
    "실적발표": "", "IR": "", "주주총회": "", "배당기준일": "up", "배당지급": "up",
    "청약": "", "신주상장": "down", "권리락": "", "전환청구": "down", "합병기일": "",
    "분할기일": "", "보호예수해제": "down", "매매거래정지": "down", "기타": "",
}


def classify(title: str) -> tuple[str, str]:
    t = title.replace(" ", "")
    typ = "etc"
    if not t.startswith("["):  # [기재정정] 등 정정공시는 '기타·정정'
        for name, kws in TYPE_RULES:
            if any(k in t for k in kws):
                typ = name
                break
    if any(k in t for k in HIGH) and typ != "etc":
        imp = "high"
    elif any(k in t for k in HIGH + MID):
        imp = "mid"
    else:
        imp = "low"
    return typ, imp


def wants_ai(d: dict) -> bool:
    t = d["title"].replace(" ", "")
    if any(k in t for k in SKIP_AI):
        return False
    return d["imp"] in ("high", "mid") or any(k in t for k in EVENT_HINTS)


def fetch_list(key: str, day: str) -> list[dict]:
    items, page = [], 1
    while True:
        r = requests.get(DART_LIST, params={
            "crtfc_key": key, "bgn_de": day, "end_de": day,
            "page_no": page, "page_count": 100,
        }, timeout=30)
        r.raise_for_status()
        j = r.json()
        status = j.get("status")
        if status == "013":  # 조회된 데이터 없음
            break
        if status != "000":
            sys.exit(f"DART 오류 {status}: {j.get('message')}")
        items.extend(j.get("list", []))
        if page >= int(j.get("total_page", 1)):
            break
        page += 1
        time.sleep(0.2)
    return items


def doc_text(key: str, rcept_no: str, limit: int = 12000) -> str:
    r = requests.get(DART_DOC, params={"crtfc_key": key, "rcept_no": rcept_no}, timeout=60)
    r.raise_for_status()
    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile:
        return ""
    raw = zf.read(zf.namelist()[0]).decode("utf-8", errors="ignore")
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"&[a-z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


PROMPT = """다음은 {corp}의 공시 「{title}」 원문(접수일 {date})입니다.
원문에 있는 사실만으로 아래 JSON을 만들어 주세요. 추측, 전망, 투자 의견은 쓰지 마세요.
JSON 외의 다른 글은 쓰지 마세요.

{{
  "summary": "투자자가 가장 먼저 알아야 할 내용을 한국어 한두 문장(80자 내외)으로",
  "points": ["핵심 내용 2~4개. 방식, 목적, 조건, 상대방 등. 각 40자 이내"],
  "numbers": [["항목명", "값"], "금액·비율·주식수·가격 등 핵심 수치 최대 4개. 값은 '820억원', '25%'처럼 단위 포함"],
  "events": [{{"date": "YYYY-MM-DD", "kind": "{kinds} 중 하나", "desc": "30자 이내 설명"}}]
}}

events에는 원문에 날짜가 명시된, 접수일 이후의 일정만 넣으세요(없으면 빈 배열).
numbers 배열의 각 원소는 ["항목명", "값"] 형태의 두 칸짜리 배열이어야 합니다.

원문:
{body}"""


def analyze(api_key: str, model: str, d: dict, date: str, body: str) -> dict:
    prompt = PROMPT.format(corp=d["corp"], title=d["title"], date=date,
                           kinds="/".join(EVENT_KINDS), body=body)
    r = requests.post("https://api.anthropic.com/v1/messages", headers={
        "x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json",
    }, json={"model": model, "max_tokens": 800,
             "messages": [{"role": "user", "content": prompt}]}, timeout=90)
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json().get("content", []))
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("AI 응답에서 JSON을 찾지 못함")
    j = json.loads(m.group(0))
    nums = [n for n in j.get("numbers", []) if isinstance(n, list) and len(n) == 2]
    evs = []
    for e in j.get("events", []):
        if isinstance(e, dict) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(e.get("date", ""))):
            kind = e.get("kind") if e.get("kind") in EVENT_KINDS else "기타"
            evs.append({"date": e["date"], "kind": kind, "desc": str(e.get("desc", ""))[:60]})
    return {
        "summary": str(j.get("summary", "")).strip(),
        "points": [str(p) for p in j.get("points", [])][:4],
        "numbers": [[str(a), str(b)] for a, b in nums][:4],
        "events": evs[:6],
    }


def update_events(items: list[dict], today: str):
    """AI가 뽑은 일정을 events_auto.json에 누적 (지난 7일 ~ 앞으로 90일만 보관)"""
    store = {}
    if EVENTS_OUT.exists():
        for e in json.loads(EVENTS_OUT.read_text(encoding="utf-8")).get("items", []):
            store[(e["rcept_no"], e["d"], e["k"])] = e
    for d in items:
        for e in d.get("events", []):
            store[(d["rcept_no"], e["date"], e["kind"])] = {
                "d": e["date"], "k": e["kind"], "tone": EVENT_KINDS.get(e["kind"], ""),
                "c": d["corp"], "s": e["desc"], "rcept_no": d["rcept_no"], "url": d["url"],
            }
    lo = (datetime.fromisoformat(today) - timedelta(days=7)).strftime("%Y-%m-%d")
    hi = (datetime.fromisoformat(today) + timedelta(days=90)).strftime("%Y-%m-%d")
    out = sorted((e for e in store.values() if lo <= e["d"] <= hi), key=lambda e: (e["d"], e["c"]))
    EVENTS_OUT.write_text(json.dumps({"updated_at": today, "items": out},
                                     ensure_ascii=False, indent=1), encoding="utf-8")
    return len(out)


def main():
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key:
        sys.exit("DART_API_KEY 환경변수가 없습니다. GitHub Secrets 또는 .env에 넣어주세요.")
    ai_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    model = os.environ.get("SUMMARY_MODEL", "claude-haiku-5-5")
    max_sum = int(os.environ.get("MAX_SUMMARIES", "25"))

    now = datetime.now(KST)
    day = now.strftime("%Y%m%d")
    iso_day = now.strftime("%Y-%m-%d")

    # 같은 날짜의 이전 결과가 있으면 '처음 확인한 시각'과 AI 분석을 이어받음
    prev = {}
    if OUT.exists():
        old = json.loads(OUT.read_text(encoding="utf-8"))
        if old.get("date") == iso_day:
            prev = {d["rcept_no"]: d for d in old.get("items", [])}
        else:
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            (ARCHIVE_DIR / f"{old.get('date', 'unknown')}.json").write_text(
                json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")

    raw = fetch_list(key, day)
    items = []
    for x in raw:
        if x.get("corp_cls") not in MARKETS:
            continue
        no = x["rcept_no"]
        title = x["report_nm"].strip()
        typ, imp = classify(title)
        p = prev.get(no, {})
        items.append({
            "rcept_no": no,
            "corp": x["corp_name"],
            "stock_code": x.get("stock_code", ""),
            "mkt": MARKETS[x["corp_cls"]],
            "type": typ,
            "imp": imp,
            "title": title,
            "flr": x.get("flr_nm", ""),
            "rm": x.get("rm", ""),
            "seen": p.get("seen") or now.strftime("%H:%M"),
            "ai": p.get("ai", ""),
            "points": p.get("points", []),
            "numbers": p.get("numbers", []),
            "events": p.get("events", []),
            "analyzed": p.get("analyzed", False),
            "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={no}",
        })

    items.sort(key=lambda d: (d["seen"], d["rcept_no"]), reverse=True)

    if ai_key:
        order = {"high": 0, "mid": 1, "low": 2}
        todo = sorted((d for d in items if not d["analyzed"] and wants_ai(d)),
                      key=lambda d: order[d["imp"]])[:max_sum]
        done = 0
        for d in todo:
            try:
                body = doc_text(key, d["rcept_no"])
                if body:
                    res = analyze(ai_key, model, d, iso_day, body)
                    d["ai"], d["points"] = res["summary"], res["points"]
                    d["numbers"], d["events"] = res["numbers"], res["events"]
                    done += 1
                d["analyzed"] = True
            except Exception as e:  # 분석 실패는 수집을 막지 않음 (다음 실행 때 재시도)
                print(f"AI 분석 실패 {d['corp']} {d['rcept_no']}: {e}")
            time.sleep(0.3)
        print(f"AI 분석 {done}건")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "date": iso_day,
        "updated_at": now.strftime("%Y-%m-%d %H:%M"),
        "count": len(items),
        "items": items,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    n_ev = update_events(items, iso_day)
    print(f"{iso_day} 코스피·코스닥 공시 {len(items)}건, 예정 일정 {n_ev}건 저장")


if __name__ == "__main__":
    main()
