import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from enum import Enum
from math import ceil
from typing import List, Tuple

import asyncpg
import pytz
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import (
    SimpleRequestHandler,
    setup_application,
)
from aiohttp import web
from bson import ObjectId
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorClient
from PIL import Image, ImageDraw, ImageFont
from web3 import AsyncHTTPProvider, AsyncWeb3, Web3

from custom_message import CUSTOM_MESSAGES_IN_FILE

# Настройки бота
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URL = os.getenv("MONGO_URL")
DB_NAME = os.getenv("DB_NAME")
PG_DATABASE_URL = f'postgresql://{os.getenv("POSTGRES_USER")}:{os.getenv("POSTGRES_PASSWORD")}@postgres:5432/{os.getenv("POSTGRES_DB")}'
ORDERS_PER_PAGE = 5

# Настройки Webhook
DOMAIN = os.getenv("DOMAIN")
WEBHOOK = os.getenv("WEBHOOK")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = 3001
WEBHOOK_PATH = "/webhook"

# Хеш для генерации картинки с ценами
CURRENT_PRICE_HASH = None

logging.basicConfig(level=logging.INFO)

# Создание подключения к Redis и его инициализация для aiogramm
# redis_client = redis.Redis(host="redis", port=6379, db=0)
storage = RedisStorage.from_url("redis://redis:6379/0")

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

# Подключение к MongoDB
client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

# Градации цен
price_gradations = [
    (1, 15.99),
    (3, 14.99),
    (6, 13.99),
    (11, 13.49),
    (101, 11.99),
    (float("inf"), 11.99),
]


# Список админов
ADMIN_IDS = [
    306409980,
]


# Определение перечисления Status
class Status(str, Enum):
    NEW = "New"
    WORKED = "Worked"
    DONE = "Done"
    CANCEL = "Cancel"


# Определение состояний для FSM
class OrderStates(StatesGroup):
    waiting_for_quantity = State()
    waiting_for_confirmation = State()


# Приветственный текст
START_TEXT = "Меня зовут <b>Варпрегер Михалыч</b>, я бот который умеет регистрировать аккаунты для Варкаста."
FUNCTION_TEXT = (
    "📊 <b>Чтобы посмотреть цены или оформить заказ, тыкай сюда:</b> /new_order"
)


# Функция для получения или создания пользователя
async def get_or_create_user(tg_user_id, tg_username):
    user = await db.users.find_one({"tg_user_id": tg_user_id})
    if not user:
        user = {
            "_id": ObjectId(),
            "create_date": datetime.utcnow(),
            "update_date": datetime.utcnow(),
            "tg_username": tg_username,
            "tg_user_id": tg_user_id,
        }
        await db.users.insert_one(user)
    return user


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    # Отправляем промежуточное сообщение
    processing_msg = await message.answer("Обработка...")

    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    username = message.from_user.username or message.from_user.first_name
    orders = await db.orders.find({"user_id": user["_id"]}).to_list(length=None)

    if orders:
        orders_text = format_orders_text(orders)
        response = f"👋 <b>Привет, {username}!</b> \n\n{START_TEXT} \n\n🗂️ <b>История твоих предыдущих заказов:</b>\n\n{orders_text}\n\n{FUNCTION_TEXT}\n\n📥 Чтобы получить товары заказа, используйте кнопки ниже:"
    else:
        response = f"👋 <b>Привет, {username}!</b> \n\n{START_TEXT} \n\nУ тебя еще нет заказов.\n\n {FUNCTION_TEXT}"

    keyboard = create_orders_keyboard(orders, 0)
    photo = FSInputFile("desc.png")

    # Удаляем промежуточное сообщение
    await processing_msg.delete()

    # Отправляем финальное сообщение
    await message.answer_photo(
        photo=photo, caption=response, parse_mode="HTML", reply_markup=keyboard
    )


@dp.message(Command("support"))
async def support_command(message: types.Message):
    support_text = (
        "📞 <b>Поддержка</b>\n\n"
        "По всем вопросам писать: @Coun1er\n"
        "➖➖➖➖➖➖➖➖➖➖➖➖➖\n"
        "⚠️ <b>Внимание!</b>\n"
        "В саппорт писать только в самом крайнем случае и с обязательной "
        "пометкой в сообщении <code>warpcast reger bot</code>.\n\n"
        "❗️ Сообщения без пометки останутся без ответа.\n"
        "🚫 Сообщения с глупыми вопросами приведут к мгновенному бану."
    )

    await message.answer(support_text, parse_mode="HTML")


