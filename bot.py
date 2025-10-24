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

ADMIN_USER_ID = 362423055
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
            [InlineKeyboardButton("🛒 КОРЗИНА ПОКУПОК", callback_data="shopping_cart")],
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
    
    def get_shopping_cart_menu(self):
        """Меню корзины покупок"""
        keyboard = [
            [InlineKeyboardButton("📄 СКАЧАТЬ СПИСОК", callback_data="download_shopping_list")],
            [InlineKeyboardButton("📅 ПРОСМОТРЕТЬ НЕДЕЛЮ", callback_data="view_week")],
            [InlineKeyboardButton("↩️ НАЗАД В МЕНЮ", callback_data="back_to_plan_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def get_back_menu(self):
        """Меню с кнопкой назад"""
        keyboard = [
            [InlineKeyboardButton("↩️ НАЗАД", callback_data="back_main")]
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
            self._setup_handlers()
            
            logger.info("✅ Bot initialized successfully")
        except Exception as e:
            logger.error(f"❌ Failed to initialize bot: {e}")
            raise
    
    def _setup_handlers(self):
        """Настройка обработчиков"""
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("menu", self.menu_command))
        self.application.add_handler(CommandHandler("dbstats", self.dbstats_command))
        self.application.add_handler(CommandHandler("export_plan", self.export_plan_command))
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
    
    async def dbstats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда для просмотра информации о БД (только для админа)"""
        try:
            user_id = update.effective_user.id
            if not is_admin(user_id):
                await update.message.reply_text("❌ Эта команда только для администратора")
                return
            
            conn = sqlite3.connect('nutrition_bot.db', check_same_thread=False)
            cursor = conn.cursor()
            
            # Получаем статистику
            cursor.execute("SELECT COUNT(*) FROM users")
            users_count = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM nutrition_plans")
            plans_count = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM daily_checkins")
            checkins_count = cursor.fetchone()[0]
            
            # Последние планы
            cursor.execute('''
                SELECT u.user_id, u.username, np.created_at 
                FROM nutrition_plans np 
                JOIN users u ON np.user_id = u.user_id 
                ORDER BY np.created_at DESC LIMIT 5
            ''')
            recent_plans = cursor.fetchall()
            
            # Размер БД
            db_size = os.path.getsize('nutrition_bot.db') if os.path.exists('nutrition_bot.db') else 0
            
            conn.close()
            
            stats_text = f"""
📊 СТАТИСТИКА БАЗЫ ДАННЫХ:

👥 Пользователей: {users_count}
📋 Планов питания: {plans_count}
📈 Чек-инов: {checkins_count}
💾 Размер БД: {db_size / 1024:.1f} KB

📅 Последние созданные планы:
"""
            for plan in recent_plans:
                user_id, username, created_at = plan
                username_display = f"@{username}" if username else "без username"
                stats_text += f"• ID: {user_id} ({username_display}) - {created_at[:10]}\n"
            
            await update.message.reply_text(stats_text)
            
        except Exception as e:
            logger.error(f"Error in db command: {e}")
            await update.message.reply_text("❌ Ошибка при получении статистики БД")
    
    async def export_plan_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда для экспорта плана в TXT"""
        try:
            user_id = update.effective_user.id
            await update.message.reply_text("📄 Подготавливаем ваш план для скачивания...")
            await self.send_plan_as_file(update, context, user_id)
            
        except Exception as e:
            logger.error(f"Error in export plan command: {e}")
            await update.message.reply_text("❌ Ошибка при подготовке плана для скачивания")
    
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
                await self._handle_checkin_menu(query, context)
            elif data == "stats":
                await self._handle_stats(query, context)
            elif data == "my_plan":
                await self._handle_my_plan_menu(query, context)
            elif data == "help":
                await self._handle_help(query, context)
            elif data == "plan_info":
                await self._handle_plan_info(query, context)
            elif data == "download_plan":
                await self._handle_download_plan(query, context)
            elif data == "view_week":
                await self._handle_view_week(query, context)
            elif data == "shopping_cart":
                await self._handle_shopping_cart(query, context)
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
            
            # Просмотр недели и приемов пищи
            elif data.startswith("day_"):
                await self._handle_day_selection(query, context, data)
            elif data.startswith("meal_"):
                await self._handle_meal_selection(query, context, data)
            elif data.startswith("next_meal_"):
                await self._handle_next_meal(query, context, data)
            
            else:
                logger.warning(f"⚠️ Unknown callback data: {data}")
                await query.edit_message_text(
                    "❌ Неизвестная команда",
                    reply_markup=self.menu.get_main_menu()
                )
                
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
    
    async def _handle_goal_back(self, query, context):
        """Назад к выбору цели"""
        try:
            context.user_data['plan_step'] = 2
            
            await query.edit_message_text(
                "📊 СОЗДАНИЕ ПЛАНА ПИТАНИЯ\n\n2️⃣ Выберите вашу цель:",
                reply_markup=self.menu.get_plan_data_input(step=2)
            )
        except Exception as e:
            logger.error(f"❌ Error in goal back handler: {e}")
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
                "Пример: 30, 180, 75\n\n"
                "Для отмены нажмите /menu",
                reply_markup=self.menu.get_back_menu()
            )
            
        except Exception as e:
            logger.error(f"❌ Error in activity handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка при выборе активности. Попробуйте снова.",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _handle_checkin_menu(self, query, context):
        """Обработчик меню чек-ина"""
        try:
            await query.edit_message_text(
                "📈 ЕЖЕДНЕВНЫЙ ЧЕК-ИН\n\n"
                "Отслеживайте ваш прогресс:\n"
                "• Вес\n"
                "• Обхват талии\n"
                "• Самочувствие\n"
                "• Качество сна\n\n"
                "Выберите действие:",
                reply_markup=self.menu.get_checkin_menu()
            )
        except Exception as e:
            logger.error(f"Error in checkin menu handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка при открытии чек-ина",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _handle_checkin_data(self, query, context):
        """Обработчик ввода данных чек-ина"""
        try:
            context.user_data['awaiting_input'] = 'checkin_data'
            
            await query.edit_message_text(
                "📝 ВВЕДИТЕ ДАННЫЕ ЧЕК-ИНА\n\n"
                "Введите данные в формате:\n"
                "Вес (кг), Обхват талии (см), Самочувствие (1-5), Сон (1-5)\n\n"
                "Пример: 75.5, 85, 4, 3\n\n"
                "📊 Шкала оценок:\n"
                "• Самочувствие: 1(плохо) - 5(отлично)\n"
                "• Сон: 1(бессонница) - 5(отлично выспался)\n\n"
                "Для отмены нажмите /menu"
            )
            
        except Exception as e:
            logger.error(f"Error in checkin data handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка при вводе данных чек-ина",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _handle_checkin_history(self, query, context):
        """Обработчик истории чек-инов"""
        try:
            user_id = query.from_user.id
            stats = get_user_stats(user_id)
            
            if not stats:
                await query.edit_message_text(
                    "📊 У вас пока нет данных чек-инов\n\n"
                    "Начните отслеживать свой прогресс!",
                    reply_markup=self.menu.get_checkin_menu()
                )
                return
            
            stats_text = "📊 ИСТОРИЯ ВАШИХ ЧЕК-ИНОВ:\n\n"
            for stat in stats:
                date, weight, waist, wellbeing, sleep = stat
                stats_text += f"📅 {date[:10]}\n"
                stats_text += f"⚖️ Вес: {weight} кг\n"
                stats_text += f"📏 Талия: {waist} см\n"
                stats_text += f"😊 Самочувствие: {wellbeing}/5\n"
                stats_text += f"😴 Сон: {sleep}/5\n\n"
            
            await query.edit_message_text(
                stats_text,
                reply_markup=self.menu.get_checkin_menu()
            )
            
        except Exception as e:
            logger.error(f"Error in checkin history handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка при получении истории чек-инов",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _handle_stats(self, query, context):
        """Обработчик статистики"""
        try:
            user_id = query.from_user.id
            stats = get_user_stats(user_id)
            
            if not stats:
                await query.edit_message_text(
                    "📊 У вас пока нет данных для статистики\n\n"
                    "Начните с ежедневных чек-инов!",
                    reply_markup=self.menu.get_main_menu()
                )
                return
            
            # Анализ прогресса
            if len(stats) >= 2:
                latest_weight = stats[0][1]
                oldest_weight = stats[-1][1]
                weight_diff = latest_weight - oldest_weight
                
                progress_text = ""
                if weight_diff < 0:
                    progress_text = f"📉 Потеря веса: {abs(weight_diff):.1f} кг"
                elif weight_diff > 0:
                    progress_text = f"📈 Набор веса: {weight_diff:.1f} кг"
                else:
                    progress_text = "⚖️ Вес стабилен"
            else:
                progress_text = "📈 Записей пока мало для анализа прогресса"
            
            stats_text = f"📊 ВАША СТАТИСТИКА\n\n{progress_text}\n\n"
            stats_text += "Последние записи:\n"
            
            for i, stat in enumerate(stats[:5]):
                date, weight, waist, wellbeing, sleep = stat
                stats_text += f"📅 {date[:10]}: {weight} кг, талия {waist} см\n"
            
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
    
    async def _handle_my_plan_menu(self, query, context):
        """Обработчик меню моего плана"""
        try:
            user_id = query.from_user.id
            plan = get_latest_plan(user_id)
            
            if not plan:
                await query.edit_message_text(
                    "📋 У вас пока нет созданных планов питания\n\n"
                    "Создайте ваш первый персональный план!",
                    reply_markup=self.menu.get_main_menu()
                )
                return
            
            user_data = plan.get('user_data', {})
            menu_text = f"📋 УПРАВЛЕНИЕ ПЛАНОМ ПИТАНИЯ\n\n"
            menu_text += f"👤 {user_data.get('gender', '')}, {user_data.get('age', '')} лет\n"
            menu_text += f"📏 {user_data.get('height', '')} см, {user_data.get('weight', '')} кг\n"
            menu_text += f"🎯 Цель: {user_data.get('goal', '')}\n"
            menu_text += f"🏃 Активность: {user_data.get('activity', '')}\n\n"
            menu_text += "Выберите действие:"
            
            await query.edit_message_text(
                menu_text,
                reply_markup=self.menu.get_plan_management_menu()
            )
        except Exception as e:
            logger.error(f"Error in my plan menu handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка при открытии меню плана",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _handle_plan_info(self, query, context):
        """Обработчик информации о планах"""
        try:
            user_id = query.from_user.id
            plans_count = get_user_plans_count(user_id)
            days_remaining = get_days_until_next_plan(user_id)
            
            info_text = f"📊 ИНФОРМАЦИЯ О ВАШИХ ПЛАНАХ\n\n"
            info_text += f"📋 Создано планов: {plans_count}\n"
            
            if is_admin(user_id):
                info_text += "👑 Статус: АДМИНИСТРАТОР (безлимитный доступ)\n"
            else:
                if days_remaining > 0:
                    info_text += f"⏳ Следующий план через: {days_remaining} дней\n"
                else:
                    info_text += "✅ Можете создать новый план!\n"
            
            info_text += "\n💡 Лимиты:\n"
            info_text += "• 1 план в 7 дней для обычных пользователей\n"
            info_text += "• Безлимитный доступ для администратора\n"
            
            await query.edit_message_text(
                info_text,
                reply_markup=self.menu.get_plan_management_menu()
            )
            
        except Exception as e:
            logger.error(f"Error in plan info handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка при получении информации о планах",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _handle_download_plan(self, query, context):
        """Обработчик скачивания плана"""
        try:
            user_id = query.from_user.id
            await self.send_plan_as_file(query, context, user_id)
            
        except Exception as e:
            logger.error(f"Error in download plan handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка при подготовке плана для скачивания",
                reply_markup=self.menu.get_plan_management_menu()
            )
    
    async def _handle_view_week(self, query, context):
        """Обработчик просмотра недели"""
        try:
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
            
            # Показываем краткую информацию по каждому дню
            for i, day in enumerate(plan.get('days', [])):
                week_text += f"📅 {day['name']}\n"
                week_text += f"🔥 {day.get('total_calories', '~1800 ккал')}\n"
                
                # Показываем первый прием пищи как пример
                if day.get('meals'):
                    first_meal = day['meals'][0]
                    week_text += f"🍽 {first_meal['name']}\n"
                
                week_text += "\n"
            
            await query.edit_message_text(
                week_text,
                reply_markup=self.menu.get_week_days_menu()
            )
            
        except Exception as e:
            logger.error(f"Error in view week handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка при загрузке плана на неделю",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _handle_day_selection(self, query, context, data):
        """Обработчик выбора дня"""
        try:
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
            
        except Exception as e:
            logger.error(f"Error in day selection handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка при загрузке дня",
                reply_markup=self.menu.get_week_days_menu()
            )
    
    async def _handle_meal_selection(self, query, context, data):
        """Обработчик выбора приема пищи"""
        try:
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
            
        except Exception as e:
            logger.error(f"Error in meal selection handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка при загрузке приема пищи",
                reply_markup=self.menu.get_week_days_menu()
            )
    
    async def _handle_next_meal(self, query, context, data):
        """Обработчик перехода к следующему приему пищи"""
        try:
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
            
            # Определяем следующий прием пищи
            next_meal_index = meal_index + 1
            next_day_index = day_index
            
            # Если это последний прием пищи дня, переходим к следующему дню
            if next_meal_index >= len(plan['days'][day_index].get('meals', [])):
                next_meal_index = 0
                next_day_index += 1
            
            # Если это последний день, возвращаемся к первому
            if next_day_index >= len(plan.get('days', [])):
                next_day_index = 0
            
            # Создаем callback данные для следующего приема пищи
            next_callback = f"meal_{next_day_index}_{next_meal_index}"
            await self._handle_meal_selection(query, context, next_callback)
            
        except Exception as e:
            logger.error(f"Error in next meal handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка при переходе к следующему приему пищи",
                reply_markup=self.menu.get_week_days_menu()
            )
    
    async def _handle_shopping_cart(self, query, context):
        """Обработчик корзины покупок"""
        try:
            user_id = query.from_user.id
            plan = get_latest_plan(user_id)
            
            if not plan:
                await query.edit_message_text(
                    "❌ У вас нет активного плана питания",
                    reply_markup=self.menu.get_main_menu()
                )
                return
            
            shopping_list = self._generate_shopping_list(plan)
            
            cart_text = "🛒 КОРЗИНА ПОКУПОК НА НЕДЕЛЮ\n\n"
            cart_text += "📋 Список продуктов:\n\n"
            cart_text += shopping_list
            
            await query.edit_message_text(
                cart_text,
                reply_markup=self.menu.get_shopping_cart_menu()
            )
            
        except Exception as e:
            logger.error(f"Error in shopping cart handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка при загрузке корзины покупок",
                reply_markup=self.menu.get_main_menu()
            )
    
    def _generate_shopping_list(self, plan):
        """Генерирует список покупок на основе плана"""
        try:
            # Собираем все ингредиенты из всех приемов пищи за неделю
            all_ingredients = []
            
            for day in plan.get('days', []):
                for meal in day.get('meals', []):
                    ingredients = meal.get('ingredients', '')
                    # Разбиваем на отдельные ингредиенты
                    lines = ingredients.split('\n')
                    for line in lines:
                        line = line.strip()
                        if line and line.startswith('•'):
                            all_ingredients.append(line[1:].strip())
            
            # Убираем дубликаты и сортируем
            unique_ingredients = sorted(list(set(all_ingredients)))
            
            if not unique_ingredients:
                return "• Куриная грудка - 700г\n• Рыба белая - 600г\n• Овощи сезонные - 2кг\n• Фрукты - 1.5кг\n• Крупы - 1кг\n• Яйца - 10шт\n• Молочные продукты - 1кг"
            
            return '\n'.join([f"• {ingredient}" for ingredient in unique_ingredients[:20]])  # Ограничиваем список
            
        except Exception as e:
            logger.error(f"Error generating shopping list: {e}")
            return "• Куриная грудка - 700г\n• Рыба белая - 600г\n• Овощи сезонные - 2кг\n• Фрукты - 1.5кг\n• Крупы - 1кг\n• Яйца - 10шт\n• Молочные продукты - 1кг"
    
    async def _handle_download_shopping_list(self, query, context):
        """Обработчик скачивания списка покупок"""
        try:
            user_id = query.from_user.id
            plan = get_latest_plan(user_id)
            
            if not plan:
                await query.edit_message_text(
                    "❌ У вас нет активного плана питания",
                    reply_markup=self.menu.get_main_menu()
                )
                return
            
            shopping_list = self._generate_shopping_list(plan)
            filename = f"shopping_list_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            
            with open(filename, 'w', encoding='utf-8') as f:
                f.write("🛒 СПИСОК ПОКУПОК НА НЕДЕЛЮ\n\n")
                f.write("📋 Продукты:\n\n")
                f.write(shopping_list)
                f.write("\n\n💡 Советы:\n")
                f.write("• Покупайте свежие продукты\n• Проверяйте сроки годности\n• Планируйте покупки на неделю\n")
            
            with open(filename, 'rb') as file:
                await context.bot.send_document(
                    chat_id=user_id,
                    document=file,
                    filename=f"Список_покупок_{user_id}.txt",
                    caption="📄 Ваш список покупок на неделю"
                )
            
            await query.edit_message_text(
                "✅ Список покупок отправлен в виде файла!",
                reply_markup=self.menu.get_shopping_cart_menu()
            )
            
            import os
            os.remove(filename)
            
        except Exception as e:
            logger.error(f"Error in download shopping list handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка при создании списка покупок",
                reply_markup=self.menu.get_shopping_cart_menu()
            )
    
    async def _handle_help(self, query, context):
        """Обработчик помощи"""
        help_text = """
🤖 СПРАВКА ПО БОТУ ПИТАНИЯ

📊 СОЗДАТЬ ПЛАН:
• Создайте персонализированный план питания
• Учитывает пол, цель, активность и параметры
• 1 план в 7 дней для обычных пользователей

📈 ЧЕК-ИН:
• Ежедневно отслеживайте прогресс
• Вес, обхват талии, самочувствие, сон
• Просматривайте историю и статистику

📋 МОЙ ПЛАН:
• Просматривайте план на неделю
• Смотрите детали по дням и приемам пищи
• Получайте список покупок
• Скачивайте план в текстовом файле

💡 СОВЕТЫ:
• Регулярно вносите данные чек-ина
• Следуйте плану питания
• Пейте достаточное количество воды
• Сочетайте питание с физической активностью

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
                
                # Очищаем состояние ожидания
                context.user_data.pop('awaiting_input', None)
            else:
                await update.message.reply_text(
                    "🤖 Используйте меню для навигации или /start для начала",
                    reply_markup=self.menu.get_main_menu()
                )
                
        except Exception as e:
            logger.error(f"Error in message handler: {e}")
            await update.message.reply_text(
                "❌ Произошла ошибка. Попробуйте снова.",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _process_plan_details(self, update, context, text):
        """Обрабатывает ввод деталей плана"""
        try:
            # Парсим введенные данные
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
            
            # Сохраняем данные
            context.user_data['plan_data'].update({
                'age': age,
                'height': height,
                'weight': weight
            })
            
            await update.message.reply_text("🔄 Создаем ваш персональный план питания...")
            
            # Генерируем план
            plan = await self._generate_nutrition_plan(context.user_data['plan_data'])
            
            if plan:
                # Сохраняем план в БД
                plan_id = save_plan(update.effective_user.id, plan)
                update_user_limit(update.effective_user.id)
                
                if plan_id:
                    await update.message.reply_text(
                        "✅ Ваш персональный план питания готов!\n\n"
                        "Используйте меню 'Мой план' для просмотра деталей.",
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
            
            # Проверяем диапазоны
            if not (1 <= wellbeing <= 5) or not (1 <= sleep <= 5):
                await update.message.reply_text(
                    "❌ Оценки должны быть от 1 до 5\nПример: 75.5, 85, 4, 3"
                )
                return
            
            # Сохраняем чек-ин
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
            logger.error(f"Error processing checkin data: {e}")
            await update.message.reply_text(
                "❌ Ошибка при сохранении данных. Попробуйте снова.",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _generate_nutrition_plan(self, user_data):
        """Генерирует план питания"""
        try:
            # Если API ключи не настроены, используем демо-данные
            if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
                return self._generate_demo_plan(user_data)
            
            # Здесь будет интеграция с Yandex GPT API
            # Пока используем демо-данные
            return self._generate_demo_plan(user_data)
            
        except Exception as e:
            logger.error(f"Error generating nutrition plan: {e}")
            return self._generate_demo_plan(user_data)
    
    def _generate_demo_plan(self, user_data):
        """Генерирует демо-план питания"""
        days = ['ПОНЕДЕЛЬНИК', 'ВТОРНИК', 'СРЕДА', 'ЧЕТВЕРГ', 'ПЯТНИЦА', 'СУББОТА', 'ВОСКРЕСЕНЬЕ']
        meals_structure = [
            {'type': 'ЗАВТРАК', 'time': '08:00', 'emoji': '🍳'},
            {'type': 'ПЕРЕКУС 1', 'time': '11:00', 'emoji': '🥗'},
            {'type': 'ОБЕД', 'time': '14:00', 'emoji': '🍲'},
            {'type': 'ПЕРЕКУС 2', 'time': '17:00', 'emoji': '🍎'},
            {'type': 'УЖИН', 'time': '20:00', 'emoji': '🍛'}
        ]
        
        demo_meals = [
            {
                'name': 'Овсянка с ягодами и орехами',
                'calories': '350 ккал',
                'ingredients': '• Овсяные хлопья - 50г\n• Молоко - 200мл\n• Ягоды свежие - 100г\n• Орехи грецкие - 20г\n• Мед - 1 ч.л.',
                'instructions': '1. Сварите овсянку на молоке\n2. Добавьте ягоды и орехи\n3. Полейте медом',
                'cooking_time': '15 мин'
            },
            {
                'name': 'Творог с фруктами',
                'calories': '200 ккал',
                'ingredients': '• Творог обезжиренный - 150г\n• Яблоко - 1 шт\n• Корица - щепотка',
                'instructions': '1. Нарежьте яблоко кубиками\n2. Смешайте с творогом\n3. Посыпьте корицей',
                'cooking_time': '5 мин'
            },
            {
                'name': 'Куриная грудка с гречкой и овощами',
                'calories': '450 ккал',
                'ingredients': '• Куриная грудка - 150г\n• Гречка - 100г\n• Овощи замороженные - 200г\n• Масло оливковое - 1 ст.л.',
                'instructions': '1. Отварите гречку\n2. Обжарьте куриную грудку\n3. Потушите овощи\n4. Подавайте вместе',
                'cooking_time': '25 мин'
            },
            {
                'name': 'Йогурт с орехами',
                'calories': '180 ккал',
                'ingredients': '• Греческий йогурт - 150г\n• Миндаль - 30г\n• Ягоды сушеные - 20г',
                'instructions': '1. Смешайте йогурт с орехами\n2. Добавьте сушеные ягоды',
                'cooking_time': '2 мин'
            },
            {
                'name': 'Рыба на пару с брокколи',
                'calories': '400 ккал',
                'ingredients': '• Филе белой рыбы - 200г\n• Брокколи - 200г\n• Лимон - 1 долька\n• Специи по вкусу',
                'instructions': '1. Приготовьте рыбу на пару\n2. Отварите брокколи\n3. Подавайте с лимоном',
                'cooking_time': '20 мин'
            }
        ]
        
        plan = {
            'user_data': user_data,
            'days': []
        }
        
        for i, day_name in enumerate(days):
            day_plan = {
                'name': day_name,
                'total_calories': '~1800 ккал',
                'meals': []
            }
            
            for j, meal_struct in enumerate(meals_structure):
                meal = demo_meals[j].copy()
                meal.update(meal_struct)
                day_plan['meals'].append(meal)
            
            plan['days'].append(day_plan)
        
        return plan
    
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
                f.write("🍎 ПЕРСОНАЛЬНЫЙ ПЛАН ПИТАНИЯ\n\n")
                
                # Информация о пользователе
                user_data = plan.get('user_data', {})
                f.write("👤 ВАШИ ДАННЫЕ:\n")
                f.write(f"Пол: {user_data.get('gender', '')}\n")
                f.write(f"Возраст: {user_data.get('age', '')} лет\n")
                f.write(f"Рост: {user_data.get('height', '')} см\n")
                f.write(f"Вес: {user_data.get('weight', '')} кг\n")
                f.write(f"Цель: {user_data.get('goal', '')}\n")
                f.write(f"Активность: {user_data.get('activity', '')}\n\n")
                
                # План на неделю
                f.write("📅 ПЛАН НА НЕДЕЛЮ:\n\n")
                
                for day in plan.get('days', []):
                    f.write(f"=== {day['name']} ===\n")
                    f.write(f"Общая калорийность: {day.get('total_calories', '~1800 ккал')}\n\n")
                    
                    for meal in day.get('meals', []):
                        f.write(f"{meal['emoji']} {meal['type']} ({meal['time']})\n")
                        f.write(f"Блюдо: {meal['name']}\n")
                        f.write(f"Калории: {meal['calories']}\n")
                        f.write(f"Время приготовления: {meal['cooking_time']}\n")
                        f.write("Ингредиенты:\n")
                        f.write(f"{meal['ingredients']}\n")
                        f.write("Приготовление:\n")
                        f.write(f"{meal['instructions']}\n")
                        f.write("-" * 40 + "\n\n")
            
            # Отправляем файл
            with open(filename, 'rb') as file:
                if hasattr(update, 'message'):
                    await update.message.reply_document(
                        document=file,
                        filename=f"План_питания_{user_id}.txt",
                        caption="📄 Ваш персональный план питания"
                    )
                else:
                    await context.bot.send_document(
                        chat_id=user_id,
                        document=file,
                        filename=f"План_питания_{user_id}.txt",
                        caption="📄 Ваш персональный план питания"
                    )
            
            # Удаляем временный файл
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

def run_bot():
    """Запускает бота"""
    try:
        # Создаем экземпляр бота
        bot = NutritionBot()
        
        # Запускаем Flask в отдельном потоке
        def run_flask():
            app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
        
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        logger.info("✅ Flask server started on port 5000")
        
        # Запускаем бота
        logger.info("✅ Starting bot polling...")
        bot.application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        logger.error(f"❌ Failed to start bot: {e}")
        sys.exit(1)

if __name__ == '__main__':
    run_bot()
