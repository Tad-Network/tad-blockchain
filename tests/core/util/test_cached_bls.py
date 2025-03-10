import unittest
from blspy import AugSchemeMPL
from tad.util import cached_bls
from tad.util.lru_cache import LRUCache


class TestCachedBLS(unittest.TestCase):
    def test_cached_bls(self):
        n_keys = 10
        seed = b"a" * 31
        sks = [AugSchemeMPL.key_gen(seed + bytes([i])) for i in range(n_keys)]
        pks = [sk.get_g1() for sk in sks]

        msgs = [("msg-%d" % (i,)).encode() for i in range(n_keys)]
        sigs = [AugSchemeMPL.sign(sk, msg) for sk, msg in zip(sks, msgs)]
        agg_sig = AugSchemeMPL.aggregate(sigs)

        pks_half = pks[: n_keys // 2]
        msgs_half = msgs[: n_keys // 2]
        sigs_half = sigs[: n_keys // 2]
        agg_sig_half = AugSchemeMPL.aggregate(sigs_half)

        assert AugSchemeMPL.aggregate_verify(pks, msgs, agg_sig)

        # Verify with empty cache and populate it
        assert cached_bls.aggregate_verify(pks_half, msgs_half, agg_sig_half, True)
        # Verify with partial cache hit
        assert cached_bls.aggregate_verify(pks, msgs, agg_sig, True)
        # Verify with full cache hit
        assert cached_bls.aggregate_verify(pks, msgs, agg_sig)

        # Use a small cache which can not accommodate all pairings
        local_cache = LRUCache(n_keys // 2)
        # Verify signatures and cache pairings one at a time
        for pk, msg, sig in zip(pks_half, msgs_half, sigs_half):
            assert cached_bls.aggregate_verify([pk], [msg], sig, True, local_cache)
        # Verify the same messages with aggregated signature (full cache hit)
        assert cached_bls.aggregate_verify(pks_half, msgs_half, agg_sig_half, False, local_cache)
        # Verify more messages (partial cache hit)
        assert cached_bls.aggregate_verify(pks, msgs, agg_sig, False, local_cache)
