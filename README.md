# QNote Auto Import Backend

This is a small FastAPI backend that crawls qnote.qq.com and returns book + chapter data as JSON.

Quick start (Windows PowerShell):

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r requirements.txt; uvicorn app:app --reload --port 8000
```

Endpoint:
- `POST /api/crawl` with JSON body `{ "num_books": 2, "num_chapters": 5 }` returns an array of books.
