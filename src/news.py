"""구글 뉴스 RSS, 일반 RSS 수집과 Claude API 호출 공통 함수."""
import datetime as dt
import email.utils
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests

from common import UA, html_escape, send_message


def _parse_date(s):
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:  # noqa: BLE001
        pass
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _strip(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def fetch_rss(url, timeout=30):
    """RSS/Atom → [{title, link, date(datetime|None), desc, source}]"""
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        src = it.find("source")
        out.append({
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "date": _parse_date(it.findtext("pubDate")),
            "desc": _strip(it.findtext("description"))[:400],
            "source": (src.text if src is not None else "") or "",
        })
    ns = "{http://www.w3.org/2005/Atom}"
    for it in root.iter(f"{ns}entry"):
        link = it.find(f"{ns}link")
        out.append({
            "title": (it.findtext(f"{ns}title") or "").strip(),
            "link": link.get("href") if link is not None else "",
            "date": _parse_date(it.findtext(f"{ns}updated") or it.findtext(f"{ns}published")),
            "desc": _strip(it.findtext(f"{ns}summary") or it.findtext(f"{ns}content"))[:400],
            "source": "",
        })
    return out


def google_news(query, lang="en", hours=26):
    """구글 뉴스 검색 RSS. 최근 hours 시간 이내 기사만 (hours=None이면 기간 제한 없이)."""
    if lang == "ko":
        hl, gl, ceid = "ko", "KR", "KR:ko"
    elif lang == "ja":
        hl, gl, ceid = "ja", "JP", "JP:ja"
    else:
        hl, gl, ceid = "en-US", "US", "US:en"
    q = urllib.parse.quote(f"{query} when:{max(1, round(hours / 24))}d" if hours else query)
    url = f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
    items = fetch_rss(url)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours) if hours else None
    res = []
    for i in items:
        if cutoff and i["date"] and i["date"] < cutoff:
            continue
        # 제목 끝의 ' - 매체명' 분리
        m = re.match(r"^(.*)\s+-\s+([^-]+)$", i["title"])
        if m and not i["source"]:
            i["title"], i["source"] = m.group(1).strip(), m.group(2).strip()
        elif m and m.group(2).strip() == i["source"].strip():
            i["title"] = m.group(1).strip()
        res.append(i)
    return res


def dedupe(items):
    seen, out = set(), []
    for i in items:
        k = re.sub(r"\W+", "", i["title"].lower())[:80]
        if k and k not in seen:
            seen.add(k)
            out.append(i)
    return out


# ---------- Claude API ----------
def llm_available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


USAGE = {}  # 모델별 사용 토큰 {model: [입력, 출력]}


def ask_claude(prompt, system="", max_tokens=3000, model=None):
    model = model or os.environ.get("CLAUDE_MODEL") or "claude-sonnet-5-5"
    body = {"model": model, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}]}
    if system:
        body["system"] = system
    for attempt in range(3):
        r = requests.post("https://api.anthropic.com/v1/messages", json=body, timeout=180, headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        })
        if r.ok:
            j = r.json()
            u = USAGE.setdefault(model, [0, 0])
            u[0] += j.get("usage", {}).get("input_tokens", 0)
            u[1] += j.get("usage", {}).get("output_tokens", 0)
            return "".join(b.get("text", "") for b in j["content"])
        print(f"claude error {r.status_code}: {r.text[:300]}", file=sys.stderr)
        if r.status_code in (429, 500, 502, 503, 529):
            time.sleep(20 * (attempt + 1))
            continue
        r.raise_for_status()
    r.raise_for_status()


def _escape_inner_quotes(raw):
    """문자열 안에 그대로 들어간 큰따옴표를 \\" 로 고침.
    문자열을 닫는 따옴표 뒤에는 , : } ] 중 하나가 와야 한다는 점으로 구분."""
    out, in_str, i, n = [], False, 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "\\" and in_str:
            out.append(raw[i:i + 2])
            i += 2
            continue
        if c == '"':
            if not in_str:
                in_str = True
            else:
                j = i + 1
                while j < n and raw[j] in " \t\r\n":
                    j += 1
                if j >= n or raw[j] in ",:}]":
                    in_str = False
                else:
                    out.append('\\"')
                    i += 1
                    continue
        out.append(c)
        i += 1
    return "".join(out)


def _parse_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M)
    m = re.search(r"[\[{].*[\]}]", text, re.S)
    raw = m.group(0) if m else text
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 흔한 형식 실수(끝 쉼표, 줄바꿈 문자, 문자열 안 큰따옴표) 보정 후 재시도
        fixed = re.sub(r",\s*([\]}])", r"\1", raw).replace("\n", " ")
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            return json.loads(_escape_inner_quotes(fixed))


def ask_claude_json(prompt, system="", max_tokens=3000, model=None):
    """JSON으로 답받기. 형식이 깨지면 최대 2번 다시 요청."""
    ask = prompt + "\n\n반드시 올바른 JSON만 출력하세요. 코드블록 표시 없이. 문자열 안의 큰따옴표는 \\\" 로 쓰세요."
    last = None
    for attempt in range(3):
        text = ask_claude(ask, system, max_tokens, model=model)
        try:
            return _parse_json(text)
        except json.JSONDecodeError as e:
            last = e
            print(f"JSON 형식 오류({attempt + 1}회): {e} — 받은 글 앞부분: {text[:300]!r}", file=sys.stderr)
            ask = (prompt + "\n\n앞선 답이 JSON 형식 오류였습니다. 이번에는 문법이 올바른 JSON만, 설명 없이 출력하세요. "
                   "문자열 안에 큰따옴표를 쓰지 말고 작은따옴표나 「」를 쓰세요.")
    raise last


# ---------- 긴 메시지 나눠 보내기 ----------
def send_long(text, silent=False, limit=3800):
    """긴 메시지를 줄 단위로 나눠 보냄 (HTML 태그가 중간에 잘리지 않도록)."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            chunks.append(cur.rstrip("\n"))
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur.rstrip("\n"))
    for c in chunks:
        send_message(c, silent=silent, limit=None)
        time.sleep(1)


def link(title, url):
    return f'<a href="{html_escape(url)}">{html_escape(title)}</a>'
