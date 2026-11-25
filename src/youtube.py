"""유튜브 영상 요약 → 텔레그램.

블로그 '롤라팔루자' 정리 글과 같은 형식으로 요약해 보낸다.
  [제목] → 핵심 발언 인용 → 한 문단 개요 → TL;DR → 핵심 요약(줄 단위) → 영상 링크

python src/youtube.py                       # 환경변수 YT_URL 의 영상 하나
python src/youtube.py https://youtu.be/...  # 직접 지정
python src/youtube.py --weekly              # 매주 토요일 아침 (금요일 밤까지 1주일): 블로거 6명이 관심 가질 이번 주 영상 5편 안팎을 골라 요약
"""
import glob
import html
import json
import os
import re
import sys
import tempfile

import requests

import datetime as dt
import time

from common import UA, html_escape, load_config, load_state, save_state, notify_error_once, send_message
from news import ask_claude_json, send_long, llm_available

LANGS = ["en", "en-US", "en-GB", "ko"]


def video_id(url):
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})", url)
    if not m:
        raise ValueError(f"영상 주소를 못 읽음: {url}")
    return m.group(1)


# ---------- 자막 가져오기 (여러 방법을 차례로 시도) ----------
def via_supadata(vid):
    """Supadata 자막 서비스 (GitHub 서버는 유튜브가 직접 막아서 이걸 먼저 씀). 무료 월 100건."""
    key = os.environ.get("SUPADATA_API_KEY")
    if not key:
        raise RuntimeError("SUPADATA_API_KEY 없음")
    h = {"x-api-key": key}
    r = requests.get("https://api.supadata.ai/v1/transcript", headers=h, timeout=120,
                     params={"url": f"https://www.youtube.com/watch?v={vid}", "text": "true", "mode": "native"})
    if r.status_code == 202 or (r.ok and "jobId" in r.json()):   # 긴 영상은 작업 번호를 주고 나중에 결과
        job = r.json()["jobId"]
        for _ in range(60):
            time.sleep(10)
            r = requests.get(f"https://api.supadata.ai/v1/transcript/{job}", headers=h, timeout=60)
            j = r.json()
            if j.get("status") in ("completed", None) and j.get("content"):
                break
            if j.get("status") == "failed":
                raise RuntimeError(f"Supadata 작업 실패: {str(j)[:200]}")
    if not r.ok:
        raise RuntimeError(f"Supadata HTTP {r.status_code}: {r.text[:200]}")
    c = r.json().get("content")
    if isinstance(c, list):
        c = " ".join(x.get("text", "") for x in c)
    return c or ""



def via_transcript_api(vid):
    from youtube_transcript_api import YouTubeTranscriptApi
    if hasattr(YouTubeTranscriptApi, "get_transcript"):          # 0.6.x
        rows = YouTubeTranscriptApi.get_transcript(vid, languages=LANGS)
        return " ".join(r["text"] for r in rows)
    rows = YouTubeTranscriptApi().fetch(vid, languages=LANGS)    # 1.x
    return " ".join(s.text for s in rows)


def via_ytdlp(vid):
    import yt_dlp
    tmp = tempfile.mkdtemp()
    opts = {"skip_download": True, "writesubtitles": True, "writeautomaticsub": True,
            "subtitleslangs": LANGS, "subtitlesformat": "json3", "quiet": True,
            "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s")}
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=True)
    files = sorted(glob.glob(os.path.join(tmp, "*.json3")), key=lambda f: (".en" not in f, f))
    if not files:
        raise RuntimeError("yt-dlp: 자막 파일 없음")
    ev = json.load(open(files[0], encoding="utf-8")).get("events", [])
    return " ".join("".join(s.get("utf8", "") for s in e.get("segs", [])) for e in ev)


def transcript(vid):
    errs = []
    for name, fn in (("Supadata", via_supadata), ("youtube-transcript-api", via_transcript_api), ("yt-dlp", via_ytdlp)):
        try:
            text = re.sub(r"\s+", " ", fn(vid)).strip()
            if len(text) > 500:
                print(f"자막: {name}로 {len(text):,}자")
                return text
            errs.append(f"{name}: 너무 짧음({len(text)}자)")
        except Exception as e:  # noqa: BLE001
            errs.append(f"{name}: {type(e).__name__}: {str(e)[:200]}")
    raise RuntimeError("자막을 못 가져옴 — " + " / ".join(errs))


