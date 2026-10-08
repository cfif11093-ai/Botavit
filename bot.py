import os
import sys
import traceback
import time
import threading
import re
import sqlite3
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

try:
    import telebot
    from telebot import types
    from bs4 import BeautifulSoup
    import cloudscraper
except ImportError as e:
    print(f"❌ Ошибка импорта библиотек: {e}")
    sys.exit(1)

# Токен берется из переменных Render или из значения по умолчанию
BOT_TOKEN = os.getenv("BOT_TOKEN", "8960373658:AAGx8LfFv2Kp-5qWQhhDHDGFvKwL0NmOEcY")
ADMIN_ID = 1218062817

bot = telebot.TeleBot(BOT_TOKEN)
DB_NAME = 'bot_database.db'

# --- 1. Легкий HTTP-сервер для прохождения проверки Render (Health Check) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Avito Bot is running!")

    def log_message(self, format, *args):
        return

def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=start_health_server, daemon=True).start()

# --- 2. Константы категорий ---
AVITO_CATS = {
    "cat_all": "", 
    "cat_transport": "transport",
    "cat_nedvizhimost": "nedvizhimost",
    "cat_rabota": "rabota",
    "cat_uslugi": "uslugi",
    "cat_veshi": "lichnye_veschi",
    "cat_dom": "dlya_doma_i_dachi",
    "cat_elektronika": "elektronika",
    "cat_hobbi": "hobbi_i_otdyh",
    "cat_zhivotnye": "zhivotnye"
}

CAT_NAMES = {
    "cat_all": "🌍 Все категории",
    "cat_transport": "🚗 Транспорт",
    "cat_nedvizhimost": "🏢 Недвижимость",
    "cat_rabota": "💼 Работа",
    "cat_uslugi": "🛠 Услуги",
    "cat_veshi": "🎽 Личные вещи",
    "cat_dom": "🛋 Для дома и дачи",
    "cat_elektronika": "💻 Электроника",
    "cat_hobbi": "🎸 Хобби и отдых",
    "cat_zhivotnye": "🐾 Животные"
}

