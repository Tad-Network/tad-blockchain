import sys
from typing import Optional

import click


@click.group("wallet", short_help="Manage your wallet")
def wallet_cmd() -> None:
    pass


@wallet_cmd.command("get_transaction", short_help="Get a transaction")
@click.option(
    "-wp",
    "--wallet-rpc-port",
    help="Set the port where the Wallet is hosting the RPC interface. See the rpc_port under wallet in config.yaml",
    type=int,
    default=None,
)
@click.option("-f", "--fingerprint", help="Set the fingerprint to specify which wallet to use", type=int)
@click.option("-i", "--id", help="Id of the wallet to use", type=int, default=1, show_default=True, required=True)
@click.option("-tx", "--tx_id", help="transaction id to search for", type=str, required=True)
@click.option("--verbose", "-v", count=True, type=int)
def get_transaction_cmd(wallet_rpc_port: Optional[int], fingerprint: int, id: int, tx_id: str, verbose: int) -> None:
    extra_params = {"id": id, "tx_id": tx_id, "verbose": verbose}
    import asyncio
    from .wallet_funcs import execute_with_wallet, get_transaction

    asyncio.run(execute_with_wallet(wallet_rpc_port, fingerprint, extra_params, get_transaction))


@wallet_cmd.command("get_transactions", short_help="Get all transactions")
@click.option(
    "-wp",
    "--wallet-rpc-port",
    help="Set the port where the Wallet is hosting the RPC interface. See the rpc_port under wallet in config.yaml",
    type=int,
    default=None,
)
@click.option("-f", "--fingerprint", help="Set the fingerprint to specify which wallet to use", type=int)
@click.option("-i", "--id", help="Id of the wallet to use", type=int, default=1, show_default=True, required=True)
@click.option(
    "-o",
    "--offset",
    help="Skip transactions from the beginning of the list",
    type=int,
    default=0,
    show_default=True,
    required=True,
)
@click.option("--verbose", "-v", count=True, type=int)
@click.option(
    "--paginate/--no-paginate",
    default=None,
    help="Prompt for each page of data.  Defaults to true for interactive consoles, otherwise false.",
)
def get_transactions_cmd(
    wallet_rpc_port: Optional[int],
    fingerprint: int,
    id: int,
    offset: int,
    verbose: bool,
    paginate: Optional[bool],
) -> None:
    extra_params = {"id": id, "verbose": verbose, "offset": offset, "paginate": paginate}
    import asyncio
    from .wallet_funcs import execute_with_wallet, get_transactions

    asyncio.run(execute_with_wallet(wallet_rpc_port, fingerprint, extra_params, get_transactions))

    # The flush/close avoids output like below when piping through `head -n 1`
    # which will close stdout.
    #
    # Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>
    # BrokenPipeError: [Errno 32] Broken pipe
    sys.stdout.flush()
    sys.stdout.close()


@wallet_cmd.command("send", short_help="Send tad to another wallet")
@click.option(
    "-wp",
    "--wallet-rpc-port",
    help="Set the port where the Wallet is hosting the RPC interface. See the rpc_port under wallet in config.yaml",
    type=int,
    default=None,
)
@click.option("-f", "--fingerprint", help="Set the fingerprint to specify which wallet to use", type=int)
@click.option("-i", "--id", help="Id of the wallet to use", type=int, default=1, show_default=True, required=True)
@click.option("-a", "--amount", help="How much tad to send, in TAD", type=str, required=True)
@click.option(
    "-m",
    "--fee",
    help="Set the fees for the transaction, in TAD",
    type=str,
    default="0",
    show_default=True,
    required=True,
)
@click.option("-t", "--address", help="Address to send the TAD", type=str, required=True)
@click.option(
    "-o", "--override", help="Submits transaction without checking for unusual values", is_flag=True, default=False
)
def send_cmd(
    wallet_rpc_port: Optional[int], fingerprint: int, id: int, amount: str, fee: str, address: str, override: bool
) -> None:
    extra_params = {"id": id, "amount": amount, "fee": fee, "address": address, "override": override}
    import asyncio
    from .wallet_funcs import execute_with_wallet, send

    asyncio.run(execute_with_wallet(wallet_rpc_port, fingerprint, extra_params, send))


@wallet_cmd.command("show", short_help="Show wallet information")
@click.option(
    "-wp",
    "--wallet-rpc-port",
    help="Set the port where the Wallet is hosting the RPC interface. See the rpc_port under wallet in config.yaml",
    type=int,
    default=None,
)
@click.option("-f", "--fingerprint", help="Set the fingerprint to specify which wallet to use", type=int)
def show_cmd(wallet_rpc_port: Optional[int], fingerprint: int) -> None:
    import asyncio
    from .wallet_funcs import execute_with_wallet, print_balances

    asyncio.run(execute_with_wallet(wallet_rpc_port, fingerprint, {}, print_balances))


@wallet_cmd.command("get_address", short_help="Get a wallet receive address")
@click.option(
    "-wp",
    "--wallet-rpc-port",
    help="Set the port where the Wallet is hosting the RPC interface. See the rpc_port under wallet in config.yaml",
    type=int,
    default=None,
)
@click.option("-i", "--id", help="Id of the wallet to use", type=int, default=1, show_default=True, required=True)
@click.option("-f", "--fingerprint", help="Set the fingerprint to specify which wallet to use", type=int)
def get_address_cmd(wallet_rpc_port: Optional[int], id, fingerprint: int) -> None:
    extra_params = {"id": id}
    import asyncio
    from .wallet_funcs import execute_with_wallet, get_address

    asyncio.run(execute_with_wallet(wallet_rpc_port, fingerprint, extra_params, get_address))


@wallet_cmd.command(
    "delete_unconfirmed_transactions", short_help="Deletes all unconfirmed transactions for this wallet ID"
)
@click.option(
    "-wp",
    "--wallet-rpc-port",
    help="Set the port where the Wallet is hosting the RPC interface. See the rpc_port under wallet in config.yaml",
    type=int,
    default=None,
)
@click.option("-i", "--id", help="Id of the wallet to use", type=int, default=1, show_default=True, required=True)
@click.option("-f", "--fingerprint", help="Set the fingerprint to specify which wallet to use", type=int)
def delete_unconfirmed_transactions_cmd(wallet_rpc_port: Optional[int], id, fingerprint: int) -> None:
    extra_params = {"id": id}
    import asyncio
    from .wallet_funcs import execute_with_wallet, delete_unconfirmed_transactions

    asyncio.run(execute_with_wallet(wallet_rpc_port, fingerprint, extra_params, delete_unconfirmed_transactions))
