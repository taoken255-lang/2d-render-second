@echo off
REM Script to build Cython blend module locally on Windows
REM Usage: build_local.bat

echo 🔨 Building Cython blend module...

REM Get script directory
cd /d "%~dp0"

REM Check if Cython is installed
python -c "import Cython" 2>nul
if errorlevel 1 (
    echo ❌ Cython not found. Installing...
    pip install cython
)

REM Check if numpy is installed
python -c "import numpy" 2>nul
if errorlevel 1 (
    echo ❌ NumPy not found. Installing...
    pip install numpy
)

REM Build extension
echo 📦 Compiling extension...
python setup.py build_ext --inplace

if errorlevel 1 (
    echo ❌ Build failed!
    exit /b 1
)

REM Clean up build artifacts
echo 🧹 Cleaning up...
if exist build rmdir /s /q build
if exist blend.c del blend.c
if exist blend.cpp del blend.cpp

REM Check for compiled module
dir /b blend*.pyd >nul 2>&1
if errorlevel 1 (
    echo ❌ Build failed - no compiled module found
    exit /b 1
)

echo ✅ Build successful!
echo 🧪 Testing import...

python -c "from blend import blend_images_cy; print('✅ Import successful!')"
if errorlevel 1 (
    echo ⚠️  Module compiled but import failed. Check dependencies.
    exit /b 1
)

echo 🎉 Module is ready to use!

