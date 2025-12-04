import os
import sqlite3
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_session import Session
import plotly
import plotly.graph_objs as go
import plotly.express as px
from werkzeug.security import generate_password_hash, check_password_hash
import pandas as pd
from telegram import Bot
import asyncio
import threading
import aiohttp

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== КОНФИГУРАЦИЯ ====================
class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'your-secret-key-here')
    SESSION_TYPE = 'filesystem'
    DATABASE = 'training_plans.db'
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    CHANNEL_ID = os.getenv('CHANNEL_ID', '@training_plans_channel')
    ADMIN_USERNAME = 'admin'
    ADMIN_PASSWORD_HASH = generate_password_hash('admin123')

# ==================== БАЗА ДАННЫХ ====================
def init_database():
    conn = sqlite3.connect(Config.DATABASE)
    cursor = conn.cursor()
    
    # Пользователи
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'editor',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Контентные планы
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS content_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            category TEXT NOT NULL,
            target_audience TEXT,
            content_type TEXT NOT NULL,
            status TEXT DEFAULT 'draft',
            publish_date DATE,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (created_by) REFERENCES users (id)
        )
    ''')
    
    # Посты
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_plan_id INTEGER,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            media_url TEXT,
            category TEXT,
            tags TEXT,
            status TEXT DEFAULT 'draft',
            scheduled_time TIMESTAMP,
            published_time TIMESTAMP,
            telegram_message_id INTEGER,
            views INTEGER DEFAULT 0,
            engagement REAL DEFAULT 0,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (content_plan_id) REFERENCES content_plans (id),
            FOREIGN KEY (created_by) REFERENCES users (id)
        )
    ''')
    
    # Статистика канала
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS channel_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE UNIQUE NOT NULL,
            subscribers INTEGER DEFAULT 0,
            new_subscribers INTEGER DEFAULT 0,
            posts_published INTEGER DEFAULT 0,
            total_views INTEGER DEFAULT 0,
            avg_engagement REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Тренировочные планы (шаблоны)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS training_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            difficulty TEXT,
            duration_weeks INTEGER,
            audience TEXT,
            description TEXT,
            content TEXT NOT NULL,
            is_active BOOLEAN DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Вставляем стандартные тренировочные планы
    insert_default_templates(cursor)
    
    # Аналитика
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric_name TEXT NOT NULL,
            metric_value REAL,
            date DATE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Добавляем администратора, если его нет
    cursor.execute('SELECT * FROM users WHERE username = ?', (Config.ADMIN_USERNAME,))
    if not cursor.fetchone():
        cursor.execute(
            'INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)',
            (Config.ADMIN_USERNAME, Config.ADMIN_PASSWORD_HASH, 'admin')
        )
    
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")

def insert_default_templates(cursor):
    """Вставляем стандартные тренировочные планы"""
    
    # План для мужчины 46 лет
    man_46_plan = {
        'name': 'Атлетическое телосложение 46+',
        'category': 'мужчины_средний_возраст',
        'difficulty': 'средний',
        'duration_weeks': 16,
        'audience': 'Мужчина 46 лет, 82 кг, 182 см',
        'description': 'Программа для создания атлетического телосложения без жира',
        'content': json.dumps({
            'goal': 'Атлетическое телосложение, рекомпозиция',
            'schedule': 'Трехдневный сплит (Пн-Ср-Пт)',
            'phases': [
                {'weeks': '1-4', 'focus': 'Вводная фаза, освоение техники'},
                {'weeks': '5-12', 'focus': 'Фаза гипертрофии, рост мышц'},
                {'weeks': '13-16', 'focus': 'Фаза "сушки" и детализации'}
            ],
            'days': {
                'day1': {
                    'name': 'Грудь, Плечи, Трицепс',
                    'exercises': [
                        'Жим штанги на наклонной скамье 4x10-12',
                        'Разведения гантелей лежа 3x12-15',
                        'Жим гантелей сидя 4x10-12',
                        'Разведения гантелей в наклоне 3x12-15',
                        'Отжимания на брусьях 3xдо отказа'
                    ]
                },
                'day2': {
                    'name': 'Спина и Бицепс',
                    'exercises': [
                        'Подтягивания широким хватом 4x8-12',
                        'Тяга штанги в наклоне 4x8-10',
                        'Тяга гантели одной рукой 3x10-12',
                        'Подъем штанги на бицепс 3x10-12',
                        'Молотковые сгибания 3x12-15'
                    ]
                },
                'day3': {
                    'name': 'Ноги и Пресс',
                    'exercises': [
                        'Приседания со штангой 4x8-10',
                        'Румынская тяга 3x10-12',
                        'Выпады с гантелями 3x10-12',
                        'Подъем на носки стоя 4x15-20',
                        'Планка 3x60-90 сек',
                        'Подъем ног в висе 3x12-15'
                    ]
                }
            },
            'recommendations': [
                'Обязательная разминка 10-15 минут',
                'Заминка и растяжка после тренировки',
                'Питание: высокий белок (2г на кг веса)',
                'Сон 7-8 часов',
                'Кардио в дни отдыха 30-45 мин'
            ]
        })
    }
    
    # План для подростка 15 лет
    teen_15_plan = {
        'name': 'Начальный уровень для подростка',
        'category': 'подростки_начальный',
        'difficulty': 'легкий',
        'duration_weeks': 12,
        'audience': 'Подросток 15 лет, 167 см, 45 кг',
        'description': 'Безопасная программа для подростков без весов',
        'content': json.dumps({
            'goal': 'Базовое развитие, укрепление мышц',
            'schedule': '3 раза в неделю (через день)',
            'warning': 'ВАЖНО: Без штанги! Только гантели, вес тела и резинки',
            'phases': [
                {'weeks': '1-4', 'focus': 'Обучение технике, подготовка связок'},
                {'weeks': '5-8', 'focus': 'Базовые движения с легкими весами'},
                {'weeks': '9-12', 'focus': 'Прогрессия нагрузок'}
            ],
            'days': {
                'full_body': {
                    'name': 'Тренировка всего тела',
                    'exercises': [
                        'Приседания с собственным весом 3x15-20',
                        'Отжимания от пола (с колен при необходимости) 3x10-15',
                        'Тяга гантелей в наклоне 3x12-15',
                        'Выпады на месте 3x10-12 на ногу',
                        'Планка на локтях 3x30-45 сек',
                        'Подтягивания с резинкой 3x5-8'
                    ]
                }
            },
            'recommendations': [
                'ФОКУС НА ТЕХНИКЕ, а не на весе',
                'Использовать легкие гантели (2-5 кг)',
                'Избегать осевой нагрузки на позвоночник',
                'Упор на базовые движения без сложной техники',
                'Обязательно включать растяжку',
                'Питание: +300-500 ккал к норме, белок 1.5г/кг',
                'Сон 8-9 часов для роста'
            ],
            'growth_specific': [
                'Не использовать штангу до 16-17 лет',
                'Избегать жимов и приседов со штангой',
                'Работать с резинками и легкими гантелями',
                'Упор на развитие координации и нейромышечной связи'
            ]
        })
    }
    
    # Проверяем, есть ли уже эти шаблоны
    cursor.execute('SELECT name FROM training_templates WHERE name = ?', (man_46_plan['name'],))
    if not cursor.fetchone():
        cursor.execute('''
            INSERT INTO training_templates (name, category, difficulty, duration_weeks, audience, description, content)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            man_46_plan['name'],
            man_46_plan['category'],
            man_46_plan['difficulty'],
            man_46_plan['duration_weeks'],
            man_46_plan['audience'],
            man_46_plan['description'],
            man_46_plan['content']
        ))
    
    cursor.execute('SELECT name FROM training_templates WHERE name = ?', (teen_15_plan['name'],))
    if not cursor.fetchone():
        cursor.execute('''
            INSERT INTO training_templates (name, category, difficulty, duration_weeks, audience, description, content)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            teen_15_plan['name'],
            teen_15_plan['category'],
            teen_15_plan['difficulty'],
            teen_15_plan['duration_weeks'],
            teen_15_plan['audience'],
            teen_15_plan['description'],
            teen_15_plan['content']
        ))

# ==================== FLASK APP ====================
app = Flask(__name__)
app.config.from_object(Config)
Session(app)

