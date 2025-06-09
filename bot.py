#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import logging
import asyncio
import json
import pickle
from datetime import datetime, timedelta
from typing import Dict, Optional
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ParseMode
import openai
import requests
from dotenv import load_dotenv
import PyPDF2
from docx import Document
import io

# Загружаем переменные окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO'))
)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
PERPLEXITY_API_KEY = os.getenv('PERPLEXITY_API_KEY')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')

# Инициализация OpenRouter (используем OpenAI SDK с изменённым базовым URL)
from openai import AsyncOpenAI
openrouter_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY
)

# Хранилище для лимитов пользователей
user_limits = {}
DAILY_QUESTION_LIMIT = 10
DAILY_DOCUMENT_LIMIT = 10

class UserLimits:
    def __init__(self):
        self.questions_count = 0
        self.documents_count = 0
        self.last_reset = datetime.now().date()
    
    def reset_if_needed(self):
        today = datetime.now().date()
        if today > self.last_reset:
            self.questions_count = 0
            self.documents_count = 0
            self.last_reset = today
    
    def can_ask_question(self) -> bool:
        self.reset_if_needed()
        return self.questions_count < DAILY_QUESTION_LIMIT
    
    def can_process_document(self) -> bool:
        self.reset_if_needed()
        return self.documents_count < DAILY_DOCUMENT_LIMIT
    
    def increment_questions(self):
        self.reset_if_needed()
        self.questions_count += 1
    
    def increment_documents(self):
        self.reset_if_needed()
        self.documents_count += 1

def save_user_limits():
    """Сохраняет лимиты пользователей в файл"""
    try:
        with open("user_limits.pkl", "wb") as f:
            pickle.dump(user_limits, f)
    except Exception as e:
        logger.error(f"Error saving user limits: {e}")

def load_user_limits():
    """Загружает лимиты пользователей из файла"""
    global user_limits
    try:
        if os.path.exists("user_limits.pkl"):
            with open("user_limits.pkl", "rb") as f:
                user_limits = pickle.load(f)
            logger.info(f"Loaded user limits for {len(user_limits)} users")
        else:
            user_limits = {}
            logger.info("No existing user limits file found, starting fresh")
    except Exception as e:
        logger.error(f"Error loading user limits: {e}")
        user_limits = {}

def get_user_limits(user_id: int) -> UserLimits:
    if user_id not in user_limits:
        user_limits[user_id] = UserLimits()
    return user_limits[user_id]

def extract_text_from_file(file_content: bytes, file_extension: str) -> str:
    """Извлекает текст из файла в зависимости от его расширения"""
    try:
        if file_extension == '.txt':
            return file_content.decode('utf-8', errors='ignore')
        
        elif file_extension == '.pdf':
            pdf_file = io.BytesIO(file_content)
            pdf_reader = PyPDF2.PdfReader(pdf_file)
            text = ""
            for page in pdf_reader.pages:
                text += page.extract_text() + "\n"
            return text
        
        elif file_extension == '.docx':
            docx_file = io.BytesIO(file_content)
            doc = Document(docx_file)
            text = ""
            for paragraph in doc.paragraphs:
                text += paragraph.text + "\n"
            return text
        
        else:
            return "Неподдерживаемый формат файла."
    
    except Exception as e:
        logger.error(f"Error extracting text from {file_extension}: {e}")
        return f"Ошибка при извлечении текста из файла формата {file_extension}."

