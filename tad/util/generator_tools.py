from typing import List, Tuple
from chiabip158 import PyBIP158

from tad.types.blockchain_format.coin import Coin
from tad.types.blockchain_format.sized_bytes import bytes32
from tad.types.full_block import FullBlock
from tad.types.header_block import HeaderBlock
from tad.types.name_puzzle_condition import NPC
from tad.util.condition_tools import created_outputs_for_conditions_dict


def get_block_header(block: FullBlock, tx_addition_coins: List[Coin], removals_names: List[bytes32]) -> HeaderBlock:
    # Create filter
    byte_array_tx: List[bytes32] = []
    addition_coins = tx_addition_coins + list(block.get_included_reward_coins())
    if block.is_transaction_block():
        for coin in addition_coins:
            byte_array_tx.append(bytearray(coin.puzzle_hash))
        for name in removals_names:
            byte_array_tx.append(bytearray(name))

    bip158: PyBIP158 = PyBIP158(byte_array_tx)
    encoded_filter: bytes = bytes(bip158.GetEncoded())

    return HeaderBlock(
        block.finished_sub_slots,
        block.reward_chain_block,
        block.challenge_chain_sp_proof,
        block.challenge_chain_ip_proof,
        block.reward_chain_sp_proof,
        block.reward_chain_ip_proof,
        block.infused_challenge_chain_ip_proof,
        block.foliage,
        block.foliage_transaction_block,
        encoded_filter,
        block.transactions_info,
    )


def additions_for_npc(npc_list: List[NPC]) -> List[Coin]:
    additions: List[Coin] = []

    for npc in npc_list:
        for coin in created_outputs_for_conditions_dict(npc.condition_dict, npc.coin_name):
            additions.append(coin)

    return additions


def tx_removals_and_additions(npc_list: List[NPC]) -> Tuple[List[bytes32], List[Coin]]:
    """
    Doesn't return farmer and pool reward.
    """

    removals: List[bytes32] = []
    additions: List[Coin] = []

    # build removals list
    if npc_list is None:
        return [], []
    for npc in npc_list:
        removals.append(npc.coin_name)

    additions.extend(additions_for_npc(npc_list))

    return removals, additions
