"""
공시레이더 - DART 공시 수집 스크립트

코스피·코스닥 공시를 OpenDART에서 받아 유형·중요도를 붙여
data/days/YYYY-MM-DD.json 에 날짜별로 계속 쌓습니다(삭제하지 않음).
매 실행마다 최근 5일(주말 포함)을 다시 확인해, 공시가 있는 최근 3일을 갱신·AI 분석합니다.
ANTHROPIC_API_KEY가 있으면 주요 공시 원문을 AI가 읽고
  - 한 줄 요약, 핵심 포인트, 핵심 수치  → 날짜별 파일
  - 앞으로 예정된 일정(주총일, 배당기준일, 신주상장일 등) → data/events_auto.json
을 만듭니다.

환경변수
  DART_API_KEY       (필수) OpenDART 인증키 40자리
  ANTHROPIC_API_KEY  (선택) AI 요약·일정 추출
  SUMMARY_MODEL      (선택) 기본 claude-haiku-5-5
  MAX_SUMMARIES      (선택) 1회 실행당 새로 분석할 최대 건수, 기본 600
  AI_TIME_LIMIT      (선택) 1회 실행에서 AI 분석에 쓸 최대 시간(초), 기본 420 = 7분
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
DAYS_DIR = ROOT / "data" / "days"

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
# AI 분석에서 제외: 수백 쪽짜리 정기보고서·신고서 (앞부분 12,000자로는 목차뿐이라 요약이 안 됨)
SKIP_AI = ["분기보고서", "반기보고서", "사업보고서", "감사보고서", "증권신고서", "투자설명서"]

EVENT_KINDS = {
    "잠정실적발표": "", "IR(실적발표)": "", "IR(경영현황)": "", "주주총회": "",
    "배당기준일": "up", "배당지급": "up", "청약": "", "납입일": "", "신주배정기준일": "",
    "신주상장": "down", "권리락": "", "감자기준일": "down", "합병기일": "", "분할기일": "",
    "매매거래정지": "down", "변경상장": "", "상호변경": "", "신규상장": "up", "재상장": "up",
    "전환청구": "down", "보호예수해제": "down", "기타": "",
}

# 캘린더 분류: 일정이 나온 '원래 공시'의 제목으로 정함 (위에서부터 먼저 맞는 것)
CAT_RULES = [
    ("잠정실적", ["잠정실적", "영업(잠정)실적"]),
    ("액면변경", ["주식분할", "주식병합", "액면분할", "액면병합", "액면변경"]),
    ("유상증자", ["유상증자", "유무상증자"]),
    ("무상증자", ["무상증자"]),
    ("감자", ["감자"]),
    ("합병/분할", ["합병", "분할"]),
    ("배당", ["배당"]),
    ("상호변경", ["상호변경"]),
    ("재상장", ["재상장"]),
    ("신규상장", ["신규상장", "주권상장", "상장예정"]),
    ("IR", ["기업설명회", "IR개최", "IR)개최"]),
]


def event_cat(title: str) -> str:
    t = title.replace(" ", "")
    for name, kws in CAT_RULES:
        if any(k in t for k in kws):
            return name
    return ""


# 회사 주식과 관계없는 상품·채권 공시 (증권사 ELS/DLS, 회사채, 기업어음 등) → 수집 제외
EXCLUDE_TITLES = ["파생결합", "일괄신고", "채무증권", "기업어음", "단기사채", "전자단기사채", "사채원리금",
                  "신탁계약", "집합투자", "투자회사", "증권발행조건확정", "조건부자본증권"]


def is_stock_disclosure(x: dict) -> bool:
    """코스피·코스닥 종목의 주식 관련 공시만 True"""
    if x.get("corp_cls") not in MARKETS:
        return False
    code = (x.get("stock_code") or "").strip()
    if not re.fullmatch(r"[0-9A-Z]{6}", code):
        return False
    t = (x.get("report_nm") or "").replace(" ", "")
    return not any(k in t for k in EXCLUDE_TITLES)


def classify(title: str) -> tuple[str, str]:
    t = title.replace(" ", "")
    typ = "etc"
    if not t.startswith("[") and not any(k in t for k in ("주식분할", "주식병합", "액면")):  # 정정공시·액면변경은 '기타'
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
    return not any(k in t for k in SKIP_AI)


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


def _clean(raw: str, limit: int) -> str:
    raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    text = re.sub(r"&[a-z]+;|&#\d+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def doc_text_api(key: str, rcept_no: str, limit: int) -> str:
    """OpenDART 원문 API (DART에 직접 제출된 공시)"""
    r = requests.get(DART_DOC, params={"crtfc_key": key, "rcept_no": rcept_no}, timeout=60)
    r.raise_for_status()
    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile:
        return ""
    raw = zf.read(zf.namelist()[0]).decode("utf-8", errors="ignore")
    return _clean(raw, limit)


VIEWER_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; gongsi-radar/1.0)"}


def doc_text_viewer(rcept_no: str, limit: int) -> str:
    """DART 웹 뷰어 (거래소 공시처럼 원문 API로 안 나오는 공시용)"""
    r = requests.get("https://dart.fss.or.kr/dsaf001/main.do", params={"rcpNo": rcept_no},
                     headers=VIEWER_HEADERS, timeout=30)
    r.raise_for_status()
    html = r.text
    m = re.search(r"viewDoc\(\s*'(\d+)'\s*,\s*'(\d+)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'", html)
    if m:
        rcp, dcm, ele, off, length, dtd = m.groups()
    else:
        m2 = re.search(r"dcmNo['\"]?\]?\s*[=:]\s*['\"](\d+)['\"]", html)
        if not m2:
            return ""
        rcp, dcm, ele, off, length, dtd = rcept_no, m2.group(1), "0", "0", "0", "HTML"
    v = requests.get("https://dart.fss.or.kr/report/viewer.do", params={
        "rcpNo": rcp, "dcmNo": dcm, "eleId": ele, "offset": off, "length": length, "dtd": dtd,
    }, headers=VIEWER_HEADERS, timeout=30)
    v.raise_for_status()
    v.encoding = v.apparent_encoding or "utf-8"
    return _clean(v.text, limit)


def doc_text(key: str, rcept_no: str, limit: int = 12000) -> str:
    text = ""
    try:
        text = doc_text_api(key, rcept_no, limit)
    except Exception:
        pass
    if len(text) < 50:
        try:
            text = doc_text_viewer(rcept_no, limit)
        except Exception as e:
            print(f"뷰어 원문 실패 {rcept_no}: {e}")
    return text if len(text) >= 50 else ""


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
기업설명회(IR)는 목적이 실적 발표면 "IR(실적발표)", 그 밖의 경영현황·사업설명이면 "IR(경영현황)"으로 쓰세요.
numbers 배열의 각 원소는 ["항목명", "값"] 형태의 두 칸짜리 배열이어야 합니다.

원문:
{body}"""


