# Передача проекта (Ubuntu) — LuchParsing

Документ описывает способы развертывания сервиса LuchParsing на сервере под управлением Ubuntu.

---

## Вариант A: Docker Compose

### Требования
- Ubuntu 20.04+
- Docker Engine
- Docker Compose plugin

### Установка и запуск

1) В каталоге проекта скопировать файл окружения:
```bash
cp .env.example .env
```

2) Отредактировать файл `.env`, указав значения:
- `PANEL_USER`
- `PANEL_PASSWORD`
- при необходимости другие параметры

3) Собрать и запустить сервис:
```bash
docker compose up -d --build
```

4) Доступ к сервису:
- API: `http://<ip_сервера>:8000/`
- Веб-панель: `http://<ip_сервера>:8000/panel`
  (точный путь зависит от маршрутов приложения)

### Хранение данных

- База данных SQLite хранится в каталоге `./database`
- Экспортированные XLSX-файлы сохраняются в каталоге `./exports`

Оба каталога смонтированы как volume и сохраняются между перезапусками контейнера.

### Остановка сервиса
```bash
docker compose down
```

### Просмотр логов
```bash
docker compose logs -f
```

---

## Вариант B: запуск без Docker (venv + systemd)

Альтернативный вариант для окружений, где Docker не используется.

### Установка системных зависимостей

```bash
sudo apt-get update
sudo apt-get install -y   python3   python3-venv   python3-pip   chromium-browser   chromium-chromedriver
```

### Установка Python-зависимостей

1) Создать виртуальное окружение:
```bash
python3 -m venv .venv
source .venv/bin/activate
```

2) Установить зависимости:
```bash
pip install -r requirements.txt
```

### Запуск приложения

Перед запуском необходимо задать переменные окружения из файла `.env`:

```bash
export $(grep -v '^#' .env | xargs)
```

Запуск сервера:
```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

Для продакшн-развертывания рекомендуется оформить запуск через `systemd`.

---

## Примечания

- Проект использует Selenium-адаптеры, которые требуют наличия **Chromium** и **chromedriver**
- В Docker-варианте браузер и драйвер устанавливаются автоматически
- Файлы `.env`, `database/*.db*` и `exports/*.xlsx` не рекомендуется хранить в git

