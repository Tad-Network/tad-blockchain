# flake8: noqa: F811, F401
import asyncio
import dataclasses
import logging
import random
import time
from typing import Dict

import pytest
import cProfile

from tad.consensus.block_record import BlockRecord
from tad.full_node.full_node_api import FullNodeAPI
from tad.protocols import full_node_protocol as fnp
from tad.types.condition_opcodes import ConditionOpcode
from tad.types.condition_with_args import ConditionWithArgs
from tad.types.unfinished_block import UnfinishedBlock
from tad.util.clvm import int_to_bytes
from tad.util.ints import uint64
from tests.wallet_tools import WalletTool
from tests.core.fixtures import empty_blockchain  # noqa: F401

from tests.connection_utils import add_dummy_connection, connect_and_get_peer
from tests.core.full_node.test_coin_store import get_future_reward_coins
from tests.core.node_height import node_height_at_least
from tests.core.fixtures import empty_blockchain  # noqa: F401
from tests.setup_nodes import bt, setup_simulators_and_wallets, test_constants
from tests.time_out_assert import time_out_assert, time_out_assert_custom_interval, time_out_messages

log = logging.getLogger(__name__)


async def get_block_path(full_node: FullNodeAPI):
    blocks_list = [await full_node.full_node.blockchain.get_full_peak()]
    assert blocks_list[0] is not None
    while blocks_list[0].height != 0:
        b = await full_node.full_node.block_store.get_full_block(blocks_list[0].prev_header_hash)
        assert b is not None
        blocks_list.insert(0, b)
    return blocks_list


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.get_event_loop()
    yield loop


@pytest.fixture(scope="module")
async def wallet_nodes():
    async_gen = setup_simulators_and_wallets(1, 1, {"MEMPOOL_BLOCK_BUFFER": 1, "MAX_BLOCK_COST_CLVM": 11000000000})
    nodes, wallets = await async_gen.__anext__()
    full_node_1 = nodes[0]
    server_1 = full_node_1.full_node.server
    wallet_a = bt.get_pool_wallet_tool()
    wallet_receiver = WalletTool(full_node_1.full_node.constants)
    yield full_node_1, server_1, wallet_a, wallet_receiver

    async for _ in async_gen:
        yield _


