import uuid
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import List, Optional, Dict
import httpx
from bs4 import BeautifulSoup
import asyncio
from fastapi.middleware.cors import CORSMiddleware
import re
import random
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Chuangshi Auto Import Job Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

JOB_STORE: Dict[str, dict] = {}

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
    num_books: int
    num_chapters: int
    crawl_mode: Optional[str] = "short"  # "short" or "long"

def clean_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all('img'):
        img.decompose()
    for a in soup.find_all('a'):
        a.replace_with(a.get_text())
    return str(soup)

async def fetch_text(client: httpx.AsyncClient, url: str) -> Optional[str]:
    try:
        r = await client.get(url, timeout=30.0, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            return r.text
    except Exception as exc:
        logger.info("fetch_text error for %s: %s", url, exc)
    return None

async def crawl_chapter(client, book_id, i):
    ch_url = f'https://chuangshi.qq.com/read/{book_id}/{i}'
    ch_body = await fetch_text(client, ch_url)
    if not ch_body:
        return None
    csoup = BeautifulSoup(ch_body, 'html.parser')
    # Tiêu đề chương
    ch_title_tag = csoup.find('h3', class_='chapter-title') or csoup.find('h1')
    ch_title = ch_title_tag.get_text().strip() if ch_title_tag else f'Chương {i}'
    # Nội dung chương
    selectors = ['.read-content', '.chapter-content', '#chapterContent', '.content', '.article']
    ch_html = None
    for sel in selectors:
        nodes = csoup.select(sel)
        if nodes:
            ch_html = ''.join(str(n) for n in nodes)
            break
    if not ch_html:
        p_nodes = csoup.select('body p') or csoup.find_all('p')
        if p_nodes:
            ch_html = ''.join(str(p) for p in p_nodes)
    if not ch_html:
        texts = [t.strip() for t in csoup.get_text(separator='\n').split('\n') if t.strip()]
        if texts:
            ch_html = '<p>' + '</p><p>'.join(texts[:50]) + '</p>'
    ch_content = clean_html(ch_html) if ch_html else '<p>Chưa có nội dung</p>'
    return Chapter(title=ch_title, content=ch_content, source=ch_url)

async def crawl_single_book(client, book_id, num_chapters, crawl_mode):
    detail_url = f'https://chuangshi.qq.com/detail/{book_id}'
    detail_body = await fetch_text(client, detail_url)
    if not detail_body:
        logger.info("Không lấy được detail của book_id %s", book_id)
        return None

    first_ch_url = f'https://chuangshi.qq.com/read/{book_id}/1'
    first_ch_body = await fetch_text(client, first_ch_url)
    source_book = first_ch_url if first_ch_body else detail_url

    dsoup = BeautifulSoup(detail_body, 'html.parser')
    # Tiêu đề truyện
    title_tag = dsoup.find('h1', class_='book-title') or dsoup.find('h1')
    title = title_tag.get_text().strip() if title_tag else f'Book {book_id}'

    # Mô tả truyện
    desc_nodes = dsoup.select('.book-intro, .intro, .detail_intro')
    if desc_nodes:
        desc_html = ''.join(str(n) for n in desc_nodes)
        description = clean_html(desc_html)
    else:
        description = '<p>Chưa có mô tả</p>'

    # Thể loại/breadcrumb
    breadcrumbs = dsoup.select('.crumbs a, .breadcrumb a')
    category_list = [a.get_text().strip() for a in breadcrumbs if a.get_text().strip()]
    category = " > ".join(category_list) if category_list else 'Unknown'

    tasks = [crawl_chapter(client, book_id, i) for i in range(1, num_chapters + 1)]
    chapters = await asyncio.gather(*tasks)
    chapters = [ch for ch in chapters if ch]

    # Truyện ngắn: gộp mô tả + tất cả chương vào content
    if crawl_mode == "short":
        full_content = description
        for ch in chapters:
            full_content += "\n\n<h3>" + ch.title + "</h3>\n" + ch.content
        # Trả về một BookResult chỉ có mô tả là gộp, chapters = []
        return BookResult(
            id=book_id,
            title=title,
            description=full_content,
            category=category,
            source_book=source_book,
            chapters=[]
        )

    # Truyện dài: tách chương riêng như cũ
    return BookResult(
        id=book_id,
        title=title,
        description=description,
        category=category,
        source_book=source_book,
        chapters=chapters
    )

async def crawl_books_job(job_id: str, req: CrawlRequest):
    JOB_STORE[job_id]['status'] = 'running'
    JOB_STORE[job_id]['progress'] = 0
    try:
        # Truyện ngắn: crawl ở cate/20076_1, truyện dài: crawl ở trang chủ
        if req.crawl_mode == "short":
            homepage = 'https://chuangshi.qq.com/cate/20076_1'
            req.num_chapters = min(req.num_chapters, 5)
        else:
            homepage = 'https://chuangshi.qq.com/'
            req.num_chapters = min(req.num_chapters, 30)

        async with httpx.AsyncClient() as client:
            body = await fetch_text(client, homepage)
            if not body:
                JOB_STORE[job_id]['status'] = 'error'
                JOB_STORE[job_id]['error'] = 'Failed to fetch homepage'
                return

            soup = BeautifulSoup(body, 'html.parser')
            links = set()
            # Lấy link truyện từ các thẻ a
            for a in soup.find_all('a', href=True):
                href = a['href']
                # Đúng định dạng truyện detail
                if re.search(r'/detail/\d+', href):
                    if href.startswith('http'):
                        links.add(href)
                    else:
                        try:
                            links.add(str(httpx.URL(homepage).join(href)))
                        except Exception:
                            links.add(href)
            book_ids = []
            for href in links:
                m = re.search(r'/detail/(\d+)', str(href))
                if m:
                    book_ids.append(m.group(1))
            book_ids = list(dict.fromkeys(book_ids))
            random.shuffle(book_ids)
            book_ids = book_ids[: req.num_books]

            JOB_STORE[job_id]['progress'] = 10
            tasks = [crawl_single_book(client, book_id, req.num_chapters, req.crawl_mode) for book_id in book_ids]
            results = await asyncio.gather(*tasks)
            results = [bk for bk in results if bk]
            JOB_STORE[job_id]['result'] = [bk.dict() for bk in results]
            JOB_STORE[job_id]['status'] = 'done'
            JOB_STORE[job_id]['progress'] = 100
    except Exception as e:
        JOB_STORE[job_id]['status'] = 'error'
        JOB_STORE[job_id]['error'] = str(e)

@app.post('/api/crawl_start')
async def api_crawl_start(req: CrawlRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    JOB_STORE[job_id] = {'status': 'pending', 'progress': 0, 'result': None}
    background_tasks.add_task(crawl_books_job, job_id, req)
    return {"job_id": job_id}

@app.get('/api/crawl_status')
def api_crawl_status(job_id: str):
    job = JOB_STORE.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"status": job['status'], "progress": job['progress'], "error": job.get('error', '')}

@app.get('/api/crawl_result', response_model=List[BookResult])
def api_crawl_result(job_id: str):
    job = JOB_STORE.get(job_id)
    if not job or job['status'] != 'done':
        raise HTTPException(status_code=404, detail="Job not found or not done")
    return job['result']

@app.get('/', tags=['root'])
def root():
    return {"status": "ok", "service": "Chuangshi Auto Import Job Backend"}
