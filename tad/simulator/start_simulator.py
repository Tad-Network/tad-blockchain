import sys
from pathlib import Path
from multiprocessing import freeze_support
from typing import Dict

from tad.full_node.full_node import FullNode
from tad.rpc.full_node_rpc_api import FullNodeRpcApi
from tad.server.outbound_message import NodeType
from tad.server.start_service import run_service
from tad.util.config import load_config_cli
from tad.util.default_root import DEFAULT_ROOT_PATH
from tad.util.path import mkdir, path_from_root
from tests.block_tools import BlockTools, create_block_tools, test_constants
from tests.util.keyring import TempKeyring

from .full_node_simulator import FullNodeSimulator

# See: https://bugs.python.org/issue29288
"".encode("idna")

SERVICE_NAME = "full_node"


def service_kwargs_for_full_node_simulator(root_path: Path, config: Dict, bt: BlockTools) -> Dict:
    mkdir(path_from_root(root_path, config["database_path"]).parent)
    constants = bt.constants

    node = FullNode(
        config,
        root_path=root_path,
        consensus_constants=constants,
        name=SERVICE_NAME,
    )

    peer_api = FullNodeSimulator(node, bt)
    network_id = config["selected_network"]
    kwargs = dict(
        root_path=root_path,
        node=node,
        peer_api=peer_api,
        node_type=NodeType.FULL_NODE,
        advertised_port=config["port"],
        service_name=SERVICE_NAME,
        server_listen_ports=[config["port"]],
        on_connect_callback=node.on_connect,
        rpc_info=(FullNodeRpcApi, config["rpc_port"]),
        network_id=network_id,
    )
    return kwargs


def main() -> None:
    # Use a temp keychain which will be deleted when it exits scope
    with TempKeyring() as keychain:
        # If launched with -D, we should connect to the keychain via the daemon instead
        # of using a local keychain
        if "-D" in sys.argv:
            keychain = None
            sys.argv.remove("-D")  # Remove -D to avoid conflicting with load_config_cli's argparse usage
        config = load_config_cli(DEFAULT_ROOT_PATH, "config.yaml", SERVICE_NAME)
        config["database_path"] = config["simulator_database_path"]
        config["peer_db_path"] = config["simulator_peer_db_path"]
        config["introducer_peer"]["host"] = "127.0.0.1"
        config["introducer_peer"]["port"] = 58555
        config["selected_network"] = "testnet0"
        config["simulation"] = True
        kwargs = service_kwargs_for_full_node_simulator(
            DEFAULT_ROOT_PATH,
            config,
            create_block_tools(test_constants, root_path=DEFAULT_ROOT_PATH, keychain=keychain),
        )
        return run_service(**kwargs)


if __name__ == "__main__":
    freeze_support()
    main()
