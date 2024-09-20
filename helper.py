from aiogram.types import BufferedInputFile


def generate_account_list_file(order_id: str, custom_messages: str, goods: list):
    goods_text = "\n\nprivate_seed:email_login:email_pass\n\n" + "\n".join(
        [f"{g['seed']}:{g['email_login']}:{g['email_pass']}" for g in goods]
    )

    description_message = f"Заказ: {str(order_id)}\nФормат выдачи: private_seed:email_login:email_pass и просто private_seed\n"

    goods_text_only_private_seed = "\n\nprivate_seed\n\n" + "\n".join(
        [f"{g['seed']}" for g in goods]
    )

    full_text = (
        description_message
        + custom_messages
        + goods_text
        + goods_text_only_private_seed
    )

    file = BufferedInputFile(
        full_text.encode(),
        filename=f"order_{str(order_id)[:8]}_warpcast_accounts.txt",
    )

    return file
