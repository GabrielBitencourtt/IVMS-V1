"""
IVMS Pro - Servidor de Streaming
FastAPI server para receber RTMP e servir HLS
"""

import os
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
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

# CORS - Permitir todas as origens para streaming
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Permitir qualquer origem
    allow_credentials=False,  # Não usar credentials com wildcard
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,  # Cache preflight por 24h
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


@app.get("/debug/hls")
async def debug_hls_dir():
    """Lista todos os diretórios e arquivos HLS - útil para debug"""
    import os as os_module
    import time
    
    result = {
        "hls_base_dir": str(HLS_DIR),
        "hls_dir_exists": HLS_DIR.exists(),
        "streams": {}
    }
    
    if HLS_DIR.exists():
        now = time.time()
        for item in HLS_DIR.iterdir():
            if item.is_dir():
                files = []
                for f in item.iterdir():
                    stat = os_module.stat(f)
                    files.append({
                        "name": f.name,
                        "size": stat.st_size,
                        "age_seconds": round(now - stat.st_mtime, 1)
                    })
                result["streams"][item.name] = {
                    "file_count": len(files),
                    "files": sorted(files, key=lambda x: x["age_seconds"])
                }
            else:
                # Arquivo na raiz (não deveria existir com hls_nested on)
                result["root_files"] = result.get("root_files", [])
                result["root_files"].append(item.name)
    
    return result


@app.get("/debug/stream/{stream_key}")
async def debug_stream(stream_key: str):
    """Debug endpoint para verificar status de um stream"""
    import os as os_module
    import time
    
    stream_info = stream_manager.get_stream_status(stream_key)
    stream_dir = HLS_DIR / stream_key
    
    files = []
    playlist_content = None
    newest_segment_age = None
    process_running = False
    
    # Verificar se processo está rodando
    if stream_key in stream_manager.processes:
        process = stream_manager.processes[stream_key]
        process_running = process.poll() is None
    
    if stream_dir.exists():
        all_files = os_module.listdir(stream_dir)
        now = time.time()
        
        for f in all_files:
            file_path = stream_dir / f
            stat = os_module.stat(file_path)
            age = now - stat.st_mtime
            files.append({
                "name": f,
                "size": stat.st_size,
                "age_seconds": round(age, 1)
            })
            
            # Track newest .ts segment
            if f.endswith('.ts'):
                if newest_segment_age is None or age < newest_segment_age:
                    newest_segment_age = age
        
        playlist_path = stream_dir / "index.m3u8"
        if playlist_path.exists():
            async with aiofiles.open(playlist_path, mode='r') as f:
                playlist_content = await f.read()
    
    return {
        "stream_key": stream_key,
        "stream_info": stream_info,
        "process_running": process_running,
        "hls_dir_exists": stream_dir.exists(),
        "files": sorted(files, key=lambda x: x["age_seconds"]),
        "newest_segment_age_seconds": round(newest_segment_age, 1) if newest_segment_age else None,
        "playlist_content": playlist_content,
        "diagnosis": "OK - FFmpeg generating segments" if (process_running and newest_segment_age and newest_segment_age < 10) 
                     else "STALLED - No new segments" if (newest_segment_age and newest_segment_age > 10)
                     else "DEAD - Process not running" if not process_running
                     else "STARTING - Waiting for segments"
    }


