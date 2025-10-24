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
from telegram.error import TelegramError, Conflict

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
        
        # Проверяем существование таблиц
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        
        required_tables = ['users', 'nutrition_plans', 'daily_checkins', 'user_limits', 'shopping_cart']
        missing_tables = [table for table in required_tables if table not in tables]
        
        if missing_tables:
            logger.warning(f"Missing tables: {missing_tables}")
            # Автоматически создаем недостающие таблицы
            init_database()
        
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
                        "text": """Ты - профессиональный диетолог. Создай персонализированный план питания на 7 дней. 
Включи рекомендации по потреблению воды. Формат строго JSON."""
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
                    
                    # Добавляем рекомендации по воде если их нет
                    if 'water_recommendation' not in plan_json:
                        plan_json['water_recommendation'] = self._get_water_recommendation(user_data)
                    
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
- Рекомендации по потреблению воды

Верни ответ ТОЛЬКО в формате JSON без дополнительного текста.
"""
        return prompt
    
    def _get_water_recommendation(self, user_data):
        """Генерирует рекомендации по воде на основе данных пользователя"""
        weight = user_data.get('weight', 70)
        activity = user_data.get('activity', '')
        
        # Базовая формула: 30-40 мл на кг веса
        base_water = weight * 35
        
        # Корректировка по активности
        activity_multiplier = {
            'НИЗКАЯ': 1.0,
            'СРЕДНЯЯ': 1.2,
            'ВЫСОКАЯ': 1.4
        }.get(activity, 1.2)
        
        recommended_water = int(base_water * activity_multiplier)
        
        return {
            "daily_recommendation": f"{recommended_water} мл",
            "description": f"Рекомендуется выпивать {recommended_water} мл воды в день. Распределите равномерно в течение дня.",
            "tips": [
                "1-2 стакана утром натощак",
                "По 1 стакану перед каждым приемом пищи", 
                "Во время тренировок - дополнительно 500-1000 мл",
                "Ограничьте потребление за 2 часа до сна"
            ]
        }
    
    def _generate_demo_plan(self, user_data):
        """Резервный демо-план"""
        days = ['ПОНЕДЕЛЬНИК', 'ВТОРНИК', 'СРЕДА', 'ЧЕТВЕРГ', 'ПЯТНИЦА', 'СУББОТА', 'ВОСКРЕСЕНЬЕ']
        
        plan = {
            'user_data': user_data,
            'water_recommendation': self._get_water_recommendation(user_data),
            'days': []
        }
        
        demo_meals = [
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
        
        for day_name in days:
            day_plan = {
                'name': day_name,
                'total_calories': '1800-2000 ккал',
                'meals': demo_meals.copy()
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
            [InlineKeyboardButton("💧 ВОДНЫЙ РЕЖИМ", callback_data="water_mode")],
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
    
    def get_water_menu(self):
        """Меню водного режима"""
        keyboard = [
            [InlineKeyboardButton("💧 РЕКОМЕНДАЦИИ ПО ВОДЕ", callback_data="water_recommendations")],
            [InlineKeyboardButton("⏱ НАПОМИНАНИЯ О ВОДЕ", callback_data="water_reminders")],
            [InlineKeyboardButton("📊 МОЯ СТАТИСТИКА ВОДЫ", callback_data="water_stats")],
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
            [InlineKeyboardButton("↩️ НАЗАД В МЕНУ", callback_data="back_main")]
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
    """Обработчик webhook от Telegram"""
    health_monitor.increment_request()
    if bot_instance and bot_instance.application:
        try:
            # Парсим обновление от Telegram
            update_data = request.get_json()
            if not update_data:
                return 'EMPTY_UPDATE', 400
                
            update = Update.de_json(update_data, bot_instance.application.bot)
            
            # Обрабатываем обновление через Application
            bot_instance.application.process_update(update)
            return 'OK'
            
        except Exception as e:
            health_monitor.increment_error()
            logger.error(f"Webhook processing error: {e}")
            return 'ERROR', 500
    else:
        logger.error("Bot instance not ready")
        return 'BOT_NOT_READY', 503

@app.route('/set_webhook', methods=['GET'])
def set_webhook():
    """Ручная установка webhook (для отладки)"""
    if not bot_instance:
        return "Bot not initialized", 503
        
    webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"
    try:
        result = bot_instance.application.bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True
        )
        return f"Webhook set to: {webhook_url}, Result: {result}"
    except Exception as e:
        return f"Error setting webhook: {e}", 500

@app.route('/delete_webhook', methods=['GET'])
def delete_webhook():
    """Удаление webhook (для отладки или локальной разработки)"""
    if not bot_instance:
        return "Bot not initialized", 503
        
    try:
        result = bot_instance.application.bot.delete_webhook()
        return f"Webhook deleted, Result: {result}"
    except Exception as e:
        return f"Error deleting webhook: {e}", 500

# ==================== ОСНОВНОЙ КЛАСС БОТА ====================

class NutritionBot:
    def __init__(self):
        self.bot_token = BOT_TOKEN
        if not self.bot_token:
            logger.error("❌ BOT_TOKEN not found")
            health_monitor.update_bot_status("error")
            raise ValueError("BOT_TOKEN is required")
            
        # Сначала инициализируем базу данных
        init_database()
        
        try:
            self.application = Application.builder().token(self.bot_token).build()
            self.menu = InteractiveMenu()
            self.yandex_gpt = YandexGPT()
            self._setup_handlers()
            
            # РЕГИСТРИРУЕМ ОБРАБОТЧИКИ ЗАВЕРШЕНИЯ
            self._register_shutdown_handlers()
            
            health_monitor.update_bot_status("healthy")
            logger.info("✅ Bot initialized successfully")
            
        except Exception as e:
            health_monitor.update_bot_status("error")
            logger.error(f"❌ Failed to initialize bot: {e}")
            raise
    
    def _register_shutdown_handlers(self):
        """Регистрирует обработчики graceful shutdown"""
        def shutdown_handler(signum, frame):
            logger.info("🛑 Received shutdown signal")
            health_monitor.update_bot_status("shutting_down")
            if hasattr(self, 'application'):
                self.application.stop()
            sys.exit(0)
        
        signal.signal(signal.SIGINT, shutdown_handler)
        signal.signal(signal.SIGTERM, shutdown_handler)
        
        # Регистрируем shutdown при выходе
        atexit.register(self._shutdown)
    
    def _shutdown(self):
        """Корректное завершение работы"""
        logger.info("🔚 Shutting down bot application")
        health_monitor.update_bot_status("stopped")
    
    def _setup_handlers(self):
        """Настройка обработчиков"""
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("menu", self.menu_command))
        self.application.add_handler(CommandHandler("dbstats", self.dbstats_command))
        self.application.add_handler(CommandHandler("export_plan", self.export_plan_command))
        self.application.add_handler(CommandHandler("wake", self.wake_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CallbackQueryHandler(self.handle_callback, pattern="^.*$"))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.application.add_error_handler(self.error_handler)
    
    async def dbstats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда для просмотра статистики БД"""
        health_monitor.increment_request()
        try:
            user_id = update.effective_user.id
            if not is_admin(user_id):
                await update.message.reply_text("❌ Эта команда только для администратора")
                return
            
            conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
            cursor = conn.cursor()
            
            cursor.execute("SELECT COUNT(*) FROM users")
            users_count = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM nutrition_plans")
            plans_count = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM daily_checkins")
            checkins_count = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM shopping_cart")
            cart_count = cursor.fetchone()[0]
            
            conn.close()
            
            stats_text = f"""
📊 СТАТИСТИКА БАЗЫ ДАННЫХ:

👥 Пользователей: {users_count}
📋 Планов питания: {plans_count}
📈 Чек-инов: {checkins_count}
🛒 Записей в корзинах: {cart_count}
"""
            await update.message.reply_text(stats_text)
            
        except Exception as e:
            health_monitor.increment_error()
            await update.message.reply_text("❌ Ошибка при получении статистики БД")
    
    async def export_plan_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда для экспорта плана в TXT"""
        health_monitor.increment_request()
        try:
            user_id = update.effective_user.id
            await update.message.reply_text("📄 Подготавливаем ваш план для скачивания...")
            await self.send_plan_as_file(update, context, user_id)
            
        except Exception as e:
            health_monitor.increment_error()
            await update.message.reply_text("❌ Ошибка при подготовке плана для скачивания")
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда для проверки статуса бота"""
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

