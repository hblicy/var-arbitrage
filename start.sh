#!/bin/bash

# 1. Setup Environment
# Navigate to script directory
cd "$(dirname "$0")"

# Activate Virtual Environment
if [ -n "$VIRTUAL_ENV" ]; then
    echo "Using already active virtual environment: $VIRTUAL_ENV"
elif [ -f "venv_var/bin/activate" ]; then
    source venv_var/bin/activate
    echo "Activated venv_var"
elif [ -f "venv_nado/bin/activate" ]; then
    source venv_nado/bin/activate
    echo "Activated venv_nado"
elif [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
    echo "Activated venv"
else
    echo "Warning: No virtual environment found (checked 'venv_var', 'venv_nado' and 'venv')."
    echo "Attempting to run with system python..."
fi

# 2. Check if already running on port 8011
PID=$(pgrep -u "$USER" -f "uvicorn api:app.*--port 8011")
if [ -n "$PID" ]; then
    echo "Nado-Variational Monitor (v1.5) is already running on port 8011 (PID: $PID)"
    exit 1
fi

# 3. Start Application
echo "Starting Nado-Variational Monitor v1.5..."
# Using uvicorn directly as per requirements
mkdir -p logs
nohup uvicorn api:app --host 0.0.0.0 --port 8011 > logs/api.log 2>&1 &

# 4. Verification
NEW_PID=$!
sleep 2
if ps -p $NEW_PID > /dev/null; then
    echo "Success! Application started with PID: $NEW_PID"
    echo "Logs are being written to logs/api.log"
    echo "Dashboard available at http://$(curl -s ifconfig.me):8011"
else
    echo "Failed to start. Check logs/api.log for details."
    tail logs/api.log
fi
