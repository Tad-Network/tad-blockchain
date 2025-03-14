import asyncio
import base64
import json
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import aiosqlite
from blspy import AugSchemeMPL, G1Element, PrivateKey
from chiabip158 import PyBIP158
from cryptography.fernet import Fernet

from tad import __version__
from tad.consensus.block_record import BlockRecord
from tad.consensus.coinbase import pool_parent_id, farmer_parent_id
from tad.consensus.constants import ConsensusConstants
from tad.consensus.find_fork_point import find_fork_point_in_chain
from tad.full_node.weight_proof import WeightProofHandler
from tad.pools.pool_puzzles import SINGLETON_LAUNCHER_HASH, solution_to_pool_state
from tad.pools.pool_wallet import PoolWallet
from tad.protocols.wallet_protocol import PuzzleSolutionResponse, RespondPuzzleSolution
from tad.types.blockchain_format.coin import Coin
from tad.types.blockchain_format.program import Program
from tad.types.blockchain_format.sized_bytes import bytes32
from tad.types.coin_spend import CoinSpend
from tad.types.full_block import FullBlock
from tad.types.header_block import HeaderBlock
from tad.types.mempool_inclusion_status import MempoolInclusionStatus
from tad.util.byte_types import hexstr_to_bytes
from tad.util.db_wrapper import DBWrapper
from tad.util.errors import Err
from tad.util.hash import std_hash
from tad.util.ints import uint32, uint64, uint128
from tad.wallet.block_record import HeaderBlockRecord
from tad.wallet.cc_wallet.cc_wallet import CCWallet
from tad.wallet.derivation_record import DerivationRecord
from tad.wallet.derive_keys import master_sk_to_backup_sk, master_sk_to_wallet_sk
from tad.wallet.key_val_store import KeyValStore
from tad.wallet.rl_wallet.rl_wallet import RLWallet
from tad.wallet.settings.user_settings import UserSettings
from tad.wallet.trade_manager import TradeManager
from tad.wallet.transaction_record import TransactionRecord
from tad.wallet.util.backup_utils import open_backup_file
from tad.wallet.util.transaction_type import TransactionType
from tad.wallet.util.wallet_types import WalletType
from tad.wallet.wallet import Wallet
from tad.wallet.wallet_action import WalletAction
from tad.wallet.wallet_action_store import WalletActionStore
from tad.wallet.wallet_block_store import WalletBlockStore
from tad.wallet.wallet_blockchain import WalletBlockchain
from tad.wallet.wallet_coin_record import WalletCoinRecord
from tad.wallet.wallet_coin_store import WalletCoinStore
from tad.wallet.wallet_info import WalletInfo, WalletInfoBackup
from tad.wallet.wallet_interested_store import WalletInterestedStore
from tad.wallet.wallet_pool_store import WalletPoolStore
from tad.wallet.wallet_puzzle_store import WalletPuzzleStore
from tad.wallet.wallet_sync_store import WalletSyncStore
from tad.wallet.wallet_transaction_store import WalletTransactionStore
from tad.wallet.wallet_user_store import WalletUserStore
from tad.server.server import TadServer
from tad.wallet.did_wallet.did_wallet import DIDWallet


def get_balance_from_coin_records(coin_records: Set[WalletCoinRecord]) -> uint128:
    amount: uint128 = uint128(0)
    for record in coin_records:
        amount = uint128(amount + record.coin.amount)
    return uint128(amount)


