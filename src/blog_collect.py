"""매주 일요일 밤 — 블로거 글 학습 + 쉰 블로거 대신 기사 보내기.

1) 블로거들의 새 글(제목·본문)을 state/blog_posts.json 에 저장 (처음엔 전체 글 목록까지)
2) API 키가 있으면 관심사 변화를 분석해 블로거별 검색어를 갱신 (state/blogs.json)
3) 지난 7일 동안 글을 하나도 안 올린 블로거가 있으면, 그 블로거가 봤을 법한 기사 15개를 링크로 보냄
   (모두 글을 올렸으면 아무것도 보내지 않음)
"""
import datetime as dt
import html as html_lib
import json
import re
import sys
import time
import urllib.parse

import requests

from blogs import send_for_silent
from common import UA, load_config, load_state, save_state
from news import _strip, ask_claude_json, fetch_rss, llm_available

H = {"User-Agent": UA, "Referer": "https://blog.naver.com/"}


def norm_date(s, now=None):
    """'2026. 9. 6.' / '7시간 전' / '15분 전' / '어제' / ISO → 'YYYY-MM-DD'. 모르면 ''."""
    now = now or dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
    s = (s or "").strip()
    m = re.match(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.match(r"(\d+)\s*(분|시간|일)\s*전", s)
    if m:
        unit = {"분": "minutes", "시간": "hours", "일": "days"}[m.group(2)]
        return (now - dt.timedelta(**{unit: int(m.group(1))})).date().isoformat()
    if s.startswith("어제"):
        return (now - dt.timedelta(days=1)).date().isoformat()
    if "방금" in s:
        return now.date().isoformat()
    return ""


def title_list(bid, max_pages=40):
    """블로그 전체 글 제목 목록 (네이버 글 목록 API). 실패하면 빈 목록."""
    out = []
    for page in range(1, max_pages + 1):
        url = (f"https://blog.naver.com/PostTitleListAsync.naver?blogId={bid}&viewdate=&currentPage={page}"
               "&categoryNo=0&parentCategoryNo=&countPerPage=30")
        try:
            r = requests.get(url, headers=H, timeout=30)
            r.raise_for_status()
            data = json.loads(r.text.replace("\\'", "'"))
        except Exception as e:  # noqa: BLE001
            print(f"{bid} 목록 {page}쪽 실패: {e}", file=sys.stderr)
            break
        posts = data.get("postList") or []
        for p in posts:
            out.append({"logNo": str(p.get("logNo")),
                        "title": urllib.parse.unquote_plus(p.get("title", "")).strip(),
                        "date": norm_date(p.get("addDate", ""))})
        if len(posts) < 30:
            break
        time.sleep(0.5)
    return out


SKIP_HOST = re.compile(r"(naver\.(com|net)|pstatic\.net|blog\.me|navercorp|kakao|coupang|"
                       r"googletagmanager|google-analytics|doubleclick|facebook\.com/sharer|w3\.org|schema\.org)", re.I)
SKIP_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|css|js|ico|woff2?)(\?|$)", re.I)


def norm_url(u):
    u = html_lib.unescape(u).replace("\\/", "/").strip()
    m = re.search(r"(?:youtube\.com/(?:embed/|watch\?v=|shorts/|live/)|youtu\.be/)([\w-]{11})", u)
    if m:
        return f"https://www.youtube.com/watch?v={m.group(1)}"
    return re.sub(r"[?&](utm_[^&]+|si=[^&]+|fbclid=[^&]+)", "", u).rstrip("?&")


def extract_links(page):
    """본문에 나온 외부 링크(기사·자료·유튜브). 네이버 내부·이미지는 뺌."""
    raw = re.findall(r'(?:href|src)="(https?://[^"]+)"', page)
    raw += re.findall(r'(?:inputUrl|link|url)(?:"|&quot;|\\")?\s*:\s*(?:"|&quot;|\\")(https?:[^"&\\]+)', page)
    raw += [f"https://www.youtube.com/watch?v={v}" for v in
            re.findall(r"(?:youtube\.com/(?:embed/|watch\?v=|shorts/|live/)|youtu\.be/)([\w-]{11})", page)]
    out = []
    for u in raw:
        u = norm_url(u)
        host = re.sub(r"^https?://", "", u).split("/")[0]
        if SKIP_HOST.search(host) or SKIP_EXT.search(u) or len(u) < 15:
            continue
        if u not in out:
            out.append(u)
    return out[:30]


