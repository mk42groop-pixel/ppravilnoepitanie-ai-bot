import os
import sqlite3
import logging
from datetime import datetime
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

# ==================== БАЗА ДАННЫХ ====================
def init_database():
    """Инициализация базы данных в памяти"""
    try:
        conn = sqlite3.connect(':memory:')
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'editor',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('SELECT * FROM users WHERE username = ?', ('admin',))
        if not cursor.fetchone():
            password_hash = generate_password_hash('admin123')
            cursor.execute(
                'INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)',
                ('admin', password_hash, 'admin')
            )
            logger.info("✅ Администратор создан: admin / admin123")
        
        conn.commit()
        
        app.config['DATABASE_CONN'] = conn
        app.config['DATABASE_CURSOR'] = cursor
        
        logger.info("✅ База данных инициализирована в памяти")
        
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации базы данных: {e}")

def get_db_connection():
    """Получение соединения с базой данных"""
    try:
        if 'DATABASE_CONN' not in app.config:
            init_database()
        
        return app.config['DATABASE_CONN'], app.config['DATABASE_CURSOR']
        
    except Exception as e:
        logger.error(f"❌ Ошибка получения соединения с БД: {e}")
        init_database()
        return app.config.get('DATABASE_CONN'), app.config.get('DATABASE_CURSOR')

# ==================== TELEGRAM ФУНКЦИИ ====================
def split_long_message(text, max_length=4000):
    """Разделяет длинное сообщение на части"""
    parts = []
    
    # Если текст уже меньше лимита
    if len(text) <= max_length:
        return [text]
    
    # Разделяем по абзацам
    paragraphs = text.split('\n\n')
    
    current_part = ""
    for paragraph in paragraphs:
        # Если добавление абзаца превышает лимит, начинаем новую часть
        if len(current_part) + len(paragraph) + 2 > max_length:
            if current_part:  # Если текущая часть не пуста
                parts.append(current_part.strip())
                current_part = ""
        
        # Добавляем абзац к текущей части
        if current_part:
            current_part += "\n\n" + paragraph
        else:
            current_part = paragraph
    
    # Добавляем последнюю часть
    if current_part:
        parts.append(current_part.strip())
    
    return parts

async def send_long_message_to_telegram(title, content, tags="", media_url=None):
    """Отправка длинных сообщений в Telegram с разделением"""
    try:
        if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHANNEL_ID:
            logger.warning("⚠️ Telegram не настроен")
            return False
        
        bot = Bot(token=Config.TELEGRAM_BOT_TOKEN)
        
        # Добавляем теги к последней части
        full_content = f"<b>{title}</b>\n\n{content}"
        if tags:
            full_content += f"\n\n{tags}"
        
        # Разделяем сообщение на части
        parts = split_long_message(full_content)
        
        # Если есть медиа, отправляем его с первой частью
        if media_url and media_url.strip():
            media_url_clean = media_url.strip()
            
            # Отправляем первую часть с медиа
            first_part = parts[0]
            if len(parts) > 1:
                first_part += "\n\n➡️ Продолжение следует..."
            
            try:
                if media_url_clean.lower().endswith(('.jpg', '.jpeg', '.png')):
                    await bot.send_photo(
                        chat_id=Config.TELEGRAM_CHANNEL_ID,
                        photo=media_url_clean,
                        caption=first_part,
                        parse_mode='HTML'
                    )
                elif media_url_clean.lower().endswith(('.gif', '.mp4', '.mov')):
                    await bot.send_video(
                        chat_id=Config.TELEGRAM_CHANNEL_ID,
                        video=media_url_clean,
                        caption=first_part,
                        parse_mode='HTML'
                    )
                else:
                    await bot.send_message(
                        chat_id=Config.TELEGRAM_CHANNEL_ID,
                        text=first_part,
                        parse_mode='HTML'
                    )
            except Exception as e:
                logger.error(f"❌ Ошибка отправки медиа: {e}")
                # Если не удалось отправить с медиа, пробуем без него
                await bot.send_message(
                    chat_id=Config.TELEGRAM_CHANNEL_ID,
                    text=first_part,
                    parse_mode='HTML'
                )
            
            # Отправляем остальные части
            for i, part in enumerate(parts[1:], 2):
                part_with_counter = f"<b>{title} (часть {i}/{len(parts)})</b>\n\n{part}"
                if i == len(parts) and tags:
                    part_with_counter += f"\n\n{tags}"
                
                await bot.send_message(
                    chat_id=Config.TELEGRAM_CHANNEL_ID,
                    text=part_with_counter,
                    parse_mode='HTML'
                )
                await asyncio.sleep(0.5)  # Небольшая задержка между сообщениями
        
        else:
            # Без медиа - просто отправляем все части
            for i, part in enumerate(parts, 1):
                part_with_counter = f"<b>{title} (часть {i}/{len(parts)})</b>\n\n{part}"
                if i == len(parts) and tags:
                    part_with_counter += f"\n\n{tags}"
                
                await bot.send_message(
                    chat_id=Config.TELEGRAM_CHANNEL_ID,
                    text=part_with_counter,
                    parse_mode='HTML'
                )
                await asyncio.sleep(0.5)
        
        logger.info(f"✅ Сообщение отправлено в Telegram ({len(parts)} частей)")
        return True
        
    except Exception as e:
        logger.error(f"❌ Ошибка отправки в Telegram: {e}")
        return False

