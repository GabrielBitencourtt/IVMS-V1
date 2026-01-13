#!/usr/bin/env python3
"""
Módulo de teste de conexão RTSP
Suporta Basic e Digest Auth (incluindo qop=auth)
"""

import re
import socket
import hashlib
import random
import base64
import logging

logger = logging.getLogger(__name__)


def test_rtsp_connection(rtsp_url: str, timeout: int = 5) -> tuple:
    """
    Testa conexão RTSP localmente com suporte a Basic e Digest Auth (incluindo qop=auth).
    Retorna (sucesso: bool, mensagem: str, detalhes: dict)
    """
    debug_info = []  # Para coletar informações de debug
    
    def md5_hash(text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()
    
    def generate_cnonce(length: int = 8) -> str:
        """Gera um client nonce aleatório (8 chars hex)"""
        return ''.join(random.choices('0123456789abcdef', k=length))
    
    def parse_www_authenticate(header: str) -> dict:
        """Parse WWW-Authenticate header para extrair realm, nonce, qop, etc"""
        result = {}
        # Extrai realm
        realm_match = re.search(r'realm="([^"]*)"', header)
        if realm_match:
            result['realm'] = realm_match.group(1)
        # Extrai nonce
        nonce_match = re.search(r'nonce="([^"]*)"', header)
        if nonce_match:
            result['nonce'] = nonce_match.group(1)
        # Extrai qop
        qop_match = re.search(r'qop="([^"]*)"', header)
        if qop_match:
            result['qop'] = qop_match.group(1)
        # Extrai opaque (se existir)
        opaque_match = re.search(r'opaque="([^"]*)"', header)
        if opaque_match:
            result['opaque'] = opaque_match.group(1)
        # Extrai algorithm (se existir)
        algo_match = re.search(r'algorithm=([^,\s]+)', header)
        if algo_match:
            result['algorithm'] = algo_match.group(1).strip('"')
        return result
    
    def create_digest_auth(username: str, password: str, realm: str, nonce: str, 
                           uri: str, method: str = "DESCRIBE", qop: str = None, 
                           opaque: str = None, nc_val: str = "00000001") -> str:
        """Cria header de autenticação Digest (RFC 2617) com suporte a qop=auth"""
        ha1 = md5_hash(f"{username}:{realm}:{password}")
        ha2 = md5_hash(f"{method}:{uri}")
        
        debug_info.append(f"HA1 input: {username}:{realm}:{password}")
        debug_info.append(f"HA1: {ha1}")
        debug_info.append(f"HA2 input: {method}:{uri}")
        debug_info.append(f"HA2: {ha2}")
        
        if qop and 'auth' in qop:
            # Com qop=auth, precisa de nc e cnonce
            nc = nc_val
            cnonce = generate_cnonce()
            response_input = f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}"
            response = md5_hash(response_input)
            
            debug_info.append(f"Response input (qop=auth): {response_input}")
            debug_info.append(f"Response: {response}")
            
            auth_parts = [
                f'username="{username}"',
                f'realm="{realm}"',
                f'nonce="{nonce}"',
                f'uri="{uri}"',
                f'qop=auth',
                f'nc={nc}',
                f'cnonce="{cnonce}"',
                f'response="{response}"',
            ]
            if opaque:
                auth_parts.append(f'opaque="{opaque}"')
            
            return 'Digest ' + ', '.join(auth_parts)
        else:
            # Sem qop (RFC 2069 estilo antigo)
            response_input = f"{ha1}:{nonce}:{ha2}"
            response = md5_hash(response_input)
            
            debug_info.append(f"Response input (no qop): {response_input}")
            debug_info.append(f"Response: {response}")
            
            auth_parts = [
                f'username="{username}"',
                f'realm="{realm}"',
                f'nonce="{nonce}"',
                f'uri="{uri}"',
                f'response="{response}"',
            ]
            if opaque:
                auth_parts.append(f'opaque="{opaque}"')
            
            return 'Digest ' + ', '.join(auth_parts)
    
    # Parse URL
    pattern = r'rtsp://(?:([^:@]+):([^@]+)@)?([^:/]+):?(\d+)?(/.*)?'
    match = re.match(pattern, rtsp_url)
    
    if not match:
        return False, "URL RTSP inválida", {}
    
    user = match.group(1) or ''
    password = match.group(2) or ''
    host = match.group(3)
    port = int(match.group(4)) if match.group(4) else 554
    path = match.group(5) or '/'
    
    try:
        # Conecta via socket TCP
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        
        # Envia DESCRIBE request inicial (sem auth)
        cseq = 1
        # URI completa para o request RTSP
        full_uri = f"rtsp://{host}:{port}{path}"
        # URI para Digest Auth (apenas o path)
        digest_uri = path
        
        describe_req = f"DESCRIBE {full_uri} RTSP/1.0\r\nCSeq: {cseq}\r\nUser-Agent: CameraScanner/1.0\r\nAccept: application/sdp\r\n\r\n"
        sock.send(describe_req.encode())
        
        response = sock.recv(4096).decode('utf-8', errors='ignore')
        
        # Analisa resposta
        if 'RTSP/1.0 200' in response:
            sock.close()
            return True, "Conexão RTSP bem-sucedida!", {"response": "200 OK", "requires_auth": False}
        
        elif 'RTSP/1.0 401' in response:
            # Requer autenticação - tenta com credenciais se fornecidas
            if user and password:
                # Verifica tipo de autenticação
                auth_type = "Basic"
                auth_params = {}
                
                debug_info.append(f"=== WWW-Authenticate Header ===")
                debug_info.append(response[:500])
                
                if 'Digest' in response:
                    auth_type = "Digest"
                    auth_params = parse_www_authenticate(response)
                    debug_info.append(f"Auth params: {auth_params}")
                
                # IMPORTANTE: Reutiliza a MESMA conexão socket!
                # Muitas câmeras geram um novo nonce por conexão
                cseq = 2
                
                if auth_type == "Digest" and auth_params.get('realm') and auth_params.get('nonce'):
                    # Usa Digest Auth com suporte a qop
                    auth_header = create_digest_auth(
                        user, password, 
                        auth_params['realm'], 
                        auth_params['nonce'],
                        digest_uri,
                        qop=auth_params.get('qop'),
                        opaque=auth_params.get('opaque')
                    )
                else:
                    # Usa Basic Auth
                    auth_string = base64.b64encode(f"{user}:{password}".encode()).decode()
                    auth_header = f"Basic {auth_string}"
                
                debug_info.append(f"=== Auth Header Enviado ===")
                debug_info.append(auth_header)
                
                # Envia na MESMA conexão
                auth_req = f"DESCRIBE {full_uri} RTSP/1.0\r\nCSeq: {cseq}\r\nUser-Agent: CameraScanner/1.0\r\nAuthorization: {auth_header}\r\nAccept: application/sdp\r\n\r\n"
                sock.send(auth_req.encode())
                
                response2 = sock.recv(4096).decode('utf-8', errors='ignore')
                sock.close()
                
                debug_info.append(f"=== Resposta da câmera ===")
                debug_info.append(response2[:300])
                
                # Log de debug no console
                print("\n".join(debug_info))
                
                if 'RTSP/1.0 200' in response2:
                    return True, f"Autenticação {auth_type} OK!", {"response": "200 OK", "requires_auth": True, "auth_type": auth_type, "debug": debug_info}
                elif 'RTSP/1.0 401' in response2:
                    # Mostra debug na mensagem de erro
                    debug_summary = f"\nRealm: {auth_params.get('realm')}, Nonce: {auth_params.get('nonce', '')[:20]}..."
                    return False, f"Credenciais incorretas ({auth_type}){debug_summary}", {"response": "401 Unauthorized", "auth_type": auth_type, "debug": debug_info}
                else:
                    status_match = re.search(r'RTSP/1\.0 (\d+)', response2)
                    status = status_match.group(1) if status_match else 'Desconhecido'
                    return False, f"Erro: {status}", {"response": status, "debug": debug_info}
            else:
                sock.close()
                return False, "Requer autenticação", {"response": "401 Unauthorized", "requires_auth": True}
        
        elif 'RTSP/1.0 404' in response:
            sock.close()
            return False, "Stream não encontrado", {"response": "404 Not Found"}
        
        elif 'RTSP/1.0 403' in response:
            sock.close()
            return False, "Acesso negado", {"response": "403 Forbidden"}
        
        else:
            sock.close()
            status_match = re.search(r'RTSP/1\.0 (\d+)', response)
            status = status_match.group(1) if status_match else 'Desconhecido'
            return False, f"Resposta: {status}", {"response": status}
        
    except socket.timeout:
        return False, "Timeout na conexão", {"error": "timeout"}
    except ConnectionRefusedError:
        return False, "Conexão recusada", {"error": "connection_refused"}
    except Exception as e:
        return False, f"Erro: {str(e)}", {"error": str(e)}
