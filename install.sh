#!/bin/bash

# ==============================================================================
# Скрипт полной установки Marzban Localized (RU) с нуля
# ==============================================================================

set -e

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() {
    echo -e "${GREEN}[$(date +"%H:%M:%S")] $1${NC}"
}

warn() {
    echo -e "${YELLOW}[$(date +"%H:%M:%S")] ВНИМАНИЕ: $1${NC}"
}

error() {
    echo -e "${RED}[$(date +"%H:%M:%S")] ОШИБКА: $1${NC}"
    exit 1
}

# 1. Проверка прав root
if [ "$EUID" -ne 0 ]; then
    error "Пожалуйста, запустите скрипт от имени root (sudo)"
fi

log "Начинаю установку Marzban Localized (RU)..."

# 2. Установка зависимостей
install_dependencies() {
    log "Проверка и установка системных зависимостей..."
    apt-get update
    apt-get install -y curl git socat tar
    
    if ! command -v docker &> /dev/null; then
        log "Установка Docker..."
        curl -fsSL https://get.docker.com | sh
    fi

    if ! command -v docker-compose &> /dev/null; then
        log "Установка Docker Compose..."
        LATEST_VERSION=$(curl -s https://api.github.com/repos/docker/compose/releases/latest | grep 'tag_name' | cut -d\" -f4)
        curl -L "https://github.com/docker/compose/releases/download/${LATEST_VERSION}/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
        chmod +x /usr/local/bin/docker-compose
    fi
}

# 3. Подготовка директории
setup_directory() {
    INSTALL_DIR="/var/lib/marzban"
    if [ -d "$INSTALL_DIR" ]; then
        warn "Директория $INSTALL_DIR уже существует."
        read -p "Хотите переустановить? (y/n): " confirm
        if [ "$confirm" != "y" ]; then
            log "Установка отменена."
            exit 0
        fi
        rm -rf "$INSTALL_DIR"
    fi

    log "Клонирование репозитория в $INSTALL_DIR..."
    git clone https://github.com/kellisfen/Marzban-RU-Xray-v26.git "$INSTALL_DIR"
    cd "$INSTALL_DIR"
}

# 4. Настройка окружения
configure_env() {
    log "Настройка .env файла..."
    if [ ! -f ".env" ]; then
        cp .env.example .env
        log "Создан .env из шаблона. Пожалуйста, отредактируйте его позже для смены токенов."
    fi
}

# 5. Запуск
launch() {
    log "Запуск контейнеров Marzban..."
    docker-compose up -d
    
    log "Ожидание запуска (10 сек)..."
    sleep 10
    
    if docker ps | grep -q "marzban"; then
        log "====================================================="
        log "УСТАНОВКА ЗАВЕРШЕНА УСПЕШНО!"
        log "Панель доступна по адресу вашего сервера."
        log "Логи установки: docker-compose logs -f marzban"
        log "Директория проекта: /var/lib/marzban"
        log "====================================================="
    else
        error "Контейнеры не запустились. Проверьте логи: docker-compose logs"
    fi
}

# Основной процесс
install_dependencies
setup_directory
configure_env
launch
