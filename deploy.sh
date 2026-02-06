#!/bin/bash

# ==============================================================================
# Скрипт автоматического развертывания Marzban (Localized)
# ==============================================================================

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Настройки
PROJECT_DIR="/var/lib/marzban"
BACKUP_DIR="/var/lib/marzban/backups"
LOG_FILE="/var/log/marzban_deploy.log"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
REPO_URL="https://github.com/kellisfen/Marzban-RU-Xray-v26.git" # Замените на ваш репозиторий

# Загрузка переменных окружения
if [ -f "$PROJECT_DIR/.env" ]; then
    export $(grep -v '^#' "$PROJECT_DIR/.env" | xargs)
fi

# Функция логирования
log() {
    echo -e "$(date +"%Y-%m-%d %H:%M:%S") : $1" | tee -a "$LOG_FILE"
}

error_exit() {
    log "${RED}ОШИБКА: $1${NC}"
    rollback
    send_notification "❌ Ошибка развертывания: $1"
    exit 1
}

# Функция уведомления (Telegram)
send_notification() {
    if [ ! -z "$TELEGRAM_LOGGER_CHANNEL_ID" ] && [ ! -z "$TELEGRAM_API_TOKEN" ]; then
        local message="$1"
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_API_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_LOGGER_CHANNEL_ID}" \
            -d "text=${message}" \
            -d "parse_mode=HTML" > /dev/null
    fi
}

# 1. Проверка обновлений
check_updates() {
    log "${YELLOW}Проверка обновлений в репозитории...${NC}"
    cd "$PROJECT_DIR" || error_exit "Не удалось перейти в директорию проекта"
    
    git fetch origin
    LOCAL=$(git rev-parse HEAD)
    REMOTE=$(git rev-parse @{u})
    
    if [ "$LOCAL" = "$REMOTE" ]; then
        log "${GREEN}У вас установлена последняя версия.${NC}"
        # Можно добавить флаг принудительного обновления
    else
        log "${YELLOW}Доступна новая версия. Начинаю процесс обновления...${NC}"
    fi
}

# 2. Резервное копирование
backup() {
    log "${YELLOW}Создание резервной копии...${NC}"
    mkdir -p "$BACKUP_DIR"
    tar -czf "$BACKUP_DIR/backup_$TIMESTAMP.tar.gz" -C "$PROJECT_DIR" . --exclude="./backups" || error_exit "Не удалось создать резервную копию"
    log "${GREEN}Бэкап создан: $BACKUP_DIR/backup_$TIMESTAMP.tar.gz${NC}"
}

# 3. Откат в случае неудачи
rollback() {
    log "${YELLOW}Выполняется откат системы...${NC}"
    LATEST_BACKUP=$(ls -t "$BACKUP_DIR"/backup_*.tar.gz | head -n 1)
    if [ -f "$LATEST_BACKUP" ]; then
        tar -xzf "$LATEST_BACKUP" -C "$PROJECT_DIR"
        docker-compose up -d
        log "${GREEN}Откат завершен успешно из $LATEST_BACKUP${NC}"
    else
        log "${RED}Резервные копии не найдены для отката!${NC}"
    fi
}

# 4. Процесс развертывания
deploy() {
    log "${YELLOW}Остановка служб...${NC}"
    docker-compose down || error_exit "Не удалось остановить контейнеры"

    log "${YELLOW}Получение кода...${NC}"
    git pull origin main || error_exit "Не удалось загрузить код из репозитория"

    log "${YELLOW}Обновление образов и запуск...${NC}"
    docker-compose pull || log "${YELLOW}Не удалось обновить образы, использую локальные${NC}"
    docker-compose up -d || error_exit "Не удалось запустить контейнеры"

    log "${YELLOW}Проверка состояния служб...${NC}"
    sleep 10
    if docker ps | grep -q "marzban"; then
        log "${GREEN}Контейнер Marzban успешно запущен.${NC}"
    else
        error_exit "Контейнер Marzban не запустился"
    fi

    # Миграции в Marzban обычно выполняются автоматически при запуске контейнера,
    # но можно вызвать их принудительно если нужно:
    # docker-compose exec -T marzban python3 main.py db upgrade
}

# 5. Тестирование
run_tests() {
    log "${YELLOW}Запуск автоматических тестов...${NC}"
    # Пример проверки доступности API
    if curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/api/docs | grep -q "200"; then
        log "${GREEN}API доступно. Тесты пройдены.${NC}"
    else
        error_exit "API недоступно после развертывания"
    fi
}

# Главный цикл
main() {
    log "${YELLOW}=== Начало развертывания ===${NC}"
    check_updates
    backup
    deploy
    run_tests
    log "${GREEN}=== Развертывание успешно завершено ===${NC}"
    send_notification "✅ <b>Marzban Deploy:</b> Успешно обновлено до последней версии.
📅 Дата: <code>$(date +"%Y-%m-%d %H:%M:%S")</code>"
}

main