# --- 3. База данных SQLite ---
def init_db():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY, free_searches INTEGER DEFAULT 30,
            sub_expires REAL DEFAULT 0, query TEXT DEFAULT 'Не задан',
            max_price INTEGER DEFAULT NULL, state TEXT DEFAULT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sent_ads (
            url TEXT PRIMARY KEY
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def get_db_connection():
    return sqlite3.connect(DB_NAME, check_same_thread=False)

def is_ad_sent(url):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT url FROM sent_ads WHERE url = ?', (url,))
    res = cursor.fetchone()
    conn.close()
    return bool(res)

def mark_ad_sent(url):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO sent_ads (url) VALUES (?)', (url,))
    conn.commit()
    conn.close()

def get_user(chat_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT chat_id, free_searches, sub_expires, query, max_price, state FROM users WHERE chat_id = ?', (chat_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute('INSERT INTO users (chat_id, free_searches, sub_expires, query, max_price, state) VALUES (?, 30, 0, ?, NULL, NULL)', (chat_id, 'Не задан'))
        conn.commit()
        row = (chat_id, 30, 0, 'Не задан', None, None)
    conn.close()
    return {'chat_id': row[0], 'free_searches': row[1], 'sub_expires': row[2], 'query': row[3], 'max_price': row[4], 'state': row[5]}

def update_user(chat_id, **kwargs):
    get_user(chat_id)
    conn = get_db_connection()
    cursor = conn.cursor()
    fields = [f"{k} = ?" for k in kwargs]
    values = list(kwargs.values()) + [chat_id]
    cursor.execute(f"UPDATE users SET {', '.join(fields)} WHERE chat_id = ?", tuple(values))
    conn.commit()
    conn.close()

def get_all_users():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT chat_id FROM users')
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]

sub_duration = 30 * 86400
broadcast_mode = set()

# --- 4. Логика ссылок и парсинга ---
def parse_user_input(text):
    match_price = re.search(r'(?:до|не дороже|цена|бюджет|=)?\s*(\d{4,6})', text, re.IGNORECASE)
    query = text
    max_price = None
    if match_price:
        max_price = int(match_price.group(1))
        query = text.replace(match_price.group(0), "").replace("до", "").replace("цена", "").replace("бюджет", "").strip()
    return query, max_price

def get_display_query(q):
    if not q or q == "cat_all": return "🌍 Любые новые товары на Авито"
    if q in CAT_NAMES: return f"Вся категория «{CAT_NAMES[q]}»"
    if "|" in q:
        cat, real_q = q.split("|", 1)
        cat_name = CAT_NAMES.get(cat, cat)
        if cat == "cat_all": return f"«{real_q}» (во всех категориях)"
        return f"«{real_q}» (в разделе {cat_name})"
    return q

def build_avito_link(query, max_price):
    if query == "cat_all":
        url = "https://www.avito.ru/all?s=104"
    elif query in AVITO_CATS:
        cat_path = AVITO_CATS[query]
        url = f"https://www.avito.ru/all/{cat_path}?s=104" if cat_path else "https://www.avito.ru/all?s=104"
    elif "|" in query:
        cat, real_q = query.split("|", 1)
        encoded_q = urllib.parse.quote(real_q)
        cat_path = AVITO_CATS.get(cat, "")
        if cat_path:
            url = f"https://www.avito.ru/all/{cat_path}?q={encoded_q}&s=104"
        else:
            url = f"https://www.avito.ru/all?q={encoded_q}&s=104"
    else:
        encoded_q = urllib.parse.quote(query)
        url = f"https://www.avito.ru/all?q={encoded_q}&s=104"
        
    if max_price:
        url += f"&pmax={max_price}"
    return url

def get_avito_ads(query, max_price, max_count=3):
    ads = []
    search_url = build_avito_link(query, max_price)

    try:
        scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True})
        resp = scraper.get(search_url, timeout=15)
        
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            items = soup.find_all('div', {'data-marker': 'item'})
            
            for item in items:
                link_tag = item.find('a', {'itemprop': 'url'})
                if not link_tag: continue
                
                link = "https://www.avito.ru" + link_tag.get('href', '')
                title_tag = item.find('h3', {'itemprop': 'name'})
                title = title_tag.text.strip() if title_tag else "Объявление"
                
                price_tag = item.find('meta', {'itemprop': 'price'})
                item_price = int(price_tag.get('content', 0)) if price_tag else None
                
                if max_price and item_price and item_price > max_price: 
                    continue
                
                if not is_ad_sent(link):
                    mark_ad_sent(link)
                    safe_title = title.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    p_text = f"💰 <b>{item_price:,} руб.</b>".replace(',', ' ') if item_price else "💰 Цена в объявлении"
                    
                    ads.append({
                        "text": f"🔥 <b>Только что выложили!</b>\n\n📌 <b>{safe_title}</b>\n{p_text}",
                        "url": link
                    })
                    if len(ads) >= max_count: 
                        break
        else:
            print(f"Статус ответа Авито: {resp.status_code}")
    except Exception as e:
        print(f"Ошибка парсинга: {e}")
        
    return ads

def is_paid_active(chat_id):
    return get_user(chat_id)['sub_expires'] > time.time()

# --- 5. Клавиатуры ---
def get_avito_categories_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("🌍 Все категории", callback_data="cat_all"))
    btns = [types.InlineKeyboardButton(name, callback_data=cat) for cat, name in CAT_NAMES.items() if cat != "cat_all"]
    markup.add(*btns)
    markup.add(types.InlineKeyboardButton("💎 Безлимит (150 ⭐)", callback_data="buy_sub_menu"))
    return markup

def get_bottom_reply_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        types.KeyboardButton("🔍 Новый поиск"),
        types.KeyboardButton("📊 Мой профиль"),
        types.KeyboardButton("🔔 Проверить сейчас"),
        types.KeyboardButton("💡 Помощь")
    )
    return markup

