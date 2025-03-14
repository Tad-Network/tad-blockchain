# flake8: noqa: E501
import logging
from os import unlink
from pathlib import Path
from secrets import token_bytes
from shutil import copy, move

import pytest
from blspy import AugSchemeMPL
from chiapos import DiskPlotter

from tad.consensus.coinbase import create_puzzlehash_for_pk
from tad.plotting.util import stream_plot_info_ph, stream_plot_info_pk, PlotRefreshResult
from tad.plotting.manager import PlotManager
from tad.protocols import farmer_protocol
from tad.rpc.farmer_rpc_api import FarmerRpcApi
from tad.rpc.farmer_rpc_client import FarmerRpcClient
from tad.rpc.harvester_rpc_api import HarvesterRpcApi
from tad.rpc.harvester_rpc_client import HarvesterRpcClient
from tad.rpc.rpc_server import start_rpc_server
from tad.types.blockchain_format.sized_bytes import bytes32
from tad.util.bech32m import decode_puzzle_hash, encode_puzzle_hash
from tests.block_tools import get_plot_dir
from tad.util.byte_types import hexstr_to_bytes
from tad.util.config import load_config, save_config
from tad.util.hash import std_hash
from tad.util.ints import uint8, uint16, uint32, uint64
from tad.wallet.derive_keys import master_sk_to_wallet_sk, master_sk_to_pooling_authentication_sk
from tests.setup_nodes import bt, self_hostname, setup_farmer_harvester, test_constants
from tests.time_out_assert import time_out_assert

log = logging.getLogger(__name__)