# Инициализация базы данных
with app.app_context():
    init_database()

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def get_db_connection():
    conn = sqlite3.connect(Config.DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def login_required(f):
    """Декоратор для проверки авторизации"""
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

def admin_required(f):
    """Декоратор для проверки прав администратора"""
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        
        conn = get_db_connection()
        user = conn.execute('SELECT role FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        conn.close()
        
        if user and user['role'] != 'admin':
            return "Доступ запрещен", 403
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

async def send_to_telegram(post_content, media_url=None):
    """Асинхронная отправка поста в Telegram"""
    if not Config.TELEGRAM_BOT_TOKEN:
        logger.warning("Telegram bot token not configured")
        return None
    
    try:
        bot = Bot(token=Config.TELEGRAM_BOT_TOKEN)
        
        message = f"<b>{post_content['title']}</b>\n\n{post_content['content']}"
        
        if post_content.get('tags'):
            message += f"\n\n{post_content['tags']}"
        
        if media_url:
            # Отправка с медиа
            if media_url.endswith(('.jpg', '.jpeg', '.png', '.gif')):
                sent_message = await bot.send_photo(
                    chat_id=Config.CHANNEL_ID,
                    photo=media_url,
                    caption=message,
                    parse_mode='HTML'
                )
            elif media_url.endswith(('.mp4', '.avi', '.mov')):
                sent_message = await bot.send_video(
                    chat_id=Config.CHANNEL_ID,
                    video=media_url,
                    caption=message,
                    parse_mode='HTML'
                )
            else:
                sent_message = await bot.send_message(
                    chat_id=Config.CHANNEL_ID,
                    text=message,
                    parse_mode='HTML'
                )
        else:
            # Отправка текста
            sent_message = await bot.send_message(
                chat_id=Config.CHANNEL_ID,
                text=message,
                parse_mode='HTML'
            )
        
        return sent_message.message_id
    
    except Exception as e:
        logger.error(f"Error sending to Telegram: {e}")
        return None

def run_async(coro):
    """Запуск асинхронной функции в синхронном контексте"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(coro)
    loop.close()
    return result

def generate_content_plan():
    """Генерирует контент-план на неделю"""
    categories = {
        'beginner': 'Новичкам',
        'intermediate': 'Опытным',
        'nutrition': 'Питание',
        'recovery': 'Восстановление',
        'motivation': 'Мотивация'
    }
    
    days = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
    plans = []
    
    for i, day in enumerate(days):
        category = list(categories.values())[i % len(categories)]
        title = f"{day}: {category}"
        
        if category == 'Новичкам':
            content = "Базовые упражнения для начинающих. Как правильно начать тренировки без травм."
        elif category == 'Питание':
            content = "Секреты правильного питания для роста мышц и сжигания жира."
        elif category == 'Восстановление':
            content = "Важность отдыха и восстановления. Как избежать перетренированности."
        else:
            content = "Советы и рекомендации от профессиональных тренеров."
        
        plans.append({
            'day': day,
            'title': title,
            'category': category,
            'content': content,
            'scheduled_date': (datetime.now() + timedelta(days=i)).date()
        })
    
    return plans

# ==================== МАРШРУТЫ ====================
@app.route('/')
@login_required
def dashboard():
    """Главная страница дашборда"""
    conn = get_db_connection()
    
    # Статистика
    total_posts = conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
    published_posts = conn.execute("SELECT COUNT(*) FROM posts WHERE status = 'published'").fetchone()[0]
    scheduled_posts = conn.execute("SELECT COUNT(*) FROM posts WHERE status = 'scheduled'").fetchone()[0]
    total_templates = conn.execute('SELECT COUNT(*) FROM training_templates').fetchone()[0]
    
    # Последние посты
    recent_posts = conn.execute('''
        SELECT p.*, u.username 
        FROM posts p 
        LEFT JOIN users u ON p.created_by = u.id 
        ORDER BY p.created_at DESC 
        LIMIT 5
    ''').fetchall()
    
    # Статистика за последние 7 дней
    week_ago = (datetime.now() - timedelta(days=7)).date()
    weekly_stats = conn.execute('''
        SELECT date, subscribers, posts_published, avg_engagement
        FROM channel_stats 
        WHERE date >= ? 
        ORDER BY date
    ''', (week_ago,)).fetchall()
    
    conn.close()
    
    # Подготовка данных для графиков
    dates = [stat['date'] for stat in weekly_stats]
    subscribers = [stat['subscribers'] for stat in weekly_stats]
    posts_count = [stat['posts_published'] for stat in weekly_stats]
    engagement = [stat['avg_engagement'] for stat in weekly_stats]
    
    # Создание графиков
    fig1 = go.Figure()
    fig1.add_trace(go.Scatter(x=dates, y=subscribers, mode='lines+markers', name='Подписчики'))
    fig1.update_layout(title='Рост подписчиков за неделю', xaxis_title='Дата', yaxis_title='Количество')
    plot1 = json.dumps(fig1, cls=plotly.utils.PlotlyJSONEncoder)
    
    fig2 = go.Figure()
    fig2.add_trace(go.Bar(x=dates, y=posts_count, name='Посты'))
    fig2.update_layout(title='Публикации за неделю', xaxis_title='Дата', yaxis_title='Количество постов')
    plot2 = json.dumps(fig2, cls=plotly.utils.PlotlyJSONEncoder)
    
    fig3 = go.Figure()
    fig3.add_trace(go.Scatter(x=dates, y=engagement, mode='lines+markers', name='Вовлеченность'))
    fig3.update_layout(title='Вовлеченность за неделю', xaxis_title='Дата', yaxis_title='Вовлеченность (%)')
    plot3 = json.dumps(fig3, cls=plotly.utils.PlotlyJSONEncoder)
    
    return render_template('dashboard.html',
                         total_posts=total_posts,
                         published_posts=published_posts,
                         scheduled_posts=scheduled_posts,
                         total_templates=total_templates,
                         recent_posts=recent_posts,
                         plot1=plot1,
                         plot2=plot2,
                         plot3=plot3)

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Страница входа"""
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()
        
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            return redirect(url_for('dashboard'))
        
        return render_template('login.html', error='Неверное имя пользователя или пароль')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    """Выход из системы"""
    session.clear()
    return redirect(url_for('login'))

@app.route('/posts')
@login_required
def posts():
    """Управление постами"""
    conn = get_db_connection()
    status_filter = request.args.get('status', 'all')
    
    if status_filter == 'published':
        posts_list = conn.execute('''
            SELECT p.*, u.username 
            FROM posts p 
            LEFT JOIN users u ON p.created_by = u.id 
            WHERE p.status = 'published'
            ORDER BY p.published_time DESC
        ''').fetchall()
    elif status_filter == 'scheduled':
        posts_list = conn.execute('''
            SELECT p.*, u.username 
            FROM posts p 
            LEFT JOIN users u ON p.created_by = u.id 
            WHERE p.status = 'scheduled'
            ORDER BY p.scheduled_time
        ''').fetchall()
    elif status_filter == 'draft':
        posts_list = conn.execute('''
            SELECT p.*, u.username 
            FROM posts p 
            LEFT JOIN users u ON p.created_by = u.id 
            WHERE p.status = 'draft'
            ORDER BY p.created_at DESC
        ''').fetchall()
    else:
        posts_list = conn.execute('''
            SELECT p.*, u.username 
            FROM posts p 
            LEFT JOIN users u ON p.created_by = u.id 
            ORDER BY p.created_at DESC
        ''').fetchall()
    
    conn.close()
    return render_template('posts.html', posts=posts_list, status_filter=status_filter)

@app.route('/posts/create', methods=['GET', 'POST'])
@login_required
def create_post():
    """Создание нового поста"""
    conn = get_db_connection()
    templates = conn.execute('SELECT id, name, category FROM training_templates WHERE is_active = 1').fetchall()
    
    if request.method == 'POST':
        title = request.form['title']
        content = request.form['content']
        category = request.form['category']
        tags = request.form.get('tags', '')
        media_url = request.form.get('media_url', '')
        status = request.form['status']
        template_id = request.form.get('template_id')
        
        # Если выбран шаблон, добавляем его содержимое
        if template_id:
            template = conn.execute('SELECT content FROM training_templates WHERE id = ?', (template_id,)).fetchone()
            if template:
                template_content = json.loads(template['content'])
                content = f"{content}\n\n---\n\n{template_content}"
        
        scheduled_time = None
        if status == 'scheduled':
            scheduled_date = request.form.get('scheduled_date')
            scheduled_time = request.form.get('scheduled_time')
            if scheduled_date and scheduled_time:
                scheduled_time = f"{scheduled_date} {scheduled_time}"
        
        conn.execute('''
            INSERT INTO posts (title, content, category, tags, media_url, status, scheduled_time, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (title, content, category, tags, media_url, status, scheduled_time, session['user_id']))
        
        conn.commit()
        post_id = conn.lastrowid
        
        # Если статус "published", отправляем сразу
        if status == 'published':
            post_data = {
                'title': title,
                'content': content,
                'tags': tags
            }
            message_id = run_async(send_to_telegram(post_data, media_url))
            
            if message_id:
                conn.execute('''
                    UPDATE posts 
                    SET published_time = CURRENT_TIMESTAMP, telegram_message_id = ?
                    WHERE id = ?
                ''', (message_id, post_id))
                conn.commit()
        
        conn.close()
        return redirect(url_for('posts'))
    
    conn.close()
    return render_template('create_post.html', templates=templates)

@app.route('/posts/edit/<int:post_id>', methods=['GET', 'POST'])
@login_required
def edit_post(post_id):
    """Редактирование поста"""
    conn = get_db_connection()
    
    if request.method == 'POST':
        title = request.form['title']
        content = request.form['content']
        category = request.form['category']
        tags = request.form.get('tags', '')
        media_url = request.form.get('media_url', '')
        status = request.form['status']
        
        scheduled_time = None
        if status == 'scheduled':
            scheduled_date = request.form.get('scheduled_date')
            scheduled_time = request.form.get('scheduled_time')
            if scheduled_date and scheduled_time:
                scheduled_time = f"{scheduled_date} {scheduled_time}"
        
        conn.execute('''
            UPDATE posts 
            SET title = ?, content = ?, category = ?, tags = ?, media_url = ?, status = ?, scheduled_time = ?
            WHERE id = ?
        ''', (title, content, category, tags, media_url, status, scheduled_time, post_id))
        
        conn.commit()
        conn.close()
        return redirect(url_for('posts'))
    
    post = conn.execute('SELECT * FROM posts WHERE id = ?', (post_id,)).fetchone()
    conn.close()
    
    if not post:
        return "Пост не найден", 404
    
    return render_template('edit_post.html', post=post)

@app.route('/posts/publish/<int:post_id>')
@login_required
def publish_post(post_id):
    """Публикация поста"""
    conn = get_db_connection()
    post = conn.execute('SELECT * FROM posts WHERE id = ?', (post_id,)).fetchone()
    
    if post:
        post_data = {
            'title': post['title'],
            'content': post['content'],
            'tags': post['tags'] or ''
        }
        
        message_id = run_async(send_to_telegram(post_data, post['media_url']))
        
        if message_id:
            conn.execute('''
                UPDATE posts 
                SET status = 'published', 
                    published_time = CURRENT_TIMESTAMP, 
                    telegram_message_id = ?
                WHERE id = ?
            ''', (message_id, post_id))
            conn.commit()
    
    conn.close()
    return redirect(url_for('posts'))

@app.route('/posts/delete/<int:post_id>')
@login_required
def delete_post(post_id):
    """Удаление поста"""
    conn = get_db_connection()
    conn.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('posts'))

@app.route('/templates')
@login_required
def templates():
    """Управление шаблонами тренировок"""
    conn = get_db_connection()
    templates_list = conn.execute('SELECT * FROM training_templates ORDER BY created_at DESC').fetchall()
    conn.close()
    
    # Преобразуем JSON-содержимое для отображения
    for template in templates_list:
        try:
            content = json.loads(template['content'])
            template['parsed_content'] = content
        except:
            template['parsed_content'] = {}
    
    return render_template('templates.html', templates=templates_list)

@app.route('/templates/create', methods=['GET', 'POST'])
@login_required
def create_template():
    """Создание нового шаблона"""
    if request.method == 'POST':
        name = request.form['name']
        category = request.form['category']
        difficulty = request.form['difficulty']
        duration_weeks = int(request.form['duration_weeks'])
        audience = request.form['audience']
        description = request.form['description']
        
        # Структурируем контент
        content = {
            'goal': request.form.get('goal', ''),
            'schedule': request.form.get('schedule', ''),
            'phases': [],
            'days': {},
            'recommendations': request.form.get('recommendations', '').split('\n')
        }
        
        # Добавляем фазы
        for i in range(1, 4):
            phase_weeks = request.form.get(f'phase{i}_weeks', '')
            phase_focus = request.form.get(f'phase{i}_focus', '')
            if phase_weeks and phase_focus:
                content['phases'].append({
                    'weeks': phase_weeks,
                    'focus': phase_focus
                })
        
        # Добавляем дни тренировок
        for i in range(1, 4):
            day_name = request.form.get(f'day{i}_name', '')
            if day_name:
                exercises = request.form.get(f'day{i}_exercises', '').split('\n')
                content['days'][f'day{i}'] = {
                    'name': day_name,
                    'exercises': [ex.strip() for ex in exercises if ex.strip()]
                }
        
        conn = get_db_connection()
        conn.execute('''
            INSERT INTO training_templates (name, category, difficulty, duration_weeks, audience, description, content)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (name, category, difficulty, duration_weeks, audience, description, json.dumps(content, ensure_ascii=False)))
        
        conn.commit()
        conn.close()
        return redirect(url_for('templates'))
    
    return render_template('create_template.html')

@app.route('/content-plan')
@login_required
def content_plan():
    """Контент-план на неделю"""
    plans = generate_content_plan()
    return render_template('content_plan.html', plans=plans)

@app.route('/analytics')
@login_required
def analytics():
    """Страница аналитики"""
    conn = get_db_connection()
    
    # Общая статистика
    total_stats = conn.execute('''
        SELECT 
            COUNT(*) as total_posts,
            SUM(views) as total_views,
            AVG(engagement) as avg_engagement,
            MAX(published_time) as last_post_date
        FROM posts 
        WHERE status = 'published'
    ''').fetchone()
    
    # Статистика по категориям
    category_stats = conn.execute('''
        SELECT 
            category,
            COUNT(*) as post_count,
            SUM(views) as total_views,
            AVG(engagement) as avg_engagement
        FROM posts 
        WHERE status = 'published'
        GROUP BY category
        ORDER BY post_count DESC
    ''').fetchall()
    
    # Ежедневная статистика за последние 30 дней
    thirty_days_ago = (datetime.now() - timedelta(days=30)).date()
    daily_stats = conn.execute('''
        SELECT 
            date(published_time) as post_date,
            COUNT(*) as posts_per_day,
            SUM(views) as views_per_day,
            AVG(engagement) as engagement_per_day
        FROM posts 
        WHERE status = 'published' AND date(published_time) >= ?
        GROUP BY date(published_time)
        ORDER BY post_date
    ''', (thirty_days_ago,)).fetchall()
    
    conn.close()
    
    # Подготовка данных для графиков
    dates = [stat['post_date'] for stat in daily_stats]
    posts_per_day = [stat['posts_per_day'] for stat in daily_stats]
    views_per_day = [stat['views_per_day'] for stat in daily_stats]
    
    fig1 = go.Figure()
    fig1.add_trace(go.Bar(x=dates, y=posts_per_day, name='Посты в день'))
    fig1.update_layout(title='Публикации по дням (30 дней)', xaxis_title='Дата', yaxis_title='Количество постов')
    plot1 = json.dumps(fig1, cls=plotly.utils.PlotlyJSONEncoder)
    
    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(x=dates, y=views_per_day, mode='lines+markers', name='Просмотры'))
    fig2.update_layout(title='Просмотры по дням (30 дней)', xaxis_title='Дата', yaxis_title='Просмотры')
    plot2 = json.dumps(fig2, cls=plotly.utils.PlotlyJSONEncoder)
    
    # Круговая диаграмма по категориям
    categories = [stat['category'] for stat in category_stats]
    post_counts = [stat['post_count'] for stat in category_stats]
    
    fig3 = go.Figure(data=[go.Pie(labels=categories, values=post_counts, hole=.3)])
    fig3.update_layout(title='Распределение постов по категориям')
    plot3 = json.dumps(fig3, cls=plotly.utils.PlotlyJSONEncoder)
    
    return render_template('analytics.html',
                         total_stats=total_stats,
                         category_stats=category_stats,
                         daily_stats=daily_stats,
                         plot1=plot1,
                         plot2=plot2,
                         plot3=plot3)

@app.route('/api/stats/update', methods=['POST'])
@admin_required
def update_stats():
    """API для обновления статистики канала"""
    data = request.json
    
    conn = get_db_connection()
    
    # Обновляем или создаем запись за сегодня
    today = datetime.now().date()
    conn.execute('''
        INSERT OR REPLACE INTO channel_stats (date, subscribers, new_subscribers, posts_published, total_views, avg_engagement)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        today,
        data.get('subscribers', 0),
        data.get('new_subscribers', 0),
        data.get('posts_published', 0),
        data.get('total_views', 0),
        data.get('avg_engagement', 0)
    ))
    
    conn.commit()
    conn.close()
    
    return jsonify({'status': 'success'})

@app.route('/api/ai/generate', methods=['POST'])
@login_required
def ai_generate():
    """API для генерации контента с помощью AI"""
    data = request.json
    topic = data.get('topic', '')
    audience = data.get('audience', '')
    
    # Здесь можно интегрировать с OpenAI API или другим AI-сервисом
    # Временная заглушка с генерацией контента
    
    generated_content = {
        'title': f"Тренировочный план: {topic}",
        'content': f"""
🎯 <b>Программа тренировок для {audience}</b>

📊 <b>Основные принципы:</b>
1. Прогрессивная нагрузка
2. Правильная техника выполнения
3. Адекватное восстановление
4. Сбалансированное питание

🏋️ <b>Пример тренировки:</b>
• Разминка: 10-15 минут
• Основная часть: 45-60 минут
• Заминка и растяжка: 10 минут

💡 <b>Советы:</b>
• Следите за техникой выполнения упражнений
• Не пропускайте разминку и заминку
• Пейте достаточное количество воды
• Спите 7-8 часов в сутки

🔥 <b>Мотивация:</b>
Регулярность - ключ к успеху!
""",
        'tags': '#тренировки #фитнес #здоровье #мотивация'
    }
    
    return jsonify(generated_content)

@app.route('/settings')
@admin_required
def settings():
    """Настройки системы"""
    return render_template('settings.html')

# ==================== HTML ШАБЛОНЫ ====================
# Создаем папку templates если её нет
os.makedirs('templates', exist_ok=True)

# base.html
with open('templates/base.html', 'w', encoding='utf-8') as f:
    f.write('''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}Дашборд тренировок{% endblock %}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.8.1/font/bootstrap-icons.css" rel="stylesheet">
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <style>
        body { background-color: #f8f9fa; }
        .sidebar { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; min-height: 100vh; }
        .sidebar a { color: rgba(255,255,255,.8); text-decoration: none; }
        .sidebar a:hover { color: white; }
        .stat-card { border-radius: 10px; transition: transform 0.3s; }
        .stat-card:hover { transform: translateY(-5px); }
        .nav-pills .nav-link.active { background-color: #764ba2; }
    </style>
</head>
<body>
    <div class="container-fluid">
        <div class="row">
            <!-- Sidebar -->
            <div class="col-md-3 col-lg-2 sidebar p-0">
                <div class="p-4">
                    <h4 class="mb-4"><i class="bi bi-speedometer2"></i> Тренировки Pro</h4>
                    <ul class="nav flex-column">
                        <li class="nav-item mb-2">
                            <a class="nav-link {% if request.endpoint == 'dashboard' %}active{% endif %}" href="{{ url_for('dashboard') }}">
                                <i class="bi bi-house-door"></i> Дашборд
                            </a>
                        </li>
                        <li class="nav-item mb-2">
                            <a class="nav-link {% if request.endpoint == 'posts' %}active{% endif %}" href="{{ url_for('posts') }}">
                                <i class="bi bi-file-post"></i> Посты
                            </a>
                        </li>
                        <li class="nav-item mb-2">
                            <a class="nav-link {% if request.endpoint == 'templates' %}active{% endif %}" href="{{ url_for('templates') }}">
                                <i class="bi bi-file-earmark-text"></i> Шаблоны
                            </a>
                        </li>
                        <li class="nav-item mb-2">
                            <a class="nav-link {% if request.endpoint == 'content_plan' %}active{% endif %}" href="{{ url_for('content_plan') }}">
                                <i class="bi bi-calendar-week"></i> Контент-план
                            </a>
                        </li>
                        <li class="nav-item mb-2">
                            <a class="nav-link {% if request.endpoint == 'analytics' %}active{% endif %}" href="{{ url_for('analytics') }}">
                                <i class="bi bi-graph-up"></i> Аналитика
                            </a>
                        </li>
                        {% if session.get('role') == 'admin' %}
                        <li class="nav-item mb-2">
                            <a class="nav-link {% if request.endpoint == 'settings' %}active{% endif %}" href="{{ url_for('settings') }}">
                                <i class="bi bi-gear"></i> Настройки
                            </a>
                        </li>
                        {% endif %}
                    </ul>
                    <hr class="bg-light">
                    <div class="mt-4">
                        <span class="text-light">Привет, {{ session.get('username', 'Гость') }}</span>
                        <a href="{{ url_for('logout') }}" class="btn btn-outline-light btn-sm mt-2 w-100">
                            <i class="bi bi-box-arrow-right"></i> Выйти
                        </a>
                    </div>
                </div>
            </div>

            <!-- Main content -->
            <div class="col-md-9 col-lg-10 ms-auto p-4">
                {% with messages = get_flashed_messages() %}
                    {% if messages %}
                        {% for message in messages %}
                            <div class="alert alert-info alert-dismissible fade show" role="alert">
                                {{ message }}
                                <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
                            </div>
                        {% endfor %}
                    {% endif %}
                {% endwith %}
                
                {% block content %}{% endblock %}
            </div>
        </div>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
    {% block scripts %}{% endblock %}
</body>
</html>
''')

# dashboard.html
with open('templates/dashboard.html', 'w', encoding='utf-8') as f:
    f.write('''
{% extends "base.html" %}

{% block title %}Дашборд - Тренировки Pro{% endblock %}

{% block content %}
<div class="d-flex justify-content-between align-items-center mb-4">
    <h1><i class="bi bi-speedometer2"></i> Дашборд</h1>
    <a href="{{ url_for('create_post') }}" class="btn btn-primary">
        <i class="bi bi-plus-circle"></i> Создать пост
    </a>
</div>

<!-- Статистика -->
<div class="row mb-4">
    <div class="col-md-3 mb-3">
        <div class="card stat-card bg-primary text-white">
            <div class="card-body">
                <div class="d-flex justify-content-between align-items-center">
                    <div>
                        <h6 class="card-subtitle mb-2">Всего постов</h6>
                        <h2 class="card-title">{{ total_posts }}</h2>
                    </div>
                    <i class="bi bi-file-post fs-1 opacity-50"></i>
                </div>
            </div>
        </div>
    </div>
    <div class="col-md-3 mb-3">
        <div class="card stat-card bg-success text-white">
            <div class="card-body">
                <div class="d-flex justify-content-between align-items-center">
                    <div>
                        <h6 class="card-subtitle mb-2">Опубликовано</h6>
                        <h2 class="card-title">{{ published_posts }}</h2>
                    </div>
                    <i class="bi bi-check-circle fs-1 opacity-50"></i>
                </div>
            </div>
        </div>
    </div>
    <div class="col-md-3 mb-3">
        <div class="card stat-card bg-warning text-white">
            <div class="card-body">
                <div class="d-flex justify-content-between align-items-center">
                    <div>
                        <h6 class="card-subtitle mb-2">Запланировано</h6>
                        <h2 class="card-title">{{ scheduled_posts }}</h2>
                    </div>
                    <i class="bi bi-clock fs-1 opacity-50"></i>
                </div>
            </div>
        </div>
    </div>
    <div class="col-md-3 mb-3">
        <div class="card stat-card bg-info text-white">
            <div class="card-body">
                <div class="d-flex justify-content-between align-items-center">
                    <div>
                        <h6 class="card-subtitle mb-2">Шаблонов</h6>
                        <h2 class="card-title">{{ total_templates }}</h2>
                    </div>
                    <i class="bi bi-file-earmark-text fs-1 opacity-50"></i>
                </div>
            </div>
        </div>
    </div>
</div>

<!-- Графики -->
<div class="row mb-4">
    <div class="col-md-6 mb-3">
        <div class="card">
            <div class="card-body">
                <h5 class="card-title">Рост подписчиков</h5>
                <div id="plot1" style="height: 300px;"></div>
            </div>
        </div>
    </div>
    <div class="col-md-6 mb-3">
        <div class="card">
            <div class="card-body">
                <h5 class="card-title">Публикации</h5>
                <div id="plot2" style="height: 300px;"></div>
            </div>
        </div>
    </div>
</div>

<div class="row mb-4">
    <div class="col-md-12">
        <div class="card">
            <div class="card-body">
                <h5 class="card-title">Вовлеченность</h5>
                <div id="plot3" style="height: 300px;"></div>
            </div>
        </div>
    </div>
</div>

<!-- Последние посты -->
<div class="card">
    <div class="card-header d-flex justify-content-between align-items-center">
        <h5 class="mb-0">Последние посты</h5>
        <a href="{{ url_for('posts') }}" class="btn btn-sm btn-outline-primary">Все посты</a>
    </div>
    <div class="card-body">
        <div class="table-responsive">
            <table class="table table-hover">
                <thead>
                    <tr>
                        <th>Заголовок</th>
                        <th>Категория</th>
                        <th>Статус</th>
                        <th>Автор</th>
                        <th>Дата</th>
                        <th>Действия</th>
                    </tr>
                </thead>
                <tbody>
                    {% for post in recent_posts %}
                    <tr>
                        <td>{{ post.title[:50] }}{% if post.title|length > 50 %}...{% endif %}</td>
                        <td><span class="badge bg-secondary">{{ post.category }}</span></td>
                        <td>
                            {% if post.status == 'published' %}
                                <span class="badge bg-success">Опубликован</span>
                            {% elif post.status == 'scheduled' %}
                                <span class="badge bg-warning">Запланирован</span>
                            {% else %}
                                <span class="badge bg-secondary">Черновик</span>
                            {% endif %}
                        </td>
                        <td>{{ post.username or 'Система' }}</td>
                        <td>{{ post.created_at[:10] }}</td>
                        <td>
                            <div class="btn-group btn-group-sm">
                                <a href="{{ url_for('edit_post', post_id=post.id) }}" class="btn btn-outline-primary">
                                    <i class="bi bi-pencil"></i>
                                </a>
                                {% if post.status == 'draft' %}
                                <a href="{{ url_for('publish_post', post_id=post.id) }}" class="btn btn-outline-success">
                                    <i class="bi bi-send"></i>
                                </a>
                                {% endif %}
                                <a href="{{ url_for('delete_post', post_id=post.id) }}" class="btn btn-outline-danger"
                                   onclick="return confirm('Удалить этот пост?')">
                                    <i class="bi bi-trash"></i>
                                </a>
                            </div>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</div>
{% endblock %}

{% block scripts %}
<script>
    var plot1 = {{ plot1|safe }};
    var plot2 = {{ plot2|safe }};
    var plot3 = {{ plot3|safe }};
    
    Plotly.newPlot('plot1', plot1.data, plot1.layout);
    Plotly.newPlot('plot2', plot2.data, plot2.layout);
    Plotly.newPlot('plot3', plot3.data, plot3.layout);
</script>
{% endblock %}
''')

# Создаем остальные шаблоны...
# Создаем остальные HTML шаблоны для полноценной работы

# login.html
with open('templates/login.html', 'w', encoding='utf-8') as f:
    f.write('''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Вход - Дашборд тренировок</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.8.1/font/bootstrap-icons.css" rel="stylesheet">
    <style>
        body {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .login-card {
            background: white;
            border-radius: 20px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.1);
            width: 100%;
            max-width: 400px;
        }
    </style>
</head>
<body>
    <div class="login-card p-4">
        <div class="text-center mb-4">
            <i class="bi bi-speedometer2 fs-1 text-primary"></i>
            <h2 class="mt-2">Тренировки Pro</h2>
            <p class="text-muted">Панель управления контентом</p>
        </div>
        
        {% if error %}
        <div class="alert alert-danger">{{ error }}</div>
        {% endif %}
        
        <form method="POST" action="{{ url_for('login') }}">
            <div class="mb-3">
                <label for="username" class="form-label">Имя пользователя</label>
                <input type="text" class="form-control" id="username" name="username" required>
            </div>
            <div class="mb-3">
                <label for="password" class="form-label">Пароль</label>
                <input type="password" class="form-control" id="password" name="password" required>
            </div>
            <button type="submit" class="btn btn-primary w-100">
                <i class="bi bi-box-arrow-in-right"></i> Войти
            </button>
        </form>
        
        <div class="mt-3 text-center">
            <small class="text-muted">По умолчанию: admin / admin123</small>
        </div>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
''')

# posts.html
with open('templates/posts.html', 'w', encoding='utf-8') as f:
    f.write('''
{% extends "base.html" %}

{% block title %}Посты - Тренировки Pro{% endblock %}

{% block content %}
<div class="d-flex justify-content-between align-items-center mb-4">
    <h1><i class="bi bi-file-post"></i> Управление постами</h1>
    <a href="{{ url_for('create_post') }}" class="btn btn-primary">
        <i class="bi bi-plus-circle"></i> Создать пост
    </a>
</div>

<!-- Фильтры -->
<div class="card mb-4">
    <div class="card-body">
        <div class="row">
            <div class="col-md-8">
                <div class="btn-group" role="group">
                    <a href="{{ url_for('posts', status='all') }}" 
                       class="btn btn-outline-secondary {% if status_filter == 'all' %}active{% endif %}">
                        Все ({{ posts|length }})
                    </a>
                    <a href="{{ url_for('posts', status='published') }}" 
                       class="btn btn-outline-success {% if status_filter == 'published' %}active{% endif %}">
                        Опубликованные
                    </a>
                    <a href="{{ url_for('posts', status='scheduled') }}" 
                       class="btn btn-outline-warning {% if status_filter == 'scheduled' %}active{% endif %}">
                        Запланированные
                    </a>
                    <a href="{{ url_for('posts', status='draft') }}" 
                       class="btn btn-outline-secondary {% if status_filter == 'draft' %}active{% endif %}">
                        Черновики
                    </a>
                </div>
            </div>
            <div class="col-md-4">
                <div class="input-group">
                    <input type="text" class="form-control" placeholder="Поиск постов..." id="searchInput">
                    <button class="btn btn-outline-primary" type="button">
                        <i class="bi bi-search"></i>
                    </button>
                </div>
            </div>
        </div>
    </div>
</div>

<!-- Таблица постов -->
<div class="card">
    <div class="card-body">
        <div class="table-responsive">
            <table class="table table-hover">
                <thead>
                    <tr>
                        <th>Заголовок</th>
                        <th>Категория</th>
                        <th>Статус</th>
                        <th>Просмотры</th>
                        <th>Автор</th>
                        <th>Дата создания</th>
                        <th>Действия</th>
                    </tr>
                </thead>
                <tbody id="postsTable">
                    {% for post in posts %}
                    <tr>
                        <td>
                            <strong>{{ post.title[:30] }}{% if post.title|length > 30 %}...{% endif %}</strong>
                            {% if post.media_url %}
                                <i class="bi bi-image text-info ms-1"></i>
                            {% endif %}
                        </td>
                        <td><span class="badge bg-secondary">{{ post.category }}</span></td>
                        <td>
                            {% if post.status == 'published' %}
                                <span class="badge bg-success">Опубликован</span>
                                {% if post.published_time %}
                                <br><small>{{ post.published_time[:10] }}</small>
                                {% endif %}
                            {% elif post.status == 'scheduled' %}
                                <span class="badge bg-warning">Запланирован</span>
                                {% if post.scheduled_time %}
                                <br><small>{{ post.scheduled_time[:16] }}</small>
                                {% endif %}
                            {% else %}
                                <span class="badge bg-secondary">Черновик</span>
                            {% endif %}
                        </td>
                        <td>
                            {% if post.views %}
                                {{ post.views }}
                                {% if post.engagement %}
                                <br><small>{{ "%.1f"|format(post.engagement) }}%</small>
                                {% endif %}
                            {% else %}
                                -
                            {% endif %}
                        </td>
                        <td>{{ post.username or 'Система' }}</td>
                        <td>{{ post.created_at[:10] }}</td>
                        <td>
                            <div class="btn-group btn-group-sm">
                                <a href="{{ url_for('edit_post', post_id=post.id) }}" class="btn btn-outline-primary" 
                                   title="Редактировать">
                                    <i class="bi bi-pencil"></i>
                                </a>
                                {% if post.status == 'draft' %}
                                <a href="{{ url_for('publish_post', post_id=post.id) }}" class="btn btn-outline-success"
                                   title="Опубликовать" onclick="return confirm('Опубликовать этот пост?')">
                                    <i class="bi bi-send"></i>
                                </a>
                                {% elif post.status == 'scheduled' %}
                                <button class="btn btn-outline-info" title="Запланирован">
                                    <i class="bi bi-clock"></i>
                                </button>
                                {% endif %}
                                <a href="{{ url_for('delete_post', post_id=post.id) }}" class="btn btn-outline-danger"
                                   title="Удалить" onclick="return confirm('Удалить этот пост?')">
                                    <i class="bi bi-trash"></i>
                                </a>
                            </div>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        
        {% if not posts %}
        <div class="text-center py-5">
            <i class="bi bi-file-text fs-1 text-muted"></i>
            <p class="text-muted mt-2">Нет постов для отображения</p>
            <a href="{{ url_for('create_post') }}" class="btn btn-primary mt-2">Создать первый пост</a>
        </div>
        {% endif %}
    </div>
</div>
{% endblock %}

{% block scripts %}
<script>
document.getElementById('searchInput').addEventListener('keyup', function() {
    var filter = this.value.toLowerCase();
    var rows = document.querySelectorAll('#postsTable tr');
    
    rows.forEach(function(row) {
        var text = row.textContent.toLowerCase();
        row.style.display = text.indexOf(filter) > -1 ? '' : 'none';
    });
});
</script>
{% endblock %}
''')

# templates.html (для шаблонов тренировок)
with open('templates/templates.html', 'w', encoding='utf-8') as f:
    f.write('''
{% extends "base.html" %}

{% block title %}Шаблоны тренировок - Тренировки Pro{% endblock %}

{% block content %}
<div class="d-flex justify-content-between align-items-center mb-4">
    <h1><i class="bi bi-file-earmark-text"></i> Шаблоны тренировок</h1>
    <div>
        <a href="{{ url_for('content_plan') }}" class="btn btn-outline-primary me-2">
            <i class="bi bi-calendar-week"></i> Контент-план
        </a>
        <a href="{{ url_for('create_template') }}" class="btn btn-primary">
            <i class="bi bi-plus-circle"></i> Создать шаблон
        </a>
    </div>
</div>

<!-- Шаблоны -->
<div class="row">
    {% for template in templates %}
    <div class="col-md-6 col-lg-4 mb-4">
        <div class="card h-100">
            <div class="card-body">
                <div class="d-flex justify-content-between align-items-start mb-2">
                    <h5 class="card-title">{{ template.name }}</h5>
                    {% if template.is_active %}
                        <span class="badge bg-success">Активен</span>
                    {% else %}
                        <span class="badge bg-secondary">Неактивен</span>
                    {% endif %}
                </div>
                
                <p class="card-text text-muted">{{ template.description }}</p>
                
                <div class="mb-3">
                    <small class="text-muted">Категория:</small>
                    <span class="badge bg-info">{{ template.category }}</span>
                    
                    <small class="text-muted ms-3">Сложность:</small>
                    <span class="badge bg-warning">{{ template.difficulty }}</span>
                </div>
                
                <div class="mb-3">
                    <small class="text-muted">Аудитория:</small>
                    <div><strong>{{ template.audience }}</strong></div>
                </div>
                
                <div class="mb-3">
                    <small class="text-muted">Длительность:</small>
                    <div>{{ template.duration_weeks }} недель</div>
                </div>
                
                {% if template.parsed_content %}
                <div class="accordion" id="accordion{{ template.id }}">
                    <div class="accordion-item">
                        <h2 class="accordion-header">
                            <button class="accordion-button collapsed" type="button" 
                                    data-bs-toggle="collapse" 
                                    data-bs-target="#collapse{{ template.id }}">
                                Просмотреть детали
                            </button>
                        </h2>
                        <div id="collapse{{ template.id }}" class="accordion-collapse collapse">
                            <div class="accordion-body">
                                <small class="text-muted">Цель:</small>
                                <p>{{ template.parsed_content.get('goal', '') }}</p>
                                
                                <small class="text-muted">Расписание:</small>
                                <p>{{ template.parsed_content.get('schedule', '') }}</p>
                                
                                {% if template.parsed_content.get('days') %}
                                <small class="text-muted">Дни тренировок:</small>
                                <ul class="small">
                                    {% for day_key, day_data in template.parsed_content.get('days', {}).items() %}
                                    <li>
                                        <strong>{{ day_data.name }}:</strong>
                                        <ul>
                                            {% for exercise in day_data.exercises %}
                                            <li>{{ exercise }}</li>
                                            {% endfor %}
                                        </ul>
                                    </li>
                                    {% endfor %}
                                </ul>
                                {% endif %}
                                
                                <button class="btn btn-sm btn-outline-primary mt-2 use-template-btn"
                                        data-template-id="{{ template.id }}"
                                        data-template-name="{{ template.name }}">
                                    <i class="bi bi-clipboard-plus"></i> Использовать шаблон
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
                {% endif %}
            </div>
            <div class="card-footer bg-transparent">
                <small class="text-muted">
                    Создан: {{ template.created_at[:10] }}
                </small>
                <button class="btn btn-sm btn-outline-danger float-end delete-template-btn"
                        data-template-id="{{ template.id }}"
                        data-template-name="{{ template.name }}">
                    <i class="bi bi-trash"></i>
                </button>
            </div>
        </div>
    </div>
    {% endfor %}
</div>

{% if not templates %}
<div class="text-center py-5">
    <i class="bi bi-file-earmark-text fs-1 text-muted"></i>
    <p class="text-muted mt-2">Нет шаблонов тренировок</p>
    <a href="{{ url_for('create_template') }}" class="btn btn-primary mt-2">Создать первый шаблон</a>
</div>
{% endif %}

<!-- Модальное окно для использования шаблона -->
<div class="modal fade" id="useTemplateModal" tabindex="-1">
    <div class="modal-dialog">
        <div class="modal-content">
            <div class="modal-header">
                <h5 class="modal-title">Использовать шаблон</h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
            </div>
            <div class="modal-body">
                <p>Использовать шаблон <strong id="templateName"></strong> для создания нового поста?</p>
                <form id="useTemplateForm" action="{{ url_for('create_post') }}" method="GET">
                    <input type="hidden" name="template_id" id="templateId">
                </form>
            </div>
            <div class="modal-footer">
                <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Отмена</button>
                <button type="button" class="btn btn-primary" onclick="document.getElementById('useTemplateForm').submit()">
                    Использовать шаблон
                </button>
            </div>
        </div>
    </div>
</div>
{% endblock %}

{% block scripts %}
<script>
// Использование шаблона
document.querySelectorAll('.use-template-btn').forEach(button => {
    button.addEventListener('click', function() {
        const templateId = this.getAttribute('data-template-id');
        const templateName = this.getAttribute('data-template-name');
        
        document.getElementById('templateId').value = templateId;
        document.getElementById('templateName').textContent = templateName;
        
        new bootstrap.Modal(document.getElementById('useTemplateModal')).show();
    });
});

// Удаление шаблона
document.querySelectorAll('.delete-template-btn').forEach(button => {
    button.addEventListener('click', function() {
        const templateId = this.getAttribute('data-template-id');
        const templateName = this.getAttribute('data-template-name');
        
        if (confirm(`Удалить шаблон "${templateName}"?`)) {
            fetch(`/api/templates/delete/${templateId}`, {
                method: 'DELETE'
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    location.reload();
                }
            });
        }
    });
});
</script>
{% endblock %}
''')

# content_plan.html
with open('templates/content_plan.html', 'w', encoding='utf-8') as f:
    f.write('''
{% extends "base.html" %}

{% block title %}Контент-план - Тренировки Pro{% endblock %}

{% block content %}
<div class="d-flex justify-content-between align-items-center mb-4">
    <h1><i class="bi bi-calendar-week"></i> Контент-план на неделю</h1>
    <button class="btn btn-primary" onclick="generateContentPlan()">
        <i class="bi bi-magic"></i> Сгенерировать план
    </button>
</div>

<div class="row">
    {% for plan in plans %}
    <div class="col-md-6 col-lg-4 mb-4">
        <div class="card h-100">
            <div class="card-header d-flex justify-content-between align-items-center">
                <strong>{{ plan.day }}</strong>
                <span class="badge bg-primary">{{ plan.category }}</span>
            </div>
            <div class="card-body">
                <h6 class="card-title">{{ plan.title }}</h6>
                <p class="card-text">{{ plan.content }}</p>
                <small class="text-muted">Запланировано на: {{ plan.scheduled_date }}</small>
            </div>
            <div class="card-footer bg-transparent">
                <button class="btn btn-sm btn-outline-primary create-from-plan-btn"
                        data-title="{{ plan.title }}"
                        data-content="{{ plan.content }}"
                        data-category="{{ plan.category }}">
                    <i class="bi bi-plus-circle"></i> Создать пост
                </button>
            </div>
        </div>
    </div>
    {% endfor %}
</div>

<!-- Модальное окно для создания поста из плана -->
<div class="modal fade" id="createFromPlanModal" tabindex="-1">
    <div class="modal-dialog modal-lg">
        <div class="modal-content">
            <div class="modal-header">
                <h5 class="modal-title">Создать пост из контент-плана</h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
            </div>
            <form action="{{ url_for('create_post') }}" method="POST">
                <div class="modal-body">
                    <input type="hidden" name="title" id="planTitle">
                    <input type="hidden" name="content" id="planContent">
                    <input type="hidden" name="category" id="planCategory">
                    
                    <div class="mb-3">
                        <label class="form-label">Заголовок</label>
                        <input type="text" class="form-control" id="displayTitle" readonly>
                    </div>
                    
                    <div class="mb-3">
                        <label class="form-label">Контент</label>
                        <textarea class="form-control" id="displayContent" rows="6" readonly></textarea>
                    </div>
                    
                    <div class="mb-3">
                        <label class="form-label">Категория</label>
                        <input type="text" class="form-control" id="displayCategory" readonly>
                    </div>
                    
                    <div class="mb-3">
                        <label class="form-label">Дополнительные теги</label>
                        <input type="text" class="form-control" name="tags" placeholder="#тренировки #фитнес #мотивация">
                    </div>
                    
                    <div class="row">
                        <div class="col-md-6">
                            <label class="form-label">Статус</label>
                            <select class="form-select" name="status">
                                <option value="draft">Черновик</option>
                                <option value="scheduled">Запланировать</option>
                                <option value="published">Опубликовать сразу</option>
                            </select>
                        </div>
                        <div class="col-md-6">
                            <label class="form-label">Дата публикации</label>
                            <input type="date" class="form-control" name="scheduled_date">
                        </div>
                    </div>
                </div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Отмена</button>
                    <button type="submit" class="btn btn-primary">Создать пост</button>
                </div>
            </form>
        </div>
    </div>
</div>
{% endblock %}

{% block scripts %}
<script>
// Создание поста из контент-плана
document.querySelectorAll('.create-from-plan-btn').forEach(button => {
    button.addEventListener('click', function() {
        const title = this.getAttribute('data-title');
        const content = this.getAttribute('data-content');
        const category = this.getAttribute('data-category');
        
        document.getElementById('planTitle').value = title;
        document.getElementById('planContent').value = content;
        document.getElementById('planCategory').value = category;
        
        document.getElementById('displayTitle').value = title;
        document.getElementById('displayContent').value = content;
        document.getElementById('displayCategory').value = category;
        
        new bootstrap.Modal(document.getElementById('createFromPlanModal')).show();
    });
});

// Генерация контент-плана
function generateContentPlan() {
    fetch('/api/content/generate', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ weeks: 1 })
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === 'success') {
            location.reload();
        }
    });
}
</script>
{% endblock %}
''')

# analytics.html
with open('templates/analytics.html', 'w', encoding='utf-8') as f:
    f.write('''
{% extends "base.html" %}

