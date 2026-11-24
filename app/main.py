import asyncio
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import os

from app.config import settings
from app.models import SearchRequest, SearchResponse
from app.orchestrator import Orchestrator

import logging

logger = logging.getLogger("stealth-price-chat")

orchestrator: Optional[Orchestrator] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global orchestrator
    orchestrator = Orchestrator()
    await browser_pool.start()
    yield
    if orchestrator:
        await orchestrator.close()
    await browser_pool.stop()


from app.browser_pool import browser_pool
from typing import Optional

app = FastAPI(title="stealth-price-chat", version="3.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    return FileResponse(os.path.join(static_dir, "index.html"))


@app.post("/api/search", response_model=SearchResponse)
async def search(request: SearchRequest):
    if not request.query or not request.query.strip():
        raise HTTPException(status_code=400, detail="Query must not be empty")
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")
    return await orchestrator.search(request)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)