# -*- coding: utf-8 -*-
"""
GSRC 춘천마라톤 라이브 트래커 - 기록 수집기
마이리저트(myresult.co.kr) 선수 기록 페이지의 구간 표를 읽어 data.json 으로 저장합니다.

- 페이지 표 구조: 구간명 / 통과시간 / 구간기록 / 누적기록
  (출발, 반환점, 5K, 10K, 15K, 20K, Half, 25K, 30K, 35K, 40K, 도착)
- 통과시간이 찍힌 가장 먼 구간을 '현재 위치'로 봅니다.
"""
import html as htmlmod
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

# ───────────────────────── 설정 ─────────────────────────
EVENT_ID = "176"  # 2026 춘천마라톤

# 5K 전 첫 '반환점' 측정 지점 거리(km) — 공식 GPX 코스에서 계산한 값 (3.73km)
# 화면(index.html)의 TURN_KM 과 같은 값이어야 합니다.
TURN_KM = 3.73

DATA_PATH = os.environ.get("DATA_PATH", "data.json")
DEBUG_SAMPLE_PATH = os.environ.get("DEBUG_SAMPLE_PATH", "")

# [비상 수동 보정] 특정 선수 거리를 강제로 맞출 때 사용  예) {"6327": 21.0975}
OVERRIDES = {
    # "6327": 12.5,
}

# 크루 명단 (이름, 배번, 조)  ─ 조별 출발: A 09:00 / B 09:05 / C 09:10 / D 09:15 / E 09:20 / F 09:25
runners = [
    # A조
    {"name": "이재형", "bib": "1234",  "group": "A"},
    # B조
    {"name": "김민호", "bib": "3093",  "group": "B"},
    {"name": "강성원", "bib": "3009",  "group": "B"},
    {"name": "김영식", "bib": "4332",  "group": "B"},
    {"name": "김희정", "bib": "5006",  "group": "B"},
    {"name": "한호규", "bib": "4293",  "group": "B"},
    # C조
    {"name": "서형관", "bib": "6327",  "group": "C"},
    {"name": "김민성", "bib": "6086",  "group": "C"},
    {"name": "이강훈", "bib": "7664",  "group": "C"},
    {"name": "김영환", "bib": "6857",  "group": "C"},
    {"name": "양승열", "bib": "6388",  "group": "C"},
    {"name": "이지훈", "bib": "7381",  "group": "C"},
    {"name": "최이현", "bib": "8036",  "group": "C"},
    # D조
    {"name": "전희경", "bib": "11424", "group": "D"},
    {"name": "박해정", "bib": "11025", "group": "D"},
    # E조
    {"name": "한태호", "bib": "12635", "group": "E"},
    {"name": "손태민", "bib": "13661", "group": "E"},
    {"name": "최현식", "bib": "13318", "group": "E"},
    {"name": "하늘이", "bib": "14428", "group": "E"},
    {"name": "정슬아", "bib": "14400", "group": "E"},
    # F조
    {"name": "이성희", "bib": "17111", "group": "F"},
    {"name": "김병채", "bib": "15089", "group": "F"},
    {"name": "이광은", "bib": "16089", "group": "F"},
]
# ────────────────────────────────────────────────────────

FULL_KM = 42.195
KST = timezone(timedelta(hours=9))

PAGE_URL = "https://myresult.co.kr/{event_id}/{bib}"
API_URLS = [  # 혹시 JSON 주소가 열려 있을 경우를 대비한 보조 경로
    "https://myresult.co.kr/api/event/{event_id}/player/{bib}",
]
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

TIME_RE = re.compile(r"^(\d{1,2}):([0-5]\d)(?::([0-5]\d))?(?:\.\d+)?$")
FIXED_POINTS = {
    "출발": 0.0, "START": 0.0,
    "반환점": TURN_KM, "반환": TURN_KM,
    "HALF": 21.0975, "하프": 21.0975,
    "도착": FULL_KM, "FINISH": FULL_KM, "골인": FULL_KM, "완주": FULL_KM,
}


def is_valid_time(value):
    """'09:12:33', '1:45:20', '45:12' 같은 시간만 인정. '?', '-', '00:00:00' 은 무효."""
    if value is None:
        return False
    m = TIME_RE.match(str(value).strip())
    if not m:
        return False
    return any(int(g) for g in m.groups() if g)


