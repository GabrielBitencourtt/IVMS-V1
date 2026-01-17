#!/usr/bin/env python3
"""
WebSocket Producer - Envia stream H.264 para servidor via WebSocket

Captura RTSP local e envia pacotes H.264 para o servidor Railway,
que faz broadcast para browsers conectados.

Latência esperada: ~1-2 segundos (vs 3-5s do HLS)
"""

import subprocess
import threading
import logging
import base64
import json
import time
import os
from typing import Optional, Callable
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class WebSocketStream:
    """Representa um stream ativo via WebSocket"""
    stream_key: str
    rtsp_url: str
    ws_url: str
    ffmpeg_process: subprocess.Popen
    ws_thread: threading.Thread
    started_at: datetime
    camera_name: str = ""
    status: str = "running"
    error_message: str = ""
    bytes_sent: int = 0


class WebSocketProducer:
    """
    Captura RTSP e envia H.264 via WebSocket para servidor relay.
    
    Fluxo:
    1. FFmpeg captura RTSP e converte para H.264 raw (stdout)
    2. Thread lê stdout do FFmpeg e envia via WebSocket
    3. Servidor Railway faz broadcast para browsers
    """
    
    def __init__(self, 
                 server_url: str = "wss://ivms-v1-production.up.railway.app",
                 on_status_change: Optional[Callable] = None,
                 ffmpeg_path: Optional[str] = None):
        self.server_url = server_url.rstrip('/')
        self.on_status_change = on_status_change
        self.active_streams: dict[str, WebSocketStream] = {}
        self._running = True
        self._ffmpeg_path: Optional[str] = ffmpeg_path
        
        # Se não foi passado, tenta encontrar
        if not self._ffmpeg_path:
            self._init_ffmpeg()
        else:
            logger.info(f"✓ WebSocket Producer usando FFmpeg: {self._ffmpeg_path}")
    
    def _init_ffmpeg(self):
        """Encontra FFmpeg no sistema"""
        import shutil
        
        self._ffmpeg_path = shutil.which("ffmpeg")
        
        if not self._ffmpeg_path:
            # Tentar caminhos comuns - incluindo diretório local do CameraScanner
            common_paths = [
                os.path.join(os.environ.get('LOCALAPPDATA', ''), 'CameraScanner', 'ffmpeg', 'bin', 'ffmpeg.exe'),
                r"C:\Users\gbdes\AppData\Local\CameraScanner\ffmpeg\bin\ffmpeg.exe",
                r"C:\ffmpeg\bin\ffmpeg.exe",
                r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
                "/usr/bin/ffmpeg",
                "/usr/local/bin/ffmpeg",
                "/opt/homebrew/bin/ffmpeg",
            ]
            
            for path in common_paths:
                if path and os.path.isfile(path):
                    self._ffmpeg_path = path
                    break
        
        if self._ffmpeg_path:
            logger.info(f"✓ FFmpeg encontrado: {self._ffmpeg_path}")
        else:
            logger.error("❌ FFmpeg não encontrado!")
    
    def is_available(self) -> bool:
        """Verifica se FFmpeg está disponível"""
        return self._ffmpeg_path is not None
    
    def _build_ffmpeg_command(self, rtsp_url: str) -> list:
        """
        Constrói comando FFmpeg para streaming via WebSocket.
        
        Gera H.264 Annex B (NAL units) que o JMuxer consegue processar.
        """
        cmd = [
            self._ffmpeg_path,
            # Opções de entrada
            "-fflags", "+genpts+discardcorrupt",
            "-rtsp_transport", "tcp",
            "-timeout", "5000000",
            "-analyzeduration", "500000",
            "-probesize", "500000",
            "-i", rtsp_url,
            # Recodificar para H.264 Baseline (máxima compatibilidade)
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-profile:v", "baseline",
            "-level", "3.1",
            "-pix_fmt", "yuv420p",
            "-b:v", "2000k",
            "-maxrate", "2500k",
            "-bufsize", "1000k",
            "-g", "30",                  # Keyframe a cada 1 segundo
            "-keyint_min", "30",
            "-sc_threshold", "0",
            "-an",
            # Output H.264 raw (Annex B format com start codes)
            "-f", "h264",
            "-bsf:v", "h264_mp4toannexb",
            "pipe:1"
        ]
        
        return cmd
    
    def start_stream(self, stream_key: str, rtsp_url: str, camera_name: str = "") -> dict:
        """
        Inicia streaming de uma câmera via WebSocket.
        
        Args:
            stream_key: Identificador único do stream
            rtsp_url: URL RTSP da câmera
            camera_name: Nome da câmera (opcional)
        
        Returns:
            Dict com status da operação
        """
        if not self._ffmpeg_path:
            return {"success": False, "error": "FFmpeg não disponível"}
        
        if stream_key in self.active_streams:
            existing = self.active_streams[stream_key]
            if existing.ffmpeg_process.poll() is None:
                return {"success": False, "error": "Stream já está ativo"}
        
        # WebSocket URL para este stream
        ws_url = f"{self.server_url}/ws/produce/{stream_key}"
        
        logger.info(f"🎬 Iniciando stream WebSocket: {stream_key}")
        logger.info(f"   RTSP: {rtsp_url}")
        logger.info(f"   WS: {ws_url}")
        
        try:
            # Iniciar FFmpeg
            cmd = self._build_ffmpeg_command(rtsp_url)
            logger.info(f"   CMD: {' '.join(cmd[:5])}...")
            
            ffmpeg_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0  # Unbuffered para baixa latência
            )
            
            # Aguardar um pouco para ver se não crashou
            time.sleep(0.5)
            
            if ffmpeg_process.poll() is not None:
                stderr = ffmpeg_process.stderr.read().decode('utf-8', errors='ignore')
                return {"success": False, "error": f"FFmpeg falhou: {stderr[-200:]}"}
            
            # Iniciar thread de envio WebSocket
            ws_thread = threading.Thread(
                target=self._websocket_sender,
                args=(stream_key, ffmpeg_process, ws_url),
                daemon=True
            )
            ws_thread.start()
            
            # Registrar stream
            stream_info = WebSocketStream(
                stream_key=stream_key,
                rtsp_url=rtsp_url,
                ws_url=ws_url,
                ffmpeg_process=ffmpeg_process,
                ws_thread=ws_thread,
                started_at=datetime.now(),
                camera_name=camera_name,
                status="running"
            )
            
            self.active_streams[stream_key] = stream_info
            
            if self.on_status_change:
                self.on_status_change(stream_key, "running", "")
            
            return {
                "success": True,
                "stream_key": stream_key,
                "ws_url": ws_url.replace("wss://", "").replace("ws://", ""),
                "mode": "websocket"
            }
            
        except Exception as e:
            logger.error(f"❌ Erro ao iniciar stream: {e}")
            return {"success": False, "error": str(e)}
    
    def _websocket_sender(self, stream_key: str, ffmpeg_process: subprocess.Popen, ws_url: str):
        """
        Thread que lê dados do FFmpeg e envia via WebSocket.
        Mantém conexão persistente com reconexão automática.
        """
        try:
            import websocket
        except ImportError:
            logger.error("❌ Biblioteca websocket-client não instalada!")
            logger.error("   Execute: pip install websocket-client")
            if stream_key in self.active_streams:
                self.active_streams[stream_key].status = "error"
                self.active_streams[stream_key].error_message = "websocket-client não instalado"
            return
        
        import select
        import sys
        
        ws = None
        reconnect_delay = 1
        max_reconnect_delay = 10
        bytes_sent_log = 0
        last_data_time = time.time()
        ping_interval = 25
        last_ping_time = time.time()
        connection_established = False
        
        # Aguardar FFmpeg produzir dados iniciais
        logger.info(f"⏳ Aguardando FFmpeg inicializar: {stream_key}")
        startup_timeout = 10  # 10 segundos para FFmpeg inicializar
        startup_start = time.time()
        
        while time.time() - startup_start < startup_timeout:
            if ffmpeg_process.poll() is not None:
                stderr_data = ffmpeg_process.stderr.read().decode('utf-8', errors='ignore')
                logger.error(f"❌ FFmpeg encerrou durante inicialização: {stderr_data[-300:]}")
                if stream_key in self.active_streams:
                    self.active_streams[stream_key].status = "error"
                    self.active_streams[stream_key].error_message = "FFmpeg falhou"
                return
            
            # Verificar se tem dados disponíveis (non-blocking check on Windows)
            # No Windows, select não funciona com pipes, então usamos peek
            if sys.platform == 'win32':
                import msvcrt
                import ctypes
                from ctypes import wintypes
                
                # Tentar ler um byte para ver se tem dados
                try:
                    # Usar PeekNamedPipe no Windows
                    kernel32 = ctypes.windll.kernel32
                    handle = msvcrt.get_osfhandle(ffmpeg_process.stdout.fileno())
                    avail = ctypes.c_ulong(0)
                    result = kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(avail), None)
                    if result and avail.value > 0:
                        logger.info(f"✅ FFmpeg produzindo dados ({avail.value} bytes disponíveis)")
                        break
                except Exception as e:
                    # Se falhar, continua esperando
                    pass
            else:
                # Unix - usar select
                readable, _, _ = select.select([ffmpeg_process.stdout], [], [], 0.1)
                if readable:
                    logger.info(f"✅ FFmpeg produzindo dados")
                    break
            
            time.sleep(0.5)
        else:
            # Timeout - mas vamos continuar tentando mesmo assim
            logger.warning(f"⚠️ Timeout esperando FFmpeg, tentando continuar...")
        
        while self._running and stream_key in self.active_streams:
            try:
                # Verificar se FFmpeg ainda está rodando
                if ffmpeg_process.poll() is not None:
                    logger.warning(f"⚠️ FFmpeg encerrou para {stream_key}")
                    break
                
                # Conectar ao servidor
                if ws is None:
                    logger.info(f"🔌 Conectando ao servidor WebSocket: {ws_url}")
                    
                    ws = websocket.create_connection(
                        ws_url,
                        timeout=30,
                        skip_utf8_validation=True,
                        ping_interval=0,
                        ping_timeout=None
                    )
                    logger.info(f"✅ WebSocket PRODUCER conectado: {stream_key}")
                    reconnect_delay = 1
                    last_ping_time = time.time()
                    connection_established = True
                
                # Ler dados do FFmpeg e enviar
                chunk_size = 8192  # 8KB chunks
                
                while self._running and stream_key in self.active_streams:
                    # Verificar se FFmpeg ainda está rodando
                    if ffmpeg_process.poll() is not None:
                        logger.warning(f"⚠️ FFmpeg encerrou para {stream_key}")
                        break
                    
                    # Enviar ping periodicamente
                    current_time = time.time()
                    if current_time - last_ping_time > ping_interval:
                        try:
                            ws.ping()
                            last_ping_time = current_time
                        except Exception as ping_err:
                            logger.warning(f"⚠️ Ping falhou: {ping_err}")
                            break
                    
                    # Ler chunk do stdout (bloqueante, mas FFmpeg deve estar produzindo)
                    try:
                        data = ffmpeg_process.stdout.read(chunk_size)
                    except Exception as read_err:
                        logger.warning(f"⚠️ Erro ao ler FFmpeg: {read_err}")
                        break
                    
                    if not data:
                        if time.time() - last_data_time > 5:
                            logger.warning(f"⚠️ Sem dados do FFmpeg por 5s: {stream_key}")
                            last_data_time = time.time()
                        time.sleep(0.01)
                        continue
                    
                    last_data_time = time.time()
                    
                    # Enviar dados binários
                    try:
                        ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
                    except websocket.WebSocketConnectionClosedException:
                        logger.warning(f"⚠️ Conexão fechada ao enviar dados")
                        break
                    except Exception as send_err:
                        logger.warning(f"⚠️ Erro ao enviar: {send_err}")
                        break
                    
                    # Atualizar contador
                    if stream_key in self.active_streams:
                        self.active_streams[stream_key].bytes_sent += len(data)
                        bytes_sent_log += len(data)
                        
                        if bytes_sent_log >= 1024 * 1024:
                            logger.info(f"📤 {stream_key}: {self.active_streams[stream_key].bytes_sent / 1024 / 1024:.1f} MB enviados")
                            bytes_sent_log = 0
                
            except websocket.WebSocketConnectionClosedException:
                logger.warning(f"⚠️ WebSocket desconectado: {stream_key}")
            except websocket.WebSocketTimeoutException:
                logger.warning(f"⚠️ WebSocket timeout: {stream_key}")
            except ConnectionRefusedError:
                logger.warning(f"⚠️ Conexão recusada pelo servidor: {stream_key}")
            except Exception as e:
                logger.error(f"❌ Erro WebSocket ({stream_key}): {e}")
            finally:
                if ws:
                    try:
                        ws.close()
                    except:
                        pass
                    ws = None
            
            # Reconectar se stream ainda ativo
            if self._running and stream_key in self.active_streams:
                # Verificar novamente se FFmpeg está rodando antes de reconectar
                if ffmpeg_process.poll() is not None:
                    logger.warning(f"⚠️ FFmpeg não está mais rodando, encerrando sender")
                    break
                logger.info(f"🔄 Reconectando em {reconnect_delay}s...")
                time.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 1.5, max_reconnect_delay)
        
        logger.info(f"🛑 WebSocket sender encerrado: {stream_key}")
    
    def stop_stream(self, stream_key: str) -> dict:
        """Para um stream ativo"""
        if stream_key not in self.active_streams:
            return {"success": False, "error": "Stream não encontrado"}
        
        stream = self.active_streams[stream_key]
        
        try:
            # Parar FFmpeg
            stream.ffmpeg_process.terminate()
            try:
                stream.ffmpeg_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                stream.ffmpeg_process.kill()
            
            # Thread vai parar automaticamente
            stream.status = "stopped"
            del self.active_streams[stream_key]
            
            if self.on_status_change:
                self.on_status_change(stream_key, "stopped", "")
            
            return {"success": True, "stream_key": stream_key}
            
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def stop_all(self):
        """Para todos os streams"""
        self._running = False
        for key in list(self.active_streams.keys()):
            self.stop_stream(key)
    
    def get_stream_status(self, stream_key: str) -> Optional[dict]:
        """Retorna status de um stream"""
        if stream_key not in self.active_streams:
            return None
        
        stream = self.active_streams[stream_key]
        is_running = stream.ffmpeg_process.poll() is None
        
        return {
            "stream_key": stream_key,
            "camera_name": stream.camera_name,
            "rtsp_url": stream.rtsp_url,
            "ws_url": stream.ws_url,
            "status": "running" if is_running else stream.status,
            "started_at": stream.started_at.isoformat(),
            "bytes_sent": stream.bytes_sent,
            "mode": "websocket"
        }
    
    def get_all_streams(self) -> list:
        """Retorna lista de todos os streams ativos"""
        return [self.get_stream_status(key) for key in self.active_streams]


# Teste standalone
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    
    producer = WebSocketProducer(
        server_url="wss://your-railway-server.up.railway.app"
    )
    
    if not producer.is_available():
        print("❌ FFmpeg não encontrado!")
        exit(1)
    
    print("✓ WebSocket Producer pronto")
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
                result = producer.start_stream(cmd[1], cmd[2])
                print(result)
            elif cmd[0] == "stop" and len(cmd) >= 2:
                result = producer.stop_stream(cmd[1])
                print(result)
            elif cmd[0] == "list":
                streams = producer.get_all_streams()
                for s in streams:
                    print(f"  {s['stream_key']}: {s['status']} ({s['bytes_sent']} bytes)")
            elif cmd[0] == "quit":
                producer.stop_all()
                break
            else:
                print("Comando inválido")
        except KeyboardInterrupt:
            producer.stop_all()
            break
