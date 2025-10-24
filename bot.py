import os
import logging
import threading
import time
import sqlite3
import json
import requests
import signal
import atexit
import socket
import sys
import re
from datetime import datetime, timedelta
from flask import Flask, jsonify, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
from telegram.error import TelegramError

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== КОНФИГУРАЦИЯ ====================

ADMIN_USER_ID = 362423055
YANDEX_API_KEY = os.getenv('YANDEX_API_KEY')
YANDEX_FOLDER_ID = os.getenv('YANDEX_FOLDER_ID')
YANDEX_GPT_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
BOT_TOKEN = os.getenv('BOT_TOKEN')
RENDER_EXTERNAL_URL = os.getenv('RENDER_EXTERNAL_URL')

# ==================== HEALTH CHECK МОНИТОРИНГ ====================

class HealthMonitor:
    def __init__(self):
        self.start_time = datetime.now()
        self.request_count = 0
        self.error_count = 0
        self.last_health_check = datetime.now()
        self.bot_status = "initializing"
        self.db_status = "unknown"
        self.telegram_api_status = "unknown"
        self.yandex_gpt_status = "unknown"
        
    def increment_request(self):
        self.request_count += 1
        
    def increment_error(self):
        self.error_count += 1
        
    def update_bot_status(self, status):
        self.bot_status = status
        self.last_health_check = datetime.now()
        
    def update_db_status(self, status):
        self.db_status = status
        
    def update_telegram_status(self, status):
        self.telegram_api_status = status
        
    def update_yandex_gpt_status(self, status):
        self.yandex_gpt_status = status
        
    def get_uptime(self):
        return datetime.now() - self.start_time
        
    def get_stats(self):
        return {
            "uptime_seconds": int(self.get_uptime().total_seconds()),
            "request_count": self.request_count,
            "error_count": self.error_count,
            "success_rate": ((self.request_count - self.error_count) / self.request_count * 100) if self.request_count > 0 else 100,
            "last_health_check": self.last_health_check.isoformat(),
            "bot_status": self.bot_status,
            "db_status": self.db_status,
            "telegram_api_status": self.telegram_api_status,
            "yandex_gpt_status": self.yandex_gpt_status
        }

# Глобальный монитор здоровья
health_monitor = HealthMonitor()

# ==================== БАЗА ДАННЫХ ====================

def init_database():
    """Инициализация базы данных с проверкой здоровья"""
    try:
        conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('PRAGMA journal_mode=WAL')
        cursor.execute('PRAGMA synchronous=NORMAL')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS nutrition_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                weight REAL,
                waist_circumference INTEGER,
                wellbeing_score INTEGER,
                sleep_quality INTEGER,
                date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_limits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE NOT NULL,
                last_plan_date TIMESTAMP,
                plan_count INTEGER DEFAULT 0
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS shopping_cart (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                ingredient TEXT NOT NULL,
                checked BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()
        
        health_monitor.update_db_status("healthy")
        logger.info("✅ Database initialized successfully")
        
    except Exception as e:
        health_monitor.update_db_status("error")
        logger.error(f"❌ Database initialization failed: {e}")
        raise

def save_user(user_data):
    """Сохраняет пользователя в БД"""
    conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            INSERT OR REPLACE INTO users (user_id, username, first_name, last_name)
            VALUES (?, ?, ?, ?)
        ''', (user_data['user_id'], user_data['username'], user_data['first_name'], user_data['last_name']))
        conn.commit()
    except Exception as e:
        logger.error(f"Error saving user: {e}")
    finally:
        conn.close()

def is_admin(user_id):
    return user_id == ADMIN_USER_ID

def can_make_request(user_id):
    """Проверяет, может ли пользователь сделать запрос плана"""
    try:
        if is_admin(user_id):
            return True
            
        conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('SELECT last_plan_date FROM user_limits WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        if not result:
            return True
            
        last_plan_date = datetime.fromisoformat(result[0])
        days_since_last_plan = (datetime.now() - last_plan_date).days
        
        conn.close()
        return days_since_last_plan >= 7
        
    except Exception as e:
        logger.error(f"Error checking request limit: {e}")
        return True

def update_user_limit(user_id):
    """Обновляет лимиты пользователя после создания плана"""
    try:
        if is_admin(user_id):
            return
            
        conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
        cursor = conn.cursor()
        
        current_time = datetime.now().isoformat()
        cursor.execute('''
            INSERT OR REPLACE INTO user_limits (user_id, last_plan_date, plan_count)
            VALUES (?, ?, COALESCE((SELECT plan_count FROM user_limits WHERE user_id = ?), 0) + 1)
        ''', (user_id, current_time, user_id))
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        logger.error(f"Error updating user limits: {e}")

def get_days_until_next_plan(user_id):
    """Возвращает количество дней до следующего доступного плана"""
    try:
        if is_admin(user_id):
            return 0
            
        conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('SELECT last_plan_date FROM user_limits WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        if not result:
            return 0
            
        last_plan_date = datetime.fromisoformat(result[0])
        days_passed = (datetime.now() - last_plan_date).days
        days_remaining = 7 - days_passed
        
        conn.close()
        return max(0, days_remaining)
        
    except Exception as e:
        logger.error(f"Error getting days until next plan: {e}")
        return 0

def save_plan(user_id, plan_data):
    """Сохраняет план питания в БД"""
    conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        cursor.execute('INSERT INTO nutrition_plans (user_id, plan_data) VALUES (?, ?)', 
                      (user_id, json.dumps(plan_data)))
        plan_id = cursor.lastrowid
        conn.commit()
        return plan_id
    except Exception as e:
        logger.error(f"Error saving plan: {e}")
        return None
    finally:
        conn.close()

def save_checkin(user_id, weight, waist, wellbeing, sleep):
    """Сохраняет ежедневный чек-ин"""
    conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            INSERT INTO daily_checkins (user_id, weight, waist_circumference, wellbeing_score, sleep_quality)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, weight, waist, wellbeing, sleep))
        conn.commit()
    except Exception as e:
        logger.error(f"Error saving checkin: {e}")
    finally:
        conn.close()

def get_user_stats(user_id):
    """Получает статистику пользователя"""
    conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            SELECT date, weight, waist_circumference, wellbeing_score, sleep_quality
            FROM daily_checkins WHERE user_id = ? ORDER BY date DESC LIMIT 7
        ''', (user_id,))
        checkins = cursor.fetchall()
        return checkins
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        return []
    finally:
        conn.close()

