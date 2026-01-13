#!/usr/bin/env python3
"""
ONVIF Events Client - Recebe eventos de câmeras via ONVIF
Suporta Motion Detection, Analytics, Tampering, etc.
"""

import os
import sys
import time
import logging
import threading
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional, Dict, Any, Callable, List
from urllib.parse import urlparse
import socket
import hashlib
import base64
import secrets as py_secrets

try:
    import requests
except ImportError:
    print("❌ Instale requests: pip install requests")
    sys.exit(1)

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ONVIF Namespaces
NAMESPACES = {
    'soap': 'http://www.w3.org/2003/05/soap-envelope',
    'wsse': 'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd',
    'wsu': 'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd',
    'tev': 'http://www.onvif.org/ver10/events/wsdl',
    'wsnt': 'http://docs.oasis-open.org/wsn/b-2',
    'tns1': 'http://www.onvif.org/ver10/topics',
    'tt': 'http://www.onvif.org/ver10/schema',
}


class OnvifEvent:
    """Representa um evento ONVIF recebido"""
    def __init__(
        self,
        event_type: str,
        topic: str,
        source: str = "",
        data: Dict[str, Any] = None,
        timestamp: datetime = None,
        camera_ip: str = "",
        camera_name: str = "",
    ):
        self.event_type = event_type
        self.topic = topic
        self.source = source
        self.data = data or {}
        self.timestamp = timestamp or datetime.now()
        self.camera_ip = camera_ip
        self.camera_name = camera_name
    
    def to_dict(self) -> Dict:
        return {
            "event_type": self.event_type,
            "topic": self.topic,
            "source": self.source,
            "data": self.data,
            "timestamp": self.timestamp.isoformat(),
            "camera_ip": self.camera_ip,
            "camera_name": self.camera_name,
        }
    
    def __repr__(self):
        return f"OnvifEvent({self.event_type}, {self.topic}, {self.camera_ip})"


class OnvifAuth:
    """Gera autenticação WS-Security para ONVIF"""
    
    @staticmethod
    def create_wsse_header(username: str, password: str) -> str:
        """Cria header WS-Security com UsernameToken"""
        nonce = py_secrets.token_bytes(16)
        created = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')
        
        # Password Digest = Base64(SHA1(nonce + created + password))
        digest_input = nonce + created.encode('utf-8') + password.encode('utf-8')
        password_digest = base64.b64encode(hashlib.sha1(digest_input).digest()).decode('utf-8')
        nonce_b64 = base64.b64encode(nonce).decode('utf-8')
        
        return f'''
        <wsse:Security soap:mustUnderstand="1" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
            <wsse:UsernameToken>
                <wsse:Username>{username}</wsse:Username>
                <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{password_digest}</wsse:Password>
                <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_b64}</wsse:Nonce>
                <wsu:Created>{created}</wsu:Created>
            </wsse:UsernameToken>
        </wsse:Security>
        '''


