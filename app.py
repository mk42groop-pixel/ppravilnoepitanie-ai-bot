import os
import sqlite3
import logging
from datetime import datetime
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Инициализация Flask
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

# Конфигурация
class Config:
    TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHANNEL_ID = os.environ.get('TELEGRAM_CHANNEL_ID', '')

# Инициализация базы данных (в памяти для надежности)
def init_database():
    """Создаем базу данных в памяти при каждом запуске"""
    try:
        # Используем SQLite в памяти для избежания проблем с файловой системой
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
        
        # Добавляем администратора если его нет
        cursor.execute('SELECT * FROM users WHERE username = ?', ('admin',))
        if not cursor.fetchone():
            password_hash = generate_password_hash('admin123')
            cursor.execute(
                'INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)',
                ('admin', password_hash, 'admin')
            )
            logger.info("✅ Администратор создан: admin / admin123")
        
        conn.commit()
        
        # Сохраняем соединение в глобальной переменной
        app.config['DATABASE_CONN'] = conn
        app.config['DATABASE_CURSOR'] = cursor
        
        logger.info("✅ База данных инициализирована в памяти")
        
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации базы данных: {e}")

def get_db_connection():
    """Получаем соединение с базой данных"""
    try:
        if 'DATABASE_CONN' not in app.config:
            init_database()
        
        # Проверяем, что соединение активно
        conn = app.config['DATABASE_CONN']
        cursor = app.config['DATABASE_CURSOR']
        
        # Создаем таблицу users если она вдруг не создана
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'editor',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        return conn, cursor
        
    except Exception as e:
        logger.error(f"❌ Ошибка получения соединения с БД: {e}")
        # Если что-то пошло не так, создаем новое соединение
        init_database()
        return app.config.get('DATABASE_CONN'), app.config.get('DATABASE_CURSOR')

# Маршруты
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
            
            # Получаем соединение с БД
            conn, cursor = get_db_connection()
            
            # Создаем таблицу users если она не существует
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT DEFAULT 'editor',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Проверяем, есть ли пользователи в базе
            cursor.execute('SELECT COUNT(*) FROM users')
            user_count = cursor.fetchone()[0]
            
            # Если база пустая, создаем администратора
            if user_count == 0:
                password_hash = generate_password_hash('admin123')
                cursor.execute(
                    'INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)',
                    ('admin', password_hash, 'admin')
                )
                conn.commit()
                logger.info("✅ Администратор добавлен в пустую базу")
            
            # Ищем пользователя
            cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
            user = cursor.fetchone()
            
            if user and check_password_hash(user[2], password):  # user[2] = password_hash
                session['user_id'] = user[0]
                session['username'] = user[1]
                session['role'] = user[3]
                logger.info(f"✅ Пользователь {username} вошел в систему")
                return redirect(url_for('dashboard'))
            
            logger.warning(f"⚠️ Неудачная попытка входа: {username}")
            return render_template('login.html', error='Неверное имя пользователя или пароль')
        
        return render_template('login.html')
    
    except Exception as e:
        logger.error(f"❌ Ошибка в login: {e}")
        return f'''
        <html>
        <body>
            <h1>Ошибка базы данных</h1>
            <p>Перезагрузите страницу или попробуйте снова через минуту.</p>
            <p>Ошибка: {str(e)}</p>
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
    
    telegram_status = "✅ Настроен" if Config.TELEGRAM_BOT_TOKEN else "❌ Не настроен"
    channel_status = "✅ Настроен" if Config.TELEGRAM_CHANNEL_ID else "❌ Не настроен"
    
    return f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Дашборд тренировок</title>
        <style>
            body {{ font-family: Arial, sans-serif; padding: 20px; }}
            .status {{ padding: 10px; margin: 10px 0; border-radius: 5px; }}
            .success {{ background: #d4edda; color: #155724; }}
            .warning {{ background: #fff3cd; color: #856404; }}
            .danger {{ background: #f8d7da; color: #721c24; }}
            a {{ display: inline-block; margin-top: 20px; padding: 10px 20px; background: #007bff; color: white; text-decoration: none; border-radius: 5px; }}
        </style>
    </head>
    <body>
        <h1>📊 Дашборд тренировок</h1>
        <p>Привет, <strong>{session.get("username")}</strong>!</p>
        
        <h2>Статус подключений:</h2>
        
        <div class="status {'success' if Config.TELEGRAM_BOT_TOKEN else 'danger'}">
            <strong>Telegram бот:</strong> {telegram_status}
        </div>
        
        <div class="status {'success' if Config.TELEGRAM_CHANNEL_ID else 'danger'}">
            <strong>Telegram канал:</strong> {channel_status}
        </div>
        
        <h3>Быстрые действия:</h3>
        <ul>
            <li><a href="/test-telegram" style="background: #28a745;">📡 Проверить Telegram</a></li>
            <li><a href="/posts" style="background: #17a2b8;">📝 Управление постами</a></li>
            <li><a href="/logout" style="background: #dc3545;">🚪 Выйти</a></li>
        </ul>
    </body>
    </html>
    '''

@app.route('/test-telegram')
def test_telegram():
    """Тест Telegram подключения"""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    return '''
    <html>
    <body>
        <h1>Тест Telegram</h1>
        <p>Функция тестирования Telegram будет доступна после настройки TELEGRAM_BOT_TOKEN.</p>
        <p>Добавьте токен в переменные окружения на Render.</p>
        <a href="/dashboard">Назад в дашборд</a>
    </body>
    </html>
    '''

@app.route('/posts')
def posts():
    """Страница постов"""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    return '''
    <html>
    <body>
        <h1>Управление постами</h1>
        <p>Функция управления постами будет доступна после настройки Telegram.</p>
        <a href="/dashboard">Назад в дашборд</a>
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

@app.route('/debug/db')
def debug_db():
    """Отладка базы данных"""
    try:
        conn, cursor = get_db_connection()
        
        # Пытаемся создать таблицу
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS debug_test (
                id INTEGER PRIMARY KEY,
                message TEXT
            )
        ''')
        
        # Добавляем тестовую запись
        cursor.execute('INSERT INTO debug_test (message) VALUES (?)', ('Тестовая запись',))
        conn.commit()
        
        # Читаем запись
        cursor.execute('SELECT * FROM debug_test')
        result = cursor.fetchall()
        
        return jsonify({
            "status": "success",
            "database": "working",
            "test_data": result
        })
        
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        })

# Инициализация при запуске
if __name__ == '__main__':
    # Инициализируем базу данных при запуске
    init_database()
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
