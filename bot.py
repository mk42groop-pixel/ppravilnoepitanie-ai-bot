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
from flask import Flask, jsonify
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

# ID администратора
ADMIN_USER_ID = 362423055

# Yandex GPT настройки
YANDEX_API_KEY = os.getenv('YANDEX_API_KEY')
YANDEX_FOLDER_ID = os.getenv('YANDEX_FOLDER_ID')
YANDEX_GPT_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

# ==================== БАЗА ДАННЫХ ====================

def init_database():
    """Инициализация базы данных"""
    conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
    cursor = conn.cursor()
    
    cursor.execute('PRAGMA journal_mode=WAL')
    cursor.execute('PRAGMA synchronous=NORMAL')
    cursor.execute('PRAGMA cache_size=-64000')
    
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
    
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_user_id ON users(user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_plans_user_id ON nutrition_plans(user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_limits_user_id ON user_limits(user_id)')
    
    conn.commit()
    conn.close()
    logger.info("Database initialized")

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
            
            # Если дней меньше 7, дополняем
            while len(structured_plan['days']) < 7:
                day_index = len(structured_plan['days'])
                structured_plan['days'].append(self._create_fallback_day(day_index))
            
            self.logger.info(f"✅ Successfully parsed {len(structured_plan['days'])} days")
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
            # Альтернативный метод разбивки
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
        
        # Если приемов пищи меньше 5, дополняем
        while len(meals) < 5:
            meal_index = len(meals)
            meals.append(self._create_fallback_meal(meal_types[meal_index] if meal_index < len(meal_types) else ('ПРИЕМ ПИЩИ', '🍽️')))
        
        return meals
    
    def _extract_meal_data(self, day_text, meal_type, emoji):
        """Извлекает данные конкретного приема пищи"""
        # Ищем секцию с приемом пищи
        meal_pattern = f'{meal_type}.*?(?=\\n\\s*(?:{"|".join([m[0] for m in [("ЗАВТРАК", ""), ("ОБЕД", ""), ("УЖИН", ""), ("ПЕРЕКУС", "")]])}|ДЕНЬ|$))'
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
            'cooking_time': self._extract_cooking_time(meal_text)
        }
    
    def _extract_meal_name(self, meal_text):
        """Извлекает название блюда"""
        # Ищем название после времени или типа приема пищи
        name_patterns = [
            r'\d{1,2}[:.]\d{2}[\s-]*(.*?)(?=\\n|$)',
            r'(?:Завтрак|Обед|Ужин|Перекус)[\s:]*(.*?)(?=\\n|$)',
        ]
        
        for pattern in name_patterns:
            match = re.search(pattern, meal_text, re.DOTALL | re.IGNORECASE)
            if match:
                name = match.group(1) if match.lastindex else match.group(0)
                cleaned_name = self._clean_text(name.strip())
                if cleaned_name and len(cleaned_name) > 2:
                    return cleaned_name
        
        return "Питательное блюдо"
    
    def _extract_meal_time(self, meal_text):
        """Извлекает время приема пищи"""
        time_pattern = r'(\d{1,2}[:.]\d{2})'
        match = re.search(time_pattern, meal_text)
        if match:
            return match.group(1).replace('.', ':')
        
        # Время по умолчанию в зависимости от типа приема пищи
        time_map = {
            'ЗАВТРАК': '8:00',
            'ПЕРЕКУС 1': '11:00', 
            'ОБЕД': '13:00',
            'ПЕРЕКУС 2': '16:00',
            'УЖИН': '19:00'
        }
        return time_map.get('ЗАВТРАК', '8:00')
    
    def _extract_calories(self, meal_text):
        """Извлекает калорийность"""
        calorie_patterns = [
            r'(\d+)\s*ккал',
            r'калорийность:\s*(\d+)',
        ]
        
        for pattern in calorie_patterns:
            match = re.search(pattern, meal_text, re.IGNORECASE)
            if match:
                return f"{match.group(1)} ккал"
        
        return "~350 ккал"
    
    def _extract_ingredients(self, meal_text):
        """Извлекает список ингредиентов"""
        # Ищем секцию с ингредиентами
        ingredients_section = self._find_section(meal_text, ['ингредиенты', 'состав', 'продукты'])
        
        if ingredients_section:
            lines = ingredients_section.split('\n')
            ingredients = []
            for line in lines:
                line = line.strip()
                if line and not re.match(r'^(ингредиенты|состав|продукты)', line.lower()):
                    clean_line = re.sub(r'^[•\-*\d\.]\s*', '', line)
                    if clean_line and len(clean_line) > 3:
                        ingredients.append(f"• {clean_line}")
            
            if ingredients:
                return '\n'.join(ingredients[:8])
        
        return "• Свежие продукты по сезону\n• Специи по вкусу"
    
    def _extract_instructions(self, meal_text):
        """Извлекает инструкции приготовления"""
        instructions_section = self._find_section(meal_text, ['приготовление', 'рецепт', 'инструкция'])
        
        if instructions_section:
            steps = self._split_into_steps(instructions_section)
            if steps:
                return '\n'.join([f"{i+1}. {step}" for i, step in enumerate(steps)])
        
        return "1. Подготовьте все ингредиенты\n2. Следуйте рецепту приготовления\n3. Подавайте свежим"
    
    def _extract_cooking_time(self, meal_text):
        """Извлекает время приготовления"""
        time_patterns = [
            r'время[^\d]*(\d+)[^\d]*минут',
            r'готовить[^\d]*(\d+)[^\d]*мин',
        ]
        
        for pattern in time_patterns:
            match = re.search(pattern, meal_text, re.IGNORECASE)
            if match:
                return f"{match.group(1)} минут"
        
        return "15-20 минут"
    
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
        
        # Ищем нумерованные шаги
        steps = re.findall(r'\d+[\.\)]\s*(.*?)(?=\d+[\.\)]|$)', text, re.DOTALL)
        if steps:
            return [self._clean_text(step) for step in steps if step.strip()]
        
        # Ищем шаги с буллетами
        steps = re.findall(r'[•\-]\s*(.*?)(?=\\n[•\-]|$)', text, re.DOTALL)
        if steps:
            return [self._clean_text(step) for step in steps if step.strip()]
        
        # Разбиваем по строкам
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        return lines[:5]
    
    def _extract_shopping_list(self, text):
        """Извлечение списка покупок"""
        shopping_section = self._find_section(text, ['список покупок', 'покупки', 'продукты на неделю'])
        
        if shopping_section:
            lines = shopping_section.split('\n')
            items = []
            for line in lines:
                line = line.strip()
                if line and not re.match(r'^(список покупок|покупки|продукты)', line.lower()):
                    clean_line = re.sub(r'^[•\-*\d\.]\s*', '', line)
                    if clean_line and len(clean_line) > 3:
                        items.append(clean_line)
            
            if items:
                unique_items = list(dict.fromkeys(items))
                return '\n'.join(unique_items[:20])
        
        return self._generate_default_shopping_list()
    
    def _extract_general_recommendations(self, text):
        """Извлекает общие рекомендации"""
        recommendations = []
        
        water_match = re.search(r'(пить.*?вод[а-я]*\s*\d+.*?мл)', text, re.IGNORECASE)
        if water_match:
            recommendations.append(f"💧 {water_match.group(1)}")
        
        return '\n'.join(recommendations) if recommendations else "💡 Следуйте сбалансированному питанию и пейте достаточное количество воды"
    
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
        """Очищает текст"""
        if not text:
            return ""
        text = re.sub(r'\s+', ' ', text)
        return text.strip()
    
    def _generate_default_shopping_list(self):
        """Генерирует стандартный список покупок"""
        return """Куриная грудка - 700г
Филе индейки - 500г
Белая рыба - 600г
Говядина - 400г
Яйца - 10 шт
Творог 5% - 500г
Йогурт натуральный - 400г
Молоко - 1 л
Сметана - 200г
Сыр - 150г
Помидоры - 500г
Огурцы - 500г
Капуста - 500г
Морковь - 300г
Лук - 300г
Чеснок - 1 головка
Зелень - 1 пучок
Яблоки - 500г
Бананы - 500г
Апельсины - 300г
Гречка - 300г
Овсяные хлопья - 300г
Рис - 300г
Хлеб ржаной - 1 буханка
Масло оливковое - 150мл"""
    
    def _create_fallback_plan(self, user_data):
        """Создает резервный план"""
        self.logger.warning("🔄 Using fallback plan")
        fallback_plan = {
            'days': [self._create_fallback_day(i) for i in range(7)],
            'shopping_list': self._generate_default_shopping_list(),
            'general_recommendations': "💡 Используйте свежие сезонные продукты и пейте достаточное количество воды",
            'water_regime': "1.5-2 литра в день",
            'user_data': user_data,
            'parsed_at': datetime.now().isoformat()
        }
        return fallback_plan
    
    def _create_fallback_day(self, day_index):
        """Создает резервный день"""
        day_names = ['ПОНЕДЕЛЬНИК', 'ВТОРНИК', 'СРЕДА', 'ЧЕТВЕРГ', 'ПЯТНИЦА', 'СУББОТА', 'ВОСКРЕСЕНЬЕ']
        day_name = day_names[day_index] if day_index < len(day_names) else f"ДЕНЬ {day_index + 1}"
        
        return {
            'name': day_name,
            'meals': [self._create_fallback_meal(meal_type) for meal_type in [
                ('ЗАВТРАК', '🍳'), ('ПЕРЕКУС 1', '🥗'), ('ОБЕД', '🍲'), 
                ('ПЕРЕКУС 2', '🍎'), ('УЖИН', '🍛')
            ]],
            'total_calories': '~1800 ккал'
        }
    
    def _create_fallback_meal(self, meal_type):
        """Создает резервный прием пищи"""
        meal_type_name, emoji = meal_type
        
        # Разные блюда для разных приемов пищи
        meals_map = {
            'ЗАВТРАК': {
                'name': 'Овсяная каша с фруктами',
                'ingredients': '• Овсяные хлопья - 60г\n• Молоко - 150мл\n• Банан - 1 шт\n• Мед - 1 ч.л.',
                'instructions': '1. Варите овсянку 10 минут\n2. Добавьте банан и мед\n3. Подавайте теплым'
            },
            'ПЕРЕКУС 1': {
                'name': 'Йогурт с орехами',
                'ingredients': '• Йогурт натуральный - 150г\n• Грецкие орехи - 30г\n• Ягоды - 50г',
                'instructions': '1. Смешайте йогурт с орехами\n2. Добавьте ягоды\n3. Подавайте свежим'
            },
            'ОБЕД': {
                'name': 'Куриная грудка с гречкой',
                'ingredients': '• Куриная грудка - 150г\n• Гречка - 80г\n• Огурцы - 100г\n• Помидоры - 100г',
                'instructions': '1. Отварите гречку\n2. Приготовьте куриную грудку\n3. Подавайте с овощами'
            },
            'ПЕРЕКУС 2': {
                'name': 'Фруктовый салат',
                'ingredients': '• Яблоко - 1 шт\n• Банан - 1 шт\n• Апельсин - 1 шт\n• Йогурт - 50г',
                'instructions': '1. Нарежьте фрукты\n2. Заправьте йогуртом\n3. Подавайте свежим'
            },
            'УЖИН': {
                'name': 'Рыба с овощами',
                'ingredients': '• Белая рыба - 200г\n• Брокколи - 150г\n• Морковь - 100г\n• Лук - 50г',
                'instructions': '1. Запеките рыбу с овощами\n2. Приправьте специями\n3. Подавайте горячим'
            }
        }
        
        meal_data = meals_map.get(meal_type_name, {
            'name': 'Сбалансированное блюдо',
            'ingredients': '• Свежие продукты\n• Специи по вкусу',
            'instructions': '1. Подготовьте ингредиенты\n2. Приготовьте по рецепту\n3. Подавайте свежим'
        })
        
        return {
            'type': meal_type_name,
            'emoji': emoji,
            'name': meal_data['name'],
            'time': self._get_default_meal_time(meal_type_name),
            'calories': '350-450 ккал',
            'ingredients': meal_data['ingredients'],
            'instructions': meal_data['instructions'],
            'cooking_time': '15-25 минут'
        }
    
    def _get_default_meal_time(self, meal_type):
        """Возвращает время по умолчанию для приема пищи"""
        time_map = {
            'ЗАВТРАК': '8:00',
            'ПЕРЕКУС 1': '11:00',
            'ОБЕД': '13:00',
            'ПЕРЕКУС 2': '16:00',
            'УЖИН': '19:00'
        }
        return time_map.get(meal_type, '12:00')

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
            [InlineKeyboardButton("❓ ПОМОЩЬ", callback_data="help")]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def get_plan_data_input(self, step=1):
        """Клавиатура для ввода данных плана"""
        if step == 1:  # Выбор пола
            keyboard = [
                [InlineKeyboardButton("👨 МУЖЧИНА", callback_data="gender_male")],
                [InlineKeyboardButton("👩 ЖЕНЩИНА", callback_data="gender_female")],
                [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_main")]
            ]
        elif step == 2:  # Выбор цели
            keyboard = [
                [InlineKeyboardButton("🎯 ПОХУДЕНИЕ", callback_data="goal_weight_loss")],
                [InlineKeyboardButton("💪 НАБОР МАССЫ", callback_data="goal_mass")],
                [InlineKeyboardButton("⚖️ ПОДДЕРЖАНИЕ", callback_data="goal_maintain")],
                [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_gender")]
            ]
        elif step == 3:  # Выбор активности
            keyboard = [
                [InlineKeyboardButton("🏃‍♂️ ВЫСОКАЯ", callback_data="activity_high")],
                [InlineKeyboardButton("🚶‍♂️ СРЕДНЯЯ", callback_data="activity_medium")],
                [InlineKeyboardButton("💤 НИЗКАЯ", callback_data="activity_low")],
                [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_goal")]
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
    return jsonify({
        "status": "healthy", 
        "service": "nutrition-bot",
        "timestamp": datetime.now().isoformat(),
        "version": "1.0"
    })

# ==================== ОСНОВНОЙ КЛАСС БОТА ====================

class NutritionBot:
    def __init__(self):
        self.bot_token = os.getenv('BOT_TOKEN')
        if not self.bot_token:
            logger.error("❌ BOT_TOKEN not found")
            raise ValueError("BOT_TOKEN is required")
            
        init_database()
        
        try:
            self.application = Application.builder().token(self.bot_token).build()
            self.menu = InteractiveMenu()
            self.parser = GPTParser()
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
        logger.info(f"📨 Callback received: {data}")
        
        try:
            # Главные команды меню
            if data == "create_plan":
                await self._handle_create_plan(query, context)
            elif data == "checkin":
                await self._handle_checkin(query, context)
            elif data == "stats":
                await self._handle_stats(query, context)
            elif data == "help":
                await self._handle_help(query, context)
            
            # Навигация назад
            elif data == "back_main":
                await self._show_main_menu(query)
            elif data == "back_gender":
                await self._handle_create_plan(query, context)
            elif data == "back_goal":
                await self._handle_gender_back(query, context)
            
            # Ввод данных плана
            elif data.startswith("gender_"):
                await self._handle_gender(query, context, data)
            elif data.startswith("goal_"):
                await self._handle_goal(query, context, data)
            elif data.startswith("activity_"):
                await self._handle_activity(query, context, data)
            else:
                logger.warning(f"⚠️ Unknown callback data: {data}")
                await query.edit_message_text("❌ Неизвестная команда", reply_markup=self.menu.get_main_menu())
                
        except Exception as e:
            logger.error(f"❌ Error in callback handler: {e}")
            await query.edit_message_text(
                "❌ Произошла ошибка. Попробуйте снова.",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _handle_create_plan(self, query, context):
        """Обработчик создания плана"""
        try:
            user_id = query.from_user.id
            
            # Проверяем лимиты
            if not is_admin(user_id) and not can_make_request(user_id):
                days_remaining = get_days_until_next_plan(user_id)
                await query.edit_message_text(
                    f"⏳ Вы уже запрашивали план питания\nСледующий доступен через {days_remaining} дней",
                    reply_markup=self.menu.get_main_menu()
                )
                return
            
            # Инициализируем данные плана
            context.user_data['plan_data'] = {}
            context.user_data['plan_step'] = 1
            
            logger.info(f"🔧 Starting plan creation for user {user_id}")
            
            await query.edit_message_text(
                "📊 СОЗДАНИЕ ПЛАНА ПИТАНИЯ\n\n1️⃣ Выберите ваш пол:",
                reply_markup=self.menu.get_plan_data_input(step=1)
            )
            
        except Exception as e:
            logger.error(f"❌ Error in create plan handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка при создании плана. Попробуйте снова.",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _handle_gender_back(self, query, context):
        """Назад к выбору пола"""
        try:
            # Сохраняем текущие данные, но возвращаемся к выбору пола
            if 'plan_data' in context.user_data:
                # Очищаем только выбранный пол, сохраняем остальные данные
                if 'gender' in context.user_data['plan_data']:
                    del context.user_data['plan_data']['gender']
            
            context.user_data['plan_step'] = 1
            
            await query.edit_message_text(
                "📊 СОЗДАНИЕ ПЛАНА ПИТАНИЯ\n\n1️⃣ Выберите ваш пол:",
                reply_markup=self.menu.get_plan_data_input(step=1)
            )
        except Exception as e:
            logger.error(f"❌ Error in gender back handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка навигации. Попробуйте с начала.",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _handle_gender(self, query, context, data):
        """Обработчик выбора пола"""
        try:
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
            
        except Exception as e:
            logger.error(f"❌ Error in gender handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка при выборе пола. Попробуйте снова.",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _handle_goal(self, query, context, data):
        """Обработчик выбора цели"""
        try:
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
            
        except Exception as e:
            logger.error(f"❌ Error in goal handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка при выборе цели. Попробуйте снова.",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _handle_activity(self, query, context, data):
        """Обработчик выбора активности"""
        try:
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
                "Пример: 30, 180, 75"
            )
            
        except Exception as e:
            logger.error(f"❌ Error in activity handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка при выборе активности. Попробуйте снова.",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _handle_checkin(self, query, context):
        """Обработчик чек-ина"""
        try:
            await query.edit_message_text(
                "📈 Функция чек-ина в разработке...",
                reply_markup=self.menu.get_main_menu()
            )
        except Exception as e:
            logger.error(f"Error in checkin handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка при открытии чек-ина",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _handle_stats(self, query, context):
        """Обработчик статистики"""
        try:
            user_id = query.from_user.id
            stats = get_user_stats(user_id)
            
            if not stats:
                await query.edit_message_text(
                    "📊 У вас пока нет данных для статистики",
                    reply_markup=self.menu.get_main_menu()
                )
                return
            
            stats_text = "📊 ВАША СТАТИСТИКА (последние 7 дней):\n\n"
            for stat in stats:
                date, weight, waist, wellbeing, sleep = stat
                stats_text += f"📅 {date[:10]}: Вес {weight}кг, Талия {waist}см\n"
                stats_text += f"   Самочувствие: {wellbeing}/5, Сон: {sleep}/5\n\n"
            
            await query.edit_message_text(
                stats_text,
                reply_markup=self.menu.get_main_menu()
            )
            
        except Exception as e:
            logger.error(f"Error in stats handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка при получении статистики",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _handle_help(self, query, context):
        """Обработчик помощи"""
        help_text = """
❓ ПОМОЩЬ ПО БОТУ

📊 СОЗДАТЬ ПЛАН:
• Создает персонализированный план питания на 7 дней
• Учитывает ваш пол, цель, активность и параметры
• Доступен раз в 7 дней (админам - безлимитно)

📈 ЧЕК-ИН:
• Ежедневное отслеживание прогресса
• Запись веса, обхвата талии, самочувствия

📊 СТАТИСТИКА:
• Просмотр вашего прогресса
• Графики изменений параметров

💡 Советы:
• Вводите данные точно
• Следуйте плану питания
• Регулярно делайте чек-ин
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
        try:
            text = update.message.text
            user_id = update.effective_user.id
            
            if context.user_data.get('awaiting_input') == 'plan_details':
                await self._process_plan_details(update, context, text)
            else:
                await update.message.reply_text(
                    "🤖 Используйте меню для навигации",
                    reply_markup=self.menu.get_main_menu()
                )
                
        except Exception as e:
            logger.error(f"Error in message handler: {e}")
            await update.message.reply_text(
                "❌ Произошла ошибка. Попробуйте снова.",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _process_plan_details(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        """Обрабатывает детали плана"""
        try:
            parts = [part.strip() for part in text.split(',')]
            if len(parts) != 3:
                raise ValueError("Нужно ввести 3 числа через запятую")
            
            age, height, weight = int(parts[0]), int(parts[1]), float(parts[2])
            
            # Проверяем корректность данных
            if not (10 <= age <= 100):
                raise ValueError("Возраст должен быть от 10 до 100 лет")
            if not (100 <= height <= 250):
                raise ValueError("Рост должен быть от 100 до 250 см")
            if not (30 <= weight <= 300):
                raise ValueError("Вес должен быть от 30 до 300 кг")
            
            user_data = {
                **context.user_data['plan_data'],
                'age': age,
                'height': height,
                'weight': weight,
                'user_id': update.effective_user.id,
                'username': update.effective_user.username
            }
            
            logger.info(f"🎯 Generating plan for: {user_data}")
            
            processing_msg = await update.message.reply_text("🔄 Генерируем ваш AI-план питания...")
            
            # Генерируем план
            plan_data = await self._generate_plan_with_gpt(user_data)
            if plan_data:
                plan_id = save_plan(user_data['user_id'], plan_data)
                update_user_limit(user_data['user_id'])
                
                await processing_msg.delete()
                
                success_text = f"""
🎉 ВАШ ПЛАН ПИТАНИЯ ГОТОВ!

👤 Данные: {user_data['gender']}, {age} лет, {height} см, {weight} кг
🎯 Цель: {user_data['goal']}
🏃 Активность: {user_data['activity']}

📋 План включает:
• 7 дней питания
• 5 приемов пищи в день  
• Сбалансированное питание
• Рекомендации по воде

План сохранен в вашем профиле!
"""
                await update.message.reply_text(
                    success_text,
                    reply_markup=self.menu.get_main_menu()
                )
                
                # Логируем успешное создание
                logger.info(f"✅ Plan successfully created for user {user_data['user_id']}")
                
            else:
                await processing_msg.delete()
                await update.message.reply_text(
                    "❌ Не удалось сгенерировать план. Попробуйте позже.",
                    reply_markup=self.menu.get_main_menu()
                )
            
            # Очищаем временные данные ТОЛЬКО после успешного завершения
            context.user_data['awaiting_input'] = None
            context.user_data['plan_data'] = {}
            context.user_data['plan_step'] = None
            
        except ValueError as e:
            error_msg = str(e)
            if "Нужно ввести 3 числа" in error_msg:
                await update.message.reply_text(
                    "❌ Ошибка в формате данных. Используйте: Возраст, Рост, Вес\nПример: 30, 180, 80"
                )
            else:
                await update.message.reply_text(f"❌ {error_msg}")
        except Exception as e:
            logger.error(f"❌ Error processing plan details: {e}")
            await update.message.reply_text(
                "❌ Произошла ошибка при создании плана. Попробуйте снова.",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _generate_plan_with_gpt(self, user_data):
        """Генерирует план питания с помощью Yandex GPT"""
        try:
            prompt = self._create_prompt(user_data)
            
            headers = {
                "Authorization": f"Api-Key {YANDEX_API_KEY}",
                "Content-Type": "application/json"
            }
            
            data = {
                "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/latest",
                "completionOptions": {
                    "stream": False,
                    "temperature": 0.7,
                    "maxTokens": 4000
                },
                "messages": [
                    {
                        "role": "system",
                        "text": "Ты эксперт по питанию и диетологии. Создай подробный план питания на 7 дней."
                    },
                    {
                        "role": "user", 
                        "text": prompt
                    }
                ]
            }
            
            logger.info("🚀 Sending request to Yandex GPT...")
            response = requests.post(YANDEX_GPT_URL, headers=headers, json=data, timeout=60)
            
            if response.status_code == 200:
                result = response.json()
                gpt_response = result['result']['alternatives'][0]['message']['text']
                logger.info("✅ GPT response received successfully")
                
                # Парсим ответ
                structured_plan = self.parser.parse_plan_response(gpt_response, user_data)
                return structured_plan
            else:
                logger.error(f"❌ GPT API error: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Error generating plan with GPT: {e}")
            return None
    
    def _create_prompt(self, user_data):
        """Создает промпт для GPT на основе данных пользователя"""
        gender = user_data['gender']
        goal = user_data['goal']
        activity = user_data['activity']
        age = user_data['age']
        height = user_data['height']
        weight = user_data['weight']
        
        prompt = f"""
Создай подробный план питания на 7 дней для:

Пол: {gender}
Цель: {goal}
Уровень активности: {activity}
Возраст: {age} лет
Рост: {height} см
Вес: {weight} кг

Требования к плану:
1. 7 дней (ПОНЕДЕЛЬНИК - ВОСКРЕСЕНЬЕ)
2. 5 приемов пищи в день: ЗАВТРАК, ПЕРЕКУС 1, ОБЕД, ПЕРЕКУС 2, УЖИН
3. Для каждого приема пищи укажи:
   - Время приема (например, 8:00)
   - Название блюда
   - Калорийность в ккал
   - Ингредиенты с количествами
   - Простые инструкции приготовления
   - Время приготовления

4. В конце предоставь:
   - Общий список покупок на неделю
   - Рекомендации по водному режиму
   - Общие рекомендации по питанию

План должен быть сбалансированным, практичным и учитывать указанную цель ({goal}).
Используй доступные продукты, простые рецепты.

Форматируй ответ четко по дням и приемам пищи.
"""
        return prompt
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик ошибок"""
        logger.error(f"❌ Exception while handling update: {context.error}")
        
        try:
            if update and update.effective_message:
                await update.effective_message.reply_text(
                    "❌ Произошла непредвиденная ошибка. Попробуйте позже.",
                    reply_markup=self.menu.get_main_menu()
                )
        except Exception as e:
            logger.error(f"Error in error handler: {e}")

# ==================== ЗАПУСК БОТА ====================

def run_bot():
    """Запускает бота"""
    try:
        bot = NutritionBot()
        
        # Запуск Flask в отдельном потоке
        def run_flask():
            port = int(os.environ.get('PORT', 5000))
            app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
        
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        logger.info(f"🚀 Flask server started on port {os.environ.get('PORT', 5000)}")
        
        # Запуск бота
        logger.info("🤖 Starting bot polling...")
        bot.application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        logger.error(f"❌ Failed to start bot: {e}")
        sys.exit(1)

if __name__ == "__main__":
    run_bot()
