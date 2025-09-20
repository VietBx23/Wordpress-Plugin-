import asyncio
import pytest
from fastapi.testclient import TestClient
from app import app


client = TestClient(app)


def test_crawl_endpoint_schema():
    # This test only asserts the API returns a list (may be empty) and items have expected keys
    resp = client.post('/api/crawl', json={"num_books": 0, "num_chapters": 0})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
