import os
import logging
import asyncio
import requests
import xml.etree.ElementTree as ET
from flask import Flask
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters, ContextTypes
)

BOT_TOKEN = os.environ.get("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== FLASK ДЛЯ UPTIMEROBOT =====
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)


# ===== КУРСЫ =====
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


def get_vtb_yuan():
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        url = "https://www.vtb.ru/api/currency-exchange/table-info?contextItemId=%7B5A68BC3E-814E-4B85-8E63-D91582A4B831%7D&conversionPlace=card&conversionType=CashlessCNY"
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            for group in data.get("GroupedRates", []):
                for rate in group.get("MonoCurrencyRates", []):
                    if rate.get("CurrencyAbbreviation") == "CNY":
                        return {
                            "buy": rate.get("BankBuyAt"),
                            "sell": rate.get("BankSellAt"),
                            "source": "ВТБ (безналичный)"
                        }
    except Exception as e:
        logger.error(f"VTB error: {e}")
    
    # Резерв: ЦБ РФ
    cb = get_cbr_rate("CNY")
    if cb:
        return {
            "buy": round(cb * 0.97, 4),
            "sell": round(cb * 1.03, 4),
            "source": "ЦБ РФ ±3% (ВТБ недоступен)"
        }
    return None


# ===== ТАМОЖНЯ =====
def calc_customs(price_eur, engine_cc, engine_type, age, euro_rate):
    price_rub = price_eur * euro_rate
    
    if engine_type == "electric":
        duty_eur = price_eur * 0.15
    elif age == "new":
        if price_eur <= 8500:
            duty_eur = max(price_eur * 0.54, engine_cc * 2.5)
        elif price_eur <= 16700:
            duty_eur = max(price_eur * 0.48, engine_cc * 3.5)
        elif price_eur <= 42300:
            duty_eur = max(price_eur * 0.48, engine_cc * 5.5)
        elif price_eur <= 84500:
            duty_eur = max(price_eur * 0.48, engine_cc * 7.5)
        elif price_eur <= 169000:
            duty_eur = max(price_eur * 0.48, engine_cc * 15.0)
        else:
            duty_eur = max(price_eur * 0.48, engine_cc * 20.0)
    elif age == "3to5":
        if engine_cc <= 1000: duty_eur = engine_cc * 1.5
        elif engine_cc <= 1500: duty_eur = engine_cc * 1.7
        elif engine_cc <= 1800: duty_eur = engine_cc * 2.5
        elif engine_cc <= 2300: duty_eur = engine_cc * 2.7
        elif engine_cc <= 3000: duty_eur = engine_cc * 3.0
        else: duty_eur = engine_cc * 3.6
    else:  # over5
        if engine_cc <= 1000: duty_eur = engine_cc * 3.0
        elif engine_cc <= 1500: duty_eur = engine_cc * 3.2
        elif engine_cc <= 1800: duty_eur = engine_cc * 3.5
        elif engine_cc <= 2300: duty_eur = engine_cc * 4.8
        elif engine_cc <= 3000: duty_eur = engine_cc * 5.0
        else: duty_eur = engine_cc * 5.7
    
    duty_rub = duty_eur * euro_rate
    
    if age == "new":
        util = 3400 * 0.17
    else:
        util = 3400 * 0.26
    
    if price_rub <= 200000: proc = 775
    elif price_rub <= 450000: proc = 1550
    elif price_rub <= 1200000: proc = 3100
    elif price_rub <= 2700000: proc = 8530
    elif price_rub <= 4200000: proc = 12000
    elif price_rub <= 5500000: proc = 15500
    elif price_rub <= 7000000: proc = 20000
    else: proc = 30000
    
    total = duty_rub + util + proc
    return {
        "price_eur": price_eur,
        "price_rub": round(price_rub, 2),
        "duty_eur": round(duty_eur, 2),
        "duty_rub": round(duty_rub, 2),
        "util": round(util, 2),
        "proc": round(proc, 2),
        "total": round(total, 2),
        "euro": euro_rate
    }


# ===== СОСТОЯНИЯ =====
ASK_PRICE, ASK_TYPE, ASK_CC, ASK_AGE = range(4)


# ===== ХЭНДЛЕРЫ =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💴 Курс юаня ВТБ", callback_data="yuan")],
        [InlineKeyboardButton("📊 Курс евро ЦБ", callback_data="euro")],
        [InlineKeyboardButton("🚗 Расчёт растаможки", callback_data="customs")],
    ]
    text = (
        "🤖 *Бот курсов и растаможки*\n\n"
        "Выберите действие:"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown"
        )


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
            f"💴 *Курс юаня (CNY)*\n"
            f"_Источник: {data['source']}_\n\n"
            f"📈 Покупка: *{data['buy']} ₽*\n"
            f"📉 Продажа: *{data['sell']} ₽*\n\n"
            f"💡 При переводе через ВТБ используется курс продажи"
        )
    else:
        text = "❌ Не удалось получить курс. Попробуйте позже."
    
    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown"
    )


async def show_euro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Загружаю курс евро...")
    
    rate = get_cbr_rate("EUR")
    kb = [
        [InlineKeyboardButton("🔄 Обновить", callback_data="euro")],
        [InlineKeyboardButton("◀️ В меню", callback_data="menu")],
    ]
    
    if rate:
        text = (
            f"📊 *Курс евро ЦБ РФ*\n\n"
            f"💶 1 EUR = *{round(rate, 4)} ₽*"
        )
    else:
        text = "❌ Не удалось получить курс."
    
    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown"
    )


