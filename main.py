import asyncio
import json
import feedparser
import httpx
from datetime import datetime, timezone, timedelta, date
from email.utils import parsedate_to_datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI()

KEYWORDS = ["아이스크림미디어", "개인정보 유출"]
POLL_INTERVAL = 60

clients: list[WebSocket] = []
seen_ids: set[str] = set()
cached_articles: list[dict] = []


def get_rss_url(keyword: str, after: str = "", before: str = "") -> str:
    from urllib.parse import quote
    q = keyword
    if after:
        q += f" after:{after}"
    if before:
        q += f" before:{before}"
    return f"https://news.google.com/rss/search?q={quote(q)}&hl=ko&gl=KR&ceid=KR:ko"


def parse_published(entry) -> datetime:
    try:
        return parsedate_to_datetime(entry.get("published", "")).astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


async def fetch_news(keyword: str, since: datetime | None = None, until: datetime | None = None, bypass_seen: bool = False) -> list[dict]:
    # Google News after/before 파라미터로 서버사이드 필터
    after_str = since.strftime("%Y-%m-%d") if since else ""
    before_str = until.strftime("%Y-%m-%d") if until else ""
    url = get_rss_url(keyword, after=after_str, before=before_str)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, follow_redirects=True)
            feed = feedparser.parse(resp.text)
            articles = []
            for entry in feed.entries:
                article_id = entry.get("id", entry.get("link", ""))
                pub = parse_published(entry)

                # 클라이언트사이드 날짜 필터 (정밀도 보완)
                if since and pub < since:
                    continue
                if until and pub > until:
                    continue

                if not bypass_seen and article_id in seen_ids:
                    continue

                seen_ids.add(article_id)
                articles.append({
                    "keyword": keyword,
                    "title": entry.get("title", ""),
                    "link": entry.get("link", ""),
                    "source": entry.get("source", {}).get("title", ""),
                    "published": pub.isoformat(),
                    "summary": entry.get("summary", "")[:200],
                    "timestamp": pub.isoformat(),
                })
            return articles
    except Exception as e:
        print(f"[ERROR] {keyword}: {e}")
        return []


async def broadcast(message: dict):
    dead = []
    for ws in clients:
        try:
            await ws.send_text(json.dumps(message, ensure_ascii=False))
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.remove(ws)


async def poll_loop():
    print("[POLLER] 시작 - 1일치 초기 로딩")
    since_1d = datetime.now(timezone.utc) - timedelta(days=1)

    for keyword in KEYWORDS:
        articles = await fetch_news(keyword, since=since_1d)
        cached_articles.extend(articles)
        print(f"[INIT] {keyword}: {len(articles)}개 로딩")

    cached_articles.sort(key=lambda a: a["published"], reverse=True)
    print(f"[INIT] 총 {len(cached_articles)}개 캐시 완료")

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        print(f"[POLL] {datetime.now().strftime('%H:%M:%S')} 체크 중...")
        for keyword in KEYWORDS:
            new_articles = await fetch_news(keyword)
            for article in new_articles:
                print(f"[NEW] {keyword}: {article['title'][:50]}")
                cached_articles.insert(0, article)
                await broadcast({"type": "article", "data": article})


@app.on_event("startup")
async def startup():
    asyncio.create_task(poll_loop())


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.append(ws)
    print(f"[WS] 클라이언트 연결 ({len(clients)}명)")

    await ws.send_text(json.dumps({
        "type": "init",
        "keywords": KEYWORDS,
        "poll_interval": POLL_INTERVAL,
        "articles": cached_articles[:100],
    }, ensure_ascii=False))

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        clients.remove(ws)
        print(f"[WS] 클라이언트 해제 ({len(clients)}명)")


@app.get("/search")
async def search_by_date(
    date_from: str = Query(..., description="YYYY-MM-DD"),
    date_to: str = Query(..., description="YYYY-MM-DD"),
):
    """날짜 범위로 기사 검색"""
    try:
        since = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        until = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    except ValueError:
        return JSONResponse({"error": "날짜 형식 오류 (YYYY-MM-DD)"}, status_code=400)

    all_articles = []
    for keyword in KEYWORDS:
        articles = await fetch_news(keyword, since=since, until=until, bypass_seen=True)
        all_articles.extend(articles)

    all_articles.sort(key=lambda a: a["published"], reverse=True)
    print(f"[SEARCH] {date_from}~{date_to}: {len(all_articles)}개")
    return {"articles": all_articles, "count": len(all_articles)}


@app.get("/keywords")
def get_keywords():
    return {"keywords": KEYWORDS}


@app.post("/keywords/{keyword}")
def add_keyword(keyword: str):
    if keyword not in KEYWORDS:
        KEYWORDS.append(keyword)
    return {"keywords": KEYWORDS}


@app.delete("/keywords/{keyword}")
def remove_keyword(keyword: str):
    if keyword in KEYWORDS:
        KEYWORDS.remove(keyword)
    return {"keywords": KEYWORDS}


@app.get("/", response_class=HTMLResponse)
def index():
    with open("index.html", encoding="utf-8") as f:
        return f.read()
