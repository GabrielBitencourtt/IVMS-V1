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
    def create_wsse_header(username: str, password: str, use_password_text: bool = False) -> str:
        """Cria header WS-Security com UsernameToken
        
        Args:
            username: Nome de usuário
            password: Senha
            use_password_text: Se True, usa PasswordText (plaintext) ao invés de PasswordDigest
        """
        nonce = py_secrets.token_bytes(16)
        created = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')
        nonce_b64 = base64.b64encode(nonce).decode('utf-8')
        
        if use_password_text:
            # PasswordText - senha em texto plano (algumas câmeras Dahua/Intelbras preferem)
            return f'''
        <wsse:Security soap:mustUnderstand="1" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
            <wsse:UsernameToken>
                <wsse:Username>{username}</wsse:Username>
                <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText">{password}</wsse:Password>
                <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_b64}</wsse:Nonce>
                <wsu:Created>{created}</wsu:Created>
            </wsse:UsernameToken>
        </wsse:Security>
        '''
        else:
            # PasswordDigest = Base64(SHA1(nonce + created + password))
            digest_input = nonce + created.encode('utf-8') + password.encode('utf-8')
            password_digest = base64.b64encode(hashlib.sha1(digest_input).digest()).decode('utf-8')
            
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
    
    Mantém uma única conexão persistente durante a vida do cliente.
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
        
        # Método de autenticação que funcionou (None = ainda não testado)
        self._working_auth_method: Optional[str] = None
        # Formato de pull que funcionou
        self._working_pull_format: Optional[int] = None
        
        # Cache de eventos para detectar duplicados
        self._last_events: Dict[str, datetime] = {}
        self._event_cooldown = 2.0  # segundos entre eventos iguais
        
        # Timestamp da última renovação de subscription
        self._subscription_created_at: Optional[datetime] = None
        self._subscription_ttl_seconds = 540  # Renovar antes de expirar (600s - 60s margem)
        
        # Flag para evitar logs repetitivos
        self._connection_logged = False
        self._poll_error_count = 0
    
    def _send_soap_request(self, url: str, action: str, body: str, debug: bool = False, try_all_auth: bool = False) -> Optional[str]:
        """Envia requisição SOAP para a câmera
        
        Args:
            url: URL do serviço ONVIF
            action: SOAP action
            body: Corpo da requisição
            debug: Se True, loga detalhes
            try_all_auth: Se True, tenta múltiplos métodos de autenticação
        """
        headers = {
            'Content-Type': 'application/soap+xml; charset=utf-8',
            'SOAPAction': action,
        }
        
        # Gera MessageID único para WS-Addressing
        import uuid
        message_id = f"urn:uuid:{uuid.uuid4()}"
        
        # WS-Addressing headers (obrigatório para algumas câmeras)
        wsa_headers = f'''
            <wsa:MessageID>{message_id}</wsa:MessageID>
            <wsa:To>{url}</wsa:To>
            <wsa:Action>{action}</wsa:Action>
        '''
        
        # Define métodos de autenticação a tentar
        # Incluindo métodos combinados para câmeras Dahua/Intelbras
        all_methods = ['http_digest', 'http_digest_wsse', 'wsse_digest', 'wsse_text', 'no_auth']
        auth_methods = []
        
        if try_all_auth:
            # Se try_all_auth, tenta todos começando pelo que funcionou antes
            if self._working_auth_method:
                auth_methods = [self._working_auth_method] + [m for m in all_methods if m != self._working_auth_method]
            else:
                auth_methods = all_methods
        elif self._working_auth_method:
            # Se já sabemos qual funciona, usa apenas esse
            auth_methods = [self._working_auth_method]
        else:
            # Padrão: tenta HTTP digest primeiro (mais comum)
            auth_methods = ['http_digest', 'wsse_digest']
        
        for auth_method in auth_methods:
            try:
                # Log apenas se estiver explorando métodos (não em polling normal)
                if try_all_auth and not self._working_auth_method:
                    logger.info(f"🔐 Tentando autenticação: {auth_method}")
                
                if auth_method == 'http_digest':
                    # HTTP Digest Auth (comum em Intelbras/Dahua)
                    from requests.auth import HTTPDigestAuth
                    
                    envelope = f'''<?xml version="1.0" encoding="UTF-8"?>
                    <soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
                                   xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
                                   xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
                                   xmlns:wsa="http://www.w3.org/2005/08/addressing">
                        <soap:Header>
                            {wsa_headers}
                        </soap:Header>
                        <soap:Body>
                            {body}
                        </soap:Body>
                    </soap:Envelope>'''
                    
                    response = requests.post(
                        url, 
                        data=envelope, 
                        headers=headers, 
                        auth=HTTPDigestAuth(self.username, self.password),
                        timeout=10
                    )
                    
                elif auth_method == 'http_digest_wsse':
                    # HTTP Digest Auth + WSSE Header (câmeras Dahua/Intelbras para alguns endpoints)
                    from requests.auth import HTTPDigestAuth
                    wsse_header = OnvifAuth.create_wsse_header(self.username, self.password, use_password_text=False)
                    
                    envelope = f'''<?xml version="1.0" encoding="UTF-8"?>
                    <soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
                                   xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
                                   xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
                                   xmlns:wsa="http://www.w3.org/2005/08/addressing">
                        <soap:Header>
                            {wsa_headers}
                            {wsse_header}
                        </soap:Header>
                        <soap:Body>
                            {body}
                        </soap:Body>
                    </soap:Envelope>'''
                    
                    response = requests.post(
                        url, 
                        data=envelope, 
                        headers=headers, 
                        auth=HTTPDigestAuth(self.username, self.password),
                        timeout=10
                    )
                    
                elif auth_method == 'wsse_text':
                    # WS-Security com PasswordText
                    wsse_header = OnvifAuth.create_wsse_header(self.username, self.password, use_password_text=True)
                    envelope = f'''<?xml version="1.0" encoding="UTF-8"?>
                    <soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
                                   xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
                                   xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
                                   xmlns:wsa="http://www.w3.org/2005/08/addressing">
                        <soap:Header>
                            {wsa_headers}
                            {wsse_header}
                        </soap:Header>
                        <soap:Body>
                            {body}
                        </soap:Body>
                    </soap:Envelope>'''
                    response = requests.post(url, data=envelope, headers=headers, timeout=10)
                    
                elif auth_method == 'wsse_digest':
                    # WS-Security com PasswordDigest
                    wsse_header = OnvifAuth.create_wsse_header(self.username, self.password, use_password_text=False)
                    envelope = f'''<?xml version="1.0" encoding="UTF-8"?>
                    <soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
                                   xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
                                   xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
                                   xmlns:wsa="http://www.w3.org/2005/08/addressing">
                        <soap:Header>
                            {wsa_headers}
                            {wsse_header}
                        </soap:Header>
                        <soap:Body>
                            {body}
                        </soap:Body>
                    </soap:Envelope>'''
                    response = requests.post(url, data=envelope, headers=headers, timeout=10)
                    
                else:  # no_auth
                    # Sem autenticação (algumas câmeras permitem)
                    envelope = f'''<?xml version="1.0" encoding="UTF-8"?>
                    <soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
                                   xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
                                   xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
                                   xmlns:wsa="http://www.w3.org/2005/08/addressing">
                        <soap:Header>
                            {wsa_headers}
                        </soap:Header>
                        <soap:Body>
                            {body}
                        </soap:Body>
                    </soap:Envelope>'''
                    response = requests.post(url, data=envelope, headers=headers, timeout=10)
                
                if response.status_code == 200:
                    if try_all_auth and not self._working_auth_method:
                        self._working_auth_method = auth_method
                        logger.info(f"✅ Método de autenticação funcionou: {auth_method}")
                    return response.text
                elif response.status_code == 401:
                    logger.info(f"❌ Auth {auth_method} falhou: 401 Unauthorized")
                    continue
                else:
                    # Log detalhado do status code e resposta
                    logger.info(f"📥 Response {auth_method}: status={response.status_code}")
                    
                    # Verifica se é erro de autenticação no SOAP
                    is_auth_error = False
                    soap_error_msg = None
                    if response.text:
                        try:
                            root = ET.fromstring(response.text)
                            fault = root.find('.//{http://www.w3.org/2003/05/soap-envelope}Fault')
                            if fault is not None:
                                reason_elem = fault.find('.//{http://www.w3.org/2003/05/soap-envelope}Reason')
                                reason_text = ""
                                if reason_elem is not None:
                                    text_elem = reason_elem.find('.//{http://www.w3.org/2003/05/soap-envelope}Text')
                                    if text_elem is not None and text_elem.text:
                                        reason_text = text_elem.text
                                    elif reason_elem.text:
                                        reason_text = reason_elem.text
                                    else:
                                        reason_text = ET.tostring(reason_elem, encoding='unicode')
                                
                                soap_error_msg = reason_text
                                logger.info(f"📛 SOAP Fault ({auth_method}): {reason_text[:200]}")
                                
                                # Verifica se é erro de autenticação
                                auth_keywords = ['not authorized', 'password', 'authentication', 'credentials', 'unauthorized']
                                if any(kw in reason_text.lower() for kw in auth_keywords):
                                    is_auth_error = True
                                    continue  # Tenta próximo método de auth
                                else:
                                    # Outro tipo de erro SOAP - não é problema de auth
                                    # Se temos try_all_auth, pode ser que outro método funcione
                                    if try_all_auth:
                                        continue
                                    return None
                        except Exception as parse_err:
                            logger.debug(f"Erro ao parsear resposta: {parse_err}")
                            logger.info(f"📄 Response body: {response.text[:300]}")
                    
                    if is_auth_error:
                        continue
                    elif soap_error_msg is None and try_all_auth:
                        # Não conseguiu parsear mas estamos tentando todos, continua
                        logger.info(f"⚠️ Resposta inesperada, tentando próximo método...")
                        continue
                    elif soap_error_msg is None:
                        logger.warning(f"SOAP request failed: {response.status_code}")
                        logger.debug(f"Response: {response.text[:500] if response.text else 'empty'}")
                        return None
                        
            except Exception as e:
                logger.error(f"❌ Auth {auth_method} erro: {e}")
                if not try_all_auth:
                    return None
                continue
        
        logger.warning("❌ Nenhum método de autenticação funcionou")
        return None
    
    def check_capabilities(self) -> bool:
        """Verifica se a câmera suporta eventos ONVIF (testa múltiplos métodos de auth)"""
        body = '''
            <tev:GetServiceCapabilities/>
        '''
        
        # Primeira requisição: tenta todos os métodos de autenticação
        response = self._send_soap_request(
            self.events_url,
            'http://www.onvif.org/ver10/events/wsdl/EventPortType/GetServiceCapabilitiesRequest',
            body,
            try_all_auth=True
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
                    if self._working_auth_method:
                        logger.info(f"🔐 Método de autenticação: {self._working_auth_method}")
                    return True
            except ET.ParseError as e:
                logger.error(f"XML parse error: {e}")
        
        return False
    
    def create_pull_point_subscription(self) -> bool:
        """Cria subscription para receber eventos via pull"""
        # Tenta diferentes formatos de requisição (compatibilidade com várias marcas)
        bodies = [
            # Formato Dahua/Intelbras específico
            '''<tev:CreatePullPointSubscription xmlns:tev="http://www.onvif.org/ver10/events/wsdl">
                <tev:InitialTerminationTime>PT600S</tev:InitialTerminationTime>
            </tev:CreatePullPointSubscription>''',
            # Formato padrão ONVIF
            '''<tev:CreatePullPointSubscription>
                <tev:InitialTerminationTime>PT1H</tev:InitialTerminationTime>
            </tev:CreatePullPointSubscription>''',
            # Formato alternativo (sem InitialTerminationTime)
            '''<tev:CreatePullPointSubscription/>''',
            # Formato com filter vazio (algumas câmeras precisam)
            '''<tev:CreatePullPointSubscription>
                <tev:Filter/>
                <tev:InitialTerminationTime>PT60M</tev:InitialTerminationTime>
            </tev:CreatePullPointSubscription>''',
            # Formato minimalista para Dahua
            '''<CreatePullPointSubscription xmlns="http://www.onvif.org/ver10/events/wsdl"/>''',
        ]
        
        for i, body in enumerate(bodies):
            logger.info(f"📋 Tentando formato {i+1}/{len(bodies)} de CreatePullPointSubscription...")
            
            response = self._send_soap_request(
                self.events_url,
                'http://www.onvif.org/ver10/events/wsdl/EventPortType/CreatePullPointSubscriptionRequest',
                body,
                debug=True,
                try_all_auth=True  # Tenta todos os métodos de auth se necessário
            )
            
            if response:
                try:
                    root = ET.fromstring(response)
                    
                    # Verifica se é um Fault SOAP
                    fault = root.find('.//{http://www.w3.org/2003/05/soap-envelope}Fault')
                    if fault is not None:
                        # Extrai detalhes do erro
                        reason = fault.find('.//{http://www.w3.org/2003/05/soap-envelope}Text')
                        reason_text = reason.text if reason is not None else "Unknown"
                        
                        # Procura por descrição detalhada
                        descr = fault.find('.//{http://docs.oasis-open.org/wsrf/bf-2}Description')
                        descr_text = descr.text if descr is not None else ""
                        
                        # Log completo do erro
                        logger.warning(f"⚠️ SOAP Fault (formato {i+1}): {reason_text}")
                        if descr_text:
                            logger.warning(f"   Descrição: {descr_text}")
                        
                        # Se o erro indica limite de subscriptions, mostra mensagem clara
                        if 'limit' in reason_text.lower() or 'maximum' in reason_text.lower():
                            logger.error("❌ Limite de subscriptions atingido! Reinicie a câmera para limpar.")
                            return False
                        
                        # Continua tentando outros formatos, mas se já tentou com auth funcionando, é outro problema
                        continue
                    
                    # Log do XML para debug (só se não for fault)
                    logger.info(f"📄 Response XML (formato {i+1}): {response[:800]}")
                    
                    # Extrai SubscriptionReference - tenta vários formatos
                    sub_ref = root.find('.//tev:SubscriptionReference/wsnt:Address', NAMESPACES)
                    if sub_ref is None:
                        sub_ref = root.find('.//{http://www.w3.org/2005/08/addressing}Address')
                    if sub_ref is None:
                        # Tenta formato alternativo
                        sub_ref = root.find('.//wsnt:SubscriptionReference/wsa:Address', 
                                          {**NAMESPACES, 'wsa': 'http://www.w3.org/2005/08/addressing'})
                    if sub_ref is None:
                        # Procura qualquer elemento Address
                        for elem in root.iter():
                            if 'Address' in elem.tag and elem.text and 'http' in elem.text:
                                sub_ref = elem
                                logger.info(f"🔍 Encontrado Address via fallback: tag={elem.tag}")
                                break
                    
                    if sub_ref is not None and sub_ref.text:
                        self.subscription_reference = sub_ref.text
                        logger.info(f"✅ Pull Point criado: {self.subscription_reference}")
                        return True
                    else:
                        logger.warning(f"⚠️ Resposta recebida mas sem SubscriptionReference")
                        
                except ET.ParseError as e:
                    logger.error(f"XML parse error: {e}")
            else:
                logger.debug(f"Formato {i+1} não retornou resposta válida")
        
        logger.error("❌ Nenhum formato de CreatePullPointSubscription funcionou")
        logger.error("💡 Dica: Tente reiniciar a câmera para limpar subscriptions pendentes")
        return False
    
    def pull_messages(self) -> List[OnvifEvent]:
        """Busca mensagens pendentes do pull point"""
        if not self.subscription_reference:
            return []
        
        # Se já sabemos qual formato funciona, usa só ele
        if hasattr(self, '_working_pull_format') and self._working_pull_format is not None:
            bodies = [self._get_pull_body(self._working_pull_format)]
        else:
            # Tenta diferentes formatos
            bodies = [
                self._get_pull_body(0),
                self._get_pull_body(1),
                self._get_pull_body(2),
            ]
        
        # Usa o subscription_reference como URL
        url = self.subscription_reference
        if not url.startswith('http'):
            url = f"{self.base_url}{url}"
        
        response = None
        
        # Tenta cada formato de body
        for i, body in enumerate(bodies):
            # Só tenta múltiplos auth se ainda não sabemos qual funciona
            should_try_all = (i == 0 and not self._working_auth_method)
            
            resp = self._send_soap_request(
                url,
                'http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest',
                body,
                try_all_auth=should_try_all
            )
            
            if resp:
                # Verifica se não é um Fault
                if '<Fault' not in resp and 'Fault>' not in resp:
                    # Cacheia o formato que funcionou
                    if not hasattr(self, '_working_pull_format') or self._working_pull_format is None:
                        # Calcula o índice real baseado no body
                        real_idx = 0 if 'tev:PullMessages' in body else (1 if 'xmlns=' in body else 2)
                        logger.info(f"✅ Formato PullMessages {real_idx+1} funcionou")
                        self._working_pull_format = real_idx
                    response = resp
                    break
        
        events = []
        
        if response:
            try:
                root = ET.fromstring(response)
                
                # Verifica se é um Fault SOAP
                fault = root.find('.//{http://www.w3.org/2003/05/soap-envelope}Fault')
                if fault is not None:
                    reason = fault.find('.//{http://www.w3.org/2003/05/soap-envelope}Text')
                    reason_text = reason.text if reason is not None else "Unknown"
                    logger.warning(f"⚠️ PullMessages SOAP Fault: {reason_text}")
                    # Se o erro indica subscription inválida, marca para reconectar
                    if 'invalid' in reason_text.lower() or 'not found' in reason_text.lower():
                        logger.error("❌ Subscription inválida - precisa reconectar")
                        self.subscription_reference = None
                    return []
                
                # Log da resposta para debug (primeiros 500 chars)
                logger.debug(f"📄 PullMessages response: {response[:500]}")
                
                # Parse notification messages
                messages = root.findall('.//wsnt:NotificationMessage', NAMESPACES)
                
                # Também tenta namespace alternativo para Dahua/Intelbras
                if not messages:
                    messages = root.findall('.//{http://docs.oasis-open.org/wsn/b-2}NotificationMessage')
                
                # Log de debug para ver quantas mensagens vieram
                if messages:
                    logger.info(f"📨 Recebidas {len(messages)} mensagens ONVIF")
                else:
                    # Log apenas a cada 30 segundos para não spammar
                    if not hasattr(self, '_last_empty_log') or (datetime.now() - self._last_empty_log).total_seconds() > 30:
                        logger.debug("📭 PullMessages: nenhuma mensagem pendente")
                        self._last_empty_log = datetime.now()
                
                for msg in messages:
                    # Log do XML da mensagem para debug
                    logger.debug(f"📄 Message XML: {ET.tostring(msg, encoding='unicode')[:500]}")
                    
                    event = self._parse_notification_message(msg)
                    if event:
                        events.append(event)
                    else:
                        logger.debug("⚠️ Mensagem não gerou evento (cooldown ou parsing)")
                        
            except ET.ParseError as e:
                logger.error(f"XML parse error: {e}")
                logger.debug(f"Response: {response[:500]}")
        
        return events
    
    def _get_pull_body(self, format_idx: int) -> str:
        """Retorna o body de PullMessages para o formato especificado"""
        if format_idx == 0:
            return '''<tev:PullMessages>
                <tev:Timeout>PT5S</tev:Timeout>
                <tev:MessageLimit>100</tev:MessageLimit>
            </tev:PullMessages>'''
        elif format_idx == 1:
            return '''<PullMessages xmlns="http://www.onvif.org/ver10/events/wsdl">
                <Timeout>PT5S</Timeout>
                <MessageLimit>100</MessageLimit>
            </PullMessages>'''
        else:
            return '''<wsnt:PullMessages xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2">
                <wsnt:Timeout>PT5S</wsnt:Timeout>
                <wsnt:MessageLimit>100</wsnt:MessageLimit>
            </wsnt:PullMessages>'''
    
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
    
    def _should_renew_subscription(self) -> bool:
        """Verifica se a subscription precisa ser renovada"""
        if not self._subscription_created_at:
            return True
        elapsed = (datetime.now() - self._subscription_created_at).total_seconds()
        return elapsed > self._subscription_ttl_seconds
    
    def _renew_subscription(self) -> bool:
        """Renova a subscription se necessário"""
        if not self._should_renew_subscription():
            return True
        
        logger.info(f"🔄 Renovando subscription para {self.camera_name}...")
        if self.create_pull_point_subscription():
            self._subscription_created_at = datetime.now()
            return True
        return False
    
    def _poll_loop(self):
        """Loop de polling para eventos - conexão persistente"""
        logger.info(f"🔄 Poll loop iniciado para {self.camera_name} (conexão persistente)")
        poll_count = 0
        
        while self.running:
            try:
                # Renova subscription se necessário (antes de expirar)
                if self._should_renew_subscription():
                    if not self._renew_subscription():
                        logger.warning(f"⚠️ Falha ao renovar subscription de {self.camera_name}, tentando novamente em 30s...")
                        self._poll_error_count += 1
                        if self._poll_error_count > 5:
                            logger.error(f"❌ Muitos erros para {self.camera_name}, pausando polling por 60s")
                            time.sleep(60)
                            self._poll_error_count = 0
                            continue
                        time.sleep(30)
                        continue
                    self._poll_error_count = 0
                
                events = self.pull_messages()
                poll_count += 1
                
                # Log periódico menos frequente (a cada 60 polls = ~1 min)
                if poll_count % 60 == 0:
                    logger.debug(f"📡 {self.camera_name}: poll #{poll_count} - conexão ativa")
                
                for event in events:
                    logger.info(f"📥 Evento: {event.event_type} de {event.camera_name}")
                    
                    if self.event_callback:
                        try:
                            self.event_callback(event)
                        except Exception as e:
                            logger.error(f"Error in event callback: {e}")
                
                # Reset error count on success
                self._poll_error_count = 0
                
            except Exception as e:
                self._poll_error_count += 1
                if self._poll_error_count <= 3:
                    logger.warning(f"⚠️ Poll error ({self._poll_error_count}): {e}")
                elif self._poll_error_count == 4:
                    logger.error(f"❌ Múltiplos erros de polling para {self.camera_name}, reduzindo logs...")
            
            time.sleep(1)  # Poll a cada 1 segundo
    
    def start(self) -> bool:
        """Inicia a escuta de eventos com conexão única e persistente"""
        if not self._connection_logged:
            logger.info(f"🎯 Conectando a {self.camera_name} ({self.camera_ip}) - conexão única")
            self._connection_logged = True
        
        # Verifica capabilities (uma vez)
        if not self._working_auth_method:
            if not self.check_capabilities():
                logger.warning(f"⚠️ Não foi possível verificar capabilities de {self.camera_name}")
        
        # Cria subscription inicial
        if not self.create_pull_point_subscription():
            logger.error(f"❌ Falha ao criar subscription para {self.camera_name}")
            return False
        
        self._subscription_created_at = datetime.now()
        
        # Inicia thread de polling (persistente)
        self.running = True
        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True, name=f"onvif-{self.camera_ip}")
        self.poll_thread.start()
        
        logger.info(f"✅ {self.camera_name}: conexão ONVIF estabelecida (subscription renovada automaticamente)")
        return True
    
    def stop(self):
        """Para a escuta de eventos"""
        logger.info(f"🛑 Parando escuta de {self.camera_name}")
        self.running = False
        
        if self.poll_thread:
            self.poll_thread.join(timeout=5)


