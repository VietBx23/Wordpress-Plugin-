
# QNote Auto Import Backend
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import httpx
from bs4 import BeautifulSoup
import asyncio
import os
import json
import re
from urllib.parse import urljoin
import random
import socket
import logging
import unicodedata
import uuid

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("qnote")

app = FastAPI(title="QNote Auto Import Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory job store for background tasks
jobs: Dict[str, Dict[str, Any]] = {}

class Chapter(BaseModel):
    title: str
    content: str
    source: str

class BookResult(BaseModel):
    id: str
    title: str
    description: str
    category: str
    source_book: str
    chapters: List[Chapter]

class CrawlRequest(BaseModel):
    num_books: int = 2
    short: bool = False

def sanitize_filename(name: str, max_len: int = 200) -> str:
    name = unicodedata.normalize('NFKD', name)
    name = name.replace('/', ' ').replace('\\', ' ')
    name = re.sub(r'[<>:\"|?*]', '', name)
    name = name.strip()
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name or 'untitled'

async def fetch_text(client: httpx.AsyncClient, url: str, timeout: float = 15.0) -> Optional[str]:
    headers = {
        'User-Agent': os.environ.get('QNOTE_USER_AGENT', 'qnote-crawler/1.0'),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    }
    try:
        r = await client.get(url, timeout=timeout, headers=headers)
        if r.status_code == 200:
            return r.text
        logger.debug('fetch_text %s returned status %s', url, r.status_code)
    except Exception as exc:
        logger.debug('fetch_text exception for %s: %s', url, exc)
    return None

def clean_html(html: str) -> str:
    soup = BeautifulSoup(html or '', 'html.parser')
    for img in soup.find_all('img'):
        img.decompose()
    for a in soup.find_all('a'):
        a.replace_with(a.get_text())
    return str(soup)

def extract_book_ids_from_homepage(html: str, base: str) -> List[str]:
    soup = BeautifulSoup(html or '', 'html.parser')
    ids = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if not href:
            continue
        try:
            href_abs = href if href.startswith('http') else urljoin(base, href)
        except Exception:
            href_abs = href
        m = re.search(r'/detail/(\d+)', href_abs)
        if m:
            ids.append(m.group(1))
    # preserve unique order
    seen = set()
    out = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out

async def crawl_single_book(client: httpx.AsyncClient, book_id: str, hard_cap: int = 10) -> Optional[Dict[str, Any]]:
    detail_url = f'https://qnote.qq.com/detail/{book_id}'
    detail = await fetch_text(client, detail_url)
    if not detail:
        return None
    dsoup = BeautifulSoup(detail, 'html.parser')
    title_tag = dsoup.find(['h1', 'h2'])
    title = title_tag.get_text().strip() if title_tag else f'Book {book_id}'
    desc = ''
    for sel in ('.intro', '.detail_intro', '.book-intro', '.summary'):
        node = dsoup.select_one(sel)
        if node and node.get_text(strip=True):
            desc = clean_html(str(node))
            break
    if not desc:
        meta = dsoup.select_one('meta[name="description"]')
        if meta and meta.get('content'):
            desc = meta.get('content')
    if not desc:
        desc = '<p>Chưa có mô tả</p>'

    breadcrumb = dsoup.select_one('.breadcrumb a:nth-of-type(2)')
    raw_cat = breadcrumb.get_text().strip() if breadcrumb else ''
    category = raw_cat or 'Unknown'

    chapters = []
    consecutive_misses = 0
    for i in range(1, hard_cap + 1):
        ch_url = f'https://qnote.qq.com/read/{book_id}/{i}'
        ch_body = await fetch_text(client, ch_url)
        if not ch_body:
            consecutive_misses += 1
            if consecutive_misses >= 3:
                break
            continue
        consecutive_misses = 0
        csoup = BeautifulSoup(ch_body, 'html.parser')
        ch_title = (csoup.find('h1').get_text().strip() if csoup.find('h1') else f'Chương {i}')
        ch_html = ''
        for sel in ('.content', '.chapter', '.read-content', '#content', '.article'):
            nodes = csoup.select(sel)
            if nodes:
                ch_html = ''.join(str(n) for n in nodes)
                break
        if not ch_html:
            p_nodes = csoup.find_all('p')
            if p_nodes:
                ch_html = ''.join(str(p) for p in p_nodes)
        if not ch_html:
            ch_html = '<p>Chưa có nội dung</p>'
        chapters.append({'title': ch_title, 'content': clean_html(ch_html), 'source': ch_url})

    src = f'https://qnote.qq.com/read/{book_id}/1' if chapters else detail_url
    return {'id': book_id, 'title': title, 'description': desc, 'category': category, 'source_book': src, 'chapters': chapters}

async def crawl_books(req: CrawlRequest) -> List[Dict[str, Any]]:
    # choose homepage depending on short vs long
    if getattr(req, 'short', False):
        homepage = 'https://qnote.qq.com/cate/30125'
    else:
        homepage = 'https://qnote.qq.com/'
    results: List[Dict[str, Any]] = []
    async with httpx.AsyncClient() as client:
        body = await fetch_text(client, homepage)
        if not body:
            raise HTTPException(status_code=502, detail='failed_to_fetch_homepage')
        book_ids = extract_book_ids_from_homepage(body, homepage)
        random.shuffle(book_ids)
        book_ids = book_ids[: max(1, min(len(book_ids), req.num_books))]
        for bid in book_ids:
            try:
                # choose chapter fetch limit: short mode -> fetch up to 5 chapters; long mode -> fetch many (200)
                cap = 5 if getattr(req, 'short', False) else 200
                book = await crawl_single_book(client, bid, hard_cap=cap)
                if not book:
                    continue
                # short mode: we fetch up to `cap` chapters per book (cap set above)
                results.append(book)
            except Exception:
                logger.exception('crawl error for %s', bid)
                continue
    return results

@app.post('/api/crawl', response_model=List[BookResult])
async def api_crawl(req: CrawlRequest):
    books = await crawl_books(req)
    return JSONResponse(content=books)


@app.get('/api/crawl_stream')
async def api_crawl_stream(num_books: int = 2, short: int = 0):
    async def event_generator():
        # Gửi event 'ready'
        yield 'data: ' + json.dumps({'type': 'ready'}) + '\n\n'
        req = CrawlRequest(num_books=num_books, short=bool(short))
        books = await crawl_books(req)
        for book in books:
            yield 'data: ' + json.dumps({'type': 'book', 'book': book}) + '\n\n'
        yield 'data: ' + json.dumps({'type': 'done'}) + '\n\n'
    return StreamingResponse(event_generator(), media_type='text/event-stream')

@app.get('/')
async def root():
    return {'status': 'ok', 'service': 'QNote Auto Import Backend'}

def is_port_free(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.close()
        return True
    except Exception:
        return False

if __name__ == '__main__':
    import uvicorn
    host = os.environ.get('QNOTE_HOST', '127.0.0.1')
    start_port = int(os.environ.get('QNOTE_PORT', '8000'))
    max_tries = int(os.environ.get('QNOTE_PORT_TRIES', '20'))
    chosen = None
    for p in range(start_port, start_port + max_tries):
        if is_port_free(host, p):
            chosen = p
            break
    if chosen is None:
        print(f'No free port in range {start_port}-{start_port+max_tries-1}. Set QNOTE_PORT to a free port.')
        raise SystemExit(1)
    print(f'Starting QNote backend on http://{host}:{chosen} (use CTRL+C to stop)')
    uvicorn.run(app, host=host, port=chosen, reload=False)