🕒 Последняя проверка: {stats['last_health_check']}
"""
        await update.message.reply_text(status_text)
    
    async def wake_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда для пробуждения бота"""
        health_monitor.increment_request()
        
        check_database_health()
        check_telegram_api_health()
        
        await update.message.reply_text("🤖 Бот активен и работает! ✅")
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
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
            
            welcome_text = """
🎯 Добро пожаловать в бот персонализированного питания с AI!

Выберите действие из меню ниже:
"""
            if is_admin(user.id):
                welcome_text += "\n👑 ВЫ АДМИНИСТРАТОР: безлимитный доступ к планам!"
            
            await update.message.reply_text(
                welcome_text,
                reply_markup=self.menu.get_main_menu()
            )
            
        except Exception as e:
            health_monitor.increment_error()
            logger.error(f"Error in start_command: {e}")
            await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
    
    async def menu_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает главное меню"""
        health_monitor.increment_request()
        await update.message.reply_text(
            "🤖 ГЛАВНОЕ МЕНЮ\n\nВыберите действие:",
            reply_markup=self.menu.get_main_menu()
        )
    
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик callback'ов"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        logger.info(f"Callback received: {data}")
        
        try:
            # Основные команды меню
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
            elif data == "water_mode":
                await self._handle_water_mode(query, context)
            elif data == "help":
                await self._handle_help(query, context)
            elif data == "plan_info":
                await self._handle_plan_info(query, context)
            elif data == "download_plan":
                await self._handle_download_plan(query, context)
            elif data == "view_week":
                await self._handle_view_week(query, context)
            elif data == "download_shopping_list":
                await self._handle_download_shopping_list(query, context)
            elif data == "back_to_plan_menu":
                await self._handle_my_plan_menu(query, context)
            
            # Навигация назад
            elif data == "back_main":
                await self._show_main_menu(query)
            elif data == "back_gender":
                await self._handle_gender_back(query, context)
            elif data == "back_goal":
                await self._handle_goal_back(query, context)
            
            # Ввод данных плана
            elif data.startswith("gender_"):
                await self._handle_gender(query, context, data)
            elif data.startswith("goal_"):
                await self._handle_goal(query, context, data)
            elif data.startswith("activity_"):
                await self._handle_activity(query, context, data)
            
            # Чек-ин
            elif data == "checkin_data":
                await self._handle_checkin_data(query, context)
            elif data == "checkin_history":
                await self._handle_checkin_history(query, context)
            
            # Водный режим
            elif data == "water_recommendations":
                await self._handle_water_recommendations(query, context)
            elif data == "water_reminders":
                await self._handle_water_reminders(query, context)
            elif data == "water_stats":
                await self._handle_water_stats(query, context)
            
            # Просмотр недели и приемов пищи
            elif data.startswith("day_"):
                await self._handle_day_selection(query, context, data)
            elif data.startswith("meal_"):
                await self._handle_meal_selection(query, context, data)
            elif data.startswith("next_meal_"):
                await self._handle_next_meal(query, context, data)
            
            # Корзина покупок
            elif data.startswith("toggle_"):
                await self._handle_toggle_cart_item(query, context, data)
            elif data.startswith("cart_page_"):
                await self._handle_cart_page(query, context, data)
            elif data == "refresh_cart":
                await self._handle_refresh_cart(query, context)
            elif data == "clear_cart":
                await self._handle_clear_cart(query, context)
            
            else:
                logger.warning(f"Unknown callback data: {data}")
                await query.edit_message_text(
                    "❌ Неизвестная команда",
                    reply_markup=self.menu.get_main_menu()
                )
                
        except Exception as e:
            health_monitor.increment_error()
            logger.error(f"Error in callback handler: {e}")
            await query.edit_message_text(
                "❌ Произошла ошибка. Попробуйте снова.",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _handle_create_plan(self, query, context):
        """Обработчик создания плана"""
        try:
            user_id = query.from_user.id
            
            if not is_admin(user_id) and not can_make_request(user_id):
                days_remaining = get_days_until_next_plan(user_id)
                await query.edit_message_text(
                    f"⏳ Вы уже запрашивали план питания\nСледующий доступен через {days_remaining} дней",
                    reply_markup=self.menu.get_main_menu()
                )
                return
            
            context.user_data['plan_data'] = {}
            context.user_data['plan_step'] = 1
            
            await query.edit_message_text(
                "📊 СОЗДАНИЕ ПЛАНА ПИТАНИЯ\n\n1️⃣ Выберите ваш пол:",
                reply_markup=self.menu.get_plan_data_input(step=1)
            )
            
        except Exception as e:
            health_monitor.increment_error()
            await query.edit_message_text(
                "❌ Ошибка при создании плана",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _handle_gender_back(self, query, context):
        """Назад к выбору пола"""
        context.user_data['plan_step'] = 1
        await query.edit_message_text(
            "📊 СОЗДАНИЕ ПЛАНА ПИТАНИЯ\n\n1️⃣ Выберите ваш пол:",
            reply_markup=self.menu.get_plan_data_input(step=1)
        )
    
    async def _handle_goal_back(self, query, context):
        """Назад к выбору цели"""
        context.user_data['plan_step'] = 2
        await query.edit_message_text(
            "📊 СОЗДАНИЕ ПЛАНА ПИТАНИЯ\n\n2️⃣ Выберите вашу цель:",
            reply_markup=self.menu.get_plan_data_input(step=2)
        )
    
    async def _handle_gender(self, query, context, data):
        """Обработчик выбора пола"""
        gender_map = {
            "gender_male": "МУЖЧИНА",
            "gender_female": "ЖЕНЩИНА"
        }
        
        context.user_data['plan_data']['gender'] = gender_map[data]
        context.user_data['plan_step'] = 2
        
        await query.edit_message_text(
            "📊 СОЗДАНИЕ ПЛАНА ПИТАНИЯ\n\n2️⃣ Выберите вашу цель:",
            reply_markup=self.menu.get_plan_data_input(step=2)
        )
    
    async def _handle_goal(self, query, context, data):
        """Обработчик выбора цели"""
        goal_map = {
            "goal_weight_loss": "ПОХУДЕНИЕ",
            "goal_mass": "НАБОР МАССЫ", 
            "goal_maintain": "ПОДДЕРЖАНИЕ"
        }
        
        context.user_data['plan_data']['goal'] = goal_map[data]
        context.user_data['plan_step'] = 3
        
        await query.edit_message_text(
            "📊 СОЗДАНИЕ ПЛАНА ПИТАНИЯ\n\n3️⃣ Выберите уровень активности:",
            reply_markup=self.menu.get_plan_data_input(step=3)
        )
    
    async def _handle_activity(self, query, context, data):
        """Обработчик выбора активности"""
        activity_map = {
            "activity_high": "ВЫСОКАЯ",
            "activity_medium": "СРЕДНЯЯ",
            "activity_low": "НИЗКАЯ"
        }
        
        context.user_data['plan_data']['activity'] = activity_map[data]
        context.user_data['awaiting_input'] = 'plan_details'
        
        await query.edit_message_text(
            "📊 СОЗДАНИЕ ПЛАНА ПИТАНИЯ\n\n4️⃣ Введите ваши данные в формате:\n"
            "Возраст, Рост (см), Вес (кг)\n\n"
            "Пример: 30, 180, 75\n\n"
            "Для отмены нажмите /menu",
            reply_markup=self.menu.get_back_menu()
        )
    
    async def _handle_checkin_menu(self, query, context):
        """Обработчик меню чек-ина"""
        await query.edit_message_text(
            "📈 ЕЖЕДНЕВНЫЙ ЧЕК-ИН\n\n"
            "Отслеживайте ваш прогресс:\n"
            "• Вес\n• Обхват талии\n• Самочувствие\n• Качество сна\n\n"
            "Выберите действие:",
            reply_markup=self.menu.get_checkin_menu()
        )
    
    async def _handle_checkin_data(self, query, context):
        """Обработчик ввода данных чек-ина"""
        context.user_data['awaiting_input'] = 'checkin_data'
        await query.edit_message_text(
            "📝 ВВЕДИТЕ ДАННЫЕ ЧЕК-ИНА\n\n"
            "Введите данные в формате:\n"
            "Вес (кг), Обхват талии (см), Самочувствие (1-5), Сон (1-5)\n\n"
            "Пример: 75.5, 85, 4, 3\n\n"
            "Для отмены нажмите /menu"
        )
    
    async def _handle_checkin_history(self, query, context):
        """Обработчик истории чек-инов"""
        user_id = query.from_user.id
        stats = get_user_stats(user_id)
        
        if not stats:
            await query.edit_message_text(
                "📊 У вас пока нет данных чек-инов",
                reply_markup=self.menu.get_checkin_menu()
            )
            return
        
        stats_text = "📊 ИСТОРИЯ ВАШИХ ЧЕК-ИНОВ:\n\n"
        for stat in stats:
            date, weight, waist, wellbeing, sleep = stat
            stats_text += f"📅 {date[:10]}: {weight} кг, талия {waist} см\n"
        
        await query.edit_message_text(stats_text, reply_markup=self.menu.get_checkin_menu())
    
    async def _handle_stats(self, query, context):
        """Обработчик статистики"""
        user_id = query.from_user.id
        stats = get_user_stats(user_id)
        
        if not stats:
            await query.edit_message_text(
                "📊 У вас пока нет данных для статистики",
                reply_markup=self.menu.get_main_menu()
            )
            return
        
        stats_text = "📊 ВАША СТАТИСТИКА\n\nПоследние записи:\n"
        for i, stat in enumerate(stats[:5]):
            date, weight, waist, wellbeing, sleep = stat
            stats_text += f"📅 {date[:10]}: {weight} кг, талия {waist} см\n"
        
        await query.edit_message_text(stats_text, reply_markup=self.menu.get_main_menu())
    
    async def _handle_my_plan_menu(self, query, context):
        """Обработчик меню моего плана"""
        user_id = query.from_user.id
        plan = get_latest_plan(user_id)
        
        if not plan:
            await query.edit_message_text(
                "📋 У вас пока нет созданных планов питания",
                reply_markup=self.menu.get_main_menu()
            )
            return
        
        user_data = plan.get('user_data', {})
        menu_text = f"📋 УПРАВЛЕНИЕ ПЛАНОМ ПИТАНИЯ\n\n"
        menu_text += f"👤 {user_data.get('gender', '')}, {user_data.get('age', '')} лет\n"
        menu_text += f"📏 {user_data.get('height', '')} см, {user_data.get('weight', '')} кг\n"
        menu_text += "Выберите действие:"
        
        await query.edit_message_text(
            menu_text,
            reply_markup=self.menu.get_plan_management_menu()
        )
    
    async def _handle_plan_info(self, query, context):
        """Обработчик информации о планах"""
        user_id = query.from_user.id
        plans_count = get_user_plans_count(user_id)
        days_remaining = get_days_until_next_plan(user_id)
        
        info_text = f"📊 ИНФОРМАЦИЯ О ВАШИХ ПЛАНАХ\n\n"
        info_text += f"📋 Создано планов: {plans_count}\n"
        
        if is_admin(user_id):
            info_text += "👑 Статус: АДМИНИСТРАТОР\n"
        else:
            if days_remaining > 0:
                info_text += f"⏳ Следующий план через: {days_remaining} дней\n"
            else:
                info_text += "✅ Можете создать новый план!\n"
        
        await query.edit_message_text(
            info_text,
            reply_markup=self.menu.get_plan_management_menu()
        )
    
    async def _handle_download_plan(self, query, context):
        """Обработчик скачивания плана"""
        user_id = query.from_user.id
        await self.send_plan_as_file(query, context, user_id)
    
    async def _handle_view_week(self, query, context):
        """Обработчик просмотра недели"""
        user_id = query.from_user.id
        plan = get_latest_plan(user_id)
        
        if not plan:
            await query.edit_message_text(
                "❌ У вас нет активного плана питания",
                reply_markup=self.menu.get_main_menu()
            )
            return
        
        week_text = "📅 ВАШ ПЛАН ПИТАНИЯ НА НЕДЕЛЮ\n\n"
        week_text += "Выберите день для просмотра деталей:\n\n"
        
        for i, day in enumerate(plan.get('days', [])):
            week_text += f"📅 {day['name']}\n"
            week_text += f"🔥 {day.get('total_calories', '~1800 ккал')}\n\n"
        
        await query.edit_message_text(
            week_text,
            reply_markup=self.menu.get_week_days_menu()
        )
    
    async def _handle_day_selection(self, query, context, data):
        """Обработчик выбора дня"""
        day_index = int(data.split('_')[1])
        user_id = query.from_user.id
        plan = get_latest_plan(user_id)
        
        if not plan or day_index >= len(plan.get('days', [])):
            await query.edit_message_text(
                "❌ Ошибка при загрузке дня",
                reply_markup=self.menu.get_week_days_menu()
            )
            return
        
        day = plan['days'][day_index]
        day_text = f"📅 {day['name']}\n\n"
        day_text += f"🔥 Общая калорийность: {day.get('total_calories', '~1800 ккал')}\n\n"
        day_text += "🍽 Приемы пищи:\n\n"
        
        for i, meal in enumerate(day.get('meals', [])):
            day_text += f"{meal['emoji']} {meal['type']} ({meal['time']})\n"
            day_text += f"   {meal['name']} - {meal['calories']}\n\n"
        
        day_text += "Выберите прием пищи для просмотра деталей:"
        
        await query.edit_message_text(
            day_text,
            reply_markup=self.menu.get_day_meals_menu(day_index)
        )
    
    async def _handle_meal_selection(self, query, context, data):
        """Обработчик выбора приема пищи"""
        parts = data.split('_')
        day_index = int(parts[1])
        meal_index = int(parts[2])
        
        user_id = query.from_user.id
        plan = get_latest_plan(user_id)
        
        if not plan or day_index >= len(plan.get('days', [])):
            await query.edit_message_text(
                "❌ Ошибка при загрузке приема пищи",
                reply_markup=self.menu.get_week_days_menu()
            )
            return
        
        day = plan['days'][day_index]
        if meal_index >= len(day.get('meals', [])):
            await query.edit_message_text(
                "❌ Ошибка при загрузке приема пищи",
                reply_markup=self.menu.get_day_meals_menu(day_index)
            )
            return
        
        meal = day['meals'][meal_index]
        meal_text = f"🍽 {meal['type']} - {day['name']}\n\n"
        meal_text += f"🕐 Время: {meal['time']}\n"
        meal_text += f"📝 Блюдо: {meal['name']}\n"
        meal_text += f"🔥 Калорийность: {meal['calories']}\n"
        meal_text += f"⏱ Время приготовления: {meal['cooking_time']}\n\n"
        
        meal_text += "📋 Ингредиенты:\n"
        meal_text += f"{meal['ingredients']}\n\n"
        
        meal_text += "👩‍🍳 Приготовление:\n"
        meal_text += f"{meal['instructions']}"
        
        await query.edit_message_text(
            meal_text,
            reply_markup=self.menu.get_meal_detail_menu(day_index, meal_index)
        )
    
    async def _handle_next_meal(self, query, context, data):
        """Обработчик перехода к следующему приему пищи"""
        parts = data.split('_')
        day_index = int(parts[2])
        meal_index = int(parts[3])
        
        user_id = query.from_user.id
        plan = get_latest_plan(user_id)
        
        if not plan:
            await query.edit_message_text(
                "❌ Ошибка при загрузке плана",
                reply_markup=self.menu.get_main_menu()
            )
            return
        
        next_meal_index = meal_index + 1
        next_day_index = day_index
        
        if next_meal_index >= len(plan['days'][day_index].get('meals', [])):
            next_meal_index = 0
            next_day_index += 1
        
        if next_day_index >= len(plan.get('days', [])):
            next_day_index = 0
        
        next_callback = f"meal_{next_day_index}_{next_meal_index}"
        await self._handle_meal_selection(query, context, next_callback)
    
    async def _handle_water_mode(self, query, context):
        """Обработчик меню водного режима"""
        await query.edit_message_text(
            "💧 ВОДНЫЙ РЕЖИМ\n\n"
            "Правильный питьевой режим - основа здоровья и эффективного похудения.\n\n"
            "Выберите действие:",
            reply_markup=self.menu.get_water_menu()
        )
    
    async def _handle_water_recommendations(self, query, context):
        """Обработчик рекомендаций по воде"""
        user_id = query.from_user.id
        plan = get_latest_plan(user_id)
        
        if plan and 'water_recommendation' in plan:
            water_info = plan['water_recommendation']
        else:
            # Генерируем рекомендации на основе средних параметров
            water_info = self.yandex_gpt._get_water_recommendation({'weight': 70, 'activity': 'СРЕДНЯЯ'})
        
        water_text = "💧 РЕКОМЕНДАЦИИ ПО ВОДНОМУ РЕЖИМУ\n\n"
        water_text += f"📊 Ежедневная норма: {water_info['daily_recommendation']}\n"
        water_text += f"📝 {water_info['description']}\n\n"
        
        water_text += "💡 Советы по потреблению воды:\n"
        for tip in water_info['tips']:
            water_text += f"{tip}\n"
        
        water_text += "\n🚰 Лучшее время для питья воды:\n"
        water_text += "• Утром натощак - 1-2 стакана\n• За 30 минут до еды\n• Через 1-2 часа после еды\n• Во время тренировок\n• При чувстве голода\n"
        
        await query.edit_message_text(
            water_text,
            reply_markup=self.menu.get_water_menu()
        )
    
    async def _handle_water_reminders(self, query, context):
        """Обработчик напоминаний о воде"""
        reminder_text = "⏱ НАСТРОЙКА НАПОМИНАНИЙ О ВОДЕ\n\n"
        reminder_text += "Для настройки напоминаний:\n\n"
        reminder_text += "1. Установите будильники на телефоне:\n"
        reminder_text += "   • 08:00 - 2 стакана\n"
        reminder_text += "   • 11:00 - 1 стакан\n"
        reminder_text += "   • 14:00 - 1 стакан\n"
        reminder_text += "   • 17:00 - 1 стакан\n"
        reminder_text += "   • 20:00 - 1 стакан\n\n"
        reminder_text += "2. Используйте приложения:\n"
        reminder_text += "   • Water Drink Reminder\n"
        reminder_text += "   • Hydro Coach\n"
        reminder_text += "   • Plant Nanny\n\n"
        reminder_text += "3. Держите воду всегда на виду\n"
        
        await query.edit_message_text(
            reminder_text,
            reply_markup=self.menu.get_water_menu()
        )
    
    async def _handle_water_stats(self, query, context):
        """Обработчик статистики воды"""
        stats_text = "📊 СТАТИСТИКА ПОТРЕБЛЕНИЯ ВОДЫ\n\n"
        stats_text += "💧 Польза достаточного потребления воды:\n"
        stats_text += "• Ускоряет метаболизм на 20-30%\n"
        stats_text += "• Снижает аппетит\n"
        stats_text += "• Улучшает состояние кожи\n"
        stats_text += "• Повышает энергию\n"
        stats_text += "• Улучшает работу мозга\n\n"
        stats_text += "📈 Ваши потенциальные результаты:\n"
        stats_text += "• +20% к скорости похудения\n"
        stats_text += "• -30% к усталости\n"
        stats_text += "• +15% к продуктивности\n"
        
        await query.edit_message_text(
            stats_text,
            reply_markup=self.menu.get_water_menu()
        )
    
    async def _handle_shopping_cart(self, query, context, page=0):
        """Обработчик корзины покупок"""
        user_id = query.from_user.id
        items = get_shopping_cart(user_id)
        
        if not items:
            plan = get_latest_plan(user_id)
            if plan:
                self._generate_and_save_shopping_cart(user_id, plan)
                items = get_shopping_cart(user_id)
        
        if not items:
            await query.edit_message_text(
                "🛒 Ваша корзина покупок пуста\n\n"
                "Создайте план питания, чтобы автоматически заполнить корзину",
                reply_markup=self.menu.get_main_menu()
            )
            return
        
        cart_text = "🛒 КОРЗИНА ПОКУПОК\n\n"
        cart_text += "✅ - куплено, ⬜ - нужно купить\n\n"
        cart_text += "Нажмите на продукт, чтобы отметить его:\n\n"
        
        items_per_page = 10
        start_idx = page * items_per_page
        end_idx = start_idx + items_per_page
        current_items = items[start_idx:end_idx]
        
        for i, item in enumerate(current_items, start=start_idx + 1):
            item_id, ingredient, checked = item
            status = "✅" if checked else "⬜"
            cart_text += f"{i}. {status} {ingredient}\n"
        
        total_items = len(items)
        checked_items = sum(1 for item in items if item[2])
        cart_text += f"\n📊 Прогресс: {checked_items}/{total_items} куплено"
        
        if page > 0 or (page + 1) * items_per_page < total_items:
            cart_text += f"\n📄 Страница {page + 1}"
        
        await query.edit_message_text(
            cart_text,
            reply_markup=self.menu.get_shopping_cart_menu(items, page)
        )
    
    async def _handle_toggle_cart_item(self, query, context, data):
        """Обработчик переключения статуса элемента корзины"""
        item_id = int(data.split('_')[1])
        user_id = query.from_user.id
        
        items = get_shopping_cart(user_id)
        current_item = next((item for item in items if item[0] == item_id), None)
        
        if current_item:
            new_checked = not current_item[2]
            update_shopping_item(item_id, new_checked)
            
            page = context.user_data.get('cart_page', 0)
            await self._handle_shopping_cart(query, context, page)
    
    async def _handle_cart_page(self, query, context, data):
        """Обработчик смены страницы корзины"""
        page = int(data.split('_')[2])
        context.user_data['cart_page'] = page
        await self._handle_shopping_cart(query, context, page)
    
    async def _handle_refresh_cart(self, query, context):
        """Обработчик обновления корзины из плана"""
        user_id = query.from_user.id
        plan = get_latest_plan(user_id)
        
        if not plan:
            await query.edit_message_text(
                "❌ У вас нет активного плана питания",
                reply_markup=self.menu.get_main_menu()
            )
            return
        
        self._generate_and_save_shopping_cart(user_id, plan)
        await query.edit_message_text(
            "✅ Корзина обновлена из текущего плана питания!",
            reply_markup=self.menu.get_main_menu()
        )
    
    async def _handle_clear_cart(self, query, context):
        """Обработчик очистки корзины"""
        user_id = query.from_user.id
        clear_shopping_cart(user_id)
        
        await query.edit_message_text(
            "✅ Корзина покупок очищена!",
            reply_markup=self.menu.get_main_menu()
        )
    
    async def _handle_download_shopping_list(self, query, context):
        """Обработчик скачивания списка покупок"""
        user_id = query.from_user.id
        items = get_shopping_cart(user_id)
        
        if not items:
            await query.edit_message_text(
                "❌ Корзина покупок пуста",
                reply_markup=self.menu.get_shopping_cart_menu([], 0)
            )
            return
        
        filename = f"shopping_list_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        
        with open(filename, 'w', encoding='utf-8') as f:
            f.write("🛒 СПИСОК ПОКУПОК НА НЕДЕЛЮ\n\n")
            f.write("📋 Продукты:\n\n")
            
            checked_count = 0
            for i, item in enumerate(items, 1):
                item_id, ingredient, checked = item
                status = "[✅]" if checked else "[ ]"
                f.write(f"{i}. {status} {ingredient}\n")
                if checked:
                    checked_count += 1
            
            f.write(f"\n📊 Прогресс: {checked_count}/{len(items)} куплено\n\n")
            f.write("💡 Советы:\n")
            f.write("• Покупайте свежие продукты\n• Проверяйте сроки годности\n")
        
        with open(filename, 'rb') as file:
            await context.bot.send_document(
                chat_id=user_id,
                document=file,
                filename=f"Список_покупок_{user_id}.txt",
                caption="📄 Ваш список покупок на неделю"
            )
        
        await query.edit_message_text(
            "✅ Список покупок отправлен в виде файла!",
            reply_markup=self.menu.get_shopping_cart_menu(items, 0)
        )
        
        import os
        os.remove(filename)
    
    async def _handle_help(self, query, context):
        """Обработчик помощи"""
        help_text = """