{% block title %}Аналитика - Тренировки Pro{% endblock %}

{% block content %}
<h1 class="mb-4"><i class="bi bi-graph-up"></i> Аналитика канала</h1>

<!-- Общая статистика -->
<div class="row mb-4">
    <div class="col-md-3 mb-3">
        <div class="card bg-primary text-white">
            <div class="card-body text-center">
                <h6 class="card-subtitle mb-2">Всего постов</h6>
                <h2 class="card-title">{{ total_stats.total_posts or 0 }}</h2>
            </div>
        </div>
    </div>
    <div class="col-md-3 mb-3">
        <div class="card bg-success text-white">
            <div class="card-body text-center">
                <h6 class="card-subtitle mb-2">Всего просмотров</h6>
                <h2 class="card-title">{{ total_stats.total_views or 0 }}</h2>
            </div>
        </div>
    </div>
    <div class="col-md-3 mb-3">
        <div class="card bg-info text-white">
            <div class="card-body text-center">
                <h6 class="card-subtitle mb-2">Средняя вовлеченность</h6>
                <h2 class="card-title">
                    {% if total_stats.avg_engagement %}
                        {{ "%.1f"|format(total_stats.avg_engagement) }}%
                    {% else %}
                        0%
                    {% endif %}
                </h2>
            </div>
        </div>
    </div>
    <div class="col-md-3 mb-3">
        <div class="card bg-warning text-white">
            <div class="card-body text-center">
                <h6 class="card-subtitle mb-2">Последний пост</h6>
                <h6 class="card-title">
                    {% if total_stats.last_post_date %}
                        {{ total_stats.last_post_date[:10] }}
                    {% else %}
                        Нет постов
                    {% endif %}
                </h6>
            </div>
        </div>
    </div>