class OnvifEventsClient:
    """
    Cliente ONVIF para receber eventos de câmeras
    Suporta:
    - Motion Detection
    - Video Analytics
    - Tampering
    - Video Source events
    """
    
    def __init__(
        self,
        camera_ip: str,
        camera_port: int = 80,
        username: str = "admin",
        password: str = "",
        camera_name: str = "",
        event_callback: Callable[[OnvifEvent], None] = None,
    ):
        self.camera_ip = camera_ip
        self.camera_port = camera_port
        self.username = username
        self.password = password
        self.camera_name = camera_name or f"Camera_{camera_ip}"
        self.event_callback = event_callback
        
        # URLs
        self.base_url = f"http://{camera_ip}:{camera_port}"
        self.events_url = f"{self.base_url}/onvif/Events"
        self.device_url = f"{self.base_url}/onvif/device_service"
        
        # Estado
        self.running = False
        self.poll_thread: Optional[threading.Thread] = None
        self.subscription_reference: Optional[str] = None
        self.event_capabilities: Dict = {}
        
        # Cache de eventos para detectar duplicados
        self._last_events: Dict[str, datetime] = {}
        self._event_cooldown = 2.0  # segundos entre eventos iguais
    
    def _send_soap_request(self, url: str, action: str, body: str) -> Optional[str]:
        """Envia requisição SOAP para a câmera"""
        wsse_header = OnvifAuth.create_wsse_header(self.username, self.password)
        
        envelope = f'''<?xml version="1.0" encoding="UTF-8"?>
        <soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
                       xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
                       xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2">
            <soap:Header>
                {wsse_header}
            </soap:Header>
            <soap:Body>
                {body}
            </soap:Body>
        </soap:Envelope>'''
        
        headers = {
            'Content-Type': 'application/soap+xml; charset=utf-8',
            'SOAPAction': action,
        }
        
        try:
            response = requests.post(url, data=envelope, headers=headers, timeout=10)
            if response.status_code == 200:
                return response.text
            else:
                logger.warning(f"SOAP request failed: {response.status_code}")
                return None
        except Exception as e:
            logger.error(f"SOAP request error: {e}")
            return None
    
    def check_capabilities(self) -> bool:
        """Verifica se a câmera suporta eventos ONVIF"""
        body = '''
            <tev:GetServiceCapabilities/>
        '''
        
        response = self._send_soap_request(
            self.events_url,
            'http://www.onvif.org/ver10/events/wsdl/EventPortType/GetServiceCapabilitiesRequest',
            body
        )
        
        if response:
            try:
                root = ET.fromstring(response)
                
                # Procura por capabilities
                caps = root.find('.//tev:Capabilities', NAMESPACES)
                if caps is not None:
                    self.event_capabilities = {
                        'basic_notification_interface': caps.get('WSBasicNotificationInterfaceSupport', 'false') == 'true',
                        'pull_point': caps.get('WSPullPointSupport', 'false') == 'true',
                        'persistent_notification': caps.get('WSPersistentNotificationInterfaceSupport', 'false') == 'true',
                    }
                    logger.info(f"📋 Capabilities: {self.event_capabilities}")
                    return True
            except ET.ParseError as e:
                logger.error(f"XML parse error: {e}")
        
        return False
    
    def create_pull_point_subscription(self) -> bool:
        """Cria subscription para receber eventos via pull"""
        body = '''
            <tev:CreatePullPointSubscription>
                <tev:InitialTerminationTime>PT1H</tev:InitialTerminationTime>
            </tev:CreatePullPointSubscription>
        '''
        
        response = self._send_soap_request(
            self.events_url,
            'http://www.onvif.org/ver10/events/wsdl/EventPortType/CreatePullPointSubscriptionRequest',
            body
        )
        
        if response:
            try:
                root = ET.fromstring(response)
                
                # Extrai SubscriptionReference
                sub_ref = root.find('.//tev:SubscriptionReference/wsnt:Address', NAMESPACES)
                if sub_ref is None:
                    sub_ref = root.find('.//{http://www.w3.org/2005/08/addressing}Address')
                
                if sub_ref is not None and sub_ref.text:
                    self.subscription_reference = sub_ref.text
                    logger.info(f"✅ Pull Point criado: {self.subscription_reference}")
                    return True
                    
            except ET.ParseError as e:
                logger.error(f"XML parse error: {e}")
        
        return False
    
    def pull_messages(self) -> List[OnvifEvent]:
        """Busca mensagens pendentes do pull point"""
        if not self.subscription_reference:
            return []
        
        body = '''
            <tev:PullMessages>
                <tev:Timeout>PT5S</tev:Timeout>
                <tev:MessageLimit>100</tev:MessageLimit>
            </tev:PullMessages>
        '''
        
        # Usa o subscription_reference como URL
        url = self.subscription_reference
        if not url.startswith('http'):
            url = f"{self.base_url}{url}"
        
        response = self._send_soap_request(
            url,
            'http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest',
            body
        )
        
        events = []
        
        if response:
            try:
                root = ET.fromstring(response)
                
                # Parse notification messages
                messages = root.findall('.//wsnt:NotificationMessage', NAMESPACES)
                
                for msg in messages:
                    event = self._parse_notification_message(msg)
                    if event:
                        events.append(event)
                        
            except ET.ParseError as e:
                logger.error(f"XML parse error: {e}")
        
        return events
    
    def _parse_notification_message(self, msg: ET.Element) -> Optional[OnvifEvent]:
        """Parse uma NotificationMessage ONVIF"""
        try:
            # Extrai Topic
            topic_elem = msg.find('.//wsnt:Topic', NAMESPACES)
            topic = topic_elem.text if topic_elem is not None else "Unknown"
            
            # Classifica o tipo de evento baseado no topic
            event_type = self._classify_event_type(topic)
            
            # Verifica cooldown para evitar spam de eventos
            event_key = f"{self.camera_ip}:{topic}"
            now = datetime.now()
            
            if event_key in self._last_events:
                elapsed = (now - self._last_events[event_key]).total_seconds()
                if elapsed < self._event_cooldown:
                    return None
            
            self._last_events[event_key] = now
            
            # Extrai dados do Message
            message_elem = msg.find('.//tt:Message', NAMESPACES)
            data = {}
            source = ""
            
            if message_elem is not None:
                # Source (ex: VideoSource, VideoAnalyticsConfiguration)
                source_elem = message_elem.find('.//tt:Source/tt:SimpleItem', NAMESPACES)
                if source_elem is not None:
                    source = source_elem.get('Value', '')
                
                # Data (ex: State = true/false)
                data_elem = message_elem.find('.//tt:Data/tt:SimpleItem', NAMESPACES)
                if data_elem is not None:
                    data[data_elem.get('Name', 'value')] = data_elem.get('Value', '')
            
            # Determina severidade
            severity = self._determine_severity(event_type, data)
            
            event = OnvifEvent(
                event_type=event_type,
                topic=topic,
                source=source,
                data=data,
                timestamp=now,
                camera_ip=self.camera_ip,
                camera_name=self.camera_name,
            )
            
            return event
            
        except Exception as e:
            logger.error(f"Error parsing notification: {e}")
            return None
    
    def _classify_event_type(self, topic: str) -> str:
        """Classifica o tipo de evento baseado no topic ONVIF"""
        topic_lower = topic.lower()
        
        if 'motion' in topic_lower:
            return 'motion_detection'
        elif 'tamper' in topic_lower:
            return 'tampering'
        elif 'analytics' in topic_lower:
            if 'linecross' in topic_lower or 'line' in topic_lower:
                return 'line_crossing'
            elif 'intrusion' in topic_lower or 'field' in topic_lower:
                return 'intrusion_detection'
            elif 'face' in topic_lower:
                return 'face_detection'
            elif 'object' in topic_lower:
                return 'object_detection'
            else:
                return 'analytics_event'
        elif 'videoloss' in topic_lower or 'video_loss' in topic_lower:
            return 'video_loss'
        elif 'disk' in topic_lower or 'storage' in topic_lower:
            return 'storage_event'
        elif 'alarm' in topic_lower:
            return 'alarm_input'
        elif 'connection' in topic_lower:
            return 'connection_event'
        else:
            return 'generic_event'
    
    def _determine_severity(self, event_type: str, data: Dict) -> str:
        """Determina a severidade do evento"""
        # Eventos críticos
        if event_type in ['tampering', 'video_loss']:
            return 'critical'
        
        # Eventos de alerta
        if event_type in ['intrusion_detection', 'line_crossing', 'alarm_input']:
            return 'warning'
        
        # Eventos informativos
        if event_type in ['motion_detection', 'face_detection', 'object_detection']:
            return 'info'
        
        return 'info'
    
    def _poll_loop(self):
        """Loop de polling para eventos"""
        logger.info(f"🔄 Iniciando poll loop para {self.camera_name}")
        
        while self.running:
            try:
                events = self.pull_messages()
                
                for event in events:
                    logger.info(f"📥 Evento: {event.event_type} de {event.camera_name}")
                    
                    if self.event_callback:
                        try:
                            self.event_callback(event)
                        except Exception as e:
                            logger.error(f"Error in event callback: {e}")
                
            except Exception as e:
                logger.error(f"Poll error: {e}")
            
            time.sleep(1)  # Poll a cada 1 segundo
    
    def start(self) -> bool:
        """Inicia a escuta de eventos"""
        logger.info(f"🎯 Conectando a {self.camera_name} ({self.camera_ip})")
        
        # Verifica capabilities
        if not self.check_capabilities():
            logger.warning(f"⚠️ Não foi possível verificar capabilities de {self.camera_name}")
        
        # Cria subscription
        if not self.create_pull_point_subscription():
            logger.error(f"❌ Falha ao criar subscription para {self.camera_name}")
            return False
        
        # Inicia thread de polling
        self.running = True
        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.poll_thread.start()
        
        return True
    
    def stop(self):
        """Para a escuta de eventos"""
        logger.info(f"🛑 Parando escuta de {self.camera_name}")
        self.running = False
        
        if self.poll_thread:
            self.poll_thread.join(timeout=5)


