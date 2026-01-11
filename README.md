# Servidor RTMP para IVMS Pro

Este servidor recebe streams RTMP e converte para HLS, permitindo visualização no browser.

## Requisitos

- Docker e Docker Compose instalados
- Porta 1935 (RTMP) e 8080 (HTTP/HLS) liberadas

## Como usar

### 1. Inicie o servidor

```bash
docker-compose up -d
```

### 2. Configure sua câmera/encoder

Use a URL de ingest gerada no IVMS Pro:
```
rtmp://SEU_IP:1935/live/STREAM_KEY
```

Exemplo:
- Server URL: `rtmp://192.168.1.100:1935/live`
- Stream Key: `abc123xyz789`

### 3. Visualize o stream

O stream HLS estará disponível em:
```
http://SEU_IP:8080/hls/STREAM_KEY.m3u8
```

## Estrutura

```
├── docker-compose.yml     # Orquestração do container
├── nginx.conf             # Configuração do nginx-rtmp
└── Dockerfile             # Imagem customizada
```

## Comandos úteis

```bash
# Ver logs.
docker-compose logs -f

# Parar servidor
docker-compose down

# Reiniciar
docker-compose restart
```

## Integração com Python (futuro)

Para adicionar processamento com Python (detecção de movimento, IA, etc), você pode:

1. Acessar os streams HLS via FFmpeg/OpenCV
2. Processar frames e enviar alertas para o IVMS Pro via API
3. Usar o script `stream_processor.py` como base

## Portas

| Porta | Protocolo | Uso |
|-------|-----------|-----|
| 1935  | RTMP      | Receber streams |
| 8080  | HTTP      | Servir HLS |

## Segurança

⚠️ Em produção, adicione:
- Firewall limitando IPs
- SSL/TLS para HTTPS
- Autenticação para publish
