import os
import logging
import threading
import time
import sqlite3
import json
import requests  # ЗАМЕНА aiohttp на requests
import signal
import atexit
import socket
import sys
import re
from datetime import datetime, timedelta
from flask import Flask, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== КОНФИГУРАЦИЯ ====================

# ID администратора (замени на свой Telegram ID)
ADMIN_USER_ID = 362423055  # ⚠️ ЗАМЕНИ на свой реальный ID

# Настройки канала для подписки
CHANNEL_USERNAME = "@ppsupershef"  # Username канала

# Yandex GPT настройки
YANDEX_API_KEY = os.getenv('YANDEX_API_KEY')
YANDEX_FOLDER_ID = os.getenv('YANDEX_FOLDER_ID')
YANDEX_GPT_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

# ==================== БАЗА ДАННЫХ ====================

def init_database():
    """Инициализация базы данных"""
    conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
    cursor = conn.cursor()
    
    # Включаем оптимизации для SQLite
    cursor.execute('PRAGMA journal_mode=WAL')
    cursor.execute('PRAGMA synchronous=NORMAL')
    cursor.execute('PRAGMA cache_size=-64000')
    cursor.execute('PRAGMA foreign_keys=ON')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            subscribed BOOLEAN DEFAULT FALSE,
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
            plan_count INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shopping_lists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan_id INTEGER NOT NULL,
            items TEXT,
            checked_items TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Создаем индексы для производительности
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_user_id ON users(user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_plans_user_id ON nutrition_plans(user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_limits_user_id ON user_limits(user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_checkins_user_date ON daily_checkins(user_id, date)')
    
    conn.commit()
    conn.close()
    logger.info("Database initialized with optimizations")

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
    """Проверяет, является ли пользователь администратором"""
    return user_id == ADMIN_USER_ID

def can_make_request(user_id):
    """Проверяет, может ли пользователь сделать запрос плана"""
    try:
        # Администратор всегда может создавать планы
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
        
        can_request = days_since_last_plan >= 7
        conn.close()
        return can_request
        
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
            INSERT OR REPLACE INTO user_limits (user_id, last_plan_date, plan_count, updated_at)
            VALUES (?, ?, COALESCE((SELECT plan_count FROM user_limits WHERE user_id = ?), 0) + 1, ?)
        ''', (user_id, current_time, user_id, current_time))
        
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
        
        # Сохраняем список покупок
        save_shopping_list(user_id, plan_id, plan_data.get('shopping_list', ''))
        
        return plan_id
    except Exception as e:
        logger.error(f"Error saving plan: {e}")
        return None
    finally:
        conn.close()

def save_shopping_list(user_id, plan_id, shopping_list):
    """Сохраняет список покупок"""
    conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            INSERT INTO shopping_lists (user_id, plan_id, items)
            VALUES (?, ?, ?)
        ''', (user_id, plan_id, shopping_list))
        conn.commit()
    except Exception as e:
        logger.error(f"Error saving shopping list: {e}")
    finally:
        conn.close()

def get_shopping_list(user_id, plan_id):
    """Получает список покупок"""
    conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        cursor.execute('SELECT items, checked_items FROM shopping_lists WHERE user_id = ? AND plan_id = ?', 
                      (user_id, plan_id))
        result = cursor.fetchone()
        if result:
            return {
                'items': result[0],
                'checked_items': json.loads(result[1]) if result[1] else []
            }
        return None
    except Exception as e:
        logger.error(f"Error getting shopping list: {e}")
        return None
    finally:
        conn.close()

def update_checked_items(user_id, plan_id, checked_items):
    """Обновляет отмеченные товары"""
    conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            UPDATE shopping_lists 
            SET checked_items = ? 
            WHERE user_id = ? AND plan_id = ?
        ''', (json.dumps(checked_items), user_id, plan_id))
        conn.commit()
    except Exception as e:
        logger.error(f"Error updating checked items: {e}")
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

# ==================== УЛУЧШЕННЫЙ ПАРСЕР GPT ====================

class GPTParser:
    """Улучшенный парсер для структурирования ответов от Yandex GPT"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
    
    def parse_plan_response(self, gpt_response, user_data):
        """Парсит полный ответ GPT и структурирует данные"""
        try:
            self.logger.info("🔍 Starting GPT response parsing...")
            
            structured_plan = {
                'days': [],
                'shopping_list': self._extract_shopping_list(gpt_response),
                'general_recommendations': self._extract_general_recommendations(gpt_response),
                'water_regime': self._extract_water_regime(gpt_response),
                'user_data': user_data,
                'parsed_at': datetime.now().isoformat()
            }
            
            # Разбиваем на дни
            days_texts = self._split_into_days(gpt_response)
            
            for i, day_text in enumerate(days_texts):
                if day_text.strip():
                    day_data = self._parse_day(day_text, i)
                    if day_data:
                        structured_plan['days'].append(day_data)
            
            # УБЕДИТЕЛЬНАЯ СИНХРОНИЗАЦИЯ: если список покупок пустой, генерируем из ингредиентов
            if not structured_plan['shopping_list'] or structured_plan['shopping_list'].strip() == self._generate_default_shopping_list():
                structured_plan['shopping_list'] = self._generate_shopping_list_from_meals(structured_plan['days'])
            
            self.logger.info(f"✅ Successfully parsed {len(structured_plan['days'])} days")
            self.logger.info(f"🛒 Shopping list synchronized: {len(structured_plan['shopping_list'].split(chr(10)))} items")
            return structured_plan
            
        except Exception as e:
            self.logger.error(f"❌ Error parsing GPT response: {e}")
            return self._create_fallback_plan(user_data)
    
    def _split_into_days(self, text):
        """Разбивает текст на секции по дням недели"""
        days_pattern = r'(?:ДЕНЬ\s+\d+|ПОНЕДЕЛЬНИК|ВТОРНИК|СРЕДА|ЧЕТВЕРГ|ПЯТНИЦА|СУББОТА|ВОСКРЕСЕНЬЕ).*?(?=(?:ДЕНЬ\s+\d+|ПОНЕДЕЛЬНИК|ВТОРНИК|СРЕДА|ЧЕТВЕРГ|ПЯТНИЦА|СУББОТА|ВОСКРЕСЕНЬЕ|$))'
        matches = re.findall(days_pattern, text, re.DOTALL | re.IGNORECASE)
        
        if matches:
            return matches
        else:
            return self._split_by_headers(text)
    
    def _split_by_headers(self, text):
        """Альтернативный метод разбивки по заголовкам"""
        lines = text.split('\n')
        days = []
        current_day = []
        day_started = False
        
        for line in lines:
            if re.match(r'.*(день|понедельник|вторник|среда|четверг|пятница|суббота|воскресенье).*', line.lower()):
                if day_started and current_day:
                    days.append('\n'.join(current_day))
                    current_day = []
                day_started = True
            
            if day_started:
                current_day.append(line)
        
        if current_day:
            days.append('\n'.join(current_day))
        
        return days if days else [text]
    
    def _parse_day(self, day_text, day_index):
        """Парсит данные одного дня"""
        day_names = ['ПОНЕДЕЛЬНИК', 'ВТОРНИК', 'СРЕДА', 'ЧЕТВЕРГ', 'ПЯТНИЦА', 'СУББОТА', 'ВОСКРЕСЕНЬЕ']
        day_name = day_names[day_index] if day_index < len(day_names) else f"ДЕНЬ {day_index + 1}"
        
        return {
            'name': day_name,
            'meals': self._extract_meals(day_text),
            'schedule': self._extract_daily_schedule(day_text),
            'total_calories': self._calculate_day_calories(day_text)
        }
    
    def _extract_meals(self, day_text):
        """Извлекает все приемы пищи за день"""
        meals = []
        meal_types = [
            ('ЗАВТРАК', '🍳'),
            ('ПЕРЕКУС 1', '🥗'), 
            ('ОБЕД', '🍲'),
            ('ПЕРЕКУС 2', '🍎'),
            ('УЖИН', '🍛')
        ]
        
        for meal_type, emoji in meal_types:
            meal_data = self._extract_meal_data(day_text, meal_type, emoji)
            if meal_data:
                meals.append(meal_data)
        
        return meals
    
    def _extract_meal_data(self, day_text, meal_type, emoji):
        """Извлекает данные конкретного приема пищи"""
        meal_pattern = f'{meal_type}.*?(?=\\n\\s*(?:{meal_type}|ЗАВТРАК|ОБЕД|УЖИН|ПЕРЕКУС|ДЕНЬ|$))'
        match = re.search(meal_pattern, day_text, re.DOTALL | re.IGNORECASE)
        
        if not match:
            return None
        
        meal_text = match.group(0)
        
        return {
            'type': meal_type,
            'emoji': emoji,
            'name': self._extract_meal_name(meal_text),
            'time': self._extract_meal_time(meal_text),
            'calories': self._extract_calories(meal_text),
            'ingredients': self._extract_ingredients(meal_text),
            'instructions': self._extract_instructions(meal_text),
            'cooking_time': self._extract_cooking_time(meal_text),
            'nutrition': self._extract_nutrition_info(meal_text)
        }
    
    def _extract_meal_name(self, meal_text):
        """Извлекает название блюда"""
        name_patterns = [
            r'\d{1,2}[:.]\d{2}[\s-]*(.*?)(?=\\n|$|Ингредиенты|Приготовление)',
            r'(?:Завтрак|Обед|Ужин|Перекус)[\s:]*(.*?)(?=\\n|$)',
            r'[A-ZА-Я][a-zа-я]+\s+[A-ZА-Яa-zа-я\s]+(?=\\n)'
        ]
        
        for pattern in name_patterns:
            match = re.search(pattern, meal_text, re.DOTALL | re.IGNORECASE)
            if match:
                name = match.group(1) if match.lastindex else match.group(0)
                return self._clean_text(name.strip())
        
        return "Блюдо дня"
    
    def _extract_meal_time(self, meal_text):
        """Извлекает время приема пищи"""
        time_pattern = r'(\d{1,2}[:.]\d{2})'
        match = re.search(time_pattern, meal_text)
        return match.group(1).replace('.', ':') if match else "8:00"
    
    def _extract_calories(self, meal_text):
        """Извлекает калорийность"""
        calorie_patterns = [
            r'(\d+)\s*ккал',
            r'калорийность:\s*(\d+)',
            r'калории:\s*(\d+)'
        ]
        
        for pattern in calorie_patterns:
            match = re.search(pattern, meal_text, re.IGNORECASE)
            if match:
                return f"{match.group(1)} ккал"
        
        return "~350 ккал"
    
    def _extract_ingredients(self, meal_text):
        """Извлекает список ингредиентов"""
        ingredients_section = self._find_section(meal_text, ['ингредиенты', 'состав', 'продукты'])
        
        if ingredients_section:
            lines = ingredients_section.split('\n')
            ingredients = []
            for line in lines:
                line = line.strip()
                if line and not re.match(r'^(ингредиенты|состав|продукы)', line.lower()):
                    clean_line = re.sub(r'^[•\-*\d\.]\s*', '', line)
                    if clean_line and len(clean_line) > 3:
                        ingredients.append(f"• {clean_line}")
            
            if ingredients:
                return '\n'.join(ingredients[:10])
        
        return self._extract_ingredients_fallback(meal_text)
    
    def _extract_instructions(self, meal_text):
        """Извлекает инструкции приготовления"""
        instructions_section = self._find_section(meal_text, ['приготовление', 'рецепт', 'инструкция', 'шаги'])
        
        if instructions_section:
            steps = self._split_into_steps(instructions_section)
            if steps:
                return '\n'.join([f"{i+1}. {step}" for i, step in enumerate(steps)])
        
        return self._generate_simple_instructions(meal_text)
    
    def _extract_cooking_time(self, meal_text):
        """Извлекает время приготовления"""
        time_patterns = [
            r'время[^\d]*(\d+)[^\d]*минут',
            r'готовить[^\d]*(\d+)[^\d]*мин',
            r'(\d+)[^\d]*минут',
            r'(\d+)[^\d]*мин'
        ]
        
        for pattern in time_patterns:
            match = re.search(pattern, meal_text, re.IGNORECASE)
            if match:
                return f"{match.group(1)} минут"
        
        return "15-20 минут"
    
    def _extract_nutrition_info(self, meal_text):
        """Извлекает информацию о БЖУ"""
        nutrition = {}
        
        protein_match = re.search(r'бел[киа-я]*[^\d]*(\d+)[^\d]*г', meal_text, re.IGNORECASE)
        if protein_match:
            nutrition['protein'] = f"{protein_match.group(1)}г"
        
        fat_match = re.search(r'жир[ыа-я]*[^\d]*(\d+)[^\d]*г', meal_text, re.IGNORECASE)
        if fat_match:
            nutrition['fat'] = f"{fat_match.group(1)}г"
        
        carb_match = re.search(r'углевод[ыа-я]*[^\d]*(\d+)[^\d]*г', meal_text, re.IGNORECASE)
        if carb_match:
            nutrition['carbs'] = f"{carb_match.group(1)}г"
        
        return nutrition
    
    def _find_section(self, text, keywords):
        """Находит секцию по ключевым словам"""
        for keyword in keywords:
            pattern = f'{keyword}.*?(?=\\n\\s*(?:{"|".join(keywords)}|$))'
            match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
            if match:
                return match.group(0)
        return None
    
    def _split_into_steps(self, text):
        """Разбивает текст на шаги приготовления"""
        text = re.sub(r'^(приготовление|рецепт|инструкция)[:\s]*', '', text, flags=re.IGNORECASE)
        
        patterns = [
            r'\d+[\.\)]\s*(.*?)(?=\d+[\.\)]|$)',
            r'[•\-]\s*(.*?)(?=\\n[•\-]|$)',
            r'(?<=\\n)(.*?)(?=\\n|$)'
        ]
        
        for pattern in patterns:
            steps = re.findall(pattern, text, re.DOTALL)
            if steps and len(steps) > 1:
                return [self._clean_text(step) for step in steps if step.strip()]
        
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        return lines[:7]
    
    def _extract_ingredients_fallback(self, meal_text):
        """Альтернативный метод извлечения ингредиентов"""
        common_ingredients = [
            'овсян', 'гречк', 'рис', 'куриц', 'рыб', 'творог', 'йогурт', 'молок',
            'яйц', 'овощ', 'фрукт', 'орех', 'сыр', 'хлеб', 'масл', 'сметан'
        ]
        
        lines = meal_text.split('\n')
        ingredients = []
        
        for line in lines:
            line_lower = line.lower()
            if any(ingredient in line_lower for ingredient in common_ingredients):
                clean_line = re.sub(r'^[•\-*\d\.]\s*', '', line.strip())
                if clean_line and len(clean_line) > 5:
                    ingredients.append(f"• {clean_line}")
        
        return '\n'.join(ingredients[:8]) if ingredients else "• Ингредиенты будут уточнены"
    
    def _generate_simple_instructions(self, meal_text):
        """Генерирует простые инструкции на основе текста"""
        return """1. Подготовьте все ингредиенты
2. Следуйте стандартному приготовлению
3. Готовьте до готовности
4. Подавайте свежим"""
    
    def _extract_shopping_list(self, text):
        """УЛУЧШЕННОЕ извлечение списка покупок"""
        shopping_section = self._find_section(text, ['список покупок', 'покупки', 'продукты на неделю', 'шопинг-лист'])
        
        if shopping_section:
            lines = shopping_section.split('\n')
            items = []
            for line in lines:
                line = line.strip()
                if line and not re.match(r'^(список покупок|покупки|продукты|шопинг-лист)', line.lower()):
                    clean_line = re.sub(r'^[•\-*\d\.]\s*', '', line)
                    if clean_line and len(clean_line) > 3:
                        items.append(clean_line)
            
            if items:
                unique_items = list(dict.fromkeys(items))  # Удаляем дубликаты
                return '\n'.join(unique_items[:25])
        
        # Если не нашли в отдельной секции, ищем в общем тексте
        return self._extract_shopping_list_from_text(text)
    
    def _extract_shopping_list_from_text(self, text):
        """Извлекает список покупок из общего текста"""
        # Ищем паттерны типа "Продукты:", "Необходимо:" и т.д.
        shopping_patterns = [
            r'(?:продукты|покупки|необходимо|ингредиенты)[:\s]*\n((?:.*\n){5,20})',
            r'(?:закупить|приобрести)[^.]*?:\n((?:.*\n){5,15})'
        ]
        
        for pattern in shopping_patterns:
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                items_text = match.group(1)
                items = []
                for line in items_text.split('\n'):
                    line = line.strip()
                    if line and len(line) > 3:
                        clean_line = re.sub(r'^[•\-*\d\.]\s*', '', line)
                        items.append(clean_line)
                
                if items:
                    return '\n'.join(items[:20])
        
        return ""
    
    def _generate_shopping_list_from_meals(self, days):
        """ГЕНЕРИРУЕТ список покупок из ингредиентов всех блюд"""
        self.logger.info("🛒 Generating shopping list from meal ingredients...")
        
        all_ingredients = []
        
        for day in days:
            for meal in day.get('meals', []):
                ingredients_text = meal.get('ingredients', '')
                if ingredients_text:
                    # Извлекаем ингредиенты из текста
                    ingredients = self._parse_ingredients_from_text(ingredients_text)
                    all_ingredients.extend(ingredients)
        
        # Объединяем и удаляем дубликаты
        unique_ingredients = list(dict.fromkeys(all_ingredients))
        
        if unique_ingredients:
            shopping_list = '\n'.join(unique_ingredients[:30])
            self.logger.info(f"✅ Generated shopping list with {len(unique_ingredients)} unique items")
            return shopping_list
        else:
            self.logger.warning("⚠️ Could not generate shopping list from ingredients, using fallback")
            return self._generate_default_shopping_list()
    
    def _parse_ingredients_from_text(self, ingredients_text):
        """Парсит ингредиенты из текста"""
        lines = ingredients_text.split('\n')
        ingredients = []
        
        for line in lines:
            line = line.strip()
            if line.startswith('•'):
                ingredient = line[1:].strip()
                if len(ingredient) > 3:
                    ingredients.append(ingredient)
        
        return ingredients
    
    def _generate_default_shopping_list(self):
        """Генерирует стандартный список покупок"""
        return """Куриная грудка - 700г
Филе индейки - 500г
Белая рыба (треска, минтай) - 600г
Говядина нежирная - 400г
Яйца - 10 шт
Творог 5% - 500г
Йогурт натуральный - 400г
Молоко 2.5% - 1 л
Сметана 15% - 200г
Сыр твердый - 150г
Помидоры - 500г
Огурцы - 500г
Капуста белокочанная - 500г
Морковь - 300г
Лук репчатый - 300г
Чеснок - 1 головка
Зелень (петрушка, укроп) - 1 пучок
Яблоки - 500г
Бананы - 500г
Апельсины - 300г
Гречка - 300г
Овсяные хлопья - 300г
Рис бурый - 300г
Хлеб ржаной - 1 буханка
Масло оливковое - 150мл
Масло подсолнечное - 150мл"""
    
    def _extract_general_recommendations(self, text):
        """Извлекает общие рекомендации"""
        recommendations = []
        
        water_match = re.search(r'(пить.*?вод[а-я]*\s*\d+.*?мл)', text, re.IGNORECASE)
        if water_match:
            recommendations.append(f"💧 {water_match.group(1)}")
        
        regime_match = re.search(r'(режим.*?сна.*?\d+.*?час)', text, re.IGNORECASE)
        if regime_match:
            recommendations.append(f"😴 {regime_match.group(1)}")
        
        return '\n'.join(recommendations) if recommendations else "💡 Следуйте индивидуальным рекомендациям плана"
    
    def _extract_water_regime(self, text):
        """Извлекает водный режим"""
        water_pattern = r'(?:вод[а-я]*\s*режим|пить[а-я]*\s*вод[а-я]*).*?(\d+.*?мл)'
        match = re.search(water_pattern, text, re.IGNORECASE)
        return match.group(1) if match else "1.5-2 литра в день"
    
    def _calculate_day_calories(self, day_text):
        """Рассчитывает общую калорийность дня"""
        calorie_matches = re.findall(r'(\d+)\s*ккал', day_text, re.IGNORECASE)
        if calorie_matches:
            total = sum(int(cal) for cal in calorie_matches[:10])
            return f"{total} ккал"
        return "~1800 ккал"
    
    def _clean_text(self, text):
        """Очищает текст от лишних пробелов и символов"""
        if not text:
            return ""
        text = re.sub(r'\s+', ' ', text)
        text = re.sub(r'[«»"“”]', '', text)
        return text.strip()
    
    def _create_fallback_plan(self, user_data):
        """Создает резервный план при ошибке парсинга"""
        self.logger.warning("🔄 Using fallback plan")
        fallback_plan = {
            'days': self._create_sample_days(),
            'shopping_list': self._generate_default_shopping_list(),
            'general_recommendations': "💡 Используйте свежие сезонные продукты",
            'water_regime': "1.5-2 литра воды в день",
            'user_data': user_data,
            'parsed_at': datetime.now().isoformat()
        }
        
        # ГАРАНТИРУЕМ синхронизацию даже в fallback-режиме
        fallback_plan['shopping_list'] = self._generate_shopping_list_from_meals(fallback_plan['days'])
        
        return fallback_plan
    
    def _create_sample_days(self):
        """Создает примерные данные дней"""
        sample_meals = [
            {
                'type': 'ЗАВТРАК',
                'emoji': '🍳',
                'name': 'Овсяная каша с фруктами',
                'time': '8:00',
                'calories': '350 ккал',
                'ingredients': '• Овсяные хлопья - 60г\n• Молоко - 150мл\n• Банан - 1 шт\n• Мед - 1 ч.л.',
                'instructions': '1. Варите овсянку 10 минут\n2. Добавьте банан и мед\n3. Подавайте теплым',
                'cooking_time': '15 минут',
                'nutrition': {'protein': '12г', 'carbs': '60г', 'fat': '8г'}
            },
            {
                'type': 'ОБЕД',
                'emoji': '🍲',
                'name': 'Куриная грудка с гречкой',
                'time': '13:00',
                'calories': '450 ккал',
                'ingredients': '• Куриная грудка - 150г\n• Гречка - 80г\n• Огурцы - 100г\n• Помидоры - 100г',
                'instructions': '1. Отварите гречку\n2. Приготовьте куриную грудку\n3. Подавайте с овощами',
                'cooking_time': '25 минут',
                'nutrition': {'protein': '35г', 'carbs': '45г', 'fat': '10г'}
            }
        ]
        
        days = []
        for i in range(7):
            day_meals = []
            for meal in sample_meals:
                # Немного варьируем ингредиенты для разных дней
                varied_meal = meal.copy()
                if i % 2 == 0:
                    varied_meal['ingredients'] = varied_meal['ingredients'].replace('Овсяные хлопья', 'Гречневые хлопья')
                if i % 3 == 0:
                    varied_meal['ingredients'] = varied_meal['ingredients'].replace('Куриная грудка', 'Филе индейки')
                day_meals.append(varied_meal)
            
            days.append({
                'name': f'ДЕНЬ {i+1}', 
                'meals': day_meals,
                'total_calories': '~1800 ккал'
            })
        
        return days

# ==================== ИНТЕРАКТИВНЫЕ МЕНЮ ====================

class InteractiveMenu:
    def __init__(self):
        self.days = ['ПОНЕДЕЛЬНИК', 'ВТОРНИК', 'СРЕДА', 'ЧЕТВЕРГ', 'ПЯТНИЦА', 'СУББОТА', 'ВОСКРЕСЕНЬЕ']
        self.meals = ['ЗАВТРАК', 'ПЕРЕКУС 1', 'ОБЕД', 'ПЕРЕКУС 2', 'УЖИН']
    
    def get_main_menu(self):
        """Главное меню команд"""
        keyboard = [
            [InlineKeyboardButton("📊 СОЗДАТЬ ПЛАН", callback_data="cmd_create_plan")],
            [InlineKeyboardButton("📈 ЧЕК-ИН", callback_data="cmd_checkin")],
            [InlineKeyboardButton("📊 СТАТИСТИКА", callback_data="cmd_stats")],
            [InlineKeyboardButton("❓ ПОМОЩЬ", callback_data="cmd_help")]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def get_plan_data_input(self, step=1):
        """Клавиатура для ввода данных плана"""
        if step == 1:  # Выбор пола
            keyboard = [
                [InlineKeyboardButton("👨 МУЖЧИНА", callback_data="gender_male")],
                [InlineKeyboardButton("👩 ЖЕНЩИНА", callback_data="gender_female")],
                [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_to_main")]
            ]
        elif step == 2:  # Выбор цели
            keyboard = [
                [InlineKeyboardButton("🎯 ПОХУДЕНИЕ", callback_data="goal_weight_loss")],
                [InlineKeyboardButton("💪 НАБОР МАССЫ", callback_data="goal_mass")],
                [InlineKeyboardButton("⚖️ ПОДДЕРЖАНИЕ", callback_data="goal_maintain")],
                [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_to_gender")]
            ]
        elif step == 3:  # Выбор активности
            keyboard = [
                [InlineKeyboardButton("🏃‍♂️ ВЫСОКАЯ", callback_data="activity_high")],
                [InlineKeyboardButton("🚶‍♂️ СРЕДНЯЯ", callback_data="activity_medium")],
                [InlineKeyboardButton("💤 НИЗКАЯ", callback_data="activity_low")],
                [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_to_goal")]
            ]
        
        return InlineKeyboardMarkup(keyboard)

    def get_checkin_options(self, step=1):
        """Опции для чек-ина"""
        if step == 1:  # Самочувствие
            keyboard = []
            for i in range(0, 10, 5):
                row = []
                for j in range(1, 6):
                    num = i + j
                    row.append(InlineKeyboardButton(str(num), callback_data=f"wellbeing_{num}"))
                keyboard.append(row)
            keyboard.append([InlineKeyboardButton("↩️ НАЗАД", callback_data="back_to_main")])
            
        elif step == 2:  # Сон
            keyboard = []
            for i in range(0, 10, 5):
                row = []
                for j in range(1, 6):
                    num = i + j
                    row.append(InlineKeyboardButton(str(num), callback_data=f"sleep_{num}"))
                keyboard.append(row)
            keyboard.append([InlineKeyboardButton("↩️ НАЗАД", callback_data="back_to_wellbeing")])
        
        return InlineKeyboardMarkup(keyboard)

    def get_days_keyboard(self):
        """Клавиатура для выбора дней + список покупок"""
        keyboard = []
        
        # Первые 6 дней в 2 ряда по 3 кнопки
        for i in range(0, 6, 3):
            row = []
            for j in range(3):
                if i + j < len(self.days):
                    row.append(InlineKeyboardButton(
                        self.days[i + j], 
                        callback_data=f"day_{i+j}"
                    ))
            keyboard.append(row)
        
        # Последний день и список покупок в одном ряду
        keyboard.append([
            InlineKeyboardButton(self.days[6], callback_data="day_6"),
            InlineKeyboardButton("🛒 СПИСОК ПОКУПОК", callback_data="shopping_list")
        ])
        
        keyboard.append([InlineKeyboardButton("💧 ВОДНЫЙ РЕЖИМ", callback_data="water_regime")])
        keyboard.append([InlineKeyboardButton("🏠 ГЛАВНОЕ МЕНЮ", callback_data="back_to_main")])
        
        return InlineKeyboardMarkup(keyboard)
    
    def get_meals_keyboard(self, day_index):
        """Клавиатура для выбора приемов пищи"""
        keyboard = []
        emojis = ['🍳', '🥗', '🍲', '🍎', '🍛']
        
        for i, meal in enumerate(self.meals):
            keyboard.append([
                InlineKeyboardButton(
                    f"{emojis[i]} {meal}", 
                    callback_data=f"meal_{day_index}_{i}"
                )
            ])
        
        keyboard.append([InlineKeyboardButton("↩️ НАЗАД К ДНЯМ", callback_data="back_to_days")])
        keyboard.append([InlineKeyboardButton("🏠 ГЛАВНОЕ МЕНЮ", callback_data="back_to_main")])
        
        return InlineKeyboardMarkup(keyboard)

    def get_shopping_list_keyboard(self, checked_count, total_count):
        """Клавиатура для списка покупок"""
        progress = f" ({checked_count}/{total_count})" if total_count > 0 else ""
        
        keyboard = [
            [InlineKeyboardButton(f"✅ ОЧИСТИТЬ ОТМЕТКИ{progress}", callback_data="clear_checked")],
            [InlineKeyboardButton("📋 СОХРАНИТЬ СПИСОК", callback_data="save_shopping_list")],
            [InlineKeyboardButton("↩️ НАЗАД К ДНЯМ", callback_data="back_to_days")],
            [InlineKeyboardButton("🏠 ГЛАВНОЕ МЕНЮ", callback_data="back_to_main")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_item_toggle_keyboard(self, item_index, is_checked):
        """Клавиатура для переключения отметки товара"""
        action = "uncheck" if is_checked else "check"
        keyboard = [
            [InlineKeyboardButton("✅ ОТМЕТИТЬ" if not is_checked else "❌ СНЯТЬ ОТМЕТКУ", 
                                callback_data=f"toggle_{action}_{item_index}")],
            [InlineKeyboardButton("↩️ НАЗАД К СПИСКУ", callback_data="back_to_shopping_list")]
        ]
        return InlineKeyboardMarkup(keyboard)

    def get_water_regime_keyboard(self):
        """Клавиатура для водный режим"""
        keyboard = [
            [InlineKeyboardButton("↩️ НАЗАД К ДНЯМ", callback_data="back_to_days")],
            [InlineKeyboardButton("🏠 ГЛАВНОЕ МЕНЮ", callback_data="back_to_main")]
        ]
        return InlineKeyboardMarkup(keyboard)

# ==================== FLASK APP ====================

app = Flask(__name__)

@app.route('/')
def home():
    return """
    <h1>🤖 Nutrition Bot is Running!</h1>
    <p>Бот для создания персональных планов питания</p>
    <p><a href="/health">Health Check</a></p>
    <p>🕒 Last update: {}</p>
    """.format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

@app.route('/health')
def health_check():
    return {
        "status": "healthy", 
        "service": "nutrition-bot",
        "timestamp": datetime.now().isoformat(),
        "version": "6.0"
    }

# ==================== ОСНОВНОЙ КЛАСС БОТА ====================

class NutritionBot:
    def __init__(self):
        self.bot_token = os.getenv('BOT_TOKEN')
        if not self.bot_token:
            logger.error("❌ BOT_TOKEN not found in environment variables")
            raise ValueError("BOT_TOKEN is required")
            
        init_database()
        
        try:
            self.application = Application.builder().token(self.bot_token).build()
            self.menu = InteractiveMenu()
            self._setup_handlers()
            
            logger.info("✅ Bot initialized successfully")
        except Exception as e:
            logger.error(f"❌ Failed to initialize bot: {e}")
            raise
    
    def _setup_handlers(self):
        """Настройка обработчиков"""
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("menu", self.menu_command))
        self.application.add_handler(CallbackQueryHandler(self.handle_callback))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.application.add_error_handler(self.error_handler)
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
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
            logger.error(f"Error in start_command: {e}")
            await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
    
    async def menu_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает главное меню"""
        await update.message.reply_text(
            "🤖 ГЛАВНОЕ МЕНЮ\n\nВыберите действие:",
            reply_markup=self.menu.get_main_menu()
        )
    
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик callback'ов"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        try:
            # Главные команды
            if data == "cmd_create_plan":
                await self._handle_create_plan(query, context)
            elif data == "cmd_checkin":
                await self._handle_checkin(query, context)
            elif data == "cmd_stats":
                await self._handle_stats(query, context)
            elif data == "cmd_help":
                await self._handle_help(query, context)
            
            # Навигация
            elif data == "back_to_main":
                await self._show_main_menu(query)
            
            # Ввод данных плана
            elif data.startswith("gender_"):
                await self._handle_gender(query, context, data)
            elif data.startswith("goal_"):
                await self._handle_goal(query, context, data)
            elif data.startswith("activity_"):
                await self._handle_activity(query, context, data)
            elif data in ["back_to_gender", "back_to_goal"]:
                await self._handle_back_navigation(query, context, data)
            
            # Чек-ин
            elif data.startswith("wellbeing_"):
                await self._handle_wellbeing(query, context, data)
            elif data.startswith("sleep_"):
                await self._handle_sleep(query, context, data)
            elif data == "back_to_wellbeing":
                await self._handle_checkin(query, context)
                
        except Exception as e:
            logger.error(f"Error in callback handler: {e}")
            await query.edit_message_text("❌ Произошла ошибка. Попробуйте снова.")
    
    async def _show_main_menu(self, query):
        """Показывает главное меню"""
        await query.edit_message_text(
            "🤖 ГЛАВНОЕ МЕНЮ\n\nВыберите действие:",
            reply_markup=self.menu.get_main_menu()
        )
    
    # ==================== СОЗДАНИЕ ПЛАНА ====================
    
    async def _handle_create_plan(self, query, context):
        """Обработчик создания плана"""
        user_id = query.from_user.id
            
        if not is_admin(user_id) and not can_make_request(user_id):
            days_remaining = get_days_until_next_plan(user_id)
            await query.edit_message_text(
                f"⏳ Вы уже запрашивали план питания\nСледующий доступен через {days_remaining} дней",
                reply_markup=self.menu.get_main_menu()
            )
            return
        
        context.user_data['plan_data'] = {}
        await query.edit_message_text(
            "📊 СОЗДАНИЕ ПЛАНА ПИТАНИЯ\n\n1️⃣ Выберите ваш пол:",
            reply_markup=self.menu.get_plan_data_input(step=1)
        )
    
    async def _handle_gender(self, query, context, data):
        """Обработчик выбора пола"""
        gender = 'Мужчина' if data == 'gender_male' else 'Женщина'
        context.user_data['plan_data']['gender'] = gender
        
        await query.edit_message_text(
            f"✅ Пол: {gender}\n\n2️⃣ Выберите вашу цель:",
            reply_markup=self.menu.get_plan_data_input(step=2)
        )
    
    async def _handle_goal(self, query, context, data):
        """Обработчик выбора цели"""
        goal_map = {'weight_loss': 'похудение', 'mass': 'набор массы', 'maintain': 'поддержание'}
        goal = goal_map[data.split('_')[1]]
        context.user_data['plan_data']['goal'] = goal
        
        await query.edit_message_text(
            f"✅ Пол: {context.user_data['plan_data']['gender']}\n"
            f"✅ Цель: {goal}\n\n"
            "3️⃣ Выберите уровень активности:",
            reply_markup=self.menu.get_plan_data_input(step=3)
        )
    
    async def _handle_activity(self, query, context, data):
        """Обработчик выбора активности"""
        activity_map = {'high': 'высокая', 'medium': 'средняя', 'low': 'низкая'}
        activity = activity_map[data.split('_')[1]]
        context.user_data['plan_data']['activity'] = activity
        
        context.user_data['awaiting_input'] = 'plan_details'
        await query.edit_message_text(
            f"✅ Пол: {context.user_data['plan_data']['gender']}\n"
            f"✅ Цель: {context.user_data['plan_data']['goal']}\n"
            f"✅ Активность: {activity}\n\n"
            "4️⃣ Введите через запятую:\n"
            "• Возраст (лет)\n• Рост (см)\n• Вес (кг)\n\n"
            "📝 Пример: 30, 180, 80\n\n"
            "Или нажмите назад для изменения данных:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_to_goal")]
            ])
        )
    
    async def _handle_back_navigation(self, query, context, data):
        """Обработчик навигации назад"""
        if data == "back_to_gender":
            await query.edit_message_text(
                "📊 СОЗДАНИЕ ПЛАНА ПИТАНИЯ\n\n1️⃣ Выберите ваш пол:",
                reply_markup=self.menu.get_plan_data_input(step=1)
            )
        elif data == "back_to_goal":
            await query.edit_message_text(
                f"✅ Пол: {context.user_data['plan_data']['gender']}\n\n2️⃣ Выберите вашу цель:",
                reply_markup=self.menu.get_plan_data_input(step=2)
            )
    
    # ==================== ЧЕК-ИН ====================
    
    async def _handle_checkin(self, query, context):
        """Обработчик чек-ина"""
        context.user_data['checkin_data'] = {}
        await query.edit_message_text(
            "📈 ЕЖЕДНЕВНЫЙ ЧЕК-ИН\n\n1️⃣ Оцените ваше самочувствие (1-10):",
            reply_markup=self.menu.get_checkin_options(step=1)
        )
    
    async def _handle_wellbeing(self, query, context, data):
        """Обработчик выбора самочувствия"""
        wellbeing = int(data.split('_')[1])
        context.user_data['checkin_data']['wellbeing'] = wellbeing
        
        await query.edit_message_text(
            f"✅ Самочувствие: {wellbeing}/10\n\n2️⃣ Оцените качество сна (1-10):",
            reply_markup=self.menu.get_checkin_options(step=2)
        )
    
    async def _handle_sleep(self, query, context, data):
        """Обработчик выбора качества сна"""
        sleep = int(data.split('_')[1])
        context.user_data['checkin_data']['sleep'] = sleep
        
        context.user_data['awaiting_input'] = 'checkin_details'
        await query.edit_message_text(
            f"✅ Самочувствие: {context.user_data['checkin_data']['wellbeing']}/10\n"
            f"✅ Сон: {sleep}/10\n\n"
            "3️⃣ Введите через запятую:\n"
            "• Вес (кг)\n• Объем талии (см)\n\n"
            "📝 Пример: 70.5, 85",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_to_wellbeing")]
            ])
        )
    
    # ==================== СТАТИСТИКА И ПОМОЩЬ ====================
    
    async def _handle_stats(self, query, context):
        """Обработчик статистики"""
        user_id = query.from_user.id
        checkins = get_user_stats(user_id)
        
        if not checkins:
            await query.edit_message_text(
                "📊 У вас пока нет данных для статистики\n\n"
                "💡 Используйте чек-ин для отслеживания прогресса",
                reply_markup=self.menu.get_main_menu()
            )
            return
        
        stats_text = "📊 ВАША СТАТИСТИКА\n\n"
        for checkin in reversed(checkins):
            date_str = datetime.fromisoformat(checkin[0]).strftime('%d.%m')
            stats_text += f"📅 {date_str}: Вес {checkin[1]}кг, Талия {checkin[2]}см\n"
        
        await query.edit_message_text(
            stats_text,
            reply_markup=self.menu.get_main_menu()
        )
    
    async def _handle_help(self, query, context):
        """Обработчик помощи"""
        help_text = """
❓ ПОМОЩЬ ПО БОТУ

📊 СОЗДАТЬ ПЛАН - индивидуальный AI-план питания
📈 ЧЕК-ИН - ежедневное отслеживание прогресса  
📊 СТАТИСТИКА - ваши результаты за 7 дней

💡 Все команды доступны через меню
🔒 Ваши данные конфиденциальны

🤖 Бот работает в тестовом режиме
✅ Все функции бесплатны
"""
        await query.edit_message_text(
            help_text,
            reply_markup=self.menu.get_main_menu()
        )
    
    # ==================== ОБРАБОТКА СООБЩЕНИЙ ====================
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик текстовых сообщений"""
        user_id = update.effective_user.id
        text = update.message.text
        
        if context.user_data.get('awaiting_input') == 'plan_details':
            await self._process_plan_details(update, context, text)
        elif context.user_data.get('awaiting_input') == 'checkin_details':
            await self._process_checkin_details(update, context, text)
        else:
            await update.message.reply_text(
                "🤖 Используйте меню для навигации:",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _process_plan_details(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        """Обрабатывает детали плана"""
        try:
            parts = [part.strip() for part in text.split(',')]
            if len(parts) != 3:
                raise ValueError("Нужно ввести 3 числа через запятую")
            
            age, height, weight = int(parts[0]), int(parts[1]), float(parts[2])
            
            user_data = {
                **context.user_data['plan_data'],
                'age': age,
                'height': height,
                'weight': weight,
                'user_id': update.effective_user.id,
                'username': update.effective_user.username
            }
            
            processing_msg = await update.message.reply_text("🔄 Генерируем ваш AI-план питания...")
            
            # Генерируем план
            plan_data = await self._generate_plan_with_gpt(user_data)
            plan_id = save_plan(user_data['user_id'], plan_data)
            update_user_limit(user_data['user_id'])
            
            await processing_msg.delete()
            
            await update.message.reply_text(
                "🎉 ВАШ ПЛАН ПИТАНИЯ ГОТОВ!\n\n"
                "📋 Используйте меню для навигации:",
                reply_markup=self.menu.get_main_menu()
            )
            
            context.user_data['awaiting_input'] = None
            
        except Exception as e:
            await update.message.reply_text(
                "❌ Ошибка в формате данных. Используйте: Возраст, Рост, Вес\nПример: 30, 180, 80"
            )
    
    async def _process_checkin_details(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        """Обрабатывает детали чек-ина"""
        try:
            parts = [part.strip() for part in text.split(',')]
            if len(parts) != 2:
                raise ValueError("Нужно ввести 2 числа через запятую")
            
            weight, waist = float(parts[0]), int(parts[1])
            wellbeing = context.user_data['checkin_data']['wellbeing']
            sleep = context.user_data['checkin_data']['sleep']
            
            save_checkin(update.effective_user.id, weight, waist, wellbeing, sleep)
            
            feedback = self._analyze_checkin(wellbeing, sleep)
            await update.message.reply_text(
                f"✅ Данные сохранены!\n\n{feedback}",
                reply_markup=self.menu.get_main_menu()
            )
            
            context.user_data['awaiting_input'] = None
            
        except Exception as e:
            await update.message.reply_text(
                "❌ Ошибка в формате данных. Используйте: Вес, Талия\nПример: 70.5, 85"
            )
    
    def _analyze_checkin(self, wellbeing, sleep):
        """Анализирует данные чек-ина"""
        feedback = []
        if wellbeing >= 8: 
            feedback.append("🎉 Отличное самочувствие!")
        elif wellbeing >= 6: 
            feedback.append("👍 Хорошее состояние")
        else: 
            feedback.append("💤 Обратите внимание на восстановление")
        
        if sleep >= 8: 
            feedback.append("😴 Качество сна на высоте!")
        elif sleep >= 6: 
            feedback.append("🛌 Сон в норме")
        else: 
            feedback.append("🌙 Старайтесь спать 7-8 часов")
        
        return "\n".join(feedback)
    
    # ==================== YANDEX GPT ====================
    
    async def _generate_plan_with_gpt(self, user_data):
        """Генерирует план питания через Yandex GPT"""
        if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
            logger.error("❌ YANDEX GPT KEYS NOT CONFIGURED!")
            return self._generate_detailed_fallback_plan(user_data)
        
        prompt = self._create_gpt_prompt(user_data)
        logger.info(f"🔮 Sending request to Yandex GPT...")
        
        try:
            headers = {
                "Authorization": f"Api-Key {YANDEX_API_KEY}",
                "Content-Type": "application/json"
            }
            
            data = {
                "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/latest",
                "completionOptions": {
                    "stream": False,
                    "temperature": 0.7,
                    "maxTokens": 8000
                },
                "messages": [
                    {
                        "role": "system", 
                        "text": "Ты - профессор нутрициологии с 20-летним опытом. Создавай детальные, практичные планы питания с конкретными рецептами и временем приемов пищи. ОБЯЗАТЕЛЬНО включай подробный список покупок на неделю, который соответствует всем рецептам."
                    },
                    {
                        "role": "user",
                        "text": prompt
                    }
                ]
            }
            
            # СИНХРОННЫЙ ЗАПРОС вместо асинхронного
            response = requests.post(YANDEX_GPT_URL, headers=headers, json=data, timeout=120)
            
            if response.status_code == 200:
                result = response.json()
                gpt_response = result['result']['alternatives'][0]['message']['text']
                logger.info("✅ Yandex GPT response received successfully!")
                
                # Используем улучшенный парсер
                parser = GPTParser()
                structured_plan = parser.parse_plan_response(gpt_response, user_data)
                return structured_plan
            else:
                logger.error(f"❌ Yandex GPT API error {response.status_code}")
                return self._generate_detailed_fallback_plan(user_data)
                
        except Exception as e:
            logger.error(f"❌ Error calling Yandex GPT: {e}")
            return self._generate_detailed_fallback_plan(user_data)

    def _create_gpt_prompt(self, user_data):
        """Создает промт для Yandex GPT"""
        gender = user_data['gender']
        goal = user_data['goal']
        activity = user_data['activity']
        age = user_data['age']
        height = user_data['height']
        weight = user_data['weight']
        
        return f"""
Создай персонализированный план питания на 7 дней с учетом:

👤 ДАННЫЕ ПОЛЬЗОВАТЕЛЯ:
• Пол: {gender}
• Возраст: {age} лет
• Рост: {height} см
• Вес: {weight} кг
• Цель: {goal}
• Уровень активности: {activity}

🎯 ТРЕБОВАНИЯ К ПЛАНУ:
• 5 приемов пищи в день (завтрак, перекус 1, обед, перекус 2, ужин)
• Сбалансированное соотношение БЖУ
• Общая калорийность соответствует цели "{goal}"
• Использование свежих сезонных продуктов
• Простые рецепты с доступными ингредиентами
• Время приготовления не более 30 минут

📋 ФОРМАТ ОТВЕТА:

ДЕНЬ 1 / ПОНЕДЕЛЬНИК

ЗАВТРАК (8:00)
[Название блюда] - [калорийность] ккал

Ингредиенты:
• [ингредиент 1] - [количество]
• [ингредиент 2] - [количество]

Приготовление:
1. [шаг 1]
2. [шаг 2]

ПЕРЕКУС 1 (11:00)
[аналогично...]

ОБЕД (13:00)
[аналогично...]

ПЕРЕКУС 2 (16:00)  
[аналогично...]

УЖИН (19:00)
[аналогично...]

[Аналогично для всех 7 дней]

🛒 СПИСОК ПОКУПОК НА НЕДЕЛЮ:
[ТОЧНОЕ перечисление всех необходимых продуктов из рецептов с количествами]

💡 ОБЩИЕ РЕКОМЕНДАЦИИ:
[советы по питанию, водному режиму, распорядку дня]

💧 ВОДНЫЙ РЕЖИМ:
[рекомендации по потреблению воды]

ВАЖНО: Список покупок должен ТОЧНО соответствовать ингредиентам из всех рецептов недели!
"""
    def _generate_detailed_fallback_plan(self, user_data):
        """Резервный план"""
        parser = GPTParser()
        return parser._create_fallback_plan(user_data)
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик ошибок"""
        logger.error(f"Exception: {context.error}")
    
    def run_web_server(self):
        """Запускает веб-сервер"""
        def run_flask():
            port = int(os.getenv('PORT', 10000))
            app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
        threading.Thread(target=run_flask, daemon=True).start()
        logger.info(f"🌐 Web server started on port {os.getenv('PORT', 10000)}")
    
    def run_bot(self, retry_count=0):
        """Запускает бота"""
        MAX_RETRIES = 2
        
        try:
            logger.info("🔧 Starting bot polling...")
            self.application.run_polling(
                drop_pending_updates=True,
                allowed_updates=['message', 'callback_query']
            )
        except Exception as e:
            if "Conflict" in str(e):
                logger.error("💥 CONFLICT: Another bot instance is running. Exiting.")
                sys.exit(1)
            elif retry_count < MAX_RETRIES:
                logger.error(f"❌ Bot error ({retry_count + 1}/{MAX_RETRIES}): {e}")
                time.sleep(30)
                self.run_bot(retry_count + 1)
            else:
                logger.error(f"💥 Max retries reached. Exiting.")
                sys.exit(1)

def main():
    """Главная функция"""
    logger.info("🚀 Starting nutrition bot services...")
    
    try:
        bot = NutritionBot()
        bot.run_web_server()
        time.sleep(5)
        bot.run_bot()
    except Exception as e:
        logger.error(f"💥 Failed to start services: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