def post_body(bid, log_no, limit=4000, with_links=False):
    try:
        r = requests.get(f"https://blog.naver.com/PostView.naver?blogId={bid}&logNo={log_no}",
                         headers=H, timeout=30)
        r.raise_for_status()
        html = r.text
        m = re.search(r'<div class="se-main-container">(.*?)</div>\s*</div>\s*</div>\s*<!--', html, re.S) \
            or re.search(r'<div class="se-main-container">(.*)', html, re.S) \
            or re.search(r'id="postViewArea"(.*)', html, re.S)
        part = m.group(1) if m else html
        text = _strip(re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", part, flags=re.S))[:limit]
        return (text, extract_links(part)) if with_links else text
    except Exception as e:  # noqa: BLE001
        print(f"{bid}/{log_no} 본문 실패: {e}", file=sys.stderr)
        return ("", []) if with_links else ""


def log_no_from_link(link):
    m = re.search(r"/(\d{9,})", link or "")
    return m.group(1) if m else ""


def collect(ids, db, fetch_bodies=40):
    added = 0
    for bid in ids:
        posts = db.setdefault(bid, {})
        if not posts:  # 처음: 전체 제목 목록
            for p in title_list(bid):
                posts.setdefault(p["logNo"], {"title": p["title"], "date": p["date"], "body": ""})
        try:
            rss = fetch_rss(f"https://rss.blog.naver.com/{bid}.xml")
        except Exception as e:  # noqa: BLE001
            print(f"{bid} RSS 실패: {e}", file=sys.stderr)
            rss = []
        n_body = 0
        for it in rss:
            ln = log_no_from_link(it["link"])
            if not ln:
                continue
            rec = posts.get(ln)
            if rec is None:
                rec = posts[ln] = {"title": it["title"], "date": it["date"].astimezone(dt.timezone(dt.timedelta(hours=9))).date().isoformat() if it["date"] else "",
                                   "body": ""}
                rec["new"] = dt.date.today().isoformat()
                added += 1
            if (not rec.get("body") or "links" not in rec) and n_body < fetch_bodies:
                body, links = post_body(bid, ln, with_links=True)
                rec["body"] = rec.get("body") or body or it["desc"]
                rec["links"] = links
                n_body += 1
                time.sleep(0.5)
    return added


# ───────── 블로거가 글에 인용한 기사·영상 ─────────
def link_title(url):
    """(제목, 출처) — 유튜브는 oEmbed, 나머지는 페이지 제목."""
    try:
        if "youtube.com/watch" in url:
            j = requests.get("https://www.youtube.com/oembed", params={"url": url, "format": "json"}, timeout=15).json()
            return j.get("title", ""), j.get("author_name", ""), j.get("author_url", "")
        r = requests.get(url, headers=H, timeout=15)
        page = r.text[:300000]
        m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', page) or \
            re.search(r"<title[^>]*>(.*?)</title>", page, re.S)
        site = re.search(r'<meta[^>]+property="og:site_name"[^>]+content="([^"]+)"', page)
        host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        return (html_lib.unescape(m.group(1)).strip()[:150] if m else ""), (site.group(1) if site else host), ""
    except Exception:  # noqa: BLE001
        return "", re.sub(r"^https?://(www\.)?", "", url).split("/")[0], ""


def cited_sources(db, days=7):
    """최근 days일 새 글에 나온 외부 링크 → [{url, title, source, blog, post}]"""
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out, seen = [], set()
    for bid, posts in db.items():
        for ln, p in posts.items():
            if not (p.get("date", "") >= since or p.get("new", "") >= since):
                continue
            for u in p.get("links") or []:
                if u in seen:
                    continue
                seen.add(u)
                out.append({"url": u, "blog": bid, "post": p.get("title", ""), "ln": ln})
    return out


def learn_sources(db, st, yt_st):
    """블로거들이 이번 주 글에 인용한 기사·영상을 '학습'에만 씀 (메시지로 보내지 않음 — 블로그는 사용자가 직접 읽음).
    - 인용 자료 제목은 관심사 학습(즐겨 보는 매체·채널·인물)에 씀
    - 인용 영상의 채널은 유튜브 주간 선별에서 새 영상을 확인할 채널 목록에 더함
    - 이미 인용된 영상·링크는 사용자가 블로그에서 볼 테니 다시 고르지 않게 기록"""
    done = set(st.get("learned_links", []))
    items = [x for x in cited_sources(db) if x["url"] not in done]
    for x in items[:60]:
        title, src, author_url = link_title(x["url"])
        db[x["blog"]][x["ln"]].setdefault("link_titles", {})[x["url"]] = f"{title or x['url']} ({src})"
        if "youtube.com/watch" in x["url"]:
            h = re.search(r"youtube\.com/(@[\w.-]+)", author_url or "")
            learned = yt_st.setdefault("learned_handles", [])
            if h and h.group(1) not in learned:
                learned.append(h.group(1))
            skip = yt_st.setdefault("sent", [])
            if x["url"][-11:] not in skip:
                skip.append(x["url"][-11:])
        time.sleep(0.3)
    st["learned_links"] = sorted(done | {x["url"] for x in items[:60]})[-3000:]
    print(f"블로거 인용 자료 {len(items[:60])}개 학습")


def recent_posts(db, days=8, min_per_blog=6, max_per_blog=15):
    """블로거마다: 최근 days일 글, 적어도 최신 min_per_blog개 (글 번호가 클수록 최신)."""
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out = {}
    for bid, posts in db.items():
        items = sorted(posts.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0, reverse=True)
        pick = [p for k, (_, p) in enumerate(items)
                if k < min_per_blog or p.get("date", "") >= since or p.get("new", "") >= since][:max_per_blog]
        out[bid] = [{"title": p["title"], "date": p.get("date", ""), "body": p.get("body", "")[:1000],
                     "cited": list((p.get("link_titles") or {}).values())[:5]} for p in pick]
    return out


def update_profile(db, prof_state):
    """API 키가 있을 때: 블로거별 최근 글 + 기존 관심사로 '앞으로 다룰 주제' 검색어 갱신."""
    recent = recent_posts(db)
    cfg_prof = load_config()["blogs"].get("profile", {})
    base = {k: cfg_prof.get(k) for k in ("summary", "next_topics") if cfg_prof.get(k)}
    prev = (prof_state.get("profile") or {})
    prev = {k: prev.get(k) for k in ("shift", "themes") if prev.get(k)}
    n = len(load_config()["blogs"]["ids"])
    prompt = (f"내가 즐겨 읽는 투자 블로거 {n}명(블로그 ID별)의 관심사 정리와 최근 글이다.\n"
              f"[기본 관심사 — 블로그 전체 글을 읽고 정리한 것]\n{json.dumps(base, ensure_ascii=False)}\n\n"
              f"[지난주 분석]\n{json.dumps(prev, ensure_ascii=False)}\n\n"
              f"[블로거별 최근 글]\n{json.dumps(recent, ensure_ascii=False)[:60000]}\n\n"
              "각 글의 cited는 블로거가 글에 인용한 기사·영상 제목이다. 이들이 어떤 출처(매체·유튜브 채널·인물)를 즐겨 보는지도 반영하라.\n"
              "1) 블로거별로 관심사가 어디로 이동하고 있는지 정리하고, "
              "2) 이들이 '앞으로' 쓸 글에 담길 내용을 예측해, 그 글을 쓰기 전에 읽을 법한 기사를 찾는 구글 뉴스 검색어를 만들어라. "
              f"반드시 {n}명 블로거 모두를 고르게 다뤄라(블로거마다 5~7개, 총 {n * 5}~{n * 7}개). "
              "이미 쓴 글의 주제를 반복하기보다 다음 단계·파생 이슈·선행 지표 위주로. "
              "검색어는 1~4단어, 한국어(lang=ko)와 영어(lang=en)를 섞어라.\n"
              '출력: {"shift": "전체 흐름 2~3문장", "by_blog": {"블로그ID": "그 블로거의 관심 이동 1~2문장"}, '
              '"themes": ["..."], "next_topics": ["..."], "favorite_sources": ["즐겨 인용하는 매체·채널·인물"], '
              '"queries": [{"q": "...", "lang": "ko", "blog": "블로그ID"}]}')
    return ask_claude_json(prompt, max_tokens=4000)


def posted_recently(posts, days=7):
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    return any(p.get("date", "") >= since or p.get("new", "") >= since for p in posts.values())


def main():
    cfg = load_config()["blogs"]
    db = load_state("blog_posts")
    for posts in db.values():  # 예전에 저장된 날짜 형식도 정리
        for p in posts.values():
            p["date"] = norm_date(p.get("date", "")) or p.get("date", "")
    added = collect(cfg["ids"], db)
    # 예전에 모은 최근 3주 글은 링크가 없어서 한 번 더 읽음 (한 번에 최대 60개)
    since = (dt.date.today() - dt.timedelta(days=21)).isoformat()
    n = 0
    for bid in cfg["ids"]:
        for ln, p in (db.get(bid) or {}).items():
            if n < 60 and "links" not in p and p.get("date", "") >= since:
                _, p["links"] = post_body(bid, ln, with_links=True)
                n += 1
                time.sleep(0.5)
    save_state("blog_posts", db)
    print(f"새 글 {added}개, 총 {sum(len(v) for v in db.values())}개 저장")

    st = load_state("blogs")
    yt_st = load_state("youtube")
    try:
        learn_sources(db, st, yt_st)
        save_state("blogs", st)
        save_state("youtube", yt_st)
        save_state("blog_posts", db)
    except Exception as e:  # noqa: BLE001
        print(f"인용 자료 학습 실패: {e}", file=sys.stderr)

    if llm_available() and "--no-llm" not in sys.argv:
        try:
            prof = update_profile(db, st)
            prof["updated"] = dt.date.today().isoformat()
            st["profile"] = prof
            save_state("blogs", st)
        except Exception as e:  # noqa: BLE001
            print(f"관심사 갱신 실패: {e}", file=sys.stderr)

    silent = [b for b in cfg["ids"] if db.get(b) and not posted_recently(db[b])]
    print(f"이번 주 글이 없는 블로거: {silent or '없음'}")
    if silent or "--force-send" in sys.argv:
        send_for_silent(silent or cfg["ids"], st.get("profile"), cfg.get("profile"), cfg.get("max_articles", 15))


if __name__ == "__main__":
    main()
