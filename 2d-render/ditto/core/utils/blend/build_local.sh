#!/bin/bash
# Script to build Cython blend module locally
# Usage: ./build_local.sh

set -e

echo "🔨 Building Cython blend module..."

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Check if Cython is installed
if ! python3 -c "import Cython" 2>/dev/null; then
    echo "❌ Cython not found. Installing..."
    pip3 install cython
fi

# Check if numpy is installed
if ! python3 -c "import numpy" 2>/dev/null; then
    echo "❌ NumPy not found. Installing..."
    pip3 install numpy
fi

# Build extension
echo "📦 Compiling extension..."
python3 setup.py build_ext --inplace

# Clean up build artifacts
echo "🧹 Cleaning up..."
rm -rf build/
rm -f blend.c blend.cpp 2>/dev/null || true

# Find the compiled module
SO_FILE=$(ls blend*.so 2>/dev/null || ls blend*.pyd 2>/dev/null || echo "")

if [ -n "$SO_FILE" ]; then
    echo "✅ Build successful! Created: $SO_FILE"
    echo "🧪 Testing import..."
    
    if python3 -c "from blend import blend_images_cy; print('✅ Import successful!')" 2>/dev/null; then
        echo "🎉 Module is ready to use!"
    else
        echo "⚠️  Module compiled but import failed. Check dependencies."
        exit 1
    fi
else
    echo "❌ Build failed - no compiled module found"
    exit 1
fi

