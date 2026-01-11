"""
Stream Manager - Gerencia conversão RTSP/RTMP → HLS usando FFmpeg
"""

import asyncio
import subprocess
import os
import shutil
from pathlib import Path
from typing import Dict, Optional
import signal


class StreamManager:
    """Gerencia múltiplas streams FFmpeg"""
    
    def __init__(self, hls_dir: Path):
        self.hls_dir = hls_dir
        self.streams: Dict[str, dict] = {}
        self.processes: Dict[str, subprocess.Popen] = {}
    
    async def start_stream(
        self,
        stream_key: str,
        source_url: str,
        name: Optional[str] = None
    ):
        """Inicia conversão de RTSP/RTMP para HLS"""
        
        # Criar diretório para esta stream
        stream_dir = self.hls_dir / stream_key
        stream_dir.mkdir(parents=True, exist_ok=True)
        
        # Registrar stream
        self.streams[stream_key] = {
            "name": name,
            "source_url": source_url,
            "status": "starting",
            "dir": str(stream_dir)
        }
        
        # Comando FFmpeg para converter RTSP/RTMP → HLS
        output_path = stream_dir / "index.m3u8"
        
        cmd = [
            "ffmpeg",
            "-rtsp_transport", "tcp",      # Usar TCP para RTSP (mais estável)
            "-i", source_url,               # URL de entrada
            "-c:v", "libx264",              # Codec de vídeo
            "-preset", "ultrafast",         # Preset rápido para baixa latência
            "-tune", "zerolatency",         # Otimizar para latência
            "-c:a", "aac",                  # Codec de áudio
            "-ar", "44100",                 # Sample rate
            "-f", "hls",                    # Formato de saída
            "-hls_time", "2",               # Duração de cada segmento (segundos)
            "-hls_list_size", "5",          # Número de segmentos na playlist
            "-hls_flags", "delete_segments+append_list",  # Limpar segmentos antigos
            "-hls_segment_filename", str(stream_dir / "segment_%03d.ts"),
            str(output_path)
        ]
        
        print(f"🎬 Starting stream: {stream_key}")
        print(f"📡 Source: {source_url}")
        
        try:
            # Iniciar processo FFmpeg
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid if os.name != 'nt' else None
            )
            
            self.processes[stream_key] = process
            self.streams[stream_key]["status"] = "running"
            self.streams[stream_key]["pid"] = process.pid
            
            print(f"✅ Stream started: {stream_key} (PID: {process.pid})")
            
            # Monitorar processo em background
            asyncio.create_task(self._monitor_process(stream_key, process))
            
        except Exception as e:
            print(f"❌ Error starting stream {stream_key}: {e}")
            self.streams[stream_key]["status"] = "error"
            self.streams[stream_key]["error"] = str(e)
    
    async def _monitor_process(self, stream_key: str, process: subprocess.Popen):
        """Monitora o processo FFmpeg e atualiza status"""
        while True:
            await asyncio.sleep(5)
            
            if stream_key not in self.processes:
                break
            
            returncode = process.poll()
            
            if returncode is not None:
                # Processo terminou
                if stream_key in self.streams:
                    if returncode == 0:
                        self.streams[stream_key]["status"] = "stopped"
                    else:
                        self.streams[stream_key]["status"] = "error"
                        # Capturar erro
                        stderr = process.stderr.read().decode() if process.stderr else ""
                        self.streams[stream_key]["error"] = stderr[-500:] if stderr else f"Exit code: {returncode}"
                        print(f"❌ Stream {stream_key} failed: {self.streams[stream_key]['error']}")
                break
    
    async def stop_stream(self, stream_key: str):
        """Para uma stream específica"""
        if stream_key in self.processes:
            process = self.processes[stream_key]
            
            try:
                # Tentar terminar graciosamente
                if os.name != 'nt':
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                else:
                    process.terminate()
                
                # Aguardar até 5 segundos
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # Forçar kill
                    if os.name != 'nt':
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    else:
                        process.kill()
                
                print(f"🛑 Stream stopped: {stream_key}")
                
            except Exception as e:
                print(f"⚠️ Error stopping stream {stream_key}: {e}")
            
            del self.processes[stream_key]
        
        # Limpar arquivos
        if stream_key in self.streams:
            stream_dir = Path(self.streams[stream_key].get("dir", ""))
            if stream_dir.exists():
                shutil.rmtree(stream_dir, ignore_errors=True)
            del self.streams[stream_key]
    
    async def stop_all(self):
        """Para todas as streams"""
        stream_keys = list(self.processes.keys())
        for key in stream_keys:
            await self.stop_stream(key)
    
    def get_stream_status(self, stream_key: str) -> Optional[dict]:
        """Retorna status de uma stream"""
        return self.streams.get(stream_key)
