import os
import logging
import requests
import xml.etree.ElementTree as ET
import gspread
import json
import re
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
    SET_PASSWORD, ENTER_PASSWORD,
    ADD_CAR_MARK, ADD_CAR_MODEL, ADD_CAR_YEAR,
    ADD_CAR_COLOR, ADD_CAR_COMPLECT, ADD_CAR_MILEAGE,
    ADD_CAR_CLIENT, ADD_CAR_CLIENT_TYPE,
    PAY_CAR, PAY_CATEGORY, PAY_AMOUNT, PAY_COMMENT,
    DEBT_CAR, DEBT_WHO, DEBT_WHOM, DEBT_AMOUNT,
    DEBT_CURRENCY, DEBT_COMMENT,
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

# ===== GOOGLE SHEETS =====
def get_sheet():
    try:
        creds_dict = json.loads(GOOGLE_CREDS)
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        return client.open_by_key(SHEET_ID)
    except Exception as e:
        logger.error(f"Google Sheets error: {e}")
        return None

def get_worksheet(name):
    sh = get_sheet()
    if sh:
        return sh.worksheet(name)
    return None

# ===== СЛЕДУЮЩИЙ ID =====
def get_next_id(sheet_name, prefix, col=0):
    try:
        ws = get_worksheet(sheet_name)
        if not ws:
            return f"{prefix}-001"
        values = ws.col_values(col + 1)[1:]
        values = [v for v in values if v.startswith(prefix)]
        if not values:
            return f"{prefix}-001"
        nums = []
        for v in values:
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

# ===== ПОЛУЧИТЬ ВСЕ МАШИНЫ =====
def get_all_cars():
    try:
        ws = get_worksheet("МАШИНЫ")
        if not ws:
            return []
        records = ws.get_all_records()
        return records
    except Exception as e:
        logger.error(f"get_all_cars error: {e}")
        return []

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
                value = float(v.find("Value").text.replace(",", "."))
                return value / nominal
    except Exception as e:
        logger.error(f"CBR error: {e}")
    return None

