"""
공시레이더 - 종목 상세 데이터 → data/corps/{종목코드}.json

  - 모든 종목: 최근 날짜별 공시 파일에서 그 종목의 공시 기록을 모아 누적 (최근 120건)
  - 관심종목(watchlist.json)만 추가로
      · 주가 추이: Yahoo Finance 일봉 6개월 (1시간에 한 번 갱신)
      · 재무 요약: OpenDART 단일회사 주요계정 (매출액·영업이익·당기순이익, 주 1회 갱신)

환경변수: DART_API_KEY (재무 요약용)
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CORPS = DATA / "corps"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
KEEP = ("rcept_no", "date", "title", "type", "imp", "ai", "url", "seen")


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_if_changed(path: Path, obj) -> bool:
    text = json.dumps(obj, ensure_ascii=False, indent=1)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def recent_day_files(n: int = 5) -> list[Path]:
    files = sorted((DATA / "days").glob("????-??-??.json"), reverse=True)
    return files[:n]


def yahoo_prices(code: str, mkt: str) -> dict | None:
    suffixes = [".KS", ".KQ"] if mkt == "코스피" else [".KQ", ".KS"]
    for suf in suffixes:
        try:
            r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{code}{suf}",
                             params={"range": "6mo", "interval": "1d"}, headers=UA, timeout=20)
            if not r.ok:
                continue
            res = r.json()["chart"]["result"][0]
            ts, closes = res.get("timestamp") or [], res["indicators"]["quote"][0]["close"]
            pts = [(datetime.fromtimestamp(t, KST).strftime("%Y-%m-%d"), round(c, 2)) for t, c in zip(ts, closes) if c]
            if len(pts) > 5:
                return {"dates": [d for d, _ in pts], "close": [c for _, c in pts]}
        except Exception as e:
            print(f"주가 실패 {code}{suf}: {e}")
    return None


def dart_fin(key: str, corp_code: str, today) -> dict | None:
    tries = [(today.year, "11014", "3분기"), (today.year, "11012", "반기"), (today.year, "11013", "1분기"),
             (today.year - 1, "11011", "사업보고서"), (today.year - 1, "11014", "3분기")]
    for year, code, label in tries:
        try:
            r = requests.get("https://opendart.fss.or.kr/api/fnlttSinglAcnt.json", params={
                "crtfc_key": key, "corp_code": corp_code, "bsns_year": str(year), "reprt_code": code}, timeout=30)
            j = r.json()
        except Exception as e:
            print(f"재무 실패 {corp_code}: {e}")
            return None
        if j.get("status") != "000":
            continue
        rows = j.get("list", [])
        fs = "CFS" if any(x.get("fs_div") == "CFS" for x in rows) else "OFS"
        out = []
        for name in ("매출액", "영업이익", "당기순이익"):
            row = next((x for x in rows if x.get("fs_div") == fs and x.get("account_nm", "").replace(" ", "").startswith(name)), None)
            if row:
                out.append({"name": name, "cur": row.get("thstrm_amount", ""), "prev": row.get("frmtrm_amount", ""),
                            "cur_nm": row.get("thstrm_nm", ""), "prev_nm": row.get("frmtrm_nm", "")})
        if out:
            return {"year": year, "report": label, "fs": "연결" if fs == "CFS" else "별도", "rows": out}
    return None


def corp_code_map(key: str, now) -> dict:
    """종목코드 → DART 고유번호 (corpCode.xml, 30일마다 갱신)"""
    path = DATA / "corp_codes.json"
    cache = load(path, {})
    try:
        fresh = cache.get("at") and now - datetime.strptime(cache["at"], "%Y-%m-%d").replace(tzinfo=KST) < timedelta(days=30)
    except Exception:
        fresh = False
    if fresh or not key:
        return cache.get("map", {})
    try:
        import io, re, zipfile
        r = requests.get("https://opendart.fss.or.kr/api/corpCode.xml", params={"crtfc_key": key}, timeout=60)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        xml = z.read(z.namelist()[0]).decode("utf-8", errors="ignore")
        m = {}
        for blk in re.findall(r"<list>(.*?)</list>", xml, re.S):
            sc = re.search(r"<stock_code>\s*(\w+)\s*</stock_code>", blk)
            cc = re.search(r"<corp_code>\s*(\d+)\s*</corp_code>", blk)
            if sc and cc:
                m[sc.group(1)] = cc.group(1)
        if m:
            path.write_text(json.dumps({"at": now.strftime("%Y-%m-%d"), "map": m}, ensure_ascii=False), encoding="utf-8")
            return m
    except Exception as e:
        print("corpCode 목록 실패:", e)
    return cache.get("map", {})


def main():
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M")
    CORPS.mkdir(parents=True, exist_ok=True)
    key = os.environ.get("DART_API_KEY", "").strip()

    # 1) 공시 기록 누적
    by_code: dict[str, dict] = {}
    for f in recent_day_files():
        for x in load(f, {}).get("items", []):
            code = x.get("stock_code")
            if not code or x.get("read_fail"):
                continue
            c = by_code.setdefault(code, {"name": x["corp"], "mkt": x.get("mkt", ""), "corp_code": x.get("corp_code", ""), "items": []})
            if x.get("corp_code"):
                c["corp_code"] = x["corp_code"]
            c["items"].append({k: x.get(k, "") for k in KEEP})
    changed = 0
    for code, c in by_code.items():
        path = CORPS / f"{code}.json"
        old = load(path, {})
        merged = {d["rcept_no"]: d for d in old.get("disclosures", [])}
        for d in c["items"]:
            merged[d["rcept_no"]] = d
        obj = {**old, "name": c["name"], "code": code, "mkt": c["mkt"] or old.get("mkt", ""),
               "corp_code": c["corp_code"] or old.get("corp_code", ""),
               "disclosures": sorted(merged.values(), key=lambda d: d["rcept_no"], reverse=True)[:120]}
        changed += save_if_changed(path, obj)

    # 2) 관심종목: 주가·재무
    watch = load(ROOT / "watchlist.json", {}).get("items", [])
    need_map = any(w.get("code") and not load(CORPS / f'{w["code"]}.json', {}).get("corp_code") for w in watch)
    cmap = corp_code_map(key, now) if need_map else {}
    for w in watch:
        code = (w.get("code") or "").strip()
        if not code:
            continue
        path = CORPS / f"{code}.json"
        obj = load(path, {"name": w.get("name", ""), "code": code, "disclosures": []})
        obj.setdefault("name", w.get("name", ""))
        if not obj.get("corp_code") and cmap.get(code):
            obj["corp_code"] = cmap[code]
        try:
            due = not obj.get("prices_at") or now - datetime.strptime(obj["prices_at"], "%Y-%m-%d %H:%M").replace(tzinfo=KST) >= timedelta(minutes=55)
        except Exception:
            due = True
        if due:
            p = yahoo_prices(code, obj.get("mkt", ""))
            if p:
                obj["prices"], obj["prices_at"] = p, stamp
            time.sleep(0.5)
        try:
            fin_due = not obj.get("fin_at") or now - datetime.strptime(obj["fin_at"], "%Y-%m-%d %H:%M").replace(tzinfo=KST) >= timedelta(days=7)
        except Exception:
            fin_due = True
        if fin_due and key and obj.get("corp_code"):
            fin = dart_fin(key, obj["corp_code"], now.date())
            if fin:
                obj["fin"] = fin
            obj["fin_at"] = stamp
        changed += save_if_changed(path, obj)
    print(f"종목 파일 갱신 {changed}개 (공시 종목 {len(by_code)}, 관심종목 {len(watch)})")


if __name__ == "__main__":
    main()