</div>

<!-- Графики -->
<div class="row mb-4">
    <div class="col-md-8 mb-3">
        <div class="card">
            <div class="card-body">
                <h5 class="card-title">Публикации по дням (30 дней)</h5>
                <div id="plot1" style="height: 400px;"></div>
            </div>
        </div>
    </div>
    <div class="col-md-4 mb-3">
        <div class="card">
            <div class="card-body">
                <h5 class="card-title">Распределение по категориям</h5>
                <div id="plot3" style="height: 400px;"></div>
            </div>
        </div>
    </div>
</div>

<div class="row mb-4">
    <div class="col-md-12">
        <div class="card">
            <div class="card-body">
                <h5 class="card-title">Просмотры по дням (30 дней)</h5>
                <div id="plot2" style="height: 400px;"></div>
            </div>
        </div>
    </div>
</div>

<!-- Статистика по категориям -->
<div class="card">
    <div class="card-header">
        <h5 class="mb-0">Статистика по категориям</h5>
    </div>
    <div class="card-body">
        <div class="table-responsive">
            <table class="table table-hover">
                <thead>
                    <tr>
                        <th>Категория</th>
                        <th>Количество постов</th>
                        <th>Всего просмотров</th>
                        <th>Средняя вовлеченность</th>
                        <th>Средние просмотры на пост</th>
                    </tr>
                </thead>
                <tbody>
                    {% for stat in category_stats %}
                    <tr>
                        <td><span class="badge bg-secondary">{{ stat.category }}</span></td>
                        <td>{{ stat.post_count }}</td>
                        <td>{{ stat.total_views or 0 }}</td>
                        <td>
                            {% if stat.avg_engagement %}
                                {{ "%.1f"|format(stat.avg_engagement) }}%
                            {% else %}
                                0%
                            {% endif %}
                        </td>
                        <td>
                            {% if stat.post_count > 0 and stat.total_views %}
                                {{ (stat.total_views / stat.post_count)|round|int }}
                            {% else %}
                                0
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</div>

