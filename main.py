import asyncio
import json
import feedparser
import httpx
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

app = FastAPI()

KEYWORDS = ["아이스크림미디어", "개인정보 유출"]
POLL_INTERVAL = 60  # 초

clients: list[WebSocket] = []
seen_ids: set[str] = set()

# 초기 1일치 기사 캐시 (새 클라이언트 접속 시 전송용)
cached_articles: list[dict] = []


def get_rss_url(keyword: str) -> str:
    from urllib.parse import quote
    return f"https://news.google.com/rss/search?q={quote(keyword)}&hl=ko&gl=KR&ceid=KR:ko"


def parse_published(entry) -> datetime:
    try:
        return parsedate_to_datetime(entry.get("published", "")).astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


async def fetch_news(keyword: str, since: datetime | None = None) -> list[dict]:
    url = get_rss_url(keyword)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, follow_redirects=True)
            feed = feedparser.parse(resp.text)
            articles = []
            for entry in feed.entries:
                article_id = entry.get("id", entry.get("link", ""))
                pub = parse_published(entry)

                # since 기준 필터 (초기 로딩용)
                if since and pub < since:
                    continue

                if article_id not in seen_ids:
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

    # 초기: 1일치 기사 전부 수집
    for keyword in KEYWORDS:
        articles = await fetch_news(keyword, since=since_1d)
        # 최신순 정렬
        articles.sort(key=lambda a: a["published"], reverse=True)
        cached_articles.extend(articles)
        print(f"[INIT] {keyword}: {len(articles)}개 로딩")

    # cached_articles 전체 최신순 정렬
    cached_articles.sort(key=lambda a: a["published"], reverse=True)
    print(f"[INIT] 총 {len(cached_articles)}개 캐시 완료")

    # 이후 폴링
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

    # 초기 메시지: 키워드 + 1일치 기사 한 번에 전송
    await ws.send_text(json.dumps({
        "type": "init",
        "keywords": KEYWORDS,
        "poll_interval": POLL_INTERVAL,
        "articles": cached_articles[:100],  # 최대 100개
    }, ensure_ascii=False))

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        clients.remove(ws)
        print(f"[WS] 클라이언트 해제 ({len(clients)}명)")


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