def log_user_action(user_id: int, username: str, action: str, query: str = ""):
    """Логирование действий пользователей"""
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "user_id": user_id,
        "username": username,
        "action": action,
        "query": query[:100] + "..." if len(query) > 100 else query
    }
    
    # Записываем в файл логов
    with open("user_logs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    
    logger.info(f"User {user_id} ({username}): {action}")

def markdown_to_html(text: str) -> str:
    """Конвертирует markdown в HTML для Telegram"""
    # Удаляем или заменяем сложную разметку
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)  # **bold** -> <b>bold</b>
    text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)      # *italic* -> <i>italic</i>
    text = re.sub(r'`(.*?)`', r'<code>\1</code>', text)  # `code` -> <code>code</code>
    text = re.sub(r'###\s*(.*)', r'<b>\1</b>', text)     # ### Header -> <b>Header</b>
    text = re.sub(r'##\s*(.*)', r'<b>\1</b>', text)      # ## Header -> <b>Header</b>
    text = re.sub(r'#\s*(.*)', r'<b>\1</b>', text)       # # Header -> <b>Header</b>
    
    # Убираем только номера источников в квадратных скобках от [1] до [20]
    text = re.sub(r'\[([1-9]|1[0-9]|20)\]', '', text)
    
    # Убираем оставшуюся markdown разметку  
    text = re.sub(r'[\[\]()]', '', text)
    
    # Нормализуем переносы строк (сохраняем структуру)
    text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)  # Убираем лишние пустые строки
    text = re.sub(r'[ \t]+', ' ', text)  # Убираем только лишние пробелы и табы, НЕ переносы строк
    
    return text.strip()