def meta(url):
    try:
        j = requests.get("https://www.youtube.com/oembed", params={"url": url, "format": "json"}, timeout=20).json()
        return j.get("title", ""), j.get("author_name", "")
    except Exception:  # noqa: BLE001
        return "", ""


# ---------- 요약 ----------
STYLE = """너는 투자 블로거처럼 영상(인터뷰·강연·팟캐스트) 내용을 한국어로 정리한다. 원문 전사문만 근거로 쓰고, 없는 숫자나 내용을 만들지 마라.
출력 JSON:
{
 "headline": "[화자 소속 또는 이름] 핵심 주제 (예: '[Altimeter] 현시점 AI 투자 전략')",
 "speaker": "화자 이름과 소속",
 "quotes": ["화자가 실제로 한 말을 한국어로 옮긴 인상적인 문장 8~10개 (따옴표 없이)"],
 "overview": "전체 내용을 3~4문장 한 문단으로. 주요 숫자 포함",
 "tldr": ["핵심 10개 안팎, 각 1문장, 숫자 그대로"],
 "details": ["영상 흐름 순서대로 핵심 내용을 30~45개 문장으로. 각 문장은 '~라고 본다/설명한다/말한다'처럼 화자의 주장임을 드러내고,
             전사문에서 기준·기간·출처가 불분명한 숫자는 '전사문에는 ~가 나오지 않는다'처럼 짚어라"]
}"""


def summarize(text, title, channel):
    if len(text) > 250000:     # 아주 긴 영상은 앞부분 위주로 (약 4~5시간 분량 이상)
        text = text[:250000]
    return ask_claude_json(f"{STYLE}\n\n영상 제목: {title}\n채널: {channel}\n\n[전사문]\n{text}", max_tokens=9000)


def render(s, url, title, channel):
    out = [f"🎬 <b>{html_escape(s.get('headline') or title)}</b>",
           f"<i>{html_escape(title)} · {html_escape(channel)}</i>", ""]
    for q in s.get("quotes") or []:
        out.append(f"“{html_escape(q)}”")
    if s.get("overview"):
        out += ["", html_escape(s["overview"])]
    if s.get("tldr"):
        out += ["", "<b>TL;DR</b>"] + [f"• {html_escape(x)}" for x in s["tldr"]]
    if s.get("details"):
        out += ["", "<b>핵심 요약</b>"] + [f"• {html_escape(x)}" for x in s["details"]]
    out += ["", f'<a href="{html_escape(url)}">영상 보기</a>']
    return "\n".join(out)


# ---------- 매주: 영상 고르기 ----------
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,ko;q=0.8"})
WEEK_FILTER = "EgIIAw%3D%3D"  # 유튜브 검색 '이번 주 업로드'


def _initial_data(page):
    i = page.find("ytInitialData")
    if i < 0:
        return {}
    j = page.find("{", i)
    try:
        return json.JSONDecoder().raw_decode(page[j:])[0]
    except ValueError:
        return {}


def _walk(o, key):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key:
                yield v
            else:
                yield from _walk(v, key)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v, key)


def _text(x):
    if not x:
        return ""
    if "simpleText" in x:
        return x["simpleText"]
    return "".join(r.get("text", "") for r in x.get("runs", []))


def _minutes(length):
    parts = [int(p) for p in re.findall(r"\d+", length or "")]
    m = 0
    for p in parts:
        m = m * 60 + p
    return m / 60 if parts else None


def search(q):
    page = S.get("https://www.youtube.com/results", params={"search_query": q, "sp": "EgIIAw=="}, timeout=30).text
    out = []
    for v in _walk(_initial_data(page), "videoRenderer"):
        out.append({"id": v.get("videoId"), "title": _text(v.get("title")), "channel": _text(v.get("ownerText")),
                    "minutes": _minutes(_text(v.get("lengthText"))), "when": _text(v.get("publishedTimeText")),
                    "views": _text(v.get("viewCountText")),
                    "desc": _text((v.get("detailedMetadataSnippets") or [{}])[0].get("snippetText"))[:200]})
    return [o for o in out if o["id"]][:20]