🤖 СПРАВКА ПО БОТУ ПИТАНИЯ

📊 СОЗДАТЬ ПЛАН:
• Персонализированный план питания на 7 дней
• Учет пола, цели, активности и параметров
• 1 план в 7 дней для обычных пользователей

📈 ЧЕК-ИН:
• Ежедневное отслеживание прогресса
• Вес, обхват талии, самочувствие, сон
• Просмотр истории и статистики

📋 МОЙ ПЛАН:
• Просмотр плана на неделю
• Детали по дням и приемам пищи
• Скачивание плана в текстовом файле

🛒 КОРЗИНА:
• Автоматический список покупок из плана
• Отметка купленных продуктов галочками
• Суммирование одинаковых продуктов
• Скачивание списка в файл

💧 ВОДНЫЙ РЕЖИМ:
• Персональные рекомендации по воде
• Советы по потреблению
• Напоминания и статистика

Для начала работы нажмите /start или выберите действие из меню.
"""
        await query.edit_message_text(
            help_text,
            reply_markup=self.menu.get_main_menu()
        )
    
    async def _show_main_menu(self, query):
        """Показывает главное меню"""
        await query.edit_message_text(
            "🤖 ГЛАВНОЕ МЕНЮ\n\nВыберите действие:",
            reply_markup=self.menu.get_main_menu()
        )
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик текстовых сообщений"""
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
                    await update.message.reply_text(
                        "❌ Неизвестный тип ввода. Используйте /menu",
                        reply_markup=self.menu.get_main_menu()
                    )
                
                context.user_data.pop('awaiting_input', None)
            else:
                await update.message.reply_text(
                    "🤖 Используйте меню для навигации или /start для начала",
                    reply_markup=self.menu.get_main_menu()
                )
                
        except Exception as e:
            health_monitor.increment_error()
            logger.error(f"Error in message handler: {e}")
            await update.message.reply_text(
                "❌ Произошла ошибка. Попробуйте снова.",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _process_plan_details(self, update, context, text):
        """Обрабатывает ввод деталей плана с Yandex GPT"""
        try:
            parts = [part.strip() for part in text.split(',')]
            if len(parts) != 3:
                await update.message.reply_text(
                    "❌ Неверный формат. Введите: Возраст, Рост (см), Вес (кг)\nПример: 30, 180, 75",
                    reply_markup=self.menu.get_back_menu()
                )
                return
            
            age = int(parts[0])
            height = int(parts[1])
            weight = float(parts[2])
            
            context.user_data['plan_data'].update({
                'age': age,
                'height': height,
                'weight': weight
            })
            
            await update.message.reply_text("🔄 Создаем ваш персональный план питания с помощью AI...")
            
            plan = self.yandex_gpt.generate_nutrition_plan(context.user_data['plan_data'])
            
            if plan:
                plan_id = save_plan(update.effective_user.id, plan)
                update_user_limit(update.effective_user.id)
                
                self._generate_and_save_shopping_cart(update.effective_user.id, plan)
                
                if plan_id:
                    await update.message.reply_text(
                        "✅ Ваш персональный план питания готов!\n\n"
                        "🛒 Корзина покупок автоматически заполнена\n"
                        "💧 Добавлены рекомендации по водному режиму\n"
                        "🤖 План создан с помощью Yandex GPT AI\n\n"
                        "Используйте меню для просмотра деталей.",
                        reply_markup=self.menu.get_main_menu()
                    )
                else:
                    await update.message.reply_text(
                        "❌ Ошибка при сохранении плана",
                        reply_markup=self.menu.get_main_menu()
                    )
            else:
                await update.message.reply_text(
                    "❌ Не удалось создать план. Попробуйте позже.",
                    reply_markup=self.menu.get_main_menu()
                )
            
        except ValueError:
            await update.message.reply_text(
                "❌ Неверный формат чисел. Убедитесь, что вводите числа правильно.\nПример: 30, 180, 75",
                reply_markup=self.menu.get_back_menu()
            )
        except Exception as e:
            health_monitor.increment_error()
            logger.error(f"Error processing plan details: {e}")
            await update.message.reply_text(
                "❌ Ошибка при обработке данных. Попробуйте снова.",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _process_checkin_data(self, update, context, text):
        """Обрабатывает ввод данных чек-ина"""
        try:
            parts = [part.strip() for part in text.split(',')]
            if len(parts) != 4:
                await update.message.reply_text(
                    "❌ Неверный формат. Введите: Вес, Талия, Самочувствие, Сон\nПример: 75.5, 85, 4, 3"
                )
                return
            
            weight = float(parts[0])
            waist = int(parts[1])
            wellbeing = int(parts[2])
            sleep = int(parts[3])
            
            if not (1 <= wellbeing <= 5) or not (1 <= sleep <= 5):
                await update.message.reply_text(
                    "❌ Оценки должны быть от 1 до 5\nПример: 75.5, 85, 4, 3"
                )
                return
            
            save_checkin(update.effective_user.id, weight, waist, wellbeing, sleep)
            
            await update.message.reply_text(
                "✅ Данные чек-ина сохранены!\n\n"
                "Продолжайте отслеживать свой прогресс 💪",
                reply_markup=self.menu.get_checkin_menu()
            )
            
        except ValueError:
            await update.message.reply_text(
                "❌ Неверный формат чисел. Убедитесь, что вводите числа правильно.\nПример: 75.5, 85, 4, 3"
            )
        except Exception as e:
            health_monitor.increment_error()
            await update.message.reply_text(
                "❌ Ошибка при сохранении данных. Попробуйте снова.",
                reply_markup=self.menu.get_main_menu()
            )
    
    def _generate_and_save_shopping_cart(self, user_id, plan):
        """Генерирует и сохраняет корзину покупок из плана с СУММИРОВАНИЕМ продуктов"""
        try:
            shopping_list = self._generate_shopping_list(plan)
            save_shopping_cart(user_id, shopping_list)
        except Exception as e:
            logger.error(f"Error generating shopping cart: {e}")
    
    def _generate_shopping_list(self, plan):
        """Генерирует список покупок на основе плана с СУММИРОВАНИЕМ одинаковых продуктов"""
        try:
            # Собираем все ингредиенты из всех приемов пищи за неделю
            all_ingredients = []
            
            for day in plan.get('days', []):
                for meal in day.get('meals', []):
                    ingredients = meal.get('ingredients', '')
                    lines = ingredients.split('\n')
                    for line in lines:
                        line = line.strip()
                        if line and (line.startswith('•') or line.startswith('-') or line[0].isdigit()):
                            clean_line = re.sub(r'^[•\-\d\.\s]+', '', line).strip()
                            if clean_line:
                                all_ingredients.append(clean_line)
            
            # Суммируем одинаковые продукты
            ingredient_totals = {}
            for ingredient in all_ingredients:
                # Извлекаем название продукта и количество
                match = re.match(r'(.+?)\s*-\s*(\d+\.?\d*)\s*([гкгмлл]?)', ingredient)
                if match:
                    name = match.group(1).strip()
                    amount = float(match.group(2))
                    unit = match.group(3) if match.group(3) else 'г'
                    
                    key = f"{name} ({unit})"
                    if key in ingredient_totals:
                        ingredient_totals[key] += amount
                    else:
                        ingredient_totals[key] = amount
                else:
                    # Если не удалось распарсить, просто добавляем как есть
                    if ingredient in ingredient_totals:
                        ingredient_totals[ingredient] += 1
                    else:
                        ingredient_totals[ingredient] = 1
            
            # Форматируем результат
            formatted_ingredients = []
            for ingredient, total in ingredient_totals.items():
                if total == int(total):
                    total = int(total)
                formatted_ingredients.append(f"{ingredient.split(' (')[0]} - {total}{ingredient.split('(')[-1].rstrip(')') if '(' in ingredient else 'шт'}")
            
            # Сортируем по алфавиту
            formatted_ingredients.sort()
            
            if not formatted_ingredients:
                # Демо-данные, если не удалось извлечь ингредиенты
                return [
                    "Куриная грудка - 700г",
                    "Рыба белая - 600г", 
                    "Овощи сезонные - 2000г",
                    "Фрукты - 1500г",
                    "Крупы - 1000г",
                    "Яйца - 10шт",
                    "Молочные продукты - 1000г",
                    "Оливковое масло - 200мл",
                    "Специи - по вкусу"
                ]
            
            return formatted_ingredients[:25]  # Ограничиваем список
            
        except Exception as e:
            logger.error(f"Error generating shopping list: {e}")
            return [
                "Куриная грудка - 700г",
                "Рыба белая - 600г",
                "Овощи сезонные - 2000г",
                "Фрукты - 1500г",
                "Крупы - 1000г"
            ]
    
    async def send_plan_as_file(self, update, context, user_id):
        """Отправляет план в виде файла"""
        try:
            plan = get_latest_plan(user_id)
            if not plan:
                if hasattr(update, 'message'):
                    await update.message.reply_text("❌ У вас нет активного плана питания")
                else:
                    await update.edit_message_text("❌ У вас нет активного плана питания")
                return
            
            filename = f"nutrition_plan_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            
            with open(filename, 'w', encoding='utf-8') as f:
                f.write("🍎 ПЕРСОНАЛЬНЫЙ ПЛАН ПИТАНИЯ\n")
                f.write("=" * 50 + "\n\n")
                
                user_data = plan.get('user_data', {})
                f.write("👤 ВАШИ ДАННЫЕ:\n")
                f.write(f"   Пол: {user_data.get('gender', '')}\n")
                f.write(f"   Возраст: {user_data.get('age', '')} лет\n")
                f.write(f"   Рост: {user_data.get('height', '')} см\n")
                f.write(f"   Вес: {user_data.get('weight', '')} кг\n")
                f.write(f"   Цель: {user_data.get('goal', '')}\n")
                f.write(f"   Активность: {user_data.get('activity', '')}\n\n")
                
                # Рекомендации по воде
                if 'water_recommendation' in plan:
                    water = plan['water_recommendation']
                    f.write("💧 РЕКОМЕНДАЦИИ ПО ВОДЕ:\n")
                    f.write(f"   Ежедневная норма: {water.get('daily_recommendation', '2000 мл')}\n")
                    f.write(f"   {water.get('description', '')}\n\n")
                    f.write("   Советы:\n")
                    for tip in water.get('tips', []):
                        f.write(f"   {tip}\n")
                    f.write("\n")
                
                # Список покупок
                f.write("🛒 СПИСОК ПОКУПОК НА НЕДЕЛЮ:\n")
                f.write("-" * 40 + "\n")
                shopping_list = self._generate_shopping_list(plan)
                for i, item in enumerate(shopping_list, 1):
                    f.write(f"{i}. {item}\n")
                f.write("\n")
                
                # План на неделю
                f.write("📅 ПЛАН ПИТАНИЯ НА НЕДЕЛЮ:\n")
                f.write("=" * 50 + "\n\n")
                
                for day in plan.get('days', []):
                    f.write(f"=== {day['name']} ===\n")
                    f.write(f"🔥 Общая калорийность: {day.get('total_calories', '~1800-2000 ккал')}\n\n")
                    
                    for meal in day.get('meals', []):
                        f.write(f"{meal['emoji']} {meal['type']} ({meal['time']})\n")
                        f.write(f"   Блюдо: {meal['name']}\n")
                        f.write(f"   Калории: {meal['calories']}\n")
                        f.write(f"   Время приготовления: {meal['cooking_time']}\n")
                        f.write("   Ингредиенты:\n")
                        ingredients_lines = meal['ingredients'].split('\n')
                        for line in ingredients_lines:
                            f.write(f"     {line}\n")
                        f.write("   Приготовление:\n")
                        instructions_lines = meal['instructions'].split('\n')
                        for line in instructions_lines:
                            f.write(f"     {line}\n")
                        f.write("-" * 40 + "\n\n")
                
                f.write("\n💡 СОВЕТЫ:\n")
                f.write("• Пейте достаточное количество воды\n")
                f.write("• Соблюдайте режим питания\n")
                f.write("• Используйте корзину покупок для отслеживания\n\n")
                
                f.write(f"📅 План создан: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n")
            
            with open(filename, 'rb') as file:
                if hasattr(update, 'message'):
                    await update.message.reply_document(
                        document=file,
                        filename=f"План_питания_{user_id}.txt",
                        caption="📄 Ваш персональный план питания со списком покупок"
                    )
                else:
                    await context.bot.send_document(
                        chat_id=user_id,
                        document=file,
                        filename=f"План_питания_{user_id}.txt",
                        caption="📄 Ваш персональный план питания со списком покупок"
                    )
            
            import os
            os.remove(filename)
            
            if not hasattr(update, 'message'):
                await update.edit_message_text("✅ План отправлен в виде файла!")
                
        except Exception as e:
            logger.error(f"Error sending plan as file: {e}")
            if hasattr(update, 'message'):
                await update.message.reply_text("❌ Ошибка при создании файла плана")
            else:
                await update.edit_message_text("❌ Ошибка при создании файла плана")
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик ошибок"""
        health_monitor.increment_error()
        
        # ИГНОРИРУЕМ КОНФЛИКТНЫЕ ОШИБКИ WEBHOOK
        if (isinstance(context.error, Conflict) and 
            "webhook is active" in str(context.error)):
            logger.warning("⚠️ Webhook conflict error (ignored)")
            return
            
        logger.error(f"Exception while handling an update: {context.error}")
        
        try:
            if update and update.effective_message:
                await update.effective_message.reply_text(
                    "❌ Произошла непредвиденная ошибка. Попробуйте позже.",
                    reply_markup=self.menu.get_main_menu()
                )
        except Exception as e:
            logger.error(f"Error in error handler: {e}")

# ==================== ЗАПУСК ПРИЛОЖЕНИЯ ====================

def run_health_checks():
    """Запускает начальные проверки здоровья"""
    logger.info("🔍 Running initial health checks...")
    
    # Сначала инициализируем базу данных
    init_database()
    
    # Затем проверяем здоровье
    if check_database_health():
        logger.info("✅ Database health check passed")
    else:
        logger.error("❌ Database health check failed")
    
    if check_telegram_api_health():
        logger.info("✅ Telegram API health check passed")
    else:
        logger.error("❌ Telegram API health check failed")
    
    if check_yandex_gpt_health():
        logger.info("✅ Yandex GPT health check passed")
    else:
        logger.warning("⚠️ Yandex GPT health check failed or not configured")

def run_webhook():
    """Запускает бота с webhook"""
    try:
        global bot_instance
        
        # Запускаем начальные проверки
        run_health_checks()
        
        # Инициализируем бота
        bot_instance = NutritionBot()
        
        # НАСТРОЙКА WEBHOOK БЕЗ POLLING
        webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"
        
        # Устанавливаем webhook
        bot_instance.application.bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True
        )
        
        logger.info(f"✅ Webhook set to: {webhook_url}")
        health_monitor.update_bot_status("running")
        
        # ЗАПУСКАЕМ FLASK APP ОТДЕЛЬНО
        port = int(os.environ.get('PORT', 5000))
        logger.info(f"🚀 Starting Flask app on port {port}")
        
        app.run(
            host='0.0.0.0',
            port=port,
            debug=False
        )
        
    except Exception as e:
        health_monitor.update_bot_status("error")
        logger.error(f"❌ Failed to start webhook bot: {e}")
        raise

def run_polling():
    """Запускает бота в polling режиме (для локальной разработки)"""
    try:
        run_health_checks()
        bot = NutritionBot()
        logger.info("🔄 Starting in POLLING mode")
        bot.application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )
    except Exception as e:
        logger.error(f"❌ Failed to start polling bot: {e}")
        raise

if __name__ == '__main__':
    # АВТОМАТИЧЕСКОЕ ОПРЕДЕЛЕНИЕ РЕЖИМА
    if RENDER_EXTERNAL_URL:
        logger.info("🚀 Starting in WEBHOOK mode for Render")
        run_webhook()
    else:
        logger.info("🔄 Starting in POLLING mode for local development")
        run_polling()
