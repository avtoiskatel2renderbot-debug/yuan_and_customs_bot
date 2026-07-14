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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== СОСТОЯНИЯ ДИАЛОГОВ =====
(
    SET_PASSWORD, ENTER_PASSWORD, OLD_PASSWORD,
    ADD_CAR_MARK_MODEL, ADD_CAR_YEAR,
    ADD_CAR_COLOR, ADD_CAR_CLIENT,
    ADD_CAR_CLIENT_TYPE, ADD_CAR_COMPANY,
    EDIT_CAR_SELECT, EDIT_CAR_FIELD, EDIT_CAR_VALUE,
    DELETE_CAR_CONFIRM,
    PAY_CAR, PAY_CATEGORY, PAY_AMOUNT, PAY_COMMENT,
    DEBT_CAR, DEBT_WHO, DEBT_AMOUNT,
    SAL_NAME, SAL_OKLAD, SAL_BONUS, SAL_MONTH,
    REPORT_CAR
) = range(25)

# ===== FLASK ДЛЯ UPTIMEROBOT =====
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# ===== GOOGLE SHEETS — ОДНО ПОДКЛЮЧЕНИЕ =====
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
        logger.info("Google Sheets подключён успешно")
        return _spreadsheet
    except Exception as e:
        logger.error(f"Google Sheets connection error: {e}")
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
    try:
        ws = get_worksheet("МАШИНЫ")
        if not ws:
            return []
        return ws.get_all_records()
    except Exception as e:
        logger.error(f"get_all_cars error: {e}")
        return []

def reset_connection():
    global _spreadsheet
    _spreadsheet = None

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

# ===== СТАВКИ ПОШЛИН =====
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

# ===== ТЕКСТЫ ИНСТРУКЦИЙ =====
INSTRUCTION_MAIN = """
📖 *ИНСТРУКЦИЯ — ФИНАНСОВЫЙ РАЗДЕЛ*

Этот раздел помогает вести учёт всех денег по каждой машине.

*С чего начать:*
1️⃣ Добавь машину в раздел 🚗 *Машины*
2️⃣ Записывай платежи через ➕ *Добавить платёж*
3️⃣ Фиксируй долги в разделе ⚖️ *Долги*
4️⃣ Зарплаты сотрудников — в разделе 👥 *Зарплаты*
5️⃣ Смотри итоги в разделе 📊 *Отчёты*

❗ *Важно:* нельзя добавить платёж или долг
без машины. Всегда начинай с добавления машины.

Выбери раздел для подробной инструкции 👇
"""

INSTRUCTION_CARS = """
📖 *ИНСТРУКЦИЯ — МАШИНЫ*

Каждой машине автоматически присваивается
номер: AUTO-001, AUTO-002 и т.д.

*➕ Добавить машину — пошагово:*
1️⃣ Марку и модель — одним сообщением:
   Пример: *Zeekr 001*
2️⃣ Год: *2024*
3️⃣ Цвет: *Белый*
4️⃣ ФИО клиента по паспорту:
   *Иванов Иван Иванович*
5️⃣ Тип: 👤 Физлицо или 🏢 Юрлицо
6️⃣ Если юрлицо — название компании:
   *ООО Автомир*

*Формат в таблице:*
Физлицо: Иванов Иван Иванович (физлицо)
Юрлицо: Петров Пётр Петрович (юрлицо — ООО Автомир)

*✏️ Редактировать машину:*
Выбери машину → выбери поле → введи новое значение

*🗑 Удалить машину:*
Удаляй только после закрытия всех долгов.
При удалении стираются ВСЕ данные по машине.
"""

INSTRUCTION_PAY = """
📖 *ИНСТРУКЦИЯ — ПЛАТЕЖИ*

*➕ Как добавить платёж:*
1️⃣ Выбери машину
2️⃣ Выбери категорию:

📥 *ДОХОДЫ:*
• 💰 Накрутка — твоя прибыль (юани ¥)
• 💵 Допы от клиента (рубли ₽)

📤 *РАСХОДЫ:*
• 🚛 Автовоз (рубли ₽)
• 🏛 Таможенный брокер (рубли ₽)
• 🔧 Допы в Китае (юани ¥)
• ⛽ Бензин (рубли ₽)
• 💸 Кэшбэк юрику (юани ¥)
• 👤 % Менеджеру — 20 000₽ фикс

3️⃣ Введи сумму цифрами: *2800*
4️⃣ Комментарий или «-» чтобы пропустить

*Валюта определяется автоматически.*
"""