@dp.message(Command("stats"))
async def stats_command(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    # Получаем все заказы со статусом "Worked"
    pipeline = [
        {"$match": {"status": "Worked"}},
        {
            "$group": {
                "_id": None,
                "total_orders": {"$sum": 1},
                "total_accounts_needed": {"$sum": "$need_accounts"},
                "total_accounts_registered": {"$sum": "$registration_accounts"},
                "total_price_sum": {"$sum": "$price_sum"},
            }
        },
    ]

    result = await db.orders.aggregate(pipeline).to_list(length=None)

    if not result:
        await message.reply("На данный момент нет заказов в работе.")
        return

    stats = result[0]
    total_orders = stats["total_orders"]
    total_accounts_needed = stats["total_accounts_needed"]
    total_accounts_registered = stats["total_accounts_registered"]
    total_price_sum = stats["total_price_sum"]
    accounts_in_queue = total_accounts_needed - total_accounts_registered

    # Рассчитываем проценты
    if total_accounts_needed > 0:
        registered_percent = (total_accounts_registered / total_accounts_needed) * 100
        queue_percent = (accounts_in_queue / total_accounts_needed) * 100
    else:
        registered_percent = 0
        queue_percent = 0

    moscow_tz = pytz.timezone("Europe/Moscow")
    today = datetime.now(moscow_tz).strftime("%d.%m.%Y")

    response = f"""
<b>Статистика заказов в работе (Worked):</b>

Количество заказов: <b>{total_orders}</b>
Общая сумма заказов: <b>${total_price_sum:.2f}</b>
Общее количество аккаунтов: <b>{total_accounts_needed}</b>
Уже зарегистрировано: <b>{total_accounts_registered}</b> ({registered_percent:.2f}%)
На сегодня ({today}) очередь на регистрацию: <b>{accounts_in_queue}</b> ({queue_percent:.2f}%)
"""

    await message.answer(response, parse_mode="HTML")


@dp.message(Command("statspay"))
async def statspay_command(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    # Получаем статистику по заказам
    pipeline = [
        {"$match": {"status": {"$in": ["Worked", "Done"]}}},
        {
            "$group": {
                "_id": "$status",
                "count": {"$sum": 1},
                "total_sum": {"$sum": "$price_sum"},
                "total_accounts": {"$sum": "$need_accounts"},
                "withdrawn": {
                    "$sum": {"$cond": [{"$eq": ["$withdrawal", True]}, 1, 0]}
                },
                "withdrawn_sum": {
                    "$sum": {"$cond": [{"$eq": ["$withdrawal", True]}, "$price_sum", 0]}
                },
                "withdrawn_accounts": {
                    "$sum": {
                        "$cond": [{"$eq": ["$withdrawal", True]}, "$need_accounts", 0]
                    }
                },
                "paid_profit": {
                    "$sum": {"$cond": [{"$eq": ["$paid", True]}, "$profit", 0]}
                },
                "unpaid_profit": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$withdrawal", True]},
                                    {"$ne": ["$paid", True]},
                                ]
                            },
                            "$profit",
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "statuses": {"$push": "$$ROOT"},
                "total_orders": {"$sum": "$count"},
                "total_sum": {"$sum": "$total_sum"},
                "total_accounts": {"$sum": "$total_accounts"},
                "total_withdrawn": {"$sum": "$withdrawn"},
                "total_withdrawn_sum": {"$sum": "$withdrawn_sum"},
                "total_withdrawn_accounts": {"$sum": "$withdrawn_accounts"},
                "total_paid_profit": {"$sum": "$paid_profit"},
                "total_unpaid_profit": {"$sum": "$unpaid_profit"},
            }
        },
    ]

    result = await db.orders.aggregate(pipeline).to_list(length=None)

    if not result:
        await message.reply("На данный момент нет заказов в статусе Worked или Done.")
        return

    stats = result[0]
    total_orders = stats["total_orders"]
    total_sum = stats["total_sum"]
    total_accounts = stats["total_accounts"]
    total_withdrawn = stats["total_withdrawn"]
    total_withdrawn_sum = stats["total_withdrawn_sum"]
    total_withdrawn_accounts = stats["total_withdrawn_accounts"]
    total_paid_profit = stats["total_paid_profit"]
    total_unpaid_profit = stats["total_unpaid_profit"]

    # Вычисляем статистику по статусам
    status_stats = {status["_id"]: status for status in stats["statuses"]}

    # Вычисляем невыведенные заказы
    not_withdrawn = total_orders - total_withdrawn
    not_withdrawn_sum = total_sum - total_withdrawn_sum
    not_withdrawn_accounts = total_accounts - total_withdrawn_accounts

    # Вычисляем среднюю прибыль с аккаунта
    avg_profit_per_account = (
        (total_paid_profit + total_unpaid_profit) / total_accounts
        if total_accounts > 0
        else 0
    )

    # Вычисляем среднюю оплату за аккаунт для невыведенных заказов
    avg_price_per_account = (
        not_withdrawn_sum / not_withdrawn_accounts if not_withdrawn_accounts > 0 else 0
    )

    moscow_tz = pytz.timezone("Europe/Moscow")
    now = datetime.now(moscow_tz).strftime("%d.%m.%Y %H:%M")

    response = f"""
<b>Статистика заказов и выплат на {now} (МСК):</b>

Всего заказов (Worked и Done): <b>{total_orders}</b> на общую сумму <b>${total_sum:.2f}</b>
- Worked: <b>{status_stats.get('Worked', {}).get('count', 0)}</b> заказов на сумму <b>${status_stats.get('Worked', {}).get('total_sum', 0):.2f}</b>
- Done: <b>{status_stats.get('Done', {}).get('count', 0)}</b> заказов на сумму <b>${status_stats.get('Done', {}).get('total_sum', 0):.2f}</b>

Выводы денег:
💰 Выведены <b>{total_withdrawn}</b> заказов на сумму <b>${total_withdrawn_sum:.2f}</b> ({total_withdrawn_sum/total_sum*100:.2f}% от общей суммы)
- Уже выплаченная прибыль: <b>${total_paid_profit:.2f}</b>
- Общая прибыль к выплате: <b>${total_unpaid_profit:.2f}</b>
- Средняя прибыль с аккаунта: <b>${avg_profit_per_account:.2f}</b>

❌ Не выведены <b>{not_withdrawn}</b> заказов на сумму <b>${not_withdrawn_sum:.2f}</b> ({not_withdrawn_sum/total_sum*100:.2f}% от общей суммы)
- Общее количество аккаунтов в невыведенных заказах: <b>{not_withdrawn_accounts}</b>
- Средняя оплата за аккаунт: <b>${avg_price_per_account:.2f}</b>
"""

    await message.answer(response, parse_mode="HTML")


