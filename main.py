"""
IVMS Pro - Servidor de Streaming
FastAPI server para receber RTMP e servir HLS
"""

import os
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from typing import Optional
import aiofiles
import aiofiles.os

from stream_manager import StreamManager

# Configuração
HLS_DIR = Path("/tmp/hls")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

# Gerenciador de streams
stream_manager = StreamManager(hls_dir=HLS_DIR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle do app - setup e cleanup"""
    # Startup
    HLS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"📁 HLS directory: {HLS_DIR}")
    print(f"🌐 CORS origins: {ALLOWED_ORIGINS}")
    yield
    # Shutdown
    await stream_manager.stop_all()
    print("🛑 All streams stopped")


app = FastAPI(
    title="IVMS Pro Streaming Server",
    description="Servidor de streaming para IVMS Pro",
    version="1.0.0",
    lifespan=lifespan
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============ Models ============

class StreamCreate(BaseModel):
    stream_key: str
    source_url: str  # URL RTSP ou RTMP de origem
    name: Optional[str] = None


class StreamInfo(BaseModel):
    stream_key: str
    name: Optional[str]
    source_url: str
    status: str
    hls_url: str


# ============ Endpoints ============

@app.get("/")
async def root():
    """Página inicial"""
    return {
        "service": "IVMS Pro Streaming Server",
        "status": "running",
        "endpoints": {
            "streams": "/streams",
            "hls": "/hls/{stream_key}.m3u8",
            "health": "/health"
        }
    }


@app.get("/health")
async def health():
    """Health check"""
    return {
        "status": "healthy",
        "active_streams": len(stream_manager.streams)
    }


@app.post("/streams", response_model=StreamInfo)
async def create_stream(stream: StreamCreate, background_tasks: BackgroundTasks):
    """
    Registra e inicia uma nova stream.
    
    O servidor irá:
    1. Conectar na source_url (RTSP/RTMP da câmera)
    2. Converter para HLS usando FFmpeg
    3. Servir os arquivos HLS
    """
    if stream.stream_key in stream_manager.streams:
        raise HTTPException(status_code=400, detail="Stream já existe")
    
    # Inicia conversão em background
    background_tasks.add_task(
        stream_manager.start_stream,
        stream.stream_key,
        stream.source_url,
        stream.name
    )
    
    base_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "localhost:8080")
    protocol = "https" if "railway" in base_url else "http"
    
    return StreamInfo(
        stream_key=stream.stream_key,
        name=stream.name,
        source_url=stream.source_url,
        status="starting",
        hls_url=f"{protocol}://{base_url}/hls/{stream.stream_key}.m3u8"
    )


@app.get("/streams")
async def list_streams():
    """Lista todas as streams ativas"""
    base_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "localhost:8080")
    protocol = "https" if "railway" in base_url else "http"
    
    streams = []
    for key, info in stream_manager.streams.items():
        streams.append({
            "stream_key": key,
            "name": info.get("name"),
            "status": info.get("status", "unknown"),
            "hls_url": f"{protocol}://{base_url}/hls/{key}.m3u8"
        })
    return {"streams": streams}


@app.delete("/streams/{stream_key}")
async def delete_stream(stream_key: str):
    """Para e remove uma stream"""
    if stream_key not in stream_manager.streams:
        raise HTTPException(status_code=404, detail="Stream não encontrada")
    
    await stream_manager.stop_stream(stream_key)
    return {"message": "Stream removida", "stream_key": stream_key}


@app.get("/hls/{stream_key}.m3u8")
async def get_playlist(stream_key: str):
    """Retorna a playlist HLS (.m3u8)"""
    playlist_path = HLS_DIR / stream_key / "index.m3u8"
    
    if not playlist_path.exists():
        raise HTTPException(status_code=404, detail="Stream não encontrada ou ainda iniciando")
    
    async with aiofiles.open(playlist_path, mode='r') as f:
        content = await f.read()
    
    return Response(
        content=content,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Access-Control-Allow-Origin": "*"
        }
    )


@app.get("/hls/{stream_key}/{segment}")
async def get_segment(stream_key: str, segment: str):
    """Retorna um segmento de vídeo (.ts)"""
    segment_path = HLS_DIR / stream_key / segment
    
    if not segment_path.exists():
        raise HTTPException(status_code=404, detail="Segmento não encontrado")
    
    return FileResponse(
        segment_path,
        media_type="video/mp2t",
        headers={
            "Cache-Control": "max-age=3600",
            "Access-Control-Allow-Origin": "*"
        }
    )


# ============ Para desenvolvimento local ============

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
