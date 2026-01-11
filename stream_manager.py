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
    
    def _build_ffmpeg_command(
        self,
        source_url: str,
        output_path: Path,
        stream_dir: Path,
        use_copy: bool = False
    ) -> list:
        """Constrói comando FFmpeg com configurações otimizadas"""
        
        is_rtsp = source_url.lower().startswith("rtsp://")
        
        cmd = ["ffmpeg", "-y"]
        
        # Parâmetros de input para streaming contínuo
        if is_rtsp:
            cmd.extend([
                "-rtsp_transport", "tcp",
                "-rtsp_flags", "prefer_tcp",
                "-timeout", "10000000",            # 10s timeout
                "-reorder_queue_size", "500",
                "-max_delay", "500000",
                "-analyzeduration", "3000000",
                "-probesize", "3000000",
                "-fflags", "+genpts+discardcorrupt+igndts",
                "-flags", "low_delay",
                "-avoid_negative_ts", "make_zero",
                "-use_wallclock_as_timestamps", "1",  # Importante para live
            ])
        else:
            cmd.extend([
                "-fflags", "nobuffer+genpts+discardcorrupt+igndts",
                "-flags", "low_delay",
                "-avoid_negative_ts", "make_zero",
                "-use_wallclock_as_timestamps", "1",
            ])
        
        # Opção para reconexão automática (apenas funciona com algumas versões)
        cmd.extend(["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"])
        
        cmd.extend(["-i", source_url])
        
        if use_copy:
            # Tentar copiar sem re-codificar (mais rápido, menor latência)
            cmd.extend([
                "-c:v", "copy",
                "-an",
            ])
        else:
            # Re-codificar (funciona com qualquer codec de entrada)
            cmd.extend([
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                "-profile:v", "baseline",
                "-level", "3.0",
                "-pix_fmt", "yuv420p",
                "-r", "15",                  # 15 fps para menor carga
                "-g", "30",                  # GOP de 2 segundos
                "-b:v", "1000k",
                "-maxrate", "1000k",
                "-bufsize", "500k",
                "-an",                       # Sem áudio
            ])
        
        # Parâmetros HLS para live streaming contínuo
        cmd.extend([
            "-f", "hls",
            "-hls_time", "2",                    # Segmentos de 2 segundos
            "-hls_list_size", "5",               # Manter 5 segmentos na playlist
            "-hls_flags", "delete_segments+append_list+omit_endlist",  # omit_endlist = stream infinito
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", str(stream_dir / "segment_%05d.ts"),
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
        
        # Se já existe, não iniciar novamente
        if stream_key in self.processes:
            process = self.processes[stream_key]
            if process.poll() is None:  # Ainda rodando
                print(f"⚠️ Stream {stream_key} já está rodando")
                return
        
        # Criar diretório para esta stream
        stream_dir = self.hls_dir / stream_key
        if stream_dir.exists():
            shutil.rmtree(stream_dir, ignore_errors=True)
        stream_dir.mkdir(parents=True, exist_ok=True)
        
        # Registrar stream
        self.streams[stream_key] = {
            "name": name,
            "source_url": source_url,
            "status": "starting",
            "dir": str(stream_dir)
        }
        
        output_path = stream_dir / "index.m3u8"
        
        print(f"🎬 Starting stream: {stream_key}")
        print(f"📡 Source: {source_url}")
        
        # Primeiro tenta com copy (mais rápido)
        # Se falhar em 5 segundos, usa re-codificação
        success = await self._try_start_with_copy(stream_key, source_url, output_path, stream_dir)
        
        if not success:
            print(f"⚠️ Copy failed, trying with re-encoding...")
            await self._start_with_reencode(stream_key, source_url, output_path, stream_dir)
    
    async def _try_start_with_copy(
        self,
        stream_key: str,
        source_url: str,
        output_path: Path,
        stream_dir: Path
    ) -> bool:
        """Tenta iniciar com copy, retorna False se falhar"""
        
        cmd = self._build_ffmpeg_command(source_url, output_path, stream_dir, use_copy=True)
        print(f"🔄 Trying copy mode: {' '.join(cmd[:10])}...")
        
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid if os.name != 'nt' else None
            )
            
            # Aguardar até 8 segundos para ver se cria o arquivo m3u8
            for i in range(16):
                await asyncio.sleep(0.5)
                
                # Verificar se processo ainda está rodando
                if process.poll() is not None:
                    stderr = process.stderr.read().decode() if process.stderr else ""
                    print(f"❌ Copy mode failed early: {stderr[-300:]}")
                    return False
                
                # Verificar se m3u8 foi criado
                if output_path.exists() and output_path.stat().st_size > 0:
                    print(f"✅ Copy mode working!")
                    self.processes[stream_key] = process
                    self.streams[stream_key]["status"] = "running"
                    self.streams[stream_key]["pid"] = process.pid
                    self.streams[stream_key]["mode"] = "copy"
                    asyncio.create_task(self._monitor_process(stream_key, process))
                    return True
            
            # Timeout - matar processo e tentar re-encode
            print(f"⚠️ Copy mode timeout, no m3u8 created")
            try:
                if os.name != 'nt':
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
            except:
                pass
            return False
            
        except Exception as e:
            print(f"❌ Error in copy mode: {e}")
            return False
    
    async def _start_with_reencode(
        self,
        stream_key: str,
        source_url: str,
        output_path: Path,
        stream_dir: Path
    ):
        """Inicia com re-codificação (fallback)"""
        
        # Limpar diretório
        if stream_dir.exists():
            shutil.rmtree(stream_dir, ignore_errors=True)
        stream_dir.mkdir(parents=True, exist_ok=True)
        
        cmd = self._build_ffmpeg_command(source_url, output_path, stream_dir, use_copy=False)
        print(f"🔄 Starting with re-encode: {' '.join(cmd[:10])}...")
        
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid if os.name != 'nt' else None
            )
            
            self.processes[stream_key] = process
            self.streams[stream_key]["status"] = "running"
            self.streams[stream_key]["pid"] = process.pid
            self.streams[stream_key]["mode"] = "reencode"
            
            print(f"✅ Stream started with re-encode: {stream_key} (PID: {process.pid})")
            
            asyncio.create_task(self._monitor_process(stream_key, process))
            
        except Exception as e:
            print(f"❌ Error starting stream {stream_key}: {e}")
            self.streams[stream_key]["status"] = "error"
            self.streams[stream_key]["error"] = str(e)
    
    async def _monitor_process(self, stream_key: str, process: subprocess.Popen):
        """Monitora o processo FFmpeg e reinicia se falhar"""
        
        # Aguardar um pouco antes de começar a monitorar
        await asyncio.sleep(10)
        
        restart_count = 0
        max_restarts = 5
        
        while True:
            await asyncio.sleep(5)
            
            if stream_key not in self.processes:
                break
            
            if stream_key not in self.streams:
                break
                
            returncode = process.poll()
            
            if returncode is not None:
                # Processo terminou
                stderr = ""
                try:
                    stderr = process.stderr.read().decode() if process.stderr else ""
                except:
                    pass
                    
                print(f"⚠️ Stream {stream_key} stopped (code: {returncode})")
                
                # Tentar reiniciar se não excedeu limite
                if restart_count < max_restarts:
                    restart_count += 1
                    print(f"🔄 Restarting stream {stream_key} (attempt {restart_count}/{max_restarts})")
                    
                    # Limpar processo antigo
                    if stream_key in self.processes:
                        del self.processes[stream_key]
                    
                    # Aguardar antes de reiniciar
                    await asyncio.sleep(2)
                    
                    # Pegar info do stream
                    if stream_key in self.streams:
                        source_url = self.streams[stream_key].get("source_url")
                        name = self.streams[stream_key].get("name")
                        
                        if source_url:
                            # Reiniciar o stream
                            await self.start_stream(stream_key, source_url, name)
                            
                            # Pegar o novo processo para continuar monitorando
                            if stream_key in self.processes:
                                process = self.processes[stream_key]
                                await asyncio.sleep(10)  # Aguardar estabilizar
                                continue
                    
                    # Se não conseguiu reiniciar, sair do loop
                    break
                else:
                    print(f"❌ Stream {stream_key} failed after {max_restarts} restart attempts")
                    if stream_key in self.streams:
                        self.streams[stream_key]["status"] = "error"
                        self.streams[stream_key]["error"] = f"Failed after {max_restarts} restarts: {stderr[-200:]}"
                    break
            else:
                # Processo ainda rodando, resetar contador de restarts
                restart_count = 0
    
    async def stop_stream(self, stream_key: str):
        """Para uma stream específica"""
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
                    if os.name != 'nt':
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    else:
                        process.kill()
                
                print(f"🛑 Stream stopped: {stream_key}")
                
            except Exception as e:
                print(f"⚠️ Error stopping stream {stream_key}: {e}")
            
            del self.processes[stream_key]
        
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