@dp.message(Command("admin"))
async def admin_command(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    response = """
<b>Панель администратора</b>

Доступные команды:

1. /stats - Статистика заказов
   Эта команда показывает общую статистику по текущим "Worked" заказам, включая количество заказов, общее количество аккаунтов, количество зарегистрированных аккаунтов и аккаунтов в очереди.

2. /statspay - Статистика выплат
   Эта команда предоставляет детальную информацию о заказах в статусах "Worked" и "Done", включая общую сумму заказов, статистику по выведенным и невыведенным заказам, прибыль и среднюю стоимость аккаунта.

Вы можете скопировать и вставить эти команды в чат для их использования.
"""

    await message.answer(response, parse_mode="HTML")


@dp.message(Command("postforall"))
async def cmd_postforall(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    if not message.reply_to_message:
        await message.answer(
            "Эта команда должна быть отправлена в ответ на сообщение, которое вы хотите разослать."
        )
        return

    processing_msg = await message.answer("Обработка...")

    post_to_send = message.reply_to_message

    # users = await db.users.find().to_list(length=None)
    users = [
        306409980,
    ]

    total_users = len(users)
    success_count = 0
    fail_count = 0

    for index, user in enumerate(users, start=1):
        tg_user_id = user["tg_user_id"]

        for attempt in range(3):
            try:
                if post_to_send.photo:
                    await message.bot.send_photo(
                        chat_id=tg_user_id,
                        photo=post_to_send.photo[-1].file_id,
                        caption=post_to_send.caption,
                        caption_entities=post_to_send.caption_entities,
                    )
                else:
                    await message.bot.send_message(
                        chat_id=tg_user_id,
                        text=post_to_send.text,
                        entities=post_to_send.entities,
                    )
                success_count += 1
                break
            except Exception:
                if attempt == 2:
                    fail_count += 1
            await asyncio.sleep(0.05)

            if index % 30 == 0:
                await processing_msg.edit_text(f"Обработка... {index}/{total_users}")

        await asyncio.sleep(0.1)
    success_percent = (success_count / total_users) * 100 if total_users > 0 else 0
    fail_percent = (fail_count / total_users) * 100 if total_users > 0 else 0

    report = (
        f"Всего пользователей: {total_users}\n"
        f"Успешно отправлено: {success_count} ({success_percent:.2f}%)\n"
        f"Не удалось отправить: {fail_count} ({fail_percent:.2f}%)"
    )

    await processing_msg.edit_text(report)


def format_orders_text(orders):
    orders_text = ""
    moscow_tz = pytz.timezone("Europe/Moscow")
    for order in orders:
        order_id = str(order["_id"])
        order_id_short = order_id[-8:]
        create_date = order.get("create_date")
        if isinstance(create_date, datetime):
            create_date = create_date.replace(tzinfo=pytz.UTC).astimezone(moscow_tz)
            create_date_str = create_date.strftime("%d.%m.%Y %H:%M")
        else:
            create_date_str = "Неизвестно"
        need_accounts = order.get("need_accounts", 0)
        registration_accounts = order.get("registration_accounts", 0)
        status = order.get("status", "Неизвестно")
        price_sum = order.get("price_sum", 0)

        orders_text += f"{order_id_short} | {create_date_str} | {need_accounts}/{registration_accounts} | {status} | ${price_sum:.2f}\n"
    return orders_text


def create_orders_keyboard(orders, page):
    keyboard_builder = InlineKeyboardBuilder()
    # Фильтруем заказы с registration_accounts > 0
    downloadable_orders = [
        order for order in orders if order.get("registration_accounts", 0) > 0
    ]
    # Вычисляем общее количество страниц
    total_pages = ceil(len(downloadable_orders) / ORDERS_PER_PAGE)
    # Получаем заказы для текущей страницы
    start_idx = page * ORDERS_PER_PAGE
    end_idx = start_idx + ORDERS_PER_PAGE
    current_page_orders = downloadable_orders[start_idx:end_idx]

    for order in current_page_orders:
        order_id = str(order["_id"])
        order_id_short = order_id[-8:]
        registration_accounts = order.get("registration_accounts", 0)
        need_accounts = order.get("need_accounts", 0)
        button_text = (
            f"Скачать №{order_id_short} {registration_accounts}/{need_accounts}"
        )
        keyboard_builder.row(
            InlineKeyboardButton(
                text=button_text, callback_data=f"order_goods:{order_id}"
            )
        )

    # Добавляем навигационные кнопки
    nav_buttons = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"page:{page-1}")
        )
    if page < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton(text="Вперед ▶️", callback_data=f"page:{page+1}")
        )
    keyboard_builder.row(*nav_buttons)

    return keyboard_builder.as_markup()


