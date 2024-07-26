import asyncio
import os
import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import List

import asyncpg
import pytz
import uvicorn
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.filters.command import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from bson import ObjectId
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel
from web3 import AsyncHTTPProvider, AsyncWeb3, Web3


# Настройки бота
API_TOKEN = os.getenv("API_TOKEN")
MONGO_URL = os.getenv("MONGO_URL")
DB_NAME = os.getenv("DB_NAME")
PG_DATABASE_URL = f'postgresql://{os.getenv("POSTGRES_USER")}:{os.getenv("POSTGRES_PASSWORD")}@postgres:5432/{os.getenv("POSTGRES_DB")}'

# Константа для ключа авторизации
AUTH_KEY = os.getenv("AUTH_KEY")

# Инициализация FastAPI
app = FastAPI()

# Инициализация бота и диспетчера
bot = Bot(token=API_TOKEN)

# Подключение к MongoDB
client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]


class AccountData(BaseModel):
    order_id: str
    user_id: str
    w_seed: str
    w_email_login: str
    w_email_pass: str


class ReadyAccount(BaseModel):
    seed: str
    email_login: str
    email_pass: str


def verify_auth_key(x_auth_key: str = Header(...)):
    if x_auth_key != AUTH_KEY:
        raise HTTPException(status_code=403, detail="Неверный ключ авторизации")
    return x_auth_key


@app.post("/add_ready_accounts")
async def add_ready_accounts(
    request: Request, auth_key: str = Depends(verify_auth_key)
):
    accounts_data = await request.body()
    accounts_data = accounts_data.decode()

    accounts = []
    for line in accounts_data.strip().split("\n"):
        parts = line.split(":")
        if len(parts) != 3:
            raise HTTPException(
                status_code=400, detail=f"Неверный формат строки: {line}"
            )
        accounts.append(
            {"seed": parts[0], "email_login": parts[1], "email_pass": parts[2]}
        )

    async with asyncpg.create_pool(PG_DATABASE_URL) as pool:
        async with pool.acquire() as connection:
            # Получаем записи из queue_goods со статусом New
            new_queue_items = await connection.fetch(
                "SELECT * FROM queue_goods WHERE status = 'New'"
            )

            # Группируем queue_goods по order_id
            grouped_queue_items = {}
            for item in new_queue_items:
                if item["order_id"] not in grouped_queue_items:
                    grouped_queue_items[item["order_id"]] = []
                grouped_queue_items[item["order_id"]].append(item)

            processed_accounts = 0
            remaining_accounts = accounts.copy()

            for order_id, queue_items in grouped_queue_items.items():
                order = await db.orders.find_one({"_id": ObjectId(order_id)})
                if not order:
                    continue

                new_registration_accounts = order["registration_accounts"]
                need_accounts = order["need_accounts"]
                tg_user_id = order["tg_user_id"]

                registered_accounts = 0
                order_accounts = []

                for queue_item in queue_items:
                    if not remaining_accounts:
                        break

                    if new_registration_accounts >= need_accounts:
                        break

                    account = remaining_accounts.pop(0)
                    new_registration_accounts += 1
                    registered_accounts += 1

                    # Добавляем новую запись в Goods
                    new_goods = {
                        "create_date": datetime.now(pytz.utc),
                        "order_id": ObjectId(order_id),
                        "user_id": queue_item["user_id"],
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
                            "$set": {
                                "registration_accounts": new_registration_accounts
                            },
                            "$push": {"goods": new_goods_id},
                        },
                    )

                    # Обновляем статус в queue_goods
                    await connection.execute(
                        "UPDATE queue_goods SET status = 'Done' WHERE id = $1",
                        queue_item["id"],
                    )

                    order_accounts.append(
                        f"{account['seed']}:{account['email_login']}:{account['email_pass']}"
                    )

                    processed_accounts += 1

                # Отправляем сообщение пользователю
                if new_registration_accounts == need_accounts:
                    await db.orders.update_one(
                        {"_id": ObjectId(order_id)}, {"$set": {"status": "Done"}}
                    )

                    completion_message = (
                        f"✅Заказ №{order_id} полностью выполнен!✅\n\n"
                        f"🔋Зарегистрировано все {new_registration_accounts}/{need_accounts} аккаунтов\n"
                    )
                    await bot.send_message(tg_user_id, completion_message)

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

                    file = BufferedInputFile(
                        goods_text.encode(), filename=f"order_{order_id[:8]}_goods.txt"
                    )
                    await bot.send_document(tg_user_id, file)
                else:
                    remaining = need_accounts - new_registration_accounts
                    partial_completion_message = (
                        f"🎉 Зарегистрировали вам <b>{registered_accounts} аккаунта(-ов)</b> "
                        f"{new_registration_accounts}/{need_accounts} 🎉\n"
                        f"⚠️Ожидайте регистрацию оставшихся {remaining} аккаунта(-ов).\n\n"
                        f"📝 Заказ №: {order_id}:\n\n"
                        f"🔑 Данные аккаунтов:\n"
                        f"Нажмите на команду старт и кликнете на кнопку под сообщением с нужным заказом: /start"
                    )
                    await bot.send_message(
                        tg_user_id, partial_completion_message, parse_mode="HTML"
                    )

            # Записываем оставшиеся аккаунты в ready_accounts
            for account in remaining_accounts:
                ready_account = {
                    "create_date": datetime.now(pytz.utc),
                    "update_date": datetime.now(pytz.utc),
                    "seed": account["seed"],
                    "email_login": account["email_login"],
                    "email_pass": account["email_pass"],
                    "status": "available",
                }
                await db.ready_accounts.insert_one(ready_account)

    return {
        "status": "success",
        "message": f"Обработано {processed_accounts} аккаунтов, {len(remaining_accounts)} добавлено в ready_accounts",
    }