def get_latest_plan(user_id):
    """Получает последний план пользователя"""
    conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            SELECT plan_data FROM nutrition_plans 
            WHERE user_id = ? ORDER BY created_at DESC LIMIT 1
        ''', (user_id,))
        result = cursor.fetchone()
        return json.loads(result[0]) if result else None
    except Exception as e:
        logger.error(f"Error getting latest plan: {e}")
        return None
    finally:
        conn.close()

def get_user_plans_count(user_id):
    """Получает количество планов пользователя"""
    conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        cursor.execute('SELECT COUNT(*) FROM nutrition_plans WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        return result[0] if result else 0
    except Exception as e:
        logger.error(f"Error getting user plans count: {e}")
        return 0
    finally:
        conn.close()

def save_shopping_cart(user_id, ingredients):
    """Сохраняет корзину покупок"""
    conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        cursor.execute('DELETE FROM shopping_cart WHERE user_id = ?', (user_id,))
        
        for ingredient in ingredients:
            cursor.execute('''
                INSERT INTO shopping_cart (user_id, ingredient, checked)
                VALUES (?, ?, ?)
            ''', (user_id, ingredient, False))
        
        conn.commit()
    except Exception as e:
        logger.error(f"Error saving shopping cart: {e}")
    finally:
        conn.close()

def get_shopping_cart(user_id):
    """Получает корзину покупок пользователя"""
    conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            SELECT id, ingredient, checked FROM shopping_cart 
            WHERE user_id = ? ORDER BY created_at
        ''', (user_id,))
        items = cursor.fetchall()
        return items
    except Exception as e:
        logger.error(f"Error getting shopping cart: {e}")
        return []
    finally:
        conn.close()

def update_shopping_item(item_id, checked):
    """Обновляет статус элемента корзины"""
    conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        cursor.execute('UPDATE shopping_cart SET checked = ? WHERE id = ?', (checked, item_id))
        conn.commit()
    except Exception as e:
        logger.error(f"Error updating shopping item: {e}")
    finally:
        conn.close()

def clear_shopping_cart(user_id):
    """Очищает корзину покупок"""
    conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        cursor.execute('DELETE FROM shopping_cart WHERE user_id = ?', (user_id,))
        conn.commit()
    except Exception as e:
        logger.error(f"Error clearing shopping cart: {e}")
    finally:
        conn.close()

def check_database_health():
    """Проверяет здоровье базы данных"""
    try:
        conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
        table_count = cursor.fetchone()[0]
        
        required_tables = ['users', 'nutrition_plans', 'daily_checkins', 'user_limits', 'shopping_cart']
        for table in required_tables:
            cursor.execute(f"SELECT COUNT(*) FROM {table} LIMIT 1")
        
        conn.close()
        
        health_monitor.update_db_status("healthy")
        return True
        
    except Exception as e:
        health_monitor.update_db_status("error")
        logger.error(f"❌ Database health check failed: {e}")
        return False

