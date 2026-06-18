import os
import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from flask import Flask
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

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


# ===== КУРС ЮАНЯ ВТБ (несколько источников) =====
def get_vtb_yuan():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }
    
    # Источник 1: прямой API ВТБ
    try:
        url = "https://www.vtb.ru/api/currency-exchange/table-info?contextItemId=%7B5A68BC3E-814E-4B85-8E63-D91582A4B831%7D&conversionPlace=card&conversionType=CashlessCNY"
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            for group in data.get("GroupedRates", []):
                for rate in group.get("MonoCurrencyRates", []):
                    if rate.get("CurrencyAbbreviation") == "CNY":
                        buy = rate.get("BankBuyAt")
                        sell = rate.get("BankSellAt")
                        if buy and sell:
                            return {"buy": buy, "sell": sell, "source": "ВТБ (официальный API)"}
    except Exception as e:
        logger.error(f"VTB API error: {e}")
    
    # Источник 2: bankiros.ru
    try:
        url = "https://bankiros.ru/bank/vtb/currency/cny"
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            text = r.text
            import re
            matches = re.findall(r'(\d{1,3}[.,]\d{2,4})', text)
            if len(matches) >= 2:
                rates = [float(m.replace(",", ".")) for m in matches if 5 < float(m.replace(",", ".")) < 30]
                if len(rates) >= 2:
                    return {"buy": rates[0], "sell": rates[1], "source": "bankiros.ru (ВТБ)"}
    except Exception as e:
        logger.error(f"bankiros error: {e}")
    
    # Источник 3: myfin.by
    try:
        url = "https://myfin.by/bank/vtb/kurs-valut"
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            import re
            text = r.text
            cny_section = re.search(r'CNY.{0,500}', text)
            if cny_section:
                matches = re.findall(r'(\d{1,3}[.,]\d{2,4})', cny_section.group())
                rates = [float(m.replace(",", ".")) for m in matches if 5 < float(m.replace(",", ".")) < 30]
                if len(rates) >= 2:
                    return {"buy": rates[0], "sell": rates[1], "source": "myfin.by (ВТБ)"}
    except Exception as e:
        logger.error(f"myfin error: {e}")
    
    # Резервный источник: ЦБ РФ
    cb = get_cbr_rate("CNY")
    if cb:
        return {
            "buy": round(cb * 0.97, 4),
            "sell": round(cb * 1.03, 4),
            "source": "ЦБ РФ ±3% (источники ВТБ недоступны)"
        }
    return None


# ===== СТАВКИ ПОШЛИН =====
def get_duty_rate(volume_cc, is_old):
    """Возвращает ставку €/см³ для проходных (False) или непроходных (True)"""
    if not is_old:  # 3-5 лет (проходные)
        if volume_cc <= 1000: return 1.5
        elif volume_cc <= 1500: return 1.7
        elif volume_cc <= 1800: return 2.5
        elif volume_cc <= 2300: return 2.7
        elif volume_cc <= 3000: return 3.0
        else: return 3.6
    else:  # старше 5 лет (непроходные)
        if volume_cc <= 1000: return 3.0
        elif volume_cc <= 1500: return 3.2
        elif volume_cc <= 1800: return 3.5
        elif volume_cc <= 2300: return 4.8
        elif volume_cc <= 3000: return 5.0
        else: return 5.7


def format_money(amount):
    """Форматирует число с пробелами: 337376 -> 337 376"""
    return f"{int(round(amount)):,}".replace(",", " ")