@dp.callback_query(lambda c: c.data.startswith("page:"))
async def process_page_button(callback_query: CallbackQuery):
    page = int(callback_query.data.split(":")[1])
    user = await get_or_create_user(
        callback_query.from_user.id, callback_query.from_user.username
    )
    orders = await db.orders.find({"user_id": user["_id"]}).to_list(length=None)

    keyboard = create_orders_keyboard(orders, page)
    await callback_query.message.edit_reply_markup(reply_markup=keyboard)
    await callback_query.answer()


async def cmd_order_goods(message: types.Message, order_id: str = None):
    if order_id is None:
        order_id = message.text.split()[1] if len(message.text.split()) > 1 else None
    if not order_id:
        await message.answer("Пожалуйста, укажите ID заказа.")
        return

    # Получаем tg_id пользователя, отправившего запрос
    tg_user_id = message.chat.id

    # Находим заказ и проверяем, принадлежит ли он пользователю
    order = await db.orders.find_one({"_id": ObjectId(order_id)})
    if not order:
        await message.answer("Заказ не найден")
        return

    # Проверка принадлежности заказа пользователю
    if order.get("tg_user_id") != tg_user_id:
        await message.answer("У вас нет доступа к этим данным")
        return

    # Остальной код функции остается без изменений
    goods_ids = order.get("goods", [])
    goods_object_ids = [ObjectId(gid) for gid in goods_ids]
    goods = await db.goods.find({"_id": {"$in": goods_object_ids}}).to_list(length=None)

    if not goods:
        await message.answer("Для этого заказа нет доступных товаров")
        return

    goods_text = "\n".join(
        [f"{g['seed']}:{g['email_login']}:{g['email_pass']}" for g in goods]
    )

    description_message = (
        f"Заказ: {str(order_id)}\nФормат выдачи: private_seed:email_login:email_pass\n"
    )

    # Объединяем пользовательский текст и текст товаров
    full_text = description_message + CUSTOM_MESSAGES_IN_FILE + goods_text

    file = BufferedInputFile(
        full_text.encode(),
        filename=f"order_{str(order_id)[:8]}_warpcast_accounts.txt",
    )

    await message.answer_document(file)


# Обработчик для callback-запросов от inline кнопок
@dp.callback_query(lambda c: c.data.startswith("order_goods:"))
async def process_order_goods_button(callback_query: types.CallbackQuery):
    order_id = callback_query.data.split(":")[1]
    await cmd_order_goods(callback_query.message, order_id)
    await callback_query.answer()


