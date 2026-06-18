import os
import logging
import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters, ContextTypes
)
from flask import Flask
from threading import Thread
import xml.etree.ElementTree as ET

# ===== НАСТРОЙКИ =====
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Flask для поддержания работы на Render
app_flask = Flask(__name__)

@app_flask.route('/')
def home():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)

# ===== ПОЛУЧЕНИЕ КУРСОВ ВАЛЮТ =====

def get_vtb_yuan_rate():
    """Получаем курс юаня ВТБ для переводов"""
    try:
        # Пробуем получить с сайта ВТБ
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36"
        }
        
        # Метод 1: API ВТБ
        url = "https://www.vtb.ru/api/currency-exchange/table-info?contextItemId=%7B5A68BC3E-814E-4B85-8E63-D91582A4B831%7D&conversionPlace=card&conversionType=CashlessCNY&renderingId=ctl00_SPFPlaceHolderMain_ctl02_ctl03"
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            groups = data.get("GroupedRates", [])
            for group in groups:
                rates = group.get("MonoCurrencyRates", [])
                for rate in rates:
                    if rate.get("CurrencyAbbreviation") == "CNY":
                        buy = rate.get("BankBuyAt")
                        sell = rate.get("BankSellAt")
                        if sell:
                            return {
                                "buy": buy,
                                "sell": sell,
                                "source": "ВТБ (безналичный)"
                            }
    except Exception as e:
        logger.error(f"Ошибка получения курса ВТБ (метод 1): {e}")
    
    try:
        # Метод 2: Другой API ВТБ
        url2 = "https://www.vtb.ru/api/currency-exchange/table-info?contextItemId=%7B5A68BC3E-814E-4B85-8E63-D91582A4B831%7D&conversionPlace=ibank&conversionType=CashlessCNY"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36"
        }
        response = requests.get(url2, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            groups = data.get("GroupedRates", [])
            for group in groups:
                rates = group.get("MonoCurrencyRates", [])
                for rate in rates:
                    if "CNY" in str(rate):
                        buy = rate.get("BankBuyAt") or rate.get("BankBuyAtDecimal")
                        sell = rate.get("BankSellAt") or rate.get("BankSellAtDecimal")
                        if sell:
                            return {
                                "buy": buy,
                                "sell": sell,
                                "source": "ВТБ (онлайн-банк)"
                            }
    except Exception as e:
        logger.error(f"Ошибка получения курса ВТБ (метод 2): {e}")
    
    try:
        # Метод 3: Курс ЦБ РФ как запасной
        cb_rate = get_cbr_rate("CNY")
        if cb_rate:
            return {
                "buy": round(cb_rate * 0.97, 4),
                "sell": round(cb_rate * 1.03, 4),
                "source": "ЦБ РФ (ВТБ недоступен, ±3% примерно)"
            }
    except Exception as e:
        logger.error(f"Ошибка получения курса ЦБ: {e}")
    
    return None


def get_cbr_rate(currency_code):
    """Получаем курс валюты от ЦБ РФ"""
    try:
        url = "https://www.cbr.ru/scripts/XML_daily.asp"
        response = requests.get(url, timeout=10)
        response.encoding = "windows-1251"
        root = ET.fromstring(response.text)
        
        for valute in root.findall("Valute"):
            char_code = valute.find("CharCode").text
            if char_code == currency_code:
                nominal = int(valute.find("Nominal").text)
                value = float(valute.find("Value").text.replace(",", "."))
                return value / nominal
    except Exception as e:
        logger.error(f"Ошибка ЦБ РФ: {e}")
    return None


def get_euro_rate():
    """Получаем курс евро от ЦБ РФ"""
    rate = get_cbr_rate("EUR")
    if rate:
        return rate
    return None


# ===== РАСЧЁТ ТАМОЖЕННЫХ ПЛАТЕЖЕЙ =====

def calculate_customs(
    price_eur, engine_cc, engine_type, car_age_category, euro_rate
):
    """
    Расчёт таможенных платежей для физических лиц
    
    car_age_category:
        "new" — до 3 лет
        "3to5" — от 3 до 5 лет
        "over5" — старше 5 лет
    
    engine_type: "petrol", "diesel", "electric", "hybrid"
    """
    
    price_rub = price_eur * euro_rate
    
    result = {
        "price_eur": price_eur,
        "price_rub": round(price_rub, 2),
        "euro_rate": euro_rate,
        "engine_cc": engine_cc,
        "engine_type": engine_type,
        "car_age": car_age_category,
    }
    
    # ===== ДЛЯ ЭЛЕКТРОМОБИЛЕЙ =====
    if engine_type == "electric":
        if car_age_category == "new":
            # Единая ставка 15%, но не менее 0 EUR за 1 кВт·ч
            customs_duty_eur = price_eur * 0.15
        elif car_age_category == "3to5":
            customs_duty_eur = price_eur * 0.15
        else:
            customs_duty_eur = price_eur * 0.15
        
        customs_duty_rub = customs_duty_eur * euro_rate
        
        # Утилизационный сбор для физлиц
        util_fee = 3400  # базовая ставка для физлиц (легковой)
        if car_age_category == "new":
            util_fee_total = util_fee * 0.17
        else:
            util_fee_total = util_fee * 0.26
        
        # Таможенный сбор
        customs_processing = get_customs_processing_fee(price_rub)
        
        result["customs_duty_eur"] = round(customs_duty_eur, 2)
        result["customs_duty_rub"] = round(customs_duty_rub, 2)
        result["util_fee"] = round(util_fee_total, 2)
        result["customs_processing"] = customs_processing
        result["total_rub"] = round(
            customs_duty_rub + util_fee_total + customs_processing, 2
        )
        return result
    
    # ===== ДЛЯ АВТО ДО 3 ЛЕТ =====
    if car_age_category == "new":
        # Таблица ставок для новых авто (до 3 лет) для физлиц
        # (процент от стоимости, EUR за 1 см³)
        if price_eur <= 8500:
            rate_pct = 0.54
            rate_per_cc = 2.5
        elif price_eur <= 16700:
            rate_pct = 0.48
            rate_per_cc = 3.5
        elif price_eur <= 42300:
            rate_pct = 0.48
            rate_per_cc = 5.5
        elif price_eur <= 84500:
            rate_pct = 0.48
            rate_per_cc = 7.5
        elif price_eur <= 169000:
            rate_pct = 0.48
            rate_per_cc = 15.0
        else:
            rate_pct = 0.48
            rate_per_cc = 20.0
        
        duty_by_pct = price_eur * rate_pct
        duty_by_cc = engine_cc * rate_per_cc
        customs_duty_eur = max(duty_by_pct, duty_by_cc)
    
    # ===== ДЛЯ АВТО ОТ 3 ДО 5 ЛЕТ =====
    elif car_age_category == "3to5":
        if engine_cc <= 1000:
            rate_per_cc = 1.5
        elif engine_cc <= 1500:
            rate_per_cc = 1.7
        elif engine_cc <= 1800:
            rate_per_cc = 2.5
        elif engine_cc <= 2300:
            rate_per_cc = 2.7
        elif engine_cc <= 3000:
            rate_per_cc = 3.0
        else:
            rate_per_cc = 3.6
        
        customs_duty_eur = engine_cc * rate_per_cc
    
    # ===== ДЛЯ АВТО СТАРШЕ 5 ЛЕТ =====
    else:  # over5
        if engine_cc <= 1000:
            rate_per_cc = 3.0
        elif engine_cc <= 1500:
            rate_per_cc = 3.2
        elif engine_cc <= 1800:
            rate_per_cc = 3.5
        elif engine_cc <= 2300:
            rate_per_cc = 4.8
        elif engine_cc <= 3000:
            rate_per_cc = 5.0
        else:
            rate_per_cc = 5.7
        
        customs_duty_eur = engine_cc * rate_per_cc
    
    customs_duty_rub = customs_duty_eur * euro_rate
    
    # Утилизационный сбор для физлиц
    base_util = 20000  # базовая ставка
    if car_age_category == "new":
        if engine_cc <= 3000:
            util_coeff = 0.17
        else:
            util_coeff = 0.26
    else:
        if engine_cc <= 3000:
            util_coeff = 0.26
        else:
            util_coeff = 0.26
    
    # Обновленные коэффициенты утильсбора для физлиц (2024)
    if car_age_category == "new":
        if engine_cc <= 1000:
            util_fee_total = 3400 * 0.17  # ~578
        elif engine_cc <= 2000:
            util_fee_total = 3400 * 0.17  # ~578
        elif engine_cc <= 3000:
            util_fee_total = 3400 * 0.17  # ~578
        else:
            util_fee_total = 3400 * 48.5  # для больших объемов
    else:
        if engine_cc <= 1000:
            util_fee_total = 3400 * 0.26  # ~884
        elif engine_cc <= 2000:
            util_fee_total = 3400 * 0.26
        elif engine_cc <= 3000:
            util_fee_total = 3400 * 0.26
        else:
            util_fee_total = 3400 * 0.26
    
    # Таможенный сбор
    customs_processing = get_customs_processing_fee(price_rub)
    
    result["customs_duty_eur"] = round(customs_duty_eur, 2)
    result["customs_duty_rub"] = round(customs_duty_rub, 2)
    result["util_fee"] = round(util_fee_total, 2)
    result["customs_processing"] = customs_processing
    result["total_rub"] = round(
        customs_duty_rub + util_fee_total + customs_processing, 2
    )
    
    return result


def get_customs_processing_fee(value_rub):
    """Таможенный сбор за оформление"""
    if value_rub <= 200000:
        return 775
    elif value_rub <= 450000:
        return 1550
    elif value_rub <= 1200000:
        return 3100
    elif value_rub <= 2700000:
        return 8530
    elif value_rub <= 4200000:
        return 12000
    elif value_rub <= 5500000:
        return 15500
    elif value_rub <= 7000000:
        return 20000
    elif value_rub <= 8000000:
        return 23000
    elif value_rub <= 9000000:
        return 25000
    elif value_rub <= 10000000:
        return 27000
    else:
        return 30000


# ===== СОСТОЯНИЯ ДИАЛОГА =====
(
    ASK_PRICE, ASK_ENGINE_CC, ASK_ENGINE_TYPE,
    ASK_AGE, CONFIRM_CALC
) = range(5)


# ===== ОБРАБОТЧИКИ КОМАНД =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💴 Курс юаня ВТБ", callback_data="yuan")],
        [InlineKeyboardButton("🚗 Расчёт таможенных платежей", callback_data="customs")],
        [InlineKeyboardButton("📊 Курс евро ЦБ РФ", callback_data="euro")],
        [InlineKeyboardButton("ℹ️ Помощь", callback_data="help")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        "🤖 *Привет! Я бот для расчёта курсов и таможенных платежей*\n\n"
        "Что я умею:\n"
        "💴 Показывать курс юаня ВТБ для международных переводов\n"
        "🚗 Считать таможенные платежи на авто\n"
        "📊 Показывать актуальный курс евро ЦБ РФ\n\n"
        "Выберите действие:"
    )
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            welcome_text, reply_markup=reply_markup, parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            welcome_text, reply_markup=reply_markup, parse_mode="Markdown"
        )