def check_telegram_api_health():
    """Проверяет доступность Telegram API"""
    try:
        response = requests.get(f'https://api.telegram.org/bot{BOT_TOKEN}/getMe', timeout=10)
        if response.status_code == 200:
            health_monitor.update_telegram_status("healthy")
            return True
        else:
            health_monitor.update_telegram_status("error")
            return False
    except Exception as e:
        health_monitor.update_telegram_status("error")
        logger.error(f"❌ Telegram API health check failed: {e}")
        return False

def check_yandex_gpt_health():
    """Проверяет доступность Yandex GPT API"""
    try:
        if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
            health_monitor.update_yandex_gpt_status("not_configured")
            return True
            
        headers = {
            'Authorization': f'Api-Key {YANDEX_API_KEY}',
            'Content-Type': 'application/json'
        }
        
        data = {
            "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/latest",
            "completionOptions": {
                "stream": False,
                "temperature": 0.7,
                "maxTokens": 10
            },
            "messages": [
                {
                    "role": "user",
                    "text": "Ответь 'OK'"
                }
            ]
        }
        
        response = requests.post(YANDEX_GPT_URL, headers=headers, json=data, timeout=15)
        if response.status_code == 200:
            health_monitor.update_yandex_gpt_status("healthy")
            return True
        else:
            health_monitor.update_yandex_gpt_status("error")
            return False
            
    except Exception as e:
        health_monitor.update_yandex_gpt_status("error")
        logger.error(f"❌ Yandex GPT health check failed: {e}")
        return False

# ==================== YANDEX GPT ИНТЕГРАЦИЯ ====================

