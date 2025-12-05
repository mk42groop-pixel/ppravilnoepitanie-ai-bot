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
            .warning {{ background: #fff3cd; color: #856404; border: 1px solid #ffeaa7; }}
            .danger {{ background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
            .action-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin: 30px 0; }}
            .action-card {{ padding: 20px; background: white; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); text-align: center; }}
            .action-card a {{ display: block; padding: 15px; background: #007bff; color: white; text-decoration: none; border-radius: 5px; margin-top: 10px; }}
            .action-card a:hover {{ background: #0056b3; }}
        </style>
    </head>
    <body>
        <h1>📊 Дашборд тренировок</h1>
        <p>Привет, <strong>{session.get("username")}</strong>! Добро пожаловать в панель управления.</p>
        
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
        
        <h3>Инструкция:</h3>
        <ol>
            <li>Нажмите "Проверить" чтобы отправить тестовое сообщение</li>
            <li>Если тест успешен - создавайте посты</li>
            <li>Используйте готовые шаблоны тренировок</li>
        </ol>
    </body>
    </html>
    '''