# Обновляем хэндлер для команды /order_goods
@dp.message(Command("order_goods"))
async def order_goods_command(message: types.Message):
    await cmd_order_goods(message)


async def get_usdc_balance(w3: AsyncWeb3, abi: str, address: str) -> int:
    contract_address = w3.to_checksum_address(
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    )
    contract_instance = w3.eth.contract(address=contract_address, abi=abi)

    max_retries = 5
    delay = 1.0

    for attempt in range(max_retries):
        try:
            balance_wei = await contract_instance.functions.balanceOf(address).call()
            balance_human = balance_wei / 10**6
            logger.info(f"Баланс у кошелька {address} | {balance_human}")
            return balance_human
        except Exception as e:
            if attempt == max_retries - 1:
                raise Exception(
                    f"Не удалось получить баланс USDC после {max_retries} попыток: {str(e)}"
                )
            print(
                f"Ошибка при получении баланса USDC (попытка {attempt + 1}/{max_retries}): {str(e)}"
            )
            await asyncio.sleep(delay)

    raise Exception(f"Не удалось получить баланс USDC после {max_retries} попыток")


async def insert_queue_goods(order_id: str, user_id: int, num_accounts: int):
    async with asyncpg.create_pool(PG_DATABASE_URL) as pool:
        async with pool.acquire() as connection:
            values = [
                (
                    str(uuid.uuid4()),
                    datetime.now(pytz.utc),
                    str(order_id),
                    str(user_id),
                    "New",
                )
                for _ in range(num_accounts)
            ]
            await connection.executemany(
                """
                INSERT INTO queue_goods (uuid, create_date, update_date, order_id, user_id, status)
                VALUES ($1, $2, $2, $3, $4, $5)
            """,
                values,
            )