<!-- Форма обновления статистики (для админа) -->
{% if session.get('role') == 'admin' %}
<div class="card mt-4">
    <div class="card-header">
        <h5 class="mb-0">Обновление статистики канала</h5>
    </div>
    <div class="card-body">
        <form id="updateStatsForm">
            <div class="row">
                <div class="col-md-3 mb-3">
                    <label class="form-label">Подписчики</label>
                    <input type="number" class="form-control" name="subscribers" required>
                </div>
                <div class="col-md-3 mb-3">
                    <label class="form-label">Новые подписчики</label>
                    <input type="number" class="form-control" name="new_subscribers">
                </div>
                <div class="col-md-3 mb-3">
                    <label class="form-label">Посты опубликовано</label>
                    <input type="number" class="form-control" name="posts_published">
                </div>
                <div class="col-md-3 mb-3">
                    <label class="form-label">Средняя вовлеченность (%)</label>
                    <input type="number" step="0.1" class="form-control" name="avg_engagement">
                </div>
            </div>
            <button type="submit" class="btn btn-primary">Обновить статистику</button>
        </form>
    </div>
</div>
{% endif %}
{% endblock %}

{% block scripts %}
<script>
// Графики
var plot1 = {{ plot1|safe }};
var plot2 = {{ plot2|safe }};
var plot3 = {{ plot3|safe }};