class OnvifEventsManager:
    """
    Gerencia múltiplos clientes ONVIF de eventos
    """
    
    def __init__(self, event_callback: Callable[[OnvifEvent], None] = None):
        self.clients: Dict[str, OnvifEventsClient] = {}
        self.event_callback = event_callback
        self.lock = threading.Lock()
    
    def add_camera(
        self,
        camera_ip: str,
        username: str = "admin",
        password: str = "",
        camera_name: str = "",
        camera_port: int = 80,
    ) -> bool:
        """Adiciona uma câmera para escuta de eventos"""
        with self.lock:
            if camera_ip in self.clients:
                logger.warning(f"Câmera {camera_ip} já está registrada")
                return False
            
            client = OnvifEventsClient(
                camera_ip=camera_ip,
                camera_port=camera_port,
                username=username,
                password=password,
                camera_name=camera_name,
                event_callback=self.event_callback,
            )
            
            if client.start():
                self.clients[camera_ip] = client
                return True
            
            return False
    
    def remove_camera(self, camera_ip: str):
        """Remove uma câmera da escuta"""
        with self.lock:
            if camera_ip in self.clients:
                self.clients[camera_ip].stop()
                del self.clients[camera_ip]
    
    def stop_all(self):
        """Para todos os clientes"""
        with self.lock:
            for client in self.clients.values():
                client.stop()
            self.clients.clear()
    
    def get_status(self) -> Dict:
        """Retorna status de todas as câmeras"""
        with self.lock:
            return {
                ip: {
                    "name": client.camera_name,
                    "running": client.running,
                    "subscription": client.subscription_reference is not None,
                }
                for ip, client in self.clients.items()
            }


# Teste local
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="ONVIF Events Client")
    parser.add_argument("--ip", required=True, help="IP da câmera")
    parser.add_argument("--port", type=int, default=80, help="Porta ONVIF")
    parser.add_argument("--user", default="admin", help="Usuário")
    parser.add_argument("--password", default="", help="Senha")
    
    args = parser.parse_args()
    
    def print_event(event: OnvifEvent):
        print(f"\n{'='*50}")
        print(f"🎬 EVENTO: {event.event_type}")
        print(f"   Topic: {event.topic}")
        print(f"   Câmera: {event.camera_name} ({event.camera_ip})")
        print(f"   Dados: {event.data}")
        print(f"   Hora: {event.timestamp}")
        print(f"{'='*50}")
    
    client = OnvifEventsClient(
        camera_ip=args.ip,
        camera_port=args.port,
        username=args.user,
        password=args.password,
        camera_name="Test Camera",
        event_callback=print_event,
    )
    
    if client.start():
        print(f"\n✅ Escutando eventos de {args.ip}...")
        print("Pressione Ctrl+C para parar\n")
        
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            client.stop()
            print("\n👋 Encerrado")
    else:
        print("❌ Falha ao iniciar cliente")