@app.post("/add_account")
async def add_account(account_data: AccountData):
    # Находим заказ по order_id
    order = await db.orders.find_one({"_id": ObjectId(account_data.order_id)})
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    tg_user_id = order["tg_user_id"]

    # Увеличиваем registration_accounts на 1
    new_registration_accounts = order["registration_accounts"] + 1

    # Проверяем, не превышает ли новое значение need_accounts
    if new_registration_accounts > order["need_accounts"]:
        # Сохраняем избыточный аккаунт в ready_accounts
        ready_account = {
            "create_date": datetime.now(pytz.utc),
            "update_date": datetime.now(pytz.utc),
            "seed": account_data.w_seed,
            "email_login": account_data.w_email_login,
            "email_pass": account_data.w_email_pass,
            "status": "available",
        }
        await db.ready_accounts.insert_one(ready_account)
        return {
            "status": "success",
            "message": f"Лимит у заказа {account_data.order_id} превышан {new_registration_accounts}/{order['need_accounts']} записали в ready_account базу.",
        }

    # Добавляем новую запись в Goods
    new_goods = {
        "create_date": datetime.now(pytz.utc),
        "order_id": ObjectId(account_data.order_id),
        "user_id": account_data.user_id,
        "seed": account_data.w_seed,
        "email_login": account_data.w_email_login,
        "email_pass": account_data.w_email_pass,
    }
    result = await db.goods.insert_one(new_goods)
    new_goods_id = result.inserted_id

    # Обновляем заказ
    await db.orders.update_one(
        {"_id": ObjectId(account_data.order_id)},
        {
            "$set": {"registration_accounts": new_registration_accounts},
            "$push": {"goods": new_goods_id},
        },
    )

    # Отправляем сообщение пользователю о новом аккаунте
    message = (
        f"🎉 Новый зарегистрированный аккаунт {new_registration_accounts}/{order['need_accounts']} 🎉\n"
        f"📝 Заказ №: {account_data.order_id}:\n\n"
        f"🔑 Данные аккаунта:\n"
        f"Нажмите на команду старт и кликнете на кнопку под сообщением с нужным заказом: /start"
        # f"<code>{account_data.w_seed}:{account_data.w_email_login}:{account_data.w_email_pass}</code>"
    )

    try:
        await bot.send_message(tg_user_id, message, parse_mode="HTML")
    except Exception as e:
        print(f"Не удалось отправить сообщение пользователю {tg_user_id}: {e}")

    # Проверяем, выполнен ли заказ полностью
    if new_registration_accounts == order["need_accounts"]:
        await db.orders.update_one(
            {"_id": ObjectId(account_data.order_id)}, {"$set": {"status": "Done"}}
        )

        completion_message = (
            f"✅Заказ №{account_data.order_id} полностью выполнен!✅\n\n"
            f"🔋Зарегистрировано все {new_registration_accounts}/{order['need_accounts']} аккаунтов\n"
        )
        await bot.send_message(tg_user_id, completion_message)

        order = await db.orders.find_one({"_id": ObjectId(account_data.order_id)})

        goods_ids = order.get("goods", [])
        goods_object_ids = [ObjectId(gid) for gid in goods_ids]

        goods = await db.goods.find({"_id": {"$in": goods_object_ids}}).to_list(
            length=None
        )

        goods_text = "\n".join(
            [f"{g['seed']}:{g['email_login']}:{g['email_pass']}" for g in goods]
        )

        file = BufferedInputFile(
            goods_text.encode(), filename=f"order_{account_data.order_id[:8]}_goods.txt"
        )
        await bot.send_document(tg_user_id, file)

    return {"status": "success", "message": "Аккаунт успешно добавлен"}


# if __name__ == "__main__":
#     uvicorn.run(app, host="0.0.0.0", port=8000)
