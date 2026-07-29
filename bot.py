import os
import logging
import requests
import xml.etree.ElementTree as ET
import gspread
import json
from datetime import datetime, date
from flask import Flask
from threading import Thread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters, ConversationHandler
)

# ===== НАСТРОЙКИ =====
BOT_TOKEN = os.environ.get("BOT_TOKEN")
SHEET_ID = os.environ.get("SHEET_ID")
GOOGLE_CREDS = os.environ.get("GOOGLE_CREDS")
BOSS_ID = 456141836
CARS_PER_PAGE = 10

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== СОСТОЯНИЯ =====
(
    SET_PASSWORD, ENTER_PASSWORD, OLD_PASSWORD,
    ADD_CAR_MARK_MODEL, ADD_CAR_YEAR, ADD_CAR_COLOR,
    ADD_CAR_VIN, ADD_CAR_CLIENT, ADD_CAR_CLIENT_TYPE,
    ADD_CAR_COMPANY,
    EDIT_CAR_FIELD, EDIT_CAR_VALUE, EDIT_CAR_CLIENT_TYPE,
    EDIT_CAR_COMPANY,
    DELETE_CAR_CONFIRM,
    PAY_CAR, PAY_CATEGORY, PAY_AMOUNT, PAY_COMMENT,
    DEBT_CAR, DEBT_WHO, DEBT_AMOUNT,
    SAL_NAME, SAL_OKLAD, SAL_BONUS, SAL_MONTH,
    REPORT_CAR, CAR_SEARCH
) = range(28)

# ===== FLASK =====
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# ===== GOOGLE SHEETS =====
_spreadsheet = None

def get_spreadsheet():
    global _spreadsheet
    try:
        if _spreadsheet is not None:
            return _spreadsheet
        creds_dict = json.loads(GOOGLE_CREDS)
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_info(
            creds_dict, scopes=scopes
        )
        client = gspread.authorize(creds)
        _spreadsheet = client.open_by_key(SHEET_ID)
        logger.info("Google Sheets подключён")
        return _spreadsheet
    except Exception as e:
        logger.error(f"Sheets connection error: {e}")
        _spreadsheet = None
        return None

def get_worksheet(name):
    global _spreadsheet
    try:
        sh = get_spreadsheet()
        if sh:
            return sh.worksheet(name)
    except Exception as e:
        logger.error(f"get_worksheet({name}) error: {e}")
        _spreadsheet = None
    return None

def reset_connection():
    global _spreadsheet
    _spreadsheet = None

def get_next_id(sheet_name, prefix):
    try:
        ws = get_worksheet(sheet_name)
        if not ws:
            return f"{prefix}-001"
        values = ws.col_values(1)[1:]
        existing = [v for v in values if v.startswith(prefix)]
        if not existing:
            return f"{prefix}-001"
        nums = []
        for v in existing:
            try:
                nums.append(int(v.split("-")[1]))
            except:
                pass
        if not nums:
            return f"{prefix}-001"
        return f"{prefix}-{str(max(nums) + 1).zfill(3)}"
    except Exception as e:
        logger.error(f"get_next_id error: {e}")
        return f"{prefix}-001"

def get_all_cars():
    """
    Получить все машины.
    Сортировка: старые вверху (по дате добавления),
    новые внизу.
    """
    try:
        ws = get_worksheet("МАШИНЫ")
        if not ws:
            return []
        records = ws.get_all_records()
        # Сортируем по дате — старые вверху
        def parse_date(r):
            try:
                return datetime.strptime(
                    r.get("Дата добавления", "01.01.2000"),
                    "%d.%m.%Y"
                )
            except:
                return datetime(2000, 1, 1)
        records.sort(key=parse_date)
        return records
    except Exception as e:
        logger.error(f"get_all_cars error: {e}")
        return []

def get_car_by_id(car_id):
    """Получить одну машину по ID"""
    cars = get_all_cars()
    return next(
        (c for c in cars if c.get("ID") == car_id), None
    )

# ===== КУРС ЕВРО ЦБ РФ =====
def get_cbr_rate(code):
    try:
        url = "https://www.cbr.ru/scripts/XML_daily.asp"
        r = requests.get(url, timeout=10)
        r.encoding = "windows-1251"
        root = ET.fromstring(r.text)
        for v in root.findall("Valute"):
            if v.find("CharCode").text == code:
                nominal = int(v.find("Nominal").text)
                value = float(
                    v.find("Value").text.replace(",", ".")
                )
                return value / nominal
    except Exception as e:
        logger.error(f"CBR error: {e}")
    return None

# ===== КУРС ЮАНЯ ВТБ =====
def get_vtb_yuan():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }
    try:
        url = (
            "https://www.vtb.ru/api/currency-exchange/table-info"
            "?contextItemId=%7B5A68BC3E-814E-4B85-8E63-D91582A4B831%7D"
            "&conversionPlace=online&conversionType=CurrencyCNY"
        )
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            for group in data.get("GroupedRates", []):
                for rate in group.get("MonoCurrencyRates", []):
                    if rate.get("CurrencyAbbreviation") == "CNY":
                        buy = rate.get("BankBuyAt")
                        sell = rate.get("BankSellAt")
                        if buy and sell:
                            return {
                                "buy": buy, "sell": sell,
                                "source": "ВТБ Интернет-банк"
                            }
    except Exception as e:
        logger.error(f"VTB online error: {e}")
    try:
        url = (
            "https://www.vtb.ru/api/currency-exchange/table-info"
            "?contextItemId=%7B5A68BC3E-814E-4B85-8E63-D91582A4B831%7D"
            "&conversionPlace=mobile&conversionType=CurrencyCNY"
        )
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            for group in data.get("GroupedRates", []):
                for rate in group.get("MonoCurrencyRates", []):
                    if rate.get("CurrencyAbbreviation") == "CNY":
                        buy = rate.get("BankBuyAt")
                        sell = rate.get("BankSellAt")
                        if buy and sell:
                            return {
                                "buy": buy, "sell": sell,
                                "source": "ВТБ Мобильный банк"
                            }
    except Exception as e:
        logger.error(f"VTB mobile error: {e}")
    cb = get_cbr_rate("CNY")
    if cb:
        return {
            "buy": round(cb * 0.98, 4),
            "sell": round(cb * 1.02, 4),
            "source": "ЦБ РФ ±2% (ВТБ недоступен)"
        }
    return None

# ===== ПОШЛИНЫ =====
def get_duty_rate(volume_cc, is_old):
    if not is_old:
        if volume_cc <= 1000: return 1.5
        elif volume_cc <= 1500: return 1.7
        elif volume_cc <= 1800: return 2.5
        elif volume_cc <= 2300: return 2.7
        elif volume_cc <= 3000: return 3.0
        else: return 3.6
    else:
        if volume_cc <= 1000: return 3.0
        elif volume_cc <= 1500: return 3.2
        elif volume_cc <= 1800: return 3.5
        elif volume_cc <= 2300: return 4.8
        elif volume_cc <= 3000: return 5.0
        else: return 5.7

def format_money(amount):
    return f"{int(round(amount)):,}".replace(",", " ")

def build_duty_table():
    euro_rate = get_cbr_rate("EUR")
    if not euro_rate:
        return None
    volumes = [
        660, 1000, 1200, 1300, 1400, 1500,
        1600, 1800, 2000, 2200, 2300,
        2400, 2500, 2700, 2800, 3000
    ]
    today = datetime.now().strftime("%d.%m.%Y")
    text = f"📊 *Расчёт таможенных пошлин*\n\n"
    text += f"📅 Дата: *{today}*\n"
    text += f"💶 Курс евро ЦБ: *{euro_rate:.2f} ₽*\n\n"
    text += "💡 *Проходные годы (3–5 лет)*\n"
    text += "```\n"
    text += "Объём  Ставка      Пошлина\n"
    text += "───────────────────────────\n"
    for v in volumes:
        rate = get_duty_rate(v, is_old=False)
        duty_rub = v * rate * euro_rate
        text += (
            f"{v:<5}  {rate}€/см³  "
            f"{format_money(duty_rub):>10} ₽\n"
        )
    text += "```\n\n"
    text += "💡 *Непроходные (старше 5 лет)*\n"
    text += "```\n"
    text += "Объём  Ставка      Пошлина\n"
    text += "───────────────────────────\n"
    for v in volumes:
        rate = get_duty_rate(v, is_old=True)
        duty_rub = v * rate * euro_rate
        text += (
            f"{v:<5}  {rate}€/см³  "
            f"{format_money(duty_rub):>10} ₽\n"
        )
    text += "```\n\n"
    text += "📌 Утильсбор до 160 л.с.: *5 200 ₽*\n"
    text += "_(младше 3 лет — 3 400 ₽)_\n\n"
    text += "📥 *Заказать авто:* https://t.me/avtoiskatelgroup\n"
    text += "📞 +7 995 870 33 09 (Кирилл)\n"
    text += "📞 +7 908 999 60 09 (Сергей)\n"
    text += "#РАСЧЁТ\\_ПОШЛИНЫ"
    return text

# ===== ИНСТРУКЦИИ =====
INSTRUCTION_MAIN = """
📖 *ИНСТРУКЦИЯ — ФИНАНСОВЫЙ РАЗДЕЛ*

*С чего начать:*
1️⃣ Добавь машину в раздел 🚗 *Машины*
2️⃣ Записывай платежи через ➕ *Добавить платёж*
3️⃣ Фиксируй долги в разделе ⚖️ *Долги*
4️⃣ Зарплаты — в разделе 👥 *Зарплаты*
5️⃣ Итоги смотри в 📊 *Отчёты*

❗ Нельзя добавить платёж или долг без машины.
"""

INSTRUCTION_CARS = """
📖 *ИНСТРУКЦИЯ — МАШИНЫ*

*Список машин:*
• Машины показываются кнопками по 10 штук
• Старые машины вверху, новые внизу
• Листай страницы кнопками ◀️ и ▶️

*🔍 Поиск:*
• Нажми кнопку Поиск
• Введи любое слово (марка, цвет, ФИО, ВИН...)
• Бот найдёт все подходящие машины

*➕ Добавить машину — шаги:*
1️⃣ Марка и модель: *Zeekr 001*
2️⃣ Год: *2024*
3️⃣ Цвет: *Белый*
4️⃣ ВИН (можно пропустить)
5️⃣ ФИО клиента по паспорту
6️⃣ Тип: Физлицо или Юрлицо
7️⃣ Если юрлицо → название компании

*При нажатии на машину:*
Открывается карточка с кнопками —
редактировать, отчёт, платёж, долги, удалить
"""

