#!/bin/bash

# 1. Setup Environment
# Navigate to script directory
cd "$(dirname "$0")"

# Activate Virtual Environment
if [ -n "$VIRTUAL_ENV" ]; then
    echo "Using already active virtual environment: $VIRTUAL_ENV"
elif [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
    echo "Activated .venv"
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
    echo "Warning: No virtual environment found (checked '.venv', 'venv_var', 'venv_nado' and 'venv')."
    echo "Attempting to run with system python..."
fi

# 2. Build dashboard if static files are missing
if [ "$SKIP_DASHBOARD_BUILD" != "1" ] && [ -f "dashboard/package.json" ]; then
    DASHBOARD_NEEDS_BUILD=0
    if [ ! -f "dashboard/dist/index.html" ]; then
        DASHBOARD_NEEDS_BUILD=1
    elif find dashboard/src dashboard/package.json dashboard/package-lock.json dashboard/index.html \
        -newer dashboard/dist/index.html -print -quit 2>/dev/null | grep -q .; then
        DASHBOARD_NEEDS_BUILD=1
    fi

    if [ "$DASHBOARD_NEEDS_BUILD" = "1" ]; then
        echo "Building dashboard frontend..."
        if ! command -v npm >/dev/null 2>&1; then
            echo "Warning: npm is not installed. Backend will start, but dashboard page may return 404."
        else
            (
                cd dashboard || exit 1
                if [ -f "package-lock.json" ]; then
                    npm ci
                else
                    npm install
                fi
                npm run build
            )
            if [ $? -ne 0 ]; then
                echo "Failed to build dashboard. Please check npm output above."
                exit 1
            fi
        fi
    fi
fi

# 3. Check if already running on port 8011
PID=$(pgrep -u "$USER" -f "uvicorn api:app.*--port 8011")
if [ -n "$PID" ]; then
    echo "Nado-Variational Monitor (v1.5) is already running on port 8011 (PID: $PID)"
    exit 1
fi

# 4. Start Application
echo "Starting Nado-Variational Monitor v1.5..."
# Using uvicorn directly as per requirements
mkdir -p logs
nohup uvicorn api:app --host 0.0.0.0 --port 8011 > logs/api.log 2>&1 &

# 5. Verification
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
