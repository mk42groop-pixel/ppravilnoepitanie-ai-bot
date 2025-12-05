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
    """Создаем базу данных в памяти при каждом запуске"""
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
        
        return app.config['DATABASE_CONN'], app.config['DATABASE_CURSOR']
        
    except Exception as e:
        logger.error(f"❌ Ошибка получения соединения с БД: {e}")
        init_database()
        return app.config.get('DATABASE_CONN'), app.config.get('DATABASE_CURSOR')

# ==================== TELEGRAM ФУНКЦИИ ====================
def send_to_telegram_sync(title, content):
    """Отправка сообщения в Telegram (синхронная обертка)"""
    try:
        if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHANNEL_ID:
            logger.warning("⚠️ Telegram не настроен")
            return False
        
        async def send_async():
            bot = Bot(token=Config.TELEGRAM_BOT_TOKEN)
            message = f"<b>{title}</b>\n\n{content}"
            await bot.send_message(
                chat_id=Config.TELEGRAM_CHANNEL_ID,
                text=message,
                parse_mode='HTML'
            )
            return True
        
        # Запускаем асинхронную функцию
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(send_async())
        loop.close()
        
        logger.info("✅ Сообщение отправлено в Telegram")
        return result
    
    except Exception as e:
        logger.error(f"❌ Ошибка отправки в Telegram: {e}")
        return False

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
            
            if user and check_password_hash(user[2], password):
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
            body {{ font-family: Arial, sans-serif; padding: 20px; max-width: 1000px; margin: 0 auto; }}
            .status {{ padding: 15px; margin: 10px 0; border-radius: 5px; }}
            .success {{ background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
            .danger {{ background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
            .action-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin: 30px 0; }}
            .action-card {{ padding: 20px; background: white; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); text-align: center; }}
            .action-card a {{ display: block; padding: 15px; background: #007bff; color: white; text-decoration: none; border-radius: 5px; margin-top: 10px; }}
            .action-card a:hover {{ background: #0056b3; }}
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
            <a href="/dashboard">Назад в дашборд</a>
        </body>
        </html>
        '''
    
    # Тестовое сообщение
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
            body {{ font-family: Arial, sans-serif; padding: 20px; }}
            a {{ display: inline-block; margin-top: 20px; padding: 10px 20px; 
                background: #007bff; color: white; text-decoration: none; border-radius: 5px; }}
        </style>
    </head>
    <body>
        <h1>Тест Telegram подключения</h1>
        {message}
        <a href="/dashboard">Назад в дашборд</a>
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
        
        if not title or not content:
            return '''
            <html>
            <body>
                <h1>Ошибка</h1>
                <p>Заполните все поля!</p>
                <a href="/create-post">Назад</a>
            </body>
            </html>
            '''
        
        # Отправляем в Telegram
        success = send_to_telegram_sync(title, content)
        
        if success:
            message = f'''
            <div style="padding: 20px; background: #d4edda; border-radius: 5px;">
                <strong>✅ Пост опубликован в Telegram!</strong>
                <p>Заголовок: {title}</p>
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
            <a href="/dashboard">В дашборд</a> | 
            <a href="/create-post">Создать еще один пост</a>
        </body>
        </html>
        '''
    
    # GET запрос - показываем форму
    return '''
    <html>
    <head>
        <style>
            body { font-family: Arial, sans-serif; padding: 20px; max-width: 800px; margin: 0 auto; }
            form { background: #f8f9fa; padding: 20px; border-radius: 5px; }
            input, textarea { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 4px; }
            button { background: #28a745; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; }
            a { display: inline-block; margin-top: 20px; padding: 10px 20px; background: #007bff; color: white; text-decoration: none; border-radius: 5px; }
        </style>
    </head>
    <body>
        <h1>📝 Создать новый пост</h1>
        <form method="POST">
            <label for="title"><strong>Заголовок:</strong></label>
            <input type="text" id="title" name="title" placeholder="Введите заголовок поста" required>
            
            <label for="content"><strong>Содержание:</strong></label>
            <textarea id="content" name="content" rows="10" placeholder="Введите текст поста..." required></textarea>
            
            <button type="submit">📤 Опубликовать в Telegram</button>
        </form>
        <a href="/dashboard">← Назад в дашборд</a>
    </body>
    </html>
    '''

@app.route('/templates')
def templates():
    """Шаблоны тренировок"""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    templates_list = [
        {
            'name': 'Атлетическое телосложение 46+',
            'description': 'Для мужчины 46 лет, 82 кг, 182 см',
            'content': 'Трехдневный сплит, фокус на технике'
        },
        {
            'name': 'Начальный уровень для подростка',
            'description': 'Для подростка 15 лет, 167 см, 45 кг',
            'content': 'Безопасные тренировки без весов'
        }
    ]
    
    html = '''
    <html>
    <head>
        <style>
            body { font-family: Arial, sans-serif; padding: 20px; }
            .template { border: 1px solid #ddd; padding: 15px; margin: 10px 0; border-radius: 5px; }
            .template h3 { margin-top: 0; }
            a { display: inline-block; margin-top: 20px; padding: 10px 20px; background: #007bff; color: white; text-decoration: none; border-radius: 5px; }
        </style>
    </head>
    <body>
        <h1>🏋️ Шаблоны тренировок</h1>
    '''
    
    for template in templates_list:
        html += f'''
        <div class="template">
            <h3>{template['name']}</h3>
            <p><strong>Описание:</strong> {template['description']}</p>
            <p><strong>Содержание:</strong> {template['content']}</p>
            <button onclick="useTemplate('{template['name']}', '{template['content']}')">Использовать шаблон</button>
        </div>
        '''
    
    html += '''
        <a href="/dashboard">← Назад в дашборд</a>
        
        <script>
        function useTemplate(name, content) {
            document.getElementById('title').value = name;
            document.getElementById('content').value = content;
            window.location.href = '/create-post';
        }
        </script>
    </body>
    </html>
    '''
    
    return html

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
    # Инициализируем базу данных
    init_database()
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