def parse_json_loose(text: str):
    """코드블록·앞뒤 설명이 섞이거나 끝이 잘린 응답에서도 최대한 JSON을 꺼냄"""
    t = re.sub(r"```(?:json)?", "", text).strip()
    start = t.find("{")
    if start < 0:
        return None
    t = t[start:]
    end = t.rfind("}")
    if end >= 0:
        try:
            return json.loads(t[:end + 1])
        except json.JSONDecodeError:
            pass
    # 끝이 잘린 경우: summary만이라도 살림
    m = re.search(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)"', t)
    if m:
        pts = re.findall(r'"points"\s*:\s*\[(.*?)\]', t, re.S)
        points = re.findall(r'"((?:[^"\\]|\\.)*)"', pts[0]) if pts else []
        return {"summary": m.group(1), "points": points, "numbers": [], "events": []}
    return None


def analyze(api_key: str, model: str, d: dict, date: str, body: str) -> dict:
    prompt = PROMPT.format(corp=d["corp"], title=d["title"], date=date,
                           kinds="/".join(EVENT_KINDS), body=body)
    r = requests.post("https://api.anthropic.com/v1/messages", headers={
        "x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json",
    }, json={"model": model, "max_tokens": 3000,
             "messages": [{"role": "user", "content": prompt}]}, timeout=120)
    r.raise_for_status()
    rj = r.json()
    text = "".join(b.get("text", "") for b in rj.get("content", []) if b.get("type") == "text")
    j = parse_json_loose(text)
    if j is None:
        kinds = ",".join(b.get("type", "?") for b in rj.get("content", []))
        snippet = re.sub(r"\s+", " ", text)[:100]
        raise ValueError(f"AI 응답 형식 오류 (stop_reason={rj.get('stop_reason')}, 블록={kinds}, 앞부분='{snippet}')")
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


