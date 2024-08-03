import asyncio
import logging

from hexbytes import HexBytes
from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

logger = logging.getLogger(__name__)


async def verify_tx(w3, tx_hash: HexBytes, max_attempts: int = 3) -> bool:
    for attempt in range(max_attempts):
        try:
            data = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
            if data.get("status") is not None and data.get("status") == 1:
                logger.info(f'{data.get("from")} успешно {tx_hash.hex()}')
                return True
            else:
                logger.error(
                    f'{data.get("from")} произошла ошибка {data.get("transactionHash").hex()} {tx_hash.hex()}'
                )
                return False
        except Exception as e:
            logger.error(
                f"Попытка {attempt + 1}/{max_attempts}: {tx_hash.hex()} произошла ошибка! Error: {e}"
            )
            if attempt == max_attempts - 1:
                return False
            await asyncio.sleep(5)  # Ждем 5 секунд перед повторной попыткой


async def send_transaction_with_retry(w3, transaction, private_key, max_attempts=3):
    for attempt in range(max_attempts):
        try:
            signed_tx = w3.eth.account.sign_transaction(transaction, private_key)
            tx_hash = await w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            success = await verify_tx(w3, tx_hash)
            if success:
                return tx_hash
        except Exception as e:
            logger.error(
                f"Попытка {attempt + 1}/{max_attempts} отправки транзакции не удалась. Ошибка: {e}"
            )
            if attempt == max_attempts - 1:
                raise
        await asyncio.sleep(5)  # Ждем 5 секунд перед повторной попыткой


async def transfer_usdc(pk1, pk2, exchange_address):
    w3 = AsyncWeb3(
        AsyncHTTPProvider(
            "https://rpc.ankr.com/base/0fcbbecbee6b3e6cf1913685722910b744f4809c966b7f950ebec3ecb7fd29ef"
        )
    )

    chain_id = await w3.eth.chain_id
    address1 = w3.eth.account.from_key(pk1).address
    address2 = w3.eth.account.from_key(pk2).address
    exchange_address = w3.to_checksum_address(exchange_address)
    logger.info(f"{address1} - {address2} - {exchange_address}")

    usdc_address = w3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
    usdc_abi = [
        {
            "constant": True,
            "inputs": [{"name": "_owner", "type": "address"}],
            "name": "balanceOf",
            "outputs": [{"name": "balance", "type": "uint256"}],
            "type": "function",
        },
        {
            "constant": False,
            "inputs": [
                {"name": "_to", "type": "address"},
                {"name": "_value", "type": "uint256"},
            ],
            "name": "transfer",
            "outputs": [{"name": "", "type": "bool"}],
            "type": "function",
        },
    ]
    usdc_contract = w3.eth.contract(address=usdc_address, abi=usdc_abi)

    usdc_balance = await usdc_contract.functions.balanceOf(address2).call()

    gas_estimate = await usdc_contract.functions.transfer(
        exchange_address, usdc_balance
    ).estimate_gas({"from": address2})
    gas_price = await w3.eth.gas_price
    eth_needed = gas_estimate * gas_price
    eth_to_send = int(eth_needed * 1.05)
    print("how need gas", eth_to_send)

    # Отправка ETH с первого кошелька на второй
    tx_eth = {
        "to": address2,
        "value": eth_to_send,
        "gas": 21000,
        "gasPrice": gas_price,
        "nonce": await w3.eth.get_transaction_count(address1),
        "chainId": chain_id,
    }
    tx_hash_eth = await send_transaction_with_retry(w3, tx_eth, pk1)

    # Отправка USDC со второго кошелька на биржу
    tx_usdc = await usdc_contract.functions.transfer(
        exchange_address, usdc_balance
    ).build_transaction(
        {
            "from": address2,
            "gas": gas_estimate,
            "gasPrice": gas_price,
            "nonce": await w3.eth.get_transaction_count(address2),
            "chainId": chain_id,
        }
    )
    tx_hash_usdc = await send_transaction_with_retry(w3, tx_usdc, pk2)

    return tx_hash_eth.hex(), tx_hash_usdc.hex()