def send_to_telegram_sync(title, content, tags="", media_url=None):
    """Отправка сообщения в Telegram (синхронная обертка)"""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(
            send_long_message_to_telegram(title, content, tags, media_url)
        )
        loop.close()
        return result
        
    except Exception as e:
        logger.error(f"❌ Ошибка в синхронной обертке: {e}")
        return False

# ==================== HTML ШАБЛОНЫ В КОДЕ ====================
def get_login_html(error=None):
    """HTML для страницы входа"""
    error_html = f'''
    <div class="alert">
        <strong>Ошибка:</strong> {error}
    </div>
    ''' if error else ''
    
    return f'''
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Вход - Дашборд тренировок</title>
        <style>
            body {{
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                margin: 0;
                font-family: Arial, sans-serif;
            }}
            .login-card {{
                background: white;
                border-radius: 15px;
                padding: 2rem;
                box-shadow: 0 10px 40px rgba(0,0,0,0.1);
                width: 100%;
                max-width: 400px;
            }}
            .alert {{
                padding: 10px;
                background: #f8d7da;
                color: #721c24;
                border-radius: 5px;
                margin-bottom: 15px;
                border: 1px solid #f5c6cb;
            }}
            .form-group {{
                margin-bottom: 15px;
            }}
            label {{
                display: block;
                margin-bottom: 5px;
                font-weight: bold;
            }}
            input {{
                width: 100%;
                padding: 10px;
                border: 1px solid #ddd;
                border-radius: 4px;
                box-sizing: border-box;
                font-size: 16px;
            }}
            button {{
                width: 100%;
                padding: 12px;
                background: #007bff;
                color: white;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                font-size: 16px;
                font-weight: bold;
            }}
            button:hover {{
                background: #0056b3;
            }}
            .text-center {{
                text-align: center;
            }}
            .text-muted {{
                color: #6c757d;
            }}
            h2 {{
                margin-top: 0;
                color: #333;
            }}
        </style>
    </head>
    <body>
        <div class="login-card">
            <div class="text-center">
                <h2>📊 Тренировки Pro</h2>
                <p class="text-muted">Панель управления Telegram-каналом</p>
            </div>
            
            {error_html}
            
            <form method="POST" action="/login">
                <div class="form-group">
                    <label for="username">Имя пользователя</label>
                    <input type="text" id="username" name="username" required placeholder="Введите логин">
                </div>
                <div class="form-group">
                    <label for="password">Пароль</label>
                    <input type="password" id="password" name="password" required placeholder="Введите пароль">
                </div>
                <button type="submit">Войти</button>
            </form>
            
            <div class="text-center" style="margin-top: 20px;">
                <small class="text-muted">По умолчанию: admin / admin123</small>
            </div>
        </div>
    </body>
    </html>
    '''

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
    try:
        if request.method == 'POST':
            username = request.form['username']
            password = request.form['password']
            
            conn, cursor = get_db_connection()
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT DEFAULT 'editor',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('SELECT COUNT(*) FROM users')
            user_count = cursor.fetchone()[0]
            
            if user_count == 0:
                password_hash = generate_password_hash('admin123')
                cursor.execute(
                    'INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)',
                    ('admin', password_hash, 'admin')
                )
                conn.commit()
                logger.info("✅ Администратор добавлен в пустую базу")
            
            cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
            user = cursor.fetchone()
            
            if user and check_password_hash(user[2], password):
                session['user_id'] = user[0]
                session['username'] = user[1]
                session['role'] = user[3]
                logger.info(f"✅ Пользователь {username} вошел в систему")
                return redirect(url_for('dashboard'))
            
            logger.warning(f"⚠️ Неудачная попытка входа: {username}")
            return get_login_html(error='Неверное имя пользователя или пароль')
        
        return get_login_html()
    
    except Exception as e:
        logger.error(f"❌ Ошибка в login: {e}")
        return f'''
        <html>
        <body>
            <h1>Ошибка сервера</h1>
            <p>Перезагрузите страницу или попробуйте снова через минуту.</p>
            <a href="/login">Попробовать снова</a>
        </body>
        </html>
        ''', 500