def get_main_keyboard():
    """Возвращает основную клавиатуру с кнопками"""
    keyboard = [
        [InlineKeyboardButton("❓ Задать юридический вопрос", callback_data="ask_question")],
        [InlineKeyboardButton("📄 Анализировать документ", callback_data="analyze_document")],
        [InlineKeyboardButton("✏️ Редактировать документ", callback_data="edit_document")],
        [InlineKeyboardButton("ℹ️ Информация", callback_data="info")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    log_user_action(user.id, user.username or "Unknown", "start")
    
    welcome_message = (
        "🏛️ <b>Здравствуйте! Рад наконец-то познакомиться с Вами.</b>\n\n"
        "Я — ваш персональный ИИ-специалист по юридическим вопросам Российской Федерации. "
        "Готов помочь вам разобраться в правовых нюансах, проанализировать документы и "
        "предоставить профессиональные консультации.\n\n"
        "<b>Мои возможности:</b>\n"
        "• Ответы на юридические вопросы по российскому праву\n"
        "• Анализ юридических документов\n"
        "• Редактирование и улучшение документов\n\n"
        "⚠️ <b>Важно:</b> Мои ответы носят информационный характер. "
        "Для принятия серьезных юридических решений рекомендую "
        "проверять информацию у квалифицированных юристов.\n\n"
        "🌟 <b>Полная профессиональная версия доступна на:</b>\n"
        "<a href='https://pocket-consultant.ru'>pocket-consultant.ru</a>\n\n"
        "Выберите нужное действие:"
    )
    
    await update.message.reply_text(
        welcome_message,
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопки"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    user_limits_obj = get_user_limits(user.id)
    
    if query.data == "ask_question":
        log_user_action(user.id, user.username or "Unknown", "button_ask_question")
        
        if not user_limits_obj.can_ask_question():
            await query.edit_message_text(
                "⛔ Вы достигли дневного лимита вопросов (10 в день).\n\n"
                "💡 <b>Что можно сделать:</b>\n"
                "• Обратиться завтра — лимит обнулится\n"
                "• Воспользоваться полной версией на "
                "<a href='https://pocket-consultant.ru'>pocket-consultant.ru</a>",
                parse_mode=ParseMode.HTML,
                reply_markup=get_main_keyboard()
            )
            return
        
        await query.edit_message_text(
            "❓ <b>Задайте ваш юридический вопрос</b>\n\n"
            f"Осталось вопросов сегодня: <b>{DAILY_QUESTION_LIMIT - user_limits_obj.questions_count}</b>\n\n"
            "Опишите вашу ситуацию подробно, указав все важные детали. "
            "Я отвечаю только на вопросы, связанные с российским правом.\n\n"
            "Просто напишите ваш вопрос в следующем сообщении:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]])
        )
        context.user_data['waiting_for'] = 'question'
    
    elif query.data == "analyze_document":
        log_user_action(user.id, user.username or "Unknown", "button_analyze_document")
        
        if not user_limits_obj.can_process_document():
            await query.edit_message_text(
                "⛔ Вы достигли дневного лимита обработки документов (10 в день).\n\n"
                "💡 <b>Что можно сделать:</b>\n"
                "• Обратиться завтра — лимит обнулится\n"
                "• Воспользоваться полной версией на "
                "<a href='https://pocket-consultant.ru'>pocket-consultant.ru</a>",
                parse_mode=ParseMode.HTML,
                reply_markup=get_main_keyboard()
            )
            return
        
        await query.edit_message_text(
            "📄 <b>Анализ документа</b>\n\n"
            f"Осталось обработок документов сегодня: <b>{DAILY_DOCUMENT_LIMIT - user_limits_obj.documents_count}</b>\n\n"
            "Отправьте документ для анализа. Поддерживаемые форматы:\n"
            "• Текстовые файлы (.txt)\n"
            "• Документы Word (.docx)\n"
            "• PDF файлы (.pdf)\n\n"
            "⚠️ <b>Ограничения:</b>\n"
            "• Максимальный размер: 20 МБ\n"
            "• Только текстовое содержимое\n\n"
            "🌟 Полная версия на <a href='https://pocket-consultant.ru'>pocket-consultant.ru</a>\n\n"
            "Просто отправьте файл в следующем сообщении:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]])
        )
        context.user_data['waiting_for'] = 'analyze_document'
    
    elif query.data == "edit_document":
        log_user_action(user.id, user.username or "Unknown", "button_edit_document")
        
        if not user_limits_obj.can_process_document():
            await query.edit_message_text(
                "⛔ Вы достигли дневного лимита обработки документов (10 в день).\n\n"
                "💡 <b>Что можно сделать:</b>\n"
                "• Обратиться завтра — лимит обнулится\n"
                "• Воспользоваться полной версией на "
                "<a href='https://pocket-consultant.ru'>pocket-consultant.ru</a>",
                parse_mode=ParseMode.HTML,
                reply_markup=get_main_keyboard()
            )
            return
        
        await query.edit_message_text(
            "✏️ <b>Редактирование документа</b>\n\n"
            f"Осталось обработок документов сегодня: <b>{DAILY_DOCUMENT_LIMIT - user_limits_obj.documents_count}</b>\n\n"
            "Отправьте документ для редактирования. Бот улучшит текст "
            "с точки зрения юридической корректности и ясности изложения.\n\n"
            "⚠️ <b>Поддерживаемые форматы для редактирования:</b>\n"
            "• Текстовые файлы (.txt) ✅\n"
            "• Документы Word (.docx) ✅\n"
            "• PDF файлы (.pdf) ❌ (только анализ)\n\n"
            "📤 <b>Результат:</b> Отредактированный файл\n\n"
            "⚠️ <b>Ограничения:</b>\n"
            "• Максимальный размер: 20 МБ\n"
            "• Только текстовое содержимое\n\n"
            "🌟 Полная версия на <a href='https://pocket-consultant.ru'>pocket-consultant.ru</a>\n\n"
            "Просто отправьте файл для редактирования:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]])
        )
        context.user_data['waiting_for'] = 'edit_document'
    
    elif query.data == "info":
        log_user_action(user.id, user.username or "Unknown", "button_info")
        
        user_limits_obj = get_user_limits(user.id)
        
        info_message = (
            "ℹ️ <b>Информация о боте</b>\n\n"
            "🤖 <b>Карманный Консультант</b> — ИИ-помощник для юридических вопросов\n\n"
            "<b>Ваши лимиты сегодня:</b>\n"
            f"• Вопросы: {user_limits_obj.questions_count}/{DAILY_QUESTION_LIMIT}\n"
            f"• Документы: {user_limits_obj.documents_count}/{DAILY_DOCUMENT_LIMIT}\n\n"
            "<b>Возможности:</b>\n"
            "• Консультации по российскому праву\n"
            "• Анализ юридических документов\n"
            "• Редактирование и улучшение текстов\n\n"
            "⚠️ <b>Отказ от ответственности:</b>\n"
            "Это упрощенная версия. Ответы носят информационный характер "
            "и не являются юридической консультацией. Обязательно проверяйте "
            "информацию из независимых источников.\n\n"
            "🌟 <b>Полная профессиональная версия доступна на:</b>\n"
            "<a href='https://pocket-consultant.ru'>pocket-consultant.ru</a>"
        )
        
        await query.edit_message_text(
            info_message,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]])
        )
    
    elif query.data == "back_to_main":
        context.user_data.pop('waiting_for', None)
        await query.edit_message_text(
            "🏛️ <b>Карманный Консультант</b>\n\n"
            "Выберите нужное действие:",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_keyboard()
        )