async def yuan_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("⏳ Загружаю курс юаня ВТБ...")
    
    rate_data = get_vtb_yuan_rate()
    
    keyboard = [
        [InlineKeyboardButton("🔄 Обновить", callback_data="yuan")],
        [InlineKeyboardButton("◀️ Назад в меню", callback_data="menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if rate_data:
        text = (
            f"💴 *Курс юаня (CNY) — {rate_data['source']}*\n\n"
            f"📈 Банк покупает: *{rate_data['buy']} ₽*\n"
            f"📉 Банк продаёт: *{rate_data['sell']} ₽*\n\n"
            f"💡 _При международном переводе через ВТБ\n"
            f"используется курс продажи банка_\n\n"
            f"🔄 Нажмите «Обновить» для актуального курса"
        )
    else:
        # Пробуем хотя бы курс ЦБ
        cbr_rate = get_cbr_rate("CNY")
        if cbr_rate:
            text = (
                f"💴 *Курс юаня (CNY)*\n\n"
                f"🏦 Курс ЦБ РФ: *{round(cbr_rate, 4)} ₽*\n\n"
                f"⚠️ _Курс ВТБ временно недоступен.\n"
                f"Показан курс ЦБ РФ. Курс ВТБ для переводов\n"
                f"обычно отличается на 1-5%_\n\n"
                f"🔄 Нажмите «Обновить» чтобы попробовать снова"
            )
        else:
            text = (
                "❌ *Не удалось получить курс юаня*\n\n"
                "Попробуйте позже или проверьте на сайте:\n"
                "🔗 vtb.ru/personal/platezhi-i-perevody/"
                "obmen-valjuty/\n\n"
                "🔄 Нажмите «Обновить» чтобы попробовать снова"
            )
    
    if query:
        await query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text, reply_markup=reply_markup, parse_mode="Markdown"
        )


async def euro_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("⏳ Загружаю курс евро ЦБ РФ...")
    
    rate = get_euro_rate()
    
    keyboard = [
        [InlineKeyboardButton("🔄 Обновить", callback_data="euro")],
        [InlineKeyboardButton("◀️ Назад в меню", callback_data="menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if rate:
        text = (
            f"📊 *Курс евро (EUR) — ЦБ РФ*\n\n"
            f"💶 1 EUR = *{round(rate, 4)} ₽*\n\n"
            f"_Этот курс используется для расчёта\n"
            f"таможенных платежей_"
        )
    else:
        text = "❌ Не удалось получить курс евро. Попробуйте позже."
    
    if query:
        await query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode="Markdown"
        )


# ===== ДИАЛОГ РАСЧЁТА ТАМОЖНИ =====

async def customs_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(
            "🚗 *Расчёт таможенных платежей*\n\n"
            "Шаг 1 из 4\n\n"
            "💰 Введите стоимость автомобиля *в евро (EUR)*:\n\n"
            "_Например: 15000_\n\n"
            "Для отмены введите /cancel",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "🚗 *Расчёт таможенных платежей*\n\n"
            "Шаг 1 из 4\n\n"
            "💰 Введите стоимость автомобиля *в евро (EUR)*:\n\n"
            "_Например: 15000_\n\n"
            "Для отмены введите /cancel",
            parse_mode="Markdown"
        )
    return ASK_PRICE


async def ask_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(" ", "").replace(",", ".")
    
    try:
        price = float(text)
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Пожалуйста, введите корректную стоимость в евро.\n"
            "Например: 15000"
        )
        return ASK_PRICE
    
    context.user_data["price_eur"] = price
    
    keyboard = [
        [
            InlineKeyboardButton("⚡ Электро", callback_data="eng_electric"),
        ],
        [
            InlineKeyboardButton("⛽ Бензин", callback_data="eng_petrol"),
            InlineKeyboardButton("🛢 Дизель", callback_data="eng_diesel"),
        ],
        [
            InlineKeyboardButton("🔄 Гибрид", callback_data="eng_hybrid"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🚗 *Расчёт таможенных платежей*\n\n"
        "Шаг 2 из 4\n\n"
        "⚙️ Выберите *тип двигателя*:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return ASK_ENGINE_TYPE


async def ask_engine_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    engine_map = {
        "eng_petrol": "petrol",
        "eng_diesel": "diesel",
        "eng_electric": "electric",
        "eng_hybrid": "hybrid",
    }
    
    engine_type = engine_map.get(query.data, "petrol")
    context.user_data["engine_type"] = engine_type
    
    engine_names = {
        "petrol": "⛽ Бензин",
        "diesel": "🛢 Дизель",
        "electric": "⚡ Электро",
        "hybrid": "🔄 Гибрид",
    }
    
    if engine_type == "electric":
        context.user_data["engine_cc"] = 0
        # Пропускаем вопрос об объёме, сразу спрашиваем возраст
        keyboard = [
            [InlineKeyboardButton("🆕 До 3 лет", callback_data="age_new")],
            [InlineKeyboardButton("📅 От 3 до 5 лет", callback_data="age_3to5")],
            [InlineKeyboardButton("📆 Старше 5 лет", callback_data="age_over5")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"🚗 *Расчёт таможенных платежей*\n\n"
            f"Тип двигателя: {engine_names[engine_type]}\n\n"
            f"Шаг 3 из 4\n\n"
            f"📅 Выберите *возраст автомобиля*:",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return ASK_AGE
    
    await query.edit_message_text(
        f"🚗 *Расчёт таможенных платежей*\n\n"
        f"Тип двигателя: {engine_names[engine_type]}\n\n"
        f"Шаг 3 из 4\n\n"
        f"🔧 Введите *объём двигателя в кубических сантиметрах (см³)*:\n\n"
        f"_Например: 1600_\n\n"
        f"Для отмены введите /cancel",
        parse_mode="Markdown"
    )
    return ASK_ENGINE_CC


async def ask_engine_cc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(" ", "")
    
    try:
        engine_cc = int(text)
        if engine_cc <= 0 or engine_cc > 20000:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Введите корректный объём двигателя (от 1 до 20000 см³).\n"
            "Например: 1600"
        )
        return ASK_ENGINE_CC
    
    context.user_data["engine_cc"] = engine_cc
    
    keyboard = [
        [InlineKeyboardButton("🆕 До 3 лет", callback_data="age_new")],
        [InlineKeyboardButton("📅 От 3 до 5 лет", callback_data="age_3to5")],
        [InlineKeyboardButton("📆 Старше 5 лет", callback_data="age_over5")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🚗 *Расчёт таможенных платежей*\n\n"
        "Шаг 4 из 4\n\n"
        "📅 Выберите *возраст автомобиля*:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return ASK_AGE


async def ask_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    age_map = {
        "age_new": "new",
        "age_3to5": "3to5",
        "age_over5": "over5",
    }
    
    car_age = age_map.get(query.data, "new")
    context.user_data["car_age"] = car_age
    
    await query.edit_message_text("⏳ Считаю таможенные платежи...")
    
    # Получаем курс евро
    euro_rate = get_euro_rate()
    
    if not euro_rate:
        keyboard = [
            [InlineKeyboardButton("🔄 Попробовать снова", callback_data="customs")],
            [InlineKeyboardButton("◀️ Назад в меню", callback_data="menu")],
        ]
        await query.edit_message_text(
            "❌ Не удалось получить курс евро ЦБ РФ.\n"
            "Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ConversationHandler.END
    
    # Расчёт
    result = calculate_customs(
        price_eur=context.user_data["price_eur"],
        engine_cc=context.user_data["engine_cc"],
        engine_type=context.user_data["engine_type"],
        car_age_category=car_age,
        euro_rate=euro_rate
    )
    
    age_names = {
        "new": "🆕 До 3 лет",
        "3to5": "📅 От 3 до 5 лет",
        "over5": "📆 Старше 5 лет",
    }
    engine_names = {
        "petrol": "⛽ Бензин",
        "diesel": "🛢 Дизель",
        "electric": "⚡ Электро",
        "hybrid": "🔄 Гибрид",
    }
    
    engine_info = ""
    if context.user_data["engine_type"] != "electric":
        engine_info = f"🔧 Объём двигателя: *{result['engine_cc']} см³*\n"
    
    text = (
        f"🚗 *РЕЗУЛЬТАТ РАСЧЁТА ТАМОЖЕННЫХ ПЛАТЕЖЕЙ*\n"
        f"{'━' * 35}\n\n"
        f"📋 *Параметры автомобиля:*\n"
        f"💰 Стоимость: *{result['price_eur']:,.2f} EUR*\n"
        f"   ({result['price_rub']:,.2f} ₽)\n"
        f"⚙️ Двигатель: *{engine_names[result['engine_type']]}*\n"
        f"{engine_info}"
        f"📅 Возраст: *{age_names[result['car_age']]}*\n\n"
        f"{'━' * 35}\n"
        f"📊 *Расчёт платежей:*\n\n"
        f"🏛 Таможенная пошлина:\n"
        f"   *{result['customs_duty_eur']:,.2f} EUR*\n"
        f"   (*{result['customs_duty_rub']:,.2f} ₽*)\n\n"
        f"♻️ Утилизационный сбор:\n"
        f"   *{result['util_fee']:,.2f} ₽*\n\n"
        f"📝 Таможенный сбор:\n"
        f"   *{result['customs_processing']:,.2f} ₽*\n\n"
        f"{'━' * 35}\n"
        f"💵 *ИТОГО: {result['total_rub']:,.2f} ₽*\n"
        f"{'━' * 35}\n\n"
        f"💶 Курс EUR ЦБ РФ: {euro_rate:.4f} ₽\n\n"
        f"⚠️ _Расчёт приблизительный для физических лиц.\n"
        f"Точную сумму уточняйте на таможне._"
    )
    
    keyboard = [
        [InlineKeyboardButton("🔄 Новый расчёт", callback_data="customs")],
        [InlineKeyboardButton("💴 Курс юаня ВТБ", callback_data="yuan")],
        [InlineKeyboardButton("◀️ Назад в меню", callback_data="menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text, reply_markup=reply_markup, parse_mode="Markdown"
    )
    
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❌ Расчёт отменён. Нажмите /start для возврата в меню."
    )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    
    text = (
        "ℹ️ *ПОМОЩЬ*\n\n"
        "*Команды:*\n"
        "/start — Главное меню\n"
        "/yuan — Курс юаня ВТБ\n"
        "/euro — Курс евро ЦБ РФ\n"
        "/customs — Расчёт таможенных платежей\n"
        "/help — Эта справка\n\n"
        "*Как считаются таможенные платежи:*\n"
        "• Таможенная пошлина — зависит от стоимости,\n"
        "  объёма двигателя и возраста авто\n"
        "• Утилизационный сбор — фиксированный\n"
        "• Таможенный сбор за оформление\n\n"
        "*Источники данных:*\n"
        "• Курс юаня — сайт ВТБ / ЦБ РФ\n"
        "• Курс евро — ЦБ РФ (cbr.ru)\n\n"
        "⚠️ _Расчёт приблизительный и предназначен\n"
        "для физических лиц._"
    )
    
    keyboard = [
        [InlineKeyboardButton("◀️ Назад в меню", callback_data="menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if query:
        await query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text, reply_markup=reply_markup, parse_mode="Markdown"
        )


# ===== ОБРАБОТЧИК КНОПОК =====

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    if data == "yuan":
        await yuan_rate(update, context)
    elif data == "euro":
        await euro_rate(update, context)
    elif data == "help":
        await help_command(update, context)
    elif data == "menu":
        await start(update, context)


# ===== ЗАПУСК =====

def main():
    # Запускаем Flask в отдельном потоке
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Создаём приложение бота
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Диалог расчёта таможни
    customs_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(customs_start, pattern="^customs$"),
            CommandHandler("customs", customs_start),
        ],
        states={
            ASK_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_price)
            ],
            ASK_ENGINE_TYPE: [
                CallbackQueryHandler(ask_engine_type, pattern="^eng_")
            ],
            ASK_ENGINE_CC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_engine_cc)
            ],
            ASK_AGE: [
                CallbackQueryHandler(ask_age, pattern="^age_")
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
        per_message=False,
    )
    
    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("yuan", yuan_rate))
    application.add_handler(CommandHandler("euro", euro_rate))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(customs_handler)
    application.add_handler(
        CallbackQueryHandler(button_handler)
    )
    
    # Запускаем бота
    logger.info("Бот запущен!")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