async def customs_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🚗 *Расчёт растаможки*\n\n"
        "Шаг 1/4: введите стоимость авто в *евро* (например: 15000)\n\n"
        "Для отмены: /cancel",
        parse_mode="Markdown"
    )
    return ASK_PRICE


async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.replace(",", ".").replace(" ", ""))
        if price <= 0: raise ValueError
    except:
        await update.message.reply_text("❌ Введите число, например: 15000")
        return ASK_PRICE
    
    context.user_data["price"] = price
    kb = [
        [InlineKeyboardButton("⛽ Бензин", callback_data="t_petrol"),
         InlineKeyboardButton("🛢 Дизель", callback_data="t_diesel")],
        [InlineKeyboardButton("⚡ Электро", callback_data="t_electric"),
         InlineKeyboardButton("🔄 Гибрид", callback_data="t_hybrid")],
    ]
    await update.message.reply_text(
        "Шаг 2/4: выберите тип двигателя",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return ASK_TYPE


async def get_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    engine_map = {"t_petrol": "petrol", "t_diesel": "diesel",
                  "t_electric": "electric", "t_hybrid": "hybrid"}
    engine_type = engine_map[query.data]
    context.user_data["type"] = engine_type
    
    if engine_type == "electric":
        context.user_data["cc"] = 0
        kb = [
            [InlineKeyboardButton("🆕 До 3 лет", callback_data="a_new")],
            [InlineKeyboardButton("📅 3-5 лет", callback_data="a_3to5")],
            [InlineKeyboardButton("📆 Старше 5 лет", callback_data="a_over5")],
        ]
        await query.edit_message_text(
            "Шаг 3/4: выберите возраст авто",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return ASK_AGE
    
    await query.edit_message_text(
        "Шаг 3/4: введите объём двигателя в см³ (например: 1600)"
    )
    return ASK_CC


async def get_cc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cc = int(update.message.text.strip())
        if cc <= 0 or cc > 20000: raise ValueError
    except:
        await update.message.reply_text("❌ Введите число от 1 до 20000")
        return ASK_CC
    
    context.user_data["cc"] = cc
    kb = [
        [InlineKeyboardButton("🆕 До 3 лет", callback_data="a_new")],
        [InlineKeyboardButton("📅 3-5 лет", callback_data="a_3to5")],
        [InlineKeyboardButton("📆 Старше 5 лет", callback_data="a_over5")],
    ]
    await update.message.reply_text(
        "Шаг 4/4: выберите возраст авто",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return ASK_AGE


async def get_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    age_map = {"a_new": "new", "a_3to5": "3to5", "a_over5": "over5"}
    age = age_map[query.data]
    
    await query.edit_message_text("⏳ Считаю...")
    
    euro = get_cbr_rate("EUR")
    if not euro:
        await query.edit_message_text("❌ Не удалось получить курс евро")
        return ConversationHandler.END
    
    r = calc_customs(
        context.user_data["price"],
        context.user_data["cc"],
        context.user_data["type"],
        age,
        euro
    )
    
    age_names = {"new": "До 3 лет", "3to5": "3-5 лет", "over5": "Старше 5 лет"}
    type_names = {"petrol": "Бензин", "diesel": "Дизель",
                  "electric": "Электро", "hybrid": "Гибрид"}
    
    cc_line = ""
    if context.user_data["type"] != "electric":
        cc_line = f"🔧 Объём: *{context.user_data['cc']} см³*\n"
    
    text = (
        f"🚗 *РЕЗУЛЬТАТ РАСЧЁТА*\n"
        f"{'─' * 25}\n"
        f"💰 Стоимость: *{r['price_eur']:,.0f} EUR*\n"
        f"   ({r['price_rub']:,.0f} ₽)\n"
        f"⚙️ Двигатель: *{type_names[context.user_data['type']]}*\n"
        f"{cc_line}"
        f"📅 Возраст: *{age_names[age]}*\n"
        f"{'─' * 25}\n"
        f"🏛 Пошлина: *{r['duty_rub']:,.0f} ₽*\n"
        f"♻️ Утильсбор: *{r['util']:,.0f} ₽*\n"
        f"📝 Сбор: *{r['proc']:,.0f} ₽*\n"
        f"{'─' * 25}\n"
        f"💵 *ИТОГО: {r['total']:,.0f} ₽*\n"
        f"{'─' * 25}\n"
        f"💶 Курс EUR: {euro:.4f} ₽\n\n"
        f"⚠️ _Расчёт приблизительный_"
    )
    
    kb = [
        [InlineKeyboardButton("🔄 Новый расчёт", callback_data="customs")],
        [InlineKeyboardButton("◀️ В меню", callback_data="menu")],
    ]
    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown"
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено. /start для меню")
    return ConversationHandler.END


async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    if data == "yuan": await show_yuan(update, context)
    elif data == "euro": await show_euro(update, context)
    elif data == "menu": await start(update, context)


# ===== ЗАПУСК =====
def main():
    # Flask в отдельном потоке
    Thread(target=run_flask, daemon=True).start()
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(customs_start, pattern="^customs$")],
        states={
            ASK_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_price)],
            ASK_TYPE: [CallbackQueryHandler(get_type, pattern="^t_")],
            ASK_CC: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_cc)],
            ASK_AGE: [CallbackQueryHandler(get_age, pattern="^a_")],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(button_router))
    
    logger.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