def label_to_km(label):
    """구간 이름 → km.  '5K'→5, 'Half'→21.0975, '반환점'→TURN_KM, '도착'→42.195, '출발'→0."""
    if label is None:
        return None
    s = re.sub(r"\s+", "", str(label)).upper()
    if not s or len(s) > 12:
        return None
    if s in FIXED_POINTS:
        return FIXED_POINTS[s]
    m = re.match(r"^(\d{1,2}(?:\.\d+)?)(K|KM)$", s)
    if m:
        v = float(m.group(1))
        if 0 < v < FULL_KM:  # '42.195K' 같은 코스 총거리 표기는 제외
            return v
    return None


def html_to_lines(text):
    t = re.sub(r"(?is)<(script|style|noscript|svg)\b[^>]*>.*?</\1>", " ", text)
    t = re.sub(r"(?s)<!--.*?-->", " ", t)
    t = re.sub(r"<[^>]+>", "\n", t)
    t = htmlmod.unescape(t)
    return [ln.strip() for ln in t.split("\n") if ln.strip()]


def finish(best, splits, start_time):
    if best is not None:
        best["start_time"] = start_time
        best["splits"] = sorted(splits.values(), key=lambda s: s["km"])
    return best


def parse_html(text):
    """반환: (가장 먼 통과 구간 정보 or None, 표에서 찾은 구간 이름 개수, 줄 목록)"""
    lines = html_to_lines(text)
    best, labels_found, start_time, splits = None, 0, None, {}
    for i, line in enumerate(lines):
        km = label_to_km(line)
        if km is None:
            continue
        nxt = lines[i + 1:i + 4]
        if not nxt:
            continue
        labels_found += 1
        if not is_valid_time(nxt[0]):
            continue  # '?' → 아직 통과 전
        if km == 0.0:
            start_time = nxt[0]  # 출발 매트를 밟은 시각 (개인 출발 시각)
        net = nxt[2] if len(nxt) >= 3 and is_valid_time(nxt[2]) else None
        splits[km] = {"km": km, "label": line, "pass": nxt[0], "net": net}
        cand = {"km": km, "point": line, "pass_time": nxt[0], "net_time": net}
        if best is None or km >= best["km"]:
            best = cand
    return finish(best, splits, start_time), labels_found, lines