Plotly.newPlot('plot1', plot1.data, plot1.layout);
Plotly.newPlot('plot2', plot2.data, plot2.layout);
Plotly.newPlot('plot3', plot3.data, plot3.layout);

// Обновление статистики
{% if session.get('role') == 'admin' %}
document.getElementById('updateStatsForm').addEventListener('submit', function(e) {
    e.preventDefault();
    
    const formData = new FormData(this);
    const data = Object.fromEntries(formData.entries());
    
    fetch('/api/stats/update', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(data)
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === 'success') {
            alert('Статистика обновлена!');
            location.reload();
        }
    })
    .catch(error => {
        console.error('Error:', error);
        alert('Ошибка при обновлении статистики');
    });
});
{% endif %}
</script>
{% endblock %}
''')

# Создаем остальные необходимые шаблоны...
# create_post.html
with open('templates/create_post.html', 'w', encoding='utf-8') as f:
    f.write('''
{% extends "base.html" %}

{% block title %}Создать пост - Тренировки Pro{% endblock %}

{% block content %}
<div class="d-flex justify-content-between align-items-center mb-4">
    <h1><i class="bi bi-plus-circle"></i> Создать новый пост</h1>
    <div>
        <a href="{{ url_for('content_plan') }}" class="btn btn-outline-primary me-2">
            <i class="bi bi-calendar-week"></i> Контент-план
        </a>
        <button type="button" class="btn btn-outline-info" onclick="generateWithAI()">
            <i class="bi bi-magic"></i> AI Генерация
        </button>
    </div>
