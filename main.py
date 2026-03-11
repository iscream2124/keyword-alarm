import asyncio
import json
import feedparser
import httpx
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import os

app = FastAPI()

# 모니터링할 키워드 목록
KEYWORDS = ["아이스크림미디어", "개인정보 유출"]
POLL_INTERVAL = 60  # 초

# 연결된 WebSocket 클라이언트들
clients: list[WebSocket] = []

# 이미 본 기사 ID 추적 (중복 방지)
seen_ids: set[str] = set()


def get_rss_url(keyword: str) -> str:
    from urllib.parse import quote
    return f"https://news.google.com/rss/search?q={quote(keyword)}&hl=ko&gl=KR&ceid=KR:ko"


async def fetch_news(keyword: str) -> list[dict]:
    """Google News RSS에서 기사 가져오기"""
    url = get_rss_url(keyword)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, follow_redirects=True)
            feed = feedparser.parse(resp.text)
            articles = []
            for entry in feed.entries:
                article_id = entry.get("id", entry.get("link", ""))
                if article_id not in seen_ids:
                    seen_ids.add(article_id)
                    articles.append({
                        "keyword": keyword,
                        "title": entry.get("title", ""),
                        "link": entry.get("link", ""),
                        "source": entry.get("source", {}).get("title", ""),
                        "published": entry.get("published", ""),
                        "summary": entry.get("summary", "")[:200],
                        "timestamp": datetime.now().isoformat(),
                    })
            return articles
    except Exception as e:
        print(f"[ERROR] {keyword}: {e}")
        return []


async def broadcast(message: dict):
    """모든 연결된 클라이언트에 메시지 전송"""
    dead = []
    for ws in clients:
        try:
            await ws.send_text(json.dumps(message, ensure_ascii=False))
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.remove(ws)


async def poll_loop():
    """백그라운드 폴링 루프"""
    print("[POLLER] 시작")
    # 초기 로딩: 각 키워드 최신 기사 5개씩 seen에 등록 (중복 방지)
    for keyword in KEYWORDS:
        articles = await fetch_news(keyword)
        print(f"[INIT] {keyword}: {len(articles)}개 초기화")

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        print(f"[POLL] {datetime.now().strftime('%H:%M:%S')} 체크 중...")
        for keyword in KEYWORDS:
            new_articles = await fetch_news(keyword)
            for article in new_articles:
                print(f"[NEW] {keyword}: {article['title'][:50]}")
                await broadcast({"type": "article", "data": article})


@app.on_event("startup")
async def startup():
    asyncio.create_task(poll_loop())


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.append(ws)
    print(f"[WS] 클라이언트 연결 ({len(clients)}명)")
    # 연결 시 현재 키워드 목록 전송
    await ws.send_text(json.dumps({
        "type": "init",
        "keywords": KEYWORDS,
        "poll_interval": POLL_INTERVAL,
    }, ensure_ascii=False))
    try:
        while True:
            await ws.receive_text()  # keep alive
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