# --- 6. Хэндлеры команд ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    chat_id = message.chat.id
    user = get_user(chat_id)
    status = "💎 <b>Статус:</b> Безлимит" if is_paid_active(chat_id) else f"🎁 <b>Бесплатные попытки:</b> {user['free_searches']} из 30"
    bot.send_message(chat_id, f"👋 <b>Добро пожаловать!</b>\n\nЯ отправляю 100% свежие товары по вашему запросу.\n\n📂 Выберите категорию или нажмите <b>«🔍 Новый поиск»</b>:\n\n{status}", parse_mode='HTML', reply_markup=get_avito_categories_keyboard())
    bot.send_message(chat_id, "👇 Панель управления:", reply_markup=get_bottom_reply_keyboard())

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.chat.id != ADMIN_ID: return
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast"))
    bot.send_message(message.chat.id, f"🛠 <b>Админ-панель:</b>\n👥 Пользователей: <b>{len(get_all_users())}</b>", parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    chat_id = call.message.chat.id
    if call.data == "admin_broadcast" and chat_id == ADMIN_ID:
        broadcast_mode.add(chat_id)
        bot.send_message(chat_id, "✍️ Введите текст рассылки:")
        return
        
    if call.data == "buy_sub_menu":
        bot.send_invoice(chat_id, title="Безлимит Avito", description="Доступ на 30 дней", invoice_payload="sub", provider_token="", currency="XTR", prices=[types.LabeledPrice(label="Безлимит", amount=150)])
        return
        
    if call.data in CAT_NAMES:
        update_user(chat_id, state=call.data)
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("👀 Искать ВСЁ в этой категории", callback_data=f"all_{call.data}"))
        bot.send_message(
            chat_id, 
            f"✅ Вы выбрали раздел: <b>{CAT_NAMES[call.data]}</b>\n\n✍️ Напишите конкретный товар (например: <i>RTX 3060 до 25000</i>)\n\n👇 <b>ИЛИ</b> нажмите кнопку ниже, чтобы мониторить <b>вообще все новые объявления</b> в этом разделе:", 
            parse_mode='HTML', 
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)
        return
        
    if call.data.startswith("all_cat_"):
        cat = call.data.replace("all_", "")
        user = get_user(chat_id)
        paid = is_paid_active(chat_id)
        
        if not paid and user['free_searches'] <= 0:
            bot.answer_callback_query(call.id, "Лимит исчерпан", show_alert=True)
            return bot.send_message(chat_id, "⏳ Лимит исчерпан. Оформите безлимит.")
        
        update_user(chat_id, free_searches=user['free_searches'] - (0 if paid else 1), query=cat, max_price=None, state=None)
        bot.answer_callback_query(call.id, "✅ Глобальный поиск запущен!")
        bot.send_message(chat_id, f"🎯 <b>Поиск запущен:</b> {get_display_query(cat)}\n🔔 Ждем новые поступления. Бот напишет сразу же, как только товар появится на Авито.", parse_mode='HTML')
        
        ads = get_avito_ads(cat, None, max_count=2)
        for ad in ads:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔗 Открыть объявление", url=ad["url"]))
            bot.send_message(chat_id, ad["text"], parse_mode='HTML', reply_markup=markup)
            time.sleep(0.3)
        return

@bot.message_handler(func=lambda m: m.text == "🔍 Новый поиск")
def menu_search(message):
    update_user(message.chat.id, state="general_search")
    bot.send_message(message.chat.id, "🔍 Напишите название товара и желаемую цену:")