def build_duty_table():
    """Строит таблицу пошлин по образцу пользователя"""
    euro_rate = get_cbr_rate("EUR")
    if not euro_rate:
        return None
    
    volumes = [660, 1000, 1200, 1300, 1400, 1500, 1600, 1800,
               2000, 2200, 2300, 2400, 2500, 2700, 2800, 3000]
    
    today = datetime.now().strftime("%d.%m.%Y")
    
    # Заголовок
    text = f"📊 *Расчёт таможенных пошлин на автомобили*\n\n"
    text += f"📅 Дата расчёта: *{today}*\n"
    text += f"💶 Курс евро ЦБ: *{euro_rate:.2f} ₽*\n\n"
    
    # Проходные годы
    text += "💡 *Автомобили проходных годов (3–5 лет)*\n"
    text += "```\n"
    text += "Объём  Ставка    Пошлина\n"
    text += "─────────────────────────\n"
    for v in volumes:
        rate = get_duty_rate(v, is_old=False)
        duty_rub = v * rate * euro_rate
        text += f"{v:<5}  {rate}€    {format_money(duty_rub):>10} ₽\n"
    text += "```\n\n"
    
    # Непроходные
    text += "💡 *Автомобили непроходных годов (старше 5 лет)*\n"
    text += "```\n"
    text += "Объём  Ставка    Пошлина\n"
    text += "─────────────────────────\n"
    for v in volumes:
        rate = get_duty_rate(v, is_old=True)
        duty_rub = v * rate * euro_rate
        text += f"{v:<5}  {rate}€    {format_money(duty_rub):>10} ₽\n"
    text += "```\n\n"
    
    # Примечания
    text += "📌 Льготный утилизационный сбор для авто мощностью "
    text += "до 160 л.с. составляет *5 200 ₽*\n"
    text += "_(для авто младше 3 лет — 3 400 ₽)_\n\n"
    text += "📌 Автомобили мощнее 160 л.с. переходят в категорию "
    text += "с коммерческими ставками\n\n"
    text += "📌 Таможенные сборы за таможенные операции зависят "
    text += "от стоимости автомобиля\n\n"
    
    # Контакты
    text += "📥 *Заказать авто:*\n"
    text += "👉 https://t.me/avtoiskatelgroup\n\n"
    text += "Если у вас есть вопросы по подбору авто, "
    text += "будем рады помочь 👇\n"
    text += "📱 *Свяжитесь с нами:*\n"
    text += "📞 +7 995 870 33 09 (Кирилл) — Руководитель отдела продаж\n"
    text += "📞 +7 908 999 60 09 (Сергей) — Руководитель\n\n"
    text += "🚛 Работаем по всей России\n"
    text += "Автоискатель — привозим технику и автомобили "
    text += "под заказ из Китая, Японии и Кореи.\n\n"
    text += "#РАСЧЁТ\\_ПОШЛИНЫ"
    
    return text


# ===== ХЭНДЛЕРЫ =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💴 Курс юаня ВТБ", callback_data="yuan")],
        [InlineKeyboardButton("📊 Расчёт пошлин", callback_data="duty")],
    ]
    text = (
        "🤖 *Автоискатель — бот расчётов*\n\n"
        "Выберите действие:\n\n"
        "💴 *Курс юаня ВТБ* — актуальный курс CNY "
        "для международных переводов\n\n"
        "📊 *Расчёт пошлин* — таблица таможенных "
        "пошлин по объёмам двигателя"
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
    await query.edit_message_text("⏳ Загружаю курс юаня ВТБ...")
    
    data = get_vtb_yuan()
    kb = [
        [InlineKeyboardButton("🔄 Обновить", callback_data="yuan")],
        [InlineKeyboardButton("◀️ В меню", callback_data="menu")],
    ]
    
    if data:
        text = (
            f"💴 *Курс юаня (CNY) для переводов*\n"
            f"_Источник: {data['source']}_\n\n"
            f"📈 Покупка: *{data['buy']} ₽*\n"
            f"📉 Продажа: *{data['sell']} ₽*\n\n"
            f"💡 При международном переводе через ВТБ "
            f"используется курс продажи банка"
        )
    else:
        text = "❌ Не удалось получить курс. Попробуйте позже."
    
    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown"
    )


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
            text, reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown", disable_web_page_preview=True
        )
    else:
        await query.edit_message_text(
            "❌ Не удалось получить курс ЦБ. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup(kb)
        )


async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    if data == "yuan":
        await show_yuan(update, context)
    elif data == "duty":
        await show_duty(update, context)
    elif data == "menu":
        await start(update, context)


# ===== ЗАПУСК =====
def main():
    Thread(target=run_flask, daemon=True).start()
    
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_router))
    
    logger.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
