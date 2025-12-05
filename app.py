import os
import sqlite3
import json
import logging
from datetime import datetime
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
import asyncio

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
    DATABASE_URL = 'training_plans.db'
    
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

# Инициализация базы данных
def init_database():
    conn = sqlite3.connect(Config.DATABASE_URL)
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

def get_db_connection():
    conn = sqlite3.connect(Config.DATABASE_URL)
    conn.row_factory = sqlite3.Row
    return conn

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
    
    except Exception as e:
        logger.error(f"❌ Ошибка в login: {e}")
        return f"Ошибка сервера: {e}", 500

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
    <head><title>Дашборд</title></head>
    <body>
        <h1>📊 Дашборд тренировок</h1>
        <p>Привет, {session.get("username")}!</p>
        <h2>Статус подключений:</h2>
        <p>Telegram бот: {telegram_status}</p>
        <p>Telegram канал: {channel_status}</p>
        <a href="/logout">Выйти</a>
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
        "telegram_configured": bool(Config.TELEGRAM_BOT_TOKEN and Config.TELEGRAM_CHANNEL_ID)
    })

@app.route('/test')
def test():
    """Тестовая страница"""
    return "✅ Приложение работает корректно!"

# Инициализация при запуске
if __name__ == '__main__':
    Config.validate()
    with app.app_context():
        init_database()
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