@bot.message_handler(func=lambda m: m.text == "📊 Мой профиль")
def menu_profile(message):
    user = get_user(message.chat.id)
    stat = "💎 Безлимит" if is_paid_active(message.chat.id) else f"🎁 {user['free_searches']} из 30 проверок"
    price_info = f" до {user['max_price']} руб." if user['max_price'] else ""
    bot.send_message(message.chat.id, f"👤 <b>Профиль</b>\n📌 Поиск: {get_display_query(user['query'])}{price_info}\n📊 Статус: {stat}", parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🔔 Проверить сейчас")
def menu_notifications(message):
    chat_id = message.chat.id
    user = get_user(chat_id)
    if not user['query'] or user['query'] == 'Не задан':
        return bot.send_message(chat_id, "⚠️ Настройте поиск.")

    bot.send_message(chat_id, f"🔍 Ищем новые поступления: <b>{get_display_query(user['query'])}</b>...", parse_mode='HTML')
    ads = get_avito_ads(user['query'], user['max_price'], max_count=3)
    
    if ads:
        for ad in ads:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔗 Открыть объявление", url=ad["url"]))
            bot.send_message(chat_id, ad["text"], parse_mode='HTML', reply_markup=markup)
            time.sleep(0.3)
    else:
        bot.send_message(chat_id, "⏳ <b>Новых товаров пока нет</b>. Бот проверяет рынок в фоне и напишет, как только появится свежак.", parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "💡 Помощь")
def menu_help(message):
    bot.send_message(message.chat.id, "💡 Выберите категорию или введите товар. Бот сам пришлет 100% новые объявления.")

@bot.pre_checkout_query_handler(func=lambda q: True)
def checkout(q): bot.answer_pre_checkout_query(q.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def got_payment(message):
    update_user(message.chat.id, sub_expires=time.time() + sub_duration)
    bot.send_message(message.chat.id, "🎉 <b>Оплата успешна!</b>", parse_mode='HTML')

@bot.message_handler(func=lambda m: True)
def handle_text(message):
    chat_id = message.chat.id
    text = message.text.strip()
    
    if chat_id == ADMIN_ID and chat_id in broadcast_mode:
        broadcast_mode.remove(chat_id)
        for uid in get_all_users():
            try: bot.send_message(uid, f"📢 {text}")
            except: pass
        return bot.send_message(chat_id, "✅ Разослано.")

    user = get_user(chat_id)
    paid = is_paid_active(chat_id)
    
    if not paid and user['free_searches'] <= 0:
        return bot.send_message(chat_id, "⏳ Лимит исчерпан. Оформите безлимит.")

    text_q, m_price = parse_user_input(text)
    state = user['state']
    
    if state and state in CAT_NAMES:
        final_query = f"{state}|{text_q}" if text_q else state
    else:
        final_query = text_q if text_q else ""

    update_user(chat_id, free_searches=user['free_searches'] - (0 if paid else 1), query=final_query, max_price=m_price, state=None)
    
    bot.send_message(chat_id, f"🎯 <b>Поиск запущен:</b> {get_display_query(final_query)}\n🔔 Ждем новые поступления.", parse_mode='HTML')
    
    ads = get_avito_ads(final_query, m_price, max_count=2)
    for ad in ads:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔗 Открыть объявление", url=ad["url"]))
        bot.send_message(chat_id, ad["text"], parse_mode='HTML', reply_markup=markup)
        time.sleep(0.3)

# --- 7. Фоновый цикл проверки ---
def bg_loop():
    while True:
        time.sleep(65)
        for cid in get_all_users():
            try:
                user = get_user(cid)
                if is_paid_active(cid) or user['free_searches'] > 0:
                    if user['query'] and user['query'] != 'Не задан':
                        ads = get_avito_ads(user['query'], user['max_price'], max_count=1)
                        for ad in ads:
                            markup = types.InlineKeyboardMarkup()
                            markup.add(types.InlineKeyboardButton("🔗 Открыть", url=ad["url"]))
                            bot.send_message(cid, f"🔔 <b>Новое по запросу:</b> <i>{get_display_query(user['query'])}</i>\n\n{ad['text']}", parse_mode='HTML', reply_markup=markup)
                            time.sleep(0.5)
            except: pass

if __name__ == '__main__':
    threading.Thread(target=bg_loop, daemon=True).start()
    print("✅ Авито-бот успешно запущен на Render и слушает Telegram...")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