def channel_id(handle, cache):
    if cache.get(handle):
        return cache[handle]
    page = S.get(f"https://www.youtube.com/{handle}", timeout=30).text
    m = re.search(r'"(?:channelId|externalId)":"(UC[\w-]{22})"', page)
    if m:
        cache[handle] = m.group(1)  # 찾은 것만 저장 (못 찾으면 다음 주에 다시 시도)
    return m.group(1) if m else None


def channel_recent(cid, days=7):
    xml = S.get("https://www.youtube.com/feeds/videos.xml", params={"channel_id": cid}, timeout=30).text
    cutoff = (dt.datetime.utcnow() - dt.timedelta(days=days)).isoformat()
    out = []
    for e in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        vid = re.search(r"<yt:videoId>(.*?)</yt:videoId>", e)
        pub = re.search(r"<published>(.*?)</published>", e)
        if not vid or not pub or pub.group(1) < cutoff:
            continue
        title = re.search(r"<title>(.*?)</title>", e)
        name = re.search(r"<name>(.*?)</name>", e)
        desc = re.search(r"<media:description>(.*?)</media:description>", e, re.S)
        out.append({"id": vid.group(1), "title": html.unescape(title.group(1)) if title else "",
                    "channel": html.unescape(name.group(1)) if name else "", "minutes": None,
                    "when": pub.group(1)[:10], "views": "", "desc": html.unescape(desc.group(1))[:200] if desc else ""})
    return out


def profile_text():
    st = load_state("blogs")
    prof = st.get("profile") or load_config()["blogs"].get("profile") or {}
    keep = {k: prof.get(k) for k in ("summary", "shift", "by_blog", "themes", "next_topics") if prof.get(k)}
    return json.dumps(keep, ensure_ascii=False)[:12000]


def make_queries(prof, n=14):
    res = ask_claude_json(
        f"아래는 투자 블로거 6명의 관심사다.\n{prof}\n\n"
        f"이들이 보고 정리할 만한 유튜브 '긴 영상'(투자자·경영진·연구자 인터뷰, 강연, 팟캐스트)을 찾을 검색어 {n}개를 만들어라. "
        "영어 위주, 한국어 2~3개. 인물 이름·회사·주제를 구체적으로 (예: 'Altimeter AI investing lecture', "
        "'Druckenmiller interview', 'SemiAnalysis Dylan Patel podcast', 'humanoid robot supply chain interview'). "
        '형식: {"queries": ["..."]}', max_tokens=800, model=load_config().get("youtube", {}).get("query_model"))
    return [q for q in (res.get("queries") or []) if isinstance(q, str)][:n]


def pick(prof, cands, n):
    payload = [{"i": k, "title": c["title"], "channel": c["channel"], "minutes": c["minutes"],
                "when": c["when"], "views": c["views"], "desc": c["desc"]} for k, c in enumerate(cands)]
    ex = load_config()["blogs"].get("examples", [])
    res = ask_claude_json(
        f"아래는 투자 블로거 6명의 관심사다.\n{prof}\n\n이번 주 올라온 유튜브 영상 후보다. "
        f"이 블로거들이 정리해 볼 만한 영상을 좋은 순서로 {n + 3}개까지 골라라.\n"
        "고를 것: 핵심 인물(투자자·경영진·연구자·애널리스트)이 직접 오래 말하는 인터뷰·강연·팟캐스트, "
        "산업 구조·수요·공급·설비투자·기술 변화처럼 앞으로를 내다보는 내용. 블로거 관심사와 겹칠수록 좋음.\n"
        "목적: 사용자가 이 블로그들 없이도(블로거들이 글을 멈춰도) 이들이 관심 가질 만한 영상을 계속 보게 하는 것.\n"
        f"이 사용자가 '이런 걸 원한다'고 직접 보여준 예시: {json.dumps(ex, ensure_ascii=False)}\n"
        "빼는 것: 15분 미만 영상·쇼츠, 뉴스 클립 재편집, 리액션·요약 채널, 개별 종목 매수·매도 추천, "
        "단기 주가·지수 전망, 같은 인물·같은 주제의 중복(하나만).\n"
        '형식: {"picks": [{"i": 번호, "why": "어느 블로거 관심사와 맞는지 한 줄"}]}\n\n'
        + json.dumps(payload, ensure_ascii=False), max_tokens=1500)
    out = []
    for p in res.get("picks") or []:
        i = p.get("i")
        if isinstance(i, int) and 0 <= i < len(cands):
            out.append(cands[i] | {"why": p.get("why", "")})
    return out


