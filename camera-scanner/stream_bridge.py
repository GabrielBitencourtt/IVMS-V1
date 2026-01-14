#!/usr/bin/env python3
"""
Stream Bridge - Ponte para streaming de câmeras locais para o servidor RTMP
Captura RTSP da rede local e envia via RTMP para o servidor na nuvem
"""

import subprocess
import threading
import logging
import shutil
import os
from typing import Dict, Optional, Callable, Tuple
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class StreamProcess:
    """Representa um processo de streaming ativo"""
    stream_key: str
    rtsp_url: str
    rtmp_url: str
    process: subprocess.Popen
    started_at: datetime
    camera_name: str = ""
    status: str = "running"
    error_message: str = ""


class StreamBridge:
    """
    Gerencia múltiplos streams simultâneos.
    Captura RTSP local → Envia RTMP para servidor na nuvem.
    Instala FFmpeg automaticamente se necessário.
    """
    
    def __init__(self, 
                 rtmp_server_url: str = "rtmp://hopper.proxy.rlwy.net:46960/live",
                 on_status_change: Optional[Callable] = None,
                 on_ffmpeg_progress: Optional[Callable] = None):
        self.rtmp_server_url = rtmp_server_url
        self.on_status_change = on_status_change
        self.on_ffmpeg_progress = on_ffmpeg_progress
        self.active_streams: Dict[str, StreamProcess] = {}
        self._ffmpeg_path: Optional[str] = None
        self._ffmpeg_available = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._running = True
        
        # Inicializa FFmpeg (verifica/instala)
        self._init_ffmpeg()
        
        # Inicia thread de monitoramento
        self._start_monitor()
    
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
                logger.error("❌ FFmpeg não disponível e não foi possível instalar")
                
        except ImportError:
            # Fallback se o instalador não estiver disponível
            logger.warning("ffmpeg_installer não encontrado, usando busca simples")
            self._ffmpeg_path = self._find_ffmpeg_simple()
            self._ffmpeg_available = self._ffmpeg_path is not None
    
    def _find_ffmpeg_simple(self) -> Optional[str]:
        """Busca simples do FFmpeg (fallback)"""
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            return ffmpeg
        
        # Caminhos comuns
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
    
    def _start_monitor(self):
        """Inicia thread que monitora os processos de streaming"""
        def monitor():
            import time
            while self._running:
                self._check_streams()
                time.sleep(2)
        
        self._monitor_thread = threading.Thread(target=monitor, daemon=True)
        self._monitor_thread.start()
    
    def _check_streams(self):
        """Verifica o status de cada stream ativo"""
        for stream_key, stream in list(self.active_streams.items()):
            if stream.process.poll() is not None:
                # Processo terminou
                exit_code = stream.process.returncode
                
                if exit_code != 0:
                    # Lê stderr para diagnóstico
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
                
                # Notifica mudança de status
                if self.on_status_change:
                    self.on_status_change(stream_key, stream.status, stream.error_message)
    
    def start_stream(self, stream_key: str, rtsp_url: str, camera_name: str = "") -> Dict:
        """
        Inicia um novo stream.
        
        Args:
            stream_key: Chave única do stream (usada no RTMP)
            rtsp_url: URL RTSP da câmera local
            camera_name: Nome da câmera (opcional)
        
        Returns:
            Dict com status da operação
        """
        if stream_key in self.active_streams:
            existing = self.active_streams[stream_key]
            if existing.process.poll() is None:
                return {
                    "success": False,
                    "error": "Stream já está ativo",
                    "stream_key": stream_key
                }
        
        # Monta URL RTMP de destino
        rtmp_url = f"{self.rtmp_server_url}/{stream_key}"
        
        # Comando FFmpeg otimizado para LL-HLS (Low-Latency HLS)
        cmd = [
            self._ffmpeg_path,
            "-fflags", "+genpts+discardcorrupt+nobuffer+flush_packets",
            "-flags", "low_delay",
            "-avioflags", "direct",
            "-rtsp_transport", "tcp",
            "-timeout", "5000000",
            "-max_delay", "0",
            "-reorder_queue_size", "0",
            "-analyzeduration", "500000",         # Análise rápida (0.5s)
            "-probesize", "500000",               # Probe pequeno
            "-i", rtsp_url,
            "-c:v", "copy",
            "-an",                                # Sem áudio para menor latência
            "-f", "flv",
            "-flvflags", "no_duration_filesize",
            rtmp_url
        ]
        
        logger.info(f"Iniciando stream: {stream_key}")
        logger.debug(f"RTSP: {rtsp_url}")
        logger.debug(f"RTMP: {rtmp_url}")
        
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL
            )
            
            # Aguarda menos tempo - apenas para verificar se não crashou imediatamente
            import time
            time.sleep(0.5)
            
            if process.poll() is not None:
                # Processo já terminou - provavelmente erro
                stderr = process.stderr.read().decode('utf-8', errors='ignore')
                error_msg = self._parse_ffmpeg_error(stderr)
                
                return {
                    "success": False,
                    "error": error_msg,
                    "stream_key": stream_key
                }
            
            # Stream iniciado com sucesso
            stream_info = StreamProcess(
                stream_key=stream_key,
                rtsp_url=rtsp_url,
                rtmp_url=rtmp_url,
                process=process,
                started_at=datetime.now(),
                camera_name=camera_name,
                status="running"
            )
            
            self.active_streams[stream_key] = stream_info
            
            logger.info(f"✓ Stream {stream_key} iniciado com sucesso")
            
            if self.on_status_change:
                self.on_status_change(stream_key, "running", "")
            
            return {
                "success": True,
                "stream_key": stream_key,
                "rtmp_url": rtmp_url,
                "hls_url": f"/hls/{stream_key}/index.m3u8"
            }
            
        except FileNotFoundError:
            return {
                "success": False,
                "error": "FFmpeg não encontrado. Instale o FFmpeg.",
                "stream_key": stream_key
            }
        except Exception as e:
            logger.error(f"Erro ao iniciar stream: {e}")
            return {
                "success": False,
                "error": str(e),
                "stream_key": stream_key
            }
    
    def _parse_ffmpeg_error(self, stderr: str) -> str:
        """Extrai mensagem de erro amigável do stderr do FFmpeg"""
        stderr_lower = stderr.lower()
        
        if "connection refused" in stderr_lower:
            return "Conexão recusada - câmera offline ou IP incorreto"
        elif "connection timed out" in stderr_lower:
            return "Timeout - câmera não respondeu (verifique IP/porta)"
        elif "401 unauthorized" in stderr_lower or "authentication" in stderr_lower:
            return "Autenticação falhou - usuário/senha incorretos"
        elif "404" in stderr_lower or "not found" in stderr_lower:
            return "Stream não encontrado - verifique a URL RTSP"
        elif "invalid data" in stderr_lower:
            return "Dados inválidos - formato de stream não suportado"
        elif "no route to host" in stderr_lower:
            return "Câmera inacessível - verifique a rede"
        elif "already exists" in stderr_lower:
            return "Stream já existe no servidor"
        else:
            # Retorna últimas linhas relevantes
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
            # Envia SIGTERM para encerramento gracioso
            stream.process.terminate()
            
            # Aguarda até 5 segundos
            try:
                stream.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Força encerramento
                stream.process.kill()
            
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
            "rtmp_url": stream.rtmp_url,
            "status": "running" if is_running else stream.status,
            "started_at": stream.started_at.isoformat(),
            "error": stream.error_message
        }
    
    def get_all_streams(self) -> list:
        """Retorna lista de todos os streams ativos"""
        return [self.get_stream_status(key) for key in self.active_streams]
    
    def shutdown(self):
        """Encerra o bridge e todos os streams"""
        self._running = False
        self.stop_all_streams()
        
        if self._monitor_thread:
            self._monitor_thread.join(timeout=3)


# Teste standalone
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    
    bridge = StreamBridge()
    
    if not bridge.is_ffmpeg_available():
        print("❌ FFmpeg não encontrado!")
        exit(1)
    
    print("✓ FFmpeg disponível")
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
                    print(f"  {s['stream_key']}: {s['status']}")
            elif cmd[0] == "quit":
                bridge.shutdown()
                break
            else:
                print("Comando inválido")
        except KeyboardInterrupt:
            bridge.shutdown()
            break