@app.route('/logout')
def logout():
    """Выход из системы"""
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
def dashboard():
    """Дашборд"""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    telegram_bot_status = "✅ Настроен" if Config.TELEGRAM_BOT_TOKEN else "❌ Не настроен"
    telegram_channel_status = "✅ Настроен" if Config.TELEGRAM_CHANNEL_ID else "❌ Не настроен"
    
    return f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Дашборд тренировок</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                padding: 20px;
                max-width: 1000px;
                margin: 0 auto;
            }}
            .status {{
                padding: 15px;
                margin: 10px 0;
                border-radius: 5px;
            }}
            .success {{
                background: #d4edda;
                color: #155724;
                border: 1px solid #c3e6cb;
            }}
            .danger {{
                background: #f8d7da;
                color: #721c24;
                border: 1px solid #f5c6cb;
            }}
            .action-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 15px;
                margin: 30px 0;
            }}
            .action-card {{
                padding: 20px;
                background: white;
                border-radius: 8px;
                box-shadow: 0 2px 5px rgba(0,0,0,0.1);
                text-align: center;
            }}
            .action-card a {{
                display: block;
                padding: 15px;
                background: #007bff;
                color: white;
                text-decoration: none;
                border-radius: 5px;
                margin-top: 10px;
            }}
            .action-card a:hover {{
                background: #0056b3;
            }}
            a.back-button {{
                display: inline-block;
                margin-top: 20px;
                padding: 10px 20px;
                background: #6c757d;
                color: white;
                text-decoration: none;
                border-radius: 5px;
            }}
        </style>
    </head>
    <body>
        <h1>📊 Дашборд тренировок</h1>
        <p>Привет, <strong>{session.get("username")}</strong>!</p>
        
        <h2>Статус подключений:</h2>
        
        <div class="status {'success' if Config.TELEGRAM_BOT_TOKEN else 'danger'}">
            <strong>🤖 Telegram бот:</strong> {telegram_bot_status}
        </div>
        
        <div class="status {'success' if Config.TELEGRAM_CHANNEL_ID else 'danger'}">
            <strong>📢 Telegram канал:</strong> {telegram_channel_status}
        </div>
        
        <h2>Быстрые действия:</h2>
        
        <div class="action-grid">
            <div class="action-card">
                <h3>📡 Проверить Telegram</h3>
                <p>Отправить тестовое сообщение</p>
                <a href="/test-telegram">Проверить</a>
            </div>
            
            <div class="action-card">
                <h3>📝 Создать пост</h3>
                <p>Опубликовать новый пост</p>
                <a href="/create-post">Создать</a>
            </div>
            
            <div class="action-card">
                <h3>🏋️ Шаблоны</h3>
                <p>Готовые программы тренировок</p>
                <a href="/templates">Просмотреть</a>
            </div>
            
            <div class="action-card">
                <h3>🚪 Выйти</h3>
                <p>Завершить сеанс</p>
                <a href="/logout">Выйти</a>
            </div>
        </div>
    </body>
    </html>
    '''

@app.route('/test-telegram')
def test_telegram():
    """Тест Telegram подключения"""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    if not Config.TELEGRAM_BOT_TOKEN:
        return '''
        <html>
        <body>
            <h1>Тест Telegram</h1>
            <div style="padding: 20px; background: #fff3cd; border-radius: 5px;">
                <strong>⚠️ TELEGRAM_BOT_TOKEN не установлен!</strong>
                <p>Добавьте токен в переменные окружения на Render.</p>
            </div>
            <a href="/dashboard" class="back-button">Назад в дашборд</a>
        </body>
        </html>
        '''
    
    test_title = "✅ Тест подключения"
    test_content = f"""
Тестовое сообщение от дашборда тренировок!

Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Пользователь: {session.get('username')}

Если вы видите это сообщение, значит подключение работает корректно!

#тест #настройка #тренировки
"""
    
    success = send_to_telegram_sync(test_title, test_content)
    
    if success:
        message = '''
        <div style="padding: 20px; background: #d4edda; border-radius: 5px;">
            <strong>✅ Тестовое сообщение отправлено в Telegram!</strong>
            <p>Проверьте ваш канал.</p>
        </div>
        '''
    else:
        message = '''
        <div style="padding: 20px; background: #f8d7da; border-radius: 5px;">
            <strong>❌ Ошибка отправки сообщения!</strong>
            <p>Проверьте:</p>
            <ul>
                <li>Токен бота правильный</li>
                <li>Бот добавлен в канал как администратор</li>
                <li>ID канала начинается с -100 (для публичного канала)</li>
            </ul>
        </div>
        '''
    
    return f'''
    <html>
    <head>
        <style>
            body {{
                font-family: Arial, sans-serif;
                padding: 20px;
            }}
            .back-button {{
                display: inline-block;
                margin-top: 20px;
                padding: 10px 20px;
                background: #007bff;
                color: white;
                text-decoration: none;
                border-radius: 5px;
            }}
        </style>
    </head>
    <body>
        <h1>Тест Telegram подключения</h1>
        {message}
        <a href="/dashboard" class="back-button">Назад в дашборд</a>
    </body>
    </html>
    '''

@app.route('/create-post', methods=['GET', 'POST'])
def create_post():
    """Создание нового поста"""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        content = request.form.get('content', '').strip()
        tags = request.form.get('tags', '').strip()
        media_url = request.form.get('media_url', '').strip()
        
        if not title or not content:
            return '''
            <html>
            <body>
                <h1>Ошибка</h1>
                <div style="padding: 20px; background: #f8d7da; border-radius: 5px;">
                    <strong>❌ Заполните все обязательные поля!</strong>
                </div>
                <a href="/create-post" style="display: inline-block; margin-top: 20px; padding: 10px 20px; background: #007bff; color: white; text-decoration: none; border-radius: 5px;">Назад</a>
            </body>
            </html>
            '''
        
        # Проверяем длину сообщения
        message_length = len(f"{title}\n\n{content}\n\n{tags}")
        
        success = send_to_telegram_sync(title, content, tags, media_url)
        
        if success:
            message = f'''
            <div style="padding: 20px; background: #d4edda; border-radius: 5px;">
                <strong>✅ Пост опубликован в Telegram!</strong>
                <p><strong>Заголовок:</strong> {title}</p>
                <p><strong>Длина сообщения:</strong> {message_length} символов</p>
                <p><strong>Частей:</strong> {message_length // 4000 + 1 if message_length > 4000 else 1}</p>
            </div>
            '''
        else:
            message = '''
            <div style="padding: 20px; background: #f8d7da; border-radius: 5px;">
                <strong>❌ Ошибка публикации!</strong>
                <p>Проверьте настройки Telegram.</p>
            </div>
            '''
        
        return f'''
        <html>
        <body>
            <h1>Результат публикации</h1>
            {message}
            <div style="margin-top: 20px;">
                <a href="/dashboard" style="display: inline-block; padding: 10px 20px; background: #007bff; color: white; text-decoration: none; border-radius: 5px; margin-right: 10px;">В дашборд</a>
                <a href="/create-post" style="display: inline-block; padding: 10px 20px; background: #28a745; color: white; text-decoration: none; border-radius: 5px;">Создать еще один пост</a>
            </div>
        </body>
        </html>
        '''
    
    return '''
    <html>
    <head>
        <style>
            body {
                font-family: Arial, sans-serif;
                padding: 20px;
                max-width: 800px;
                margin: 0 auto;
            }
            form {
                background: #f8f9fa;
                padding: 20px;
                border-radius: 5px;
            }
            .form-group {
                margin-bottom: 15px;
            }
            label {
                display: block;
                margin-bottom: 5px;
                font-weight: bold;
            }
            input, textarea {
                width: 100%;
                padding: 10px;
                border: 1px solid #ddd;
                border-radius: 4px;
                box-sizing: border-box;
            }
            button {
                background: #28a745;
                color: white;
                padding: 12px 24px;
                border: none;
                border-radius: 4px;
                cursor: pointer;
                font-size: 16px;
                font-weight: bold;
            }
            button:hover {
                background: #218838;
            }
            .back-button {
                display: inline-block;
                margin-top: 20px;
                padding: 10px 20px;
                background: #6c757d;
                color: white;
                text-decoration: none;
                border-radius: 5px;
            }
            .info-box {
                padding: 10px;
                background: #d1ecf1;
                border-radius: 5px;
                margin: 10px 0;
                border-left: 4px solid #0c5460;
            }
        </style>
    </head>
    <body>
        <h1>📝 Создать новый пост</h1>
        
        <div class="info-box">
            <strong>📢 Важно:</strong> Длинные посты автоматически разделяются на несколько сообщений.
            Максимальная длина одного сообщения в Telegram - 4096 символов.
        </div>
        
        <form method="POST">
            <div class="form-group">
                <label for="title"><strong>Заголовок *</strong></label>
                <input type="text" id="title" name="title" placeholder="Введите заголовок поста" required>
            </div>
            
            <div class="form-group">
                <label for="content"><strong>Содержание *</strong></label>
                <textarea id="content" name="content" rows="15" placeholder="Введите текст поста..." required></textarea>
            </div>
            
            <div class="form-group">
                <label for="tags"><strong>Теги (через пробел)</strong></label>
                <input type="text" id="tags" name="tags" placeholder="#тренировки #фитнес #здоровье">
            </div>
            
            <div class="form-group">
                <label for="media_url"><strong>Ссылка на изображение/видео</strong></label>
                <input type="url" id="media_url" name="media_url" placeholder="https://example.com/image.jpg">
                <small>Поддерживаются: JPG, PNG, GIF, MP4. Медиа будет в первом сообщении.</small>
            </div>
            
            <button type="submit">📤 Опубликовать в Telegram</button>
        </form>
        <a href="/dashboard" class="back-button">← Назад в дашборд</a>
    </body>
    </html>
    '''

@app.route('/templates')
def templates():
    """Шаблоны тренировок"""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    # КОРОТКАЯ ВЕРСИЯ ПРОГРАММЫ (для демонстрации)
    short_program = '''🏆 **ПОЛНАЯ ПРОГРАММА ТРЕНИРОВОК (3 раза в неделю)**
🎯 Цель: гармоничное развитие всех мышечных групп

🏋️ **ТРЕНИРОВКА 1: ГРУДЬ + ТРИЦЕПС**
1. Жим штанги на наклонной: 4x10-12
2. Жим гантелей: 3x10-12
3. Жим в хаммере: 3x10-12
4. Французский жим: 4x10-12
5. Разгибания на блоке: 3x12-15

🏋️ **ТРЕНИРОВКА 2: СПИНА + БИЦЕПС**
1. Тяга верхнего блока: 4x10-12
2. Тяга горизонтального: 4x10-12
3. Подъем штанги: 4x8-10
4. Подъем гантелей: 3x10-12
5. Задняя дельта: 3x12-15

🏋️ **ТРЕНИРОВКА 3: НОГИ + ПЛЕЧИ**
1. Жим ногами: 4x10-12
2. Сгибания ног: 3x10-12
3. Икры: 4x15-20
4. Жим гантелей: 4x10-12
5. Махи в стороны: 3x12-15
6. Пресс: 3x15-20

🥗 **ПИТАНИЕ:**
• Белки: 2г/кг
• Углеводы: 3-4г/кг
• Жиры: 1г/кг
• Вода: 2.5-3л

#полнаяпрограмма #тренажеры #фитнес #тренировки'''

    templates_data = [
        {
            'id': 1,
            'name': '🔥 ПОЛНАЯ ПРОГРАММА (КОРОТКАЯ ВЕРСИЯ)',
            'description': 'Сокращенная версия для Telegram. Все отделы груди, трицепс, бицепс, спина, пресс, плечи, ноги',
            'content': short_program
        },
        {
            'id': 2,
            'name': '🏋️ СПЛИТ 4 ДНЯ (для продвинутых)',
            'description': 'Раздельная проработка мышц на 4 дня',
            'content': '''📅 **4-ДНЕВНЫЙ СПЛИТ**
День 1: Грудь + Трицепс
День 2: Спина + Бицепс
День 4: Ноги
День 5: Плечи + Пресс

**ДЕНЬ 1: ГРУДЬ + ТРИЦЕПС**
1. Жим штанги на наклонной: 4x8-10
2. Жим гантелей: 3x10-12
3. Разгибания на блоке: 3x12-15

**ДЕНЬ 2: СПИНА + БИЦЕПС**
1. Тяга верхнего блока: 4x10-12
2. Тяга горизонтального: 4x10-12
3. Подъем штанги на бицепс: 4x8-10

**ДЕНЬ 4: НОГИ**
1. Жим ногами: 4x10-12
2. Сгибания ног: 3x10-12
3. Разгибания ног: 3x12-15

**ДЕНЬ 5: ПЛЕЧИ + ПРЕСС**
1. Жим гантелей: 4x10-12
2. Махи в стороны: 3x12-15
3. Скручивания: 3x15-20

#сплит #4дня #продвинутый'''
        },
        {
            'id': 3,
            'name': '💪 КРУГОВАЯ ТРЕНИРОВКА (жиросжигание)',
            'description': 'Высокая интенсивность для сжигания жира',
            'content': '''🔥 **КРУГОВАЯ ТРЕНИРОВКА (3 круга)**
1. Жим ногами: 15 повторений
2. Тяга верхнего блока: 12 повторений
3. Жим штанги: 12 повторений
4. Подъем штанги: 12 повторений
5. Скручивания: 20 повторений

**Отдых:** 30 сек между упражнениями
**Круги:** 3 с отдыхом 2 мин

**ПИТАНИЕ ДЛЯ СУШКИ:**
• Дефицит 15-20%
• Белки: 2.2-2.5г/кг
• Кардио 30 мин после тренировки

#круговая #жиросжигание #сушка'''
        }
    ]
    
    templates_html = ''
    for template in templates_data:
        templates_html += f'''
        <div class="template-card">
            <h3>{template['name']}</h3>
            <p><strong>Описание:</strong> {template['description']}</p>
            <div style="margin: 10px 0;">
                <button onclick="copyTemplate({template['id']})" class="copy-button" style="background: #28a745; color: white; padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer; margin-right: 10px;">
                    📋 Скопировать программу
                </button>
                <button onclick="sendToPost({template['id']})" class="send-button" style="background: #007bff; color: white; padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer;">
                    📤 Отправить в редактор
                </button>
            </div>
            <button onclick="toggleContent({template['id']})" id="toggle-btn-{template['id']}" style="background: #6c757d; color: white; padding: 5px 10px; border: none; border-radius: 4px; cursor: pointer; margin-top: 5px;">
                👁 Показать программу
            </button>
            <div id="template-{template['id']}" class="template-content" style="display: none; background: #f8f9fa; padding: 15px; border-radius: 5px; margin-top: 10px; border-left: 4px solid #007bff; white-space: pre-wrap; font-family: monospace; max-height: 500px; overflow-y: auto;">
                {template['content']}
            </div>
        </div>
        '''
    
    return f'''
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{
                font-family: Arial, sans-serif;
                padding: 20px;
                max-width: 1000px;
                margin: 0 auto;
            }}
            .template-card {{
                border: 1px solid #ddd;
                padding: 20px;
                margin: 15px 0;
                border-radius: 8px;
                background: white;
                box-shadow: 0 2px 5px rgba(0,0,0,0.1);
            }}
            .copy-button, .send-button {{
                transition: all 0.3s;
            }}
            .copy-button:hover {{
                background: #218838 !important;
            }}
            .send-button:hover {{
                background: #0056b3 !important;
            }}
            .back-button {{
                display: inline-block;
                margin-top: 20px;
                padding: 10px 20px;
                background: #6c757d;
                color: white;
                text-decoration: none;
                border-radius: 5px;
            }}
            .notification {{
                position: fixed;
                top: 20px;
                right: 20px;
                padding: 15px;
                background: #28a745;
                color: white;
                border-radius: 5px;
                display: none;
                z-index: 1000;
            }}
        </style>
    </head>
    <body>
        <h1>🏋️ Профессиональные программы тренировок</h1>
        <p><strong>⚠️ Внимание:</strong> Все программы оптимизированы для Telegram (до 4096 символов)</p>
        
        {templates_html}
        
        <div class="notification" id="notification">
            📋 Программа скопирована в буфер обмена!
        </div>
        
        <a href="/dashboard" class="back-button">← Назад в дашборд</a>
        
        <script>
            function copyTemplate(templateId) {{
                const templateContent = document.getElementById('template-' + templateId).innerText;
                navigator.clipboard.writeText(templateContent).then(() => {{
                    showNotification('Программа скопирована в буфер обмена!');
                }});
            }}
            
            function sendToPost(templateId) {{
                const templateContent = document.getElementById('template-' + templateId).innerText;
                localStorage.setItem('templateContent', templateContent);
                window.location.href = '/create-post';
            }}
            
            function toggleContent(templateId) {{
                const contentDiv = document.getElementById('template-' + templateId);
                const toggleBtn = document.getElementById('toggle-btn-' + templateId);
                
                if (contentDiv.style.display === 'none') {{
                    contentDiv.style.display = 'block';
                    toggleBtn.textContent = '👁 Скрыть программу';
                    toggleBtn.style.background = '#dc3545';
                }} else {{
                    contentDiv.style.display = 'none';
                    toggleBtn.textContent = '👁 Показать программу';
                    toggleBtn.style.background = '#6c757d';
                }}
            }}
            
            function showNotification(message) {{
                const notification = document.getElementById('notification');
                notification.textContent = message;
                notification.style.display = 'block';
                setTimeout(() => {{
                    notification.style.display = 'none';
                }}, 3000);
            }}
            
            // Проверяем, есть ли сохраненный шаблон при загрузке страницы создания поста
            if (window.location.pathname === '/create-post') {{
                window.addEventListener('load', function() {{
                    const savedContent = localStorage.getItem('templateContent');
                    if (savedContent) {{
                        document.getElementById('content').value = savedContent;
                        localStorage.removeItem('templateContent');
                        showNotification('Шаблон загружен в редактор!');
                    }}
                }});
            }}
        </script>
    </body>
    </html>
    '''

@app.route('/health')
def health():
    """Health check для Render"""
    return jsonify({
        "status": "healthy",
        "service": "training-plans-dashboard",
        "timestamp": datetime.now().isoformat(),
        "database": "sqlite-in-memory",
        "telegram_configured": bool(Config.TELEGRAM_BOT_TOKEN and Config.TELEGRAM_CHANNEL_ID)
    })

@app.route('/test')
def test():
    """Тестовая страница"""
    return "✅ Приложение работает корректно!"

# ==================== ЗАПУСК ПРИЛОЖЕНИЯ ====================
if __name__ == '__main__':
    init_database()
    
    logger.info("=" * 50)
    logger.info("🚀 Запуск приложения Тренировки Pro")
    logger.info("=" * 50)
    
    if Config.TELEGRAM_BOT_TOKEN:
        logger.info("✅ TELEGRAM_BOT_TOKEN: настроен")
    else:
        logger.warning("⚠️ TELEGRAM_BOT_TOKEN: не настроен")
    
    if Config.TELEGRAM_CHANNEL_ID:
        logger.info(f"✅ TELEGRAM_CHANNEL_ID: {Config.TELEGRAM_CHANNEL_ID}")
    else:
        logger.warning("⚠️ TELEGRAM_CHANNEL_ID: не настроен")
    
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"🌐 Запуск на порту: {port}")
    
    app.run(host='0.0.0.0', port=port, debug=False)
