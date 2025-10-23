# 🤖 Nutrition Bot - Персональный AI-бот питания

[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org/)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-blue.svg)](https://core.telegram.org/bots)
[![Flask](https://img.shields.io/badge/Flask-2.3-green.svg)](https://flask.palletsprojects.com/)
[![Render](https://img.shields.io/badge/Deploy-Render-blueviolet.svg)](https://render.com)

Персональный Telegram-бот для создания индивидуальных планов питания с использованием искусственного интеллекта Yandex GPT.

## 🚀 Быстрый старт

### 1. Получить Telegram Bot Token
1. Напишите [@BotFather](https://t.me/BotFather) в Telegram
2. Используйте команду `/newbot`
3. Следуйте инструкциям и получите токен
4. Сохраните токен для следующего шага

### 2. Развертывание на Render.com

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy)

**Ручное развертывание:**
1. Форкните этот репозиторий
2. Создайте аккаунт на [Render.com](https://render.com)
3. Нажмите "New Web Service"
4. Подключите ваш GitHub репозиторий
5. Заполните настройки:

**Environment Variables:**
```env
BOT_TOKEN=your_telegram_bot_token_here
YANDEX_API_KEY=your_yandex_gpt_api_key_optional
YANDEX_FOLDER_ID=your_yandex_folder_id_optional
PORT=10000