async def wait_for_payment(
    order_id: str, tg_user_id: int, total_price: float, end_time: datetime
):
    w3 = AsyncWeb3(
        AsyncHTTPProvider(
            "https://rpc.ankr.com/base/0fcbbecbee6b3e6cf1913685722910b744f4809c966b7f950ebec3ecb7fd29ef"
        )
    )
    current_dir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(current_dir, "data/erc_20_abi.json"), "r") as f:
        ERC_20 = f.read()

    while datetime.now(pytz.utc) < end_time:
        order = await db.orders.find_one({"_id": ObjectId(order_id)})
        if not order:
            return  # Заказ не найден, выходим из функции

        balance_in_usdc = await get_usdc_balance(
            w3=w3, abi=ERC_20, address=order["pay_address"]
        )
        if balance_in_usdc >= total_price or order["status"] == "Worked":
            # Оплата получена
            await db.orders.update_one(
                {"_id": ObjectId(order_id)}, {"$set": {"status": "Worked"}}
            )

            # Отправляем сообщение об успешной оплате
            await bot.send_message(
                tg_user_id,
                f"✅ Оплата успешно получена!\n"
                f"Ваш заказ <b>№{order_id}</b> на регистрацию <b>{order['need_accounts']}</b> аккаунта(-ов) отправлен в работу.\n\n"
                f"📋 <b>Информация по заказу:</b>\n\n"
                f"- Аккаунты всех заказов регистрируются в рандомном порядке.\n"
                f"- По мере регистрации мы будем отправлять вам уведомления и они сразу станут доступны для скачивания в списке заказов по команде /start в формате: seed:email_login:email_pass.\n",
                parse_mode="HTML",
            )

            # Проверяем доступные аккаунты в ready_accounts
            available_accounts = await db.ready_accounts.find(
                {"status": "available"}
            ).to_list(length=order["need_accounts"])
            registered_accounts = min(len(available_accounts), order["need_accounts"])

            # Проверяем что нету готовых аккаунтов
            if registered_accounts == 0:
                # Если нет доступных аккаунтов, добавляем все аккаунты заказа в PostgreSQL
                await insert_queue_goods(
                    order_id, order["user_id"], order["need_accounts"]
                )
                return

            for account in available_accounts[:registered_accounts]:
                new_goods = {
                    "create_date": datetime.now(pytz.utc),
                    "order_id": ObjectId(order_id),
                    "user_id": order["user_id"],
                    "seed": account["seed"],
                    "email_login": account["email_login"],
                    "email_pass": account["email_pass"],
                }
                result = await db.goods.insert_one(new_goods)
                new_goods_id = result.inserted_id

                # Обновляем заказ
                await db.orders.update_one(
                    {"_id": ObjectId(order_id)},
                    {
                        "$inc": {"registration_accounts": 1},
                        "$push": {"goods": new_goods_id},
                    },
                )

                # Обновляем статус аккаунта в ready_accounts
                await db.ready_accounts.update_one(
                    {"_id": account["_id"]}, {"$set": {"status": "used"}}
                )

            # Отправляем сообщение о зарегистрированных аккаунтах
            if registered_accounts > 0:
                if registered_accounts == order["need_accounts"]:
                    # Cтавим статус заказа Done если все аки зареганы
                    await db.orders.update_one(
                        {"_id": ObjectId(order_id)},
                        {"$set": {"status": "Done"}},
                    )

                    # Если все аккаунты зарегистрированы
                    completion_message = (
                        f"✅Заказ №{order_id} полностью выполнен!✅\n\n"
                        f"🔋Зарегистрировано все {registered_accounts}/{order['need_accounts']} аккаунтов\n\n"
                        f"🔑 Данные аккаунта:\n"
                        f"Нажмите на команду старт и кликнете на кнопку под сообщением с нужным заказом: /start"
                    )
                    await bot.send_message(
                        tg_user_id, completion_message, parse_mode="HTML"
                    )

                    order = await db.orders.find_one({"_id": ObjectId(order_id)})
                    goods_ids = order.get("goods", [])
                    goods_object_ids = [ObjectId(gid) for gid in goods_ids]
                    goods = await db.goods.find(
                        {"_id": {"$in": goods_object_ids}}
                    ).to_list(length=None)

                    goods_text = "\n".join(
                        [
                            f"{g['seed']}:{g['email_login']}:{g['email_pass']}"
                            for g in goods
                        ]
                    )

                    description_message = f"Заказ: {str(order_id)}\nФормат выдачи: private_seed:email_login:email_pass\n"

                    # Объединяем пользовательский текст и текст товаров
                    full_text = (
                        description_message + CUSTOM_MESSAGES_IN_FILE + goods_text
                    )

                    file = BufferedInputFile(
                        full_text.encode(),
                        filename=f"order_{str(order_id)[:8]}_warpcast_accounts.txt",
                    )
                    await bot.send_document(tg_user_id, file)
                else:
                    # Если зарегистрирована только часть аккаунтов
                    remaining_accounts = order["need_accounts"] - registered_accounts
                    partial_completion_message = (
                        f"🎉 Зарегистрировали вам <b>{registered_accounts} аккаунтов</b> "
                        f"{registered_accounts}/{order['need_accounts']} 🎉\n"
                        f"⚠️Ожидайте регистрацию оставшихся {remaining_accounts} аккаунтов.\n\n"
                        f"📝 Заказ №: {order_id}:\n\n"
                        f"🔑 Данные аккаунта:\n"
                        f"Нажмите на команду старт и кликнете на кнопку под сообщением с нужным заказом: /start"
                    )
                    await bot.send_message(
                        tg_user_id,
                        partial_completion_message,
                        parse_mode="HTML",
                    )

                    # Добавляем записи в PostgreSQL только для оставшихся аккаунтов
                    await insert_queue_goods(
                        order_id, order["user_id"], remaining_accounts
                    )

            return

        # Ждем 30 секунд перед следующей проверкой
        await asyncio.sleep(30)

    # Время истекло, оплата не получена
    await db.orders.update_one(
        {"_id": ObjectId(order_id)}, {"$set": {"status": "Cancel"}}
    )
    await bot.send_message(
        tg_user_id,
        f"❌ <b>В отведенное время оплата так и не поступила, поэтому заказ №{order_id} отменен.</b>",
        parse_mode="HTML",
    )


