from dataclasses import dataclass
from enum import Enum
import time
from typing import Optional

from blspy import G1Element, G2Element

from tad.types.blockchain_format.proof_of_space import ProofOfSpace
from tad.types.blockchain_format.sized_bytes import bytes32
from tad.util.ints import uint8, uint16, uint32, uint64
from tad.util.streamable import Streamable, streamable

POOL_PROTOCOL_VERSION = uint8(1)


class PoolErrorCode(Enum):
    REVERTED_SIGNAGE_POINT = 1
    TOO_LATE = 2
    NOT_FOUND = 3
    INVALID_PROOF = 4
    PROOF_NOT_GOOD_ENOUGH = 5
    INVALID_DIFFICULTY = 6
    INVALID_SIGNATURE = 7
    SERVER_EXCEPTION = 8
    INVALID_P2_SINGLETON_PUZZLE_HASH = 9
    FARMER_NOT_KNOWN = 10
    FARMER_ALREADY_KNOWN = 11
    INVALID_AUTHENTICATION_TOKEN = 12
    INVALID_PAYOUT_INSTRUCTIONS = 13
    INVALID_SINGLETON = 14
    DELAY_TIME_TOO_SHORT = 15
    REQUEST_FAILED = 16


# Used to verify GET /farmer and GET /login
@dataclass(frozen=True)
@streamable
class AuthenticationPayload(Streamable):
    method_name: str
    launcher_id: bytes32
    target_puzzle_hash: bytes32
    authentication_token: uint64


# GET /pool_info
@dataclass(frozen=True)
@streamable
class GetPoolInfoResponse(Streamable):
    name: str
    logo_url: str
    minimum_difficulty: uint64
    relative_lock_height: uint32
    protocol_version: uint8
    fee: str
    description: str
    target_puzzle_hash: bytes32
    authentication_token_timeout: uint8


# POST /partial


@dataclass(frozen=True)
@streamable
class PostPartialPayload(Streamable):
    launcher_id: bytes32
    authentication_token: uint64
    proof_of_space: ProofOfSpace
    sp_hash: bytes32
    end_of_sub_slot: bool
    harvester_id: bytes32


@dataclass(frozen=True)
@streamable
class PostPartialRequest(Streamable):
    payload: PostPartialPayload
    aggregate_signature: G2Element


# Response in success case
@dataclass(frozen=True)
@streamable
class PostPartialResponse(Streamable):
    new_difficulty: uint64


# GET /farmer


# Response in success case
@dataclass(frozen=True)
@streamable
class GetFarmerResponse(Streamable):
    authentication_public_key: G1Element
    payout_instructions: str
    current_difficulty: uint64
    current_points: uint64


# POST /farmer


@dataclass(frozen=True)
@streamable
class PostFarmerPayload(Streamable):
    launcher_id: bytes32
    authentication_token: uint64
    authentication_public_key: G1Element
    payout_instructions: str
    suggested_difficulty: Optional[uint64]


@dataclass(frozen=True)
@streamable
class PostFarmerRequest(Streamable):
    payload: PostFarmerPayload
    signature: G2Element


# Response in success case
@dataclass(frozen=True)
@streamable
class PostFarmerResponse(Streamable):
    welcome_message: str


# PUT /farmer


@dataclass(frozen=True)
@streamable
class PutFarmerPayload(Streamable):
    launcher_id: bytes32
    authentication_token: uint64
    authentication_public_key: Optional[G1Element]
    payout_instructions: Optional[str]
    suggested_difficulty: Optional[uint64]


@dataclass(frozen=True)
@streamable
class PutFarmerRequest(Streamable):
    payload: PutFarmerPayload
    signature: G2Element


# Response in success case
@dataclass(frozen=True)
@streamable
class PutFarmerResponse(Streamable):
    authentication_public_key: Optional[bool]
    payout_instructions: Optional[bool]
    suggested_difficulty: Optional[bool]


# Misc


# Response in error case for all endpoints of the pool protocol
@dataclass(frozen=True)
@streamable
class ErrorResponse(Streamable):
    error_code: uint16
    error_message: Optional[str]


# Get the current authentication toke according "Farmer authentication" in SPECIFICATION.md
def get_current_authentication_token(timeout: uint8) -> uint64:
    return uint64(int(int(time.time() / 60) / timeout))


# Validate a given authentication token against our local time
def validate_authentication_token(token: uint64, timeout: uint8):
    return abs(token - get_current_authentication_token(timeout)) <= timeout
