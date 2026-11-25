"""블로거 관심 기사 고르기 (blog_collect.py 가 매주 일요일 밤에 사용).

글을 쉰 블로거의 관심사 검색어로 지난 7일 기사를 모은 뒤,
그 블로거가 다음 글을 준비하며 읽었을 법한 기사만 골라 제목·링크로 보냄.
"""
import collections
import datetime as dt
import json
import re
import sys

from common import load_config, html_escape
from news import ask_claude_json, dedupe, google_news, link, llm_available, send_long


def queries_for(blog_ids, ai_prof, cfg_prof):
    """해당 블로거들의 검색어: AI가 갱신한 것(블로그 표시가 있는 것) 우선, 없으면 config 기본값."""
    qs = [q for q in (ai_prof or {}).get("queries", []) if q.get("blog") in blog_ids]
    if not qs:
        qs = [q for q in (cfg_prof or {}).get("queries", []) if q.get("blog") in blog_ids]
    return qs


def send_for_silent(silent, ai_prof, cfg_prof, n=15):
    qs = queries_for(set(silent), ai_prof, cfg_prof)
    if not qs:
        print(f"검색어 없음: {silent}", file=sys.stderr)
        return
    cands = []
    for q in qs:
        try:
            for i in google_news(q["q"], q.get("lang", "ko"), hours=24 * 7)[:10]:
                i["q"] = q["q"]
                cands.append(i)
        except Exception as e:  # noqa: BLE001
            print(f"{q}: {e}", file=sys.stderr)
    cands = dedupe(cands)
    if not cands:
        return

    picks = None
    if llm_available():
        info = {"블로거별 관심사": {b: (ai_prof or {}).get("by_blog", {}).get(b) for b in silent},
                "전체 요약": (cfg_prof or {}).get("summary", ""),
                "예상 주제": (ai_prof or {}).get("next_topics") or (cfg_prof or {}).get("next_topics")}
        lst = [{"id": k, "title": c["title"], "source": c["source"]} for k, c in enumerate(cands[:400])]
        prompt = (f"이번 주 글을 올리지 않은 투자 블로거({', '.join(silent)})의 관심사: {json.dumps(info, ensure_ascii=False)}\n\n"
                  f"이 블로거들이 계속 글을 썼다면 읽고 참고했을 지난 7일 기사 {n}개를 골라라.\n"
                  "선별 기준:\n"
                  "1) 이들은 AI·반도체·에너지·금리·유가 같은 거시 흐름을 다룬다. 업황 전체를 가늠할 수 있는 기사만 고른다.\n"
                  "   - 포함 예: 하이퍼스케일러 투자 계획, HBM4·메모리 가격, 소프트뱅크 가스터빈 주문 취소, 탱커 운임 하락, 연준 금리 결정, 유가·OPEC\n"
                  "   - 포함 예: '휴머노이드 로봇 ETF 출시'처럼 개별 기업 소식이어도 산업 전체의 흐름을 보여 주는 것\n"
                  "   - 포함 예: '머스크가 SpaceXAI 내부 채팅방 이름을 \"경쟁 못 하니 컴퓨팅을 팔자\"로 바꿨다'(NYT 보도)처럼 "
                  "AI 업계 큰 회사의 전략 전환·내부 판단을 보여 주는 기사\n"
                  f"   - 사용자가 직접 보여준 원하는 예시: {json.dumps(load_config()['blogs'].get('examples', []), ensure_ascii=False)}\n"
                  "   - 제외 예: 'LS일렉트릭 1300억 전력설비 공급', '삼현, 휴머노이드 액추에이터 공급 임박…내년 로봇 매출 200억 전망'처럼 "
                  "업황 전체를 보기엔 작은 개별 기업의 수주·실적·매출 전망 기사 (증권사 개별 종목 리포트 포함)\n"
                  "2) 주가·지수 전망 기사는 제외 (예: '코스피 3900까지 떨어질 것', 목표주가, 증시 전망, 애널리스트 매수 추천).\n"
                  "3) 같은 사건·같은 보고서·같은 인물의 같은 주제는 하나만 (예: SemiAnalysis capex 하향을 다룬 기사 여러 개 → 1개, "
                  "케빈 워시 관련 기사 여러 개 → 가장 중요한 1개). 각 기사에 topic(사건을 짧게)을 붙여라.\n"
                  "4) 새 전개·선행 신호·파생 이슈를 우선, 광고·단순 시세 기사는 제외. 중요도 순. 기준에 맞는 게 적으면 15개보다 적어도 된다.\n"
                  + json.dumps(lst, ensure_ascii=False) + '\n출력: {"picks": [{"id": 번호, "topic": "사건 요약 몇 단어"}]}')
        try:
            res = ask_claude_json(prompt, max_tokens=1200)
            seen_topics, picks = set(), []
            for x in res.get("picks", []):
                i, topic = x.get("id"), re.sub(r"\W+", "", str(x.get("topic", "")).lower())
                if not isinstance(i, int) or not 0 <= i < len(cands) or (topic and topic in seen_topics):
                    continue
                seen_topics.add(topic)
                picks.append(cands[i])
            picks = picks[:n]
        except Exception as e:  # noqa: BLE001
            print(f"AI 선별 실패: {e}", file=sys.stderr)
    if not picks:  # 키가 없거나 실패: 검색어마다 최신 기사를 돌아가며
        by_q = collections.defaultdict(list)
        for c in sorted(cands, key=lambda c: -(c["date"].timestamp() if c["date"] else 0)):
            by_q[c["q"]].append(c)
        picks, rnd = [], 0
        while len(picks) < n and any(len(v) > rnd for v in by_q.values()):
            for q in qs:
                lst = by_q.get(q["q"], [])
                if len(lst) > rnd and len(picks) < n:
                    picks.append(lst[rnd])
            rnd += 1

    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%m/%d")
    lines = [f"📚 <b>이번 주 쉰 블로거 대신 고른 기사</b> ({today})",
             f"<i>{html_escape(', '.join(silent))}</i>"]
    for k, c in enumerate(picks, 1):
        lines.append(f"{k}. {link(c['title'][:110], c['link'])}")
    send_long("\n".join(lines))