INSTRUCTION_PAY = """
📖 *ИНСТРУКЦИЯ — ПЛАТЕЖИ*

*Как добавить платёж:*
1️⃣ Выбери машину
2️⃣ Выбери категорию:

📥 *ДОХОДЫ:*
• 💰 Накрутка — прибыль босса (юани ¥)
• 💵 Допы от клиента (рубли ₽)

📤 *РАСХОДЫ:*
• 🚛 Автовоз (рубли ₽)
• 🏛 Таможенный брокер (рубли ₽)
• 🔧 Допы в Китае (юани ¥)
• ⛽ Бензин (рубли ₽)
• 💸 Кэшбэк юрику (юани ¥)
• 👤 % Менеджеру — 20 000₽ фикс

3️⃣ Введи сумму цифрами
4️⃣ Комментарий или «-» чтобы пропустить
"""

INSTRUCTION_DEBTS = """
📖 *ИНСТРУКЦИЯ — ДОЛГИ*

*Когда добавлять долг:*
• Клиент должен за допы
• Мы должны клиенту (переплата)
• Мы должны поставщику за допы
• Мы должны юрику кэшбэк
• Мы должны менеджеру %
• Мы должны брокеру
• Мы должны автовозу

*➕ Добавить:* машина → тип → сумма
*✅ Закрыть:* выбери оплаченный долг

❗ После закрытия долга запиши платёж.
"""

INSTRUCTION_SAL = """
📖 *ИНСТРУКЦИЯ — ЗАРПЛАТЫ*

*➕ Добавить:*
1️⃣ Имя сотрудника
2️⃣ Оклад (цифры)
3️⃣ Бонус (или 0)
4️⃣ Месяц: *07.2026*

*✅ Отметить выплату:*
Выбери сотрудника → готово

❌ не выплачено | ✅ выплачено
"""

INSTRUCTION_REPORTS = """
📖 *ИНСТРУКЦИЯ — ОТЧЁТЫ*

*📅 За день* — платежи за сегодня
*📅 За неделю* — за 7 дней
*📅 За месяц* — текущий месяц
*🚗 По машине* — все данные одной машины
*📈 P&L* — прибыль/убытки за месяц
*⚖️ Все долги* — открытые долги

📥 доход | 📤 расход
¥ юани | ₽ рубли
❌ не оплачено | ✅ оплачено
"""

# ===== ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ СПИСКА МАШИН =====
def build_cars_keyboard(cars, page=0, prefix="car"):
    """
    Строит клавиатуру со списком машин постранично.
    page — текущая страница (начиная с 0).
    prefix — префикс callback_data для кнопок машин.
    """
    total = len(cars)
    total_pages = max(1, (total + CARS_PER_PAGE - 1) // CARS_PER_PAGE)
    start = page * CARS_PER_PAGE
    end = start + CARS_PER_PAGE
    page_cars = cars[start:end]

    buttons = []
    for car in page_cars:
        vin = car.get("ВИН", "")
        vin_mark = " 🔑" if vin else ""
        label = (
            f"{car.get('ID')} — "
            f"{car.get('Марка', '—')} "
            f"{car.get('Год', '—')}"
            f"{vin_mark}"
        )
        buttons.append([InlineKeyboardButton(
            label,
            callback_data=f"{prefix}_{car.get('ID')}"
        )])

    # Навигация
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "◀️", callback_data=f"carpage_{page-1}"
        ))
    nav.append(InlineKeyboardButton(
        f"{page+1}/{total_pages}",
        callback_data="carpage_noop"
    ))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(
            "▶️", callback_data=f"carpage_{page+1}"
        ))
    if nav:
        buttons.append(nav)

    return buttons, total_pages

# ===== ГЛАВНОЕ МЕНЮ =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💴 Курс юаня ВТБ", callback_data="yuan")],
        [InlineKeyboardButton("📊 Расчёт пошлин", callback_data="duty")],
        [InlineKeyboardButton("💰 Финансы", callback_data="finance_enter")],
    ]
    text = (
        "🤖 *Автоискатель — бот расчётов*\n\n"
        "Выберите действие:\n\n"
        "💴 *Курс юаня ВТБ* — курс CNY интернет-банк\n\n"
        "📊 *Расчёт пошлин* — таможенные пошлины по ЦБ\n\n"
        "💰 *Финансы* — учёт платежей, долгов, зарплат"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )

# ===== КУРС ЮАНЯ =====
async def show_yuan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Загружаю курс юаня...")
    data = get_vtb_yuan()
    kb = [
        [InlineKeyboardButton("🔄 Обновить", callback_data="yuan")],
        [InlineKeyboardButton("◀️ В меню", callback_data="menu")],
    ]
    if data:
        text = (
            f"💴 *Курс юаня (CNY) — интернет-банк*\n"
            f"_Источник: {data['source']}_\n\n"
            f"📈 Покупка банком: *{data['buy']} ₽*\n"
            f"📉 Продажа банком: *{data['sell']} ₽*\n\n"
            f"💡 Для перевода используется курс продажи"
        )
    else:
        text = "❌ Не удалось получить курс. Попробуйте позже."
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

# ===== РАСЧЁТ ПОШЛИН =====
async def show_duty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Считаю пошлины...")
    text = build_duty_table()
    kb = [
        [InlineKeyboardButton("🔄 Обновить", callback_data="duty")],
        [InlineKeyboardButton("◀️ В меню", callback_data="menu")],
    ]
    if text:
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    else:
        await query.edit_message_text(
            "❌ Не удалось получить курс ЦБ.",
            reply_markup=InlineKeyboardMarkup(kb)
        )