async def ask_perplexity(question: str) -> str:
    """Запрос к Perplexity API"""
    url = "https://api.perplexity.ai/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # Добавляем контекст для юридических вопросов
    system_prompt = (
        "Ты — профессиональный юрист-консультант, специализирующийся на российском праве. "
        "Отвечай только на вопросы, связанные с правовой системой Российской Федерации. "
        "Если вопрос не касается российского права или юридических вопросов, "
        "вежливо откажись отвечать и предложи задать юридический вопрос. "
        "Давай подробные, профессиональные ответы со ссылками на нормативные акты где возможно."
    )
    
    data = {
        "model": "sonar-pro",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question}
        ],
        "max_tokens": 2000,
        "temperature": 0.2
    }
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=30)
        response.raise_for_status()
        
        result = response.json()
        return result['choices'][0]['message']['content']
    
    except Exception as e:
        logger.error(f"Perplexity API error: {e}")
        return "Извините, произошла ошибка при обработке вашего запроса. Попробуйте позже."

async def ask_chatgpt(prompt: str, document_content: str = "") -> str:
    """Запрос к OpenAI через OpenRouter API"""
    try:
        if document_content:
            full_prompt = f"{prompt}\n\nДокумент для обработки:\n{document_content}"
        else:
            full_prompt = prompt
        
        # Определяем системный промпт в зависимости от типа задачи
        if "редактор" in prompt:
            system_content = """Ты — профессиональный юрист-редактор, специализирующийся на улучшении юридических документов по российскому праву. 

КРИТИЧЕСКИ ВАЖНО для редактирования:
- Ты ДОЛЖЕН реально изменить и улучшить текст
- НЕ просто копируй исходный текст
- Улучши формулировки, сделай их более точными юридически
- Исправь стилистические ошибки
- Убери повторы и неточности
- Сделай текст более профессиональным
- Но сохрани смысл и структуру документа

Работай только с юридическими документами. Если документ не связан с правовыми вопросами, вежливо откажись его обрабатывать."""
        else:
            system_content = "Ты — профессиональный юрист, специализирующийся на анализе и редактировании юридических документов по российскому праву. Работай только с юридическими документами. Если документ не связан с правовыми вопросами, вежливо откажись его обрабатывать и предложи прислать юридический документ."
        
        response = await openrouter_client.chat.completions.create(
            model="openai/gpt-4o-mini",  # Используем OpenAI модель через OpenRouter
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": full_prompt}
            ],
            max_tokens=3000,
            temperature=0.7,  # Увеличиваем температуру для более творческого редактирования
            extra_headers={
                "HTTP-Referer": "https://pocket-consultant.ru",
                "X-Title": "Pocket Consultant Bot"
            }
        )
        
        return response.choices[0].message.content
    
    except Exception as e:
        logger.error(f"OpenRouter API error: {e}")
        return "Извините, произошла ошибка при обработке документа. Попробуйте позже."

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    user = update.effective_user
    message_text = update.message.text
    waiting_for = context.user_data.get('waiting_for')
    
    if waiting_for == 'question':
        # Обработка вопроса через Perplexity
        user_limits_obj = get_user_limits(user.id)
        
        if not user_limits_obj.can_ask_question():
            await update.message.reply_text(
                "⛔ Вы достигли дневного лимита вопросов (10 в день).\n\n"
                "💡 <b>Что можно сделать:</b>\n"
                "• Обратиться завтра — лимит обнулится\n"
                "• Воспользоваться полной версией на "
                "<a href='https://pocket-consultant.ru'>pocket-consultant.ru</a>",
                parse_mode=ParseMode.HTML,
                reply_markup=get_main_keyboard()
            )
            return
        
        log_user_action(user.id, user.username or "Unknown", "ask_question", message_text)
        
        # Показываем индикатор печати
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        
        # Отправляем сообщение-заглушку
        waiting_message = await update.message.reply_text(
            "⏳ Подождите, пожалуйста, анализирую ваш запрос...\n"
            "Это может занять до 20 секунд."
        )
        
        # Продолжаем показывать статус "печатает" во время обработки
        async def keep_typing():
            while True:
                await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
                await asyncio.sleep(4)  # Обновляем каждые 4 секунды
        
        # Запускаем индикатор печати в фоне
        typing_task = asyncio.create_task(keep_typing())
        
        try:
            # Запрос к Perplexity
            answer = await ask_perplexity(message_text)
        finally:
            # Останавливаем индикатор печати
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass
            
            # Удаляем сообщение-заглушку
            try:
                await waiting_message.delete()
            except Exception:
                pass  # Игнорируем ошибки удаления
        user_limits_obj.increment_questions()
        save_user_limits()  # Сохраняем изменения
        
        # Конвертируем markdown в HTML
        formatted_answer = markdown_to_html(answer)
        
        # Добавляем предупреждения
        final_answer = (
            f"{formatted_answer}\n\n"
            "⚠️ <b>Важно:</b> Это информационная консультация. "
            "Для принятия юридических решений обязательно проконсультируйтесь с квалифицированным юристом.\n\n"
            "🌟 Полная профессиональная версия доступна на "
            "<a href='https://pocket-consultant.ru'>pocket-consultant.ru</a>"
        )
        
        # Проверяем длину сообщения (лимит Telegram ~4096 символов)
        if len(final_answer) > 4000:
            # Разбиваем на части
            parts = [final_answer[i:i+4000] for i in range(0, len(final_answer), 4000)]
            for part in parts:
                await update.message.reply_text(
                    part,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
        else:
            await update.message.reply_text(
                final_answer,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
        
        # Возвращаем главное меню
        await update.message.reply_text(
            "Выберите следующее действие:",
            reply_markup=get_main_keyboard()
        )
        
        context.user_data.pop('waiting_for', None)
    
    else:
        # Если пользователь пишет без контекста
        await update.message.reply_text(
            "Для начала работы выберите нужное действие:",
            reply_markup=get_main_keyboard()
        )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик документов"""
    user = update.effective_user
    waiting_for = context.user_data.get('waiting_for')
    
    if waiting_for not in ['analyze_document', 'edit_document']:
        await update.message.reply_text(
            "Для обработки документов сначала выберите соответствующее действие:",
            reply_markup=get_main_keyboard()
        )
        return
    
    user_limits_obj = get_user_limits(user.id)
    
    if not user_limits_obj.can_process_document():
        await update.message.reply_text(
            "⛔ Вы достигли дневного лимита обработки документов (10 в день).\n\n"
            "💡 <b>Что можно сделать:</b>\n"
            "• Обратиться завтра — лимит обнулится\n"
            "• Воспользоваться полной версией на "
            "<a href='https://pocket-consultant.ru'>pocket-consultant.ru</a>",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_keyboard()
        )
        return
    
    document = update.message.document
    
    # Проверяем размер файла (20 МБ)
    if document.file_size > 20 * 1024 * 1024:
        await update.message.reply_text(
            "❌ Файл слишком большой. Максимальный размер: 20 МБ",
            reply_markup=get_main_keyboard()
        )
        return
    
    # Проверяем формат файла
    allowed_extensions = ['.txt', '.docx', '.pdf']
    file_extension = os.path.splitext(document.file_name)[1].lower()
    
    if file_extension not in allowed_extensions:
        await update.message.reply_text(
            "❌ Неподдерживаемый формат файла. Поддерживаются: .txt, .docx, .pdf",
            reply_markup=get_main_keyboard()
        )
        return
    
    # Дополнительная проверка для редактирования
    if waiting_for == 'edit_document' and file_extension == '.pdf':
        await update.message.reply_text(
            "❌ <b>PDF файлы нельзя редактировать</b>\n\n"
            "PDF файлы можно только анализировать. Для редактирования и получения "
            "отредактированного файла используйте:\n\n"
            "✅ Текстовые файлы (.txt)\n"
            "✅ Документы Word (.docx)\n\n"
            "💡 Можете сконвертировать PDF в Word и затем отредактировать.",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_keyboard()
        )
        return
    
    log_user_action(user.id, user.username or "Unknown", f"{waiting_for}_file", document.file_name)
    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    # Отправляем сообщение-заглушку для документа
    if waiting_for == 'analyze_document':
        waiting_text = "⏳ Подождите, пожалуйста, анализирую документ...\n"
    else:
        waiting_text = "⏳ Подождите, пожалуйста, редактирую документ...\n"
    
    waiting_message = await update.message.reply_text(
        waiting_text + "Это может занять до 30 секунд."
    )
    
    # Продолжаем показывать статус "печатает" во время обработки
    async def keep_typing():
        while True:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
            await asyncio.sleep(4)  # Обновляем каждые 4 секунды
    
    # Запускаем индикатор печати в фоне
    typing_task = asyncio.create_task(keep_typing())
    
    response = None  # Инициализируем переменную
    
    try:
        # Получаем файл
        file = await context.bot.get_file(document.file_id)
        file_content = await file.download_as_bytearray()
        
        # Извлекаем текст из файла
        document_text = extract_text_from_file(file_content, file_extension)
        
        # Проверяем что текст извлечен
        if not document_text or len(document_text.strip()) < 10:
            raise Exception("Не удалось извлечь текст из документа или документ слишком короткий")
        
        # Формируем запрос для ChatGPT
        if waiting_for == 'analyze_document':
            prompt = "Проанализируй этот юридический документ. Выдели основные пункты, возможные риски и рекомендации."
        else:  # edit_document
            prompt = """Ты юрист-редактор. Твоя задача - улучшить этот юридический документ.

ВАЖНЫЕ ТРЕБОВАНИЯ:
1. В ответе пришли ТОЛЬКО отредактированный текст документа
2. НЕ добавляй никаких комментариев, объяснений или вводных фраз
3. НЕ пиши "Вот отредактированный документ:" или похожие фразы
4. Сохрани структуру документа, но улучши формулировки
5. Исправь грамматические ошибки и улучши юридическую точность
6. Сделай текст более ясным и понятным

Просто пришли улучшенную версию документа:"""
        
        logger.info(f"Processing {waiting_for} for user {user.id}, document length: {len(document_text)} chars")
        logger.info(f"Original document preview: {document_text[:200]}...")
        
        # Запрос к ChatGPT
        response = await ask_chatgpt(prompt, document_text[:8000])  # Ограничиваем длину
        logger.info(f"AI response preview: {response[:200]}..." if response else "No response received")
        user_limits_obj.increment_documents()
        save_user_limits()  # Сохраняем изменения
        
    finally:
        # Останавливаем индикатор печати
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
        
        # Удаляем сообщение-заглушку
        try:
            await waiting_message.delete()
        except Exception:
            pass  # Игнорируем ошибки удаления
    
    # Форматируем ответ
    try:
        if response is None:
            raise Exception("No response received")
        
        if waiting_for == 'edit_document':
            # Для редактирования отправляем файл
            try:
                # Создаем отредактированный файл
                if file_extension == '.txt':
                    # Для текстовых файлов просто сохраняем ответ
                    edited_content = response.encode('utf-8')
                    new_filename = f"edited_{document.file_name}"
                    
                elif file_extension == '.docx':
                    # Для DOCX файлов создаем новый документ
                    from docx import Document as DocxDocument
                    doc = DocxDocument()
                    # Разбиваем текст на параграфы
                    paragraphs = response.split('\n\n')
                    for paragraph_text in paragraphs:
                        if paragraph_text.strip():
                            doc.add_paragraph(paragraph_text.strip())
                    
                    # Сохраняем в BytesIO
                    docx_buffer = io.BytesIO()
                    doc.save(docx_buffer)
                    edited_content = docx_buffer.getvalue()
                    new_filename = f"edited_{document.file_name}"
                    
                else:  # PDF не поддерживаем для редактирования, только для анализа
                    raise Exception("PDF файлы нельзя редактировать, только анализировать")
                
                # Отправляем файл
                await update.message.reply_document(
                    document=io.BytesIO(edited_content),
                    filename=new_filename,
                    caption=(
                        "✅ <b>Документ отредактирован</b>\n\n"
                        "⚠️ <b>Важно:</b> Это автоматическое редактирование. "
                        "Обязательно проверьте результат и проконсультируйтесь с юристом.\n\n"
                        "🌟 Полная профессиональная версия доступна на "
                        "<a href='https://pocket-consultant.ru'>pocket-consultant.ru</a>"
                    ),
                    parse_mode=ParseMode.HTML
                )
                
            except Exception as e:
                logger.error(f"Error creating edited file: {e}")
                # В случае ошибки создания файла отправляем текстом
                formatted_response = markdown_to_html(response)
                await update.message.reply_text(
                    f"📄 <b>Отредактированный документ:</b>\n\n"
                    f"{formatted_response}\n\n"
                    "❌ <b>Не удалось создать файл, поэтому отправляю текстом.</b>\n\n"
                    "⚠️ <b>Важно:</b> Это автоматическое редактирование. "
                    "Обязательно проверьте результат и проконсультируйтесь с юристом.\n\n"
                    "🌟 Полная профессиональная версия доступна на "
                    "<a href='https://pocket-consultant.ru'>pocket-consultant.ru</a>",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
        else:
            # Для анализа отправляем текстом как раньше
            formatted_response = markdown_to_html(response)
            
            final_response = (
                f"📄 <b>Результат анализа документа:</b>\n\n"
                f"{formatted_response}\n\n"
                "⚠️ <b>Важно:</b> Это упрощенная версия анализа. "
                "Для полной юридической экспертизы обратитесь к специалисту.\n\n"
                "🌟 Полная профессиональная версия доступна на "
                "<a href='https://pocket-consultant.ru'>pocket-consultant.ru</a>"
            )
            
            # Отправляем ответ (с учетом лимита символов)
            if len(final_response) > 4000:
                parts = [final_response[i:i+4000] for i in range(0, len(final_response), 4000)]
                for part in parts:
                    await update.message.reply_text(
                        part,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True
                    )
            else:
                await update.message.reply_text(
                    final_response,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
    
    except Exception as e:
        logger.error(f"Document processing error: {e}")
        
        # Более детализированные сообщения об ошибках
        if "No response received" in str(e):
            error_msg = "❌ Не удалось получить ответ от AI. Попробуйте позже."
        elif "extract text" in str(e).lower():
            error_msg = "❌ Не удалось прочитать текст из документа. Проверьте что файл не поврежден и содержит текст."
        elif "слишком короткий" in str(e):
            error_msg = "❌ Документ слишком короткий или не содержит читаемого текста."
        else:
            error_msg = f"❌ Произошла ошибка при обработке документа: {str(e)[:100]}"
        
        await update.message.reply_text(
            error_msg,
            reply_markup=get_main_keyboard()
        )
    
    # Возвращаем главное меню
    await update.message.reply_text(
        "Выберите следующее действие:",
        reply_markup=get_main_keyboard()
    )
    
    context.user_data.pop('waiting_for', None)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"Exception while handling an update: {context.error}")

def main():
    """Основная функция запуска бота"""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not found in environment variables")
        return
    
    # Загружаем лимиты пользователей
    load_user_limits()
    
    # Создаем приложение
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Добавляем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    # Добавляем обработчик ошибок
    application.add_error_handler(error_handler)
    
    # Запускаем бота
    logger.info("Starting bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main() 