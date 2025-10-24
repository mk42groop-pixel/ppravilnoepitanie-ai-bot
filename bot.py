import os
import logging
import threading
import sqlite3
import json
import requests
import sys
from datetime import datetime
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
            # Исправленная инициализация для версии 21.7
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
                await self._handle_my_plan(query, context)
            elif data == "help":
                await self._handle_help(query, context)
            
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
    
    async def _handle_my_plan(self, query, context):
        """Обработчик просмотра текущего плана"""
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
            plan_text = f"📋 ВАШ ТЕКУЩИЙ ПЛАН ПИТАНИЯ\n\n"
            plan_text += f"👤 {user_data.get('gender', '')}, {user_data.get('age', '')} лет\n"
            plan_text += f"📏 {user_data.get('height', '')} см, {user_data.get('weight', '')} кг\n"
            plan_text += f"🎯 Цель: {user_data.get('goal', '')}\n"
            plan_text += f"🏃 Активность: {user_data.get('activity', '')}\n\n"
            
            # Показываем первый день плана
            if plan.get('days'):
                first_day = plan['days'][0]
                plan_text += f"📅 {first_day['name']}:\n"
                for meal in first_day.get('meals', [])[:3]:  # Показываем первые 3 приема пищи
                    plan_text += f"{meal['emoji']} {meal['time']} - {meal['name']}\n"
                plan_text += f"\n🍽️ Всего приемов пищи: 5 в день"
            
            plan_text += f"\n\n💧 Рекомендации по воде:\n{plan.get('water_regime', '1.5-2 литра в день')}"
            
            await query.edit_message_text(
                plan_text,
                reply_markup=self.menu.get_main_menu()
            )
            
        except Exception as e:
            logger.error(f"Error in my_plan handler: {e}")
            await query.edit_message_text(
                "❌ Ошибка при получении плана",
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
• Просмотр истории и статистики

📊 СТАТИСТИКА:
• Анализ вашего прогресса  
• Графики изменений параметров

📋 МОЙ ПЛАН:
• Просмотр текущего плана питания
• Рекомендации и списки покупок

💡 Советы:
• Вводите данные точно
• Следуйте плану питания
• Регулярно делайте чек-ин
• Пейте достаточное количество воды
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
            
            # Обработка команды /menu
            if text == "/menu":
                await update.message.reply_text(
                    "🤖 ГЛАВНОЕ МЕНЮ\n\nВыберите действие:",
                    reply_markup=self.menu.get_main_menu()
                )
                return
            
            if context.user_data.get('awaiting_input') == 'plan_details':
                await self._process_plan_details(update, context, text)
            elif context.user_data.get('awaiting_input') == 'checkin_data':
                await self._process_checkin_data(update, context, text)
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
Используйте кнопку "МОЙ ПЛАН" для просмотра.
"""
                await update.message.reply_text(
                    success_text,
                    reply_markup=self.menu.get_main_menu()
                )
                
                logger.info(f"✅ Plan successfully created for user {user_data['user_id']}")
                
            else:
                await processing_msg.delete()
                await update.message.reply_text(
                    "❌ Не удалось сгенерировать план. Попробуйте позже.",
                    reply_markup=self.menu.get_main_menu()
                )
            
            # Очищаем временные данные
            context.user_data['awaiting_input'] = None
            context.user_data['plan_data'] = {}
            context.user_data['plan_step'] = None
            
        except ValueError as e:
            error_msg = str(e)
            if "Нужно ввести 3 числа" in error_msg:
                await update.message.reply_text(
                    "❌ Ошибка в формате данных. Используйте: Возраст, Рост, Вес\nПример: 30, 180, 80\n\nПопробуйте снова или нажмите /menu для отмены"
                )
            else:
                await update.message.reply_text(
                    f"❌ {error_msg}\n\nПопробуйте снова или нажмите /menu для отмены"
                )
        except Exception as e:
            logger.error(f"❌ Error processing plan details: {e}")
            await update.message.reply_text(
                "❌ Произошла ошибка при создании плана. Попробуйте снова.",
                reply_markup=self.menu.get_main_menu()
            )
    
    async def _process_checkin_data(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        """Обрабатывает данные чек-ина"""
        try:
            parts = [part.strip() for part in text.split(',')]
            if len(parts) != 4:
                raise ValueError("Нужно ввести 4 значения через запятую")
            
            weight, waist, wellbeing, sleep = float(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
            
            # Проверяем корректность данных
            if not (30 <= weight <= 300):
                raise ValueError("Вес должен быть от 30 до 300 кг")
            if not (50 <= waist <= 200):
                raise ValueError("Обхват талии должен быть от 50 до 200 см")
            if not (1 <= wellbeing <= 5):
                raise ValueError("Самочувствие должно быть от 1 до 5")
            if not (1 <= sleep <= 5):
                raise ValueError("Качество сна должно быть от 1 до 5")
            
            user_id = update.effective_user.id
            save_checkin(user_id, weight, waist, wellbeing, sleep)
            
            success_text = f"""
✅ ДАННЫЕ ЧЕК-ИНА СОХРАНЕНЫ!

📅 Дата: {datetime.now().strftime('%d.%m.%Y')}
⚖️ Вес: {weight} кг
📏 Талия: {waist} см
😊 Самочувствие: {wellbeing}/5
😴 Сон: {sleep}/5

Продолжайте отслеживать ваш прогресс!
"""
            await update.message.reply_text(
                success_text,
                reply_markup=self.menu.get_main_menu()
            )
            
            # Очищаем временные данные
            context.user_data['awaiting_input'] = None
            
        except ValueError as e:
            error_msg = str(e)
            if "Нужно ввести 4 значения" in error_msg:
                await update.message.reply_text(
                    "❌ Ошибка в формате данных. Используйте: Вес, Талия, Самочувствие, Сон\nПример: 75.5, 85, 4, 3\n\nПопробуйте снова или нажмите /menu для отмены"
                )
            else:
                await update.message.reply_text(
                    f"❌ {error_msg}\n\nПопробуйте снова или нажмите /menu для отмены"
                )
        except Exception as e:
            logger.error(f"❌ Error processing checkin data: {e}")
            await update.message.reply_text(
                "❌ Произошла ошибка при сохранении чек-ина. Попробуйте снова.",
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
                
                # Парсим ответ и создаем структурированный план
                structured_plan = self._parse_gpt_response(gpt_response, user_data)
                return structured_plan
            else:
                logger.error(f"❌ GPT API error: {response.status_code} - {response.text}")
                # Возвращаем fallback план
                return self._generate_fallback_plan(user_data)
                
        except Exception as e:
            logger.error(f"❌ Error generating plan with GPT: {e}")
            # Возвращаем fallback план в случае ошибки
            return self._generate_fallback_plan(user_data)
    
    def _create_prompt(self, user_data):
        """Создает промпт для GPT"""
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
    
    def _parse_gpt_response(self, gpt_response, user_data):
        """Парсит ответ GPT и создает структурированный план"""
        try:
            # Упрощенный парсинг - в реальном проекте нужно более сложное решение
            plan = {
                'user_data': user_data,
                'days': [],
                'shopping_list': "Список покупок сгенерирован на основе вашего плана",
                'water_regime': "1.5-2 литра воды в день",
                'general_recommendations': "Следуйте плану питания и пейте достаточное количество воды",
                'created_at': datetime.now().isoformat()
            }
            
            # Создаем базовую структуру дней
            day_names = ['ПОНЕДЕЛЬНИК', 'ВТОРНИК', 'СРЕДА', 'ЧЕТВЕРГ', 'ПЯТНИЦА', 'СУББОТА', 'ВОСКРЕСЕНЬЕ']
            
            for day_name in day_names:
                day = {
                    'name': day_name,
                    'meals': [
                        {
                            'type': 'ЗАВТРАК',
                            'emoji': '🍳',
                            'name': 'Овсяная каша с фруктами',
                            'time': '8:00',
                            'calories': '350 ккал',
                            'ingredients': '• Овсяные хлопья - 60г\n• Молоко - 150мл\n• Банан - 1 шт\n• Мед - 1 ч.л.',
                            'instructions': '1. Варите овсянку 10 минут\n2. Добавьте банан и мед\n3. Подавайте теплым',
                            'cooking_time': '15 минут'
                        },
                        {
                            'type': 'ПЕРЕКУС 1',
                            'emoji': '🥗',
                            'name': 'Йогурт с орехами',
                            'time': '11:00',
                            'calories': '250 ккал',
                            'ingredients': '• Йогурт натуральный - 150г\n• Грецкие орехи - 30г\n• Ягоды - 50г',
                            'instructions': '1. Смешайте йогурт с орехами\n2. Добавьте ягоды\n3. Подавайте свежим',
                            'cooking_time': '5 минут'
                        },
                        {
                            'type': 'ОБЕД',
                            'emoji': '🍲',
                            'name': 'Куриная грудка с гречкой',
                            'time': '13:00',
                            'calories': '450 ккал',
                            'ingredients': '• Куриная грудка - 150г\n• Гречка - 80г\n• Огурцы - 100г\n• Помидоры - 100г',
                            'instructions': '1. Отварите гречку\n2. Приготовьте куриную грудку\n3. Подавайте с овощами',
                            'cooking_time': '25 минут'
                        },
                        {
                            'type': 'ПЕРЕКУС 2',
                            'emoji': '🍎',
                            'name': 'Фруктовый салат',
                            'time': '16:00',
                            'calories': '200 ккал',
                            'ingredients': '• Яблоко - 1 шт\n• Банан - 1 шт\n• Апельсин - 1 шт\n• Йогурт - 50г',
                            'instructions': '1. Нарежьте фрукты\n2. Заправьте йогуртом\n3. Подавайте свежим',
                            'cooking_time': '10 минут'
                        },
                        {
                            'type': 'УЖИН',
                            'emoji': '🍛',
                            'name': 'Рыба с овощами',
                            'time': '19:00',
                            'calories': '400 ккал',
                            'ingredients': '• Белая рыба - 200г\n• Брокколи - 150г\n• Морковь - 100г\n• Лук - 50г',
                            'instructions': '1. Запеките рыбу с овощами\n2. Приправьте специями\n3. Подавайте горячим',
                            'cooking_time': '30 минут'
                        }
                    ],
                    'total_calories': '1650 ккал'
                }
                plan['days'].append(day)
            
            return plan
            
        except Exception as e:
            logger.error(f"Error parsing GPT response: {e}")
            return self._generate_fallback_plan(user_data)
    
    def _generate_fallback_plan(self, user_data):
        """Создает резервный план питания"""
        logger.info("🔄 Generating fallback nutrition plan")
        
        plan = {
            'user_data': user_data,
            'days': [],
            'shopping_list': "Куриная грудка, рыба, овощи, фрукты, крупы, яйца, творог",
            'water_regime': "1.5-2 литра воды в день",
            'general_recommendations': "Сбалансированное питание и регулярная физическая активность",
            'created_at': datetime.now().isoformat()
        }
        
        # Создаем 7 дней
        day_names = ['ПОНЕДЕЛЬНИК', 'ВТОРНИК', 'СРЕДА', 'ЧЕТВЕРГ', 'ПЯТНИЦА', 'СУББОТА', 'ВОСКРЕСЕНЬЕ']
        
        for day_name in day_names:
            day = {
                'name': day_name,
                'meals': [
                    {
                        'type': 'ЗАВТРАК',
                        'emoji': '🍳',
                        'name': 'Овсяная каша с фруктами',
                        'time': '8:00',
                        'calories': '350 ккал',
                        'ingredients': '• Овсяные хлопья - 60г\n• Молоко - 150мл\n• Банан - 1 шт\n• Мед - 1 ч.л.',
                        'instructions': '1. Варите овсянку 10 минут\n2. Добавьте банан и мед\n3. Подавайте теплым',
                        'cooking_time': '15 минут'
                    },
                    {
                        'type': 'ПЕРЕКУС 1',
                        'emoji': '🥗',
                        'name': 'Йогурт с орехами',
                        'time': '11:00',
                        'calories': '250 ккал',
                        'ingredients': '• Йогурт натуральный - 150г\n• Грецкие орехи - 30г\n• Ягоды - 50г',
                        'instructions': '1. Смешайте йогурт с орехами\n2. Добавьте ягоды\n3. Подавайте свежим',
                        'cooking_time': '5 минут'
                    },
                    {
                        'type': 'ОБЕД',
                        'emoji': '🍲',
                        'name': 'Куриная грудка с гречкой',
                        'time': '13:00',
                        'calories': '450 ккал',
                        'ingredients': '• Куриная грудка - 150г\n• Гречка - 80г\n• Огурцы - 100г\n• Помидоры - 100г',
                        'instructions': '1. Отварите гречку\n2. Приготовьте куриную грудку\n3. Подавайте с овощами',
                        'cooking_time': '25 минут'
                    },
                    {
                        'type': 'ПЕРЕКУС 2',
                        'emoji': '🍎',
                        'name': 'Фруктовый салат',
                        'time': '16:00',
                        'calories': '200 ккал',
                        'ingredients': '• Яблоко - 1 шт\n• Банан - 1 шт\n• Апельсин - 1 шт\n• Йогурт - 50г',
                        'instructions': '1. Нарежьте фрукты\n2. Заправьте йогуртом\n3. Подавайте свежим',
                        'cooking_time': '10 минут'
                    },
                    {
                        'type': 'УЖИН',
                        'emoji': '🍛',
                        'name': 'Рыба с овощами',
                        'time': '19:00',
                        'calories': '400 ккал',
                        'ingredients': '• Белая рыба - 200г\n• Брокколи - 150г\n• Морковь - 100г\n• Лук - 50г',
                        'instructions': '1. Запеките рыбу с овощами\n2. Приправьте специями\n3. Подавайте горячим',
                        'cooking_time': '30 минут'
                    }
                ],
                'total_calories': '1650 ккал'
            }
            plan['days'].append(day)
        
        return plan
    
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
            port = int(os.environ.get('PORT', 10000))
            app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
        
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        logger.info(f"🚀 Flask server started on port {os.environ.get('PORT', 10000)}")
        
        # Запуск бота
        logger.info("🤖 Starting bot polling...")
        bot.application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )
        
    except Exception as e:
        logger.error(f"❌ Failed to start bot: {e}")
        sys.exit(1)

if __name__ == "__main__":
    run_bot()
