"""
Stream Manager - Gerencia conversão RTSP/RTMP → HLS usando FFmpeg
Versão ultra-robusta com reconnect automático e watchdog agressivo
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
    """Gerencia múltiplas streams FFmpeg com watchdog e auto-reconnect"""
    
    def __init__(self, hls_dir: Path):
        self.hls_dir = hls_dir
        self.streams: Dict[str, dict] = {}
        self.processes: Dict[str, subprocess.Popen] = {}
        self.watchdog_tasks: Dict[str, asyncio.Task] = {}
    
    async def _probe_stream(self, source_url: str) -> dict:
        """Usa ffprobe para detectar codec do stream"""
        cmd = [
            "ffprobe",
            "-v", "error",
            "-rtsp_transport", "tcp",
            "-timeout", "5000000",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height",
            "-of", "csv=p=0",
            source_url
        ]
        
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode().strip()
            
            if output:
                parts = output.split(',')
                codec = parts[0] if len(parts) > 0 else "unknown"
                width = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                height = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
                
                print(f"📹 Stream info: codec={codec}, {width}x{height}")
                return {"codec": codec, "width": width, "height": height, "copy_ok": codec == "h264"}
            else:
                print(f"⚠️ Probe returned empty output")
        except asyncio.TimeoutError:
            print(f"⚠️ Probe timeout after 10s")
        except Exception as e:
            print(f"⚠️ Probe failed: {e}")
        
        # Default: não tentar copy mode
        return {"codec": "unknown", "width": 0, "height": 0, "copy_ok": False}
    
    def _build_ffmpeg_command(
        self,
        source_url: str,
        output_path: Path,
        stream_dir: Path,
        use_copy: bool = False
    ) -> list:
        """Constrói comando FFmpeg OTIMIZADO para streaming contínuo"""
        
        is_rtsp = source_url.lower().startswith("rtsp://")
        
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning"]
        
        # Opções globais para stream contínuo
        cmd.extend([
            "-fflags", "+genpts+discardcorrupt+nobuffer",
            "-flags", "low_delay",
            "-strict", "experimental",
        ])
        
        # Configurações de entrada ULTRA otimizadas para baixa latência
        if is_rtsp:
            cmd.extend([
                "-rtsp_transport", "tcp",
                "-rtsp_flags", "prefer_tcp",
                "-timeout", "3000000",           # 3 segundos timeout (era 5)
                "-stimeout", "3000000",          # Socket timeout
                "-buffer_size", "512000",        # Buffer menor
                "-max_delay", "100000",          # 100ms max delay
                "-reorder_queue_size", "0",      # Sem reordenação
                "-analyzeduration", "300000",    # 0.3s análise
                "-probesize", "300000",          # 300KB probe
            ])
        else:
            cmd.extend([
                "-analyzeduration", "300000",
                "-probesize", "300000",
            ])
        
        cmd.extend(["-i", source_url])
        
        if use_copy:
            cmd.extend([
                "-c:v", "copy",
                "-an",  # Sem áudio no copy mode
                "-bsf:v", "h264_mp4toannexb",
            ])
        else:
            # Re-encoding OTIMIZADO - SEM ÁUDIO para evitar problemas
            cmd.extend([
                "-c:v", "libx264",
                "-preset", "veryfast",            # Mais rápido, menos CPU
                "-tune", "zerolatency",           # Crítico para live
                "-profile:v", "baseline",         # Máxima compatibilidade
                "-level", "3.1",
                "-pix_fmt", "yuv420p",
                "-vf", "scale=960:-2",            # 960p - bom equilíbrio
                "-r", "20",                       # 20 fps
                "-g", "40",                       # GOP = 2 segundos (20fps * 2)
                "-keyint_min", "20",              # Keyframe mínimo = 1s
                "-sc_threshold", "0",             # Desabilita scene change detection
                "-b:v", "1000k",
                "-maxrate", "1200k",
                "-bufsize", "2000k",
                "-an",                            # SEM ÁUDIO - evita stalls
                "-threads", "2",
            ])
        
        # HLS ULTRA OTIMIZADO - segmentos menores para início rápido
        cmd.extend([
            "-f", "hls",
            "-hls_time", "0.5",                   # Segmentos de 0.5 segundo
            "-hls_list_size", "4",                # 4 segmentos = 2s na playlist
            "-hls_flags", "delete_segments+independent_segments+split_by_time",
            "-hls_segment_type", "mpegts",
            "-hls_start_number_source", "datetime",
            "-start_number", "1",
            "-hls_segment_filename", str(stream_dir / "seg_%03d.ts"),
            str(output_path)
        ])
        
        return cmd
    
    async def start_stream(
        self,
        stream_key: str,
        source_url: str,
        name: Optional[str] = None
    ):
        """Inicia conversão de RTSP/RTMP para HLS (API pública)"""
        
        # Se já existe e está rodando, retornar
        if stream_key in self.processes:
            process = self.processes[stream_key]
            if process.poll() is None:
                print(f"⚠️ Stream {stream_key} já está rodando (PID: {process.pid})")
                return
        
        # Cancelar watchdog anterior
        await self._cancel_watchdog(stream_key)
        
        await self._start_stream_internal(stream_key, source_url, name)
    
    async def _start_stream_internal(
        self,
        stream_key: str,
        source_url: str,
        name: Optional[str] = None
    ):
        """Lógica interna de iniciar stream (chamada por restart também)"""
        
        # Criar/limpar diretório
        stream_dir = self.hls_dir / stream_key
        if stream_dir.exists():
            shutil.rmtree(stream_dir, ignore_errors=True)
        stream_dir.mkdir(parents=True, exist_ok=True)
        
        # Preservar restart_count se existir
        restart_count = 0
        if stream_key in self.streams:
            restart_count = self.streams[stream_key].get("restart_count", 0)
        
        # Registrar stream
        self.streams[stream_key] = {
            "name": name,
            "source_url": source_url,
            "status": "starting",
            "dir": str(stream_dir),
            "start_time": time.time(),
            "restart_count": restart_count,
        }
        
        output_path = stream_dir / "index.m3u8"
        
        print(f"🎬 Starting stream: {stream_key}")
        print(f"📡 Source: {source_url}")
        
        # Detectar codec para decidir se tenta copy
        probe_info = await self._probe_stream(source_url)
        
        success = False
        if probe_info["copy_ok"]:
            success = await self._try_start_with_copy(stream_key, source_url, output_path, stream_dir)
        
        if not success:
            if probe_info["copy_ok"]:
                print(f"⚠️ Copy mode failed, trying re-encoding...")
            else:
                print(f"ℹ️ Codec '{probe_info['codec']}' requires re-encoding")
            await self._start_with_reencode(stream_key, source_url, output_path, stream_dir)
    
    async def _cancel_watchdog(self, stream_key: str, from_watchdog: bool = False):
        """Cancela watchdog de uma stream de forma segura"""
        if stream_key not in self.watchdog_tasks:
            return
            
        task = self.watchdog_tasks[stream_key]
        
        # Se está sendo chamado de dentro do próprio watchdog, só remover da lista
        if from_watchdog:
            del self.watchdog_tasks[stream_key]
            return
        
        # Se a task já terminou, só limpar
        if task.done():
            del self.watchdog_tasks[stream_key]
            return
        
        # Cancelar e aguardar com tratamento seguro
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, RuntimeError, Exception):
            pass
        
        if stream_key in self.watchdog_tasks:
            del self.watchdog_tasks[stream_key]
    
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
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid if os.name != 'nt' else None
            )
            
            # Aguardar até 8 segundos para criar segmento .ts
            for i in range(16):
                await asyncio.sleep(0.5)
                
                if process.poll() is not None:
                    stderr = process.stderr.read().decode() if process.stderr else ""
                    last_error = stderr.split('\n')[-5:] if stderr else []
                    print(f"❌ Copy mode failed: {''.join(last_error)[-200:]}")
                    return False
                
                # Verificar se há pelo menos um .ts
                ts_files = list(stream_dir.glob("*.ts"))
                if len(ts_files) > 0 and output_path.exists():
                    print(f"✅ Copy mode working!")
                    self._register_process(stream_key, process, "copy")
                    return True
            
            print(f"⚠️ Copy mode timeout - no segments generated")
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
        print(f"📝 CMD: {' '.join(cmd[:20])}...")
        
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
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
        
        # Iniciar watchdog agressivo
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
        """Watchdog agressivo - verifica a cada 3s, reinicia após 10s sem segmentos
        
        IMPORTANTE: Este watchdog é apenas para streams FFmpeg pull (RTSP→HLS).
        Streams RTMP push são gerenciados pelo nginx-rtmp e não precisam de restart.
        """
        
        await asyncio.sleep(8)  # Aguardar inicialização (reduzido de 15s)
        
        max_restarts = 15
        stall_threshold = 10  # Segundos sem novos segmentos (reduzido de 15s)
        check_interval = 3    # Verificar a cada 3s (reduzido de 5s)
        
        consecutive_stalls = 0
        
        while stream_key in self.streams:
            await asyncio.sleep(check_interval)
            
            # IMPORTANTE: Não reiniciar streams RTMP push - são gerenciados externamente
            stream_info = self.streams.get(stream_key, {})
            if stream_info.get("mode") == "rtmp-push":
                # Para RTMP push, apenas verificar se HLS está sendo gerado
                stream_dir = Path(stream_info.get("dir", ""))
                if stream_dir.exists():
                    newest = self._get_newest_segment_time(stream_dir)
                    if newest:
                        age = time.time() - newest
                        if age < 10:
                            # HLS sendo gerado normalmente
                            if stream_info.get("status") != "running":
                                self.streams[stream_key]["status"] = "running"
                        elif age > 30:
                            # Sem segmentos por muito tempo - marcar como stopped
                            if stream_info.get("status") == "running":
                                print(f"⚠️ RTMP push stream {stream_key} stopped (no segments for {age:.0f}s)")
                                self.streams[stream_key]["status"] = "stopped"
                continue
            
            if stream_key not in self.processes:
                break
            
            process = self.processes[stream_key]
            stream_dir = Path(self.streams[stream_key]["dir"])
            
            # Verificar se processo está rodando
            if process.poll() is not None:
                exit_code = process.poll()
                stderr = ""
                try:
                    stderr = process.stderr.read().decode()[-300:] if process.stderr else ""
                except:
                    pass
                print(f"⚠️ Stream {stream_key} process died (exit: {exit_code})")
                if stderr:
                    print(f"   Last error: {stderr}")
                await self._handle_restart(stream_key)
                consecutive_stalls = 0
                continue
            
            # Verificar se está gerando segmentos
            newest_segment_time = self._get_newest_segment_time(stream_dir)
            
            if newest_segment_time:
                self.streams[stream_key]["last_segment_time"] = newest_segment_time
                age = time.time() - newest_segment_time
                
                if age > stall_threshold:
                    consecutive_stalls += 1
                    print(f"⚠️ Stream {stream_key} stalled ({age:.1f}s, count={consecutive_stalls})")
                    
                    if consecutive_stalls >= 2:  # 2 checks seguidos = restart
                        await self._handle_restart(stream_key)
                        consecutive_stalls = 0
                else:
                    consecutive_stalls = 0
            else:
                # Sem segmentos ainda
                stream_age = time.time() - self.streams[stream_key].get("start_time", time.time())
                if stream_age > 15:  # Se não gerou nenhum segmento em 15s
                    print(f"⚠️ Stream {stream_key} never produced segments")
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
        """Reinicia stream com backoff"""
        
        if stream_key not in self.streams:
            return
        
        restart_count = self.streams[stream_key].get("restart_count", 0)
        max_restarts = 15
        
        if restart_count >= max_restarts:
            print(f"❌ Stream {stream_key} exceeded max restarts ({max_restarts})")
            self.streams[stream_key]["status"] = "error"
            self.streams[stream_key]["error"] = f"Exceeded {max_restarts} restarts"
            return
        
        # Backoff exponencial: 1s, 2s, 4s, 8s... max 30s
        backoff = min(30, 2 ** min(restart_count, 5))
        
        print(f"🔄 Restarting stream {stream_key} (attempt {restart_count + 1}, backoff {backoff}s)")
        
        # Matar processo atual
        if stream_key in self.processes:
            self._kill_process(self.processes[stream_key])
            del self.processes[stream_key]
        
        # Incrementar contador
        self.streams[stream_key]["restart_count"] = restart_count + 1
        self.streams[stream_key]["status"] = "restarting"
        
        await asyncio.sleep(backoff)
        
        # Reiniciar
        source_url = self.streams[stream_key].get("source_url")
        name = self.streams[stream_key].get("name")
        
        if source_url:
            # from_watchdog=True porque estamos sendo chamados de dentro do watchdog
            await self._cancel_watchdog(stream_key, from_watchdog=True)
            await self._start_stream_internal(stream_key, source_url, name)
    
    async def stop_stream(self, stream_key: str):
        """Para uma stream"""
        
        # Cancelar watchdog de forma segura
        await self._cancel_watchdog(stream_key)
        
        # Parar processo
        if stream_key in self.processes:
            process = self.processes[stream_key]
            
            try:
                if os.name != 'nt':
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                else:
                    process.terminate()
                
                try:
                    process.wait(timeout=3)
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
        
        # Contar segmentos
        try:
            info["segment_count"] = len(list(Path(info.get("dir", "")).glob("*.ts")))
        except:
            info["segment_count"] = 0
        
        return info
