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
        "streams": {},
        "all_items": []  # Lista todos os itens no diretório raiz
    }
    
    if HLS_DIR.exists():
        now = time.time()
        
        # Listar TUDO no diretório raiz
        for item in HLS_DIR.iterdir():
            item_info = {
                "name": item.name,
                "is_dir": item.is_dir(),
                "size": item.stat().st_size if item.is_file() else None,
            }
            result["all_items"].append(item_info)
            
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
                # Arquivo na raiz
                result["root_files"] = result.get("root_files", [])
                result["root_files"].append(item.name)
    
    return result


@app.get("/debug/stream/{stream_key}")
async def debug_stream(stream_key: str):
    """Debug endpoint para verificar status de um stream"""
    import os as os_module
    import time
    import subprocess
    
    stream_info = stream_manager.get_stream_status(stream_key)
    stream_dir = HLS_DIR / stream_key
    
    files = []
    playlist_content = None
    newest_segment_age = None
    process_running = False
    nginx_hls_exists = False
    
    # Verificar se processo FFmpeg está rodando (para streams pull)
    if stream_key in stream_manager.processes:
        process = stream_manager.processes[stream_key]
        process_running = process.poll() is None
    
    # Verificar diretório HLS criado pelo nginx-rtmp
    if stream_dir.exists():
        nginx_hls_exists = True
        all_files = os_module.listdir(stream_dir)
        now = time.time()
        
        for f in all_files:
            file_path = stream_dir / f
            try:
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
            except:
                pass
        
        # Tentar ler playlist
        for playlist_name in ["index.m3u8", f"{stream_key}.m3u8"]:
            playlist_path = stream_dir / playlist_name
            if playlist_path.exists():
                async with aiofiles.open(playlist_path, mode='r') as f:
                    playlist_content = await f.read()
                break
    
    # Verificar se nginx está rodando e escutando na porta 1935
    nginx_status = "unknown"
    try:
        result = subprocess.run(["pgrep", "-x", "nginx"], capture_output=True, timeout=2)
        nginx_status = "running" if result.returncode == 0 else "stopped"
    except:
        pass
    
    # Determinar diagnóstico
    mode = stream_info.get("mode") if stream_info else None
    
    if mode == "rtmp-push":
        if nginx_hls_exists and files:
            if newest_segment_age and newest_segment_age < 10:
                diagnosis = "OK - nginx-rtmp generating segments"
            else:
                diagnosis = f"STALLED - No new segments for {newest_segment_age:.0f}s" if newest_segment_age else "STALLED"
        elif nginx_hls_exists:
            diagnosis = "WAITING - Directory exists but no files yet"
        else:
            diagnosis = "ERROR - HLS directory not created by nginx-rtmp"
    else:
        diagnosis = "OK - FFmpeg generating segments" if (process_running and newest_segment_age and newest_segment_age < 10) \
                     else "STALLED - No new segments" if (newest_segment_age and newest_segment_age > 10) \
                     else "DEAD - Process not running" if not process_running \
                     else "STARTING - Waiting for segments"
    
    return {
        "stream_key": stream_key,
        "stream_info": stream_info,
        "mode": mode,
        "nginx_status": nginx_status,
        "process_running": process_running,
        "hls_dir_exists": nginx_hls_exists,
        "files": sorted(files, key=lambda x: x["age_seconds"]) if files else [],
        "file_count": len(files),
        "newest_segment_age_seconds": round(newest_segment_age, 1) if newest_segment_age else None,
        "playlist_content": playlist_content,
        "diagnosis": diagnosis
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
    
    stream_dir = HLS_DIR / stream_key
    
    # Tentar encontrar playlist - pode ser index.m3u8 ou {stream_key}.m3u8
    playlist_path = None
    possible_names = ["index.m3u8", f"{stream_key}.m3u8", "playlist.m3u8"]
    
    for name in possible_names:
        candidate = stream_dir / name
        if candidate.exists():
            playlist_path = candidate
            break
    
    # Se não encontrou por nome, procurar qualquer .m3u8
    if not playlist_path and stream_dir.exists():
        m3u8_files = list(stream_dir.glob("*.m3u8"))
        if m3u8_files:
            playlist_path = m3u8_files[0]
    
    if not playlist_path or not playlist_path.exists():
        raise HTTPException(status_code=404, detail="Stream não encontrada ou ainda iniciando")
    
    # Verificar idade da playlist - se muito antiga, stream provavelmente parou
    playlist_age = time.time() - playlist_path.stat().st_mtime
    if playlist_age > 30:
        # Playlist não foi atualizada em 30s - stream provavelmente morreu
        raise HTTPException(
            status_code=503, 
            detail=f"Stream inativo (playlist não atualizada há {int(playlist_age)}s)"
        )
    
    async with aiofiles.open(playlist_path, mode='r') as f:
        content = await f.read()
    
    # Ajustar paths dos segmentos para incluir o stream_key
    # E verificar se os segmentos existem
    lines = content.split('\n')
    adjusted_lines = []
    valid_segments = 0
    
    for line in lines:
        if line.endswith('.ts'):
            segment_path = stream_dir / line
            if segment_path.exists():
                # Adicionar timestamp para evitar cache de segmentos
                adjusted_lines.append(f"{stream_key}/{line}?_={int(time.time() * 1000)}")
                valid_segments += 1
            else:
                # Segmento não existe mais - pular (não adicionar linha anterior também)
                # Remover a linha EXTINF anterior se foi adicionada
                if adjusted_lines and adjusted_lines[-1].startswith('#EXTINF'):
                    adjusted_lines.pop()
        else:
            adjusted_lines.append(line)
    
    if valid_segments == 0:
        raise HTTPException(status_code=404, detail="Nenhum segmento válido encontrado")
    
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
        addr = form.get("addr", "unknown")
        
        print(f"📡 RTMP on_publish received: app={app_name}, name={stream_key}, addr={addr}")
        
        if not stream_key:
            print("⚠️ No stream key received")
            return Response(status_code=200)
        
        # Se já existe um stream com esse key, limpar tudo primeiro
        existing = stream_manager.streams.get(stream_key)
        if existing:
            old_mode = existing.get("mode", "unknown")
            print(f"⚠️ Stream {stream_key} already exists (mode: {old_mode}), cleaning up...")
            
            # Se tinha um processo FFmpeg rodando, parar
            if stream_key in stream_manager.processes:
                process = stream_manager.processes[stream_key]
                if process.poll() is None:
                    print(f"   Stopping FFmpeg process PID {process.pid}")
                    try:
                        import signal
                        import os as os_module
                        if os_module.name != 'nt':
                            os_module.killpg(os_module.getpgid(process.pid), signal.SIGKILL)
                        else:
                            process.kill()
                    except:
                        pass
                del stream_manager.processes[stream_key]
            
            # Cancelar watchdog se existir
            if stream_key in stream_manager.watchdog_tasks:
                task = stream_manager.watchdog_tasks[stream_key]
                if not task.done():
                    task.cancel()
                del stream_manager.watchdog_tasks[stream_key]
        
        # Registrar/atualizar como stream RTMP push
        print(f"✅ Registering RTMP push stream: {stream_key}")
        stream_manager.streams[stream_key] = {
            "name": stream_key,
            "source_url": f"rtmp://localhost/live/{stream_key}",
            "status": "running",
            "mode": "rtmp-push",  # IMPORTANTE: marcar como RTMP push
            "dir": str(HLS_DIR / stream_key),
            "start_time": __import__('time').time(),
            "restart_count": 0,
        }
        
        return Response(status_code=200)
        
    except Exception as e:
        print(f"❌ Error in on_publish: {e}")
        import traceback
        traceback.print_exc()
        return Response(status_code=200)


@app.post("/rtmp/on_publish_done")
async def rtmp_on_publish_done(request: Request):
    """
    Callback chamado pelo nginx-rtmp quando um stream termina.
    NÃO remove o stream, apenas marca como parado para permitir reconexão.
    """
    try:
        form = await request.form()
        stream_key = form.get("name", "")
        
        print(f"🛑 RTMP stream ended: {stream_key}")
        
        # Apenas atualizar status - NÃO remover para permitir reconexão fácil
        if stream_key and stream_key in stream_manager.streams:
            stream_manager.streams[stream_key]["status"] = "waiting"  # Aguardando reconexão
            stream_manager.streams[stream_key]["last_disconnect"] = __import__('time').time()
        
        return Response(status_code=200)
        
    except Exception as e:
        print(f"❌ Error in on_publish_done: {e}")
        return Response(status_code=200)


@app.post("/streams/{stream_key}/reset")
async def reset_stream(stream_key: str):
    """
    Reseta o estado de um stream para permitir reconexão.
    Útil quando a câmera precisa reconectar mas o estado está inconsistente.
    """
    print(f"🔄 Resetting stream state: {stream_key}")
    
    # Limpar processo FFmpeg se existir
    if stream_key in stream_manager.processes:
        process = stream_manager.processes[stream_key]
        if process.poll() is None:
            try:
                import signal
                import os as os_module
                if os_module.name != 'nt':
                    os_module.killpg(os_module.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
            except:
                pass
        del stream_manager.processes[stream_key]
    
    # Cancelar watchdog
    if stream_key in stream_manager.watchdog_tasks:
        task = stream_manager.watchdog_tasks[stream_key]
        if not task.done():
            task.cancel()
        del stream_manager.watchdog_tasks[stream_key]
    
    # Limpar arquivos HLS antigos
    stream_dir = HLS_DIR / stream_key
    if stream_dir.exists():
        import shutil
        try:
            shutil.rmtree(stream_dir)
            stream_dir.mkdir(parents=True, exist_ok=True)
            print(f"   Cleaned HLS directory for {stream_key}")
        except Exception as e:
            print(f"   Error cleaning HLS dir: {e}")
    
    # Resetar ou criar entrada do stream
    stream_manager.streams[stream_key] = {
        "name": stream_key,
        "source_url": "",
        "status": "waiting",
        "mode": "rtmp-push",
        "dir": str(stream_dir),
        "start_time": __import__('time').time(),
        "restart_count": 0,
    }
    
    return {"message": "Stream reset", "stream_key": stream_key, "status": "waiting"}


# ============ Para desenvolvimento local ============

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