def generate_price_image(
    price_gradations: List[Tuple[float, float]],
    filename: str = "price_list.png",
) -> str:
    global CURRENT_PRICE_HASH
    new_price_hash = hashlib.md5(str(price_gradations).encode()).hexdigest()

    if CURRENT_PRICE_HASH == new_price_hash:
        return filename

    width, height = 600, 400
    image = Image.new("RGB", (width, height), "#1b1b1b")
    draw = ImageDraw.Draw(image)

    title_font = ImageFont.truetype("nofire.ttf", 28)
    price_font = ImageFont.truetype("nofire.ttf", 24)

    # Рассчитываем общую высоту всего текста
    title = "ACCOUNTS PRICE"
    title_bbox = title_font.getbbox(title)
    title_height = title_bbox[3] - title_bbox[1]

    price_texts = []
    total_text_height = title_height
    for i, (min_qty, price) in enumerate(price_gradations[:-1]):
        if i == len(price_gradations) - 2 or price_gradations[i + 1][0] == float("inf"):
            range_str = f"{min_qty}+"
        else:
            next_min_qty = price_gradations[i + 1][0]
            range_str = f"{min_qty}-{next_min_qty-1}"
        text = f"{range_str} accounts: ${price:.2f} per account"
        price_texts.append(text)
        text_bbox = price_font.getbbox(text)
        total_text_height += (
            text_bbox[3] - text_bbox[1] + 10
        )  # 10 пикселей отступа между строками

    # Рассчитываем начальную Y-позицию для вертикального центрирования
    start_y = (height - total_text_height) // 2

    # Рисуем заголовок
    title_bbox = title_font.getbbox(title)
    title_width = title_bbox[2] - title_bbox[0]
    title_position = ((width - title_width) // 2, start_y)
    draw.text(title_position, title, font=title_font, fill="white")

    # Рисуем цены
    y = start_y + title_height + 20  # 20 пикселей отступа после заголовка
    for text in price_texts:
        text_bbox = price_font.getbbox(text)
        text_width = text_bbox[2] - text_bbox[0]
        text_position = ((width - text_width) // 2, y)
        draw.text(text_position, text, font=price_font, fill="white")
        y += text_bbox[3] - text_bbox[1] + 10  # 10 пикселей отступа между строками

    image.save(filename)

    CURRENT_PRICE_HASH = new_price_hash

    return filename


def generate_price_text(price_gradations: List[Tuple[float, float]]) -> str:
    price_ranges = []
    for i, (min_qty, price) in enumerate(
        price_gradations[:-1]
    ):  # Исключаем последний элемент
        if i == len(price_gradations) - 2 or price_gradations[i + 1][0] == float("inf"):
            range_str = f"{min_qty}+"
        else:
            next_min_qty = price_gradations[i + 1][0]
            range_str = f"{min_qty}-{next_min_qty-1}"

        price_ranges.append(f"<b>{range_str} аккаунтов:</b> ${price:.2f} за аккаунт")

    price_text = "<b>💲 Цены на аккаунты в зависимости от количества:</b>\n\n"
    price_text += "\n".join(price_ranges)
    price_text += "\n\n<b>📥 Введите количество аккаунтов (от 1 до 200):</b>"

    return price_text


def calculate_price(quantity):
    for min_qty, price_per_account in reversed(price_gradations):
        if quantity >= min_qty:
            total_price = round(quantity * price_per_account, 2)
            return price_per_account, total_price
    return None, None


@dp.message(Command("new_order"))
async def cmd_new_order(message: types.Message, state: FSMContext):
    # Отправляем промежуточное сообщение
    processing_msg = await message.answer("Формируем прайс...")

    # price_text = generate_price_text(price_gradations)
    text = "- Для регистрации используются трастовые <b>европейские IP-адреса</b>\n- Аккаунты оплачиваются только <b>банковской картой</b> (не варпы!)\n- У аккаунтов заполнены уникальная <b>аватарка, тег и ник</b>\n\n\n📥 <b>Введите желаемое количество аккаунтов для расчета стоимости:</b>"
    image_path = generate_price_image(price_gradations)

    # Удаляем промежуточное сообщение
    await processing_msg.delete()

    await message.answer_photo(
        photo=types.FSInputFile(image_path), caption=text, parse_mode="HTML"
    )
    # await message.answer(price_text, parse_mode="HTML")
    await state.set_state(OrderStates.waiting_for_quantity)


@dp.message(OrderStates.waiting_for_quantity)
async def process_quantity(message: types.Message, state: FSMContext):
    try:
        quantity = int(message.text)
        if quantity < 1 or quantity > 200:
            if quantity > 200:
                await message.answer(
                    "Такое количество можно заказать только по дополнительному согласованию, сейчас можно оформить заказ от 1 до 200 аккаунтов."
                )
            else:
                await message.answer(
                    "Пожалуйста, введите колличество аккаунтов от 1 до 200."
                )
            return
    except ValueError:
        await message.answer(
            "Пожалуйста, введите целое число без пробелов и специальных символов."
        )
        return

    price_per_account, total_price = calculate_price(quantity)

    await state.update_data(
        quantity=quantity,
        price_per_account=price_per_account,
        total_price=total_price,
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Я согласен с условиями, оформляю заказ",
                    callback_data="confirm_order",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отменить/Изменить заказ", callback_data="cancel_order"
                )
            ],
        ]
    )

    await message.answer(
        f"📋 Для регистрации <u>{quantity} аккаунта(-ов)</u> общая сумма заказа составит <b>{total_price} USDC</b> (по $<code>{price_per_account}</code> за аккаунт)",
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    await state.set_state(OrderStates.waiting_for_confirmation)


@dp.callback_query(OrderStates.waiting_for_confirmation)
async def process_callback(callback_query: types.CallbackQuery, state: FSMContext):
    if callback_query.data == "cancel_order":
        await state.clear()
        await cmd_new_order(callback_query.message, state)
    elif callback_query.data == "confirm_order":
        # Отправляем промежуточное сообщение
        processing_msg = await callback_query.message.answer("Создаем заказ...")

        # Генерация адреса и приватного ключа
        w3 = Web3()
        account = w3.eth.account.create()
        pay_address = account.address
        pay_address_pk = account.key.hex()

        # Получение данных из состояния
        data = await state.get_data()
        quantity = data["quantity"]
        price_per_account = data["price_per_account"]
        total_price = data["total_price"]

        # Сохранение в бэкап
        backup_data = {
            "address": pay_address,
            "private_key": pay_address_pk,
            "quantity": quantity,
            "price_per_account": price_per_account,
            "total_price": total_price,
            "timestamp": datetime.now().isoformat(),
        }

        backup_dir = "./backup"
        os.makedirs(backup_dir, exist_ok=True)
        backup_file = os.path.join(
            backup_dir,
            f"wallet_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        )

        with open(backup_file, "w") as f:
            json.dump(backup_data, f, indent=4)

        # Создание заказа в базе данных
        user = await get_or_create_user(
            callback_query.from_user.id, callback_query.from_user.username
        )
        new_order = {
            "user_id": user["_id"],
            "tg_user_id": user["tg_user_id"],
            "create_date": datetime.utcnow(),
            "need_accounts": quantity,
            "registration_accounts": 0,
            "status": "New",
            "price_per_account": price_per_account,
            "price_sum": total_price,
            "pay_address": pay_address,
            "pay_address_pk": pay_address_pk,
            "goods": [],
        }
        result = await db.orders.insert_one(new_order)
        order_id = result.inserted_id

        # Расчет времени окончания ожидания оплаты
        moscow_tz = pytz.timezone("Europe/Moscow")
        end_time = datetime.now(moscow_tz) + timedelta(minutes=15)
        formatted_end_time = end_time.strftime("%Y-%m-%d %H:%M:%S")

        # Удаляем промежуточное сообщение
        await processing_msg.delete()

        await callback_query.message.answer(
            f"""📦 Заказ <b>№{order_id}</b> на регистрацию <b>{quantity}</b> аккаунта(-ов) создан!\n\n💼 Ожидаем оплату:\n\n🔵 Сеть: BASE\n💰 Сумма: <code>{total_price}</code> <b>USDC</b> (Оплата в USDC, а не ETH❗)\n🏦 Адрес для перевода: <code>{pay_address}</code>\n\n⏳ Оплату будем ожидать в течение <b>15 минут до {formatted_end_time}</b>. После поступления денег вы получите сообщение об успешной оплате. По прошествии этого времени заказ будет автоматически отменен.\n\n🔔 Важная информация:\n\nУбедитесь, что вы переводите средства на правильный адрес.\nПожалуйста, переводите не меньше указанной суммы - больше можно, меньше нет.\n\nСпасибо за понимание! 💬""",
            parse_mode="HTML",
        )
        await state.clear()

        # Запуск задачи ожидания оплаты
        asyncio.create_task(
            wait_for_payment(
                order_id, callback_query.from_user.id, total_price, end_time
            )
        )


@dp.message(OrderStates.waiting_for_confirmation)
async def invalid_confirmation_input(message: types.Message):
    await message.delete()


async def on_startup(bot: Bot) -> None:
    responce = await bot.set_webhook(
        f"{DOMAIN}{WEBHOOK_PATH}", secret_token=WEBHOOK_SECRET
    )
    logger.info(responce)
    logger.info(
        f"Telegram servers now send updates to {DOMAIN}{WEBHOOK_PATH}. Bot is online"
    )


def main_webhook() -> None:
    # Register startup hook to initialize webhook
    # dp.startup.register(on_startup)

    # Create aiohttp.web.Application instance
    app = web.Application()

    # Create an instance of request handler,
    # aiogram has few implementations for different cases of usage
    # In this example we use SimpleRequestHandler which is designed to handle simple cases
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    )
    # Register webhook handler on application
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)

    # Mount dispatcher startup and shutdown hooks to aiohttp application
    setup_application(app, dp, bot=bot)

    # And finally start webserver
    web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)


async def main_polling():
    await bot.delete_webhook()
    logger.info("Бот запущен через полинг")
    await dp.start_polling(bot)


if __name__ == "__main__":
    logger.info(f"Выбран режим: {WEBHOOK}")
    if WEBHOOK == "1":
        main_webhook()
    else:
        asyncio.run(main_polling())
