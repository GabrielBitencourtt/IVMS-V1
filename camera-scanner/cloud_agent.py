#!/usr/bin/env python3
"""
IVMS Cloud Agent - Agente local que conecta ao backend cloud
Este agente roda na rede do cliente e se comunica com o cloud via HTTPS/WSS
"""

import os
import sys
import json
import time
import socket
import platform
import threading
import subprocess
import logging
import signal
from datetime import datetime
from typing import Optional, Dict, Any, Callable, List
import argparse

# Dependências
try:
    import requests
except ImportError:
    print("❌ Instale requests: pip install requests")
    sys.exit(1)

try:
    from supabase import create_client, Client
except ImportError:
    print("❌ Instale supabase: pip install supabase")
    sys.exit(1)

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


class StreamProcess:
    """Representa um processo de streaming ativo"""
    def __init__(self, stream_key: str, rtsp_url: str, camera_name: str = ""):
        self.stream_key = stream_key
        self.rtsp_url = rtsp_url
        self.camera_name = camera_name
        self.process: Optional[subprocess.Popen] = None
        self.started_at: Optional[datetime] = None
        self.status: str = "stopped"  # stopped, starting, running, error
        self.error: Optional[str] = None


class CloudAgent:
    """
    Agente que conecta ao backend cloud IVMS
    - Autentica via device token
    - Envia heartbeats periódicos
    - Recebe comandos do cloud
    - Gerencia streams RTSP → RTMP
    """
    
    def __init__(
        self,
        cloud_url: str,
        device_token: str,
        rtmp_url: str = "rtmp://localhost/live",
        heartbeat_interval: int = 10,  # Reduced from 30s for faster command processing
    ):
        self.cloud_url = cloud_url.rstrip('/')
        self.device_token = device_token
        self.rtmp_url = rtmp_url
        self.heartbeat_interval = heartbeat_interval
        
        # Estado
        self.agent_id: Optional[str] = None
        self.client_id: Optional[str] = None
        self.user_id: Optional[str] = None
        self.supabase: Optional[Client] = None
        self.supabase_url: Optional[str] = None
        self.supabase_key: Optional[str] = None
        
        # Streams ativos
        self.streams: Dict[str, StreamProcess] = {}
        self.streams_lock = threading.Lock()
        
        # FFmpeg
        self.ffmpeg_path = self._find_ffmpeg()
        self.ffmpeg_available = self.ffmpeg_path is not None
        
        # Sistema
        self.hostname = socket.gethostname()
        self.local_ip = self._get_local_ip()
        self.os_info = f"{platform.system()} {platform.release()}"
        self.network_range = self._get_network_range()
        
        # Controle
        self.running = False
        self.heartbeat_thread: Optional[threading.Thread] = None
        self.monitor_thread: Optional[threading.Thread] = None
        self.realtime_channel = None
        
        logger.info(f"🖥️  Hostname: {self.hostname}")
        logger.info(f"🌐 IP Local: {self.local_ip}")
        logger.info(f"💻 Sistema: {self.os_info}")
        logger.info(f"🎬 FFmpeg: {'✓ Disponível' if self.ffmpeg_available else '✗ Não encontrado'}")
    
    def _find_ffmpeg(self) -> Optional[str]:
        """Encontra o executável do FFmpeg"""
        # Tenta encontrar no PATH
        try:
            result = subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0:
                return "ffmpeg"
        except:
            pass
        
        # Tenta caminhos comuns
        common_paths = [
            "/usr/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "C:\\ffmpeg\\bin\\ffmpeg.exe",
            os.path.expanduser("~/.local/bin/ffmpeg"),
        ]
        
        for path in common_paths:
            if os.path.isfile(path):
                return path
        
        return None
    
    def _get_local_ip(self) -> str:
        """Obtém o IP local da máquina"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"
    
    def _get_network_range(self) -> str:
        """Estima o range da rede local"""
        try:
            ip_parts = self.local_ip.split('.')
            return f"{ip_parts[0]}.{ip_parts[1]}.{ip_parts[2]}.0/24"
        except:
            return "192.168.1.0/24"
    
    def register(self) -> bool:
        """Registra o agente no cloud"""
        logger.info("📡 Registrando agente no cloud...")
        
        try:
            response = requests.post(
                f"{self.cloud_url}/functions/v1/agent-register",
                headers={
                    "Content-Type": "application/json",
                    "x-device-token": self.device_token,
                },
                json={
                    "hostname": self.hostname,
                    "local_ip": self.local_ip,
                    "os_info": self.os_info,
                    "ffmpeg_installed": self.ffmpeg_available,
                    "network_range": self.network_range,
                },
                timeout=30,
            )
            
            if response.status_code != 200:
                logger.error(f"❌ Falha no registro: {response.status_code} - {response.text}")
                return False
            
            data = response.json()
            
            if not data.get("success"):
                logger.error(f"❌ Registro rejeitado: {data.get('error')}")
                return False
            
            self.agent_id = data["agent_id"]
            self.client_id = data["client_id"]
            self.user_id = data["user_id"]
            self.supabase_url = data["supabase_url"]
            self.supabase_key = data["supabase_anon_key"]
            
            logger.info(f"✅ Registrado como {data.get('agent_name', 'Agent')}")
            logger.info(f"   Agent ID: {self.agent_id}")
            logger.info(f"   User ID: {self.user_id}")
            
            # Inicializa cliente Supabase para Realtime
            self.supabase = create_client(self.supabase_url, self.supabase_key)
            
            return True
            
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Erro de conexão: {e}")
            return False
    
    def send_heartbeat(self) -> List[Dict]:
        """Envia heartbeat e retorna comandos pendentes"""
        try:
            # Coleta status das câmeras ativas
            camera_statuses = []
            with self.streams_lock:
                for stream in self.streams.values():
                    camera_statuses.append({
                        "stream_key": stream.stream_key,
                        "status": stream.status,
                        "error_message": stream.error,
                    })
            
            response = requests.post(
                f"{self.cloud_url}/functions/v1/agent-heartbeat",
                headers={
                    "Content-Type": "application/json",
                    "x-device-token": self.device_token,
                },
                json={
                    "client_id": self.client_id,
                    "active_streams": len([s for s in self.streams.values() if s.status == "running"]),
                    "camera_statuses": camera_statuses,
                },
                timeout=15,
            )
            
            if response.status_code == 200:
                data = response.json()
                return data.get("pending_commands", [])
            else:
                logger.warning(f"⚠️ Heartbeat falhou: {response.status_code}")
                return []
                
        except Exception as e:
            logger.error(f"❌ Erro no heartbeat: {e}")
            return []
    
    def send_command_result(
        self,
        command_id: str,
        status: str,
        result: Optional[Dict] = None,
        error_message: Optional[str] = None
    ):
        """Envia resultado de um comando para o cloud"""
        try:
            requests.post(
                f"{self.cloud_url}/functions/v1/agent-command-result",
                headers={
                    "Content-Type": "application/json",
                    "x-device-token": self.device_token,
                },
                json={
                    "command_id": command_id,
                    "status": status,
                    "result": result,
                    "error_message": error_message,
                },
                timeout=15,
            )
        except Exception as e:
            logger.error(f"❌ Erro ao enviar resultado: {e}")
    
    def process_command(self, command: Dict):
        """Processa um comando recebido do cloud"""
        cmd_id = command["id"]
        cmd_type = command["command_type"]
        payload = command.get("payload", {})
        
        logger.info(f"📥 Comando recebido: {cmd_type}")
        
        try:
            if cmd_type == "start_stream":
                result = self._handle_start_stream(payload)
                self.send_command_result(cmd_id, "completed", result)
                
            elif cmd_type == "stop_stream":
                result = self._handle_stop_stream(payload)
                self.send_command_result(cmd_id, "completed", result)
                
            elif cmd_type == "test_rtsp":
                result = self._handle_test_rtsp(payload)
                self.send_command_result(cmd_id, "completed", result)
                
            elif cmd_type == "scan_network":
                result = self._handle_scan_network(payload)
                self.send_command_result(cmd_id, "completed", result)
                
            elif cmd_type == "get_status":
                result = self._handle_get_status()
                self.send_command_result(cmd_id, "completed", result)
                
            else:
                self.send_command_result(
                    cmd_id, 
                    "failed", 
                    error_message=f"Comando desconhecido: {cmd_type}"
                )
                
        except Exception as e:
            logger.error(f"❌ Erro ao processar comando: {e}")
            self.send_command_result(cmd_id, "failed", error_message=str(e))
    
    def _handle_start_stream(self, payload: Dict) -> Dict:
        """Inicia um stream RTSP → RTMP"""
        stream_key = payload.get("stream_key")
        rtsp_url = payload.get("rtsp_url")
        camera_name = payload.get("camera_name", "")
        
        if not stream_key or not rtsp_url:
            return {"success": False, "error": "stream_key e rtsp_url são obrigatórios"}
        
        if not self.ffmpeg_available:
            return {"success": False, "error": "FFmpeg não disponível"}
        
        with self.streams_lock:
            if stream_key in self.streams:
                return {"success": False, "error": "Stream já existe"}
            
            stream = StreamProcess(stream_key, rtsp_url, camera_name)
            stream.status = "starting"
            self.streams[stream_key] = stream
        
        # Inicia FFmpeg em thread separada
        def start_ffmpeg():
            rtmp_output = f"{self.rtmp_url}/{stream_key}"
            
            cmd = [
                self.ffmpeg_path,
                "-rtsp_transport", "tcp",
                "-i", rtsp_url,
                "-c:v", "copy",
                "-c:a", "aac",
                "-f", "flv",
                "-flvflags", "no_duration_filesize",
                rtmp_output,
            ]
            
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                
                with self.streams_lock:
                    if stream_key in self.streams:
                        self.streams[stream_key].process = process
                        self.streams[stream_key].status = "running"
                        self.streams[stream_key].started_at = datetime.now()
                
                logger.info(f"✅ Stream {stream_key} iniciado")
                
            except Exception as e:
                logger.error(f"❌ Erro ao iniciar stream: {e}")
                with self.streams_lock:
                    if stream_key in self.streams:
                        self.streams[stream_key].status = "error"
                        self.streams[stream_key].error = str(e)
        
        threading.Thread(target=start_ffmpeg, daemon=True).start()
        
        return {"success": True, "stream_key": stream_key}
    
    def _handle_stop_stream(self, payload: Dict) -> Dict:
        """Para um stream ativo"""
        stream_key = payload.get("stream_key")
        
        if not stream_key:
            return {"success": False, "error": "stream_key é obrigatório"}
        
        with self.streams_lock:
            if stream_key not in self.streams:
                return {"success": False, "error": "Stream não encontrado"}
            
            stream = self.streams[stream_key]
            if stream.process:
                stream.process.terminate()
                try:
                    stream.process.wait(timeout=5)
                except:
                    stream.process.kill()
            
            del self.streams[stream_key]
        
        logger.info(f"🛑 Stream {stream_key} parado")
        return {"success": True, "stream_key": stream_key}
    
    def _handle_test_rtsp(self, payload: Dict) -> Dict:
        """Testa conexão RTSP usando socket direto com suporte a Digest Auth"""
        rtsp_url = payload.get("rtsp_url")
        
        if not rtsp_url:
            return {"success": False, "error": "rtsp_url é obrigatório"}
        
        start_time = time.time()
        
        try:
            # Usa o módulo de teste RTSP com suporte a Digest Auth
            from rtsp_tester import test_rtsp_connection
            
            success, message, details = test_rtsp_connection(rtsp_url, timeout=10)
            response_time = int((time.time() - start_time) * 1000)
            
            if success:
                return {
                    "success": True,
                    "message": message,
                    "rtsp_url": rtsp_url,
                    "response_time_ms": response_time,
                    "requires_auth": details.get("requires_auth", False),
                    "auth_type": details.get("auth_type"),
                    "codec": details.get("codec"),
                }
            else:
                return {
                    "success": False,
                    "rtsp_url": rtsp_url,
                    "error": message,
                    "requires_auth": details.get("requires_auth", False),
                }
                
        except Exception as e:
            logger.error(f"Erro ao testar RTSP: {e}")
            return {
                "success": False,
                "rtsp_url": rtsp_url,
                "error": str(e),
            }
    
    def _handle_scan_network(self, payload: Dict) -> Dict:
        """Escaneia a rede em busca de câmeras (simplificado)"""
        # TODO: Implementar scanner de rede completo
        return {
            "success": True,
            "network_range": self.network_range,
            "message": "Scan de rede não implementado nesta versão",
        }
    
    def _handle_get_status(self) -> Dict:
        """Retorna status atual do agente"""
        with self.streams_lock:
            streams_info = [
                {
                    "stream_key": s.stream_key,
                    "camera_name": s.camera_name,
                    "status": s.status,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "error": s.error,
                }
                for s in self.streams.values()
            ]
        
        return {
            "hostname": self.hostname,
            "local_ip": self.local_ip,
            "os_info": self.os_info,
            "ffmpeg_available": self.ffmpeg_available,
            "active_streams": len([s for s in streams_info if s["status"] == "running"]),
            "streams": streams_info,
        }
    
    def _heartbeat_loop(self):
        """Loop de heartbeat"""
        while self.running:
            commands = self.send_heartbeat()
            
            # Processa comandos recebidos
            for cmd in commands:
                try:
                    self.process_command(cmd)
                except Exception as e:
                    logger.error(f"Erro ao processar comando: {e}")
            
            time.sleep(self.heartbeat_interval)
    
    def _monitor_streams(self):
        """Monitora saúde dos streams"""
        while self.running:
            with self.streams_lock:
                for stream_key, stream in list(self.streams.items()):
                    if stream.process and stream.status == "running":
                        retcode = stream.process.poll()
                        if retcode is not None:
                            logger.warning(f"⚠️ Stream {stream_key} terminou (code: {retcode})")
                            stream.status = "error"
                            stream.error = f"FFmpeg terminou com código {retcode}"
            
            time.sleep(5)
    
    def start(self):
        """Inicia o agente"""
        logger.info("🚀 Iniciando IVMS Cloud Agent...")
        
        # Registra no cloud
        if not self.register():
            logger.error("❌ Falha no registro. Encerrando.")
            return False
        
        self.running = True
        
        # Inicia threads
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()
        
        self.monitor_thread = threading.Thread(target=self._monitor_streams, daemon=True)
        self.monitor_thread.start()
        
        logger.info("✅ Agente iniciado e conectado ao cloud!")
        logger.info(f"📡 Heartbeat a cada {self.heartbeat_interval}s")
        
        return True
    
    def stop(self):
        """Para o agente"""
        logger.info("🛑 Parando agente...")
        self.running = False
        
        # Para todos os streams
        with self.streams_lock:
            for stream_key in list(self.streams.keys()):
                self._handle_stop_stream({"stream_key": stream_key})
        
        logger.info("👋 Agente encerrado")
    
    def run_forever(self):
        """Executa o agente indefinidamente"""
        if not self.start():
            return
        
        # Configura handler de sinal
        def signal_handler(sig, frame):
            self.stop()
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Mantém rodando
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()


def main():
    parser = argparse.ArgumentParser(description="IVMS Cloud Agent")
    parser.add_argument(
        "--cloud-url",
        default=os.environ.get("CLOUD_URL", "https://cedkflgtubaologqjker.supabase.co"),
        help="URL do backend cloud"
    )
    parser.add_argument(
        "--device-token",
        default=os.environ.get("DEVICE_TOKEN"),
        help="Token de autenticação do dispositivo"
    )
    parser.add_argument(
        "--rtmp-url",
        default=os.environ.get("RTMP_URL", "rtmp://localhost/live"),
        help="URL do servidor RTMP"
    )
    parser.add_argument(
        "--heartbeat",
        type=int,
        default=30,
        help="Intervalo de heartbeat em segundos"
    )
    
    args = parser.parse_args()
    
    if not args.device_token:
        print("❌ Device token é obrigatório!")
        print("   Use: --device-token TOKEN")
        print("   Ou defina a variável DEVICE_TOKEN")
        sys.exit(1)
    
    agent = CloudAgent(
        cloud_url=args.cloud_url,
        device_token=args.device_token,
        rtmp_url=args.rtmp_url,
        heartbeat_interval=args.heartbeat,
    )
    
    agent.run_forever()


if __name__ == "__main__":
    main()