class WalletStateManager:
    constants: ConsensusConstants
    config: Dict
    tx_store: WalletTransactionStore
    puzzle_store: WalletPuzzleStore
    user_store: WalletUserStore
    action_store: WalletActionStore
    basic_store: KeyValStore

    start_index: int

    # Makes sure only one asyncio thread is changing the blockchain state at one time
    lock: asyncio.Lock

    tx_lock: asyncio.Lock

    log: logging.Logger

    # TODO Don't allow user to send tx until wallet is synced
    sync_mode: bool
    genesis: FullBlock

    state_changed_callback: Optional[Callable]
    pending_tx_callback: Optional[Callable]
    puzzle_hash_created_callbacks: Dict = defaultdict(lambda *x: None)
    new_peak_callbacks: Dict = defaultdict(lambda *x: None)
    db_path: Path
    db_connection: aiosqlite.Connection
    db_wrapper: DBWrapper

    main_wallet: Wallet
    wallets: Dict[uint32, Any]
    private_key: PrivateKey

    trade_manager: TradeManager
    new_wallet: bool
    user_settings: UserSettings
    blockchain: Any
    block_store: WalletBlockStore
    coin_store: WalletCoinStore
    sync_store: WalletSyncStore
    interested_store: WalletInterestedStore
    pool_store: WalletPoolStore
    weight_proof_handler: Any
    server: TadServer
    root_path: Path

    @staticmethod
    async def create(
        private_key: PrivateKey,
        config: Dict,
        db_path: Path,
        constants: ConsensusConstants,
        server: TadServer,
        root_path: Path,
        name: str = None,
    ):
        self = WalletStateManager()
        self.new_wallet = False
        self.config = config
        self.constants = constants
        self.server = server
        self.root_path = root_path
        self.log = logging.getLogger(name if name else __name__)
        self.lock = asyncio.Lock()
        self.log.debug(f"Starting in db path: {db_path}")
        self.db_connection = await aiosqlite.connect(db_path)
        await self.db_connection.execute("pragma journal_mode=wal")
        await self.db_connection.execute("pragma synchronous=OFF")

        self.db_wrapper = DBWrapper(self.db_connection)
        self.coin_store = await WalletCoinStore.create(self.db_wrapper)
        self.tx_store = await WalletTransactionStore.create(self.db_wrapper)
        self.puzzle_store = await WalletPuzzleStore.create(self.db_wrapper)
        self.user_store = await WalletUserStore.create(self.db_wrapper)
        self.action_store = await WalletActionStore.create(self.db_wrapper)
        self.basic_store = await KeyValStore.create(self.db_wrapper)
        self.trade_manager = await TradeManager.create(self, self.db_wrapper)
        self.user_settings = await UserSettings.create(self.basic_store)
        self.block_store = await WalletBlockStore.create(self.db_wrapper)
        self.interested_store = await WalletInterestedStore.create(self.db_wrapper)
        self.pool_store = await WalletPoolStore.create(self.db_wrapper)

        self.blockchain = await WalletBlockchain.create(
            self.block_store,
            self.coin_store,
            self.tx_store,
            self.pool_store,
            self.constants,
            self.new_transaction_block_callback,
            self.reorg_rollback,
            self.lock,
        )
        self.weight_proof_handler = WeightProofHandler(self.constants, self.blockchain)

        self.sync_mode = False
        self.sync_store = await WalletSyncStore.create()

        self.state_changed_callback = None
        self.pending_tx_callback = None
        self.db_path = db_path

        main_wallet_info = await self.user_store.get_wallet_by_id(1)
        assert main_wallet_info is not None

        self.private_key = private_key
        self.main_wallet = await Wallet.create(self, main_wallet_info)

        self.wallets = {main_wallet_info.id: self.main_wallet}

        wallet = None
        for wallet_info in await self.get_all_wallet_info_entries():
            if wallet_info.type == WalletType.STANDARD_WALLET:
                if wallet_info.id == 1:
                    continue
                wallet = await Wallet.create(config, wallet_info)
            elif wallet_info.type == WalletType.COLOURED_COIN:
                wallet = await CCWallet.create(
                    self,
                    self.main_wallet,
                    wallet_info,
                )
            elif wallet_info.type == WalletType.RATE_LIMITED:
                wallet = await RLWallet.create(self, wallet_info)
            elif wallet_info.type == WalletType.DISTRIBUTED_ID:
                wallet = await DIDWallet.create(
                    self,
                    self.main_wallet,
                    wallet_info,
                )
            elif wallet_info.type == WalletType.POOLING_WALLET:
                wallet = await PoolWallet.create_from_db(
                    self,
                    self.main_wallet,
                    wallet_info,
                )
            if wallet is not None:
                self.wallets[wallet_info.id] = wallet

        async with self.puzzle_store.lock:
            index = await self.puzzle_store.get_last_derivation_path()
            if index is None or index < self.config["initial_num_public_keys"] - 1:
                await self.create_more_puzzle_hashes(from_zero=True)

        return self

    @property
    def peak(self) -> Optional[BlockRecord]:
        peak = self.blockchain.get_peak()
        return peak

    def get_derivation_index(self, pubkey: G1Element, max_depth: int = 1000) -> int:
        for i in range(0, max_depth):
            derived = self.get_public_key(uint32(i))
            if derived == pubkey:
                return i
        return -1

    def get_public_key(self, index: uint32) -> G1Element:
        return master_sk_to_wallet_sk(self.private_key, index).get_g1()

    async def load_wallets(self):
        for wallet_info in await self.get_all_wallet_info_entries():
            if wallet_info.id in self.wallets:
                continue
            if wallet_info.type == WalletType.STANDARD_WALLET:
                if wallet_info.id == 1:
                    continue
                wallet = await Wallet.create(self.config, wallet_info)
                self.wallets[wallet_info.id] = wallet
            # TODO add RL AND DiD WALLETS HERE
            elif wallet_info.type == WalletType.COLOURED_COIN:
                wallet = await CCWallet.create(
                    self,
                    self.main_wallet,
                    wallet_info,
                )
                self.wallets[wallet_info.id] = wallet
            elif wallet_info.type == WalletType.DISTRIBUTED_ID:
                wallet = await DIDWallet.create(
                    self,
                    self.main_wallet,
                    wallet_info,
                )
                self.wallets[wallet_info.id] = wallet

    async def get_keys(self, puzzle_hash: bytes32) -> Optional[Tuple[G1Element, PrivateKey]]:
        index_for_puzzlehash = await self.puzzle_store.index_for_puzzle_hash(puzzle_hash)
        if index_for_puzzlehash is None:
            raise ValueError(f"No key for this puzzlehash {puzzle_hash})")
        private = master_sk_to_wallet_sk(self.private_key, index_for_puzzlehash)
        pubkey = private.get_g1()
        return pubkey, private

    async def create_more_puzzle_hashes(self, from_zero: bool = False, in_transaction=False):
        """
        For all wallets in the user store, generates the first few puzzle hashes so
        that we can restore the wallet from only the private keys.
        """
        targets = list(self.wallets.keys())

        unused: Optional[uint32] = await self.puzzle_store.get_unused_derivation_path()
        if unused is None:
            # This handles the case where the database has entries but they have all been used
            unused = await self.puzzle_store.get_last_derivation_path()
            if unused is None:
                # This handles the case where the database is empty
                unused = uint32(0)

        if self.new_wallet:
            to_generate = self.config["initial_num_public_keys_new_wallet"]
        else:
            to_generate = self.config["initial_num_public_keys"]

        for wallet_id in targets:
            target_wallet = self.wallets[wallet_id]

            last: Optional[uint32] = await self.puzzle_store.get_last_derivation_path_for_wallet(wallet_id)

            start_index = 0
            derivation_paths: List[DerivationRecord] = []

            if last is not None:
                start_index = last + 1

            # If the key was replaced (from_zero=True), we should generate the puzzle hashes for the new key
            if from_zero:
                start_index = 0

            for index in range(start_index, unused + to_generate):
                if WalletType(target_wallet.type()) == WalletType.POOLING_WALLET:
                    continue
                if WalletType(target_wallet.type()) == WalletType.RATE_LIMITED:
                    if target_wallet.rl_info.initialized is False:
                        break
                    wallet_type = target_wallet.rl_info.type
                    if wallet_type == "user":
                        rl_pubkey = G1Element.from_bytes(target_wallet.rl_info.user_pubkey)
                    else:
                        rl_pubkey = G1Element.from_bytes(target_wallet.rl_info.admin_pubkey)
                    rl_puzzle: Program = target_wallet.puzzle_for_pk(rl_pubkey)
                    puzzle_hash: bytes32 = rl_puzzle.get_tree_hash()

                    rl_index = self.get_derivation_index(rl_pubkey)
                    if rl_index == -1:
                        break

                    derivation_paths.append(
                        DerivationRecord(
                            uint32(rl_index),
                            puzzle_hash,
                            rl_pubkey,
                            target_wallet.type(),
                            uint32(target_wallet.id()),
                        )
                    )
                    break

                pubkey: G1Element = self.get_public_key(uint32(index))
                puzzle: Program = target_wallet.puzzle_for_pk(bytes(pubkey))
                if puzzle is None:
                    self.log.warning(f"Unable to create puzzles with wallet {target_wallet}")
                    break
                puzzlehash: bytes32 = puzzle.get_tree_hash()
                self.log.info(f"Puzzle at index {index} wallet ID {wallet_id} puzzle hash {puzzlehash.hex()}")
                derivation_paths.append(
                    DerivationRecord(
                        uint32(index),
                        puzzlehash,
                        pubkey,
                        target_wallet.type(),
                        uint32(target_wallet.id()),
                    )
                )

            await self.puzzle_store.add_derivation_paths(derivation_paths, in_transaction)
        if unused > 0:
            await self.puzzle_store.set_used_up_to(uint32(unused - 1), in_transaction)

    async def update_wallet_puzzle_hashes(self, wallet_id):
        derivation_paths: List[DerivationRecord] = []
        target_wallet = self.wallets[wallet_id]
        last: Optional[uint32] = await self.puzzle_store.get_last_derivation_path_for_wallet(wallet_id)
        unused: Optional[uint32] = await self.puzzle_store.get_unused_derivation_path()
        if unused is None:
            # This handles the case where the database has entries but they have all been used
            unused = await self.puzzle_store.get_last_derivation_path()
            if unused is None:
                # This handles the case where the database is empty
                unused = uint32(0)
        for index in range(unused, last):
            pubkey: G1Element = self.get_public_key(uint32(index))
            puzzle: Program = target_wallet.puzzle_for_pk(bytes(pubkey))
            puzzlehash: bytes32 = puzzle.get_tree_hash()
            self.log.info(f"Generating public key at index {index} puzzle hash {puzzlehash.hex()}")
            derivation_paths.append(
                DerivationRecord(
                    uint32(index),
                    puzzlehash,
                    pubkey,
                    target_wallet.wallet_info.type,
                    uint32(target_wallet.wallet_info.id),
                )
            )
        await self.puzzle_store.add_derivation_paths(derivation_paths)

    async def get_unused_derivation_record(self, wallet_id: uint32, in_transaction=False) -> DerivationRecord:
        """
        Creates a puzzle hash for the given wallet, and then makes more puzzle hashes
        for every wallet to ensure we always have more in the database. Never reusue the
        same public key more than once (for privacy).
        """
        async with self.puzzle_store.lock:
            # If we have no unused public keys, we will create new ones
            unused: Optional[uint32] = await self.puzzle_store.get_unused_derivation_path()
            if unused is None:
                await self.create_more_puzzle_hashes()

            # Now we must have unused public keys
            unused = await self.puzzle_store.get_unused_derivation_path()
            assert unused is not None
            record: Optional[DerivationRecord] = await self.puzzle_store.get_derivation_record(unused, wallet_id)
            assert record is not None

            # Set this key to used so we never use it again
            await self.puzzle_store.set_used_up_to(record.index, in_transaction=in_transaction)

            # Create more puzzle hashes / keys
            await self.create_more_puzzle_hashes(in_transaction=in_transaction)
            return record

    async def get_current_derivation_record_for_wallet(self, wallet_id: uint32) -> Optional[DerivationRecord]:
        async with self.puzzle_store.lock:
            # If we have no unused public keys, we will create new ones
            current: Optional[DerivationRecord] = await self.puzzle_store.get_current_derivation_record_for_wallet(
                wallet_id
            )
            return current

    def set_callback(self, callback: Callable):
        """
        Callback to be called when the state of the wallet changes.
        """
        self.state_changed_callback = callback

    def set_pending_callback(self, callback: Callable):
        """
        Callback to be called when new pending transaction enters the store
        """
        self.pending_tx_callback = callback

    def set_coin_with_puzzlehash_created_callback(self, puzzlehash: bytes32, callback: Callable):
        """
        Callback to be called when new coin is seen with specified puzzlehash
        """
        self.puzzle_hash_created_callbacks[puzzlehash] = callback

    def set_new_peak_callback(self, wallet_id: int, callback: Callable):
        """
        Callback to be called when blockchain adds new peak
        """
        self.new_peak_callbacks[wallet_id] = callback

    async def puzzle_hash_created(self, coin: Coin):
        callback = self.puzzle_hash_created_callbacks[coin.puzzle_hash]
        if callback is None:
            return None
        await callback(coin)

    def state_changed(self, state: str, wallet_id: int = None, data_object=None):
        """
        Calls the callback if it's present.
        """
        if data_object is None:
            data_object = {}
        if self.state_changed_callback is None:
            return None
        self.state_changed_callback(state, wallet_id, data_object)

    def tx_pending_changed(self) -> None:
        """
        Notifies the wallet node that there's new tx pending
        """
        if self.pending_tx_callback is None:
            return None

        self.pending_tx_callback()

    async def synced(self):
        if self.sync_mode is True:
            return False
        peak: Optional[BlockRecord] = self.blockchain.get_peak()
        if peak is None:
            return False

        curr = peak
        while not curr.is_transaction_block and not curr.height == 0:
            curr = self.blockchain.try_block_record(curr.prev_hash)
            if curr is None:
                return False
        if curr.is_transaction_block and curr.timestamp > int(time.time()) - 7 * 60:
            return True
        return False

    def set_sync_mode(self, mode: bool):
        """
        Sets the sync mode. This changes the behavior of the wallet node.
        """
        self.sync_mode = mode
        self.state_changed("sync_changed")

    async def get_confirmed_spendable_balance_for_wallet(self, wallet_id: int, unspent_records=None) -> uint128:
        """
        Returns the balance amount of all coins that are spendable.
        """

        spendable: Set[WalletCoinRecord] = await self.get_spendable_coins_for_wallet(wallet_id, unspent_records)

        spendable_amount: uint128 = uint128(0)
        for record in spendable:
            spendable_amount = uint128(spendable_amount + record.coin.amount)

        return spendable_amount

    async def does_coin_belong_to_wallet(self, coin: Coin, wallet_id: int) -> bool:
        """
        Returns true if we have the key for this coin.
        """
        info = await self.puzzle_store.wallet_info_for_puzzle_hash(coin.puzzle_hash)

        if info is None:
            return False

        coin_wallet_id, wallet_type = info
        if wallet_id == coin_wallet_id:
            return True

        return False

    async def get_confirmed_balance_for_wallet_already_locked(self, wallet_id: int) -> uint128:
        # This is a workaround to be able to call la locking operation when already locked
        # for example, in the create method of DID wallet
        if self.lock.locked() is False:
            raise AssertionError("expected wallet_state_manager to be locked")
        unspent_coin_records = await self.coin_store.get_unspent_coins_for_wallet(wallet_id)
        return get_balance_from_coin_records(unspent_coin_records)

    async def get_confirmed_balance_for_wallet(
        self,
        wallet_id: int,
        unspent_coin_records: Optional[Set[WalletCoinRecord]] = None,
    ) -> uint128:
        """
        Returns the confirmed balance, including coinbase rewards that are not spendable.
        """
        # lock only if unspent_coin_records is None.
        # This API should change so that get_balance_from_coin_records is called for Set[WalletCoinRecord]
        # and this method is called only for the unspent_coin_records==None case.
        if unspent_coin_records is None:
            unspent_coin_records = await self.get_confirmed_balance_for_wallet_with_lock(wallet_id)
        return get_balance_from_coin_records(unspent_coin_records)

    async def get_confirmed_balance_for_wallet_with_lock(self, wallet_id: int) -> Set[WalletCoinRecord]:
        if self.lock.locked() is True:
            # raise AssertionError("expected wallet_state_manager to be unlocked")
            pass
        async with self.lock:
            return await self.coin_store.get_unspent_coins_for_wallet(wallet_id)

    async def get_unconfirmed_balance(
        self, wallet_id, unspent_coin_records: Optional[Set[WalletCoinRecord]] = None
    ) -> uint128:
        """
        Returns the balance, including coinbase rewards that are not spendable, and unconfirmed
        transactions.
        """
        # This API should change so that get_balance_from_coin_records is called for Set[WalletCoinRecord]
        # and this method is called only for the unspent_coin_records==None case.
        confirmed_amount = await self.get_confirmed_balance_for_wallet(wallet_id, unspent_coin_records)
        return await self._get_unconfirmed_balance(wallet_id, confirmed_amount)

    async def get_unconfirmed_balance_already_locked(self, wallet_id) -> uint128:
        confirmed_amount = await self.get_confirmed_balance_for_wallet_already_locked(wallet_id)
        return await self._get_unconfirmed_balance(wallet_id, confirmed_amount)

    async def _get_unconfirmed_balance(self, wallet_id, confirmed: uint128) -> uint128:
        unconfirmed_tx: List[TransactionRecord] = await self.tx_store.get_unconfirmed_for_wallet(wallet_id)
        removal_amount: int = 0
        addition_amount: int = 0

        for record in unconfirmed_tx:
            for removal in record.removals:
                if await self.does_coin_belong_to_wallet(removal, wallet_id):
                    removal_amount += removal.amount
            for addition in record.additions:
                # This change or a self transaction
                if await self.does_coin_belong_to_wallet(addition, wallet_id):
                    addition_amount += addition.amount

        result = (confirmed + addition_amount) - removal_amount
        return uint128(result)

    async def unconfirmed_additions_for_wallet(self, wallet_id: int) -> Dict[bytes32, Coin]:
        """
        Returns change coins for the wallet_id.
        (Unconfirmed addition transactions that have not been confirmed yet.)
        """
        additions: Dict[bytes32, Coin] = {}
        unconfirmed_tx = await self.tx_store.get_unconfirmed_for_wallet(wallet_id)
        for record in unconfirmed_tx:
            for coin in record.additions:
                if await self.is_addition_relevant(coin):
                    additions[coin.name()] = coin
        return additions

    async def unconfirmed_removals_for_wallet(self, wallet_id: int) -> Dict[bytes32, Coin]:
        """
        Returns new removals transactions that have not been confirmed yet.
        """
        removals: Dict[bytes32, Coin] = {}
        unconfirmed_tx = await self.tx_store.get_unconfirmed_for_wallet(wallet_id)
        for record in unconfirmed_tx:
            for coin in record.removals:
                removals[coin.name()] = coin
        return removals

    async def new_transaction_block_callback(
        self,
        removals: List[Coin],
        additions: List[Coin],
        block: BlockRecord,
        additional_coin_spends: List[CoinSpend],
    ):
        height: uint32 = block.height
        for coin in additions:
            await self.puzzle_hash_created(coin)
        trade_additions, added = await self.coins_of_interest_added(additions, block)
        trade_removals, removed = await self.coins_of_interest_removed(removals, height)
        if len(trade_additions) > 0 or len(trade_removals) > 0:
            await self.trade_manager.coins_of_interest_farmed(trade_removals, trade_additions, height)

        if len(additional_coin_spends) > 0:
            created_pool_wallet_ids: List[int] = []
            for cs in additional_coin_spends:
                if cs.coin.puzzle_hash == SINGLETON_LAUNCHER_HASH:
                    already_have = False
                    pool_state = None
                    for wallet_id, wallet in self.wallets.items():
                        if (
                            wallet.type() == WalletType.POOLING_WALLET
                            and (await wallet.get_current_state()).launcher_id == cs.coin.name()
                        ):
                            self.log.warning("Already have, not recreating")
                            already_have = True
                    if not already_have:
                        try:
                            pool_state = solution_to_pool_state(cs)
                        except Exception as e:
                            self.log.debug(f"Not a pool wallet launcher {e}")
                            continue
                        if pool_state is None:
                            self.log.debug("Not a pool wallet launcher")
                            continue
                        self.log.info("Found created launcher. Creating pool wallet")
                        pool_wallet = await PoolWallet.create(
                            self, self.main_wallet, cs.coin.name(), additional_coin_spends, height, True, "pool_wallet"
                        )
                        created_pool_wallet_ids.append(pool_wallet.wallet_id)

            for wallet_id, wallet in self.wallets.items():
                if wallet.type() == WalletType.POOLING_WALLET:
                    await wallet.apply_state_transitions(additional_coin_spends, height)

        added_notified = set()
        removed_notified = set()
        for coin_record in added:
            if coin_record.wallet_id in added_notified:
                continue
            added_notified.add(coin_record.wallet_id)
            self.state_changed("coin_added", coin_record.wallet_id)
        for coin_record in removed:
            if coin_record.wallet_id in removed_notified:
                continue
            removed_notified.add(coin_record.wallet_id)
            self.state_changed("coin_removed", coin_record.wallet_id)

        self.tx_pending_changed()

    async def coins_of_interest_added(
        self, coins: List[Coin], block: BlockRecord
    ) -> Tuple[List[Coin], List[WalletCoinRecord]]:
        (
            trade_removals,
            trade_additions,
        ) = await self.trade_manager.get_coins_of_interest()
        trade_adds: List[Coin] = []
        height = block.height

        pool_rewards = set()
        farmer_rewards = set()
        added = []

        prev = await self.blockchain.get_block_record_from_db(block.prev_hash)
        # [block 1] [block 2] [tx block 3] [block 4] [block 5] [tx block 6]
        # [tx block 6] will contain rewards for [block 1] [block 2] [tx block 3]
        while prev is not None:
            # step 1 find previous block
            if prev.is_transaction_block:
                break
            prev = await self.blockchain.get_block_record_from_db(prev.prev_hash)

        if prev is not None:
            # include last block
            pool_parent = pool_parent_id(uint32(prev.height), self.constants.GENESIS_CHALLENGE)
            farmer_parent = farmer_parent_id(uint32(prev.height), self.constants.GENESIS_CHALLENGE)
            pool_rewards.add(pool_parent)
            farmer_rewards.add(farmer_parent)
            prev = await self.blockchain.get_block_record_from_db(prev.prev_hash)

        while prev is not None:
            # step 2 traverse from previous block to the block before it
            pool_parent = pool_parent_id(uint32(prev.height), self.constants.GENESIS_CHALLENGE)
            farmer_parent = farmer_parent_id(uint32(prev.height), self.constants.GENESIS_CHALLENGE)
            pool_rewards.add(pool_parent)
            farmer_rewards.add(farmer_parent)
            if prev.is_transaction_block:
                break
            prev = await self.blockchain.get_block_record_from_db(prev.prev_hash)
        wallet_ids: Set[int] = set()
        for coin in coins:
            info = await self.puzzle_store.wallet_info_for_puzzle_hash(coin.puzzle_hash)
            if info is not None:
                wallet_ids.add(info[0])

        all_outgoing_tx: Dict[int, List[TransactionRecord]] = {}
        for wallet_id in wallet_ids:
            all_outgoing_tx[wallet_id] = await self.tx_store.get_all_transactions_for_wallet(
                wallet_id, TransactionType.OUTGOING_TX
            )

        for coin in coins:
            if coin.name() in trade_additions:
                trade_adds.append(coin)

            is_coinbase = False
            is_fee_reward = False
            if coin.parent_coin_info in pool_rewards:
                is_coinbase = True
            if coin.parent_coin_info in farmer_rewards:
                is_fee_reward = True

            info = await self.puzzle_store.wallet_info_for_puzzle_hash(coin.puzzle_hash)
            if info is not None:
                wallet_id, wallet_type = info
                added_coin_record = await self.coin_added(
                    coin,
                    is_coinbase,
                    is_fee_reward,
                    uint32(wallet_id),
                    wallet_type,
                    height,
                    all_outgoing_tx.get(wallet_id, []),
                )
                added.append(added_coin_record)
            else:
                interested_wallet_id = await self.interested_store.get_interested_puzzle_hash_wallet_id(
                    puzzle_hash=coin.puzzle_hash
                )
                if interested_wallet_id is not None:
                    wallet_type = self.wallets[uint32(interested_wallet_id)].type()
                    added_coin_record = await self.coin_added(
                        coin,
                        is_coinbase,
                        is_fee_reward,
                        uint32(interested_wallet_id),
                        wallet_type,
                        height,
                        all_outgoing_tx.get(interested_wallet_id, []),
                    )
                    added.append(added_coin_record)

            derivation_index = await self.puzzle_store.index_for_puzzle_hash(coin.puzzle_hash)
            if derivation_index is not None:
                await self.puzzle_store.set_used_up_to(derivation_index, True)

        return trade_adds, added

    async def coins_of_interest_removed(
        self, coins: List[Coin], height: uint32
    ) -> Tuple[List[Coin], List[WalletCoinRecord]]:
        # This gets called when coins of our interest are spent on chain
        if len(coins) > 0:
            self.log.info(f"Coins removed {coins} at height: {height}")
        (
            trade_removals,
            trade_additions,
        ) = await self.trade_manager.get_coins_of_interest()

        # Keep track of trade coins that are removed
        trade_coin_removed: List[Coin] = []
        removed = []
        all_unconfirmed: List[TransactionRecord] = await self.tx_store.get_all_unconfirmed()
        for coin in coins:
            record = await self.coin_store.get_coin_record(coin.name())
            if coin.name() in trade_removals:
                trade_coin_removed.append(coin)
            if record is None:
                self.log.info(f"Record for removed coin {coin.name()} is None. (ephemeral)")
            else:
                await self.coin_store.set_spent(coin.name(), height)
            for unconfirmed_record in all_unconfirmed:
                for rem_coin in unconfirmed_record.removals:
                    if rem_coin.name() == coin.name():
                        self.log.info(f"Setting tx_id: {unconfirmed_record.name} to confirmed")
                        await self.tx_store.set_confirmed(unconfirmed_record.name, height)
            if record is not None:
                removed.append(record)

        return trade_coin_removed, removed

    async def coin_added(
        self,
        coin: Coin,
        coinbase: bool,
        fee_reward: bool,
        wallet_id: uint32,
        wallet_type: WalletType,
        height: uint32,
        all_outgoing_transaction_records: List[TransactionRecord],
    ) -> WalletCoinRecord:
        """
        Adding coin to DB
        """
        self.log.info(f"Adding coin: {coin} at {height}")
        farm_reward = False
        if coinbase or fee_reward:
            farm_reward = True
            now = uint64(int(time.time()))
            if coinbase:
                tx_type: int = TransactionType.COINBASE_REWARD.value
            else:
                tx_type = TransactionType.FEE_REWARD.value
            tx_record = TransactionRecord(
                confirmed_at_height=uint32(height),
                created_at_time=now,
                to_puzzle_hash=coin.puzzle_hash,
                amount=coin.amount,
                fee_amount=uint64(0),
                confirmed=True,
                sent=uint32(0),
                spend_bundle=None,
                additions=[coin],
                removals=[],
                wallet_id=wallet_id,
                sent_to=[],
                trade_id=None,
                type=uint32(tx_type),
                name=coin.name(),
            )
            await self.tx_store.add_transaction_record(tx_record, True)
        else:
            records: List[TransactionRecord] = []
            for record in all_outgoing_transaction_records:
                for add_coin in record.additions:
                    if add_coin.name() == coin.name():
                        records.append(record)

            if len(records) > 0:
                # This is the change from this transaction
                for record in records:
                    if record.confirmed is False:
                        await self.tx_store.set_confirmed(record.name, height)
            else:
                now = uint64(int(time.time()))
                tx_record = TransactionRecord(
                    confirmed_at_height=uint32(height),
                    created_at_time=now,
                    to_puzzle_hash=coin.puzzle_hash,
                    amount=coin.amount,
                    fee_amount=uint64(0),
                    confirmed=True,
                    sent=uint32(0),
                    spend_bundle=None,
                    additions=[coin],
                    removals=[],
                    wallet_id=wallet_id,
                    sent_to=[],
                    trade_id=None,
                    type=uint32(TransactionType.INCOMING_TX.value),
                    name=coin.name(),
                )
                if coin.amount > 0:
                    await self.tx_store.add_transaction_record(tx_record, True)

        coin_record: WalletCoinRecord = WalletCoinRecord(
            coin, height, uint32(0), False, farm_reward, wallet_type, wallet_id
        )
        await self.coin_store.add_coin_record(coin_record)

        if wallet_type == WalletType.COLOURED_COIN or wallet_type == WalletType.DISTRIBUTED_ID:
            wallet = self.wallets[wallet_id]
            await wallet.coin_added(coin, height)

        return coin_record

    async def add_pending_transaction(self, tx_record: TransactionRecord):
        """
        Called from wallet before new transaction is sent to the full_node
        """
        # Wallet node will use this queue to retry sending this transaction until full nodes receives it
        await self.tx_store.add_transaction_record(tx_record, False)
        self.tx_pending_changed()
        self.state_changed("pending_transaction", tx_record.wallet_id)

    async def add_transaction(self, tx_record: TransactionRecord):
        """
        Called from wallet to add transaction that is not being set to full_node
        """
        await self.tx_store.add_transaction_record(tx_record, False)
        self.state_changed("pending_transaction", tx_record.wallet_id)

    async def remove_from_queue(
        self,
        spendbundle_id: bytes32,
        name: str,
        send_status: MempoolInclusionStatus,
        error: Optional[Err],
    ):
        """
        Full node received our transaction, no need to keep it in queue anymore
        """
        updated = await self.tx_store.increment_sent(spendbundle_id, name, send_status, error)
        if updated:
            tx: Optional[TransactionRecord] = await self.get_transaction(spendbundle_id)
            if tx is not None:
                self.state_changed("tx_update", tx.wallet_id, {"transaction": tx})

    async def get_all_transactions(self, wallet_id: int) -> List[TransactionRecord]:
        """
        Retrieves all confirmed and pending transactions
        """
        records = await self.tx_store.get_all_transactions_for_wallet(wallet_id)
        return records

    async def get_transaction(self, tx_id: bytes32) -> Optional[TransactionRecord]:
        return await self.tx_store.get_transaction_record(tx_id)

    async def get_filter_additions_removals(
        self, new_block: HeaderBlock, transactions_filter: bytes, fork_point_with_peak: Optional[uint32]
    ) -> Tuple[List[bytes32], List[bytes32]]:
        """Returns a list of our coin ids, and a list of puzzle_hashes that positively match with provided filter."""
        # assert new_block.prev_header_hash in self.blockchain.blocks

        tx_filter = PyBIP158([b for b in transactions_filter])

        # Find fork point
        if fork_point_with_peak is not None:
            fork_h: int = fork_point_with_peak
        elif new_block.prev_header_hash != self.constants.GENESIS_CHALLENGE and self.peak is not None:
            block_record = await self.blockchain.get_block_record_from_db(self.peak.header_hash)
            # this may return -1, in case there is no shared ancestor block
            fork_h = find_fork_point_in_chain(
                self.blockchain,
                block_record,
                new_block,
            )
        else:
            fork_h = 0

        # Get all unspent coins
        my_coin_records: Set[WalletCoinRecord] = await self.coin_store.get_unspent_coins_at_height(
            uint32(fork_h) if fork_h >= 0 else None
        )

        # Filter coins up to and including fork point
        unspent_coin_names: Set[bytes32] = set()
        for coin in my_coin_records:
            if coin.confirmed_block_height <= fork_h:
                unspent_coin_names.add(coin.name())

        # Get all blocks after fork point up to but not including this block
        if new_block.height > 0:
            curr: BlockRecord = self.blockchain.block_record(new_block.prev_hash)
            reorg_blocks: List[HeaderBlockRecord] = []
            while curr.height > fork_h:
                header_block_record = await self.block_store.get_header_block_record(curr.header_hash)
                assert header_block_record is not None
                reorg_blocks.append(header_block_record)
                if curr.height == 0:
                    break
                curr = await self.blockchain.get_block_record_from_db(curr.prev_hash)
            reorg_blocks.reverse()

            # For each block, process additions to get all Coins, then process removals to get unspent coins
            for reorg_block in reorg_blocks:
                for addition in reorg_block.additions:
                    unspent_coin_names.add(addition.name())
                for removal in reorg_block.removals:
                    record = await self.puzzle_store.get_derivation_record_for_puzzle_hash(removal.puzzle_hash)
                    if record is None:
                        continue
                    unspent_coin_names.remove(removal)

        my_puzzle_hashes = self.puzzle_store.all_puzzle_hashes

        removals_of_interest: bytes32 = []
        additions_of_interest: bytes32 = []

        (
            trade_removals,
            trade_additions,
        ) = await self.trade_manager.get_coins_of_interest()
        for name, trade_coin in trade_removals.items():
            if tx_filter.Match(bytearray(trade_coin.name())):
                removals_of_interest.append(trade_coin.name())

        for name, trade_coin in trade_additions.items():
            if tx_filter.Match(bytearray(trade_coin.puzzle_hash)):
                additions_of_interest.append(trade_coin.puzzle_hash)

        for coin_name in unspent_coin_names:
            if tx_filter.Match(bytearray(coin_name)):
                removals_of_interest.append(coin_name)

        for puzzle_hash in my_puzzle_hashes:
            if tx_filter.Match(bytearray(puzzle_hash)):
                additions_of_interest.append(puzzle_hash)

        for coin_id in await self.interested_store.get_interested_coin_ids():
            if tx_filter.Match(bytearray(coin_id)):
                removals_of_interest.append(coin_id)

        for puzzle_hash, _ in await self.interested_store.get_interested_puzzle_hashes():
            if tx_filter.Match(bytearray(puzzle_hash)):
                additions_of_interest.append(puzzle_hash)

        return additions_of_interest, removals_of_interest

    async def is_addition_relevant(self, addition: Coin):
        """
        Check whether we care about a new addition (puzzle_hash). Returns true if we
        control this puzzle hash.
        """
        result = await self.puzzle_store.puzzle_hash_exists(addition.puzzle_hash)
        return result

    async def get_wallet_for_coin(self, coin_id: bytes32) -> Any:
        coin_record = await self.coin_store.get_coin_record(coin_id)
        if coin_record is None:
            return None
        wallet_id = uint32(coin_record.wallet_id)
        wallet = self.wallets[wallet_id]
        return wallet

    async def reorg_rollback(self, height: int):
        """
        Rolls back and updates the coin_store and transaction store. It's possible this height
        is the tip, or even beyond the tip.
        """
        await self.coin_store.rollback_to_block(height)

        reorged: List[TransactionRecord] = await self.tx_store.get_transaction_above(height)
        await self.tx_store.rollback_to_block(height)

        for record in reorged:
            if record.type in [
                TransactionType.OUTGOING_TX,
                TransactionType.OUTGOING_TRADE,
                TransactionType.INCOMING_TRADE,
            ]:
                await self.tx_store.tx_reorged(record)

        # Removes wallets that were created from a blockchain transaction which got reorged.
        remove_ids = []
        for wallet_id, wallet in self.wallets.items():
            if wallet.type() == WalletType.POOLING_WALLET.value:
                remove: bool = await wallet.rewind(height)
                if remove:
                    remove_ids.append(wallet_id)
        for wallet_id in remove_ids:
            await self.user_store.delete_wallet(wallet_id, in_transaction=True)
            self.wallets.pop(wallet_id)
            self.new_peak_callbacks.pop(wallet_id)

    async def close_all_stores(self) -> None:
        if self.blockchain is not None:
            self.blockchain.shut_down()
        await self.db_connection.close()

    async def clear_all_stores(self):
        await self.coin_store._clear_database()
        await self.tx_store._clear_database()
        await self.puzzle_store._clear_database()
        await self.user_store._clear_database()
        await self.basic_store._clear_database()

    def unlink_db(self):
        Path(self.db_path).unlink()

    async def get_all_wallet_info_entries(self) -> List[WalletInfo]:
        return await self.user_store.get_all_wallet_info_entries()

    async def get_start_height(self):
        """
        If we have coin use that as starting height next time,
        otherwise use the peak
        """

        first_coin_height = await self.coin_store.get_first_coin_height()
        if first_coin_height is None:
            start_height = self.blockchain.get_peak()
        else:
            start_height = first_coin_height

        return start_height

    async def create_wallet_backup(self, file_path: Path):
        all_wallets = await self.get_all_wallet_info_entries()
        for wallet in all_wallets:
            if wallet.id == 1:
                all_wallets.remove(wallet)
                break

        backup_pk = master_sk_to_backup_sk(self.private_key)
        now = uint64(int(time.time()))
        wallet_backup = WalletInfoBackup(all_wallets)

        backup: Dict[str, Any] = {}

        data = wallet_backup.to_json_dict()
        data["version"] = __version__
        data["fingerprint"] = self.private_key.get_g1().get_fingerprint()
        data["timestamp"] = now
        data["start_height"] = await self.get_start_height()
        key_base_64 = base64.b64encode(bytes(backup_pk))
        f = Fernet(key_base_64)
        data_bytes = json.dumps(data).encode()
        encrypted = f.encrypt(data_bytes)

        meta_data: Dict[str, Any] = {"timestamp": now, "pubkey": bytes(backup_pk.get_g1()).hex()}

        meta_data_bytes = json.dumps(meta_data).encode()
        signature = bytes(AugSchemeMPL.sign(backup_pk, std_hash(encrypted) + std_hash(meta_data_bytes))).hex()

        backup["data"] = encrypted.decode()
        backup["meta_data"] = meta_data
        backup["signature"] = signature

        backup_file_text = json.dumps(backup)
        file_path.write_text(backup_file_text)

    async def import_backup_info(self, file_path) -> None:
        json_dict = open_backup_file(file_path, self.private_key)
        wallet_list_json = json_dict["data"]["wallet_list"]

        for wallet_info in wallet_list_json:
            await self.user_store.create_wallet(
                wallet_info["name"],
                wallet_info["type"],
                wallet_info["data"],
                wallet_info["id"],
            )
        await self.load_wallets()
        await self.user_settings.user_imported_backup()
        await self.create_more_puzzle_hashes(from_zero=True)

    async def get_wallet_for_colour(self, colour):
        for wallet_id in self.wallets:
            wallet = self.wallets[wallet_id]
            if wallet.type() == WalletType.COLOURED_COIN:
                if bytes(wallet.cc_info.my_genesis_checker).hex() == colour:
                    return wallet
        return None

    async def add_new_wallet(self, wallet: Any, wallet_id: int, create_puzzle_hashes=True):
        self.wallets[uint32(wallet_id)] = wallet
        if create_puzzle_hashes:
            await self.create_more_puzzle_hashes()

    # search through the blockrecords and return the most recent coin to use a given puzzlehash
    async def search_blockrecords_for_puzzlehash(self, puzzlehash: bytes32):
        header_hash_of_interest = None
        highest_block_height = 0
        peak: Optional[BlockRecord] = self.blockchain.get_peak()
        if peak is None:
            return None, None
        peak_block: Optional[HeaderBlockRecord] = await self.blockchain.block_store.get_header_block_record(
            peak.header_hash
        )
        while peak_block is not None:
            tx_filter = PyBIP158([b for b in peak_block.header.transactions_filter])
            if tx_filter.Match(bytearray(puzzlehash)) and peak_block.height > highest_block_height:
                header_hash_of_interest = peak_block.header_hash
                highest_block_height = peak_block.height
                break
            else:
                peak_block = await self.blockchain.block_store.get_header_block_record(
                    peak_block.header.prev_header_hash
                )

        return highest_block_height, header_hash_of_interest

    async def get_spendable_coins_for_wallet(self, wallet_id: int, records=None) -> Set[WalletCoinRecord]:
        if self.peak is None:
            return set()

        if records is None:
            records = await self.coin_store.get_unspent_coins_for_wallet(wallet_id)

        # Coins that are currently part of a transaction
        unconfirmed_tx: List[TransactionRecord] = await self.tx_store.get_unconfirmed_for_wallet(wallet_id)
        removal_dict: Dict[bytes32, Coin] = {}
        for tx in unconfirmed_tx:
            for coin in tx.removals:
                # TODO, "if" might not be necessary once unconfirmed tx doesn't contain coins for other wallets
                if await self.does_coin_belong_to_wallet(coin, wallet_id):
                    removal_dict[coin.name()] = coin

        # Coins that are part of the trade
        offer_locked_coins: Dict[bytes32, WalletCoinRecord] = await self.trade_manager.get_locked_coins()

        filtered = set()
        for record in records:
            if record.coin.name() in offer_locked_coins:
                continue
            if record.coin.name() in removal_dict:
                continue
            filtered.add(record)

        return filtered

    async def create_action(
        self, name: str, wallet_id: int, wallet_type: int, callback: str, done: bool, data: str, in_transaction: bool
    ):
        await self.action_store.create_action(name, wallet_id, wallet_type, callback, done, data, in_transaction)
        self.tx_pending_changed()

    async def set_action_done(self, action_id: int):
        await self.action_store.action_done(action_id)

    async def generator_received(self, height: uint32, header_hash: uint32, program: Program):

        actions: List[WalletAction] = await self.action_store.get_all_pending_actions()
        for action in actions:
            data = json.loads(action.data)
            action_data = data["data"]["action_data"]
            if action.name == "request_generator":
                stored_header_hash = bytes32(hexstr_to_bytes(action_data["header_hash"]))
                stored_height = uint32(action_data["height"])
                if stored_header_hash == header_hash and stored_height == height:
                    if action.done:
                        return None
                    wallet = self.wallets[uint32(action.wallet_id)]
                    callback_str = action.wallet_callback
                    if callback_str is not None:
                        callback = getattr(wallet, callback_str)
                        await callback(height, header_hash, program, action.id)

    async def puzzle_solution_received(self, response: RespondPuzzleSolution):
        unwrapped: PuzzleSolutionResponse = response.response
        actions: List[WalletAction] = await self.action_store.get_all_pending_actions()
        for action in actions:
            data = json.loads(action.data)
            action_data = data["data"]["action_data"]
            if action.name == "request_puzzle_solution":
                stored_coin_name = bytes32(hexstr_to_bytes(action_data["coin_name"]))
                height = uint32(action_data["height"])
                if stored_coin_name == unwrapped.coin_name and height == unwrapped.height:
                    if action.done:
                        return None
                    wallet = self.wallets[uint32(action.wallet_id)]
                    callback_str = action.wallet_callback
                    if callback_str is not None:
                        callback = getattr(wallet, callback_str)
                        await callback(unwrapped, action.id)

    def get_peak(self) -> Optional[BlockRecord]:
        return self.blockchain.get_peak()

    async def get_next_interesting_coin_ids(self, spend: CoinSpend, in_transaction: bool) -> List[bytes32]:
        pool_wallet_interested: List[bytes32] = PoolWallet.get_next_interesting_coin_ids(spend)
        for coin_id in pool_wallet_interested:
            await self.interested_store.add_interested_coin_id(coin_id, in_transaction)
        return pool_wallet_interested

    async def new_peak(self):
        peak: Optional[BlockRecord] = self.get_peak()
        if peak is None:
            return

        for wallet_id, callback in self.new_peak_callbacks.items():
            await callback(peak)