INSTRUCTION_DEBTS = """
📖 *ИНСТРУКЦИЯ — ДОЛГИ*

Долг — это когда кто-то кому-то должен,
но ещё не заплатил.

*Типы долгов:*
• Клиент должен нам за допы
• Мы должны клиенту (переплата)
• Мы должны поставщику за допы
• Мы должны юрику кэшбэк
• Мы должны менеджеру %
• Мы должны брокеру
• Мы должны автовозу

*➕ Добавить долг:*
Выбери машину → тип долга → введи сумму

*✅ Закрыть долг (когда оплатили):*
Нажми ✅ Закрыть долг → выбери долг

❗ После закрытия долга запиши платёж
в разделе ➕ Платёж
"""

INSTRUCTION_SAL = """
📖 *ИНСТРУКЦИЯ — ЗАРПЛАТЫ*

*➕ Добавить зарплату:*
1️⃣ Имя сотрудника: *Иванов Кирилл*
2️⃣ Оклад: *30000*
3️⃣ Бонус: *5000* (нет бонуса → *0*)
4️⃣ Месяц: *07.2026*
Итого считается автоматически.

*✅ Отметить выплату:*
Нажми ✅ Отметить выплату → выбери сотрудника

*Статусы:*
❌ — не выплачено
✅ — выплачено
"""

INSTRUCTION_REPORTS = """
📖 *ИНСТРУКЦИЯ — ОТЧЁТЫ*

*📅 За день* — платежи за сегодня
*📅 За неделю* — платежи за 7 дней
*📅 За месяц* — платежи текущего месяца
*🚗 По машине* — все данные по одной машине
*📈 P&L* — прибыль и убытки за месяц
*⚖️ Все долги* — список открытых долгов

*Как читать:*
📥 доход | 📤 расход
¥ юани | ₽ рубли
❌ не оплачено | ✅ оплачено
"""

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
            "❌ Не удалось получить курс ЦБ. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup(kb)
        )