def weekly():
    cfg = load_config().get("youtube", {})
    n = cfg.get("per_week", 5)
    st = load_state("youtube")
    sent = set(st.get("sent", []))
    cache = st.setdefault("channel_ids", {})
    prof = profile_text()
    cands = []
    for q in make_queries(prof) + cfg.get("extra_queries", []):
        try:
            cands += search(q)
        except Exception as e:  # noqa: BLE001
            print(f"검색 실패 {q}: {e}", file=sys.stderr)
        time.sleep(1)
    # 블로거들이 이미 인용한 영상은 st["sent"]에 들어 있어 제외됨. 그들이 즐겨 보는 채널(learned_handles)은 확인 대상에 더함
    for h in list(dict.fromkeys(cfg.get("channels", []) + st.get("learned_handles", []))):
        try:
            cid = channel_id(h, cache)
            if cid:
                cands += channel_recent(cid)
            else:
                print(f"채널 못 찾음: {h}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"채널 실패 {h}: {e}", file=sys.stderr)
        time.sleep(0.5)
    seen, uniq = set(), []
    for c in cands:
        if c["id"] in seen or c["id"] in sent:
            continue
        if c["minutes"] is not None and c["minutes"] < 15:
            continue
        seen.add(c["id"])
        uniq.append(c)
    print(f"후보 영상 {len(uniq)}개")
    if not uniq:
        notify_error_once("유튜브 주간", "후보 영상을 하나도 못 찾음 (유튜브 검색이 막혔을 수 있음)")
        return
    picks = pick(prof, uniq[:250], n)
    done, failed = [], []
    for c in picks:
        if len(done) >= n:
            break
        url = f"https://www.youtube.com/watch?v={c['id']}"
        try:
            s = summarize(transcript(c["id"]), c["title"], c["channel"])
            if not s or not s.get("details"):
                raise RuntimeError("요약 실패")
            done.append((c, render(s, url, c["title"], c["channel"])))
        except Exception as e:  # noqa: BLE001
            print(f"요약 실패 {c['id']}: {e}", file=sys.stderr)
            failed.append(c)
    show = [c for c, _ in done] + (failed[: max(0, n - len(done))] if len(done) < n else [])
    if not show:
        return
    head = [f"🎬 <b>이번 주 블로거 관심 영상</b> ({dt.date.today().strftime('%m/%d')})"]
    for k, c in enumerate(show, 1):
        tag = "" if any(c is d for d, _ in done) else " <i>(자막을 못 받아 요약 없음)</i>"
        head.append(f'{k}. <a href="https://www.youtube.com/watch?v={c["id"]}">{html_escape(c["title"][:90])}</a> '
                    f'· {html_escape(c["channel"])}{tag}\n   {html_escape(c.get("why", ""))}')
    send_message("\n".join(head), preview=False)
    for _, text in done:
        time.sleep(1.5)
        send_long(text)
    st["sent"] = sorted(sent | {c["id"] for c in show})[-2000:]
    save_state("youtube", st)


def main():
    if "--weekly" in sys.argv:
        if not llm_available():
            print("ANTHROPIC_API_KEY 없음")
            sys.exit(1)
        weekly()
        return
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    url = (args[0] if args else os.environ.get("YT_URL", "")).strip()
    if not url:
        print("영상 주소가 없음 (실행할 때 '영상 주소' 칸에 넣어 주세요)")
        sys.exit(1)
    if not llm_available():
        print("ANTHROPIC_API_KEY 없음")
        sys.exit(1)
    vid = video_id(url)
    url = f"https://www.youtube.com/watch?v={vid}"
    title, channel = meta(url)
    print(f"영상: {title} / {channel}")
    try:
        text = transcript(vid)
    except Exception as e:
        notify_error_once("유튜브 요약", e)
        raise
    s = summarize(text, title, channel)
    if not s or not s.get("details"):
        raise RuntimeError("요약 실패")
    send_long(render(s, url, title, channel))


if __name__ == "__main__":
    main()
