import os
import sqlite3
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from telegram import Bot
import asyncio

# ==================== НАСТРОЙКА ЛОГИРОВАНИЯ ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== ИНИЦИАЛИЗАЦИЯ FLASK ====================
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

# ==================== КОНФИГУРАЦИЯ ====================
class Config:
    TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHANNEL_ID = os.environ.get('TELEGRAM_CHANNEL_ID', '')
    DATABASE_URL = os.environ.get('DATABASE_URL', 'training_plans.db')
    
    @classmethod
    def validate(cls):
        if not cls.TELEGRAM_BOT_TOKEN:
            logger.warning("⚠️ TELEGRAM_BOT_TOKEN не установлен")
        else:
            logger.info("✅ TELEGRAM_BOT_TOKEN установлен")
            
        if not cls.TELEGRAM_CHANNEL_ID:
            logger.warning("⚠️ TELEGRAM_CHANNEL_ID не установлен")
        else:
            logger.info(f"✅ TELEGRAM_CHANNEL_ID: {cls.TELEGRAM_CHANNEL_ID}")
            
        return True

Config.validate()

# ==================== БАЗА ДАННЫХ ====================
def init_database():
    """Инициализация базы данных"""
    conn = sqlite3.connect(Config.DATABASE_URL)
    cursor = conn.cursor()
    
    # Таблица пользователей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'editor',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Таблица постов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT,
            tags TEXT,
            media_url TEXT,
            status TEXT DEFAULT 'draft',
            scheduled_time TIMESTAMP,
            published_time TIMESTAMP,
            telegram_message_id INTEGER,
            views INTEGER DEFAULT 0,
            engagement REAL DEFAULT 0,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Таблица шаблонов тренировок
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
    
    # Добавляем начальные шаблоны
    add_initial_templates(cursor)
    
    # Добавляем администратора
    cursor.execute('SELECT * FROM users WHERE username = ?', ('admin',))
    if not cursor.fetchone():
        password_hash = generate_password_hash('admin123')
        cursor.execute(
            'INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)',
            ('admin', password_hash, 'admin')
        )
        logger.info("✅ Администратор создан: admin / admin123")
    
    conn.commit()
    conn.close()
    logger.info("✅ База данных инициализирована")