# ===== ФИНАНСЫ — ВХОД С ПАРОЛЕМ =====
async def finance_enter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Исправленный вход в финансы.
    Теперь работает с любого аккаунта — больше не молчит.
    """
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    saved_password = context.bot_data.get("finance_password")

    # Пароль не задан — только босс может задать
    if not saved_password:
        if user_id == BOSS_ID:
            kb = [[InlineKeyboardButton(
                "◀️ В меню", callback_data="menu"
            )]]
            await query.edit_message_text(
                "🔐 *Пароль не задан*\n\n"
                "Вы первый раз входите в финансы.\n"
                "Придумайте и введите пароль:",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown"
            )
            return SET_PASSWORD
        else:
            kb = [[InlineKeyboardButton(
                "◀️ В меню", callback_data="menu"
            )]]
            await query.edit_message_text(
                "🔒 *Финансовый раздел защищён*\n\n"
                "Пароль ещё не задан руководителем.\n"
                "Обратитесь к руководителю.",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown"
            )
            return ConversationHandler.END

    # Проверяем авторизован ли пользователь в этой сессии
    if context.user_data.get("finance_auth"):
        await show_finance_menu(query, context)
        return ConversationHandler.END

    # Запрашиваем пароль — ВСЕГДА, с любого аккаунта
    kb = [[InlineKeyboardButton(
        "◀️ В меню", callback_data="menu"
    )]]
    await query.edit_message_text(
        "🔐 *Финансовый раздел*\n\n"
        "Введите пароль для входа:",
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
        "✅ *Пароль установлен!*\n\n"
        "Теперь финансовый раздел защищён паролем.",
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
            "✅ *Пароль верный! Добро пожаловать.*",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    else:
        kb = [
            [InlineKeyboardButton(
                "🔄 Попробовать ещё раз",
                callback_data="finance_enter"
            )],
            [InlineKeyboardButton(
                "◀️ В меню", callback_data="menu"
            )],
        ]
        await update.message.chat.send_message(
            "❌ *Неверный пароль.*\n\nПопробуйте ещё раз.",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    return ConversationHandler.END

# ===== МЕНЮ ФИНАНСОВ =====
async def show_finance_menu(query_or_update, context):
    kb = [
        [InlineKeyboardButton(
            "🚗 Машины", callback_data="fin_cars"
        )],
        [InlineKeyboardButton(
            "➕ Добавить платёж", callback_data="fin_pay"
        )],
        [InlineKeyboardButton(
            "⚖️ Долги", callback_data="fin_debts"
        )],
        [InlineKeyboardButton(
            "👥 Зарплаты", callback_data="fin_sal"
        )],
        [InlineKeyboardButton(
            "📊 Отчёты", callback_data="fin_reports"
        )],
        [InlineKeyboardButton(
            "📖 Инструкция", callback_data="inst_main"
        )],
        [InlineKeyboardButton(
            "🔑 Сменить пароль", callback_data="fin_chpass"
        )],
        [InlineKeyboardButton(
            "◀️ В главное меню", callback_data="menu"
        )],
    ]
    text = (
        "💰 *Финансовый раздел*\n\n"
        "🚗 *Машины* — список, добавление, редактирование\n"
        "➕ *Платёж* — записать доход или расход\n"
        "⚖️ *Долги* — кто кому должен\n"
        "👥 *Зарплаты* — учёт зарплат\n"
        "📊 *Отчёты* — за день, неделю, месяц, P&L\n"
        "📖 *Инструкция* — как пользоваться\n\n"
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
            "🔒 *Требуется авторизация*\n\n"
            "Нажмите кнопку чтобы войти в финансовый раздел.",
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

    back = [InlineKeyboardButton(
        "◀️ К инструкции", callback_data="inst_main"
    )]

    instructions = {
        "inst_main": (INSTRUCTION_MAIN, [
            [InlineKeyboardButton(
                "🚗 Машины", callback_data="inst_cars"
            )],
            [InlineKeyboardButton(
                "➕ Платежи", callback_data="inst_pay"
            )],
            [InlineKeyboardButton(
                "⚖️ Долги", callback_data="inst_debts"
            )],
            [InlineKeyboardButton(
                "👥 Зарплаты", callback_data="inst_sal"
            )],
            [InlineKeyboardButton(
                "📊 Отчёты", callback_data="inst_reports"
            )],
            [InlineKeyboardButton(
                "◀️ В финансы", callback_data="finance_menu"
            )],
        ]),
        "inst_cars": (INSTRUCTION_CARS, [
            [back[0]],
            [InlineKeyboardButton(
                "🚗 Перейти в Машины", callback_data="fin_cars"
            )],
        ]),
        "inst_pay": (INSTRUCTION_PAY, [
            [back[0]],
            [InlineKeyboardButton(
                "➕ Добавить платёж", callback_data="fin_pay"
            )],
        ]),
        "inst_debts": (INSTRUCTION_DEBTS, [
            [back[0]],
            [InlineKeyboardButton(
                "⚖️ Перейти в Долги", callback_data="fin_debts"
            )],
        ]),
        "inst_sal": (INSTRUCTION_SAL, [
            [back[0]],
            [InlineKeyboardButton(
                "👥 Перейти в Зарплаты", callback_data="fin_sal"
            )],
        ]),
        "inst_reports": (INSTRUCTION_REPORTS, [
            [back[0]],
            [InlineKeyboardButton(
                "📊 Перейти в Отчёты", callback_data="fin_reports"
            )],
        ]),
    }

    if data in instructions:
        text, kb = instructions[data]
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )

# ===== МАШИНЫ =====
async def fin_cars(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    cars = get_all_cars()
    kb = [
        [InlineKeyboardButton(
            "➕ Добавить машину", callback_data="add_car"
        )],
        [InlineKeyboardButton(
            "✏️ Редактировать машину", callback_data="edit_car"
        )],
        [InlineKeyboardButton(
            "🗑 Удалить машину", callback_data="delete_car"
        )],
        [InlineKeyboardButton(
            "❓ Инструкция", callback_data="inst_cars"
        )],
        [InlineKeyboardButton(
            "◀️ Назад", callback_data="finance_menu"
        )],
    ]
    if not cars:
        text = (
            "🚗 *Машины*\n\n"
            "Машин пока нет.\n\n"
            "Нажми ➕ *Добавить машину* чтобы начать."
        )
    else:
        text = f"🚗 *Машины* — всего: {len(cars)}\n\n"
        for car in cars[-10:]:
            text += (
                f"*{car.get('ID', '—')}* — "
                f"{car.get('Марка', '—')} "
                f"{car.get('Год', '—')}\n"
                f"👤 {car.get('Клиент', '—')}\n\n"
            )
        if len(cars) > 10:
            text += f"_...и ещё {len(cars) - 10} машин_\n"
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

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
        "Шаг 1 из 4\n\n"
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
        "Шаг 2 из 4\n\nВведите *год выпуска*:\n"
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
        "Шаг 3 из 4\n\nВведите *цвет*:\n"
        "_Пример: Белый, Чёрный, Серый_",
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
    kb = [[InlineKeyboardButton(
        "❌ Отмена", callback_data="fin_cars"
    )]]
    await update.message.reply_text(
        "Шаг 4 из 4\n\n"
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
        # Физлицо — сразу сохраняем
        context.user_data["new_car"]["Клиент"] = (
            f"{fio} (физлицо)"
        )
        context.user_data["new_car"]["Тип клиента"] = "Физлицо"
        return await save_new_car(query, context)

    else:
        # Юрлицо — спрашиваем название компании
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

    # Создаём фиктивный query-подобный объект не нужен —
    # вызываем сохранение напрямую через message
    return await save_new_car_from_message(
        update.message, context
    )

async def save_new_car(query, context):
    """Сохранение машины когда последний шаг — кнопка"""
    car = context.user_data["new_car"]
    try:
        ws = get_worksheet("МАШИНЫ")
        if not ws:
            raise Exception("Нет подключения к таблице")
        car_id = get_next_id("МАШИНЫ", "AUTO")
        today = datetime.now().strftime("%d.%m.%Y")
        row = [
            car_id,
            car.get("Марка", ""),
            "",
            car.get("Год", ""),
            car.get("Цвет", ""),
            "", "",
            car.get("Клиент", ""),
            car.get("Тип клиента", ""),
            today
        ]
        ws.append_row(row)
        kb = [
            [InlineKeyboardButton(
                "➕ Добавить платёж", callback_data="fin_pay"
            )],
            [InlineKeyboardButton(
                "◀️ К машинам", callback_data="fin_cars"
            )],
        ]
        await query.edit_message_text(
            f"✅ *Машина добавлена!*\n\n"
            f"🆔 Номер: *{car_id}*\n"
            f"🚗 {car.get('Марка')} {car.get('Год')}\n"
            f"🎨 Цвет: {car.get('Цвет')}\n"
            f"👤 {car.get('Клиент')}\n\n"
            f"_Теперь можно добавить платежи._",
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
            "❌ *Ошибка при сохранении.*\n\nПопробуйте ещё раз.",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    context.user_data.pop("new_car", None)
    return ConversationHandler.END

async def save_new_car_from_message(message, context):
    """Сохранение машины когда последний шаг — текст"""
    car = context.user_data["new_car"]
    try:
        ws = get_worksheet("МАШИНЫ")
        if not ws:
            raise Exception("Нет подключения к таблице")
        car_id = get_next_id("МАШИНЫ", "AUTO")
        today = datetime.now().strftime("%d.%m.%Y")
        row = [
            car_id,
            car.get("Марка", ""),
            "",
            car.get("Год", ""),
            car.get("Цвет", ""),
            "", "",
            car.get("Клиент", ""),
            car.get("Тип клиента", ""),
            today
        ]
        ws.append_row(row)
        kb = [
            [InlineKeyboardButton(
                "➕ Добавить платёж", callback_data="fin_pay"
            )],
            [InlineKeyboardButton(
                "◀️ К машинам", callback_data="fin_cars"
            )],
        ]
        await message.reply_text(
            f"✅ *Машина добавлена!*\n\n"
            f"🆔 Номер: *{car_id}*\n"
            f"🚗 {car.get('Марка')} {car.get('Год')}\n"
            f"🎨 Цвет: {car.get('Цвет')}\n"
            f"👤 {car.get('Клиент')}\n\n"
            f"_Теперь можно добавить платежи._",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"save_new_car_from_message error: {e}")
        reset_connection()
        kb = [[InlineKeyboardButton(
            "◀️ Назад", callback_data="fin_cars"
        )]]
        await message.reply_text(
            "❌ *Ошибка при сохранении.*\n\nПопробуйте ещё раз.",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    context.user_data.pop("new_car", None)
    return ConversationHandler.END

# ===== РЕДАКТИРОВАНИЕ МАШИНЫ =====
# Поля которые можно редактировать
EDIT_FIELDS = [
    ("🚗 Марка и модель", "Марка", 2),
    ("📅 Год", "Год", 4),
    ("🎨 Цвет", "Цвет", 5),
    ("👤 ФИО клиента", "Клиент_ФИО", 8),
]

async def edit_car_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    cars = get_all_cars()

    if not cars:
        kb = [[InlineKeyboardButton(
            "◀️ Назад", callback_data="fin_cars"
        )]]
        await query.edit_message_text(
            "❌ Машин нет.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return ConversationHandler.END

    car_buttons = []
    for car in cars[-10:]:
        label = (
            f"{car['ID']} — "
            f"{car.get('Марка', '—')} | "
            f"{car.get('Год', '—')}"
        )
        car_buttons.append([InlineKeyboardButton(
            label, callback_data=f"editcar_{car['ID']}"
        )])
    car_buttons.append([InlineKeyboardButton(
        "◀️ Отмена", callback_data="fin_cars"
    )])

    await query.edit_message_text(
        "✏️ *Редактирование машины*\n\n"
        "Выберите машину для редактирования:",
        reply_markup=InlineKeyboardMarkup(car_buttons),
        parse_mode="Markdown"
    )
    return EDIT_CAR_SELECT

async def edit_car_selected(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    car_id = query.data.replace("editcar_", "")
    context.user_data["edit_car_id"] = car_id

    # Показываем текущие данные машины
    cars = get_all_cars()
    car_data = next(
        (c for c in cars if c.get("ID") == car_id), None
    )

    field_buttons = []
    for label, field, col in EDIT_FIELDS:
        field_buttons.append([InlineKeyboardButton(
            label, callback_data=f"editfield_{field}"
        )])
    field_buttons.append([InlineKeyboardButton(
        "◀️ Отмена", callback_data="fin_cars"
    )])

    current = ""
    if car_data:
        current = (
            f"*Текущие данные:*\n"
            f"🚗 {car_data.get('Марка', '—')} "
            f"{car_data.get('Год', '—')}\n"
            f"🎨 {car_data.get('Цвет', '—')}\n"
            f"👤 {car_data.get('Клиент', '—')}\n\n"
        )

    await query.edit_message_text(
        f"✏️ *Редактирование {car_id}*\n\n"
        f"{current}"
        f"Что хотите изменить?",
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
        "Марка": (
            "Введите новую *марку и модель*:\n"
            "_Пример: Zeekr 001_"
        ),
        "Год": (
            "Введите новый *год выпуска*:\n"
            "_Пример: 2024_"
        ),
        "Цвет": (
            "Введите новый *цвет*:\n"
            "_Пример: Белый_"
        ),
        "Клиент_ФИО": (
            "Введите новое *ФИО клиента по паспорту*:\n"
            "_Пример: Иванов Иван Иванович_\n\n"
            "После этого выберете тип клиента."
        ),
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

    # Если редактируем клиента — спрашиваем тип
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
        return EDIT_CAR_VALUE

    # Для остальных полей — сразу сохраняем
    field_to_col = {
        "Марка": 2,
        "Год": 4,
        "Цвет": 5,
    }
    col = field_to_col.get(field)

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
                "◀️ К машинам", callback_data="fin_cars"
            )],
        ]
        await update.message.reply_text(
            f"✅ *Данные обновлены!*\n\n"
            f"Машина: *{car_id}*\n"
            f"Поле изменено успешно.",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"edit_car_value error: {e}")
        reset_connection()
        await update.message.reply_text(
            "❌ Ошибка при сохранении. Попробуйте ещё раз."
        )

    context.user_data.pop("edit_car_id", None)
    context.user_data.pop("edit_field", None)
    return ConversationHandler.END

async def edit_client_type(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Выбор типа клиента при редактировании"""
    query = update.callback_query
    await query.answer()
    car_id = context.user_data.get("edit_car_id")
    fio = context.user_data.get("edit_new_fio", "")

    if query.data == "editclient_fiz":
        new_client = f"{fio} (физлицо)"
        new_type = "Физлицо"
        return await save_edited_client(
            query, context, car_id, new_client, new_type
        )
    else:
        kb = [[InlineKeyboardButton(
            "❌ Отмена", callback_data="fin_cars"
        )]]
        await query.edit_message_text(
            "Введите *название компании*:\n"
            "_Пример: ООО Автомир_",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        context.user_data["edit_field"] = "Клиент_company"
        return EDIT_CAR_VALUE

async def edit_client_company(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Ввод названия компании при редактировании"""
    company = update.message.text.strip()
    car_id = context.user_data.get("edit_car_id")
    fio = context.user_data.get("edit_new_fio", "")
    new_client = f"{fio} (юрлицо — {company})"
    new_type = "Юрлицо"

    try:
        ws = get_worksheet("МАШИНЫ")
        records = ws.get_all_records()
        for i, r in enumerate(records):
            if r.get("ID") == car_id:
                ws.update_cell(i + 2, 8, new_client)
                ws.update_cell(i + 2, 9, new_type)
                break

        kb = [
            [InlineKeyboardButton(
                "✏️ Изменить ещё",
                callback_data=f"editcar_{car_id}"
            )],
            [InlineKeyboardButton(
                "◀️ К машинам", callback_data="fin_cars"
            )],
        ]
        await update.message.reply_text(
            f"✅ *Клиент обновлён!*\n\n"
            f"👤 {new_client}",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"edit_client_company error: {e}")
        reset_connection()
        await update.message.reply_text(
            "❌ Ошибка при сохранении."
        )

    context.user_data.pop("edit_car_id", None)
    context.user_data.pop("edit_field", None)
    context.user_data.pop("edit_new_fio", None)
    return ConversationHandler.END

async def save_edited_client(
    query, context, car_id, new_client, new_type
):
    """Сохранение отредактированного клиента"""
    try:
        ws = get_worksheet("МАШИНЫ")
        records = ws.get_all_records()
        for i, r in enumerate(records):
            if r.get("ID") == car_id:
                ws.update_cell(i + 2, 8, new_client)
                ws.update_cell(i + 2, 9, new_type)
                break

        kb = [
            [InlineKeyboardButton(
                "✏️ Изменить ещё",
                callback_data=f"editcar_{car_id}"
            )],
            [InlineKeyboardButton(
                "◀️ К машинам", callback_data="fin_cars"
            )],
        ]
        await query.edit_message_text(
            f"✅ *Клиент обновлён!*\n\n"
            f"👤 {new_client}",
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
    context.user_data.pop("edit_field", None)
    context.user_data.pop("edit_new_fio", None)
    return ConversationHandler.END

# ===== УДАЛИТЬ МАШИНУ =====
async def delete_car_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    cars = get_all_cars()

    if not cars:
        kb = [[InlineKeyboardButton(
            "◀️ Назад", callback_data="fin_cars"
        )]]
        await query.edit_message_text(
            "❌ Машин нет.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return ConversationHandler.END

    car_buttons = []
    for car in cars[-10:]:
        label = (
            f"{car['ID']} — "
            f"{car.get('Марка', '—')} | "
            f"{car.get('Клиент', '—')[:20]}"
        )
        car_buttons.append([InlineKeyboardButton(
            label, callback_data=f"delcar_{car['ID']}"
        )])
    car_buttons.append([InlineKeyboardButton(
        "◀️ Отмена", callback_data="fin_cars"
    )])

    await query.edit_message_text(
        "🗑 *Удаление машины*\n\n"
        "⚠️ Удаляй только после закрытия всех долгов!\n\n"
        "Выберите машину для удаления:",
        reply_markup=InlineKeyboardMarkup(car_buttons),
        parse_mode="Markdown"
    )
    return DELETE_CAR_CONFIRM

async def delete_car_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    car_id = query.data.replace("delcar_", "")

    kb = [
        [InlineKeyboardButton(
            "🗑 Да, удалить всё",
            callback_data=f"delconfirm_{car_id}"
        )],
        [InlineKeyboardButton(
            "❌ Отмена", callback_data="fin_cars"
        )],
    ]
    await query.edit_message_text(
        f"⚠️ *Подтверждение удаления*\n\n"
        f"Машина: *{car_id}*\n\n"
        f"Будут удалены:\n"
        f"• Карточка машины\n"
        f"• Все платежи по этой машине\n"
        f"• Все долги по этой машине\n\n"
        f"*Это действие нельзя отменить!*",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

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
            f"✅ *Машина {car_id} удалена.*\n\n"
            f"Все связанные платежи и долги тоже удалены.",
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
            "❌ Ошибка при удалении. Попробуйте ещё раз.",
            reply_markup=InlineKeyboardMarkup(kb)
        )

# ===== КАТЕГОРИИ ПЛАТЕЖЕЙ =====
CATEGORIES = [
    ("💰 Накрутка — моя прибыль (юани ¥)",
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
    ("👤 % Менеджеру 20 000₽ (фикс)",
     "% Менеджеру", "RUB", "Исходящий"),
]

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
    car_buttons = []
    for car in cars[-10:]:
        label = f"{car['ID']} — {car.get('Марка', '—')}"
        car_buttons.append([InlineKeyboardButton(
            label, callback_data=f"paycar_{car['ID']}"
        )])
    car_buttons.append([InlineKeyboardButton(
        "◀️ Отмена", callback_data="finance_menu"
    )])
    await query.edit_message_text(
        "➕ *Добавить платёж*\n\n"
        "Шаг 1 из 3 — Выберите машину:",
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
        f"Машина: *{car_id}*\n\n"
        "Шаг 2 из 3 — Выберите категорию:",
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

    kb = [[InlineKeyboardButton(
        "❌ Отмена", callback_data="finance_menu"
    )]]

    if cat_name == "% Менеджеру":
        context.user_data["new_pay"]["amount"] = "20000"
        await query.edit_message_text(
            f"Категория: *{cat_name}*\n"
            f"Сумма: *20 000 ₽* (фиксированная)\n\n"
            f"Шаг 3 из 3\n\n"
            f"Добавить комментарий?\n"
            f"_Текст или «-» чтобы пропустить_",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    else:
        cl = "юанях (¥)" if currency == "CNY" else "рублях (₽)"
        await query.edit_message_text(
            f"Категория: *{cat_name}*\n\n"
            f"Шаг 3 из 3\n\n"
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
        kb = [
            [InlineKeyboardButton(
                "➕ Ещё платёж", callback_data="fin_pay"
            )],
            [InlineKeyboardButton(
                "◀️ В финансы", callback_data="finance_menu"
            )],
        ]
        await update.message.reply_text(
            f"✅ *Платёж записан!*\n\n"
            f"🆔 {pay_id}\n"
            f"🚗 Машина: *{pay.get('car_id')}*\n"
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
            "❌ Ошибка при сохранении. Попробуйте ещё раз."
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
    car_buttons = []
    for car in cars[-10:]:
        label = f"{car['ID']} — {car.get('Марка', '—')}"
        car_buttons.append([InlineKeyboardButton(
            label, callback_data=f"debtcar_{car['ID']}"
        )])
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
        f"*{label}*\n\n"
        f"Введите сумму в {cl}:\n"
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
        kb = [
            [InlineKeyboardButton(
                "➕ Ещё долг", callback_data="add_debt"
            )],
            [InlineKeyboardButton(
                "◀️ К долгам", callback_data="fin_debts"
            )],
        ]
        await update.message.reply_text(
            f"✅ *Долг записан!*\n\n"
            f"🆔 {debt_id}\n"
            f"🚗 {debt.get('car_id')}\n"
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
            "✅ *Закрыть долг*\n\n"
            "Выберите долг который был оплачен:",
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
            f"✅ *Долг {debt_id} закрыт!*\n\n"
            f"_Не забудь записать платёж в ➕ Добавить платёж_",
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
            text = (
                "👥 *Зарплаты*\n\n"
                "Записей нет.\n\n"
                "Нажми ➕ *Добавить зарплату*."
            )
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
                    f"Оклад: {r.get('Оклад')} ₽ | "
                    f"Бонус: {r.get('Бонус')} ₽ | "
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
        "👥 *Добавить зарплату*\n\n"
        "Шаг 1 из 4\n\n"
        "Введите *имя сотрудника*:\n"
        "_Пример: Иванов Кирилл_",
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
        "Шаг 4 из 4\n\nВведите *месяц*:\n"
        "_Пример: 07.2026_",
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
            f"Оклад: {oklad:,.0f} ₽\n"
            f"Бонус: {bonus:,.0f} ₽\n"
            f"Итого: *{total:,.0f} ₽*\n"
            f"📌 Статус: ❌ Не выплачено",
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
            "✅ *Зарплата выплачена!*\n\nДата зафиксирована.",
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
        [InlineKeyboardButton(
            "📅 За день", callback_data="report_day"
        )],
        [InlineKeyboardButton(
            "📅 За неделю", callback_data="report_week"
        )],
        [InlineKeyboardButton(
            "📅 За месяц", callback_data="report_month"
        )],
        [InlineKeyboardButton(
            "🚗 По машине", callback_data="report_car"
        )],
        [InlineKeyboardButton(
            "📈 P&L", callback_data="report_pl"
        )],
        [InlineKeyboardButton(
            "⚖️ Все долги", callback_data="report_debts"
        )],
        [InlineKeyboardButton(
            "❓ Инструкция", callback_data="inst_reports"
        )],
        [InlineKeyboardButton(
            "◀️ Назад", callback_data="finance_menu"
        )],
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
    text += f"   Рубли: *{income_rub:,.0f} ₽*\n"
    text += f"   Юани: *{income_cny:,.0f} ¥*\n\n"
    text += "📤 *Расходы:*\n"
    text += f"   Рубли: *{expense_rub:,.0f} ₽*\n"
    text += f"   Юани: *{expense_cny:,.0f} ¥*\n\n"
    text += "─────────────────\n"
    text += f"💵 Итого ₽: *{income_rub - expense_rub:,.0f}*\n"
    text += f"💴 Итого ¥: *{income_cny - expense_cny:,.0f}*\n"
    text += f"\n📋 Операций: {len(records)}"
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
                filtered, f"Отчёт за {now.strftime('%m.%Y')}"
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

    car_buttons = []
    for car in cars[-10:]:
        label = f"{car['ID']} — {car.get('Марка', '—')}"
        car_buttons.append([InlineKeyboardButton(
            label, callback_data=f"repcar_{car['ID']}"
        )])
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

        kb = [[InlineKeyboardButton(
            "◀️ К отчётам", callback_data="fin_reports"
        )]]
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
        text += f"   Рубли: *{income_rub:,.0f} ₽*\n"
        text += f"   Юани: *{income_cny:,.0f} ¥*\n\n"
        text += "📤 *РАСХОДЫ:*\n"
        text += f"   Рубли: *{expense_rub:,.0f} ₽*\n"
        text += f"   Юани: *{expense_cny:,.0f} ¥*\n\n"
        text += f"👥 *Зарплаты: {sal_total:,.0f} ₽*\n\n"
        text += "─────────────────\n"
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

# ===== СМЕНА ПАРОЛЯ (С ПРОВЕРКОЙ СТАРОГО) =====
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
        "🔑 *Смена пароля*\n\n"
        "Шаг 1 из 2\n\n"
        "Введите *текущий пароль*:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return OLD_PASSWORD

async def handle_old_password(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Проверка старого пароля перед сменой"""
    old_pass = update.message.text.strip()
    await update.message.delete()
    saved = context.bot_data.get("finance_password")

    if old_pass != saved:
        kb = [
            [InlineKeyboardButton(
                "🔄 Попробовать ещё раз",
                callback_data="fin_chpass"
            )],
            [InlineKeyboardButton(
                "◀️ В финансы", callback_data="finance_menu"
            )],
        ]
        await update.message.chat.send_message(
            "❌ *Неверный текущий пароль.*\n\n"
            "Смена пароля отменена.",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    kb = [[InlineKeyboardButton(
        "❌ Отмена", callback_data="finance_menu"
    )]]
    await update.message.chat.send_message(
        "✅ Текущий пароль верный.\n\n"
        "Шаг 2 из 2\n\n"
        "Введите *новый пароль*:",
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
        "edit_car": edit_car_start,
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
        "delete_car": delete_car_start,
    }

    inst_keys = [
        "inst_main", "inst_cars", "inst_pay",
        "inst_debts", "inst_sal", "inst_reports"
    ]

    if data in routes:
        await routes[data](update, context)
    elif data in inst_keys:
        await show_instruction(update, context)
    elif data.startswith("closedebt_"):
        await close_debt_confirm(update, context)
    elif data.startswith("paysal_"):
        await pay_sal_confirm(update, context)
    elif data.startswith("delconfirm_"):
        await delete_car_execute(update, context)
    elif data.startswith("editcar_"):
        # Повторный вход в редактирование конкретной машины
        car_id = data.replace("editcar_", "")
        context.user_data["edit_car_id"] = car_id
        cars = get_all_cars()
        car_data = next(
            (c for c in cars if c.get("ID") == car_id), None
        )
        field_buttons = []
        for label, field, col in EDIT_FIELDS:
            field_buttons.append([InlineKeyboardButton(
                label, callback_data=f"editfield_{field}"
            )])
        field_buttons.append([InlineKeyboardButton(
            "◀️ К машинам", callback_data="fin_cars"
        )])
        current = ""
        if car_data:
            current = (
                f"*Текущие данные:*\n"
                f"🚗 {car_data.get('Марка', '—')} "
                f"{car_data.get('Год', '—')}\n"
                f"🎨 {car_data.get('Цвет', '—')}\n"
                f"👤 {car_data.get('Клиент', '—')}\n\n"
            )
        await update.callback_query.edit_message_text(
            f"✏️ *Редактирование {car_id}*\n\n"
            f"{current}Что хотите изменить?",
            reply_markup=InlineKeyboardMarkup(field_buttons),
            parse_mode="Markdown"
        )
    elif data.startswith("editclient_"):
        await edit_client_type(update, context)

# ===== ЗАПУСК =====
def main():
    Thread(target=run_flask, daemon=True).start()

    logger.info("Подключаемся к Google Sheets...")
    get_spreadsheet()

    app = Application.builder().token(BOT_TOKEN).build()

    # Диалог входа в финансы
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

    # Диалог смены пароля
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

    # Диалог добавления машины
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
                filters.TEXT & ~filters.COMMAND, add_car_year
            )],
            ADD_CAR_COLOR: [MessageHandler(
                filters.TEXT & ~filters.COMMAND, add_car_color
            )],
            ADD_CAR_CLIENT: [MessageHandler(
                filters.TEXT & ~filters.COMMAND, add_car_client
            )],
            ADD_CAR_CLIENT_TYPE: [
                CallbackQueryHandler(
                    add_car_client_type, pattern="^client_"
                )
            ],
            ADD_CAR_COMPANY: [MessageHandler(
                filters.TEXT & ~filters.COMMAND, add_car_company
            )],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Диалог редактирования машины
    edit_car_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(
            edit_car_start, pattern="^edit_car$"
        )],
        states={
            EDIT_CAR_SELECT: [CallbackQueryHandler(
                edit_car_selected, pattern="^editcar_"
            )],
            EDIT_CAR_FIELD: [CallbackQueryHandler(
                edit_car_field_selected, pattern="^editfield_"
            )],
            EDIT_CAR_VALUE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    edit_car_value
                ),
                CallbackQueryHandler(
                    edit_client_type, pattern="^editclient_"
                ),
            ],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Диалог удаления машины
    delete_car_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(
            delete_car_start, pattern="^delete_car$"
        )],
        states={
            DELETE_CAR_CONFIRM: [CallbackQueryHandler(
                delete_car_confirm, pattern="^delcar_"
            )],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Диалог платежа
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
                filters.TEXT & ~filters.COMMAND, pay_amount
            )],
            PAY_COMMENT: [MessageHandler(
                filters.TEXT & ~filters.COMMAND, pay_comment
            )],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Диалог долга
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
                filters.TEXT & ~filters.COMMAND, debt_amount
            )],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Диалог зарплат
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

    # Диалог отчёта по машине
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

    # Порядок важен — более специфичные handlers первыми
    app.add_handler(auth_conv)
    app.add_handler(chpass_conv)
    app.add_handler(car_conv)
    app.add_handler(edit_car_conv)
    app.add_handler(delete_car_conv)
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