def parse_json(data):
    """JSON 형태로 받을 경우의 보조 파서."""
    state = {"best": None, "start": None}
    splits = {}

    def consider(label, t, net=None):
        km = label_to_km(label)
        if km is None or not is_valid_time(t):
            return
        t = str(t).strip()
        net = str(net).strip() if is_valid_time(net) else None
        if km == 0.0:
            state["start"] = t
        splits[km] = {"km": km, "label": str(label), "pass": t, "net": net}
        if state["best"] is None or km >= state["best"]["km"]:
            state["best"] = {"km": km, "point": str(label), "pass_time": t, "net_time": net}

    def pick(o, keys):
        for k in keys:
            v = o.get(k)
            if isinstance(v, (str, int, float)) and str(v).strip():
                return v
        return None

    def walk(o):
        if isinstance(o, dict):
            label = pick(o, ("point", "pointName", "section", "sectionName", "name", "title", "label"))
            t = pick(o, ("passTime", "time", "clockTime", "record", "netTime", "chipTime"))
            net = pick(o, ("netTime", "cumulative", "totalTime", "accTime", "chipTime"))
            if label is not None and t is not None:
                consider(label, t, net)
            for k, v in o.items():
                if isinstance(v, (dict, list)):
                    walk(v)
                else:
                    consider(k, v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(data)
    return finish(state["best"], splits, state["start"])


def fetch_runner(bib):
    """한 선수의 기록을 가져옴. ok=False 면 '이번엔 못 읽었음'(이전 값 유지)."""
    last_err = ""
    url = PAGE_URL.format(event_id=EVENT_ID, bib=bib)
    for _ in range(2):
        try:
            res = requests.get(url, headers=HEADERS, timeout=8)
            if res.status_code == 200:
                text = res.text
                m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
                if m:
                    try:
                        best = parse_json(json.loads(m.group(1)))
                        if best:
                            return {"ok": True, "best": best, "source": "next", "raw": text}
                    except Exception:
                        pass
                best, labels_found, _ = parse_html(text)
                if labels_found > 0:
                    return {"ok": True, "best": best, "source": "html", "raw": text}
                last_err = "구간표를 찾지 못함(사이트 구조 변경 의심)"
                break
            last_err = f"HTTP {res.status_code}"
        except Exception as e:  # 네트워크 오류
            last_err = str(e)[:120]
        time.sleep(1.0)

    for tpl in API_URLS:  # 보조 경로
        try:
            res = requests.get(tpl.format(event_id=EVENT_ID, bib=bib), headers=HEADERS, timeout=6)
            if res.status_code == 200:
                best = parse_json(res.json())
                return {"ok": True, "best": best, "source": "api", "raw": res.text}
        except Exception:
            pass
    return {"ok": False, "error": last_err}


def load_prev():
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            return {"runners": raw}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def main():
    now = time.time()
    prev = load_prev()
    prev_map = {str(r.get("bib")): r for r in prev.get("runners", []) if isinstance(r, dict)}

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {r["bib"]: ex.submit(fetch_runner, r["bib"]) for r in runners}
        results = {}
        for bib, fut in futures.items():
            try:
                results[bib] = fut.result()
            except Exception as e:
                results[bib] = {"ok": False, "error": str(e)[:120]}

    out, ok_count, errors, sample = [], 0, [], None
    for r in runners:
        bib = r["bib"]
        old = prev_map.get(bib, {})
        try:
            old_km = float(old.get("km") or 0)
        except (TypeError, ValueError):
            old_km = 0.0
        rec = {
            "name": r["name"], "bib": bib, "group": r["group"],
            "km": old_km,
            "started": bool(old.get("started")) or old_km > 0,
            "point": old.get("point"),
            "pass_time": old.get("pass_time"),
            "net_time": old.get("net_time"),
            "start_time": old.get("start_time"),
            "splits": old.get("splits") if isinstance(old.get("splits"), list) else [],
            "km_epoch": float(old.get("km_epoch") or now),
        }
        res = results.get(bib, {})

        if bib in OVERRIDES:
            rec.update(km=float(OVERRIDES[bib]), started=True, point="수동 보정")
        elif res.get("ok"):
            ok_count += 1
            if sample is None:
                sample = (bib, res)
            best = res.get("best")
            if best and best["km"] >= old_km:
                km = best["km"]
                # [안전장치] 짧은 시간에 비정상적으로 멀리 점프하면 오탐으로 보고 무시.
                # 허용치는 '마지막 변화 후 흐른 시간'에 비례해 커지므로 영원히 멈추지 않음.
                hours = max(0.0, now - rec["km_epoch"]) / 3600
                allowed = 10 + hours * 25
                if old_km > 0 and km - old_km > allowed:
                    print(f"[ALERT] {bib} 비정상 점프 {old_km}→{km} (허용 {allowed:.1f}km) 무시")
                else:
                    if km > old_km:
                        rec["km_epoch"] = now
                    rec.update(km=km, started=True, point=best["point"],
                               pass_time=best["pass_time"],
                               net_time=best["net_time"] or rec["net_time"],
                               start_time=best.get("start_time") or rec["start_time"],
                               splits=best.get("splits") or rec["splits"])
        else:
            errors.append(bib)
            print(f"[WARN] {bib} 읽기 실패: {res.get('error')}")

        rec["km"] = round(rec["km"], 4)
        out.append(rec)

    kst_now = datetime.now(KST).strftime("%H:%M:%S")
    payload = {
        "event_id": EVENT_ID,
        # 한 명이라도 성공했을 때만 '갱신 시각'을 바꿈 (사이트 장애 시 시각 동결)
        "updated_at": kst_now if ok_count > 0 else prev.get("updated_at", "--:--:--"),
        "updated_epoch": int(now) if ok_count > 0 else prev.get("updated_epoch", 0),
        "checked_at": kst_now,
        "ok_count": ok_count,
        "total": len(runners),
        "errors": errors,
        "runners": out,
    }

    os.makedirs(os.path.dirname(DATA_PATH) or ".", exist_ok=True)
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, DATA_PATH)  # 반쯤 쓰인 파일이 올라가는 일 방지

    if DEBUG_SAMPLE_PATH and sample:
        bib, res = sample
        lines = html_to_lines(res.get("raw", ""))
        with open(DEBUG_SAMPLE_PATH, "w", encoding="utf-8") as f:
            f.write(f"checked_at={kst_now} bib={bib} source={res.get('source')}\n")
            f.write(f"parsed={json.dumps(res.get('best'), ensure_ascii=False)}\n")
            f.write("-" * 40 + "\n")
            f.write("\n".join(lines[:250]))

    print(f"[DONE] {kst_now} 성공 {ok_count}/{len(runners)}, 실패 {len(errors)}")


if __name__ == "__main__":
    main()
