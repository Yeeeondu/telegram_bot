"""지수 이동평균선 알림 (매일).

python src/ma_alert.py --kr   # 코스피·코스닥 (장 마감 뒤 16:23 KST)
python src/ma_alert.py --us   # 나스닥·S&P500·다우 (미국 장 마감 뒤 06:53 KST)

잡는 것 (그날 일봉 기준)
- 이평선끼리: 골든크로스·데드크로스, 간격이 기준(코스피·코스닥 5%, 미국 3%) 안으로 새로 좁혀짐, 1% 안으로 새로 좁혀짐
- 일봉과 이평선: 종가 돌파·종가 이탈, 장중 돌파했지만 종가는 밑(밀림), 장중 이탈했지만 종가는 위(회복),
  고가가 이평선 바로 아래(1%/0.7% 이내)까지 왔다가 음봉 마감(근접 뒤 하락), 저가가 바로 위까지 왔다가 양봉 마감(근접 뒤 반등)
그날 해당 내용이 없는 지수는 빼고, 전부 없으면 보내지 않음.
"""
import re
import sys

import requests

from common import UA, load_config, load_state, save_state, notify_error_once, send_message, html_escape

DEFAULT = {
    "kr": [{"name": "코스피", "yahoo": "^KS11", "naver": "KOSPI"},
           {"name": "코스닥", "yahoo": "^KQ11", "naver": "KOSDAQ"}],
    "us": [{"name": "나스닥", "yahoo": "^IXIC"}, {"name": "S&P500", "yahoo": "^GSPC"}, {"name": "다우", "yahoo": "^DJI"}],
}
RULES = {"kr": {"mas": [20, 60, 120, 240], "near_ma": 5.0, "near_candle": 1.0},
         "us": {"mas": [20, 50, 100, 200], "near_ma": 3.0, "near_candle": 0.7}}


def yahoo(sym):
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                     params={"range": "2y", "interval": "1d"}, headers={"User-Agent": UA}, timeout=30)
    res = r.json()["chart"]["result"][0]
    q, off = res["indicators"]["quote"][0], res["meta"].get("gmtoffset", 0)
    import datetime as dt
    out = []
    for i, t in enumerate(res["timestamp"]):
        if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]):
            continue
        d = dt.datetime.utcfromtimestamp(t + off).date().isoformat()
        out.append({"d": d, "o": q["open"][i], "h": q["high"][i], "l": q["low"][i], "c": q["close"][i]})
    return out


def naver(sym):
    """네이버 금융 일봉 (한국 지수는 야후가 하루 늦게 올라오는 경우가 있어 먼저 씀)."""
    r = requests.get("https://fchart.stock.naver.com/sise.nhn",
                     params={"symbol": sym, "timeframe": "day", "count": 600, "requestType": 0},
                     headers={"User-Agent": UA}, timeout=30)
    out = []
    for d, o, h, lo, c, _ in re.findall(r'data="(\d{8})\|([\d.]+)\|([\d.]+)\|([\d.]+)\|([\d.]+)\|(\d*)"', r.text):
        out.append({"d": f"{d[:4]}-{d[4:6]}-{d[6:]}", "o": float(o), "h": float(h), "l": float(lo), "c": float(c)})
    return out


def bars(ix):
    srcs = []
    if ix.get("naver"):
        try:
            srcs.append(naver(ix["naver"]))
        except Exception as e:  # noqa: BLE001
            print(f"{ix['name']} 네이버 실패: {e}", file=sys.stderr)
    try:
        srcs.append(yahoo(ix["yahoo"]))
    except Exception as e:  # noqa: BLE001
        print(f"{ix['name']} 야후 실패: {e}", file=sys.stderr)
    srcs = [s for s in srcs if len(s) >= 260]
    if not srcs:
        raise RuntimeError(f"{ix['name']} 시세를 못 받음")
    return max(srcs, key=lambda s: s[-1]["d"])  # 가장 최근 날짜까지 있는 쪽


def sma(a, i, n):
    return None if i + 1 < n else sum(x["c"] for x in a[i + 1 - n:i + 1]) / n


def fmt(v):
    return f"{v:,.2f}"


