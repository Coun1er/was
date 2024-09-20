from aiogram.types import BufferedInputFile


def generate_account_list_file(order_id: str, custom_messages: str, goods: list):
    goods_text = "\n".join(
        [f"{g['seed']}:{g['email_login']}:{g['email_pass']}" for g in goods]
    )

    description_message = (
        f"Заказ: {str(order_id)}\nФормат выдачи: private_seed:email_login:email_pass\n"
    )

    full_text = description_message + custom_messages + goods_text

    file = BufferedInputFile(
        full_text.encode(),
        filename=f"order_{str(order_id)[:8]}_warpcast_accounts.txt",
    )

    return file
