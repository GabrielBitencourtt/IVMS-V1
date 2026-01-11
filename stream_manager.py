"""
Stream Manager - Gerencia conversão RTSP/RTMP → HLS usando FFmpeg
Versão robusta com watchdog e auto-restart
"""

import asyncio
import subprocess
import os
import shutil
import time
from pathlib import Path
from typing import Dict, Optional
import signal


class StreamManager:
    """Gerencia múltiplas streams FFmpeg com watchdog"""
    
    def __init__(self, hls_dir: Path):
        self.hls_dir = hls_dir
        self.streams: Dict[str, dict] = {}
        self.processes: Dict[str, subprocess.Popen] = {}
        self.watchdog_tasks: Dict[str, asyncio.Task] = {}
    
    def _build_ffmpeg_command(
        self,
        source_url: str,
        output_path: Path,
        stream_dir: Path,
        use_copy: bool = False
    ) -> list:
        """Constrói comando FFmpeg otimizado para estabilidade"""
        
        is_rtsp = source_url.lower().startswith("rtsp://")
        
        cmd = ["ffmpeg", "-y"]
        
        # Configurações de entrada robustas
        if is_rtsp:
            cmd.extend([
                "-rtsp_transport", "tcp",
                "-rtsp_flags", "prefer_tcp",
                "-timeout", "10000000",
                "-reorder_queue_size", "1000",
                "-max_delay", "1000000",
                "-analyzeduration", "5000000",
                "-probesize", "5000000",
                "-fflags", "+genpts+discardcorrupt+igndts",
                "-flags", "low_delay",
                "-avoid_negative_ts", "make_zero",
            ])
        else:
            cmd.extend([
                "-fflags", "+genpts+discardcorrupt+igndts",
                "-flags", "low_delay",
                "-avoid_negative_ts", "make_zero",
            ])
        
        cmd.extend(["-i", source_url])
        
        if use_copy:
            cmd.extend([
                "-c:v", "copy",
                "-an",
                "-bsf:v", "h264_mp4toannexb",
            ])
        else:
            # Encoding robusto e tolerante a erros
            cmd.extend([
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                "-profile:v", "baseline",
                "-level", "3.1",
                "-pix_fmt", "yuv420p",
                "-r", "15",
                "-g", "15",                      # GOP curto = recuperação mais rápida
                "-keyint_min", "15",
                "-sc_threshold", "0",            # Desabilita scene detection
                "-b:v", "800k",
                "-maxrate", "1000k",
                "-bufsize", "2000k",             # Buffer maior
                "-an",
                # Tolerância a erros
                "-err_detect", "ignore_err",
                "-ec", "favor_inter",
            ])
        
        # HLS otimizado para live streaming
        cmd.extend([
            "-f", "hls",
            "-hls_time", "1",                    # Segmentos de 1 segundo
            "-hls_list_size", "10",              # Mais segmentos na playlist
            "-hls_flags", "delete_segments+append_list+omit_endlist+temp_file",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", str(stream_dir / "seg_%06d.ts"),
            "-hls_start_number_source", "epoch",
            str(output_path)
        ])
        
        return cmd
    
    async def start_stream(
        self,
        stream_key: str,
        source_url: str,
        name: Optional[str] = None
    ):
        """Inicia conversão de RTSP/RTMP para HLS"""
        
        # Se já existe e está rodando, não iniciar novamente
        if stream_key in self.processes:
            process = self.processes[stream_key]
            if process.poll() is None:
                print(f"⚠️ Stream {stream_key} já está rodando (PID: {process.pid})")
                return
        
        # Cancelar watchdog anterior se existir
        if stream_key in self.watchdog_tasks:
            self.watchdog_tasks[stream_key].cancel()
            try:
                await self.watchdog_tasks[stream_key]
            except asyncio.CancelledError:
                pass
        
        # Criar/limpar diretório para esta stream
        stream_dir = self.hls_dir / stream_key
        if stream_dir.exists():
            shutil.rmtree(stream_dir, ignore_errors=True)
        stream_dir.mkdir(parents=True, exist_ok=True)
        
        # Registrar stream
        self.streams[stream_key] = {
            "name": name,
            "source_url": source_url,
            "status": "starting",
            "dir": str(stream_dir),
            "start_time": time.time(),
            "restart_count": 0,
        }
        
        output_path = stream_dir / "index.m3u8"
        
        print(f"🎬 Starting stream: {stream_key}")
        print(f"📡 Source: {source_url}")
        
        # Tentar com copy primeiro, depois re-encode
        success = await self._try_start_with_copy(stream_key, source_url, output_path, stream_dir)
        
        if not success:
            print(f"⚠️ Copy mode failed, trying re-encoding...")
            await self._start_with_reencode(stream_key, source_url, output_path, stream_dir)
    
    async def _try_start_with_copy(
        self,
        stream_key: str,
        source_url: str,
        output_path: Path,
        stream_dir: Path
    ) -> bool:
        """Tenta iniciar com copy mode"""
        
        cmd = self._build_ffmpeg_command(source_url, output_path, stream_dir, use_copy=True)
        print(f"🔄 Trying copy mode...")
        
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid if os.name != 'nt' else None
            )
            
            # Aguardar até 12 segundos para criar m3u8
            for i in range(24):
                await asyncio.sleep(0.5)
                
                if process.poll() is not None:
                    stderr = process.stderr.read().decode() if process.stderr else ""
                    print(f"❌ Copy mode failed: {stderr[-200:]}")
                    return False
                
                if output_path.exists() and output_path.stat().st_size > 0:
                    print(f"✅ Copy mode working!")
                    self._register_process(stream_key, process, "copy")
                    return True
            
            # Timeout
            print(f"⚠️ Copy mode timeout")
            self._kill_process(process)
            return False
            
        except Exception as e:
            print(f"❌ Copy mode error: {e}")
            return False
    
    async def _start_with_reencode(
        self,
        stream_key: str,
        source_url: str,
        output_path: Path,
        stream_dir: Path
    ):
        """Inicia com re-encoding"""
        
        # Limpar diretório
        if stream_dir.exists():
            shutil.rmtree(stream_dir, ignore_errors=True)
        stream_dir.mkdir(parents=True, exist_ok=True)
        
        cmd = self._build_ffmpeg_command(source_url, output_path, stream_dir, use_copy=False)
        print(f"🔄 Starting with re-encode...")
        
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid if os.name != 'nt' else None
            )
            
            self._register_process(stream_key, process, "reencode")
            print(f"✅ Stream started: {stream_key} (PID: {process.pid})")
            
        except Exception as e:
            print(f"❌ Error starting stream: {e}")
            self.streams[stream_key]["status"] = "error"
            self.streams[stream_key]["error"] = str(e)
    
    def _register_process(self, stream_key: str, process: subprocess.Popen, mode: str):
        """Registra processo e inicia watchdog"""
        self.processes[stream_key] = process
        self.streams[stream_key]["status"] = "running"
        self.streams[stream_key]["pid"] = process.pid
        self.streams[stream_key]["mode"] = mode
        self.streams[stream_key]["last_segment_time"] = time.time()
        
        # Iniciar watchdog
        self.watchdog_tasks[stream_key] = asyncio.create_task(
            self._watchdog(stream_key)
        )
    
    def _kill_process(self, process: subprocess.Popen):
        """Mata processo de forma segura"""
        try:
            if os.name != 'nt':
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
        except:
            pass
    
    async def _watchdog(self, stream_key: str):
        """Monitora stream e reinicia se necessário"""
        
        await asyncio.sleep(15)  # Aguardar inicialização
        
        max_restarts = 10
        stall_threshold = 15  # Segundos sem novos segmentos
        
        while stream_key in self.streams:
            await asyncio.sleep(5)
            
            if stream_key not in self.processes:
                break
            
            process = self.processes[stream_key]
            stream_dir = Path(self.streams[stream_key]["dir"])
            
            # Verificar se processo está rodando
            if process.poll() is not None:
                print(f"⚠️ Stream {stream_key} process died")
                await self._handle_restart(stream_key)
                continue
            
            # Verificar se está gerando segmentos
            newest_segment_time = self._get_newest_segment_time(stream_dir)
            
            if newest_segment_time:
                self.streams[stream_key]["last_segment_time"] = newest_segment_time
                age = time.time() - newest_segment_time
                
                if age > stall_threshold:
                    print(f"⚠️ Stream {stream_key} stalled ({age:.1f}s since last segment)")
                    await self._handle_restart(stream_key)
    
    def _get_newest_segment_time(self, stream_dir: Path) -> Optional[float]:
        """Retorna timestamp do segmento mais recente"""
        if not stream_dir.exists():
            return None
        
        newest_time = None
        try:
            for f in stream_dir.iterdir():
                if f.suffix == '.ts':
                    mtime = f.stat().st_mtime
                    if newest_time is None or mtime > newest_time:
                        newest_time = mtime
        except:
            pass
        
        return newest_time
    
    async def _handle_restart(self, stream_key: str):
        """Tenta reiniciar stream"""
        
        if stream_key not in self.streams:
            return
        
        restart_count = self.streams[stream_key].get("restart_count", 0)
        max_restarts = 10
        
        if restart_count >= max_restarts:
            print(f"❌ Stream {stream_key} exceeded max restarts")
            self.streams[stream_key]["status"] = "error"
            self.streams[stream_key]["error"] = f"Exceeded {max_restarts} restarts"
            return
        
        print(f"🔄 Restarting stream {stream_key} (attempt {restart_count + 1})")
        
        # Matar processo atual
        if stream_key in self.processes:
            self._kill_process(self.processes[stream_key])
            del self.processes[stream_key]
        
        # Incrementar contador
        self.streams[stream_key]["restart_count"] = restart_count + 1
        
        await asyncio.sleep(2)
        
        # Reiniciar
        source_url = self.streams[stream_key].get("source_url")
        name = self.streams[stream_key].get("name")
        
        if source_url:
            # Cancelar watchdog atual antes de reiniciar
            if stream_key in self.watchdog_tasks:
                self.watchdog_tasks[stream_key].cancel()
                try:
                    await self.watchdog_tasks[stream_key]
                except asyncio.CancelledError:
                    pass
            
            await self.start_stream(stream_key, source_url, name)
    
    async def stop_stream(self, stream_key: str):
        """Para uma stream"""
        
        # Cancelar watchdog
        if stream_key in self.watchdog_tasks:
            self.watchdog_tasks[stream_key].cancel()
            try:
                await self.watchdog_tasks[stream_key]
            except asyncio.CancelledError:
                pass
            del self.watchdog_tasks[stream_key]
        
        # Parar processo
        if stream_key in self.processes:
            process = self.processes[stream_key]
            
            try:
                if os.name != 'nt':
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                else:
                    process.terminate()
                
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._kill_process(process)
                
                print(f"🛑 Stream stopped: {stream_key}")
                
            except Exception as e:
                print(f"⚠️ Error stopping stream: {e}")
            
            del self.processes[stream_key]
        
        # Limpar diretório
        if stream_key in self.streams:
            stream_dir = Path(self.streams[stream_key].get("dir", ""))
            if stream_dir.exists():
                shutil.rmtree(stream_dir, ignore_errors=True)
            del self.streams[stream_key]
    
    async def stop_all(self):
        """Para todas as streams"""
        stream_keys = list(self.streams.keys())
        for key in stream_keys:
            await self.stop_stream(key)
    
    def get_stream_status(self, stream_key: str) -> Optional[dict]:
        """Retorna status de uma stream"""
        if stream_key not in self.streams:
            return None
        
        info = self.streams[stream_key].copy()
        
        # Adicionar info do processo
        if stream_key in self.processes:
            process = self.processes[stream_key]
            info["process_running"] = process.poll() is None
        else:
            info["process_running"] = False
        
        # Adicionar idade do último segmento
        stream_dir = Path(info.get("dir", ""))
        newest_time = self._get_newest_segment_time(stream_dir)
        if newest_time:
            info["last_segment_age"] = round(time.time() - newest_time, 1)
        
        return info