# ===== КУРС ЮАНЯ ВТБ (ИНТЕРНЕТ-БАНК) =====
def get_vtb_yuan():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }

    # Источник 1: API ВТБ — интернет-банк
    try:
        url = (
            "https://www.vtb.ru/api/currency-exchange/table-info"
            "?contextItemId=%7B5A68BC3E-814E-4B85-8E63-D91582A4B831%7D"
            "&conversionPlace=online"
            "&conversionType=CurrencyCNY"
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
                                "buy": buy,
                                "sell": sell,
                                "source": "ВТБ Интернет-банк (официальный)"
                            }
    except Exception as e:
        logger.error(f"VTB online API error: {e}")

    # Источник 2: API ВТБ — мобильный банк
    try:
        url = (
            "https://www.vtb.ru/api/currency-exchange/table-info"
            "?contextItemId=%7B5A68BC3E-814E-4B85-8E63-D91582A4B831%7D"
            "&conversionPlace=mobile"
            "&conversionType=CurrencyCNY"
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
                                "buy": buy,
                                "sell": sell,
                                "source": "ВТБ Мобильный банк (официальный)"
                            }
    except Exception as e:
        logger.error(f"VTB mobile API error: {e}")

    # Резервный: ЦБ РФ
    cb = get_cbr_rate("CNY")
    if cb:
        return {
            "buy": round(cb * 0.98, 4),
            "sell": round(cb * 1.02, 4),
            "source": "ЦБ РФ ±2% (ВТБ временно недоступен)"
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
    text = f"📊 *Расчёт таможенных пошлин на автомобили*\n\n"
    text += f"📅 Дата расчёта: *{today}*\n"
    text += f"💶 Курс евро ЦБ: *{euro_rate:.2f} ₽*\n\n"
    text += "💡 *Автомобили проходных годов (3–5 лет)*\n"
    text += "```\n"
    text += "Объём  Ставка    Пошлина\n"
    text += "─────────────────────────\n"
    for v in volumes:
        rate = get_duty_rate(v, is_old=False)
        duty_rub = v * rate * euro_rate
        text += f"{v:<5}  {rate}€    {format_money(duty_rub):>10} ₽\n"
    text += "```\n\n"
    text += "💡 *Автомобили непроходных годов (старше 5 лет)*\n"
    text += "```\n"
    text += "Объём  Ставка    Пошлина\n"
    text += "─────────────────────────\n"
    for v in volumes:
        rate = get_duty_rate(v, is_old=True)
        duty_rub = v * rate * euro_rate
        text += f"{v:<5}  {rate}€    {format_money(duty_rub):>10} ₽\n"
    text += "```\n\n"
    text += "📌 Льготный утилизационный сбор для авто мощностью "
    text += "до 160 л.с. составляет *5 200 ₽*\n"
    text += "_(для авто младше 3 лет — 3 400 ₽)_\n\n"
    text += "📌 Автомобили мощнее 160 л.с. переходят в категорию "
    text += "с коммерческими ставками\n\n"
    text += "📌 Таможенные сборы за таможенные операции зависят "
    text += "от стоимости автомобиля\n\n"
    text += "📥 *Заказать авто:*\n"
    text += "👉 https://t.me/avtoiskatelgroup\n\n"
    text += "📱 *Свяжитесь с нами:*\n"
    text += "📞 +7 995 870 33 09 (Кирилл)\n"
    text += "📞 +7 908 999 60 09 (Сергей)\n\n"
    text += "🚛 Работаем по всей России\n"
    text += "#РАСЧЁТ\\_ПОШЛИНЫ"
    return text

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
        "💴 *Курс юаня ВТБ* — курс CNY интернет-банк ВТБ\n\n"
        "📊 *Расчёт пошлин* — таблица таможенных пошлин\n\n"
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
    await query.edit_message_text("⏳ Загружаю курс юаня ВТБ (интернет-банк)...")
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
            f"💡 Для международного перевода используется "
            f"курс продажи банка"
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
    await query.edit_message_text("⏳ Считаю пошлины по актуальному курсу ЦБ...")
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
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    saved_password = context.bot_data.get("finance_password")

    # Если пароль ещё не задан
    if not saved_password:
        if user_id == BOSS_ID:
            kb = [[InlineKeyboardButton("◀️ В меню", callback_data="menu")]]
            await query.edit_message_text(
                "🔐 *Пароль для финансового раздела не задан*\n\n"
                "Введите новый пароль:",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown"
            )
            context.user_data["action"] = "set_password"
            return SET_PASSWORD
        else:
            kb = [[InlineKeyboardButton("◀️ В меню", callback_data="menu")]]
            await query.edit_message_text(
                "🔒 Финансовый раздел защищён паролем.\n"
                "Обратитесь к руководителю.",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return ConversationHandler.END

    # Если пароль уже задан — проверяем сессию
    if context.user_data.get("finance_auth"):
        await show_finance_menu(query, context)
        return ConversationHandler.END

    # Запрашиваем пароль
    kb = [[InlineKeyboardButton("◀️ В меню", callback_data="menu")]]
    await query.edit_message_text(
        "🔐 *Финансовый раздел*\n\nВведите пароль:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ENTER_PASSWORD

async def handle_set_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    await update.message.delete()
    context.bot_data["finance_password"] = password
    context.user_data["finance_auth"] = True
    kb = [[InlineKeyboardButton("💰 Открыть финансы", callback_data="finance_menu")]]
    await update.message.chat.send_message(
        "✅ *Пароль установлен!*\n\nТеперь финансовый раздел защищён.",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def handle_enter_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    await update.message.delete()
    saved = context.bot_data.get("finance_password")
    if password == saved:
        context.user_data["finance_auth"] = True
        kb = [[InlineKeyboardButton("💰 Открыть финансы", callback_data="finance_menu")]]
        await update.message.chat.send_message(
            "✅ *Пароль верный!*",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    else:
        kb = [[InlineKeyboardButton("◀️ В меню", callback_data="menu")]]
        await update.message.chat.send_message(
            "❌ *Неверный пароль.*\n\nПопробуйте ещё раз или вернитесь в меню.",
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
        [InlineKeyboardButton("🔑 Сменить пароль", callback_data="fin_chpass")],
        [InlineKeyboardButton("◀️ В меню", callback_data="menu")],
    ]
    text = (
        "💰 *Финансовый раздел*\n\n"
        "🚗 *Машины* — список авто и добавление новых\n"
        "➕ *Платёж* — записать доход или расход\n"
        "⚖️ *Долги* — кто кому должен\n"
        "👥 *Зарплаты* — учёт зарплат сотрудников\n"
        "📊 *Отчёты* — за день, неделю, месяц, P&L"
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

async def finance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not context.user_data.get("finance_auth"):
        await finance_enter(update, context)
        return
    await show_finance_menu(query, context)

# ===== МАШИНЫ =====
async def fin_cars(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cars = get_all_cars()
    kb = [
        [InlineKeyboardButton("➕ Добавить машину", callback_data="add_car")],
        [InlineKeyboardButton("◀️ Назад", callback_data="finance_menu")],
    ]
    if not cars:
        text = "🚗 *Машины*\n\nМашин пока нет. Добавьте первую!"
    else:
        text = f"🚗 *Машины* — всего: {len(cars)}\n\n"
        for car in cars[-10:]:
            text += (
                f"*{car.get('ID', '—')}* — "
                f"{car.get('Марка', '—')} {car.get('Модель', '—')} "
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

# ===== ДОБАВИТЬ МАШИНУ — ДИАЛОГ =====
async def add_car_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["new_car"] = {}
    kb = [[InlineKeyboardButton("❌ Отмена", callback_data="fin_cars")]]
    await query.edit_message_text(
        "🚗 *Добавление машины*\n\n"
        "Шаг 1 из 8\n\n"
        "Введите *марку* автомобиля:\n"
        "_Пример: Zeekr, Haval, Chery_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ADD_CAR_MARK

async def add_car_mark(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_car"]["Марка"] = update.message.text.strip()
    kb = [[InlineKeyboardButton("❌ Отмена", callback_data="fin_cars")]]
    await update.message.reply_text(
        "Шаг 2 из 8\n\nВведите *модель*:\n_Пример: 001, H6, Tiggo 8_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ADD_CAR_MODEL

async def add_car_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_car"]["Модель"] = update.message.text.strip()
    kb = [[InlineKeyboardButton("❌ Отмена", callback_data="fin_cars")]]
    await update.message.reply_text(
        "Шаг 3 из 8\n\nВведите *год выпуска*:\n_Пример: 2024_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ADD_CAR_YEAR

async def add_car_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_car"]["Год"] = update.message.text.strip()
    kb = [[InlineKeyboardButton("❌ Отмена", callback_data="fin_cars")]]
    await update.message.reply_text(
        "Шаг 4 из 8\n\nВведите *цвет*:\n_Пример: Белый, Чёрный_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ADD_CAR_COLOR

async def add_car_color(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_car"]["Цвет"] = update.message.text.strip()
    kb = [[InlineKeyboardButton("❌ Отмена", callback_data="fin_cars")]]
    await update.message.reply_text(
        "Шаг 5 из 8\n\nВведите *комплектацию*:\n_Пример: Максимум, Pro, Luxury_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ADD_CAR_COMPLECT

async def add_car_complect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_car"]["Комплектация"] = update.message.text.strip()
    kb = [[InlineKeyboardButton("❌ Отмена", callback_data="fin_cars")]]
    await update.message.reply_text(
        "Шаг 6 из 8\n\nВведите *пробег* (км):\n_Пример: 0 или 15000_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ADD_CAR_MILEAGE

async def add_car_mileage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_car"]["Пробег"] = update.message.text.strip()
    kb = [[InlineKeyboardButton("❌ Отмена", callback_data="fin_cars")]]
    await update.message.reply_text(
        "Шаг 7 из 8\n\nВведите *имя клиента*:\n\n"
        "Если физлицо — просто ФИО:\n_Пример: Иванов Иван Иванович_\n\n"
        "Если юрлицо — название компании:\n_Пример: ООО Автомир_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ADD_CAR_CLIENT

async def add_car_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_car"]["Клиент_raw"] = update.message.text.strip()
    kb = [
        [
            InlineKeyboardButton("👤 Физлицо", callback_data="client_fiz"),
            InlineKeyboardButton("🏢 Юрлицо", callback_data="client_yur")
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data="fin_cars")]
    ]
    await update.message.reply_text(
        "Шаг 8 из 8\n\nТип клиента:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ADD_CAR_CLIENT_TYPE

async def add_car_client_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    client_raw = context.user_data["new_car"].pop("Клиент_raw")
    if query.data == "client_yur":
        client_str = f"{client_raw} (юр. лицо)"
        client_type = "Юрлицо"
    else:
        client_str = client_raw
        client_type = "Физлицо"

    car = context.user_data["new_car"]
    car["Клиент"] = client_str
    car["Тип клиента"] = client_type

    # Сохраняем в таблицу
    try:
        ws = get_worksheet("МАШИНЫ")
        car_id = get_next_id("МАШИНЫ", "AUTO", 0)
        today = datetime.now().strftime("%d.%m.%Y")
        row = [
            car_id,
            car.get("Марка", ""),
            car.get("Модель", ""),
            car.get("Год", ""),
            car.get("Цвет", ""),
            car.get("Комплектация", ""),
            car.get("Пробег", ""),
            car.get("Клиент", ""),
            car.get("Тип клиента", ""),
            today
        ]
        ws.append_row(row)
        kb = [[InlineKeyboardButton("◀️ К машинам", callback_data="fin_cars")]]
        await query.edit_message_text(
            f"✅ *Машина добавлена!*\n\n"
            f"🆔 {car_id}\n"
            f"🚗 {car.get('Марка')} {car.get('Модель')} {car.get('Год')}\n"
            f"🎨 {car.get('Цвет')} | {car.get('Комплектация')}\n"
            f"📍 Пробег: {car.get('Пробег')} км\n"
            f"👤 {car.get('Клиент')}",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"add_car error: {e}")
        kb = [[InlineKeyboardButton("◀️ Назад", callback_data="fin_cars")]]
        await query.edit_message_text(
            "❌ Ошибка при сохранении. Попробуйте ещё раз.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    context.user_data.pop("new_car", None)
    return ConversationHandler.END

# ===== ДОБАВИТЬ ПЛАТЁЖ =====
CATEGORIES = [
    ("💰 Накрутка (доход)", "Накрутка", "CNY", "Входящий"),
    ("💵 Допы от клиента (доход)", "Допы от клиента", "RUB", "Входящий"),
    ("🚛 Автовоз", "Автовоз", "RUB", "Исходящий"),
    ("🏛 Таможенный брокер", "Таможенный брокер", "RUB", "Исходящий"),
    ("🔧 Допы в Китае", "Допы в Китае", "CNY", "Исходящий"),
    ("⛽ Бензин", "Бензин", "RUB", "Исходящий"),
    ("💸 Кэшбэк юрику", "Кэшбэк юрику", "CNY", "Исходящий"),
    ("👤 % Менеджеру (20 000₽)", "% Менеджеру", "RUB", "Исходящий"),
]

async def fin_pay_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cars = get_all_cars()
    if not cars:
        kb = [[InlineKeyboardButton("◀️ Назад", callback_data="finance_menu")]]
        await query.edit_message_text(
            "❌ Сначала добавьте машину!",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return ConversationHandler.END

    context.user_data["new_pay"] = {}
    # Кнопки машин (последние 10)
    car_buttons = []
    for car in cars[-10:]:
        label = f"{car['ID']} — {car['Марка']} {car['Модель']}"
        car_buttons.append(
            [InlineKeyboardButton(label, callback_data=f"paycar_{car['ID']}")]
        )
    car_buttons.append([InlineKeyboardButton("◀️ Отмена", callback_data="finance_menu")])
    await query.edit_message_text(
        "➕ *Добавить платёж*\n\nШаг 1 из 3\n\nВыберите машину:",
        reply_markup=InlineKeyboardMarkup(car_buttons),
        parse_mode="Markdown"
    )
    return PAY_CAR

async def pay_car_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    car_id = query.data.replace("paycar_", "")
    context.user_data["new_pay"]["car_id"] = car_id

    cat_buttons = []
    for i, (label, _, _, _) in enumerate(CATEGORIES):
        cat_buttons.append(
            [InlineKeyboardButton(label, callback_data=f"paycat_{i}")]
        )
    cat_buttons.append([InlineKeyboardButton("◀️ Отмена", callback_data="finance_menu")])
    await query.edit_message_text(
        f"Машина: *{car_id}*\n\nШаг 2 из 3\n\nВыберите категорию:",
        reply_markup=InlineKeyboardMarkup(cat_buttons),
        parse_mode="Markdown"
    )
    return PAY_CATEGORY

async def pay_category_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.replace("paycat_", ""))
    label, cat_name, currency, pay_type = CATEGORIES[idx]
    context.user_data["new_pay"]["category"] = cat_name
    context.user_data["new_pay"]["currency"] = currency
    context.user_data["new_pay"]["type"] = pay_type

    # Для % Менеджеру сумма фиксирована
    if cat_name == "% Менеджеру":
        context.user_data["new_pay"]["amount"] = "20000"
        kb = [[InlineKeyboardButton("❌ Отмена", callback_data="finance_menu")]]
        await query.edit_message_text(
            f"Категория: *{cat_name}*\n"
            f"Сумма: *20 000 ₽* (фиксированная)\n\n"
            f"Шаг 3 из 3\n\nДобавить комментарий? (или напишите «-» чтобы пропустить)",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    else:
        kb = [[InlineKeyboardButton("❌ Отмена", callback_data="finance_menu")]]
        await query.edit_message_text(
            f"Категория: *{cat_name}* | Валюта: *{currency}*\n\n"
            f"Шаг 3 из 3\n\nВведите *сумму*:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    return PAY_AMOUNT

async def pay_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if context.user_data["new_pay"].get("amount") != "20000":
        context.user_data["new_pay"]["amount"] = text
    kb = [[InlineKeyboardButton("❌ Отмена", callback_data="finance_menu")]]
    await update.message.reply_text(
        "Добавить комментарий?\n_(или напишите «-» чтобы пропустить)_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return PAY_COMMENT

async def pay_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comment = update.message.text.strip()
    if comment == "-":
        comment = ""
    pay = context.user_data["new_pay"]
    pay["comment"] = comment

    try:
        ws = get_worksheet("ПЛАТЕЖИ")
        pay_id = get_next_id("ПЛАТЕЖИ", "PAY", 0)
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
        currency_sign = "¥" if pay.get("currency") == "CNY" else "₽"
        kb = [
            [InlineKeyboardButton("➕ Ещё платёж", callback_data="fin_pay")],
            [InlineKeyboardButton("◀️ В финансы", callback_data="finance_menu")],
        ]
        await update.message.reply_text(
            f"✅ *Платёж записан!*\n\n"
            f"🆔 {pay_id}\n"
            f"🚗 Машина: {pay.get('car_id')}\n"
            f"📂 {pay.get('category')}\n"
            f"💵 {pay.get('amount')} {currency_sign}\n"
            f"{'📥' if pay.get('type') == 'Входящий' else '📤'} {pay.get('type')}",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"pay_comment error: {e}")
        await update.message.reply_text("❌ Ошибка при сохранении.")

    context.user_data.pop("new_pay", None)
    return ConversationHandler.END

# ===== ДОЛГИ =====
DEBT_TYPES = [
    ("👤 Клиент должен нам за допы", "Клиент", "Нам", "RUB"),
    ("🏢 Мы должны клиенту (переплата)", "Мы", "Клиенту", "RUB"),
    ("🇨🇳 Мы должны поставщику за допы", "Мы", "Поставщику", "CNY"),
    ("💸 Мы должны юрику кэшбэк", "Мы", "Юрику", "CNY"),
    ("👤 Мы должны менеджеру %", "Мы", "Менеджеру", "RUB"),
    ("🏛 Мы должны брокеру", "Мы", "Брокеру", "RUB"),
    ("🚛 Мы должны автовозу", "Мы", "Автовозу", "RUB"),
]

async def fin_debts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        ws = get_worksheet("ДОЛГИ")
        records = ws.get_all_records()
        unpaid = [r for r in records if r.get("Статус") == "Не оплачен"]
        kb = [
            [InlineKeyboardButton("➕ Добавить долг", callback_data="add_debt")],
            [InlineKeyboardButton("✅ Закрыть долг", callback_data="close_debt")],
            [InlineKeyboardButton("◀️ Назад", callback_data="finance_menu")],
        ]
        if not unpaid:
            text = "⚖️ *Долги*\n\n✅ Все долги погашены!"
        else:
            text = f"⚖️ *Долги* — открытых: {len(unpaid)}\n\n"
            for d in unpaid[-8:]:
                currency_sign = "¥" if d.get("Валюта") == "CNY" else "₽"
                text += (
                    f"*{d.get('ID долга')}* | {d.get('ID машины')}\n"
                    f"{d.get('Кто должен')} → {d.get('Кому должен')}: "
                    f"*{d.get('Сумма')} {currency_sign}*\n\n"
                )
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"fin_debts error: {e}")

async def add_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cars = get_all_cars()
    if not cars:
        kb = [[InlineKeyboardButton("◀️ Назад", callback_data="fin_debts")]]
        await query.edit_message_text(
            "❌ Сначала добавьте машину!",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return ConversationHandler.END

    context.user_data["new_debt"] = {}
    car_buttons = []
    for car in cars[-10:]:
        label = f"{car['ID']} — {car['Марка']} {car['Модель']}"
        car_buttons.append(
            [InlineKeyboardButton(label, callback_data=f"debtcar_{car['ID']}")]
        )
    car_buttons.append([InlineKeyboardButton("◀️ Отмена", callback_data="fin_debts")])
    await query.edit_message_text(
        "⚖️ *Добавить долг*\n\nВыберите машину:",
        reply_markup=InlineKeyboardMarkup(car_buttons),
        parse_mode="Markdown"
    )
    return DEBT_CAR

async def debt_car_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    car_id = query.data.replace("debtcar_", "")
    context.user_data["new_debt"]["car_id"] = car_id

    debt_buttons = []
    for i, (label, _, _, _) in enumerate(DEBT_TYPES):
        debt_buttons.append(
            [InlineKeyboardButton(label, callback_data=f"debttype_{i}")]
        )
    debt_buttons.append([InlineKeyboardButton("◀️ Отмена", callback_data="fin_debts")])
    await query.edit_message_text(
        f"Машина: *{car_id}*\n\nТип долга:",
        reply_markup=InlineKeyboardMarkup(debt_buttons),
        parse_mode="Markdown"
    )
    return DEBT_WHO

async def debt_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.replace("debttype_", ""))
    label, who, whom, currency = DEBT_TYPES[idx]
    context.user_data["new_debt"]["who"] = who
    context.user_data["new_debt"]["whom"] = whom
    context.user_data["new_debt"]["currency"] = currency

    kb = [[InlineKeyboardButton("❌ Отмена", callback_data="fin_debts")]]
    currency_sign = "¥ (юани)" if currency == "CNY" else "₽ (рубли)"
    await query.edit_message_text(
        f"*{label}*\n\nВалюта: *{currency_sign}*\n\nВведите сумму:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return DEBT_AMOUNT

async def debt_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_debt"]["amount"] = update.message.text.strip()
    try:
        debt = context.user_data["new_debt"]
        ws = get_worksheet("ДОЛГИ")
        debt_id = get_next_id("ДОЛГИ", "DEBT", 0)
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
        currency_sign = "¥" if debt.get("currency") == "CNY" else "₽"
        kb = [
            [InlineKeyboardButton("➕ Ещё долг", callback_data="add_debt")],
            [InlineKeyboardButton("◀️ К долгам", callback_data="fin_debts")],
        ]
        await update.message.reply_text(
            f"✅ *Долг записан!*\n\n"
            f"🆔 {debt_id}\n"
            f"🚗 {debt.get('car_id')}\n"
            f"{debt.get('who')} → {debt.get('whom')}: "
            f"*{debt.get('amount')} {currency_sign}*\n"
            f"📌 Статус: Не оплачен",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"debt_amount error: {e}")
        await update.message.reply_text("❌ Ошибка при сохранении.")

    context.user_data.pop("new_debt", None)
    return ConversationHandler.END

async def close_debt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        ws = get_worksheet("ДОЛГИ")
        records = ws.get_all_records()
        unpaid = [r for r in records if r.get("Статус") == "Не оплачен"]
        if not unpaid:
            kb = [[InlineKeyboardButton("◀️ Назад", callback_data="fin_debts")]]
            await query.edit_message_text(
                "✅ Все долги уже погашены!",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return ConversationHandler.END

        debt_buttons = []
        for d in unpaid[-10:]:
            currency_sign = "¥" if d.get("Валюта") == "CNY" else "₽"
            label = (
                f"{d.get('ID долга')} | {d.get('ID машины')} | "
                f"{d.get('Сумма')} {currency_sign}"
            )
            debt_buttons.append(
                [InlineKeyboardButton(label, callback_data=f"closedebt_{d.get('ID долга')}")]
            )
        debt_buttons.append([InlineKeyboardButton("◀️ Отмена", callback_data="fin_debts")])
        await query.edit_message_text(
            "✅ *Закрыть долг*\n\nВыберите долг который погашен:",
            reply_markup=InlineKeyboardMarkup(debt_buttons),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"close_debt error: {e}")

async def close_debt_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        kb = [[InlineKeyboardButton("◀️ К долгам", callback_data="fin_debts")]]
        await query.edit_message_text(
            f"✅ *Долг {debt_id} закрыт!*",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"close_debt_confirm error: {e}")

# ===== ЗАРПЛАТЫ =====
async def fin_sal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        ws = get_worksheet("ЗАРПЛАТЫ")
        records = ws.get_all_records()
        unpaid = [r for r in records if r.get("Статус") == "Не выплачено"]
        kb = [
            [InlineKeyboardButton("➕ Добавить зарплату", callback_data="add_sal")],
            [InlineKeyboardButton("✅ Отметить выплату", callback_data="pay_sal")],
            [InlineKeyboardButton("◀️ Назад", callback_data="finance_menu")],
        ]
        if not records:
            text = "👥 *Зарплаты*\n\nЗаписей пока нет."
        else:
            text = f"👥 *Зарплаты*\n\nНе выплачено: {len(unpaid)}\n\n"
            for r in records[-8:]:
                status_icon = "❌" if r.get("Статус") == "Не выплачено" else "✅"
                text += (
                    f"{status_icon} *{r.get('Сотрудник')}* — {r.get('Месяц')}\n"
                    f"Оклад: {r.get('Оклад')} ₽ | "
                    f"Бонус: {r.get('Бонус')} ₽ | "
                    f"Итого: *{r.get('Итого')} ₽*\n\n"
                )
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkrap(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"fin_sal error: {e}")

async def add_sal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["new_sal"] = {}
    kb = [[InlineKeyboardButton("❌ Отмена", callback_data="fin_sal")]]
    await query.edit_message_text(
        "👥 *Добавить зарплату*\n\nШаг 1 из 4\n\nВведите *имя сотрудника*:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return SAL_NAME

async def sal_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_sal"]["name"] = update.message.text.strip()
    kb = [[InlineKeyboardButton("❌ Отмена", callback_data="fin_sal")]]
    await update.message.reply_text(
        "Шаг 2 из 4\n\nВведите *оклад* (₽):",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return SAL_OKLAD

async def sal_oklad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_sal"]["oklad"] = update.message.text.strip()
    kb = [[InlineKeyboardButton("❌ Отмена", callback_data="fin_sal")]]
    await update.message.reply_text(
        "Шаг 3 из 4\n\nВведите *бонус* (₽):\n_(если нет бонуса — напишите 0)_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return SAL_BONUS

async def sal_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_sal"]["bonus"] = update.message.text.strip()
    kb = [[InlineKeyboardButton("❌ Отмена", callback_data="fin_sal")]]
    await update.message.reply_text(
        "Шаг 4 из 4\n\nВведите *месяц*:\n_Пример: 07.2026_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return SAL_MONTH

async def sal_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sal = context.user_data["new_sal"]
    sal["month"] = update.message.text.strip()
    try:
        oklad = float(sal.get("oklad", 0))
        bonus = float(sal.get("bonus", 0))
        total = oklad + bonus
        ws = get_worksheet("ЗАРПЛАТЫ")
        sal_id = get_next_id("ЗАРПЛАТЫ", "SAL", 0)
        row = [
            sal_id,
            sal.get("name", ""),
            oklad,
            bonus,
            total,
            sal.get("month", ""),
            "Не выплачено",
            ""
        ]
        ws.append_row(row)
        kb = [
            [InlineKeyboardButton("➕ Ещё", callback_data="add_sal")],
            [InlineKeyboardButton("◀️ К зарплатам", callback_data="fin_sal")],
        ]
        await update.message.reply_text(
            f"✅ *Зарплата добавлена!*\n\n"
            f"👤 {sal.get('name')}\n"
            f"📅 {sal.get('month')}\n"
            f"Оклад: {oklad:,.0f} ₽\n"
            f"Бонус: {bonus:,.0f} ₽\n"
            f"Итого: *{total:,.0f} ₽*\n"
            f"📌 Статус: Не выплачено",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"sal_month error: {e}")
        await update.message.reply_text("❌ Ошибка при сохранении.")
    context.user_data.pop("new_sal", None)
    return ConversationHandler.END

async def pay_sal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        ws = get_worksheet("ЗАРПЛАТЫ")
        records = ws.get_all_records()
        unpaid = [r for r in records if r.get("Статус") == "Не выплачено"]
        if not unpaid:
            kb = [[InlineKeyboardButton("◀️ Назад", callback_data="fin_sal")]]
            await query.edit_message_text(
                "✅ Все зарплаты выплачены!",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return

        sal_buttons = []
        for r in unpaid:
            label = f"{r.get('Сотрудник')} | {r.get('Месяц')} | {r.get('Итого')} ₽"
            sal_buttons.append(
                [InlineKeyboardButton(label, callback_data=f"paysal_{r.get('ID')}")]
            )
        sal_buttons.append([InlineKeyboardButton("◀️ Отмена", callback_data="fin_sal")])
        await query.edit_message_text(
            "✅ *Отметить выплату*\n\nВыберите сотрудника:",
            reply_markup=InlineKeyboardMarkup(sal_buttons),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"pay_sal error: {e}")

async def pay_sal_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        kb = [[InlineKeyboardButton("◀️ К зарплатам", callback_data="fin_sal")]]
        await query.edit_message_text(
            f"✅ *Зарплата выплачена!*",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"pay_sal_confirm error: {e}")

# ===== ОТЧЁТЫ =====
async def fin_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = [
        [InlineKeyboardButton("📅 За день", callback_data="report_day")],
        [InlineKeyboardButton("📅 За неделю", callback_data="report_week")],
        [InlineKeyboardButton("📅 За месяц", callback_data="report_month")],
        [InlineKeyboardButton("🚗 По машине", callback_data="report_car")],
        [InlineKeyboardButton("📈 P&L", callback_data="report_pl")],
        [InlineKeyboardButton("⚖️ Все долги", callback_data="report_debts")],
        [InlineKeyboardButton("◀️ Назад", callback_data="finance_menu")],
    ]
    await query.edit_message_text(
        "📊 *Отчёты*\n\nВыберите тип отчёта:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

def build_period_report(records, label):
    if not records:
        return f"📊 *{label}*\n\nДанных нет."
    income_rub = sum(
        float(r.get("Сумма", 0))
        for r in records
        if r.get("Тип") == "Входящий" and r.get("Валюта") == "RUB"
    )
    income_cny = sum(
        float(r.get("Сумма", 0))
        for r in records
        if r.get("Тип") == "Входящий" and r.get("Валюта") == "CNY"
    )
    expense_rub = sum(
        float(r.get("Сумма", 0))
        for r in records
        if r.get("Тип") == "Исходящий" and r.get("Валюта") == "RUB"
    )
    expense_cny = sum(
        float(r.get("Сумма", 0))
        for r in records
        if r.get("Тип") == "Исходящий" and r.get("Валюта") == "CNY"
    )
    text = f"📊 *{label}*\n\n"
    text += f"📥 *Доходы:*\n"
    text += f"   Рубли: *{income_rub:,.0f} ₽*\n"
    text += f"   Юани: *{income_cny:,.0f} ¥*\n\n"
    text += f"📤 *Расходы:*\n"
    text += f"   Рубли: *{expense_rub:,.0f} ₽*\n"
    text += f"   Юани: *{expense_cny:,.0f} ¥*\n\n"
    text += f"💵 *Итого (рубли): {income_rub - expense_rub:,.0f} ₽*\n"
    text += f"💴 *Итого (юани): {income_cny - expense_cny:,.0f} ¥*\n"
    text += f"\n📋 Операций: {len(records)}"
    return text

async def report_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        ws = get_worksheet("ПЛАТЕЖИ")
        records = ws.get_all_records()
        today = datetime.now().strftime("%d.%m.%Y")
        filtered = [r for r in records if r.get("Дата") == today]
        text = build_period_report(filtered, f"Отчёт за {today}")
        kb = [[InlineKeyboardButton("◀️ К отчётам", callback_data="fin_reports")]]
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"report_day error: {e}")

async def report_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        ws = get_worksheet("ПЛАТЕЖИ")
        records = ws.get_all_records()
        today = date.today()
        filtered = []
        for r in records:
            try:
                d = datetime.strptime(r.get("Дата", ""), "%d.%m.%Y").date()
                if (today - d).days <= 7:
                    filtered.append(r)
            except:
                pass
        text = build_period_report(filtered, "Отчёт за неделю")
        kb = [[InlineKeyboardButton("◀️ К отчётам", callback_data="fin_reports")]]
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"report_week error: {e}")

async def report_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        ws = get_worksheet("ПЛАТЕЖИ")
        records = ws.get_all_records()
        now = datetime.now()
        filtered = []
        for r in records:
            try:
                d = datetime.strptime(r.get("Дата", ""), "%d.%m.%Y")
                if d.month == now.month and d.year == now.year:
                    filtered.append(r)
            except:
                pass
        month_name = now.strftime("%m.%Y")
        text = build_period_report(filtered, f"Отчёт за {month_name}")
        kb = [[InlineKeyboardButton("◀️ К отчётам", callback_data="fin_reports")]]
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"report_month error: {e}")

async def report_car_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cars = get_all_cars()
    if not cars:
        kb = [[InlineKeyboardButton("◀️ Назад", callback_data="fin_reports")]]
        await query.edit_message_text(
            "❌ Машин нет.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return ConversationHandler.END

    car_buttons = []
    for car in cars[-10:]:
        label = f"{car['ID']} — {car['Марка']} {car['Модель']}"
        car_buttons.append(
            [InlineKeyboardButton(label, callback_data=f"repcar_{car['ID']}")]
        )
    car_buttons.append([InlineKeyboardButton("◀️ Отмена", callback_data="fin_reports")])
    await query.edit_message_text(
        "🚗 *Отчёт по машине*\n\nВыберите машину:",
        reply_markup=InlineKeyboardMarkup(car_buttons),
        parse_mode="Markdown"
    )
    return REPORT_CAR

async def report_car_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    car_id = query.data.replace("repcar_", "")
    try:
        pay_ws = get_worksheet("ПЛАТЕЖИ")
        debt_ws = get_worksheet("ДОЛГИ")
        pays = [r for r in pay_ws.get_all_records() if r.get("ID машины") == car_id]
        debts = [r for r in debt_ws.get_all_records() if r.get("ID машины") == car_id]

        text = f"🚗 *Отчёт по машине {car_id}*\n\n"

        # Платежи
        if pays:
            text += "💳 *Платежи:*\n"
            for p in pays:
                icon = "📥" if p.get("Тип") == "Входящий" else "📤"
                currency_sign = "¥" if p.get("Валюта") == "CNY" else "₽"
                text += (
                    f"{icon} {p.get('Категория')}: "
                    f"*{p.get('Сумма')} {currency_sign}*"
                    f" ({p.get('Дата')})\n"
                )
        else:
            text += "💳 Платежей нет\n"

        # Долги
        text += "\n⚖️ *Долги:*\n"
        if debts:
            for d in debts:
                status_icon = "❌" if d.get("Статус") == "Не оплачен" else "✅"
                currency_sign = "¥" if d.get("Валюта") == "CNY" else "₽"
                text += (
                    f"{status_icon} {d.get('Кто должен')} → "
                    f"{d.get('Кому должен')}: "
                    f"*{d.get('Сумма')} {currency_sign}*\n"
                )
        else:
            text += "Долгов нет\n"

        kb = [[InlineKeyboardButton("◀️ К отчётам", callback_data="fin_reports")]]
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"report_car_selected error: {e}")
    return ConversationHandler.END

async def report_pl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        pay_ws = get_worksheet("ПЛАТЕЖИ")
        sal_ws = get_worksheet("ЗАРПЛАТЫ")
        pays = pay_ws.get_all_records()
        sals = sal_ws.get_all_records()
        now = datetime.now()

        month_pays = []
        for r in pays:
            try:
                d = datetime.strptime(r.get("Дата", ""), "%d.%m.%Y")
                if d.month == now.month and d.year == now.year:
                    month_pays.append(r)
            except:
                pass

        income_rub = sum(
            float(r.get("Сумма", 0))
            for r in month_pays
            if r.get("Тип") == "Входящий" and r.get("Валюта") == "RUB"
        )
        income_cny = sum(
            float(r.get("Сумма", 0))
            for r in month_pays
            if r.get("Тип") == "Входящий" and r.get("Валюта") == "CNY"
        )
        expense_rub = sum(
            float(r.get("Сумма", 0))
            for r in month_pays
            if r.get("Тип") == "Исходящий" and r.get("Валюта") == "RUB"
        )
        expense_cny = sum(
            float(r.get("Сумма", 0))
            for r in month_pays
            if r.get("Тип") == "Исходящий" and r.get("Валюта") == "CNY"
        )

        # Зарплаты за месяц
        month_str = now.strftime("%m.%Y")
        sal_total = sum(
            float(r.get("Итого", 0))
            for r in sals
            if r.get("Месяц") == month_str
        )

        month_name = now.strftime("%m.%Y")
        text = f"📈 *P&L Отчёт за {month_name}*\n\n"
        text += f"📥 *ДОХОДЫ:*\n"
        text += f"   Рубли: *{income_rub:,.0f} ₽*\n"
        text += f"   Юани: *{income_cny:,.0f} ¥*\n\n"
        text += f"📤 *РАСХОДЫ:*\n"
        text += f"   Рубли: *{expense_rub:,.0f} ₽*\n"
        text += f"   Юани: *{expense_cny:,.0f} ¥*\n\n"
        text += f"👥 *Зарплаты: {sal_total:,.0f} ₽*\n\n"
        text += f"💵 *ИТОГО (рубли):*\n"
        text += f"   *{income_rub - expense_rub - sal_total:,.0f} ₽*\n"
        text += f"💴 *ИТОГО (юани):*\n"
        text += f"   *{income_cny - expense_cny:,.0f} ¥*"

        kb = [[InlineKeyboardButton("◀️ К отчётам", callback_data="fin_reports")]]
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"report_pl error: {e}")

async def report_debts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        ws = get_worksheet("ДОЛГИ")
        records = ws.get_all_records()
        unpaid = [r for r in records if r.get("Статус") == "Не оплачен"]
        paid = [r for r in records if r.get("Статус") == "Оплачен"]

        text = "⚖️ *Все долги*\n\n"
        if unpaid:
            text += f"❌ *Не оплачено: {len(unpaid)}*\n"
            for d in unpaid:
                currency_sign = "¥" if d.get("Валюта") == "CNY" else "₽"
                text += (
                    f"  {d.get('ID долга')} | {d.get('ID машины')}\n"
                    f"  {d.get('Кто должен')} → {d.get('Кому должен')}: "
                    f"*{d.get('Сумма')} {currency_sign}*\n"
                )
        else:
            text += "✅ Все долги погашены!\n"

        text += f"\n✅ *Оплачено за всё время: {len(paid)}*"

        kb = [[InlineKeyboardButton("◀️ К отчётам", callback_data="fin_reports")]]
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"report_debts error: {e}")

# ===== СМЕНА ПАРОЛЯ =====
async def fin_change_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != BOSS_ID:
        kb = [[InlineKeyboardButton("◀️ Назад", callback_data="finance_menu")]]
        await query.edit_message_text(
            "❌ Только руководитель может менять пароль.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return ConversationHandler.END
    kb = [[InlineKeyboardButton("❌ Отмена", callback_data="finance_menu")]]
    await query.edit_message_text(
        "🔑 *Смена пароля*\n\nВведите новый пароль:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    context.user_data["action"] = "set_password"
    return SET_PASSWORD

# ===== РОУТЕР КНОПОК =====
async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    if data == "yuan":
        await show_yuan(update, context)
    elif data == "duty":
        await show_duty(update, context)
    elif data == "menu":
        await start(update, context)
    elif data == "finance_menu":
        await finance_menu(update, context)
    elif data == "fin_cars":
        await fin_cars(update, context)
    elif data == "add_car":
        await add_car_start(update, context)
    elif data == "fin_pay":
        await fin_pay_start(update, context)
    elif data == "fin_debts":
        await fin_debts(update, context)
    elif data == "add_debt":
        await add_debt_start(update, context)
    elif data == "close_debt":
        await close_debt(update, context)
    elif data.startswith("closedebt_"):
        await close_debt_confirm(update, context)
    elif data == "fin_sal":
        await fin_sal(update, context)
    elif data == "add_sal":
        await add_sal_start(update, context)
    elif data == "pay_sal":
        await pay_sal(update, context)
    elif data.startswith("paysal_"):
        await pay_sal_confirm(update, context)
    elif data == "fin_reports":
        await fin_reports(update, context)
    elif data == "report_day":
        await report_day(update, context)
    elif data == "report_week":
        await report_week(update, context)
    elif data == "report_month":
        await report_month(update, context)
    elif data == "report_pl":
        await report_pl(update, context)
    elif data == "report_debts":
        await report_debts(update, context)
    elif data == "fin_chpass":
        await fin_change_password(update, context)

# ===== ЗАПУСК =====
def main():
    Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    # Диалог входа в финансы
    auth_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(finance_enter, pattern="^finance_enter$")
        ],
        states={
            SET_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_set_password)],
            ENTER_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_enter_password)],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Диалог добавления машины
    car_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_car_start, pattern="^add_car$")
        ],
        states={
            ADD_CAR_MARK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_car_mark)],
            ADD_CAR_MODEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_car_model)],
            ADD_CAR_YEAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_car_year)],
            ADD_CAR_COLOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_car_color)],
            ADD_CAR_COMPLECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_car_complect)],
            ADD_CAR_MILEAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_car_mileage)],
            ADD_CAR_CLIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_car_client)],
            ADD_CAR_CLIENT_TYPE: [CallbackQueryHandler(add_car_client_type, pattern="^client_")],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Диалог добавления платежа
    pay_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(fin_pay_start, pattern="^fin_pay$")
        ],
        states={
            PAY_CAR: [CallbackQueryHandler(pay_car_selected, pattern="^paycar_")],
            PAY_CATEGORY: [CallbackQueryHandler(pay_category_selected, pattern="^paycat_")],
            PAY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_amount)],
            PAY_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_comment)],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Диалог добавления долга
    debt_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_debt_start, pattern="^add_debt$")
        ],
        states={
            DEBT_CAR: [CallbackQueryHandler(debt_car_selected, pattern="^debtcar_")],
            DEBT_WHO: [CallbackQueryHandler(debt_type_selected, pattern="^debttype_")],
            DEBT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, debt_amount)],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Диалог зарплат
    sal_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_sal_start, pattern="^add_sal$")
        ],
        states={
            SAL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sal_name)],
            SAL_OKLAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, sal_oklad)],
            SAL_BONUS: [MessageHandler(filters.TEXT & ~filters.COMMAND, sal_bonus)],
            SAL_MONTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, sal_month)],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Диалог смены пароля
    chpass_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(fin_change_password, pattern="^fin_chpass$")
        ],
        states={
            SET_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_set_password)],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    # Диалог отчёта по машине
    repcar_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(report_car_start, pattern="^report_car$")
        ],
        states={
            REPORT_CAR: [CallbackQueryHandler(report_car_selected, pattern="^repcar_")],
        },
        fallbacks=[CallbackQueryHandler(button_router)],
        per_message=False
    )

    app.add_handler(auth_conv)
    app.add_handler(car_conv)
    app.add_handler(pay_conv)
    app.add_handler(debt_conv)
    app.add_handler(sal_conv)
    app.add_handler(chpass_conv)
    app.add_handler(repcar_conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_router))

    logger.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
