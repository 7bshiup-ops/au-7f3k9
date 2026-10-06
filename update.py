#!/usr/bin/env python3
"""Midas daily update. Deterministic: no AI, no opinions.

Rules were measured on 1,417 past sessions (GC futures May 2024-Oct 2026 and
PAX Gold spot 2021-2024). Setup is taken from hourly bars before 09:00 New York;
results are measured on the 09:00-12:00 New York bars (session 09:30-12:00).
"""
import json, os, statistics, sys, time, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36",
      "Accept": "application/json"}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")

# Tested tables (share of past sessions). Distance unit = typical session move (median of last 10 session high-low).
NEAR_BREAK = [(0.15, 78), (0.30, 65), (0.50, 48), (0.75, 32), (99, 22)]   # nearer extreme gets broken
FAR_HOLD = [(0.50, 60), (0.75, 76), (99, 91)]                              # farther extreme holds
INSIDE = [(0.15, 16), (0.30, 23), (0.50, 37), (0.75, 52), (99, 71)]       # both extremes hold
SAFE_K = 0.91        # safe level = farther of (far extreme, price -/+ 0.91 typical moves); held 90-92% out of sample
SAFE_PCT = 90
RANGE_LO, RANGE_TYP, RANGE_HI = 0.56, 0.94, 1.85   # session high-low vs typical move; 80% band held 78-80%


_S = None


def get(url, tries=4):
    """Fetch JSON like a normal Chrome browser (plain Python clients get HTTP 429 from Yahoo)."""
    global _S
    last = None
    for n in range(tries):
        try:
            try:
                from curl_cffi import requests as cr
                if _S is None:
                    _S = cr.Session(impersonate="chrome")
                    try:
                        _S.get("https://fc.yahoo.com", timeout=20)
                    except Exception:
                        pass
                r = _S.get(url, timeout=30)
                if r.status_code == 429:
                    raise RuntimeError("429")
                r.raise_for_status()
                return r.json()
            except ImportError:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
        except Exception as e:  # noqa
            last = e
            time.sleep(8 * (n + 1))
    raise last


def yahoo(sym, rng, interval):
    err = None
    for host in ("query1", "query2"):
        try:
            return get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval={interval}")["chart"]["result"][0]
        except Exception as e:  # noqa
            err = e
    raise err


def lookup(table, x):
    for lim, p in table:
        if x < lim:
            return p
    return table[-1][1]


def build_days(res):
    q = res["indicators"]["quote"][0]
    days = {}
    for i, t in enumerate(res["timestamp"]):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if o is None or c is None or h is None or l is None:
            continue
        d = datetime.fromtimestamp(t, NY)
        hr = d.hour
        key = (d.date() + timedelta(days=1)) if hr >= 18 else d.date()
        if key.weekday() >= 5:
            continue
        x = days.setdefault(key, {"pH": None, "pL": None, "sH": None, "sL": None})
        if hr >= 18 or hr < 9:
            x["pH"] = h if x["pH"] is None else max(x["pH"], h)
            x["pL"] = l if x["pL"] is None else min(x["pL"], l)
            x["p9"] = c
        if 9 <= hr < 12:
            x["sH"] = h if x["sH"] is None else max(x["sH"], h)
            x["sL"] = l if x["sL"] is None else min(x["sL"], l)
            if hr == 9:
                x["o9"] = o
            if hr == 11:
                x["c12"] = c
            x["last"] = c
        if hr == 16:
            x["c17"] = c
    return days


def setup(days, keys, i):
    """Plan for day keys[i] using only data known at 09:00 New York."""
    d = days[keys[i]]
    if d.get("p9") is None or d["pH"] is None:
        return None
    ranges = []
    for k in reversed(keys[:i]):
        z = days[k]
        if z.get("c12") is not None and z["sH"] is not None:
            ranges.append(z["sH"] - z["sL"])
        if len(ranges) == 10:
            break
    if len(ranges) < 6:
        return None
    atr = statistics.median(ranges)
    p9, pH, pL = d["p9"], d["pH"], d["pL"]
    dH, dL = (pH - p9) / atr, (p9 - pL) / atr
    near_up = dH <= dL
    d_near, d_far = min(dH, dL), max(dH, dL)
    if d_near < 0.30:
        mode = "up" if near_up else "down"
    elif d_near >= 0.50:
        mode = "range"
    else:
        mode = "mixed"
    safe = min(pL, p9 - SAFE_K * atr) if near_up else max(pH, p9 + SAFE_K * atr)
    return {
        "p9": round(p9, 1), "atr": round(atr, 1), "mode": mode, "nearUp": near_up,
        "high": round(pH, 1), "low": round(pL, 1),
        "nearProb": lookup(NEAR_BREAK, d_near), "farProb": lookup(FAR_HOLD, d_far),
        "insideProb": lookup(INSIDE, d_near),
        "safe": round(safe, 1), "safeProb": SAFE_PCT,
        "range": {"lo": round(RANGE_LO * atr), "typ": round(RANGE_TYP * atr), "hi": round(RANGE_HI * atr)},
    }