# ===== ВХОД В ФИНАНСЫ =====
async def finance_enter(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    saved_password = context.bot_data.get("finance_password")

    if not saved_password:
        if query.from_user.id == BOSS_ID:
            kb = [[InlineKeyboardButton(
                "◀️ В меню", callback_data="menu"
            )]]
            await query.edit_message_text(
                "🔐 *Первый вход*\n\nПридумайте пароль:",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown"
            )
            return SET_PASSWORD
        else:
            kb = [[InlineKeyboardButton(
                "◀️ В меню", callback_data="menu"
            )]]
            await query.edit_message_text(
                "🔒 Пароль не задан. Обратитесь к руководителю.",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return ConversationHandler.END

    if context.user_data.get("finance_auth"):
        await show_finance_menu(query, context)
        return ConversationHandler.END

    kb = [[InlineKeyboardButton(
        "◀️ В меню", callback_data="menu"
    )]]
    await query.edit_message_text(
        "🔐 *Финансовый раздел*\n\nВведите пароль:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ENTER_PASSWORD

async def handle_set_password(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    password = update.message.text.strip()
    await update.message.delete()
    context.bot_data["finance_password"] = password
    context.user_data["finance_auth"] = True
    kb = [[InlineKeyboardButton(
        "💰 Открыть финансы", callback_data="finance_menu"
    )]]
    await update.message.chat.send_message(
        "✅ *Пароль установлен!*",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def handle_enter_password(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    password = update.message.text.strip()
    await update.message.delete()
    saved = context.bot_data.get("finance_password")
    if password == saved:
        context.user_data["finance_auth"] = True
        kb = [[InlineKeyboardButton(
            "💰 Открыть финансы", callback_data="finance_menu"
        )]]
        await update.message.chat.send_message(
            "✅ *Пароль верный!*",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    else:
        kb = [
            [InlineKeyboardButton(
                "🔄 Попробовать снова",
                callback_data="finance_enter"
            )],
            [InlineKeyboardButton(
                "◀️ В меню", callback_data="menu"
            )],
        ]
        await update.message.chat.send_message(
            "❌ *Неверный пароль.*",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    return ConversationHandler.END

# ===== МЕНЮ ФИНАНСОВ =====
async def show_finance_menu(query_or_update, context):
    kb = [
        [InlineKeyboardButton("🚗 Машины", callback_data="fin_cars")],
        [InlineKeyboardButton("➕ Добавить платёж", callback_data="fin_pay")],
        [InlineKeyboardButton("⚖️ Долги", callback_data="fin_debts")],
        [InlineKeyboardButton("👥 Зарплаты", callback_data="fin_sal")],
        [InlineKeyboardButton("📊 Отчёты", callback_data="fin_reports")],
        [InlineKeyboardButton("📖 Инструкция", callback_data="inst_main")],
        [InlineKeyboardButton("🔑 Сменить пароль", callback_data="fin_chpass")],
        [InlineKeyboardButton("◀️ В главное меню", callback_data="menu")],
    ]
    text = (
        "💰 *Финансовый раздел*\n\n"
        "🚗 *Машины* — список, поиск, карточки\n"
        "➕ *Платёж* — записать доход или расход\n"
        "⚖️ *Долги* — кто кому должен\n"
        "👥 *Зарплаты* — учёт зарплат\n"
        "📊 *Отчёты* — за день, неделю, месяц\n\n"
        "❗ *Начни с добавления машины*"
    )
    if hasattr(query_or_update, 'edit_message_text'):
        await query_or_update.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    else:
        await query_or_update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )

async def finance_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    if not context.user_data.get("finance_auth"):
        kb = [[InlineKeyboardButton(
            "🔐 Войти", callback_data="finance_enter"
        )]]
        await query.edit_message_text(
            "🔒 *Требуется авторизация*",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return
    await show_finance_menu(query, context)

# ===== ИНСТРУКЦИИ =====
async def show_instruction(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    data = query.data
    back = InlineKeyboardButton(
        "◀️ К инструкции", callback_data="inst_main"
    )
    instructions = {
        "inst_main": (INSTRUCTION_MAIN, [
            [InlineKeyboardButton("🚗 Машины", callback_data="inst_cars")],
            [InlineKeyboardButton("➕ Платежи", callback_data="inst_pay")],
            [InlineKeyboardButton("⚖️ Долги", callback_data="inst_debts")],
            [InlineKeyboardButton("👥 Зарплаты", callback_data="inst_sal")],
            [InlineKeyboardButton("📊 Отчёты", callback_data="inst_reports")],
            [InlineKeyboardButton("◀️ В финансы", callback_data="finance_menu")],
        ]),
        "inst_cars": (INSTRUCTION_CARS, [
            [back],
            [InlineKeyboardButton("🚗 В Машины", callback_data="fin_cars")],
        ]),
        "inst_pay": (INSTRUCTION_PAY, [
            [back],
            [InlineKeyboardButton("➕ Добавить платёж", callback_data="fin_pay")],
        ]),
        "inst_debts": (INSTRUCTION_DEBTS, [
            [back],
            [InlineKeyboardButton("⚖️ В Долги", callback_data="fin_debts")],
        ]),
        "inst_sal": (INSTRUCTION_SAL, [
            [back],
            [InlineKeyboardButton("👥 В Зарплаты", callback_data="fin_sal")],
        ]),
        "inst_reports": (INSTRUCTION_REPORTS, [
            [back],
            [InlineKeyboardButton("📊 В Отчёты", callback_data="fin_reports")],
        ]),
    }
    if data in instructions:
        text, kb = instructions[data]
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )

# ===== РАЗДЕЛ МАШИНЫ =====
async def fin_cars(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    context.user_data["cars_page"] = 0
    await show_cars_page(query, context, page=0)

async def show_cars_page(query, context, page=0):
    """Показать страницу со списком машин"""
    cars = get_all_cars()
    kb_buttons, total_pages = build_cars_keyboard(
        cars, page=page, prefix="car"
    )

    # Служебные кнопки
    service_buttons = [
        [
            InlineKeyboardButton(
                "🔍 Поиск", callback_data="car_search"
            ),
            InlineKeyboardButton(
                "➕ Добавить", callback_data="add_car"
            )
        ],
        [InlineKeyboardButton(
            "◀️ Назад", callback_data="finance_menu"
        )],
    ]

    all_buttons = kb_buttons + service_buttons

    if not cars:
        text = (
            "🚗 *Машины*\n\n"
            "Машин пока нет.\n\n"
            "Нажми ➕ *Добавить* чтобы начать."
        )
    else:
        text = (
            f"🚗 *Машины* — всего: {len(cars)}\n"
            f"Страница {page+1}/{total_pages}\n\n"
            f"_Нажми на машину чтобы открыть карточку_\n"
            f"_🔑 — есть ВИН номер_"
        )

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(all_buttons),
        parse_mode="Markdown"
    )

async def cars_page_nav(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Навигация по страницам машин"""
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "carpage_noop":
        return
    page = int(data.replace("carpage_", ""))
    context.user_data["cars_page"] = page
    await show_cars_page(query, context, page=page)

# ===== КАРТОЧКА МАШИНЫ =====
async def show_car_card(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Показать карточку машины при нажатии на кнопку"""
    query = update.callback_query
    await query.answer()
    car_id = query.data.replace("car_", "")

    car = get_car_by_id(car_id)
    if not car:
        kb = [[InlineKeyboardButton(
            "◀️ Назад", callback_data="fin_cars"
        )]]
        await query.edit_message_text(
            "❌ Машина не найдена.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    # Формируем карточку
    vin = car.get("ВИН", "")
    vin_line = f"🔑 ВИН: *{vin}*\n" if vin else "🔑 ВИН: _не указан_\n"

    text = (
        f"🚗 *{car.get('ID')} — {car.get('Марка', '—')} "
        f"{car.get('Год', '—')}*\n\n"
        f"🎨 Цвет: *{car.get('Цвет', '—')}*\n"
        f"{vin_line}"
        f"👤 Клиент: {car.get('Клиент', '—')}\n"
        f"📅 Добавлена: {car.get('Дата добавления', '—')}"
    )

    user_id = query.from_user.id
    kb = [
        [InlineKeyboardButton(
            "✏️ Редактировать",
            callback_data=f"editcar_{car_id}"
        )],
        [InlineKeyboardButton(
            "📊 Отчёт по машине",
            callback_data=f"repcar_{car_id}"
        )],
        [InlineKeyboardButton(
            "➕ Добавить платёж",
            callback_data=f"payfromcar_{car_id}"
        )],
        [InlineKeyboardButton(
            "⚖️ Долги по машине",
            callback_data=f"debtsfromcar_{car_id}"
        )],
    ]
    if user_id == BOSS_ID:
        kb.append([InlineKeyboardButton(
            "🗑 Удалить машину",
            callback_data=f"delcar_{car_id}"
        )])
    kb.append([InlineKeyboardButton(
        "◀️ К списку", callback_data="fin_cars"
    )])

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

# ===== ПОИСК МАШИН =====
async def car_search_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    kb = [[InlineKeyboardButton(
        "❌ Отмена", callback_data="fin_cars"
    )]]
    await query.edit_message_text(
        "🔍 *Поиск машины*\n\n"
        "Введите любой текст для поиска:\n"
        "_Марка, цвет, ФИО клиента, ВИН, год..._\n\n"
        "Пример: *Zeekr* или *Иванов* или *2024*",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return CAR_SEARCH

async def car_search_execute(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query_text = update.message.text.strip().lower()
    cars = get_all_cars()

    # Ищем по всем полям
    found = []
    for car in cars:
        searchable = " ".join([
            str(car.get("ID", "")),
            str(car.get("Марка", "")),
            str(car.get("Год", "")),
            str(car.get("Цвет", "")),
            str(car.get("ВИН", "")),
            str(car.get("Клиент", "")),
            str(car.get("Тип клиента", "")),
            str(car.get("Дата добавления", "")),
        ]).lower()
        if query_text in searchable:
            found.append(car)

    if not found:
        kb = [
            [InlineKeyboardButton(
                "🔍 Искать снова", callback_data="car_search"
            )],
            [InlineKeyboardButton(
                "◀️ К машинам", callback_data="fin_cars"
            )],
        ]
        await update.message.reply_text(
            f"❌ *По запросу «{query_text}» ничего не найдено.*",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    # Показываем результаты кнопками
    car_buttons = []
    for car in found[:15]:  # Максимум 15 результатов
        vin = car.get("ВИН", "")
        vin_mark = " 🔑" if vin else ""
        label = (
            f"{car.get('ID')} — "
            f"{car.get('Марка', '—')} "
            f"{car.get('Год', '—')}"
            f"{vin_mark}"
        )
        car_buttons.append([InlineKeyboardButton(
            label, callback_data=f"car_{car.get('ID')}"
        )])

    car_buttons.append([InlineKeyboardButton(
        "◀️ К машинам", callback_data="fin_cars"
    )])

    text = (
        f"🔍 *Результаты поиска «{query_text}»:*\n"
        f"Найдено: {len(found)} машин\n\n"
        f"_Нажми на машину чтобы открыть карточку_"
    )
    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(car_buttons),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# ===== ДОБАВИТЬ МАШИНУ =====
async def add_car_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    context.user_data["new_car"] = {}
    kb = [[InlineKeyboardButton(
        "❌ Отмена", callback_data="fin_cars"
    )]]
    await query.edit_message_text(
        "🚗 *Добавление машины*\n\n"
        "Шаг 1 из 5\n\n"
        "Введите *марку и модель* одним сообщением:\n"
        "_Примеры: Zeekr 001 / Haval H6 / Chery Tiggo 8_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ADD_CAR_MARK_MODEL

async def add_car_mark_model(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    context.user_data["new_car"]["Марка"] = (
        update.message.text.strip()
    )
    kb = [[InlineKeyboardButton(
        "❌ Отмена", callback_data="fin_cars"
    )]]
    await update.message.reply_text(
        "Шаг 2 из 5\n\nВведите *год выпуска*:\n"
        "_Пример: 2024_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ADD_CAR_YEAR

async def add_car_year(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    context.user_data["new_car"]["Год"] = (
        update.message.text.strip()
    )
    kb = [[InlineKeyboardButton(
        "❌ Отмена", callback_data="fin_cars"
    )]]
    await update.message.reply_text(
        "Шаг 3 из 5\n\nВведите *цвет*:\n"
        "_Пример: Белый, Чёрный_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ADD_CAR_COLOR

async def add_car_color(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    context.user_data["new_car"]["Цвет"] = (
        update.message.text.strip()
    )
    kb = [
        [InlineKeyboardButton(
            "⏭ Пропустить ВИН", callback_data="skip_vin"
        )],
        [InlineKeyboardButton(
            "❌ Отмена", callback_data="fin_cars"
        )],
    ]
    await update.message.reply_text(
        "Шаг 4 из 5\n\nВведите *ВИН номер*:\n"
        "_Пример: LSGJA52B2HG123456_\n\n"
        "Или нажмите ⏭ *Пропустить* если ВИН неизвестен",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ADD_CAR_VIN

async def add_car_vin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    context.user_data["new_car"]["ВИН"] = (
        update.message.text.strip()
    )
    kb = [[InlineKeyboardButton(
        "❌ Отмена", callback_data="fin_cars"
    )]]
    await update.message.reply_text(
        "Шаг 5 из 5\n\n"
        "Введите *ФИО клиента по паспорту*:\n"
        "_Пример: Иванов Иван Иванович_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ADD_CAR_CLIENT

async def skip_vin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    context.user_data["new_car"]["ВИН"] = ""
    kb = [[InlineKeyboardButton(
        "❌ Отмена", callback_data="fin_cars"
    )]]
    await query.edit_message_text(
        "Шаг 5 из 5\n\n"
        "Введите *ФИО клиента по паспорту*:\n"
        "_Пример: Иванов Иван Иванович_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ADD_CAR_CLIENT

async def add_car_client(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    context.user_data["new_car"]["ФИО"] = (
        update.message.text.strip()
    )
    kb = [
        [
            InlineKeyboardButton(
                "👤 Физлицо", callback_data="client_fiz"
            ),
            InlineKeyboardButton(
                "🏢 Юрлицо", callback_data="client_yur"
            )
        ],
        [InlineKeyboardButton(
            "❌ Отмена", callback_data="fin_cars"
        )]
    ]
    await update.message.reply_text(
        "Тип клиента:",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return ADD_CAR_CLIENT_TYPE

async def add_car_client_type(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    fio = context.user_data["new_car"].get("ФИО", "")

    if query.data == "client_fiz":
        context.user_data["new_car"]["Клиент"] = (
            f"{fio} (физлицо)"
        )
        context.user_data["new_car"]["Тип клиента"] = "Физлицо"
        return await save_new_car(query, context)
    else:
        context.user_data["new_car"]["Тип клиента"] = "Юрлицо"
        kb = [[InlineKeyboardButton(
            "❌ Отмена", callback_data="fin_cars"
        )]]
        await query.edit_message_text(
            "Введите *название компании*:\n"
            "_Пример: ООО Автомир_",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return ADD_CAR_COMPANY

async def add_car_company(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    company = update.message.text.strip()
    fio = context.user_data["new_car"].get("ФИО", "")
    context.user_data["new_car"]["Клиент"] = (
        f"{fio} (юрлицо — {company})"
    )
    return await save_new_car_msg(update.message, context)

async def save_new_car(query, context):
    car = context.user_data["new_car"]
    try:
        ws = get_worksheet("МАШИНЫ")
        if not ws:
            raise Exception("Нет подключения")
        car_id = get_next_id("МАШИНЫ", "AUTO")
        today = datetime.now().strftime("%d.%m.%Y")
        row = [
            car_id,
            car.get("Марка", ""),
            "",
            car.get("Год", ""),
            car.get("Цвет", ""),
            car.get("ВИН", ""),
            "", "",
            car.get("Клиент", ""),
            car.get("Тип клиента", ""),
            today
        ]
        ws.append_row(row)
        kb = [
            [InlineKeyboardButton(
                "📋 Открыть карточку",
                callback_data=f"car_{car_id}"
            )],
            [InlineKeyboardButton(
                "◀️ К машинам", callback_data="fin_cars"
            )],
        ]
        await query.edit_message_text(
            f"✅ *Машина добавлена!*\n\n"
            f"🆔 *{car_id}*\n"
            f"🚗 {car.get('Марка')} {car.get('Год')}\n"
            f"🎨 {car.get('Цвет')}\n"
            f"🔑 ВИН: {car.get('ВИН') or 'не указан'}\n"
            f"👤 {car.get('Клиент')}",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"save_new_car error: {e}")
        reset_connection()
        kb = [[InlineKeyboardButton(
            "◀️ Назад", callback_data="fin_cars"
        )]]
        await query.edit_message_text(
            "❌ *Ошибка при сохранении.* Попробуйте ещё раз.",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    context.user_data.pop("new_car", None)
    return ConversationHandler.END

async def save_new_car_msg(message, context):
    car = context.user_data["new_car"]
    try:
        ws = get_worksheet("МАШИНЫ")
        if not ws:
            raise Exception("Нет подключения")
        car_id = get_next_id("МАШИНЫ", "AUTO")
        today = datetime.now().strftime("%d.%m.%Y")
        row = [
            car_id,
            car.get("Марка", ""),
            "",
            car.get("Год", ""),
            car.get("Цвет", ""),
            car.get("ВИН", ""),
            "", "",
            car.get("Клиент", ""),
            car.get("Тип клиента", ""),
            today
        ]
        ws.append_row(row)
        kb = [
            [InlineKeyboardButton(
                "📋 Открыть карточку",
                callback_data=f"car_{car_id}"
            )],
            [InlineKeyboardButton(
                "◀️ К машинам", callback_data="fin_cars"
            )],
        ]
        await message.reply_text(
            f"✅ *Машина добавлена!*\n\n"
            f"🆔 *{car_id}*\n"
            f"🚗 {car.get('Марка')} {car.get('Год')}\n"
            f"🎨 {car.get('Цвет')}\n"
            f"🔑 ВИН: {car.get('ВИН') or 'не указан'}\n"
            f"👤 {car.get('Клиент')}",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"save_new_car_msg error: {e}")
        reset_connection()
        kb = [[InlineKeyboardButton(
            "◀️ Назад", callback_data="fin_cars"
        )]]
        await message.reply_text(
            "❌ *Ошибка при сохранении.* Попробуйте ещё раз.",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    context.user_data.pop("new_car", None)
    return ConversationHandler.END

# ===== РЕДАКТИРОВАНИЕ МАШИНЫ =====
EDIT_FIELDS = [
    ("🚗 Марка и модель", "Марка", 2),
    ("📅 Год", "Год", 4),
    ("🎨 Цвет", "Цвет", 5),
    ("🔑 ВИН номер", "ВИН", 6),
    ("👤 ФИО клиента", "Клиент_ФИО", 9),
]

async def edit_car_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Меню редактирования из карточки машины"""
    query = update.callback_query
    await query.answer()
    car_id = query.data.replace("editcar_", "")
    context.user_data["edit_car_id"] = car_id

    car = get_car_by_id(car_id)
    if not car:
        kb = [[InlineKeyboardButton(
            "◀️ Назад", callback_data="fin_cars"
        )]]
        await query.edit_message_text(
            "❌ Машина не найдена.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return ConversationHandler.END

    field_buttons = []
    for label, field, col in EDIT_FIELDS:
        field_buttons.append([InlineKeyboardButton(
            label, callback_data=f"editfield_{field}"
        )])
    field_buttons.append([InlineKeyboardButton(
        "◀️ К карточке",
        callback_data=f"car_{car_id}"
    )])

    vin = car.get("ВИН", "") or "не указан"
    text = (
        f"✏️ *Редактирование {car_id}*\n\n"
        f"🚗 {car.get('Марка', '—')} {car.get('Год', '—')}\n"
        f"🎨 {car.get('Цвет', '—')}\n"
        f"🔑 ВИН: {vin}\n"
        f"👤 {car.get('Клиент', '—')}\n\n"
        f"Что хотите изменить?"
    )
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(field_buttons),
        parse_mode="Markdown"
    )
    return EDIT_CAR_FIELD

async def edit_car_field_selected(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    field = query.data.replace("editfield_", "")
    context.user_data["edit_field"] = field

    kb = [[InlineKeyboardButton(
        "❌ Отмена", callback_data="fin_cars"
    )]]

    prompts = {
        "Марка": "Введите новую *марку и модель*:\n_Пример: Zeekr 001_",
        "Год": "Введите новый *год*:\n_Пример: 2024_",
        "Цвет": "Введите новый *цвет*:\n_Пример: Белый_",
        "ВИН": "Введите новый *ВИН номер*:\n_Пример: LSGJA52B2HG123456_\n\nИли напишите «-» чтобы очистить ВИН",
        "Клиент_ФИО": "Введите новое *ФИО клиента по паспорту*:\n_Пример: Иванов Иван Иванович_",
    }

    await query.edit_message_text(
        prompts.get(field, "Введите новое значение:"),
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return EDIT_CAR_VALUE

async def edit_car_value(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    new_value = update.message.text.strip()
    car_id = context.user_data.get("edit_car_id")
    field = context.user_data.get("edit_field")

    if field == "Клиент_ФИО":
        context.user_data["edit_new_fio"] = new_value
        kb = [
            [
                InlineKeyboardButton(
                    "👤 Физлицо",
                    callback_data="editclient_fiz"
                ),
                InlineKeyboardButton(
                    "🏢 Юрлицо",
                    callback_data="editclient_yur"
                )
            ],
            [InlineKeyboardButton(
                "❌ Отмена", callback_data="fin_cars"
            )]
        ]
        await update.message.reply_text(
            "Тип клиента:",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return EDIT_CAR_CLIENT_TYPE

    field_to_col = {
        "Марка": 2,
        "Год": 4,
        "Цвет": 5,
        "ВИН": 6,
    }
    col = field_to_col.get(field)

    if new_value == "-" and field == "ВИН":
        new_value = ""

    try:
        ws = get_worksheet("МАШИНЫ")
        records = ws.get_all_records()
        for i, r in enumerate(records):
            if r.get("ID") == car_id:
                ws.update_cell(i + 2, col, new_value)
                break
        kb = [
            [InlineKeyboardButton(
                "✏️ Изменить ещё",
                callback_data=f"editcar_{car_id}"
            )],
            [InlineKeyboardButton(
                "📋 К карточке",
                callback_data=f"car_{car_id}"
            )],
        ]
        await update.message.reply_text(
            f"✅ *Данные обновлены!*",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"edit_car_value error: {e}")
        reset_connection()
        await update.message.reply_text(
            "❌ Ошибка при сохранении."
        )

    context.user_data.pop("edit_car_id", None)
    context.user_data.pop("edit_field", None)
    return ConversationHandler.END

async def edit_car_client_type(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    car_id = context.user_data.get("edit_car_id")
    fio = context.user_data.get("edit_new_fio", "")

    if query.data == "editclient_fiz":
        new_client = f"{fio} (физлицо)"
        return await save_edited_client(
            query, context, car_id, new_client, "Физлицо"
        )
    else:
        kb = [[InlineKeyboardButton(
            "❌ Отмена", callback_data="fin_cars"
        )]]
        await query.edit_message_text(
            "Введите *название компании*:\n_Пример: ООО Автомир_",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return EDIT_CAR_COMPANY

async def edit_car_company(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    company = update.message.text.strip()
    car_id = context.user_data.get("edit_car_id")
    fio = context.user_data.get("edit_new_fio", "")
    new_client = f"{fio} (юрлицо — {company})"
    try:
        ws = get_worksheet("МАШИНЫ")
        records = ws.get_all_records()
        for i, r in enumerate(records):
            if r.get("ID") == car_id:
                ws.update_cell(i + 2, 9, new_client)
                ws.update_cell(i + 2, 10, "Юрлицо")
                break
        kb = [
            [InlineKeyboardButton(
                "✏️ Изменить ещё",
                callback_data=f"editcar_{car_id}"
            )],
            [InlineKeyboardButton(
                "📋 К карточке",
                callback_data=f"car_{car_id}"
            )],
        ]
        await update.message.reply_text(
            f"✅ *Клиент обновлён!*\n\n👤 {new_client}",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"edit_car_company error: {e}")
        reset_connection()
        await update.message.reply_text(
            "❌ Ошибка при сохранении."
        )
    context.user_data.pop("edit_car_id", None)
    context.user_data.pop("edit_new_fio", None)
    return ConversationHandler.END

async def save_edited_client(
    query, context, car_id, new_client, new_type
):
    try:
        ws = get_worksheet("МАШИНЫ")
        records = ws.get_all_records()
        for i, r in enumerate(records):
            if r.get("ID") == car_id:
                ws.update_cell(i + 2, 9, new_client)
                ws.update_cell(i + 2, 10, new_type)
                break
        kb = [
            [InlineKeyboardButton(
                "✏️ Изменить ещё",
                callback_data=f"editcar_{car_id}"
            )],
            [InlineKeyboardButton(
                "📋 К карточке",
                callback_data=f"car_{car_id}"
            )],
        ]
        await query.edit_message_text(
            f"✅ *Клиент обновлён!*\n\n👤 {new_client}",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"save_edited_client error: {e}")
        reset_connection()
        kb = [[InlineKeyboardButton(
            "◀️ Назад", callback_data="fin_cars"
        )]]
        await query.edit_message_text(
            "❌ Ошибка при сохранении.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    context.user_data.pop("edit_car_id", None)
    context.user_data.pop("edit_new_fio", None)
    return ConversationHandler.END

# ===== УДАЛЕНИЕ МАШИНЫ =====
async def delete_car_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    car_id = query.data.replace("delcar_", "")

    if query.from_user.id != BOSS_ID:
        kb = [[InlineKeyboardButton(
            "◀️ Назад", callback_data=f"car_{car_id}"
        )]]
        await query.edit_message_text(
            "❌ Только руководитель может удалять машины.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    kb = [
        [InlineKeyboardButton(
            "🗑 Да, удалить всё",
            callback_data=f"delconfirm_{car_id}"
        )],
        [InlineKeyboardButton(
            "❌ Отмена",
            callback_data=f"car_{car_id}"
        )],
    ]
    await query.edit_message_text(
        f"⚠️ *Подтверждение удаления*\n\n"
        f"Машина: *{car_id}*\n\n"
        f"Будут удалены:\n"
        f"• Карточка машины\n"
        f"• Все платежи\n"
        f"• Все долги\n\n"
        f"*Это нельзя отменить!*",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

async def delete_car_execute(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    car_id = query.data.replace("delconfirm_", "")

    try:
        car_ws = get_worksheet("МАШИНЫ")
        if car_ws:
            records = car_ws.get_all_records()
            for i, r in enumerate(records):
                if r.get("ID") == car_id:
                    car_ws.delete_rows(i + 2)
                    break

        pay_ws = get_worksheet("ПЛАТЕЖИ")
        if pay_ws:
            records = pay_ws.get_all_records()
            rows = [
                i + 2 for i, r in enumerate(records)
                if r.get("ID машины") == car_id
            ]
            for row in sorted(rows, reverse=True):
                pay_ws.delete_rows(row)

        debt_ws = get_worksheet("ДОЛГИ")
        if debt_ws:
            records = debt_ws.get_all_records()
            rows = [
                i + 2 for i, r in enumerate(records)
                if r.get("ID машины") == car_id
            ]
            for row in sorted(rows, reverse=True):
                debt_ws.delete_rows(row)

        kb = [[InlineKeyboardButton(
            "◀️ К машинам", callback_data="fin_cars"
        )]]
        await query.edit_message_text(
            f"✅ *Машина {car_id} удалена.*",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"delete_car_execute error: {e}")
        reset_connection()
        kb = [[InlineKeyboardButton(
            "◀️ Назад", callback_data="fin_cars"
        )]]
        await query.edit_message_text(
            "❌ Ошибка при удалении.",
            reply_markup=InlineKeyboardMarkup(kb)
        )

# ===== ПЛАТЁЖ ИЗ КАРТОЧКИ МАШИНЫ =====
CATEGORIES = [
    ("💰 Накрутка — прибыль (юани ¥)",
     "Накрутка", "CNY", "Входящий"),
    ("💵 Допы от клиента (рубли ₽)",
     "Допы от клиента", "RUB", "Входящий"),
    ("🚛 Автовоз (рубли ₽)",
     "Автовоз", "RUB", "Исходящий"),
    ("🏛 Таможенный брокер (рубли ₽)",
     "Таможенный брокер", "RUB", "Исходящий"),
    ("🔧 Допы в Китае (юани ¥)",
     "Допы в Китае", "CNY", "Исходящий"),
    ("⛽ Бензин (рубли ₽)",
     "Бензин", "RUB", "Исходящий"),
    ("💸 Кэшбэк юрику (юани ¥)",
     "Кэшбэк юрику", "CNY", "Исходящий"),
    ("👤 % Менеджеру 20 000₽",
     "% Менеджеру", "RUB", "Исходящий"),
]

async def pay_from_car(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Добавить платёж прямо из карточки машины"""
    query = update.callback_query
    await query.answer()
    car_id = query.data.replace("payfromcar_", "")
    context.user_data["new_pay"] = {"car_id": car_id}

    cat_buttons = []
    for i, (label, _, _, _) in enumerate(CATEGORIES):
        cat_buttons.append([InlineKeyboardButton(
            label, callback_data=f"paycat_{i}"
        )])
    cat_buttons.append([InlineKeyboardButton(
        "◀️ Отмена", callback_data=f"car_{car_id}"
    )])
    await query.edit_message_text(
        f"➕ *Платёж для {car_id}*\n\n"
        "Выберите категорию:",
        reply_markup=InlineKeyboardMarkup(cat_buttons),
        parse_mode="Markdown"
    )
    return PAY_CATEGORY

# ===== ДОЛГИ ИЗ КАРТОЧКИ МАШИНЫ =====
async def debts_from_car(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Показать долги по конкретной машине из карточки"""
    query = update.callback_query
    await query.answer()
    car_id = query.data.replace("debtsfromcar_", "")

    try:
        ws = get_worksheet("ДОЛГИ")
        records = ws.get_all_records() if ws else []
        car_debts = [
            r for r in records
            if r.get("ID машины") == car_id
        ]
        unpaid = [
            r for r in car_debts
            if r.get("Статус") == "Не оплачен"
        ]

        kb = [
            [InlineKeyboardButton(
                "➕ Добавить долг",
                callback_data="add_debt"
            )],
            [InlineKeyboardButton(
                "◀️ К карточке",
                callback_data=f"car_{car_id}"
            )],
        ]

        if not car_debts:
            text = (
                f"⚖️ *Долги по {car_id}*\n\n"
                f"✅ Долгов нет."
            )
        else:
            text = (
                f"⚖️ *Долги по {car_id}*\n\n"
                f"Открытых: {len(unpaid)}\n\n"
            )
            for d in car_debts:
                si = (
                    "❌" if d.get("Статус") == "Не оплачен"
                    else "✅"
                )
                cs = "¥" if d.get("Валюта") == "CNY" else "₽"
                text += (
                    f"{si} {d.get('Кто должен')} → "
                    f"{d.get('Кому должен')}: "
                    f"*{d.get('Сумма')} {cs}*\n"
                )

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"debts_from_car error: {e}")

# ===== ДОБАВИТЬ ПЛАТЁЖ =====
async def fin_pay_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    cars = get_all_cars()

    if not cars:
        kb = [
            [InlineKeyboardButton(
                "🚗 Добавить машину", callback_data="add_car"
            )],
            [InlineKeyboardButton(
                "◀️ Назад", callback_data="finance_menu"
            )],
        ]
        await query.edit_message_text(
            "❌ *Сначала добавьте машину!*",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    context.user_data["new_pay"] = {}
    car_buttons, _ = build_cars_keyboard(
        cars, page=0, prefix="paycar"
    )
    car_buttons.append([InlineKeyboardButton(
        "◀️ Отмена", callback_data="finance_menu"
    )])
    await query.edit_message_text(
        "➕ *Добавить платёж*\n\nВыберите машину:",
        reply_markup=InlineKeyboardMarkup(car_buttons),
        parse_mode="Markdown"
    )
    return PAY_CAR

async def pay_car_selected(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    car_id = query.data.replace("paycar_", "")
    context.user_data["new_pay"]["car_id"] = car_id

    cat_buttons = []
    for i, (label, _, _, _) in enumerate(CATEGORIES):
        cat_buttons.append([InlineKeyboardButton(
            label, callback_data=f"paycat_{i}"
        )])
    cat_buttons.append([InlineKeyboardButton(
        "◀️ Отмена", callback_data="finance_menu"
    )])
    await query.edit_message_text(
        f"Машина: *{car_id}*\n\nВыберите категорию:",
        reply_markup=InlineKeyboardMarkup(cat_buttons),
        parse_mode="Markdown"
    )
    return PAY_CATEGORY

async def pay_category_selected(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.replace("paycat_", ""))
    label, cat_name, currency, pay_type = CATEGORIES[idx]
    context.user_data["new_pay"]["category"] = cat_name
    context.user_data["new_pay"]["currency"] = currency
    context.user_data["new_pay"]["type"] = pay_type

    car_id = context.user_data["new_pay"].get("car_id", "")
    kb = [[InlineKeyboardButton(
        "❌ Отмена", callback_data="finance_menu"
    )]]

    if cat_name == "% Менеджеру":
        context.user_data["new_pay"]["amount"] = "20000"
        await query.edit_message_text(
            f"Категория: *{cat_name}*\n"
            f"Сумма: *20 000 ₽* (фиксированная)\n\n"
            f"Добавить комментарий?\n"
            f"_Текст или «-» чтобы пропустить_",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    else:
        cl = "юанях (¥)" if currency == "CNY" else "рублях (₽)"
        await query.edit_message_text(
            f"Машина: *{car_id}* | {cat_name}\n\n"
            f"Введите сумму в {cl}:\n"
            f"_Только цифры: 2800_",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    return PAY_AMOUNT

async def pay_amount(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    text = update.message.text.strip()
    if context.user_data["new_pay"].get("amount") != "20000":
        context.user_data["new_pay"]["amount"] = text
    kb = [[InlineKeyboardButton(
        "❌ Отмена", callback_data="finance_menu"
    )]]
    await update.message.reply_text(
        "Добавить комментарий?\n"
        "_Текст или «-» чтобы пропустить_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return PAY_COMMENT

async def pay_comment(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    comment = update.message.text.strip()
    if comment == "-":
        comment = ""
    pay = context.user_data["new_pay"]
    pay["comment"] = comment

    try:
        ws = get_worksheet("ПЛАТЕЖИ")
        if not ws:
            raise Exception("Нет подключения")
        pay_id = get_next_id("ПЛАТЕЖИ", "PAY")
        today = datetime.now().strftime("%d.%m.%Y")
        row = [
            pay_id,
            pay.get("car_id", ""),
            pay.get("category", ""),
            pay.get("amount", ""),
            pay.get("currency", ""),
            pay.get("type", ""),
            today,
            pay.get("comment", "")
        ]
        ws.append_row(row)
        cs = "¥" if pay.get("currency") == "CNY" else "₽"
        ti = "📥" if pay.get("type") == "Входящий" else "📤"
        car_id = pay.get("car_id", "")
        kb = [
            [InlineKeyboardButton(
                "➕ Ещё платёж", callback_data="fin_pay"
            )],
            [InlineKeyboardButton(
                "📋 К машине",
                callback_data=f"car_{car_id}"
            )],
            [InlineKeyboardButton(
                "◀️ В финансы", callback_data="finance_menu"
            )],
        ]
        await update.message.reply_text(
            f"✅ *Платёж записан!*\n\n"
            f"🆔 {pay_id}\n"
            f"🚗 *{car_id}*\n"
            f"📂 {pay.get('category')}\n"
            f"💵 *{pay.get('amount')} {cs}*\n"
            f"{ti} {pay.get('type')}",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"pay_comment error: {e}")
        reset_connection()
        await update.message.reply_text(
            "❌ Ошибка при сохранении."
        )
    context.user_data.pop("new_pay", None)
    return ConversationHandler.END

# ===== ТИПЫ ДОЛГОВ =====
DEBT_TYPES = [
    ("👤 Клиент должен нам за допы (₽)",
     "Клиент", "Нам", "RUB"),
    ("🏢 Мы должны клиенту — переплата (₽)",
     "Мы", "Клиенту", "RUB"),
    ("🇨🇳 Мы должны поставщику за допы (¥)",
     "Мы", "Поставщику", "CNY"),
    ("💸 Мы должны юрику кэшбэк (¥)",
     "Мы", "Юрику", "CNY"),
    ("👤 Мы должны менеджеру % (₽)",
     "Мы", "Менеджеру", "RUB"),
    ("🏛 Мы должны брокеру (₽)",
     "Мы", "Брокеру", "RUB"),
    ("🚛 Мы должны автовозу (₽)",
     "Мы", "Автовозу", "RUB"),
]

# ===== ДОЛГИ =====
async def fin_debts(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    try:
        ws = get_worksheet("ДОЛГИ")
        records = ws.get_all_records() if ws else []
        unpaid = [
            r for r in records
            if r.get("Статус") == "Не оплачен"
        ]
        kb = [
            [InlineKeyboardButton(
                "➕ Добавить долг", callback_data="add_debt"
            )],
            [InlineKeyboardButton(
                "✅ Закрыть долг", callback_data="close_debt"
            )],
            [InlineKeyboardButton(
                "❓ Инструкция", callback_data="inst_debts"
            )],
            [InlineKeyboardButton(
                "◀️ Назад", callback_data="finance_menu"
            )],
        ]
        if not unpaid:
            text = "⚖️ *Долги*\n\n✅ Все долги погашены!"
        else:
            text = f"⚖️ *Долги* — открытых: {len(unpaid)}\n\n"
            for d in unpaid[-8:]:
                cs = "¥" if d.get("Валюта") == "CNY" else "₽"
                text += (
                    f"❌ *{d.get('ID долга')}* | "
                    f"{d.get('ID машины')}\n"
                    f"{d.get('Кто должен')} → "
                    f"{d.get('Кому должен')}: "
                    f"*{d.get('Сумма')} {cs}*\n\n"
                )
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"fin_debts error: {e}")

async def add_debt_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    cars = get_all_cars()
    if not cars:
        kb = [[InlineKeyboardButton(
            "◀️ Назад", callback_data="fin_debts"
        )]]
        await query.edit_message_text(
            "❌ Сначала добавьте машину!",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return ConversationHandler.END

    context.user_data["new_debt"] = {}
    car_buttons, _ = build_cars_keyboard(
        cars, page=0, prefix="debtcar"
    )
    car_buttons.append([InlineKeyboardButton(
        "◀️ Отмена", callback_data="fin_debts"
    )])
    await query.edit_message_text(
        "⚖️ *Добавить долг*\n\nВыберите машину:",
        reply_markup=InlineKeyboardMarkup(car_buttons),
        parse_mode="Markdown"
    )
    return DEBT_CAR

async def debt_car_selected(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    car_id = query.data.replace("debtcar_", "")
    context.user_data["new_debt"]["car_id"] = car_id

    debt_buttons = []
    for i, (label, _, _, _) in enumerate(DEBT_TYPES):
        debt_buttons.append([InlineKeyboardButton(
            label, callback_data=f"debttype_{i}"
        )])
    debt_buttons.append([InlineKeyboardButton(
        "◀️ Отмена", callback_data="fin_debts"
    )])
    await query.edit_message_text(
        f"Машина: *{car_id}*\n\nВыберите тип долга:",
        reply_markup=InlineKeyboardMarkup(debt_buttons),
        parse_mode="Markdown"
    )
    return DEBT_WHO

async def debt_type_selected(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.replace("debttype_", ""))
    label, who, whom, currency = DEBT_TYPES[idx]
    context.user_data["new_debt"]["who"] = who
    context.user_data["new_debt"]["whom"] = whom
    context.user_data["new_debt"]["currency"] = currency

    kb = [[InlineKeyboardButton(
        "❌ Отмена", callback_data="fin_debts"
    )]]
    cl = "юанях (¥)" if currency == "CNY" else "рублях (₽)"
    await query.edit_message_text(
        f"*{label}*\n\nВведите сумму в {cl}:\n"
        f"_Только цифры: 5000_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return DEBT_AMOUNT

async def debt_amount(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    context.user_data["new_debt"]["amount"] = (
        update.message.text.strip()
    )
    try:
        debt = context.user_data["new_debt"]
        ws = get_worksheet("ДОЛГИ")
        if not ws:
            raise Exception("Нет подключения")
        debt_id = get_next_id("ДОЛГИ", "DEBT")
        today = datetime.now().strftime("%d.%m.%Y")
        row = [
            debt_id,
            debt.get("car_id", ""),
            debt.get("who", ""),
            debt.get("whom", ""),
            debt.get("amount", ""),
            debt.get("currency", ""),
            "Не оплачен",
            today
        ]
        ws.append_row(row)
        cs = "¥" if debt.get("currency") == "CNY" else "₽"
        car_id = debt.get("car_id", "")
        kb = [
            [InlineKeyboardButton(
                "➕ Ещё долг", callback_data="add_debt"
            )],
            [InlineKeyboardButton(
                "📋 К машине",
                callback_data=f"car_{car_id}"
            )],
            [InlineKeyboardButton(
                "◀️ К долгам", callback_data="fin_debts"
            )],
        ]
        await update.message.reply_text(
            f"✅ *Долг записан!*\n\n"
            f"🆔 {debt_id}\n"
            f"🚗 {car_id}\n"
            f"{debt.get('who')} → {debt.get('whom')}: "
            f"*{debt.get('amount')} {cs}*\n"
            f"📌 Статус: ❌ Не оплачен",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"debt_amount error: {e}")
        reset_connection()
        await update.message.reply_text(
            "❌ Ошибка при сохранении."
        )
    context.user_data.pop("new_debt", None)
    return ConversationHandler.END

async def close_debt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    try:
        ws = get_worksheet("ДОЛГИ")
        records = ws.get_all_records() if ws else []
        unpaid = [
            r for r in records
            if r.get("Статус") == "Не оплачен"
        ]
        if not unpaid:
            kb = [[InlineKeyboardButton(
                "◀️ Назад", callback_data="fin_debts"
            )]]
            await query.edit_message_text(
                "✅ Все долги погашены!",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return

        debt_buttons = []
        for d in unpaid[-10:]:
            cs = "¥" if d.get("Валюта") == "CNY" else "₽"
            label = (
                f"{d.get('ID долга')} | "
                f"{d.get('ID машины')} | "
                f"{d.get('Сумма')} {cs}"
            )
            debt_buttons.append([InlineKeyboardButton(
                label,
                callback_data=f"closedebt_{d.get('ID долга')}"
            )])
        debt_buttons.append([InlineKeyboardButton(
            "◀️ Отмена", callback_data="fin_debts"
        )])
        await query.edit_message_text(
            "✅ *Закрыть долг*\n\nВыберите оплаченный долг:",
            reply_markup=InlineKeyboardMarkup(debt_buttons),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"close_debt error: {e}")

async def close_debt_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    debt_id = query.data.replace("closedebt_", "")
    try:
        ws = get_worksheet("ДОЛГИ")
        records = ws.get_all_records()
        for i, r in enumerate(records):
            if r.get("ID долга") == debt_id:
                ws.update_cell(i + 2, 7, "Оплачен")
                break
        kb = [[InlineKeyboardButton(
            "◀️ К долгам", callback_data="fin_debts"
        )]]
        await query.edit_message_text(
            f"✅ *Долг {debt_id} закрыт!*",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"close_debt_confirm error: {e}")
        reset_connection()

# ===== ЗАРПЛАТЫ =====
async def fin_sal(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    try:
        ws = get_worksheet("ЗАРПЛАТЫ")
        records = ws.get_all_records() if ws else []
        unpaid = [
            r for r in records
            if r.get("Статус") == "Не выплачено"
        ]
        kb = [
            [InlineKeyboardButton(
                "➕ Добавить зарплату", callback_data="add_sal"
            )],
            [InlineKeyboardButton(
                "✅ Отметить выплату", callback_data="pay_sal"
            )],
            [InlineKeyboardButton(
                "❓ Инструкция", callback_data="inst_sal"
            )],
            [InlineKeyboardButton(
                "◀️ Назад", callback_data="finance_menu"
            )],
        ]
        if not records:
            text = "👥 *Зарплаты*\n\nЗаписей нет."
        else:
            text = (
                f"👥 *Зарплаты*\n\n"
                f"Не выплачено: {len(unpaid)}\n\n"
            )
            for r in records[-8:]:
                si = (
                    "❌" if r.get("Статус") == "Не выплачено"
                    else "✅"
                )
                text += (
                    f"{si} *{r.get('Сотрудник')}* "
                    f"— {r.get('Месяц')}\n"
                    f"Итого: *{r.get('Итого')} ₽*\n\n"
                )
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"fin_sal error: {e}")

async def add_sal_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    context.user_data["new_sal"] = {}
    kb = [[InlineKeyboardButton(
        "❌ Отмена", callback_data="fin_sal"
    )]]
    await query.edit_message_text(
        "👥 *Добавить зарплату*\n\nШаг 1 из 4\n\n"
        "Введите *имя сотрудника*:\n_Пример: Иванов Кирилл_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return SAL_NAME

async def sal_name(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    context.user_data["new_sal"]["name"] = (
        update.message.text.strip()
    )
    kb = [[InlineKeyboardButton(
        "❌ Отмена", callback_data="fin_sal"
    )]]
    await update.message.reply_text(
        "Шаг 2 из 4\n\nВведите *оклад* (₽):\n"
        "_Только цифры: 30000_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return SAL_OKLAD

async def sal_oklad(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    context.user_data["new_sal"]["oklad"] = (
        update.message.text.strip()
    )
    kb = [[InlineKeyboardButton(
        "❌ Отмена", callback_data="fin_sal"
    )]]
    await update.message.reply_text(
        "Шаг 3 из 4\n\nВведите *бонус* (₽):\n"
        "_Нет бонуса → напишите 0_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return SAL_BONUS

async def sal_bonus(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    context.user_data["new_sal"]["bonus"] = (
        update.message.text.strip()
    )
    kb = [[InlineKeyboardButton(
        "❌ Отмена", callback_data="fin_sal"
    )]]
    await update.message.reply_text(
        "Шаг 4 из 4\n\nВведите *месяц*:\n_Пример: 07.2026_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return SAL_MONTH

async def sal_month(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    sal = context.user_data["new_sal"]
    sal["month"] = update.message.text.strip()
    try:
        oklad = float(sal.get("oklad", 0))
        bonus = float(sal.get("bonus", 0))
        total = oklad + bonus
        ws = get_worksheet("ЗАРПЛАТЫ")
        if not ws:
            raise Exception("Нет подключения")
        sal_id = get_next_id("ЗАРПЛАТЫ", "SAL")
        row = [
            sal_id,
            sal.get("name", ""),
            oklad, bonus, total,
            sal.get("month", ""),
            "Не выплачено", ""
        ]
        ws.append_row(row)
        kb = [
            [InlineKeyboardButton(
                "➕ Ещё", callback_data="add_sal"
            )],
            [InlineKeyboardButton(
                "◀️ К зарплатам", callback_data="fin_sal"
            )],
        ]
        await update.message.reply_text(
            f"✅ *Зарплата добавлена!*\n\n"
            f"👤 {sal.get('name')}\n"
            f"📅 {sal.get('month')}\n"
            f"Итого: *{total:,.0f} ₽*",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"sal_month error: {e}")
        reset_connection()
        await update.message.reply_text(
            "❌ Ошибка при сохранении."
        )
    context.user_data.pop("new_sal", None)
    return ConversationHandler.END

async def pay_sal(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    try:
        ws = get_worksheet("ЗАРПЛАТЫ")
        records = ws.get_all_records() if ws else []
        unpaid = [
            r for r in records
            if r.get("Статус") == "Не выплачено"
        ]
        if not unpaid:
            kb = [[InlineKeyboardButton(
                "◀️ Назад", callback_data="fin_sal"
            )]]
            await query.edit_message_text(
                "✅ Все зарплаты выплачены!",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return

        sal_buttons = []
        for r in unpaid:
            label = (
                f"{r.get('Сотрудник')} | "
                f"{r.get('Месяц')} | "
                f"{r.get('Итого')} ₽"
            )
            sal_buttons.append([InlineKeyboardButton(
                label, callback_data=f"paysal_{r.get('ID')}"
            )])
        sal_buttons.append([InlineKeyboardButton(
            "◀️ Отмена", callback_data="fin_sal"
        )])
        await query.edit_message_text(
            "✅ *Отметить выплату*\n\nВыберите сотрудника:",
            reply_markup=InlineKeyboardMarkup(sal_buttons),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"pay_sal error: {e}")

async def pay_sal_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    sal_id = query.data.replace("paysal_", "")
    try:
        ws = get_worksheet("ЗАРПЛАТЫ")
        records = ws.get_all_records()
        for i, r in enumerate(records):
            if str(r.get("ID")) == str(sal_id):
                today = datetime.now().strftime("%d.%m.%Y")
                ws.update_cell(i + 2, 7, "Выплачено")
                ws.update_cell(i + 2, 8, today)
                break
        kb = [[InlineKeyboardButton(
            "◀️ К зарплатам", callback_data="fin_sal"
        )]]
        await query.edit_message_text(
            "✅ *Зарплата выплачена!*",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"pay_sal_confirm error: {e}")
        reset_connection()

# ===== ОТЧЁТЫ =====
async def fin_reports(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    kb = [
        [InlineKeyboardButton("📅 За день", callback_data="report_day")],
        [InlineKeyboardButton("📅 За неделю", callback_data="report_week")],
        [InlineKeyboardButton("📅 За месяц", callback_data="report_month")],
        [InlineKeyboardButton("🚗 По машине", callback_data="report_car")],
        [InlineKeyboardButton("📈 P&L", callback_data="report_pl")],
        [InlineKeyboardButton("⚖️ Все долги", callback_data="report_debts")],
        [InlineKeyboardButton("❓ Инструкция", callback_data="inst_reports")],
        [InlineKeyboardButton("◀️ Назад", callback_data="finance_menu")],
    ]
    await query.edit_message_text(
        "📊 *Отчёты*\n\nВыберите тип:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

def build_period_report(records, label):
    if not records:
        return f"📊 *{label}*\n\nДанных нет."
    income_rub = sum(
        float(r.get("Сумма", 0)) for r in records
        if r.get("Тип") == "Входящий"
        and r.get("Валюта") == "RUB"
    )
    income_cny = sum(
        float(r.get("Сумма", 0)) for r in records
        if r.get("Тип") == "Входящий"
        and r.get("Валюта") == "CNY"
    )
    expense_rub = sum(
        float(r.get("Сумма", 0)) for r in records
        if r.get("Тип") == "Исходящий"
        and r.get("Валюта") == "RUB"
    )
    expense_cny = sum(
        float(r.get("Сумма", 0)) for r in records
        if r.get("Тип") == "Исходящий"
        and r.get("Валюта") == "CNY"
    )
    text = f"📊 *{label}*\n\n"
    text += "📥 *Доходы:*\n"
    text += f"   ₽: *{income_rub:,.0f}*\n"
    text += f"   ¥: *{income_cny:,.0f}*\n\n"
    text += "📤 *Расходы:*\n"
    text += f"   ₽: *{expense_rub:,.0f}*\n"
    text += f"   ¥: *{expense_cny:,.0f}*\n\n"
    text += "─────────────\n"
    text += f"💵 Итого ₽: *{income_rub - expense_rub:,.0f}*\n"
    text += f"💴 Итого ¥: *{income_cny - expense_cny:,.0f}*\n"
    text += f"📋 Операций: {len(records)}"
    return text

async def report_day(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    try:
        ws = get_worksheet("ПЛАТЕЖИ")
        records = ws.get_all_records() if ws else []
        today = datetime.now().strftime("%d.%m.%Y")
        filtered = [r for r in records if r.get("Дата") == today]
        kb = [[InlineKeyboardButton(
            "◀️ К отчётам", callback_data="fin_reports"
        )]]
        await query.edit_message_text(
            build_period_report(filtered, f"Отчёт за {today}"),
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"report_day error: {e}")

async def report_week(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    try:
        ws = get_worksheet("ПЛАТЕЖИ")
        records = ws.get_all_records() if ws else []
        today = date.today()
        filtered = []
        for r in records:
            try:
                d = datetime.strptime(
                    r.get("Дата", ""), "%d.%m.%Y"
                ).date()
                if (today - d).days <= 7:
                    filtered.append(r)
            except:
                pass
        kb = [[InlineKeyboardButton(
            "◀️ К отчётам", callback_data="fin_reports"
        )]]
        await query.edit_message_text(
            build_period_report(filtered, "Отчёт за неделю"),
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"report_week error: {e}")

async def report_month(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    try:
        ws = get_worksheet("ПЛАТЕЖИ")
        records = ws.get_all_records() if ws else []
        now = datetime.now()
        filtered = []
        for r in records:
            try:
                d = datetime.strptime(
                    r.get("Дата", ""), "%d.%m.%Y"
                )
                if d.month == now.month and d.year == now.year:
                    filtered.append(r)
            except:
                pass
        kb = [[InlineKeyboardButton(
            "◀️ К отчётам", callback_data="fin_reports"
        )]]
        await query.edit_message_text(
            build_period_report(
                filtered,
                f"Отчёт за {now.strftime('%m.%Y')}"
            ),
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"report_month error: {e}")

async def report_car_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    cars = get_all_cars()
    if not cars:
        kb = [[InlineKeyboardButton(
            "◀️ Назад", callback_data="fin_reports"
        )]]
        await query.edit_message_text(
            "❌ Машин нет.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return ConversationHandler.END

    car_buttons, _ = build_cars_keyboard(
        cars, page=0, prefix="repcar"
    )
    car_buttons.append([InlineKeyboardButton(
        "◀️ Отмена", callback_data="fin_reports"
    )])
    await query.edit_message_text(
        "🚗 *Отчёт по машине*\n\nВыберите машину:",
        reply_markup=InlineKeyboardMarkup(car_buttons),
        parse_mode="Markdown"
    )
    return REPORT_CAR

async def report_car_selected(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    car_id = query.data.replace("repcar_", "")
    try:
        pay_ws = get_worksheet("ПЛАТЕЖИ")
        debt_ws = get_worksheet("ДОЛГИ")
        pays = [
            r for r in pay_ws.get_all_records()
            if r.get("ID машины") == car_id
        ]
        debts = [
            r for r in debt_ws.get_all_records()
            if r.get("ID машины") == car_id
        ]

        text = f"🚗 *Отчёт по {car_id}*\n\n"
        if pays:
            text += "💳 *Платежи:*\n"
            for p in pays:
                icon = (
                    "📥" if p.get("Тип") == "Входящий"
                    else "📤"
                )
                cs = "¥" if p.get("Валюта") == "CNY" else "₽"
                text += (
                    f"{icon} {p.get('Категория')}: "
                    f"*{p.get('Сумма')} {cs}*"
                    f" ({p.get('Дата')})\n"
                )
        else:
            text += "💳 Платежей нет\n"

        text += "\n⚖️ *Долги:*\n"
        if debts:
            for d in debts:
                si = (
                    "❌" if d.get("Статус") == "Не оплачен"
                    else "✅"
                )
                cs = "¥" if d.get("Валюта") == "CNY" else "₽"
                text += (
                    f"{si} {d.get('Кто должен')} → "
                    f"{d.get('Кому должен')}: "
                    f"*{d.get('Сумма')} {cs}*\n"
                )
        else:
            text += "Долгов нет\n"

        kb = [
            [InlineKeyboardButton(
                "📋 Открыть карточку",
                callback_data=f"car_{car_id}"
            )],
            [InlineKeyboardButton(
                "◀️ К отчётам", callback_data="fin_reports"
            )],
        ]
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"report_car_selected error: {e}")
    return ConversationHandler.END

async def report_pl(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    try:
        pay_ws = get_worksheet("ПЛАТЕЖИ")
        sal_ws = get_worksheet("ЗАРПЛАТЫ")
        pays = pay_ws.get_all_records() if pay_ws else []
        sals = sal_ws.get_all_records() if sal_ws else []
        now = datetime.now()

        month_pays = []
        for r in pays:
            try:
                d = datetime.strptime(
                    r.get("Дата", ""), "%d.%m.%Y"
                )
                if d.month == now.month and d.year == now.year:
                    month_pays.append(r)
            except:
                pass

        income_rub = sum(
            float(r.get("Сумма", 0)) for r in month_pays
            if r.get("Тип") == "Входящий"
            and r.get("Валюта") == "RUB"
        )
        income_cny = sum(
            float(r.get("Сумма", 0)) for r in month_pays
            if r.get("Тип") == "Входящий"
            and r.get("Валюта") == "CNY"
        )
        expense_rub = sum(
            float(r.get("Сумма", 0)) for r in month_pays
            if r.get("Тип") == "Исходящий"
            and r.get("Валюта") == "RUB"
        )
        expense_cny = sum(
            float(r.get("Сумма", 0)) for r in month_pays
            if r.get("Тип") == "Исходящий"
            and r.get("Валюта") == "CNY"
        )
        month_str = now.strftime("%m.%Y")
        sal_total = sum(
            float(r.get("Итого", 0)) for r in sals
            if r.get("Месяц") == month_str
        )

        text = f"📈 *P&L за {month_str}*\n\n"
        text += "📥 *ДОХОДЫ:*\n"
        text += f"   ₽: *{income_rub:,.0f}*\n"
        text += f"   ¥: *{income_cny:,.0f}*\n\n"
        text += "📤 *РАСХОДЫ:*\n"
        text += f"   ₽: *{expense_rub:,.0f}*\n"
        text += f"   ¥: *{expense_cny:,.0f}*\n\n"
        text += f"👥 *Зарплаты: {sal_total:,.0f} ₽*\n\n"
        text += "─────────────\n"
        text += (
            f"💵 *Итого ₽: "
            f"{income_rub - expense_rub - sal_total:,.0f}*\n"
        )
        text += f"💴 *Итого ¥: {income_cny - expense_cny:,.0f}*"

        kb = [[InlineKeyboardButton(
            "◀️ К отчётам", callback_data="fin_reports"
        )]]
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"report_pl error: {e}")

async def report_debts(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    try:
        ws = get_worksheet("ДОЛГИ")
        records = ws.get_all_records() if ws else []
        unpaid = [
            r for r in records
            if r.get("Статус") == "Не оплачен"
        ]
        paid = [
            r for r in records
            if r.get("Статус") == "Оплачен"
        ]

        text = "⚖️ *Все долги*\n\n"
        if unpaid:
            text += f"❌ *Не оплачено: {len(unpaid)}*\n\n"
            for d in unpaid:
                cs = "¥" if d.get("Валюта") == "CNY" else "₽"
                text += (
                    f"  *{d.get('ID долга')}* | "
                    f"{d.get('ID машины')}\n"
                    f"  {d.get('Кто должен')} → "
                    f"{d.get('Кому должен')}: "
                    f"*{d.get('Сумма')} {cs}*\n\n"
                )
        else:
            text += "✅ *Все долги погашены!*\n\n"
        text += f"✅ Закрыто всего: {len(paid)}"

        kb = [[InlineKeyboardButton(
            "◀️ К отчётам", callback_data="fin_reports"
        )]]
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"report_debts error: {e}")

# ===== СМЕНА ПАРОЛЯ =====
async def fin_change_password(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != BOSS_ID:
        kb = [[InlineKeyboardButton(
            "◀️ Назад", callback_data="finance_menu"
        )]]
        await query.edit_message_text(
            "❌ Только руководитель может менять пароль.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return ConversationHandler.END
    kb = [[InlineKeyboardButton(
        "❌ Отмена", callback_data="finance_menu"
    )]]
    await query.edit_message_text(
        "🔑 *Смена пароля*\n\nШаг 1 из 2\n\n"
        "Введите *текущий пароль*:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return OLD_PASSWORD

async def handle_old_password(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    old_pass = update.message.text.strip()
    await update.message.delete()
    saved = context.bot_data.get("finance_password")
    if old_pass != saved:
        kb = [
            [InlineKeyboardButton(
                "🔄 Попробовать снова",
                callback_data="fin_chpass"
            )],
            [InlineKeyboardButton(
                "◀️ В финансы", callback_data="finance_menu"
            )],
        ]
        await update.message.chat.send_message(
            "❌ *Неверный текущий пароль.*",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    kb = [[InlineKeyboardButton(
        "❌ Отмена", callback_data="finance_menu"
    )]]
    await update.message.chat.send_message(
        "✅ Верно.\n\nШаг 2 из 2\n\nВведите *новый пароль*:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return SET_PASSWORD

# ===== РОУТЕР КНОПОК =====
async def button_router(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    data = update.callback_query.data

    routes = {
        "yuan": show_yuan,
        "duty": show_duty,
        "menu": start,
        "finance_menu": finance_menu,
        "fin_cars": fin_cars,
        "fin_pay": fin_pay_start,
        "fin_debts": fin_debts,
        "add_debt": add_debt_start,
        "close_debt": close_debt,
        "fin_sal": fin_sal,
        "add_sal": add_sal_start,
        "pay_sal": pay_sal,
        "fin_reports": fin_reports,
        "report_day": report_day,
        "report_week": report_week,
        "report_month": report_month,
        "report_pl": report_pl,
        "report_debts": report_debts,
        "car_search": car_search_start,
    }

    inst_keys = [
        "inst_main", "inst_cars", "inst_pay",
        "inst_debts", "inst_sal", "inst_reports"
    ]

    if data in routes:
        await routes[data](update, context)
    elif data in inst_keys:
        await show_instruction(update, context)
    elif data.startswith("car_") and not data.startswith("car_search"):
        await show_car_card(update, context)
    elif data.startswith("carpage_"):
        await cars_page_nav(update, context)
    elif data.startswith("editcar_"):
        await edit_car_menu(update, context)
    elif data.startswith("payfromcar_"):
        await pay_from_car(update, context)
    elif data.startswith("debtsfromcar_"):
        await debts_from_car(update, context)
    elif data.startswith("delcar_"):
        await delete_car_confirm(update, context)
    elif data.startswith("delconfirm_"):
        await delete_car_execute(update, context)
    elif data.startswith("closedebt_"):
        await close_debt_confirm(update, context)
    elif data.startswith("paysal_"):
        await pay_sal_confirm(update, context)
    elif data.startswith("repcar_"):
        # Из отчёта по машине
        await report_car_selected(update, context)
    elif data.startswith("editclient_"):
        await edit_car_client_type(update, context)

# ===== ЗАПУСК =====
def main():
    Thread(target=run_flask, daemon=True).start()

    logger.info("Подключаемся к Google Sheets...")
    get_spreadsheet()

    app = Application.builder().token(BOT_TOKEN).build()

    # Вход в финансы
    auth_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(
            finance_enter, pattern="^finance_enter$"
        )],
        states={
            SET_PASSWORD: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                handle_set_password
            )],
            ENTER_PASSWORD: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                handle_enter_password
            )],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Смена пароля
    chpass_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(
            fin_change_password, pattern="^fin_chpass$"
        )],
        states={
            OLD_PASSWORD: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                handle_old_password
            )],
            SET_PASSWORD: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                handle_set_password
            )],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Добавление машины
    car_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(
            add_car_start, pattern="^add_car$"
        )],
        states={
            ADD_CAR_MARK_MODEL: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                add_car_mark_model
            )],
            ADD_CAR_YEAR: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                add_car_year
            )],
            ADD_CAR_COLOR: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                add_car_color
            )],
            ADD_CAR_VIN: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    add_car_vin
                ),
                CallbackQueryHandler(
                    skip_vin, pattern="^skip_vin$"
                ),
            ],
            ADD_CAR_CLIENT: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                add_car_client
            )],
            ADD_CAR_CLIENT_TYPE: [CallbackQueryHandler(
                add_car_client_type, pattern="^client_"
            )],
            ADD_CAR_COMPANY: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                add_car_company
            )],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Редактирование машины
    edit_car_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(
            edit_car_menu, pattern="^editcar_"
        )],
        states={
            EDIT_CAR_FIELD: [CallbackQueryHandler(
                edit_car_field_selected, pattern="^editfield_"
            )],
            EDIT_CAR_VALUE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    edit_car_value
                ),
                CallbackQueryHandler(
                    edit_car_client_type,
                    pattern="^editclient_"
                ),
            ],
            EDIT_CAR_CLIENT_TYPE: [CallbackQueryHandler(
                edit_car_client_type, pattern="^editclient_"
            )],
            EDIT_CAR_COMPANY: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                edit_car_company
            )],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Поиск машин
    search_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(
            car_search_start, pattern="^car_search$"
        )],
        states={
            CAR_SEARCH: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                car_search_execute
            )],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Платёж из карточки машины
    pay_from_car_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(
            pay_from_car, pattern="^payfromcar_"
        )],
        states={
            PAY_CATEGORY: [CallbackQueryHandler(
                pay_category_selected, pattern="^paycat_"
            )],
            PAY_AMOUNT: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                pay_amount
            )],
            PAY_COMMENT: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                pay_comment
            )],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Платёж из общего меню
    pay_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(
            fin_pay_start, pattern="^fin_pay$"
        )],
        states={
            PAY_CAR: [CallbackQueryHandler(
                pay_car_selected, pattern="^paycar_"
            )],
            PAY_CATEGORY: [CallbackQueryHandler(
                pay_category_selected, pattern="^paycat_"
            )],
            PAY_AMOUNT: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                pay_amount
            )],
            PAY_COMMENT: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                pay_comment
            )],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Долги
    debt_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(
            add_debt_start, pattern="^add_debt$"
        )],
        states={
            DEBT_CAR: [CallbackQueryHandler(
                debt_car_selected, pattern="^debtcar_"
            )],
            DEBT_WHO: [CallbackQueryHandler(
                debt_type_selected, pattern="^debttype_"
            )],
            DEBT_AMOUNT: [MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                debt_amount
            )],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Зарплаты
    sal_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(
            add_sal_start, pattern="^add_sal$"
        )],
        states={
            SAL_NAME: [MessageHandler(
                filters.TEXT & ~filters.COMMAND, sal_name
            )],
            SAL_OKLAD: [MessageHandler(
                filters.TEXT & ~filters.COMMAND, sal_oklad
            )],
            SAL_BONUS: [MessageHandler(
                filters.TEXT & ~filters.COMMAND, sal_bonus
            )],
            SAL_MONTH: [MessageHandler(
                filters.TEXT & ~filters.COMMAND, sal_month
            )],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Отчёт по машине
    repcar_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(
            report_car_start, pattern="^report_car$"
        )],
        states={
            REPORT_CAR: [CallbackQueryHandler(
                report_car_selected, pattern="^repcar_"
            )],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Порядок важен!
    app.add_handler(auth_conv)
    app.add_handler(chpass_conv)
    app.add_handler(search_conv)
    app.add_handler(car_conv)
    app.add_handler(edit_car_conv)
    app.add_handler(pay_from_car_conv)
    app.add_handler(pay_conv)
    app.add_handler(debt_conv)
    app.add_handler(sal_conv)
    app.add_handler(repcar_conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_router))

    logger.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