class OnvifEventsManager:
    """
    Gerencia múltiplos clientes ONVIF de eventos.
    Mantém conexões persistentes e evita reconexões desnecessárias.
    """
    
    def __init__(self, event_callback: Callable[[OnvifEvent], None] = None):
        self.clients: Dict[str, OnvifEventsClient] = {}
        self.event_callback = event_callback
        self.lock = threading.Lock()
        self._started_at = datetime.now()
        logger.info("📡 OnvifEventsManager inicializado")
    
    def add_camera(
        self,
        camera_ip: str,
        username: str = "admin",
        password: str = "",
        camera_name: str = "",
        camera_port: int = 80,
    ) -> bool:
        """Adiciona uma câmera para escuta de eventos (conexão persistente)"""
        with self.lock:
            if camera_ip in self.clients:
                client = self.clients[camera_ip]
                if client.running:
                    logger.debug(f"📡 {camera_ip}: já está conectado e ativo")
                    return True
                else:
                    # Cliente existe mas não está rodando, remove e recria
                    logger.info(f"📡 {camera_ip}: reconectando (estava inativo)")
                    try:
                        client.stop()
                    except:
                        pass
                    del self.clients[camera_ip]
            
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
                logger.info(f"✅ {camera_name} ({camera_ip}): conexão ONVIF persistente estabelecida")
                return True
            
            logger.warning(f"⚠️ {camera_name} ({camera_ip}): falha ao estabelecer conexão ONVIF")
            return False
    
    def remove_camera(self, camera_ip: str):
        """Remove uma câmera da escuta"""
        with self.lock:
            if camera_ip in self.clients:
                logger.info(f"🛑 Removendo escuta ONVIF de {camera_ip}")
                self.clients[camera_ip].stop()
                del self.clients[camera_ip]
    
    def stop_all(self):
        """Para todos os clientes - chamado quando o app fecha"""
        with self.lock:
            logger.info(f"🛑 Encerrando {len(self.clients)} conexões ONVIF...")
            for ip, client in self.clients.items():
                try:
                    client.stop()
                    logger.debug(f"   ✓ {ip} desconectado")
                except Exception as e:
                    logger.warning(f"   ⚠️ Erro ao desconectar {ip}: {e}")
            self.clients.clear()
            logger.info("✅ Todas as conexões ONVIF encerradas")
    
    def get_status(self) -> Dict:
        """Retorna status de todas as câmeras"""
        with self.lock:
            active_count = sum(1 for c in self.clients.values() if c.running)
            return {
                "total_cameras": len(self.clients),
                "active_cameras": active_count,
                "uptime_seconds": (datetime.now() - self._started_at).total_seconds(),
                "cameras": {
                    ip: {
                        "name": client.camera_name,
                        "running": client.running,
                        "subscription_active": client.subscription_reference is not None,
                    }
                    for ip, client in self.clients.items()
                }
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