</div>

<form method="POST" action="{{ url_for('create_post') }}">
    <div class="row">
        <div class="col-md-8">
            <!-- Основная информация -->
            <div class="card mb-4">
                <div class="card-body">
                    <h5 class="card-title mb-3">Основная информация</h5>
                    
                    <div class="mb-3">
                        <label for="title" class="form-label">Заголовок поста *</label>
                        <input type="text" class="form-control" id="title" name="title" required 
                               placeholder="Например: Полная программа тренировок для мужчин 40+">
                    </div>
                    
                    <div class="mb-3">
                        <label for="content" class="form-label">Содержание поста *</label>
                        <textarea class="form-control" id="content" name="content" rows="12" required
                                  placeholder="Подробное описание тренировочной программы..."></textarea>
                        <small class="text-muted">Используйте HTML-теги для форматирования (b, i, code и т.д.)</small>
                    </div>
                    
                    <div class="mb-3">
                        <label for="category" class="form-label">Категория *</label>
                        <select class="form-select" id="category" name="category" required>
                            <option value="">Выберите категорию</option>
                            <option value="Мужские тренировки">Мужские тренировки</option>
                            <option value="Женские тренировки">Женские тренировки</option>
                            <option value="Подростки">Подростки</option>
                            <option value="Питание">Питание</option>
                            <option value="Восстановление">Восстановление</option>
                            <option value="Мотивация">Мотивация</option>
                            <option value="Советы">Советы</option>
                            <option value="Новичкам">Новичкам</option>
                            <option value="Профи">Профи</option>
                        </select>
                    </div>
                    
                    <div class="mb-3">
                        <label for="tags" class="form-label">Теги</label>
                        <input type="text" class="form-control" id="tags" name="tags" 
                               placeholder="#тренировки #фитнес #здоровье #мотивация">
                        <small class="text-muted">Разделяйте теги пробелами или запятыми</small>
                    </div>
                </div>
            </div>
            
            <!-- Медиа -->
            <div class="card mb-4">
                <div class="card-body">
                    <h5 class="card-title mb-3">Медиафайлы</h5>
                    
                    <div class="mb-3">
                        <label for="media_url" class="form-label">Ссылка на изображение или видео</label>
                        <input type="url" class="form-control" id="media_url" name="media_url" 
                               placeholder="https://example.com/image.jpg">
                        <small class="text-muted">Поддерживаются: JPG, PNG, GIF, MP4. Для видео - до 50MB</small>
                    </div>
                    
                    <div class="mb-3">
                        <label class="form-label">Предпросмотр медиа</label>
                        <div id="mediaPreview" class="border rounded p-3 text-center" style="min-height: 100px;">
                            <p class="text-muted mb-0">Здесь будет предпросмотр медиафайла</p>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <div class="col-md-4">
            <!-- Настройки публикации -->
            <div class="card mb-4">
                <div class="card-body">
                    <h5 class="card-title mb-3">Настройки публикации</h5>
                    
                    <div class="mb-3">
                        <label for="status" class="form-label">Статус *</label>
                        <select class="form-select" id="status" name="status" required onchange="toggleSchedule()">
                            <option value="draft">Черновик</option>
                            <option value="scheduled">Запланировать</option>
                            <option value="published">Опубликовать сразу</option>
                        </select>
                    </div>
                    
                    <div id="scheduleFields" style="display: none;">
                        <div class="mb-3">
                            <label for="scheduled_date" class="form-label">Дата публикации</label>
                            <input type="date" class="form-control" id="scheduled_date" name="scheduled_date"
                                   min="{{ datetime.now().date().isoformat() }}">
                        </div>
                        
                        <div class="mb-3">
                            <label for="scheduled_time" class="form-label">Время публикации</label>
                            <input type="time" class="form-control" id="scheduled_time" name="scheduled_time" value="12:00">
                        </div>
                    </div>
                    
                    <!-- Шаблоны тренировок -->
                    <div class="mb-3">
                        <label for="template_id" class="form-label">Шаблон тренировки</label>
                        <select class="form-select" id="template_id" name="template_id">
                            <option value="">Без шаблона</option>
                            {% for template in templates %}
                            <option value="{{ template.id }}">{{ template.name }} ({{ template.category }})</option>
                            {% endfor %}
                        </select>
                        <small class="text-muted">Добавить готовый план тренировок к посту</small>
                    </div>
                    
                    <!-- Предпросмотр в Telegram -->
                    <div class="mb-3">
                        <label class="form-label">Предпросмотр в Telegram</label>
                        <div class="border rounded p-3 bg-light" id="telegramPreview">
                            <small class="text-muted">Предпросмотр появится после ввода данных</small>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Быстрые действия -->
            <div class="card">
                <div class="card-body">
                    <h5 class="card-title mb-3">Быстрые действия</h5>
                    
                    <div class="d-grid gap-2">
                        <button type="submit" class="btn btn-primary">
                            <i class="bi bi-save"></i> Сохранить пост
                        </button>
                        
                        <button type="submit" class="btn btn-success" onclick="document.getElementById('status').value='published'">
                            <i class="bi bi-send"></i> Опубликовать сразу
                        </button>
                        
                        <a href="{{ url_for('posts') }}" class="btn btn-outline-secondary">
                            <i class="bi bi-x-circle"></i> Отмена
                        </a>
                    </div>
                </div>
            </div>
        </div>
    </div>
</form>

<!-- AI Генерация -->
<div class="modal fade" id="aiModal" tabindex="-1">
    <div class="modal-dialog">
        <div class="modal-content">
            <div class="modal-header">
                <h5 class="modal-title">AI Генерация контента</h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
            </div>
            <div class="modal-body">
                <div class="mb-3">
                    <label class="form-label">Тема поста</label>
                    <input type="text" class="form-control" id="aiTopic" 
                           placeholder="Например: программа тренировок для подростков">
                </div>
                <div class="mb-3">
                    <label class="form-label">Целевая аудитория</label>
                    <input type="text" class="form-control" id="aiAudience" 
                           placeholder="Например: подростки 15-18 лет">
                </div>
                <button class="btn btn-primary w-100" onclick="generateAIContent()">
                    <i class="bi bi-magic"></i> Сгенерировать
                </button>
                <div id="aiLoading" class="text-center mt-3" style="display: none;">
                    <div class="spinner-border text-primary" role="status">
                        <span class="visually-hidden">Загрузка...</span>
                    </div>
                    <p class="mt-2">Генерируем контент...</p>
                </div>
            </div>
        </div>
    </div>
</div>
{% endblock %}

{% block scripts %}
<script>
// Показать/скрыть поля планирования
function toggleSchedule() {
    const status = document.getElementById('status').value;
    const scheduleFields = document.getElementById('scheduleFields');
    
    if (status === 'scheduled') {
        scheduleFields.style.display = 'block';
        
        // Установить минимальную дату - сегодня
        const today = new Date().toISOString().split('T')[0];
        document.getElementById('scheduled_date').min = today;
        
        // Установить дату по умолчанию - завтра
        const tomorrow = new Date();
        tomorrow.setDate(tomorrow.getDate() + 1);
        document.getElementById('scheduled_date').value = tomorrow.toISOString().split('T')[0];
    } else {
        scheduleFields.style.display = 'none';
    }
}

