#!/bin/bash

# Plex Library Auditor Launch Script

# Exit on any error
set -e

echo "🚀 Starting Plex Library Auditor setup..."

# 1. Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "❌ Error: Python 3 is not installed."
    exit 1
fi

# 2. Try to create virtual environment, fallback to --user install if it fails
if [ -d "venv" ] && [ ! -f "venv/bin/activate" ]; then
    echo "🧹 Cleaning up invalid virtual environment..."
    rm -rf venv
fi

if [ ! -d "venv" ]; then
    echo "📦 Attempting to create virtual environment..."
    if python3 -m venv venv 2>/dev/null; then
        echo "✅ Virtual environment created."
        PYTHON_EXEC="./venv/bin/python3"
    else
        echo "⚠️  Could not create virtual environment (missing python3-venv). Falling back to --user installation."
        PYTHON_EXEC="python3"
    fi
else
    PYTHON_EXEC="./venv/bin/python3"
fi

# 3. Install/Update dependencies
echo "📥 Installing dependencies..."
$PYTHON_EXEC -m pip install --quiet --user -r requirements.txt || $PYTHON_EXEC -m pip install --quiet -r requirements.txt

# 4. Check for .env file
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        echo "📝 Creating .env from template..."
        cp .env.example .env
    else
        echo "📝 Creating empty .env file..."
        touch .env
    fi
fi

echo "✅ Setup complete!"
echo "🌐 Launching Streamlit dashboard..."
echo "------------------------------------------------"
echo "🔗 Access the tool at: http://localhost:8501"
echo "------------------------------------------------"

# 5. Launch Streamlit
$PYTHON_EXEC -m streamlit run app.py --server.headless true