def ai_error_text(e: Exception) -> tuple[str, bool]:
    """AI 오류를 사람이 읽을 수 있는 문장으로. (문장, 이번 실행에서 더 시도해도 소용없는 오류인지)"""
    resp = getattr(e, "response", None)
    code = getattr(resp, "status_code", None)
    body = ""
    try:
        body = resp.text[:300] if resp is not None else ""
    except Exception:
        pass
    low = body.lower()
    if code == 401:
        return "Anthropic API 키가 올바르지 않아요. 키를 새로 만들어 GitHub Secrets의 ANTHROPIC_API_KEY를 바꿔 주세요.", True
    if code == 403:
        return "Anthropic API 키에 권한이 없어요. 콘솔에서 키 상태를 확인해 주세요.", True
    if "credit" in low or "billing" in low or "balance" in low:
        return "Anthropic 크레딧이 부족해요. console.anthropic.com → Billing에서 크레딧을 충전해 주세요.", True
    if code == 404 or "model" in low and "not" in low:
        return f"AI 모델 이름을 찾지 못했어요 (응답: {body[:120]})", True
    if code == 429:
        return "Anthropic 사용 한도(분당 요청 수)에 걸렸어요. 잠시 뒤 자동으로 다시 시도해요.", False
    if code and code >= 500:
        return "Anthropic 서버가 일시적으로 응답하지 않아요. 다음 실행 때 다시 시도해요.", False
    return f"AI 요약 실패: {str(e)[:220]}", False


def update_events(items: list[dict], today: str):
    """공시에서 나온 일정을 events_auto.json에 누적 (지난 7일 ~ 앞으로 90일만 보관)"""
    store = {}
    if EVENTS_OUT.exists():
        for e in json.loads(EVENTS_OUT.read_text(encoding="utf-8")).get("items", []):
            store[(e["rcept_no"], e["d"], e["k"])] = e
    # 저장된 날짜별 공시에 남아 있는(=상장사 주식 공시인) 접수번호만 유지 → 예전에 섞여 들어온 상품 공시 일정 정리
    valid = {}
    for f in DAYS_DIR.glob("????-??-??.json"):
        try:
            for it in json.loads(f.read_text(encoding="utf-8")).get("items", []):
                if not any(k in it.get("title", "").replace(" ", "") for k in EXCLUDE_TITLES):
                    valid[it["rcept_no"]] = it
        except Exception:
            pass
    for it in items:
        valid[it["rcept_no"]] = it
    store = {k: e for k, e in store.items() if e["rcept_no"] in valid}
    for d in items:
        cat = event_cat(d["title"]) or "기타"
        evs = list(d.get("events", []))
        # 잠정실적은 공시일이 곧 일정 → AI 없이 바로 등록
        if cat == "잠정실적" and not d["title"].startswith("["):
            evs.append({"date": d["date"], "kind": "잠정실적발표", "desc": d.get("ai") or "잠정실적 공시"})
        for e in evs:
            store[(d["rcept_no"], e["date"], e["kind"])] = {
                "d": e["date"], "k": e["kind"], "cat": cat, "tone": EVENT_KINDS.get(e["kind"], ""),
                "c": d["corp"], "code": d.get("stock_code", ""), "s": e["desc"][:60], "rcept_no": d["rcept_no"], "url": d["url"],
            }
    lo = (datetime.fromisoformat(today) - timedelta(days=7)).strftime("%Y-%m-%d")
    hi = (datetime.fromisoformat(today) + timedelta(days=90)).strftime("%Y-%m-%d")
    out = sorted((e for e in store.values() if lo <= e["d"] <= hi), key=lambda e: (e["d"], e["c"]))
    EVENTS_OUT.write_text(json.dumps({"updated_at": today, "items": out},
                                     ensure_ascii=False, indent=1), encoding="utf-8")
    return len(out)


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def migrate_old_files():
    """이전 버전의 latest.json / archive/*.json 을 days/ 폴더로 옮김"""
    DAYS_DIR.mkdir(parents=True, exist_ok=True)
    olds = list(ARCHIVE_DIR.glob("*.json")) if ARCHIVE_DIR.exists() else []
    if OUT.exists():
        olds.append(OUT)
    for f in olds:
        j = load_json(f)
        date = j.get("date")
        if date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) and not (DAYS_DIR / f"{date}.json").exists():
            for it in j.get("items", []):
                it.setdefault("date", date)
            (DAYS_DIR / f"{date}.json").write_text(json.dumps(j, ensure_ascii=False, indent=1), encoding="utf-8")
        f.unlink()
    if ARCHIVE_DIR.exists() and not any(ARCHIVE_DIR.iterdir()):
        ARCHIVE_DIR.rmdir()


