#!/usr/bin/env python3
"""
HLS Relay Bridge - Gera HLS localmente e envia para servidor Railway

Arquitetura:
1. App local captura RTSP e gera segmentos HLS com FFmpeg
2. Segmentos são enviados via HTTP POST para o servidor Railway
3. Railway apenas serve os arquivos - sem processamento

Benefícios:
- Menor latência (processamento local)
- Menor carga no servidor Railway
- Funciona com IPs privados sem port forwarding
"""

import subprocess
import threading
import logging
import shutil
import os
import time
import requests
from pathlib import Path
from typing import Dict, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import hashlib

logger = logging.getLogger(__name__)


@dataclass
class HLSStreamProcess:
    """Representa um processo de streaming HLS local"""
    stream_key: str
    rtsp_url: str
    hls_dir: Path
    process: subprocess.Popen
    started_at: datetime
    camera_name: str = ""
    status: str = "running"
    error_message: str = ""
    segments_sent: int = 0
    last_segment_time: float = field(default_factory=time.time)


class HLSRelayBridge:
    """
    Gerencia streams locais com HLS Relay.
    Gera HLS localmente e envia para servidor Railway.
    """
    
    def __init__(
        self,
        relay_server_url: str = "https://your-railway-app.up.railway.app",
        local_hls_dir: str = None,
        on_status_change: Optional[Callable] = None,
        on_ffmpeg_progress: Optional[Callable] = None
    ):
        self.relay_server_url = relay_server_url.rstrip('/')
        self.on_status_change = on_status_change
        self.on_ffmpeg_progress = on_ffmpeg_progress
        self.active_streams: Dict[str, HLSStreamProcess] = {}
        self._ffmpeg_path: Optional[str] = None
        self._ffmpeg_available = False
        self._running = True
        
        # Diretório local para HLS
        if local_hls_dir:
            self._local_hls_dir = Path(local_hls_dir)
        else:
            # Usar temp dir do sistema
            import tempfile
            self._local_hls_dir = Path(tempfile.gettempdir()) / "ivms_hls_relay"
        
        self._local_hls_dir.mkdir(parents=True, exist_ok=True)
        
        # Thread pool para envio de segmentos
        self._upload_executor = ThreadPoolExecutor(max_workers=4)
        
        # Sessão HTTP persistente para melhor performance
        self._http_session = requests.Session()
        self._http_session.headers.update({
            'User-Agent': 'IVMS-HLS-Relay/1.0',
            'Connection': 'keep-alive'
        })
        
        # Inicializa FFmpeg
        self._init_ffmpeg()
        
        # Inicia threads de monitoramento
        self._start_monitors()
    
    def _init_ffmpeg(self):
        """Inicializa FFmpeg - verifica ou instala automaticamente"""
        try:
            from ffmpeg_installer import FFmpegInstaller
            
            def progress_callback(msg, pct):
                logger.info(f"FFmpeg: {msg}")
                if self.on_ffmpeg_progress:
                    self.on_ffmpeg_progress(msg, pct)
            
            installer = FFmpegInstaller(progress_callback)
            success, path = installer.ensure_ffmpeg()
            
            if success and path:
                self._ffmpeg_path = path
                self._ffmpeg_available = True
                logger.info(f"✓ FFmpeg disponível: {path}")
            else:
                self._ffmpeg_available = False
                logger.error("❌ FFmpeg não disponível")
                
        except ImportError:
            logger.warning("ffmpeg_installer não encontrado, usando busca simples")
            self._ffmpeg_path = self._find_ffmpeg_simple()
            self._ffmpeg_available = self._ffmpeg_path is not None
    
    def _find_ffmpeg_simple(self) -> Optional[str]:
        """Busca simples do FFmpeg (fallback)"""
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            return ffmpeg
        
        common_paths = [
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            "/usr/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "/opt/homebrew/bin/ffmpeg",
        ]
        
        for path in common_paths:
            if os.path.isfile(path):
                return path
        
        return None
    
    def is_ffmpeg_available(self) -> bool:
        """Verifica se o FFmpeg está disponível"""
        if not self._ffmpeg_path:
            return False
        
        try:
            result = subprocess.run(
                [self._ffmpeg_path, "-version"],
                capture_output=True,
                timeout=5
            )
            return result.returncode == 0
        except Exception as e:
            logger.error(f"FFmpeg não disponível: {e}")
            return False
    
    def _start_monitors(self):
        """Inicia threads de monitoramento"""
        # Monitor de processos
        self._monitor_thread = threading.Thread(target=self._monitor_processes, daemon=True)
        self._monitor_thread.start()
        
        # Monitor de segmentos para upload
        self._upload_thread = threading.Thread(target=self._monitor_segments, daemon=True)
        self._upload_thread.start()
    
    def _monitor_processes(self):
        """Monitora status dos processos FFmpeg"""
        while self._running:
            time.sleep(2)
            
            for stream_key, stream in list(self.active_streams.items()):
                if stream.process.poll() is not None:
                    exit_code = stream.process.returncode
                    
                    if exit_code != 0:
                        stderr_output = ""
                        try:
                            stderr_output = stream.process.stderr.read() if stream.process.stderr else ""
                            if isinstance(stderr_output, bytes):
                                stderr_output = stderr_output.decode('utf-8', errors='ignore')
                        except:
                            pass
                        
                        stream.status = "error"
                        stream.error_message = f"FFmpeg encerrou com código {exit_code}"
                        logger.error(f"Stream {stream_key} falhou: {stderr_output[-500:]}")
                    else:
                        stream.status = "stopped"
                    
                    if self.on_status_change:
                        self.on_status_change(stream_key, stream.status, stream.error_message)
    
    def _monitor_segments(self):
        """Monitora novos segmentos HLS e faz upload para o servidor"""
        uploaded_segments: Dict[str, set] = {}  # stream_key -> set of uploaded segment names
        
        while self._running:
            time.sleep(0.3)  # Checar a cada 300ms para baixa latência
            
            for stream_key, stream in list(self.active_streams.items()):
                if stream.status != "running":
                    continue
                
                if stream_key not in uploaded_segments:
                    uploaded_segments[stream_key] = set()
                
                stream_dir = stream.hls_dir
                if not stream_dir.exists():
                    continue
                
                # Listar arquivos .ts e .m3u8
                try:
                    files = list(stream_dir.iterdir())
                except:
                    continue
                
                for file_path in files:
                    if not file_path.is_file():
                        continue
                    
                    filename = file_path.name
                    
                    # Para .ts, verificar se já foi enviado
                    if filename.endswith('.ts'):
                        if filename in uploaded_segments[stream_key]:
                            continue
                        
                        # Verificar se arquivo está completo (não está sendo escrito)
                        try:
                            size1 = file_path.stat().st_size
                            time.sleep(0.05)
                            size2 = file_path.stat().st_size
                            if size1 != size2:
                                continue  # Ainda sendo escrito
                        except:
                            continue
                        
                        # Enviar segmento em background
                        uploaded_segments[stream_key].add(filename)
                        self._upload_executor.submit(
                            self._upload_segment,
                            stream_key,
                            file_path,
                            filename
                        )
                    
                    # Para .m3u8, sempre enviar (atualiza constantemente)
                    elif filename.endswith('.m3u8'):
                        self._upload_executor.submit(
                            self._upload_playlist,
                            stream_key,
                            file_path,
                            filename
                        )
                
                # Limpar segmentos antigos do cache
                if len(uploaded_segments[stream_key]) > 50:
                    # Manter apenas os últimos 20
                    sorted_segments = sorted(uploaded_segments[stream_key])
                    uploaded_segments[stream_key] = set(sorted_segments[-20:])
    
    def _upload_segment(self, stream_key: str, file_path: Path, filename: str):
        """Envia um segmento .ts para o servidor"""
        try:
            url = f"{self.relay_server_url}/relay/{stream_key}/{filename}"
            
            with open(file_path, 'rb') as f:
                content = f.read()
            
            response = self._http_session.put(
                url,
                data=content,
                headers={'Content-Type': 'video/mp2t'},
                timeout=5
            )
            
            if response.status_code in (200, 201):
                if stream_key in self.active_streams:
                    self.active_streams[stream_key].segments_sent += 1
                    self.active_streams[stream_key].last_segment_time = time.time()
                logger.debug(f"✓ Uploaded {filename}")
            else:
                logger.warning(f"Upload failed {filename}: {response.status_code}")
                
        except Exception as e:
            logger.error(f"Error uploading segment {filename}: {e}")
    
    def _upload_playlist(self, stream_key: str, file_path: Path, filename: str):
        """Envia playlist .m3u8 para o servidor"""
        try:
            url = f"{self.relay_server_url}/relay/{stream_key}/{filename}"
            
            with open(file_path, 'r') as f:
                content = f.read()
            
            response = self._http_session.put(
                url,
                data=content,
                headers={'Content-Type': 'application/vnd.apple.mpegurl'},
                timeout=3
            )
            
            if response.status_code not in (200, 201):
                logger.warning(f"Playlist upload failed: {response.status_code}")
                
        except Exception as e:
            logger.debug(f"Error uploading playlist: {e}")
    
    def start_stream(self, stream_key: str, rtsp_url: str, camera_name: str = "") -> Dict:
        """
        Inicia um novo stream com HLS Relay.
        
        Args:
            stream_key: Chave única do stream
            rtsp_url: URL RTSP da câmera local
            camera_name: Nome da câmera (opcional)
        
        Returns:
            Dict com status da operação
        """
        if not self._ffmpeg_available:
            return {
                "success": False,
                "error": "FFmpeg não disponível",
                "stream_key": stream_key
            }
        
        if stream_key in self.active_streams:
            existing = self.active_streams[stream_key]
            if existing.process.poll() is None:
                return {
                    "success": False,
                    "error": "Stream já está ativo",
                    "stream_key": stream_key
                }
        
        # Criar diretório HLS local
        stream_dir = self._local_hls_dir / stream_key
        if stream_dir.exists():
            shutil.rmtree(stream_dir, ignore_errors=True)
        stream_dir.mkdir(parents=True, exist_ok=True)
        
        output_playlist = stream_dir / "index.m3u8"
        
        # Comando FFmpeg ULTRA LOW LATENCY para HLS local
        cmd = [
            self._ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel", "warning",
            # Opções de baixa latência
            "-fflags", "+genpts+discardcorrupt+nobuffer",
            "-flags", "low_delay",
            "-strict", "experimental",
            # Input RTSP
            "-rtsp_transport", "tcp",
            "-rtsp_flags", "prefer_tcp",
            "-timeout", "5000000",
            "-buffer_size", "512000",
            "-max_delay", "100000",
            "-analyzeduration", "500000",
            "-probesize", "500000",
            "-i", rtsp_url,
            # Video encoding - ultra low latency
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-profile:v", "baseline",
            "-level", "3.1",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=854:-2",  # 480p
            "-r", "25",
            "-g", "12",  # GOP = 0.5s
            "-keyint_min", "12",
            "-sc_threshold", "0",
            "-b:v", "800k",
            "-maxrate", "900k",
            "-bufsize", "400k",
            "-an",  # Sem áudio
            "-threads", "2",
            # HLS output - segmentos pequenos
            "-f", "hls",
            "-hls_time", "0.5",  # Segmentos de 0.5s
            "-hls_list_size", "4",  # 4 segmentos = 2s buffer
            "-hls_flags", "delete_segments+independent_segments+split_by_time",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", str(stream_dir / "seg_%03d.ts"),
            str(output_playlist)
        ]
        
        logger.info(f"Iniciando HLS Relay: {stream_key}")
        logger.debug(f"RTSP: {rtsp_url}")
        logger.debug(f"HLS Dir: {stream_dir}")
        
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL
            )
            
            # Aguarda um pouco para verificar se não crashou
            time.sleep(0.8)
            
            if process.poll() is not None:
                stderr = process.stderr.read().decode('utf-8', errors='ignore')
                error_msg = self._parse_ffmpeg_error(stderr)
                
                return {
                    "success": False,
                    "error": error_msg,
                    "stream_key": stream_key
                }
            
            # Registrar stream na API do servidor
            self._register_stream_on_server(stream_key, camera_name)
            
            # Stream iniciado com sucesso
            stream_info = HLSStreamProcess(
                stream_key=stream_key,
                rtsp_url=rtsp_url,
                hls_dir=stream_dir,
                process=process,
                started_at=datetime.now(),
                camera_name=camera_name,
                status="running"
            )
            
            self.active_streams[stream_key] = stream_info
            
            logger.info(f"✓ HLS Relay stream {stream_key} iniciado")
            
            if self.on_status_change:
                self.on_status_change(stream_key, "running", "")
            
            return {
                "success": True,
                "stream_key": stream_key,
                "mode": "hls-relay",
                "hls_url": f"{self.relay_server_url}/hls/{stream_key}.m3u8"
            }
            
        except FileNotFoundError:
            return {
                "success": False,
                "error": "FFmpeg não encontrado",
                "stream_key": stream_key
            }
        except Exception as e:
            logger.error(f"Erro ao iniciar stream: {e}")
            return {
                "success": False,
                "error": str(e),
                "stream_key": stream_key
            }
    
    def _register_stream_on_server(self, stream_key: str, camera_name: str = ""):
        """Registra stream no servidor Railway"""
        try:
            url = f"{self.relay_server_url}/relay/register"
            response = self._http_session.post(
                url,
                json={
                    "stream_key": stream_key,
                    "name": camera_name,
                    "mode": "hls-relay"
                },
                timeout=5
            )
            
            if response.status_code in (200, 201):
                logger.info(f"✓ Stream registrado no servidor: {stream_key}")
            else:
                logger.warning(f"Falha ao registrar stream: {response.status_code}")
                
        except Exception as e:
            logger.warning(f"Erro ao registrar stream no servidor: {e}")
    
    def _parse_ffmpeg_error(self, stderr: str) -> str:
        """Extrai mensagem de erro amigável do stderr do FFmpeg"""
        stderr_lower = stderr.lower()
        
        if "connection refused" in stderr_lower:
            return "Conexão recusada - câmera offline ou IP incorreto"
        elif "connection timed out" in stderr_lower:
            return "Timeout - câmera não respondeu"
        elif "401 unauthorized" in stderr_lower or "authentication" in stderr_lower:
            return "Autenticação falhou - usuário/senha incorretos"
        elif "404" in stderr_lower or "not found" in stderr_lower:
            return "Stream não encontrado - verifique a URL RTSP"
        elif "invalid data" in stderr_lower:
            return "Dados inválidos - formato não suportado"
        elif "no route to host" in stderr_lower:
            return "Câmera inacessível - verifique a rede"
        else:
            lines = [l for l in stderr.split('\n') if l.strip() and 'error' in l.lower()]
            if lines:
                return lines[-1][:200]
            return "Erro desconhecido ao conectar"
    
    def stop_stream(self, stream_key: str) -> Dict:
        """Para um stream ativo"""
        if stream_key not in self.active_streams:
            return {
                "success": False,
                "error": "Stream não encontrado"
            }
        
        stream = self.active_streams[stream_key]
        
        try:
            stream.process.terminate()
            
            try:
                stream.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                stream.process.kill()
            
            # Notificar servidor
            self._unregister_stream_on_server(stream_key)
            
            # Limpar arquivos locais
            if stream.hls_dir.exists():
                shutil.rmtree(stream.hls_dir, ignore_errors=True)
            
            stream.status = "stopped"
            del self.active_streams[stream_key]
            
            logger.info(f"✓ Stream {stream_key} parado")
            
            if self.on_status_change:
                self.on_status_change(stream_key, "stopped", "")
            
            return {
                "success": True,
                "stream_key": stream_key
            }
            
        except Exception as e:
            logger.error(f"Erro ao parar stream: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _unregister_stream_on_server(self, stream_key: str):
        """Remove registro do stream no servidor"""
        try:
            url = f"{self.relay_server_url}/relay/{stream_key}"
            self._http_session.delete(url, timeout=3)
        except:
            pass
    
    def stop_all_streams(self):
        """Para todos os streams ativos"""
        for stream_key in list(self.active_streams.keys()):
            self.stop_stream(stream_key)
    
    def get_stream_status(self, stream_key: str) -> Optional[Dict]:
        """Retorna o status de um stream específico"""
        if stream_key not in self.active_streams:
            return None
        
        stream = self.active_streams[stream_key]
        is_running = stream.process.poll() is None
        
        return {
            "stream_key": stream_key,
            "camera_name": stream.camera_name,
            "rtsp_url": stream.rtsp_url,
            "mode": "hls-relay",
            "status": "running" if is_running else stream.status,
            "started_at": stream.started_at.isoformat(),
            "segments_sent": stream.segments_sent,
            "last_segment_time": stream.last_segment_time,
            "error": stream.error_message,
            "hls_url": f"{self.relay_server_url}/hls/{stream_key}.m3u8"
        }
    
    def get_all_streams(self) -> list:
        """Retorna lista de todos os streams ativos"""
        return [self.get_stream_status(key) for key in self.active_streams]
    
    def shutdown(self):
        """Encerra o bridge e todos os streams."""
        self._running = False
        self.stop_all_streams()
        self._upload_executor.shutdown(wait=False)
        self._http_session.close()


# Teste standalone
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    
    import sys
    
    server_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
    
    bridge = HLSRelayBridge(relay_server_url=server_url)
    
    if not bridge.is_ffmpeg_available():
        print("❌ FFmpeg não encontrado!")
        exit(1)
    
    print(f"✓ FFmpeg disponível")
    print(f"📡 Servidor: {server_url}")
    print("\nDigite: start <stream_key> <rtsp_url>")
    print("       stop <stream_key>")
    print("       list")
    print("       quit")
    
    while True:
        try:
            cmd = input("\n> ").strip().split()
            if not cmd:
                continue
            
            if cmd[0] == "start" and len(cmd) >= 3:
                result = bridge.start_stream(cmd[1], cmd[2])
                print(result)
            elif cmd[0] == "stop" and len(cmd) >= 2:
                result = bridge.stop_stream(cmd[1])
                print(result)
            elif cmd[0] == "list":
                streams = bridge.get_all_streams()
                for s in streams:
                    print(f"  {s['stream_key']}: {s['status']} (sent: {s['segments_sent']})")
            elif cmd[0] == "quit":
                bridge.shutdown()
                break
            else:
                print("Comando inválido")
        except KeyboardInterrupt:
            bridge.shutdown()
            break