@app.post("/streams", response_model=StreamInfo)
async def create_stream(stream: StreamCreate, background_tasks: BackgroundTasks):
    """
    Registra e inicia uma nova stream.
    
    Para RTMP push (source_url vazia): apenas registra e aguarda stream via nginx-rtmp
    Para RTSP/RTMP pull (source_url preenchida): inicia FFmpeg para conversão
    """
    base_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "localhost:8080")
    protocol = "https" if "railway" in base_url else "http"
    
    # Se source_url está vazia, é um stream RTMP push (OBS/câmera envia diretamente)
    if not stream.source_url or stream.source_url.strip() == "":
        print(f"📡 RTMP push mode: registrando {stream.stream_key} (aguardando conexão)")
        
        # Criar diretório HLS para nginx-rtmp usar
        stream_dir = HLS_DIR / stream.stream_key
        stream_dir.mkdir(parents=True, exist_ok=True)
        
        # Registrar stream como "waiting" - nginx-rtmp vai atualizar quando receber
        stream_manager.streams[stream.stream_key] = {
            "name": stream.name,
            "source_url": "",
            "status": "waiting",  # Aguardando conexão RTMP
            "mode": "rtmp-push",
            "dir": str(stream_dir),
            "start_time": __import__('time').time(),
            "restart_count": 0,
        }
        
        return StreamInfo(
            stream_key=stream.stream_key,
            name=stream.name,
            source_url="",
            status="waiting",
            hls_url=f"{protocol}://{base_url}/hls/{stream.stream_key}.m3u8"
        )
    
    # Se já existe, parar antes de reiniciar
    if stream.stream_key in stream_manager.streams:
        print(f"⚠️ Stream {stream.stream_key} já existe, parando para reiniciar...")
        await stream_manager.stop_stream(stream.stream_key)
    
    # Inicia conversão FFmpeg em background
    background_tasks.add_task(
        stream_manager.start_stream,
        stream.stream_key,
        stream.source_url,
        stream.name
    )
    
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
    """Retorna a playlist HLS (.m3u8) com paths corrigidos - ANTI-CACHE"""
    import time
    
    playlist_path = HLS_DIR / stream_key / "index.m3u8"
    
    if not playlist_path.exists():
        raise HTTPException(status_code=404, detail="Stream não encontrada ou ainda iniciando")
    
    async with aiofiles.open(playlist_path, mode='r') as f:
        content = await f.read()
    
    # Ajustar paths dos segmentos para incluir o stream_key
    lines = content.split('\n')
    adjusted_lines = []
    for line in lines:
        if line.endswith('.ts'):
            # Adicionar timestamp para evitar cache de segmentos
            adjusted_lines.append(f"{stream_key}/{line}?_={int(time.time() * 1000)}")
        else:
            adjusted_lines.append(line)
    
    adjusted_content = '\n'.join(adjusted_lines)
    
    return Response(
        content=adjusted_content,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "Access-Control-Allow-Origin": "*",
            "X-Content-Type-Options": "nosniff",
        }
    )


@app.get("/hls/{stream_key}/{segment}")
async def get_segment(stream_key: str, segment: str):
    """Retorna um segmento de vídeo (.ts) - permite cache curto"""
    # Remover query string do nome do segmento se existir
    segment_name = segment.split('?')[0]
    segment_path = HLS_DIR / stream_key / segment_name
    
    if not segment_path.exists():
        raise HTTPException(status_code=404, detail="Segmento não encontrado")
    
    return FileResponse(
        segment_path,
        media_type="video/mp2t",
        headers={
            # Segmentos podem ter cache curto (já são imutáveis uma vez criados)
            "Cache-Control": "public, max-age=2",
            "Access-Control-Allow-Origin": "*",
        }
    )


# ============ RTMP Push Callbacks (nginx-rtmp) ============

@app.post("/rtmp/on_publish")
async def rtmp_on_publish(request: Request):
    """
    Callback chamado pelo nginx-rtmp quando um stream começa.
    nginx-rtmp envia dados como form-urlencoded.
    """
    try:
        form = await request.form()
        stream_key = form.get("name", "")
        app_name = form.get("app", "")
        
        print(f"📡 RTMP on_publish received: app={app_name}, name={stream_key}")
        
        if not stream_key:
            print("⚠️ No stream key received")
            # Retornar 200 mesmo assim para não bloquear
            return Response(status_code=200)
        
        # Registrar stream no manager (nginx já gera HLS automaticamente)
        base_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "localhost:8080")
        
        stream_manager.streams[stream_key] = {
            "name": stream_key,
            "source_url": f"rtmp://localhost/live/{stream_key}",
            "status": "running",
            "mode": "rtmp-push",
            "dir": str(HLS_DIR / stream_key),
            "start_time": __import__('time').time(),
            "restart_count": 0,
        }
        
        print(f"✅ Stream registered: {stream_key}")
        
        # Retornar 200 OK para permitir o stream
        return Response(status_code=200)
        
    except Exception as e:
        print(f"❌ Error in on_publish: {e}")
        # Retornar 200 mesmo em erro para não bloquear stream
        return Response(status_code=200)


@app.post("/rtmp/on_publish_done")
async def rtmp_on_publish_done(request: Request):
    """
    Callback chamado pelo nginx-rtmp quando um stream termina.
    """
    try:
        form = await request.form()
        stream_key = form.get("name", "")
        
        print(f"🛑 RTMP stream ended: {stream_key}")
        
        # Atualizar status no manager
        if stream_key and stream_key in stream_manager.streams:
            stream_manager.streams[stream_key]["status"] = "stopped"
        
        return Response(status_code=200)
        
    except Exception as e:
        print(f"❌ Error in on_publish_done: {e}")
        return Response(status_code=200)


# ============ Para desenvolvimento local ============

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