class YandexGPT:
    def __init__(self):
        self.api_key = YANDEX_API_KEY
        self.folder_id = YANDEX_FOLDER_ID
        self.url = YANDEX_GPT_URL
    
    def generate_nutrition_plan(self, user_data):
        """Генерирует план питания через Yandex GPT"""
        try:
            if not self.api_key or not self.folder_id:
                logger.warning("Yandex GPT credentials not set, using demo data")
                return self._generate_demo_plan(user_data)
            
            prompt = self._create_prompt(user_data)
            
            headers = {
                'Authorization': f'Api-Key {self.api_key}',
                'Content-Type': 'application/json'
            }
            
            data = {
                "modelUri": f"gpt://{self.folder_id}/yandexgpt/latest",
                "completionOptions": {
                    "stream": False,
                    "temperature": 0.7,
                    "maxTokens": 4000
                },
                "messages": [
                    {
                        "role": "system",
                        "text": """Ты - профессиональный диетолог. Создай персонализированный план питания на 7 дней. Формат строго JSON."""
                    },
                    {
                        "role": "user",
                        "text": prompt
                    }
                ]
            }
            
            response = requests.post(self.url, headers=headers, json=data, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                plan_text = result['result']['alternatives'][0]['message']['text']
                
                json_match = re.search(r'\{.*\}', plan_text, re.DOTALL)
                if json_match:
                    plan_json = json.loads(json_match.group())
                    plan_json['user_data'] = user_data
                    return plan_json
                else:
                    logger.error("No JSON found in GPT response")
                    return self._generate_demo_plan(user_data)
            else:
                logger.error(f"Yandex GPT API error: {response.status_code}")
                return self._generate_demo_plan(user_data)
                
        except Exception as e:
            logger.error(f"Error generating plan with Yandex GPT: {e}")
            return self._generate_demo_plan(user_data)
    
    def _create_prompt(self, user_data):
        """Создает промпт для GPT"""
        gender = user_data.get('gender', '')
        age = user_data.get('age', '')
        height = user_data.get('height', '')
        weight = user_data.get('weight', '')
        goal = user_data.get('goal', '')
        activity = user_data.get('activity', '')
        
        prompt = f"""
Создай персонализированный план питания на 7 дней со следующими параметрами:

Пол: {gender}
Возраст: {age} лет
Рост: {height} см
Вес: {weight} кг
Цель: {goal}
Уровень активности: {activity}

Требования:
- Разнообразные блюда каждый день
- Практичные рецепты с доступными ингредиентами
- Сбалансированное питание
- Учет цели {goal}
- 5 приемов пищи в день
- Указание калорийности для каждого приема пищи
- Список ингредиентов с количествами
- Пошаговые инструкции приготовления
- Время приготовления

Верни ответ ТОЛЬКО в формате JSON без дополнительного текста.
"""
        return prompt
    
    def _generate_demo_plan(self, user_data):
        """Резервный демо-план"""
        days = ['ПОНЕДЕЛЬНИК', 'ВТОРНИК', 'СРЕДА', 'ЧЕТВЕРГ', 'ПЯТНИЦА', 'СУББОТА', 'ВОСКРЕСЕНЬЕ']
        
        plan = {
            'user_data': user_data,
            'days': []
        }
        
        for day_name in days:
            day_plan = {
                'name': day_name,
                'total_calories': '1800-2000 ккал',
                'meals': [
                    {
                        'type': 'ЗАВТРАК',
                        'time': '08:00',
                        'emoji': '🍳',
                        'name': 'Овсянка с фруктами',
                        'calories': '350 ккал',
                        'ingredients': '• Овсяные хлопья - 50г\n• Молоко - 200мл\n• Банан - 1 шт\n• Мед - 1 ч.л.',
                        'instructions': '1. Сварите овсянку на молоке\n2. Добавьте банан и мед',
                        'cooking_time': '15 мин'
                    },
                    {
                        'type': 'ПЕРЕКУС 1', 
                        'time': '11:00',
                        'emoji': '🥗',
                        'name': 'Йогурт с орехами',
                        'calories': '200 ккал',
                        'ingredients': '• Греческий йогурт - 150г\n• Миндаль - 30г\n• Ягоды - 50г',
                        'instructions': '1. Смешайте йогурт с орехами\n2. Добавьте ягоды',
                        'cooking_time': '2 мин'
                    },
                    {
                        'type': 'ОБЕД',
                        'time': '14:00', 
                        'emoji': '🍲',
                        'name': 'Куриная грудка с гречкой',
                        'calories': '450 ккал',
                        'ingredients': '• Куриная грудка - 150г\n• Гречка - 100г\n• Овощи - 200г\n• Масло оливковое - 1 ст.л.',
                        'instructions': '1. Отварите гречку\n2. Обжарьте куриную грудку\n3. Потушите овощи',
                        'cooking_time': '25 мин'
                    },
                    {
                        'type': 'ПЕРЕКУС 2',
                        'time': '17:00',
                        'emoji': '🍎', 
                        'name': 'Творог с фруктами',
                        'calories': '180 ккал',
                        'ingredients': '• Творог обезжиренный - 150г\n• Яблоко - 1 шт\n• Корица - щепотка',
                        'instructions': '1. Нарежьте яблоко\n2. Смешайте с творогом\n3. Посыпьте корицей',
                        'cooking_time': '5 мин'
                    },
                    {
                        'type': 'УЖИН',
                        'time': '20:00',
                        'emoji': '🍛',
                        'name': 'Рыба на пару с овощами',
                        'calories': '400 ккал', 
                        'ingredients': '• Филе рыбы - 200г\n• Брокколи - 150г\n• Морковь - 1 шт\n• Лимон - 1 долька',
                        'instructions': '1. Приготовьте рыбу на пару\n2. Отварите овощи\n3. Подавайте с лимоном',
                        'cooking_time': '20 мин'
                    }
                ]
            }
            plan['days'].append(day_plan)
        
        return plan

# ==================== ИНТЕРАКТИВНЫЕ МЕНЮ ====================

class InteractiveMenu:
    def __init__(self):
        self.days = ['ПОНЕДЕЛЬНИК', 'ВТОРНИК', 'СРЕДА', 'ЧЕТВЕРГ', 'ПЯТНИЦА', 'СУББОТА', 'ВОСКРЕСЕНЬЕ']
        self.meals = ['ЗАВТРАК', 'ПЕРЕКУС 1', 'ОБЕД', 'ПЕРЕКУС 2', 'УЖИН']
    
    def get_main_menu(self):
        """Главное меню команд"""
        keyboard = [
            [InlineKeyboardButton("📊 СОЗДАТЬ ПЛАН", callback_data="create_plan")],
            [InlineKeyboardButton("📈 ЧЕК-ИН", callback_data="checkin")],
            [InlineKeyboardButton("📊 СТАТИСТИКА", callback_data="stats")],
            [InlineKeyboardButton("📋 МОЙ ПЛАН", callback_data="my_plan")],
            [InlineKeyboardButton("🛒 КОРЗИНА", callback_data="shopping_cart")],
            [InlineKeyboardButton("❓ ПОМОЩЬ", callback_data="help")]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def get_plan_data_input(self, step=1):
        """Клавиатура для ввода данных плана"""
        if step == 1:
            keyboard = [
                [InlineKeyboardButton("👨 МУЖЧИНА", callback_data="gender_male")],
                [InlineKeyboardButton("👩 ЖЕНЩИНА", callback_data="gender_female")],
                [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_main")]
            ]
        elif step == 2:
            keyboard = [
                [InlineKeyboardButton("🎯 ПОХУДЕНИЕ", callback_data="goal_weight_loss")],
                [InlineKeyboardButton("💪 НАБОР МАССЫ", callback_data="goal_mass")],
                [InlineKeyboardButton("⚖️ ПОДДЕРЖАНИЕ", callback_data="goal_maintain")],
                [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_gender")]
            ]
        elif step == 3:
            keyboard = [
                [InlineKeyboardButton("🏃‍♂️ ВЫСОКАЯ", callback_data="activity_high")],
                [InlineKeyboardButton("🚶‍♂️ СРЕДНЯЯ", callback_data="activity_medium")],
                [InlineKeyboardButton("💤 НИЗКАЯ", callback_data="activity_low")],
                [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_goal")]
            ]
        
        return InlineKeyboardMarkup(keyboard)
    
    def get_checkin_menu(self):
        """Меню для чек-ина"""
        keyboard = [
            [InlineKeyboardButton("✅ ЗАПИСАТЬ ДАННЫЕ", callback_data="checkin_data")],
            [InlineKeyboardButton("📊 ПОСМОТРЕТЬ ИСТОРИЮ", callback_data="checkin_history")],
            [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_main")]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def get_plan_management_menu(self):
        """Меню управления планами"""
        keyboard = [
            [InlineKeyboardButton("📅 ПРОСМОТРЕТЬ НЕДЕЛЮ", callback_data="view_week")],
            [InlineKeyboardButton("📄 СКАЧАТЬ В TXT", callback_data="download_plan")],
            [InlineKeyboardButton("📊 ИНФО О ПЛАНАХ", callback_data="plan_info")],
            [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_main")]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def get_week_days_menu(self):
        """Меню выбора дня недели"""
        keyboard = [
            [InlineKeyboardButton("📅 ПОНЕДЕЛЬНИК", callback_data="day_0")],
            [InlineKeyboardButton("📅 ВТОРНИК", callback_data="day_1")],
            [InlineKeyboardButton("📅 СРЕДА", callback_data="day_2")],
            [InlineKeyboardButton("📅 ЧЕТВЕРГ", callback_data="day_3")],
            [InlineKeyboardButton("📅 ПЯТНИЦА", callback_data="day_4")],
            [InlineKeyboardButton("📅 СУББОТА", callback_data="day_5")],
            [InlineKeyboardButton("📅 ВОСКРЕСЕНЬЕ", callback_data="day_6")],
            [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_to_plan_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def get_day_meals_menu(self, day_index):
        """Меню приемов пищи для конкретного дня"""
        meals = ['🍳 ЗАВТРАК', '🥗 ПЕРЕКУС 1', '🍲 ОБЕД', '🍎 ПЕРЕКУС 2', '🍛 УЖИН']
        keyboard = []
        
        for i, meal in enumerate(meals):
            keyboard.append([InlineKeyboardButton(meal, callback_data=f"meal_{day_index}_{i}")])
        
        keyboard.append([InlineKeyboardButton("📅 ВЫБРАТЬ ДРУГОЙ ДЕНЬ", callback_data="view_week")])
        keyboard.append([InlineKeyboardButton("↩️ НАЗАД В МЕНЮ", callback_data="back_to_plan_menu")])
        
        return InlineKeyboardMarkup(keyboard)
    
    def get_meal_detail_menu(self, day_index, meal_index):
        """Меню деталей приема пищи"""
        keyboard = [
            [InlineKeyboardButton("📅 СЛЕДУЮЩИЙ ПРИЕМ ПИЩИ", callback_data=f"next_meal_{day_index}_{meal_index}")],
            [InlineKeyboardButton("📅 ВЫБРАТЬ ДРУГОЙ ДЕНЬ", callback_data="view_week")],
            [InlineKeyboardButton("↩️ НАЗАД В МЕНЮ", callback_data="back_to_plan_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def get_shopping_cart_menu(self, items, page=0):
        """Меню корзины покупок с галочками"""
        items_per_page = 10
        start_idx = page * items_per_page
        end_idx = start_idx + items_per_page
        current_items = items[start_idx:end_idx]
        
        keyboard = []
        
        for item in current_items:
            item_id, ingredient, checked = item
            status = "✅" if checked else "⬜"
            keyboard.append([
                InlineKeyboardButton(f"{status} {ingredient}", callback_data=f"toggle_{item_id}")
            ])
        
        navigation_buttons = []
        if page > 0:
            navigation_buttons.append(InlineKeyboardButton("◀️ НАЗАД", callback_data=f"cart_page_{page-1}"))
        
        if end_idx < len(items):
            navigation_buttons.append(InlineKeyboardButton("ВПЕРЕД ▶️", callback_data=f"cart_page_{page+1}"))
        
        if navigation_buttons:
            keyboard.append(navigation_buttons)
        
        keyboard.extend([
            [InlineKeyboardButton("🔄 ОБНОВИТЬ СПИСОК ИЗ ПЛАНА", callback_data="refresh_cart")],
            [InlineKeyboardButton("🧹 ОЧИСТИТЬ КОРЗИНУ", callback_data="clear_cart")],
            [InlineKeyboardButton("📄 СКАЧАТЬ СПИСОК", callback_data="download_shopping_list")],
            [InlineKeyboardButton("↩️ НАЗАД В МЕНЮ", callback_data="back_main")]
        ])
        
        return InlineKeyboardMarkup(keyboard)
    
    def get_back_menu(self):
        """Меню с кнопкой назад"""
        keyboard = [
            [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_main")]
        ]
        return InlineKeyboardMarkup(keyboard)

# ==================== FLASK APP ====================

app = Flask(__name__)
bot_instance = None

@app.route('/')
def home():
    health_monitor.increment_request()
    stats = health_monitor.get_stats()
    status_emoji = "✅" if health_monitor.bot_status == "healthy" else "❌"
    
    return f"""
    <h1>🤖 Nutrition Bot Status {status_emoji}</h1>
    <p>Бот для создания персональных планов питания</p>
    <p><strong>Uptime:</strong> {stats['uptime_seconds']} seconds</p>
    <p><strong>Status:</strong> {health_monitor.bot_status.upper()}</p>
    <p><strong>Requests:</strong> {stats['request_count']}</p>
    <p><a href="/health">Health Check</a> | <a href="/ping">Ping</a> | <a href="/wakeup">Wakeup</a></p>
    <p>🕒 Last update: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    """

@app.route('/health')
def health_check():
    health_monitor.increment_request()
    
    db_healthy = check_database_health()
    telegram_healthy = check_telegram_api_health()
    yandex_healthy = check_yandex_gpt_health()
    
    all_healthy = db_healthy and telegram_healthy and (yandex_healthy or health_monitor.yandex_gpt_status == "not_configured")
    
    if all_healthy:
        health_monitor.update_bot_status("healthy")
        status_code = 200
    else:
        health_monitor.update_bot_status("degraded")
        status_code = 503
    
    response = {
        "status": "healthy" if all_healthy else "degraded",
        "timestamp": datetime.now().isoformat(),
        "service": "nutrition-bot",
        "version": "2.0",
        "checks": {
            "database": health_monitor.db_status,
            "telegram_api": health_monitor.telegram_api_status,
            "yandex_gpt": health_monitor.yandex_gpt_status
        },
        "stats": health_monitor.get_stats()
    }
    
    return jsonify(response), status_code

@app.route('/ping')
def ping():
    health_monitor.increment_request()
    return jsonify({"status": "pong", "timestamp": datetime.now().isoformat()})

@app.route('/wakeup')
def wakeup():
    health_monitor.increment_request()
    check_database_health()
    check_telegram_api_health()
    return jsonify({"status": "awake", "timestamp": datetime.now().isoformat()})

@app.route('/webhook', methods=['POST'])
def webhook():
    health_monitor.increment_request()
    if bot_instance and bot_instance.application:
        try:
            update = Update.de_json(request.get_json(), bot_instance.application.bot)
            bot_instance.application.update_queue.put(update)
            return 'OK'
        except Exception as e:
            health_monitor.increment_error()
            return 'ERROR', 500
    return 'BOT_NOT_READY', 503

# ==================== ОСНОВНОЙ КЛАСС БОТА ====================

class NutritionBot:
    def __init__(self):
        self.bot_token = BOT_TOKEN
        if not self.bot_token:
            logger.error("❌ BOT_TOKEN not found")
            health_monitor.update_bot_status("error")
            raise ValueError("BOT_TOKEN is required")
            
        init_database()
        
        try:
            self.application = Application.builder().token(self.bot_token).build()
            self.menu = InteractiveMenu()
            self.yandex_gpt = YandexGPT()
            self._setup_handlers()
            
            health_monitor.update_bot_status("healthy")
            logger.info("✅ Bot initialized successfully")
            
        except Exception as e:
            health_monitor.update_bot_status("error")
            logger.error(f"❌ Failed to initialize bot: {e}")
            raise
    
    def _setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("menu", self.menu_command))
        self.application.add_handler(CommandHandler("dbstats", self.dbstats_command))
        self.application.add_handler(CommandHandler("export_plan", self.export_plan_command))
        self.application.add_handler(CommandHandler("wake", self.wake_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CallbackQueryHandler(self.handle_callback, pattern="^.*$"))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.application.add_error_handler(self.error_handler)
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        health_monitor.increment_request()
        stats = health_monitor.get_stats()
        status_text = f"""
🤖 **СТАТУС БОТА**

✅ **Бот:** {health_monitor.bot_status.upper()}
🗄️ **База данных:** {health_monitor.db_status.upper()}
📱 **Telegram API:** {health_monitor.telegram_api_status.upper()}
🤖 **Yandex GPT:** {health_monitor.yandex_gpt_status.upper()}

📊 **Статистика:**
• Время работы: {stats['uptime_seconds']} сек
• Запросов: {stats['request_count']}
• Ошибок: {stats['error_count']}
• Успешность: {stats['success_rate']:.1f}%
"""
        await update.message.reply_text(status_text)
    
    async def wake_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        health_monitor.increment_request()
        check_database_health()
        check_telegram_api_health()
        await update.message.reply_text("🤖 Бот активен и работает! ✅")
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        health_monitor.increment_request()
        try:
            user = update.effective_user
            user_data = {
                'user_id': user.id,
                'username': user.username,
                'first_name': user.first_name,
                'last_name': user.last_name
            }
            save_user(user_data)
            
            welcome_text = "🎯 Добро пожаловать в бот персонализированного питания с AI!"
            if is_admin(user.id):
                welcome_text += "\n👑 ВЫ АДМИНИСТРАТОР: безлимитный доступ к планам!"
            
            await update.message.reply_text(welcome_text, reply_markup=self.menu.get_main_menu())
            
        except Exception as e:
            health_monitor.increment_error()
            await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
    
    async def menu_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        health_monitor.increment_request()
        await update.message.reply_text("🤖 ГЛАВНОЕ МЕНЮ", reply_markup=self.menu.get_main_menu())
    
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data
        logger.info(f"Callback: {data}")
        
        try:
            if data == "create_plan":
                await self._handle_create_plan(query, context)
            elif data == "checkin":
                await self._handle_checkin_menu(query, context)
            elif data == "stats":
                await self._handle_stats(query, context)
            elif data == "my_plan":
                await self._handle_my_plan_menu(query, context)
            elif data == "shopping_cart":
                await self._handle_shopping_cart(query, context)
            elif data == "help":
                await self._handle_help(query, context)
            elif data == "back_main":
                await self._show_main_menu(query)
            else:
                await query.edit_message_text("❌ Неизвестная команда", reply_markup=self.menu.get_main_menu())
                
        except Exception as e:
            health_monitor.increment_error()
            await query.edit_message_text("❌ Ошибка", reply_markup=self.menu.get_main_menu())
    
    async def _handle_create_plan(self, query, context):
        user_id = query.from_user.id
        if not is_admin(user_id) and not can_make_request(user_id):
            days_remaining = get_days_until_next_plan(user_id)
            await query.edit_message_text(f"⏳ Следующий план через {days_remaining} дней", reply_markup=self.menu.get_main_menu())
            return
        
        context.user_data['plan_data'] = {}
        context.user_data['plan_step'] = 1
        await query.edit_message_text("📊 СОЗДАНИЕ ПЛАНА\n\n1️⃣ Выберите пол:", reply_markup=self.menu.get_plan_data_input(step=1))
    
    async def _handle_my_plan_menu(self, query, context):
        user_id = query.from_user.id
        plan = get_latest_plan(user_id)
        
        if not plan:
            await query.edit_message_text("📋 У вас пока нет планов", reply_markup=self.menu.get_main_menu())
            return
        
        user_data = plan.get('user_data', {})
        menu_text = f"📋 УПРАВЛЕНИЕ ПЛАНОМ\n\n👤 {user_data.get('gender', '')}, {user_data.get('age', '')} лет"
        await query.edit_message_text(menu_text, reply_markup=self.menu.get_plan_management_menu())
    
    async def _handle_shopping_cart(self, query, context, page=0):
        user_id = query.from_user.id
        items = get_shopping_cart(user_id)
        
        if not items:
            plan = get_latest_plan(user_id)
            if plan:
                self._generate_and_save_shopping_cart(user_id, plan)
                items = get_shopping_cart(user_id)
        
        if not items:
            await query.edit_message_text("🛒 Корзина пуста", reply_markup=self.menu.get_main_menu())
            return
        
        cart_text = "🛒 КОРЗИНА ПОКУПОК\n\nНажмите на продукт для отметки:\n\n"
        for i, item in enumerate(items, 1):
            item_id, ingredient, checked = item
            status = "✅" if checked else "⬜"
            cart_text += f"{i}. {status} {ingredient}\n"
        
        await query.edit_message_text(cart_text, reply_markup=self.menu.get_shopping_cart_menu(items, page))
    
    async def _handle_help(self, query, context):
        help_text = """
🤖 СПРАВКА ПО БОТУ

📊 СОЗДАТЬ ПЛАН - персонализированный план питания
📈 ЧЕК-ИН - отслеживание прогресса  
📋 МОЙ ПЛАН - управление планом питания
🛒 КОРЗИНА - список покупок с отметками
"""
        await query.edit_message_text(help_text, reply_markup=self.menu.get_main_menu())
    
    async def _show_main_menu(self, query):
        await query.edit_message_text("🤖 ГЛАВНОЕ МЕНЮ", reply_markup=self.menu.get_main_menu())
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        health_monitor.increment_request()
        try:
            user_id = update.effective_user.id
            text = update.message.text
            
            if 'awaiting_input' in context.user_data:
                input_type = context.user_data['awaiting_input']
                
                if input_type == 'plan_details':
                    await self._process_plan_details(update, context, text)
                elif input_type == 'checkin_data':
                    await self._process_checkin_data(update, context, text)
                else:
                    await update.message.reply_text("❌ Неизвестный тип ввода", reply_markup=self.menu.get_main_menu())
                
                context.user_data.pop('awaiting_input', None)
            else:
                await update.message.reply_text("🤖 Используйте меню", reply_markup=self.menu.get_main_menu())
                
        except Exception as e:
            health_monitor.increment_error()
            await update.message.reply_text("❌ Ошибка", reply_markup=self.menu.get_main_menu())
    
    async def _process_plan_details(self, update, context, text):
        try:
            parts = [part.strip() for part in text.split(',')]
            if len(parts) != 3:
                await update.message.reply_text("❌ Неверный формат. Пример: 30, 180, 75", reply_markup=self.menu.get_back_menu())
                return
            
            age = int(parts[0])
            height = int(parts[1])
            weight = float(parts[2])
            
            context.user_data['plan_data'].update({
                'age': age,
                'height': height,
                'weight': weight
            })
            
            await update.message.reply_text("🔄 Создаем ваш план...")
            
            plan = self.yandex_gpt.generate_nutrition_plan(context.user_data['plan_data'])
            
            if plan:
                plan_id = save_plan(update.effective_user.id, plan)
                update_user_limit(update.effective_user.id)
                self._generate_and_save_shopping_cart(update.effective_user.id, plan)
                
                if plan_id:
                    await update.message.reply_text("✅ План готов!", reply_markup=self.menu.get_main_menu())
                else:
                    await update.message.reply_text("❌ Ошибка сохранения", reply_markup=self.menu.get_main_menu())
            else:
                await update.message.reply_text("❌ Ошибка создания", reply_markup=self.menu.get_main_menu())
            
        except ValueError:
            await update.message.reply_text("❌ Неверный формат чисел", reply_markup=self.menu.get_back_menu())
        except Exception as e:
            health_monitor.increment_error()
            await update.message.reply_text("❌ Ошибка", reply_markup=self.menu.get_main_menu())
    
    def _generate_and_save_shopping_cart(self, user_id, plan):
        try:
            shopping_list = self._generate_shopping_list(plan)
            save_shopping_cart(user_id, shopping_list)
        except Exception as e:
            logger.error(f"Error generating shopping cart: {e}")
    
    def _generate_shopping_list(self, plan):
        try:
            all_ingredients = []
            for day in plan.get('days', []):
                for meal in day.get('meals', []):
                    ingredients = meal.get('ingredients', '')
                    lines = ingredients.split('\n')
                    for line in lines:
                        line = line.strip()
                        if line and (line.startswith('•') or line.startswith('-')):
                            clean_line = re.sub(r'^[•\-\s]+', '', line).strip()
                            if clean_line:
                                all_ingredients.append(clean_line)
            
            unique_ingredients = sorted(list(set(all_ingredients)))
            return unique_ingredients[:20] if unique_ingredients else ["Куриная грудка - 500г", "Овощи - 1кг", "Фрукты - 500г"]
            
        except Exception as e:
            return ["Куриная грудка - 500г", "Овощи - 1кг", "Фрукты - 500г"]
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        health_monitor.increment_error()
        logger.error(f"Error: {context.error}")

# ==================== ЗАПУСК ====================

def run_health_checks():
    logger.info("🔍 Running health checks...")
    check_database_health()
    check_telegram_api_health()
    check_yandex_gpt_health()

def run_webhook():
    try:
        global bot_instance
        run_health_checks()
        bot_instance = NutritionBot()
        
        webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"
        bot_instance.application.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get('PORT', 5000)),
            url_path=BOT_TOKEN,
            webhook_url=webhook_url
        )
        
        logger.info(f"✅ Webhook bot started on {webhook_url}")
        health_monitor.update_bot_status("running")
        
    except Exception as e:
        logger.error(f"❌ Failed to start bot: {e}")

if __name__ == '__main__':
    if RENDER_EXTERNAL_URL:
        logger.info("🚀 Starting in WEBHOOK mode")
        run_webhook()
    else:
        logger.info("🔄 Starting in POLLING mode")
        # Для простоты в этом примере только webhook
        print("Для локальной разработки настройте RENDER_EXTERNAL_URL")