class TestRpc:
    @pytest.fixture(scope="function")
    async def simulation(self):
        async for _ in setup_farmer_harvester(test_constants):
            yield _

    @pytest.mark.asyncio
    async def test1(self, simulation):
        test_rpc_port = uint16(21522)
        test_rpc_port_2 = uint16(21523)
        harvester, farmer_api = simulation

        def stop_node_cb():
            pass

        def stop_node_cb_2():
            pass

        config = bt.config
        hostname = config["self_hostname"]
        daemon_port = config["daemon_port"]

        farmer_rpc_api = FarmerRpcApi(farmer_api.farmer)
        harvester_rpc_api = HarvesterRpcApi(harvester)

        rpc_cleanup = await start_rpc_server(
            farmer_rpc_api,
            hostname,
            daemon_port,
            test_rpc_port,
            stop_node_cb,
            bt.root_path,
            config,
            connect_to_daemon=False,
        )
        rpc_cleanup_2 = await start_rpc_server(
            harvester_rpc_api,
            hostname,
            daemon_port,
            test_rpc_port_2,
            stop_node_cb_2,
            bt.root_path,
            config,
            connect_to_daemon=False,
        )

        try:
            client = await FarmerRpcClient.create(self_hostname, test_rpc_port, bt.root_path, config)
            client_2 = await HarvesterRpcClient.create(self_hostname, test_rpc_port_2, bt.root_path, config)

            async def have_connections():
                return len(await client.get_connections()) > 0

            await time_out_assert(15, have_connections, True)

            assert (await client.get_signage_point(std_hash(b"2"))) is None
            assert len(await client.get_signage_points()) == 0

            async def have_signage_points():
                return len(await client.get_signage_points()) > 0

            sp = farmer_protocol.NewSignagePoint(
                std_hash(b"1"), std_hash(b"2"), std_hash(b"3"), uint64(1), uint64(1000000), uint8(2)
            )
            await farmer_api.new_signage_point(sp)

            await time_out_assert(5, have_signage_points, True)
            assert (await client.get_signage_point(std_hash(b"2"))) is not None

            async def have_plots():
                return len((await client_2.get_plots())["plots"]) > 0

            await time_out_assert(5, have_plots, True)

            res = await client_2.get_plots()
            num_plots = len(res["plots"])
            assert num_plots > 0
            plot_dir = get_plot_dir() / "subdir"
            plot_dir.mkdir(parents=True, exist_ok=True)

            plot_dir_sub = get_plot_dir() / "subdir" / "subsubdir"
            plot_dir_sub.mkdir(parents=True, exist_ok=True)

            plotter = DiskPlotter()
            filename = "test_farmer_harvester_rpc_plot.plot"
            filename_2 = "test_farmer_harvester_rpc_plot2.plot"
            plotter.create_plot_disk(
                str(plot_dir),
                str(plot_dir),
                str(plot_dir),
                filename,
                18,
                stream_plot_info_pk(bt.pool_pk, bt.farmer_pk, AugSchemeMPL.key_gen(bytes([4] * 32))),
                token_bytes(32),
                128,
                0,
                2000,
                0,
                False,
            )

            # Making a plot with a puzzle hash encoded into it instead of pk
            plot_id_2 = token_bytes(32)
            plotter.create_plot_disk(
                str(plot_dir),
                str(plot_dir),
                str(plot_dir),
                filename_2,
                18,
                stream_plot_info_ph(std_hash(b"random ph"), bt.farmer_pk, AugSchemeMPL.key_gen(bytes([5] * 32))),
                plot_id_2,
                128,
                0,
                2000,
                0,
                False,
            )

            # Making the same plot, in a different dir. This should not be farmed
            plotter.create_plot_disk(
                str(plot_dir_sub),
                str(plot_dir_sub),
                str(plot_dir_sub),
                filename_2,
                18,
                stream_plot_info_ph(std_hash(b"random ph"), bt.farmer_pk, AugSchemeMPL.key_gen(bytes([5] * 32))),
                plot_id_2,
                128,
                0,
                2000,
                0,
                False,
            )

            res_2 = await client_2.get_plots()
            assert len(res_2["plots"]) == num_plots

            # Test farmer get_harvesters
            async def test_get_harvesters():
                farmer_res = await client.get_harvesters()
                if len(list(farmer_res["harvesters"])) != 1:
                    return False
                if len(list(farmer_res["harvesters"][0]["plots"])) != num_plots:
                    return False
                return True

            await time_out_assert(30, test_get_harvesters)

            expected_result: PlotRefreshResult = PlotRefreshResult()

            def test_refresh_callback(refresh_result: PlotRefreshResult):
                assert refresh_result.loaded_plots == expected_result.loaded_plots
                assert refresh_result.removed_plots == expected_result.removed_plots
                assert refresh_result.processed_files == expected_result.processed_files
                assert refresh_result.remaining_files == expected_result.remaining_files

            harvester.plot_manager.set_refresh_callback(test_refresh_callback)

            async def test_case(
                trigger, expect_loaded, expect_removed, expect_processed, expected_directories, expect_total_plots
            ):
                expected_result.loaded_plots = expect_loaded
                expected_result.removed_plots = expect_removed
                expected_result.processed_files = expect_processed
                await trigger
                harvester.plot_manager.trigger_refresh()
                assert len(await client_2.get_plot_directories()) == expected_directories
                await time_out_assert(5, harvester.plot_manager.needs_refresh, value=False)
                result = await client_2.get_plots()
                assert len(result["plots"]) == expect_total_plots
                assert len(harvester.plot_manager.cache) == expect_total_plots
                assert len(harvester.plot_manager.failed_to_open_filenames) == 0

            # Add plot_dir with two new plots
            await test_case(
                client_2.add_plot_directory(str(plot_dir)),
                expect_loaded=2,
                expect_removed=0,
                expect_processed=2,
                expected_directories=2,
                expect_total_plots=num_plots + 2,
            )
            # Add plot_dir_sub with one duplicate
            await test_case(
                client_2.add_plot_directory(str(plot_dir_sub)),
                expect_loaded=0,
                expect_removed=0,
                expect_processed=1,
                expected_directories=3,
                expect_total_plots=num_plots + 2,
            )
            # Delete one plot
            await test_case(
                client_2.delete_plot(str(plot_dir / filename)),
                expect_loaded=0,
                expect_removed=1,
                expect_processed=0,
                expected_directories=3,
                expect_total_plots=num_plots + 1,
            )
            # Remove directory with the duplicate
            await test_case(
                client_2.remove_plot_directory(str(plot_dir_sub)),
                expect_loaded=0,
                expect_removed=1,
                expect_processed=0,
                expected_directories=2,
                expect_total_plots=num_plots + 1,
            )
            # Re-add the directory with the duplicate for other tests
            await test_case(
                client_2.add_plot_directory(str(plot_dir_sub)),
                expect_loaded=0,
                expect_removed=0,
                expect_processed=1,
                expected_directories=3,
                expect_total_plots=num_plots + 1,
            )
            # Remove the directory which has the duplicated plot loaded. This removes the duplicated plot from plot_dir
            # and in the same run loads the plot from plot_dir_sub which is not longer seen as duplicate.
            await test_case(
                client_2.remove_plot_directory(str(plot_dir)),
                expect_loaded=1,
                expect_removed=1,
                expect_processed=1,
                expected_directories=2,
                expect_total_plots=num_plots + 1,
            )
            # Re-add the directory now the plot seen as duplicate is from plot_dir, not from plot_dir_sub like before
            await test_case(
                client_2.add_plot_directory(str(plot_dir)),
                expect_loaded=0,
                expect_removed=0,
                expect_processed=1,
                expected_directories=3,
                expect_total_plots=num_plots + 1,
            )
            # Remove the duplicated plot
            await test_case(
                client_2.delete_plot(str(plot_dir / filename_2)),
                expect_loaded=0,
                expect_removed=1,
                expect_processed=0,
                expected_directories=3,
                expect_total_plots=num_plots + 1,
            )
            # Remove the directory with the loaded plot which is not longer a duplicate
            await test_case(
                client_2.remove_plot_directory(str(plot_dir_sub)),
                expect_loaded=0,
                expect_removed=1,
                expect_processed=0,
                expected_directories=2,
                expect_total_plots=num_plots,
            )
            # Remove the directory which contains all other plots
            await test_case(
                client_2.remove_plot_directory(str(get_plot_dir())),
                expect_loaded=0,
                expect_removed=20,
                expect_processed=0,
                expected_directories=1,
                expect_total_plots=0,
            )
            # Recover the plots to test caching
            # First make sure cache gets written if required and new plots are loaded
            await test_case(
                client_2.add_plot_directory(str(get_plot_dir())),
                expect_loaded=20,
                expect_removed=0,
                expect_processed=20,
                expected_directories=2,
                expect_total_plots=20,
            )
            assert harvester.plot_manager.cache.path().exists()
            unlink(harvester.plot_manager.cache.path())
            # Should not write the cache again on shutdown because it didn't change
            assert not harvester.plot_manager.cache.path().exists()
            harvester.plot_manager.stop_refreshing()
            assert not harvester.plot_manager.cache.path().exists()
            # Manually trigger `save_cache` and make sure it creates a new cache file
            harvester.plot_manager.cache.save()
            assert harvester.plot_manager.cache.path().exists()

            expected_result.loaded_plots = 20
            expected_result.removed_plots = 0
            expected_result.processed_files = 20
            expected_result.remaining_files = 0
            plot_manager: PlotManager = PlotManager(harvester.root_path, test_refresh_callback)
            plot_manager.start_refreshing()
            assert len(harvester.plot_manager.cache) == len(plot_manager.cache)
            await time_out_assert(5, plot_manager.needs_refresh, value=False)
            for path, plot_info in harvester.plot_manager.plots.items():
                assert path in plot_manager.plots
                assert plot_manager.plots[path].prover.get_filename() == plot_info.prover.get_filename()
                assert plot_manager.plots[path].prover.get_id() == plot_info.prover.get_id()
                assert plot_manager.plots[path].prover.get_memo() == plot_info.prover.get_memo()
                assert plot_manager.plots[path].prover.get_size() == plot_info.prover.get_size()
                assert plot_manager.plots[path].pool_public_key == plot_info.pool_public_key
                assert plot_manager.plots[path].pool_contract_puzzle_hash == plot_info.pool_contract_puzzle_hash
                assert plot_manager.plots[path].plot_public_key == plot_info.plot_public_key
                assert plot_manager.plots[path].file_size == plot_info.file_size
                assert plot_manager.plots[path].time_modified == plot_info.time_modified

            assert harvester.plot_manager.plot_filename_paths == plot_manager.plot_filename_paths
            assert harvester.plot_manager.failed_to_open_filenames == plot_manager.failed_to_open_filenames
            assert harvester.plot_manager.no_key_filenames == plot_manager.no_key_filenames
            plot_manager.stop_refreshing()
            # Modify the content of the plot_manager.dat
            with open(harvester.plot_manager.cache.path(), "r+b") as file:
                file.write(b"\xff\xff")  # Sets Cache.version to 65535
            # Make sure it just loads the plots normally if it fails to load the cache
            plot_manager = PlotManager(harvester.root_path, test_refresh_callback)
            plot_manager.cache.load()
            assert len(plot_manager.cache) == 0
            plot_manager.set_public_keys(
                harvester.plot_manager.farmer_public_keys, harvester.plot_manager.pool_public_keys
            )
            expected_result.loaded_plots = 20
            expected_result.removed_plots = 0
            expected_result.processed_files = 20
            expected_result.remaining_files = 0
            plot_manager.start_refreshing()
            await time_out_assert(5, plot_manager.needs_refresh, value=False)
            assert len(plot_manager.plots) == len(harvester.plot_manager.plots)
            plot_manager.stop_refreshing()

            # Test re-trying if processing a plot failed
            # First save the plot
            retry_test_plot = Path(plot_dir_sub / filename_2).resolve()
            retry_test_plot_save = Path(plot_dir_sub / "save").resolve()
            copy(retry_test_plot, retry_test_plot_save)
            # Invalidate the plot
            with open(plot_dir_sub / filename_2, "r+b") as file:
                file.write(bytes(100))
            # Add it and validate it fails to load
            await harvester.add_plot_directory(str(plot_dir_sub))
            expected_result.loaded_plots = 0
            expected_result.removed_plots = 0
            expected_result.processed_files = 1
            expected_result.remaining_files = 0
            harvester.plot_manager.start_refreshing()
            await time_out_assert(5, harvester.plot_manager.needs_refresh, value=False)
            assert retry_test_plot in harvester.plot_manager.failed_to_open_filenames
            # Make sure the file stays in `failed_to_open_filenames` and doesn't get loaded or processed in the next
            # update round
            expected_result.loaded_plots = 0
            expected_result.processed_files = 0
            harvester.plot_manager.trigger_refresh()
            await time_out_assert(5, harvester.plot_manager.needs_refresh, value=False)
            assert retry_test_plot in harvester.plot_manager.failed_to_open_filenames
            # Now decrease the re-try timeout, restore the valid plot file and make sure it properly loads now
            harvester.plot_manager.refresh_parameter.retry_invalid_seconds = 0
            move(retry_test_plot_save, retry_test_plot)
            expected_result.loaded_plots = 1
            expected_result.processed_files = 1
            harvester.plot_manager.trigger_refresh()
            await time_out_assert(5, harvester.plot_manager.needs_refresh, value=False)
            assert retry_test_plot not in harvester.plot_manager.failed_to_open_filenames

            targets_1 = await client.get_reward_targets(False)
            assert "have_pool_sk" not in targets_1
            assert "have_farmer_sk" not in targets_1
            targets_2 = await client.get_reward_targets(True)
            assert targets_2["have_pool_sk"] and targets_2["have_farmer_sk"]

            new_ph: bytes32 = create_puzzlehash_for_pk(master_sk_to_wallet_sk(bt.farmer_master_sk, uint32(10)).get_g1())
            new_ph_2: bytes32 = create_puzzlehash_for_pk(
                master_sk_to_wallet_sk(bt.pool_master_sk, uint32(472)).get_g1()
            )

            await client.set_reward_targets(encode_puzzle_hash(new_ph, "tad"), encode_puzzle_hash(new_ph_2, "tad"))
            targets_3 = await client.get_reward_targets(True)
            assert decode_puzzle_hash(targets_3["farmer_target"]) == new_ph
            assert decode_puzzle_hash(targets_3["pool_target"]) == new_ph_2
            assert targets_3["have_pool_sk"] and targets_3["have_farmer_sk"]

            new_ph_3: bytes32 = create_puzzlehash_for_pk(
                master_sk_to_wallet_sk(bt.pool_master_sk, uint32(1888)).get_g1()
            )
            await client.set_reward_targets(None, encode_puzzle_hash(new_ph_3, "tad"))
            targets_4 = await client.get_reward_targets(True)
            assert decode_puzzle_hash(targets_4["farmer_target"]) == new_ph
            assert decode_puzzle_hash(targets_4["pool_target"]) == new_ph_3
            assert not targets_4["have_pool_sk"] and targets_3["have_farmer_sk"]

            root_path = farmer_api.farmer._root_path
            config = load_config(root_path, "config.yaml")
            assert config["farmer"]["tad_target_address"] == encode_puzzle_hash(new_ph, "tad")
            assert config["pool"]["tad_target_address"] == encode_puzzle_hash(new_ph_3, "tad")

            new_ph_3_encoded = encode_puzzle_hash(new_ph_3, "tad")
            added_char = new_ph_3_encoded + "a"
            with pytest.raises(ValueError):
                await client.set_reward_targets(None, added_char)

            replaced_char = new_ph_3_encoded[0:-1] + "a"
            with pytest.raises(ValueError):
                await client.set_reward_targets(None, replaced_char)

            assert len((await client.get_pool_state())["pool_state"]) == 0
            all_sks = farmer_api.farmer.local_keychain.get_all_private_keys()
            auth_sk = master_sk_to_pooling_authentication_sk(all_sks[0][0], 2, 1)
            pool_list = [
                {
                    "launcher_id": "ae4ef3b9bfe68949691281a015a9c16630fc8f66d48c19ca548fb80768791afa",
                    "authentication_public_key": bytes(auth_sk.get_g1()).hex(),
                    "owner_public_key": "84c3fcf9d5581c1ddc702cb0f3b4a06043303b334dd993ab42b2c320ebfa98e5ce558448615b3f69638ba92cf7f43da5",
                    "payout_instructions": "c2b08e41d766da4116e388357ed957d04ad754623a915f3fd65188a8746cf3e8",
                    "pool_url": "localhost",
                    "p2_singleton_puzzle_hash": "16e4bac26558d315cded63d4c5860e98deb447cc59146dd4de06ce7394b14f17",
                    "target_puzzle_hash": "344587cf06a39db471d2cc027504e8688a0a67cce961253500c956c73603fd58",
                }
            ]
            config["pool"]["pool_list"] = pool_list
            save_config(root_path, "config.yaml", config)
            await farmer_api.farmer.update_pool_state()

            pool_state = (await client.get_pool_state())["pool_state"]
            assert len(pool_state) == 1
            assert (
                pool_state[0]["pool_config"]["payout_instructions"]
                == "c2b08e41d766da4116e388357ed957d04ad754623a915f3fd65188a8746cf3e8"
            )
            await client.set_payout_instructions(hexstr_to_bytes(pool_state[0]["pool_config"]["launcher_id"]), "1234vy")
            await farmer_api.farmer.update_pool_state()
            pool_state = (await client.get_pool_state())["pool_state"]
            assert pool_state[0]["pool_config"]["payout_instructions"] == "1234vy"

        finally:
            # Checks that the RPC manages to stop the node
            client.close()
            client_2.close()
            await client.await_closed()
            await client_2.await_closed()
            await rpc_cleanup()
            await rpc_cleanup_2()
