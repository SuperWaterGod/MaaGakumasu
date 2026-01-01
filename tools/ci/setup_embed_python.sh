#!/bin/bash
# macOS 嵌入式 Python 安装脚本
# 使用 python-build-standalone 提供的独立 Python 环境

set -e

# 基本变量
PYTHON_VERSION="3.12.9"
DEST_DIR="install/python"
SCRIPTS_DIR="tools/ci"

# 获取架构参数
ARCH="${1:-aarch64}"

# 确定下载 URL
# python-build-standalone 使用的格式: cpython-{version}+{build}-{arch}-apple-darwin-install_only.tar.gz
# 参考: https://github.com/astral-sh/python-build-standalone/releases
PBS_TAG="20250317"  # python-build-standalone release tag

if [ "$ARCH" == "aarch64" ]; then
    PLATFORM="aarch64-apple-darwin"
elif [ "$ARCH" == "x86_64" ]; then
    PLATFORM="x86_64-apple-darwin"
else
    echo "Error: Unsupported architecture: $ARCH"
    echo "Supported architectures: aarch64, x86_64"
    exit 1
fi

PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/cpython-${PYTHON_VERSION}+${PBS_TAG}-${PLATFORM}-install_only.tar.gz"

echo "=== macOS Embedded Python Setup ==="
echo "Python Version: $PYTHON_VERSION"
echo "Architecture: $ARCH"
echo "Platform: $PLATFORM"

# 创建目标目录
mkdir -p "$DEST_DIR"
echo "Created directory: $DEST_DIR"

# 检查 Python 是否已经存在
PYTHON_EXE="$DEST_DIR/bin/python3"
if [ -f "$PYTHON_EXE" ]; then
    echo "Python already exists in $DEST_DIR, skipping download."
else
    # 下载 Python
    PYTHON_TAR="python-standalone.tar.gz"
    echo "Downloading Python from: $PYTHON_URL"
    curl -L -o "$PYTHON_TAR" "$PYTHON_URL"

    # 解压 Python
    echo "Extracting Python to: $DEST_DIR"
    tar -xzf "$PYTHON_TAR" -C "install"
    rm "$PYTHON_TAR"
    
    echo "Python extracted successfully."
fi

# 复制 setup_pip.py
SETUP_PIP_SOURCE="$SCRIPTS_DIR/setup_pip.py"
SETUP_PIP_DEST="$DEST_DIR/setup_pip.py"

if [ -f "$SETUP_PIP_SOURCE" ]; then
    cp "$SETUP_PIP_SOURCE" "$SETUP_PIP_DEST"
    echo "Copied setup_pip.py to $DEST_DIR"
else
    echo "Error: $SETUP_PIP_SOURCE not found"
    exit 1
fi

# 检查并安装 pip
echo "Checking pip installation..."
cd "$DEST_DIR"

if ./bin/python3 -m pip --version > /dev/null 2>&1; then
    echo "pip is already installed."
else
    echo "Installing pip..."
    ./bin/python3 setup_pip.py
    echo "pip installed successfully."
fi

echo "=== macOS Embedded Python Setup Complete ==="