def grade(s, d):
    """Outcome of a finished session (needs the 09:00-12:00 bars)."""
    if d.get("c12") is None or d["sH"] is None:
        return None
    broke_high, broke_low = d["sH"] > s["high"], d["sL"] < s["low"]
    near_broke = broke_high if s["nearUp"] else broke_low
    far_held = (not broke_low) if s["nearUp"] else (not broke_high)
    safe_held = d["sL"] >= s["safe"] if s["nearUp"] else d["sH"] <= s["safe"]
    inside = not broke_high and not broke_low
    call_right = near_broke if s["mode"] in ("up", "down") else (inside if s["mode"] == "range" else None)
    return {"nearBroke": near_broke, "farHeld": far_held, "safeHeld": safe_held, "inside": inside,
            "callRight": call_right, "move": round(d["sH"] - d["sL"], 1)}


def news(today):
    try:
        ev = get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", tries=2)
    except Exception:
        return None
    out = []
    for e in ev:
        if e.get("country") != "USD" or e.get("impact") not in ("High", "Medium"):
            continue
        try:
            t = datetime.fromisoformat(e["date"]).astimezone(NY)
        except Exception:
            continue
        if t.date() != today:
            continue
        m = t.hour * 60 + t.minute
        if 9 * 60 + 30 < m < 12 * 60:
            out.append({"t": t.strftime("%H:%M"), "name": e.get("title", "")[:40], "impact": e["impact"].lower()})
    return out


def main():
    now = datetime.now(NY)
    res = yahoo("GC=F", "60d", "1h")
    days = build_days(res)
    keys = sorted(days)
    today = now.date()
    meta = res.get("meta", {})
    price = meta.get("regularMarketPrice")

    try:
        old = json.load(open(OUT))
    except Exception:
        old = {}

    # history: last 20 finished sessions, graded with the same fixed rules
    hist = []
    for i in range(len(keys)):
        if keys[i] >= today and not (keys[i] == today and now.hour >= 12):
            continue
        s = setup(days, keys, i)
        g = grade(s, days[keys[i]]) if s else None
        if s and g:
            hist.append({"date": keys[i].isoformat(), "mode": s["mode"], **g})
    hist = hist[-20:]
    called = [h for h in hist if h["callRight"] is not None]
    track = {"n": len(hist), "called": len(called), "callRight": sum(h["callRight"] for h in called),
             "farHeld": sum(h["farHeld"] for h in hist), "safeHeld": sum(h["safeHeld"] for h in hist)}

    plan, result = None, None
    if today in days and today.weekday() < 5 and now.hour >= 9:
        i = keys.index(today)
        plan = setup(days, keys, i)
        if plan and now.hour >= 12:
            result = grade(plan, days[today])

    prev_close = None
    for k in reversed(keys):
        if k < today and days[k].get("c17"):
            prev_close = days[k]["c17"]
            break
    chg = round((price / prev_close - 1) * 100, 2) if price and prev_close else None

    data = {
        "asOf": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session": today.isoformat() if plan else old.get("session"),
        "plan": plan if plan else old.get("plan"),
        "result": result,
        "price": round(price, 1) if price else None,
        "chgPct": chg,
        "events": news(today) if plan else old.get("events"),
        "track": track,
        "hist": hist,
        "tested": 1417,
    }
    if data["events"] is None:
        data["events"] = old.get("events", [])
    with open(OUT, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print(json.dumps({k: data[k] for k in ("session", "plan", "result", "price", "track")}, indent=1))


if __name__ == "__main__":
    sys.exit(main())