// Предпросмотр медиа
document.getElementById('media_url').addEventListener('input', function() {
    const url = this.value;
    const preview = document.getElementById('mediaPreview');
    
    if (url) {
        if (url.match(/\.(jpg|jpeg|png|gif)$/i)) {
            preview.innerHTML = `<img src="${url}" class="img-fluid" style="max-height: 200px;" alt="Preview">`;
        } else if (url.match(/\.(mp4|avi|mov)$/i)) {
            preview.innerHTML = `
                <video controls class="w-100" style="max-height: 200px;">
                    <source src="${url}" type="video/mp4">
                    Ваш браузер не поддерживает видео.
                </video>`;
        } else {
            preview.innerHTML = '<p class="text-danger">Неподдерживаемый формат медиа</p>';
        }
    } else {
        preview.innerHTML = '<p class="text-muted mb-0">Здесь будет предпросмотр медиафайла</p>';
    }
});

// Предпросмотр в Telegram
function updateTelegramPreview() {
    const title = document.getElementById('title').value;
    const content = document.getElementById('content').value;
    const tags = document.getElementById('tags').value;
    const preview = document.getElementById('telegramPreview');
    
    if (title || content) {
        let previewHTML = '';
        
        if (title) {
            previewHTML += `<strong>${title}</strong><br><br>`;
        }
        
        if (content) {
            // Обрезаем контент для предпросмотра
            const shortContent = content.length > 200 ? content.substring(0, 200) + '...' : content;
            previewHTML += shortContent.replace(/\\n/g, '<br>');
        }
        
        if (tags) {
            previewHTML += `<br><br><small class="text-muted">${tags}</small>`;
        }
        
        preview.innerHTML = previewHTML;
    } else {
        preview.innerHTML = '<small class="text-muted">Предпросмотр появится после ввода данных</small>';
    }
}

// Обновление предпросмотра при вводе
['title', 'content', 'tags'].forEach(id => {
    document.getElementById(id).addEventListener('input', updateTelegramPreview);
});

// AI Генерация
function generateWithAI() {
    new bootstrap.Modal(document.getElementById('aiModal')).show();
}

function generateAIContent() {
    const topic = document.getElementById('aiTopic').value;
    const audience = document.getElementById('aiAudience').value;
    
    if (!topic || !audience) {
        alert('Пожалуйста, заполните все поля');
        return;
    }
    
    const loading = document.getElementById('aiLoading');
    loading.style.display = 'block';
    
    fetch('/api/ai/generate', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ topic, audience })
    })
    .then(response => response.json())
    .then(data => {
        loading.style.display = 'none';
        
        if (data.title && data.content) {
            document.getElementById('title').value = data.title;
            document.getElementById('content').value = data.content;
            document.getElementById('tags').value = data.tags || '';
            
            updateTelegramPreview();
            
            // Закрыть модальное окно
            bootstrap.Modal.getInstance(document.getElementById('aiModal')).hide();
            
            alert('Контент успешно сгенерирован!');
        } else {
            alert('Ошибка при генерации контента');
        }
    })
    .catch(error => {
        loading.style.display = 'none';
        console.error('Error:', error);
        alert('Ошибка при генерации контента');
    });
}

// Инициализация
document.addEventListener('DOMContentLoaded', function() {
    toggleSchedule();
    updateTelegramPreview();
});
</script>
{% endblock %}
''')

# create_template.html
with open('templates/create_template.html', 'w', encoding='utf-8') as f:
    f.write('''
{% extends "base.html" %}

{% block title %}Создать шаблон - Тренировки Pro{% endblock %}

{% block content %}
<div class="d-flex justify-content-between align-items-center mb-4">
    <h1><i class="bi bi-file-earmark-plus"></i> Создать шаблон тренировки</h1>
    <a href="{{ url_for('templates') }}" class="btn btn-outline-secondary">
        <i class="bi bi-arrow-left"></i> Назад к шаблонам
    </a>
</div>

<form method="POST" action="{{ url_for('create_template') }}">
    <div class="row">
        <div class="col-md-6">
            <!-- Основная информация -->
            <div class="card mb-4">
                <div class="card-body">
                    <h5 class="card-title mb-3">Основная информация</h5>
                    
                    <div class="mb-3">
                        <label for="name" class="form-label">Название шаблона *</label>
                        <input type="text" class="form-control" id="name" name="name" required
                               placeholder="Например: Атлетическое телосложение 46+">
                    </div>
                    
                    <div class="mb-3">
                        <label for="category" class="form-label">Категория *</label>
                        <select class="form-select" id="category" name="category" required>
                            <option value="">Выберите категорию</option>
                            <option value="мужчины_средний_возраст">Мужчины средний возраст</option>
                            <option value="мужчины_молодые">Мужчины молодые</option>
                            <option value="женщины_похудение">Женщины похудение</option>
                            <option value="женщины_тонинг">Женщины тонинг</option>
                            <option value="подростки_начальный">Подростки начальный</option>
                            <option value="подростки_продвинутый">Подростки продвинутый</option>
                            <option value="новички">Новички</option>
                            <option value="профи">Профи</option>
                            <option value="похудение">Похудение</option>
                            <option value="набор_массы">Набор массы</option>
                        </select>
                    </div>
                    
                    <div class="row">
                        <div class="col-md-6 mb-3">
                            <label for="difficulty" class="form-label">Сложность *</label>
                            <select class="form-select" id="difficulty" name="difficulty" required>
                                <option value="легкий">Легкий</option>
                                <option value="средний">Средний</option>
                                <option value="сложный">Сложный</option>
                                <option value="профи">Профи</option>
                            </select>
                        </div>
                        <div class="col-md-6 mb-3">
                            <label for="duration_weeks" class="form-label">Длительность (недель) *</label>
                            <input type="number" class="form-control" id="duration_weeks" name="duration_weeks" 
                                   min="1" max="52" value="12" required>
                        </div>
                    </div>
                    
                    <div class="mb-3">
                        <label for="audience" class="form-label">Целевая аудитория *</label>
                        <input type="text" class="form-control" id="audience" name="audience" required
                               placeholder="Например: Мужчина 46 лет, 82 кг, 182 см">
                    </div>
                    
                    <div class="mb-3">
                        <label for="description" class="form-label">Краткое описание *</label>
                        <textarea class="form-control" id="description" name="description" rows="3" required
                                  placeholder="Краткое описание программы..."></textarea>
                    </div>
                    
                    <div class="mb-3">
                        <label for="goal" class="form-label">Цель программы *</label>
                        <input type="text" class="form-control" id="goal" name="goal" required
                               placeholder="Например: Атлетическое телосложение, рекомпозиция">
                    </div>
                    
                    <div class="mb-3">
                        <label for="schedule" class="form-label">Расписание тренировок *</label>
                        <input type="text" class="form-control" id="schedule" name="schedule" required
                               placeholder="Например: Трехдневный сплит (Пн-Ср-Пт)">
                    </div>
                </div>
            </div>
            
            <!-- Рекомендации -->
            <div class="card mb-4">
                <div class="card-body">
                    <h5 class="card-title mb-3">Рекомендации</h5>
                    
                    <div class="mb-3">
                        <label for="recommendations" class="form-label">Общие рекомендации (по одной на строку)</label>
                        <textarea class="form-control" id="recommendations" name="recommendations" rows="5"
                                  placeholder="1. Обязательная разминка 10-15 минут
2. Заминка и растяжка после тренировки
3. Питание: высокий белок (2г на кг веса)"></textarea>
                    </div>
                </div>
            </div>
        </div>
        
        <div class="col-md-6">
            <!-- Фазы программы -->
            <div class="card mb-4">
                <div class="card-body">
                    <h5 class="card-title mb-3">Фазы программы (до 3 фаз)</h5>
                    
                    {% for i in range(1, 4) %}
                    <div class="card mb-3">
                        <div class="card-body">
                            <h6 class="card-subtitle mb-2">Фаза {{ i }}</h6>
                            <div class="row">
                                <div class="col-md-6 mb-2">
                                    <label class="form-label">Недели</label>
                                    <input type="text" class="form-control" name="phase{{ i }}_weeks" 
                                           placeholder="1-4">
                                </div>
                                <div class="col-md-6 mb-2">
                                    <label class="form-label">Фокус</label>
                                    <input type="text" class="form-control" name="phase{{ i }}_focus"
                                           placeholder="Вводная фаза, освоение техники">
                                </div>
                            </div>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            </div>
            
            <!-- Дни тренировок -->
            <div class="card mb-4">
                <div class="card-body">
                    <h5 class="card-title mb-3">Дни тренировок (до 4 дней)</h5>
                    
                    {% for i in range(1, 5) %}
                    <div class="card mb-3">
                        <div class="card-body">
                            <h6 class="card-subtitle mb-2">День {{ i }}</h6>
                            <div class="mb-2">
                                <label class="form-label">Название дня</label>
                                <input type="text" class="form-control" name="day{{ i }}_name"
                                       placeholder="Например: Грудь, Плечи, Трицепс">
                            </div>
                            <div class="mb-2">
                                <label class="form-label">Упражнения (по одному на строку)</label>
                                <textarea class="form-control" name="day{{ i }}_exercises" rows="3"
                                          placeholder="Жим штанги на наклонной скамье 4x10-12
Разведения гантелей лежа 3x12-15
Жим гантелей сидя 4x10-12"></textarea>
                            </div>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            </div>
            
            <!-- Кнопки действий -->
            <div class="card">
                <div class="card-body">
                    <div class="d-grid gap-2">
                        <button type="submit" class="btn btn-primary">
                            <i class="bi bi-save"></i> Сохранить шаблон
                        </button>
                        <a href="{{ url_for('templates') }}" class="btn btn-outline-secondary">
                            <i class="bi bi-x-circle"></i> Отмена
                        </a>
                    </div>
                </div>
            </div>
        </div>
    </div>
</form>
{% endblock %}
''')

# ==================== ЗАПУСК ПРИЛОЖЕНИЯ ====================
if __name__ == '__main__':
    # Создаем папку для сессий
    os.makedirs('flask_session', exist_ok=True)
    
    # Запускаем Flask приложение
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
