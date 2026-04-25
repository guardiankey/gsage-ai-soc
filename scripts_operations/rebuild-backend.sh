#!/bin/bash
# Quick rebuild script for backend development

set -e

echo "🔨 Rebuilding backend_api..."
docker compose build backend_api

echo "🔄 Recreating backend_api container..."
docker compose up -d --force-recreate backend_api

echo "⏳ Waiting for backend to be ready..."
sleep 3

echo "📋 Checking backend_api logs..."
docker compose logs --tail=50 backend_api

echo ""
echo "✅ Backend_api rebuild complete!"
