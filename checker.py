import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta

import asyncpg
import pytz
from aiogram import Bot, Dispatcher
from aiogram.types import (
    BufferedInputFile,
)
from bson import ObjectId
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorClient
from web3 import AsyncHTTPProvider, AsyncWeb3

from custom_message import CUSTOM_MESSAGES_IN_FILE

# Настройки бота
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URL = os.getenv("MONGO_URL")
DB_NAME = os.getenv("DB_NAME")
PG_DATABASE_URL = f'postgresql://{os.getenv("POSTGRES_USER")}:{os.getenv("POSTGRES_PASSWORD")}@postgres:5432/{os.getenv("POSTGRES_DB")}'

logging.basicConfig(level=logging.INFO)

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Подключение к MongoDB
client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]


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
            logger.info("Добавили в бд для работы")


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

    logger.info(
        f"Заказ {order_id} мы внутри функции wait payment время окончания {end_time} текущие время {datetime.now(pytz.utc)}"
    )
    while datetime.now(pytz.utc) < end_time:
        logger.info("Зашли внутрь цикла бесконечного")
        order = await db.orders.find_one({"_id": ObjectId(order_id)})
        if not order:
            return  # Заказ не найден, выходим из функции
        logger.info("Дошли до проверки баланса")
        balance_in_usdc = await get_usdc_balance(
            w3=w3, abi=ERC_20, address=order["pay_address"]
        )
        logger.info(
            f"Заказ {order_id} баланс кошелька {balance_in_usdc} а нужно для оплаты {total_price}"
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

            logger.info(f"зареганные аккаунты {registered_accounts}")
            # Проверяем что нету готовых аккаунтов
            if registered_accounts == 0:
                logger.info("Зашли в добавление")
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
                    # Если все аккаунты зарегистрированы
                    completion_message = (
                        f"✅Заказ №{order_id} полностью выполнен!✅\n\n"
                        f"🔋Зарегистрировано все {registered_accounts}/{order['need_accounts']} аккаунтов\n\n"
                        f"🔑 Данные аккаунта:\n"
                        f"Введите команду старт и кликнете на кнопку под сообщением с нужным заказом: /start"
                    )
                    await bot.send_message(
                        tg_user_id, completion_message, parse_mode="HTML"
                    )

                    order = await db.orders.find_one({"_id": ObjectId(order_id)})

                    # Cтавим статус заказа Done если все аки зареганы
                    await db.orders.update_one(
                        {"_id": ObjectId(order_id)}, {"$set": {"status": "Done"}}
                    )

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
                        f"Введите команду старт и кликнете на кнопку под сообщением с нужным заказом: /start"
                    )
                    await bot.send_message(
                        tg_user_id, partial_completion_message, parse_mode="HTML"
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


async def check_pending_orders():
    # Получаем все заказы в статусе "New"
    current_time = datetime.now(pytz.utc)
    pending_orders = await db.orders.find({"status": "New"}).to_list(None)

    for order in pending_orders:
        order_id = order["_id"]
        tg_user_id = order["tg_user_id"]
        total_price = order["price_sum"]
        create_date = order["create_date"]

        if create_date.tzinfo is None:
            create_date = pytz.utc.localize(create_date)

        # Вычисляем время окончания ожидания оплаты (15 минут после создания заказа)
        end_time = create_date + timedelta(minutes=15)

        if current_time > end_time:
            # Время ожидания истекло, меняем статус на "Cancel"
            await db.orders.update_one(
                {"_id": order_id}, {"$set": {"status": "Cancel"}}
            )

            # Отправляем сообщение пользователю
            try:
                await bot.send_message(
                    tg_user_id,
                    f"❌ <b>В отведенное время оплата так и не поступила, поэтому заказ №{order_id} отменен.</b>",
                    parse_mode="HTML",
                )
            except Exception as e:
                print(f"Не удалось отправить сообщение пользователю {tg_user_id}: {e}")
        else:
            logger.info(f"Запустили ожидания платежа {order_id}")
            # Время ожидания еще не истекло, запускаем wait_for_payment
            await wait_for_payment(order_id, tg_user_id, total_price, end_time)
            # asyncio.create_task(
            #     wait_for_payment(order_id, tg_user_id, total_price, end_time)
            # )

    print(
        f"Проверка незавершенных заказов завершена. Обработано заказов: {len(pending_orders)}"
    )


async def check_queue_goods_table():
    async with asyncpg.create_pool(PG_DATABASE_URL) as pool:
        async with pool.acquire() as connection:
            # Проверяем существование таблицы
            table_exists = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_name = 'queue_goods'
                );
            """
            )

            if not table_exists:
                # Если таблица не существует, создаем ее
                await connection.execute(
                    """
                    CREATE TABLE queue_goods (
                        id SERIAL PRIMARY KEY,
                        uuid UUID NOT NULL,
                        create_date TIMESTAMP WITH TIME ZONE NOT NULL,
                        update_date TIMESTAMP WITH TIME ZONE NOT NULL,
                        order_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        status TEXT NOT NULL
                    );
                """
                )
                print("Таблица queue_goods успешно создана.")
            else:
                print("Таблица queue_goods уже существует.")


async def main():
    logger.info("Запустили чекер")
    await check_pending_orders()
    await check_queue_goods_table()


if __name__ == "__main__":
    asyncio.run(main())