def build_items(raw: list[dict], date: str, prev: dict, seen_now: str) -> list[dict]:
    items = []
    for x in raw:
        if not is_stock_disclosure(x):
            continue
        no = x["rcept_no"]
        title = x["report_nm"].strip()
        typ, imp = classify(title)
        p = prev.get(no, {})
        items.append({
            "rcept_no": no,
            "date": date,
            "corp": x["corp_name"],
            "stock_code": x.get("stock_code", ""),
            "corp_code": x.get("corp_code", ""),
            "mkt": MARKETS[x["corp_cls"]],
            "type": typ,
            "imp": imp,
            "title": title,
            "flr": x.get("flr_nm", ""),
            "rm": x.get("rm", ""),
            "seen": p.get("seen") if "seen" in p else seen_now,
            "ai": p.get("ai", ""),
            "points": p.get("points", []),
            "numbers": p.get("numbers", []),
            "events": p.get("events", []),
            # 이전 버전에서 원문을 못 읽은 공시(read_fail 표시 없음)는 웹 뷰어로 한 번 더 시도
            "analyzed": p.get("analyzed", False) and (bool(p.get("ai")) or bool(p.get("read_fail"))),
            **({"read_fail": True} if p.get("read_fail") else {}),
            "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={no}",
        })
    items.sort(key=lambda d: (d["seen"] or "", d["rcept_no"]), reverse=True)
    return items


