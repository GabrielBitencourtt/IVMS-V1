# Usar imagem com FFmpeg e nginx-rtmp
FROM python:3.11-slim

# Instalar FFmpeg (static build) e nginx com módulo RTMP
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        wget xz-utils \
        nginx libnginx-mod-rtmp \
        procps && \
    # FFmpeg static build
    wget -q https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz && \
    tar -xf ffmpeg-release-amd64-static.tar.xz && \
    mv ffmpeg-*-amd64-static/ffmpeg /usr/local/bin/ && \
    mv ffmpeg-*-amd64-static/ffprobe /usr/local/bin/ && \
    rm -rf ffmpeg-* && \
    # Cleanup
    apt-get remove -y wget xz-utils && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Diretório de trabalho
WORKDIR /app

# Copiar requirements primeiro (cache de camadas)
COPY requirements.txt .

# Instalar dependências Python
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código
COPY . .

# Criar diretório HLS com permissões corretas e tornar script executável
RUN mkdir -p /tmp/hls && \
    chmod 777 /tmp/hls && \
    chmod +x start.sh

# Expor portas (HTTP + RTMP)
EXPOSE 8080 1935

# Comando de inicialização
CMD ["./start.sh"]