def events(a, rule, i=-1):
    i = len(a) + i if i < 0 else i
    x, p = a[i], a[i - 1]
    ns, out = rule["mas"], []
    m = {n: sma(a, i, n) for n in ns}
    pm = {n: sma(a, i - 1, n) for n in ns}
    ct = rule["near_candle"] / 100
    for n in ns:
        if m[n] is None or pm[n] is None:
            continue
        v = fmt(m[n])
        if p["c"] < pm[n] and x["c"] >= m[n]:
            out.append(f"🔺 종가가 {n}일선({v}) 돌파")
        elif p["c"] > pm[n] and x["c"] <= m[n]:
            out.append(f"🔻 종가가 {n}일선({v}) 이탈")
        elif x["c"] < m[n] <= x["h"]:
            out.append(f"↘️ 장중 {n}일선({v}) 돌파했지만 종가는 아래 (고가 {fmt(x['h'])})")
        elif x["c"] < m[n] and x["h"] >= m[n] * (1 - ct) and x["c"] < x["o"]:
            out.append(f"↘️ {n}일선({v}) 근접 뒤 하락 마감 (고가 {fmt(x['h'])}, 선까지 {100 * (m[n] / x['h'] - 1):.1f}%)")
        elif x["c"] > m[n] >= x["l"]:
            out.append(f"↗️ 장중 {n}일선({v}) 이탈했지만 종가는 위 (저가 {fmt(x['l'])})")
        elif x["c"] > m[n] and x["l"] <= m[n] * (1 + ct) and x["c"] > x["o"]:
            out.append(f"↗️ {n}일선({v}) 근접 뒤 반등 마감 (저가 {fmt(x['l'])}, 선까지 {100 * (x['l'] / m[n] - 1):.1f}%)")
    thr = rule["near_ma"] / 100
    for k, s in enumerate(ns):
        for lng in ns[k + 1:]:
            if None in (m[s], m[lng], pm[s], pm[lng]):
                continue
            g, pg = m[s] / m[lng] - 1, pm[s] / pm[lng] - 1
            if pg < 0 <= g:
                out.append(f"✨ 골든크로스: {s}일선이 {lng}일선 위로")
            elif pg > 0 >= g:
                out.append(f"💀 데드크로스: {s}일선이 {lng}일선 아래로")
            elif abs(pg) > 0.01 >= abs(g):
                out.append(f"🎯 {s}일선–{lng}일선 간격 {100 * g:+.1f}% (1% 안으로 근접)")
            elif abs(pg) > thr >= abs(g):
                out.append(f"📐 {s}일선–{lng}일선 간격 {100 * g:+.1f}% ({rule['near_ma']:g}% 안으로 근접)")
    return out


def main():
    mkt = "kr" if "--kr" in sys.argv else "us"
    cfg = load_config().get("ma_alert", {})
    indices = cfg.get(mkt) or DEFAULT[mkt]
    rule = RULES[mkt] | (cfg.get("rules", {}).get(mkt) or {})
    st = load_state("ma_alert")
    blocks, day = [], None
    fails = []
    for ix in indices:
        try:
            a = bars(ix)
        except Exception as e:  # noqa: BLE001
            fails.append(str(e))
            continue
        last = a[-1]["d"]
        if st.get(ix["name"]) == last and "--force" not in sys.argv:
            print(f"{ix['name']} {last} 이미 확인함")
            continue
        st[ix["name"]] = last
        day = day or last
        ev = events(a, rule)
        chg = 100 * (a[-1]["c"] / a[-2]["c"] - 1)
        print(f"{ix['name']} {last}: {len(ev)}건")
        if ev:
            blocks.append(f"<b>{html_escape(ix['name'])}</b> {fmt(a[-1]['c'])} ({chg:+.2f}%)\n" + "\n".join(f"• {e}" for e in ev))
    save_state("ma_alert", st)
    if fails and len(fails) == len(indices):
        notify_error_once(f"이평선 알림({mkt})", " / ".join(fails))
    if blocks:
        mas = "·".join(str(n) for n in rule["mas"])
        head = f"📏 <b>{'국내' if mkt == 'kr' else '미국'} 지수 이평선</b> ({day[5:].replace('-', '/')}, {mas}일선)"
        send_message(head + "\n\n" + "\n\n".join(blocks))


if __name__ == "__main__":
    main()