class TestPerformance:
    @pytest.mark.asyncio
    async def test_full_block_performance(self, wallet_nodes):
        full_node_1, server_1, wallet_a, wallet_receiver = wallet_nodes
        blocks = await full_node_1.get_all_full_blocks()
        full_node_1.full_node.mempool_manager.limit_factor = 1

        wallet_ph = wallet_a.get_new_puzzlehash()
        blocks = bt.get_consecutive_blocks(
            10,
            block_list_input=blocks,
            guarantee_transaction_block=True,
            farmer_reward_puzzle_hash=wallet_ph,
            pool_reward_puzzle_hash=wallet_ph,
        )
        for block in blocks:
            await full_node_1.full_node.respond_block(fnp.RespondBlock(block))

        start_height = (
            full_node_1.full_node.blockchain.get_peak().height
            if full_node_1.full_node.blockchain.get_peak() is not None
            else -1
        )
        incoming_queue, node_id = await add_dummy_connection(server_1, 12312)
        fake_peer = server_1.all_connections[node_id]
        # Mempool has capacity of 100, make 110 unspents that we can use
        puzzle_hashes = []

        # Makes a bunch of coins
        for i in range(20):
            conditions_dict: Dict = {ConditionOpcode.CREATE_COIN: []}
            # This should fit in one transaction
            for _ in range(100):
                receiver_puzzlehash = wallet_receiver.get_new_puzzlehash()
                puzzle_hashes.append(receiver_puzzlehash)
                output = ConditionWithArgs(ConditionOpcode.CREATE_COIN, [receiver_puzzlehash, int_to_bytes(100000000)])

                conditions_dict[ConditionOpcode.CREATE_COIN].append(output)

            spend_bundle = wallet_a.generate_signed_transaction(
                100,
                puzzle_hashes[0],
                get_future_reward_coins(blocks[1 + i])[0],
                condition_dic=conditions_dict,
            )
            assert spend_bundle is not None

            respond_transaction_2 = fnp.RespondTransaction(spend_bundle)
            await full_node_1.respond_transaction(respond_transaction_2, fake_peer)

            blocks = bt.get_consecutive_blocks(
                1,
                block_list_input=blocks,
                guarantee_transaction_block=True,
                transaction_data=spend_bundle,
            )
            await full_node_1.full_node.respond_block(fnp.RespondBlock(blocks[-1]), fake_peer)

        await time_out_assert(10, node_height_at_least, True, full_node_1, start_height + 20)

        spend_bundles = []
        spend_bundle_ids = []

        # Fill mempool
        for puzzle_hash in puzzle_hashes[1:]:
            coin_record = (await full_node_1.full_node.coin_store.get_coin_records_by_puzzle_hash(True, puzzle_hash))[0]
            receiver_puzzlehash = wallet_receiver.get_new_puzzlehash()
            if puzzle_hash == puzzle_hashes[-1]:
                fee = 100000000  # 100 million (20 fee per cost)
            else:
                fee = random.randint(1, 100000000)
            spend_bundle = wallet_receiver.generate_signed_transaction(
                uint64(500), receiver_puzzlehash, coin_record.coin, fee=fee
            )
            spend_bundles.append(spend_bundle)
            spend_bundle_ids.append(spend_bundle.get_hash())

        pr = cProfile.Profile()
        pr.enable()

        start = time.time()
        num_tx: int = 0
        for spend_bundle, spend_bundle_id in zip(spend_bundles, spend_bundle_ids):
            num_tx += 1
            respond_transaction = fnp.RespondTransaction(spend_bundle)

            await full_node_1.respond_transaction(respond_transaction, fake_peer)

            request = fnp.RequestTransaction(spend_bundle_id)
            req = await full_node_1.request_transaction(request)

            if req is None:
                break
        log.warning(f"Num Tx: {num_tx}")
        log.warning(f"Time for mempool: {time.time() - start}")
        pr.create_stats()
        pr.dump_stats("./mempool-benchmark.pstats")

        # Create an unfinished block
        peak = full_node_1.full_node.blockchain.get_peak()
        assert peak is not None
        curr: BlockRecord = peak
        while not curr.is_transaction_block:
            curr = full_node_1.full_node.blockchain.block_record(curr.prev_hash)
        mempool_bundle = await full_node_1.full_node.mempool_manager.create_bundle_from_mempool(curr.header_hash)
        if mempool_bundle is None:
            spend_bundle = None
        else:
            spend_bundle = mempool_bundle[0]

        current_blocks = await full_node_1.get_all_full_blocks()
        blocks = bt.get_consecutive_blocks(
            1,
            transaction_data=spend_bundle,
            block_list_input=current_blocks,
            guarantee_transaction_block=True,
        )
        block = blocks[-1]
        unfinished = UnfinishedBlock(
            block.finished_sub_slots,
            block.reward_chain_block.get_unfinished(),
            block.challenge_chain_sp_proof,
            block.reward_chain_sp_proof,
            block.foliage,
            block.foliage_transaction_block,
            block.transactions_info,
            block.transactions_generator,
            [],
        )

        pr = cProfile.Profile()
        pr.enable()

        start = time.time()
        res = await full_node_1.respond_unfinished_block(fnp.RespondUnfinishedBlock(unfinished), fake_peer)
        log.warning(f"Res: {res}")
        log.warning(f"Time for unfinished: {time.time() - start}")

        pr.create_stats()
        pr.dump_stats("./unfinished-benchmark.pstats")

        pr = cProfile.Profile()
        pr.enable()

        start = time.time()
        # No transactions generator, the full node already cached it from the unfinished block
        block_small = dataclasses.replace(block, transactions_generator=None)
        res = await full_node_1.full_node.respond_block(fnp.RespondBlock(block_small))
        log.warning(f"Res: {res}")
        log.warning(f"Time for full block: {time.time() - start}")

        pr.create_stats()
        pr.dump_stats("./full-block-benchmark.pstats")
