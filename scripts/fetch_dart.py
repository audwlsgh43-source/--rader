"""
공시레이더 - DART 공시 수집 스크립트

오늘(한국시간) 접수된 코스피·코스닥 공시를 OpenDART에서 받아
유형·중요도를 붙여 data/latest.json 으로 저장합니다.

환경변수
  DART_API_KEY       (필수) OpenDART 인증키 40자리
  ANTHROPIC_API_KEY  (선택) 있으면 중요도 '높음' 공시를 AI로 한두 줄 요약
  SUMMARY_MODEL      (선택) 요약 모델, 기본 claude-haiku-5-5
  MAX_SUMMARIES      (선택) 1회 실행당 새로 요약할 최대 건수, 기본 15
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


def doc_text(key: str, rcept_no: str, limit: int = 8000) -> str:
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


def summarize(api_key: str, model: str, corp: str, title: str, body: str) -> str:
    prompt = (
        f"다음은 {corp}의 공시 '{title}' 원문 일부입니다.\n"
        "투자자가 꼭 알아야 할 핵심(금액, 비율, 일정, 방식)을 한국어 한두 문장, 80자 내외로 요약하세요. "
        "추측이나 투자 의견은 쓰지 말고 원문에 있는 사실만 쓰세요.\n\n" + body
    )
    r = requests.post("https://api.anthropic.com/v1/messages", headers={
        "x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json",
    }, json={"model": model, "max_tokens": 200, "messages": [{"role": "user", "content": prompt}]}, timeout=60)
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()


def main():
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key:
        sys.exit("DART_API_KEY 환경변수가 없습니다. GitHub Secrets 또는 .env에 넣어주세요.")
    ai_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    model = os.environ.get("SUMMARY_MODEL", "claude-haiku-5-5")
    max_sum = int(os.environ.get("MAX_SUMMARIES", "15"))

    now = datetime.now(KST)
    day = now.strftime("%Y%m%d")
    iso_day = now.strftime("%Y-%m-%d")

    # 같은 날짜의 이전 결과가 있으면 '처음 확인한 시각'과 요약을 이어받음
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
            "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={no}",
        })

    if ai_key:
        todo = [d for d in items if d["imp"] == "high" and not d["ai"]][:max_sum]
        for d in todo:
            try:
                body = doc_text(key, d["rcept_no"])
                if body:
                    d["ai"] = summarize(ai_key, model, d["corp"], d["title"], body)
            except Exception as e:  # 요약 실패는 수집을 막지 않음
                print(f"요약 실패 {d['corp']} {d['rcept_no']}: {e}")
            time.sleep(0.5)

    items.sort(key=lambda d: (d["seen"], d["rcept_no"]), reverse=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "date": iso_day,
        "updated_at": now.strftime("%Y-%m-%d %H:%M"),
        "count": len(items),
        "items": items,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{iso_day} 코스피·코스닥 공시 {len(items)}건 저장 → {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
