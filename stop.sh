#!/bin/bash

# 1. Setup Environment
cd "$(dirname "$0")"

# 2. Find Process (Filter by current user and port to be precise)
# Port 8011 is specific to v1.1
PIDS=$(pgrep -u "$USER" -f "uvicorn api:app.*--port 8011")

if [ -z "$PIDS" ]; then
    echo "Nado-Variational Monitor (v1.5 on port 8011) is not running."
    exit 0
fi

# 3. Stop Process
echo "Stopping (PIDs: $PIDS)..."
for PID in $PIDS; do
    echo "Sending SIGTERM to $PID..."
    kill $PID 2>/dev/null
done

# 4. Wait for termination
sleep 2
STILL_RUNNING=""
for PID in $PIDS; do
    if ps -p $PID > /dev/null 2>&1; then
        STILL_RUNNING="$STILL_RUNNING $PID"
    fi
done

if [ -n "$STILL_RUNNING" ]; then
    echo "Force killing: $STILL_RUNNING..."
    for PID in $STILL_RUNNING; do
        kill -9 $PID 2>/dev/null
    done
fi

echo "Stopped."
