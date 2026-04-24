# Balance Snapshot API

Backend-сервис для сбора, хранения и сравнения снимков баланса аккаунтов через внешний API статистики.

Проект реализован на FastAPI и предназначен для автоматического сохранения баланса аккаунтов в определённые моменты времени. Сервис хранит список аккаунтов, получает актуальные данные из внешнего API, сохраняет snapshot-записи в базе данных и позволяет сравнивать текущие значения с историческими.

## Стек

- Python
- FastAPI
- SQLAlchemy Async
- SQLite
- httpx
- Pydantic
- Uvicorn
- python-dotenv
- Linux VPS
- systemd
- nginx

## Возможности

- Добавление и обновление аккаунтов
- Получение списка аккаунтов
- Активация и деактивация аккаунтов
- Ручной запуск snapshot через API
- Сохранение истории баланса в SQLite
- Получение последних snapshot-записей
- Сравнение текущего баланса с последним snapshot
- Сравнение текущего баланса со snapshot за конкретные минуты
- Синхронизация списка аккаунтов из внешней CRM
- Работа через proxy из переменных окружения
- Health-check endpoint
- Retry-механизм при ошибках внешнего API
- Подготовка к запуску на VPS через systemd

## Архитектура

Сервис состоит из нескольких основных частей:

1. **FastAPI-приложение** — предоставляет REST endpoints для управления аккаунтами, запуска snapshot и получения данных.
2. **SQLAlchemy Async** — используется для работы с базой данных.
3. **SQLite** — хранит аккаунты и историю snapshot-записей.
4. **httpx** — выполняет запросы к внешнему API статистики.
5. **systemd** — используется для запуска сервиса на VPS.
6. **nginx** — может использоваться как reverse proxy перед приложением.

## Основные endpoints

### Health-check

```http
GET /health

Проверяет состояние сервиса, количество аккаунтов в базе и базовые параметры конфигурации.
Добавить или обновить аккаунт
POST /models

Пример тела запроса:

{
  "username": "example_user",
  "token": "example_token",
  "is_active": true
}

Деактивировать аккаунт
POST /models/{username}/deactivate

Активировать аккаунт
POST /models/{username}/activate

Запустить snapshot
POST /snapshot/run
Получить историю snapshot
GET /snapshots?username=example_user&limit=100

Сравнить текущий баланс с последним snapshot
GET /balance/compare?username=example_user

Сравнить текущий баланс со snapshot за конкретное время
GET /balance/compare_times?username=example_user&day=2026-04-24&times=07:29,07:55

Установка
git clone https://github.com/your-username/balance-snapshot-api.git
cd balance-snapshot-api

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env

Пример запросов

Добавление аккаунта:

curl -X POST "http://127.0.0.1:8080/models" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "example_user",
    "token": "example_token",
    "is_active": true
  }'

Запуск snapshot:

curl -X POST "http://127.0.0.1:8080/snapshot/run"

Получение истории:

curl "http://127.0.0.1:8080/snapshots?username=example_user&limit=10"

Сравнение текущего баланса с последним snapshot:

curl "http://127.0.0.1:8080/balance/compare?username=example_user"
```
Безопасность

В репозитории не хранятся реальные токены, cookies, proxy URL, IP-адреса, .env файлы и база данных.

Все приватные значения должны передаваться через переменные окружения.

Файл .env.example содержит только пример конфигурации.

Что реализовано в проекте
REST API на FastAPI
Асинхронная работа с базой данных через SQLAlchemy Async
Модели для аккаунтов и snapshot-записей
Получение данных из внешнего API через httpx
Retry-механизм и timeout для внешних запросов
Сохранение истории баланса
Сравнение текущих и исторических значений
Поиск snapshot по конкретному временному окну
Синхронизация аккаунтов из внешней CRM
Активация и деактивация аккаунтов
Health-check endpoint
Подготовка сервиса к запуску на VPS через systemd
Цель проекта

Проект был создан как backend-сервис для автоматизации регулярного сбора статистических данных, их хранения и последующего сравнения. Основной акцент сделан на практическую серверную разработку: API, базу данных, обработку ошибок, интеграцию с внешним сервисом и деплой на Linux-сервер.