def main():
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key:
        sys.exit("DART_API_KEY 환경변수가 없습니다. GitHub Secrets 또는 .env에 넣어주세요.")
    ai_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    model = os.environ.get("SUMMARY_MODEL", "claude-haiku-5-5")
    max_sum = int(os.environ.get("MAX_SUMMARIES", "600"))
    time_limit = int(os.environ.get("AI_TIME_LIMIT", "420"))
    workers = int(os.environ.get("AI_WORKERS", "4"))        # 동시에 분석할 개수
    show_days = int(os.environ.get("SHOW_DAYS", "3"))      # 공시가 있는 최근 며칠을 보여주고 AI 분석할지
    look_back = int(os.environ.get("LOOKBACK_DAYS", "5"))  # 주말을 건너뛰기 위해 달력 기준 며칠을 확인할지

    migrate_old_files()
    now = datetime.now(KST)
    today = now.date()
    iso_today = today.isoformat()

    # 매번 최근 LOOKBACK_DAYS일(주말 포함)을 다시 확인 → 공시가 있는 최근 SHOW_DAYS일이 항상 채워짐
    targets = [today - timedelta(days=i) for i in range(look_back)]

    days = {}
    for d in targets:
        iso = d.isoformat()
        old = load_json(DAYS_DIR / f"{iso}.json")
        prev = {x["rcept_no"]: x for x in old.get("items", [])}
        raw = fetch_list(key, d.strftime("%Y%m%d"))
        # 오늘 처음 본 공시에만 확인 시각을 붙이고, 지난 날짜 공시는 시각을 비워 둠
        days[iso] = build_items(raw, iso, prev, now.strftime("%H:%M") if iso == iso_today else "")

    ai_error = ""
    if ai_key:
        order = {"high": 0, "mid": 1, "low": 2}
        recent = sorted((iso for iso, items in days.items() if items), reverse=True)[:show_days]
        pool = [x for iso in recent for x in days[iso]]
        todo = [x for x in pool if not x["analyzed"] and wants_ai(x)]
        todo.sort(key=lambda x: x["seen"] or "", reverse=True)                       # 같은 조건이면 최신 먼저
        # 일정 분류 공시 먼저 → 최근 날짜 → 중요도 순
        todo.sort(key=lambda x: (0 if event_cat(x["title"]) else 1, -int(x["date"].replace("-", "")), order[x["imp"]]))
        todo = todo[:max_sum]

        errors, fatal = [], []

        t_start = time.time()

        def work(x):
            if fatal:  # 키·크레딧 문제면 남은 건은 시도하지 않음
                return 0
            if time.time() - t_start > time_limit:  # 시간 초과분은 다음 실행에서 이어서
                return 0
            try:
                body = doc_text(key, x["rcept_no"])
                if body:
                    res = analyze(ai_key, model, x, x["date"], body)
                    x["ai"], x["points"] = res["summary"], res["points"]
                    x["numbers"], x["events"] = res["numbers"], res["events"]
                    x.pop("read_fail", None)
                else:
                    x["read_fail"] = True  # 원문을 끝내 못 읽음 → 사이트 피드에서 숨김
                x["analyzed"] = True
                return 1 if x["ai"] else 0
            except Exception as e:  # 분석 실패는 수집을 막지 않음 (다음 실행 때 재시도)
                msg, stop = ai_error_text(e)
                errors.append(msg)
                if stop or (len(errors) >= 5 and not any(t.get("ai") for t in todo)):
                    fatal.append(msg)  # 같은 오류가 반복되면 비용 낭비를 막기 위해 이번 실행은 중단
                print(f"AI 분석 실패 {x['corp']} {x['rcept_no']}: {msg}")
                return 0

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as ex:
            done = sum(ex.map(work, todo))
        if done == 0 and errors:
            ai_error = fatal[0] if fatal else errors[0]
        left = sum(1 for x in pool if not x["analyzed"] and wants_ai(x))
        print(f"AI 분석 {done}건 (남은 대상 {left}건)")

    stamp = now.strftime("%Y-%m-%d %H:%M")
    # 예전에 저장된 날짜 파일에서도 상품·채권 공시를 정리 (한 번 정리되면 다시 바뀌지 않음)
    for f in DAYS_DIR.glob("????-??-??.json"):
        if f.stem in days:
            continue
        j = load_json(f)
        its = j.get("items", [])
        keep = [x for x in its if x.get("stock_code") and not any(k in x.get("title", "").replace(" ", "") for k in EXCLUDE_TITLES)]
        if len(keep) != len(its):
            j["items"], j["count"] = keep, sum(1 for x in keep if not x.get("read_fail"))
            f.write_text(json.dumps(j, ensure_ascii=False, indent=1), encoding="utf-8")
    for iso, items in days.items():
        (DAYS_DIR / f"{iso}.json").write_text(json.dumps({
            "date": iso, "updated_at": stamp, "count": sum(1 for x in items if not x.get("read_fail")), "items": items,
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    # 날짜 목록 (사이트가 이 파일을 보고 날짜 탭을 만듦)
    index = []
    for f in sorted(DAYS_DIR.glob("????-??-??.json"), reverse=True):
        if f.stem in days:
            n = sum(1 for x in days[f.stem] if not x.get("read_fail"))
        else:
            n = load_json(f).get("count", 0)
        index.append({"date": f.stem, "count": n})
    (DAYS_DIR / "index.json").write_text(json.dumps({
        "today": iso_today, "updated_at": stamp, "ai": bool(ai_key), "ai_error": ai_error, "days": index,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    n_ev = update_events([x for items in days.values() for x in items], iso_today)
    summary = ", ".join(f"{k} {len(v)}건" for k, v in sorted(days.items(), reverse=True))
    print(f"저장: {summary} / 예정 일정 {n_ev}건 / 누적 {len(index)}일")


if __name__ == "__main__":
    main()
