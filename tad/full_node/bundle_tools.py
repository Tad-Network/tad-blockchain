import re
from typing import Optional, Tuple, List, Union

from clvm import SExp
from clvm_tools import binutils

from tad.full_node.generator import create_compressed_generator
from tad.types.blockchain_format.program import SerializedProgram, Program
from tad.types.coin_spend import CoinSpend
from tad.types.generator_types import BlockGenerator, CompressorArg
from tad.types.spend_bundle import SpendBundle
from tad.util.byte_types import hexstr_to_bytes
from tad.util.ints import uint32, uint64


def spend_bundle_to_serialized_coin_spend_entry_list(bundle: SpendBundle) -> bytes:
    r = b""
    for coin_spend in bundle.coin_spends:
        r += b"\xff"
        r += b"\xff" + SExp.to(coin_spend.coin.parent_coin_info).as_bin()
        r += b"\xff" + bytes(coin_spend.puzzle_reveal)
        r += b"\xff" + SExp.to(coin_spend.coin.amount).as_bin()
        r += b"\xff" + bytes(coin_spend.solution)
        r += b"\x80"
    r += b"\x80"
    return r


def simple_solution_generator(bundle: SpendBundle) -> BlockGenerator:
    """
    Simply quotes the solutions we know.
    """
    cse_list = spend_bundle_to_serialized_coin_spend_entry_list(bundle)
    block_program = b"\xff"

    block_program += SExp.to(binutils.assemble("#q")).as_bin()

    block_program += b"\xff" + cse_list + b"\x80"

    return BlockGenerator(SerializedProgram.from_bytes(block_program), [])


STANDARD_TRANSACTION_PUZZLE_PREFIX = r"""ff02ffff01ff02ffff01ff02ffff03ff0bffff01ff02ffff03ffff09ff05ffff1dff0bffff1effff0bff0bffff02ff06ffff04ff02ffff04ff17ff8080808080808080ffff01ff02ff17ff2f80ffff01ff088080ff0180ffff01ff04ffff04ff04ffff04ff05ffff04ffff02ff06ffff04ff02ffff04ff17ff80808080ff80808080ffff02ff17ff2f808080ff0180ffff04ffff01ff32ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff06ffff04ff02ffff04ff09ff80808080ffff02ff06ffff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff018080ffff04ffff01"""  # noqa

STANDARD_TRANSACTION_PUZZLE_PATTERN = re.compile(STANDARD_TRANSACTION_PUZZLE_PREFIX + r"(b0[a-f0-9]{96})ff018080")


# match_standard_transaction_anywhere
def match_standard_transaction_at_any_index(generator_body: bytes) -> Optional[Tuple[int, int]]:
    """Return (start, end) of match, or None if pattern could not be found"""

    # We intentionally match the entire puzzle, not just the prefix that we will use,
    # in case we later want to convert the template generator into a tree of CLVM
    # Objects before operating on it
    m = STANDARD_TRANSACTION_PUZZLE_PATTERN.search(generator_body.hex())
    if m:
        assert m.start() % 2 == 0 and m.end() % 2 == 0
        start = m.start() // 2
        end = (m.end() - 98 - len("ff018080")) // 2
        assert generator_body[start:end] == bytes.fromhex(STANDARD_TRANSACTION_PUZZLE_PREFIX)
        return start, end
    else:
        return None


def match_standard_transaction_exactly_and_return_pubkey(puzzle: SerializedProgram) -> Optional[bytes]:
    m = STANDARD_TRANSACTION_PUZZLE_PATTERN.fullmatch(bytes(puzzle).hex())
    return None if m is None else hexstr_to_bytes(m.group(1))


def compress_cse_puzzle(puzzle: SerializedProgram) -> Optional[bytes]:
    return match_standard_transaction_exactly_and_return_pubkey(puzzle)


def compress_coin_spend(coin_spend: CoinSpend):
    compressed_puzzle = compress_cse_puzzle(coin_spend.puzzle_reveal)
    return [
        [coin_spend.coin.parent_coin_info, coin_spend.coin.amount],
        [compressed_puzzle, Program.from_bytes(bytes(coin_spend.solution))],
    ]


def puzzle_suitable_for_compression(puzzle: SerializedProgram) -> bool:
    return True if match_standard_transaction_exactly_and_return_pubkey(puzzle) else False


def bundle_suitable_for_compression(bundle: SpendBundle):
    ok = []
    for coin_spend in bundle.coin_spends:
        ok.append(puzzle_suitable_for_compression(coin_spend.puzzle_reveal))
    return all(ok)


def compressed_coin_spend_entry_list(bundle: SpendBundle) -> List:
    compressed_cse_list: List[List[Union[List[uint64], List[Union[bytes, None, Program]]]]] = []
    for coin_spend in bundle.coin_spends:
        compressed_cse_list.append(compress_coin_spend(coin_spend))
    return compressed_cse_list


def compressed_spend_bundle_solution(original_generator_params: CompressorArg, bundle: SpendBundle) -> BlockGenerator:
    compressed_cse_list = compressed_coin_spend_entry_list(bundle)
    return create_compressed_generator(original_generator_params, compressed_cse_list)


def best_solution_generator_from_template(previous_generator: CompressorArg, bundle: SpendBundle) -> BlockGenerator:
    """
    Creates a compressed block generator, taking in a block that passes the checks below
    """
    if bundle_suitable_for_compression(bundle):
        return compressed_spend_bundle_solution(previous_generator, bundle)
    else:
        return simple_solution_generator(bundle)


def detect_potential_template_generator(block_height: uint32, program: SerializedProgram) -> Optional[CompressorArg]:
    """
    If this returns a GeneratorArg, that means that the input, `program`, has a standard transaction
    that is not compressed that we can use as a template for future blocks.
    If it returns None, this block cannot be used.
    In this implementation, we store the offsets needed by the compressor in the GeneratorArg
    This block will serve as a template for the compression of other newly farmed blocks.
    """

    m = match_standard_transaction_at_any_index(bytes(program))
    if m is None:
        return None
    start, end = m
    if start and end and end > start >= 0:
        return CompressorArg(block_height, program, start, end)
    else:
        return None
