#!/usr/bin/env bash
# Official Linux manual install: https://docs.ollama.com/linux
set -euo pipefail
if [[ "$(uname -m)" != x86_64 ]]; then echo 'Expected x86_64'; exit 1; fi
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
available_kb=$(df --output=avail /usr/local /var/lib | tail -n 1 | tr -d ' ')
if (( available_kb < 15728640 )); then echo 'Need at least 15 GiB free'; exit 1; fi
command -v zstd >/dev/null
sudo -v
if ! command -v ollama >/dev/null; then
    archive=$(mktemp /tmp/ollama-linux-amd64.XXXXXX.tar.zst)
    trap 'rm -f "$archive"' EXIT
    curl --fail --location --retry 3 https://ollama.com/download/ollama-linux-amd64.tar.zst -o "$archive"
    sudo tar --zstd -xf "$archive" -C /usr/local
fi
if ! id ollama >/dev/null 2>&1; then
    sudo useradd -r -s /usr/sbin/nologin -U -m -d /usr/share/ollama ollama
fi
for group in render video; do
    if getent group "$group" >/dev/null; then sudo usermod -a -G "$group" ollama; fi
done
sudo install -d -o ollama -g ollama -m 750 /var/lib/ollama/models
sudo install -d -m 755 /etc/systemd/system/ollama.service.d
ollama_binary=$(command -v ollama)
if ! systemctl cat ollama.service >/dev/null 2>&1; then
    sudo tee /etc/systemd/system/ollama.service >/dev/null <<EOF
[Unit]
Description=Ollama local model server
After=network-online.target

[Service]
ExecStart=$ollama_binary serve
User=ollama
Group=ollama
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
fi
sudo tee /etc/systemd/system/ollama.service.d/local-audit.conf >/dev/null <<'EOF'
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_MODELS=/var/lib/ollama/models"
Environment="OLLAMA_CONTEXT_LENGTH=8192"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_KEEP_ALIVE=10m"
Environment="OLLAMA_NO_CLOUD=1"
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now ollama
sudo systemctl restart ollama
for attempt in {1..30}; do
    if curl -fsS http://127.0.0.1:11434/api/version; then
        printf '\nOllama ready. Model directory: /var/lib/ollama/models\n'
        exit 0
    fi
    sleep 1
done
sudo journalctl -u ollama -n 30 --no-pager
exit 1