def add_initial_templates(cursor):
    """Добавляем начальные шаблоны тренировок"""
    
    templates = [
        {
            'name': 'Атлетическое телосложение 46+',
            'category': 'мужчины_средний_возраст',
            'difficulty': 'средний',
            'duration_weeks': 16,
            'audience': 'Мужчина 46 лет, 82 кг, 182 см',
            'description': 'Программа для создания атлетического телосложения без жира',
            'content': json.dumps({
                'goal': 'Атлетическое телосложение, рекомпозиция',
                'schedule': 'Трехдневный сплит (Пн-Ср-Пт)',
                'days': {
                    'day1': 'Грудь, Плечи, Трицепс',
                    'day2': 'Спина и Бицепс',
                    'day3': 'Ноги и Пресс'
                },
                'recommendations': [
                    'Обязательная разминка 10-15 минут',
                    'Питание: высокий белок (2г на кг веса)',
                    'Сон 7-8 часов',
                    'Кардио в дни отдыха'
                ]
            })
        },
        {
            'name': 'Начальный уровень для подростка',
            'category': 'подростки_начальный',
            'difficulty': 'легкий',
            'duration_weeks': 12,
            'audience': 'Подросток 15 лет, 167 см, 45 кг',
            'description': 'Безопасная программа для подростков без весов',
            'content': json.dumps({
                'goal': 'Базовое развитие, укрепление мышц',
                'schedule': '3 раза в неделю (через день)',
                'warning': 'ВАЖНО: Без штанги! Только гантели и вес тела',
                'recommendations': [
                    'Фокус на технике, а не на весе',
                    'Использовать легкие гантели (2-5 кг)',
                    'Избегать осевой нагрузки на позвоночник',
                    'Питание: +300-500 ккал к норме, белок 1.5г/кг',
                    'Сон 8-9 часов для роста'
                ]
            })
        }
    ]
    
    for template in templates:
        cursor.execute('SELECT name FROM training_templates WHERE name = ?', (template['name'],))
        if not cursor.fetchone():
            cursor.execute('''
                INSERT INTO training_templates (name, category, difficulty, duration_weeks, audience, description, content)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                template['name'],
                template['category'],
                template['difficulty'],
                template['duration_weeks'],
                template['audience'],
                template['description'],
                template['content']
            ))

def get_db_connection():
    """Создание соединения с БД"""
    conn = sqlite3.connect(Config.DATABASE_URL)
    conn.row_factory = sqlite3.Row
    return conn

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def login_required(f):
    """Декоратор для проверки авторизации"""
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

async def send_to_telegram(title, content, tags="", media_url=None):
    """Отправка поста в Telegram"""
    try:
        if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHANNEL_ID:
            logger.error("❌ Токен бота или ID канала не установлены")
            return None
        
        logger.info(f"🔄 Отправка в Telegram: {title[:50]}...")
        
        bot = Bot(token=Config.TELEGRAM_BOT_TOKEN)
        
        message = f"<b>{title}</b>\n\n{content}"
        if tags:
            message += f"\n\n{tags}"
        
        if media_url and media_url.strip():
            media_url = media_url.strip()
            
            if media_url.lower().endswith(('.jpg', '.jpeg', '.png')):
                sent = await bot.send_photo(
                    chat_id=Config.TELEGRAM_CHANNEL_ID,
                    photo=media_url,
                    caption=message,
                    parse_mode='HTML'
                )
                logger.info("✅ Фото отправлено")
            elif media_url.lower().endswith(('.gif', '.mp4', '.mov', '.avi')):
                sent = await bot.send_video(
                    chat_id=Config.TELEGRAM_CHANNEL_ID,
                    video=media_url,
                    caption=message,
                    parse_mode='HTML'
                )
                logger.info("✅ Видео отправлено")
            else:
                sent = await bot.send_message(
                    chat_id=Config.TELEGRAM_CHANNEL_ID,
                    text=message,
                    parse_mode='HTML'
                )
                logger.info("✅ Текстовый пост отправлен (неизвестный тип медиа)")
        else:
            sent = await bot.send_message(
                chat_id=Config.TELEGRAM_CHANNEL_ID,
                text=message,
                parse_mode='HTML'
            )
            logger.info("✅ Текстовый пост отправлен")
        
        return sent.message_id
    
    except Exception as e:
        logger.error(f"❌ Ошибка отправки в Telegram: {str(e)}")
        return None

def run_async(coro):
    """Запуск асинхронной функции"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(coro)
    loop.close()
    return result

# ==================== МАРШРУТЫ ====================
@app.route('/')
def index():
    """Главная страница"""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Страница входа"""
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        conn = get_db_connection()
        user = conn.execute(
            'SELECT * FROM users WHERE username = ?', 
            (username,)
        ).fetchone()
        conn.close()
        
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            logger.info(f"✅ Пользователь {username} вошел в систему")
            return redirect(url_for('dashboard'))
        
        logger.warning(f"⚠️ Неудачная попытка входа: {username}")
        return render_template('login.html', error='Неверное имя пользователя или пароль')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    """Выход из системы"""
    username = session.get('username', 'неизвестный')
    session.clear()
    logger.info(f"✅ Пользователь {username} вышел из системы")
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    """Дашборд"""
    conn = get_db_connection()
    
    total_posts = conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
    published_posts = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE status = 'published'"
    ).fetchone()[0]
    drafts_count = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE status = 'draft'"
    ).fetchone()[0]
    
    recent_posts = conn.execute(
        'SELECT * FROM posts ORDER BY created_at DESC LIMIT 5'
    ).fetchall()
    
    last_post = conn.execute(
        "SELECT * FROM posts WHERE status = 'published' ORDER BY published_time DESC LIMIT 1"
    ).fetchone()
    
    conn.close()
    
    return render_template(
        'dashboard.html',
        total_posts=total_posts,
        published_posts=published_posts,
        drafts_count=drafts_count,
        recent_posts=recent_posts,
        last_post=last_post,
        username=session.get('username'),
        telegram_bot_token=Config.TELEGRAM_BOT_TOKEN[:10] + "..." if Config.TELEGRAM_BOT_TOKEN else "Не настроен",
        telegram_channel_id=Config.TELEGRAM_CHANNEL_ID
    )

@app.route('/posts')
@login_required
def posts():
    """Список постов"""
    conn = get_db_connection()
    status_filter = request.args.get('status', 'all')
    
    if status_filter == 'published':
        posts_list = conn.execute(
            "SELECT * FROM posts WHERE status = 'published' ORDER BY published_time DESC"
        ).fetchall()
    elif status_filter == 'scheduled':
        posts_list = conn.execute(
            "SELECT * FROM posts WHERE status = 'scheduled' ORDER BY scheduled_time"
        ).fetchall()
    elif status_filter == 'draft':
        posts_list = conn.execute(
            "SELECT * FROM posts WHERE status = 'draft' ORDER BY created_at DESC"
        ).fetchall()
    else:
        posts_list = conn.execute(
            'SELECT * FROM posts ORDER BY created_at DESC'
        ).fetchall()
    
    conn.close()
    return render_template('posts.html', 
                         posts=posts_list, 
                         status_filter=status_filter,
                         username=session.get('username'))

@app.route('/posts/create', methods=['GET', 'POST'])
@login_required
def create_post():
    """Создание поста"""
    conn = get_db_connection()
    
    if request.method == 'POST':
        title = request.form['title']
        content = request.form['content']
        category = request.form.get('category', '')
        tags = request.form.get('tags', '')
        media_url = request.form.get('media_url', '')
        status = request.form['status']
        
        logger.info(f"📝 Создание поста: {title[:50]}...")
        
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO posts (title, content, category, tags, media_url, status, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (title, content, category, tags, media_url, status, session['user_id']))
        
        post_id = cursor.lastrowid
        
        if status == 'published':
            logger.info(f"🚀 Публикация поста #{post_id} в Telegram...")
            message_id = run_async(send_to_telegram(title, content, tags, media_url))
            
            if message_id:
                cursor.execute('''
                    UPDATE posts 
                    SET published_time = CURRENT_TIMESTAMP, 
                        telegram_message_id = ?
                    WHERE id = ?
                ''', (message_id, post_id))
                logger.info(f"✅ Пост #{post_id} опубликован в Telegram (ID: {message_id})")
            else:
                cursor.execute('''
                    UPDATE posts SET status = 'draft' WHERE id = ?
                ''', (post_id,))
        
        conn.commit()
        conn.close()
        
        return redirect(url_for('posts'))
    
    templates = conn.execute(
        'SELECT id, name, category FROM training_templates WHERE is_active = 1'
    ).fetchall()
    
    conn.close()
    
    return render_template('create_post.html', 
                         templates=templates,
                         username=session.get('username'))

@app.route('/posts/publish/<int:post_id>')
@login_required
def publish_post(post_id):
    """Публикация черновика"""
    conn = get_db_connection()
    post = conn.execute('SELECT * FROM posts WHERE id = ?', (post_id,)).fetchone()
    
    if post and post['status'] == 'draft':
        logger.info(f"🚀 Публикация черновика #{post_id}: {post['title'][:50]}...")
        
        message_id = run_async(
            send_to_telegram(post['title'], post['content'], post['tags'], post['media_url'])
        )
        
        if message_id:
            conn.execute('''
                UPDATE posts 
                SET status = 'published', 
                    published_time = CURRENT_TIMESTAMP,
                    telegram_message_id = ?
                WHERE id = ?
            ''', (message_id, post_id))
            conn.commit()
            logger.info(f"✅ Пост #{post_id} опубликован в Telegram")
    
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
    logger.info(f"🗑️ Пост #{post_id} удален")
    return redirect(url_for('posts'))

@app.route('/templates')
@login_required
def templates():
    """Шаблоны тренировок"""
    conn = get_db_connection()
    templates_list = conn.execute(
        'SELECT * FROM training_templates ORDER BY created_at DESC'
    ).fetchall()
    conn.close()
    
    for template in templates_list:
        try:
            template['parsed_content'] = json.loads(template['content'])
        except:
            template['parsed_content'] = {}
    
    return render_template('templates.html', 
                         templates=templates_list,
                         username=session.get('username'))

@app.route('/test-telegram')
@login_required
def test_telegram():
    """Тест отправки в Telegram"""
    test_title = "✅ Тест подключения к Telegram"
    test_content = f"""
Тестовое сообщение от дашборда тренировок.

Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Бот: {'Настроен' if Config.TELEGRAM_BOT_TOKEN else 'Не настроен'}
Канал: {Config.TELEGRAM_CHANNEL_ID or 'Не настроен'}

Если вы видите это сообщение, значит подключение работает корректно!

#тест #настройка #тренировки
"""
    
    logger.info("🔄 Отправка тестового сообщения в Telegram...")
    message_id = run_async(send_to_telegram(test_title, test_content))
    
    if message_id:
        logger.info("✅ Тестовое сообщение отправлено успешно")
        return jsonify({
            'success': True,
            'message': 'Тестовое сообщение отправлено в Telegram',
            'message_id': message_id
        })
    else:
        logger.error("❌ Ошибка отправки тестового сообщения")
        return jsonify({
            'success': False,
            'message': 'Ошибка отправки тестового сообщения',
            'check': [
                'TELEGRAM_BOT_TOKEN установлен',
                'TELEGRAM_CHANNEL_ID установлен',
                'Бот добавлен в канал как администратор'
            ]
        })

@app.route('/health')
def health():
    """Health check для Render"""
    return jsonify({
        "status": "healthy",
        "service": "training-plans-dashboard",
        "timestamp": datetime.now().isoformat(),
        "database": "ok",
        "telegram_configured": bool(Config.TELEGRAM_BOT_TOKEN and Config.TELEGRAM_CHANNEL_ID)
    })

# ==================== ЗАПУСК ПРИЛОЖЕНИЯ ====================
if __name__ == '__main__':
    with app.app_context():
        init_database()
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
