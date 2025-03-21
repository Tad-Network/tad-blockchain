from dataclasses import dataclass

from tad.consensus.constants import ConsensusConstants
from tad.types.blockchain_format.sized_bytes import bytes100
from tad.util.streamable import Streamable, streamable


@dataclass(frozen=True)
@streamable
class ClassgroupElement(Streamable):
    """
    Represents a classgroup element (a,b,c) where a, b, and c are 512 bit signed integers. However this is using
    a compressed representation. VDF outputs are a single classgroup element. VDF proofs can also be one classgroup
    element (or multiple).
    """

    data: bytes100

    @staticmethod
    def from_bytes(data) -> "ClassgroupElement":
        if len(data) < 100:
            data += b"\x00" * (100 - len(data))
        return ClassgroupElement(bytes100(data))

    @staticmethod
    def get_default_element() -> "ClassgroupElement":
        # Bit 3 in the first byte of serialized compressed form indicates if
        # it's the default generator element.
        return ClassgroupElement.from_bytes(b"\x08")

    @staticmethod
    def get_size(constants: ConsensusConstants):
        return 100
