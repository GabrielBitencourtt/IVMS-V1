#!/bin/bash
set -e

# Criar diretórios necessários
mkdir -p /tmp/hls

# Iniciar nginx-rtmp em background
echo "🎬 Starting nginx-rtmp server on port 1935..."
nginx -c /app/nginx.conf -g 'daemon on;' &

# Aguardar nginx iniciar
sleep 3

# Verificar se nginx está rodando
if pgrep -x "nginx" > /dev/null; then
    echo "✅ nginx-rtmp started successfully on port 1935"
    # Testar se porta 1935 está aberta
    if command -v ss &> /dev/null; then
        ss -tlnp | grep 1935 || echo "⚠️ Port 1935 not listening yet"
    fi
else
    echo "❌ nginx-rtmp failed to start!"
    cat /var/log/nginx/error.log 2>/dev/null || true
fi

# Iniciar FastAPI
echo "🚀 Starting FastAPI server on port 8080..."
exec uvicorn main:app --host 0.0.0.0 --port 8080
