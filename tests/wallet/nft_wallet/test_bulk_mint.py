import asyncio
import csv
import logging
import time
from secrets import token_bytes
from typing import Any, Awaitable, Callable, Dict, List

import pytest
from faker import Faker

from chia.consensus.block_rewards import calculate_base_farmer_reward, calculate_pool_reward
from chia.full_node.mempool_manager import MempoolManager
from chia.rpc.wallet_rpc_api import WalletRpcApi
from chia.simulator.full_node_simulator import FullNodeSimulator
from chia.simulator.simulator_protocol import FarmNewBlockProtocol
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.peer_info import PeerInfo
from chia.util.bech32m import decode_puzzle_hash, encode_puzzle_hash
from chia.util.byte_types import hexstr_to_bytes
from chia.util.ints import uint16, uint32, uint64
from chia.wallet.did_wallet.did_info import DID_HRP
from chia.wallet.did_wallet.did_wallet import DIDWallet
from chia.wallet.nft_wallet.nft_wallet import NFTWallet
from tests.time_out_assert import time_out_assert, time_out_assert_not_none

logging.getLogger("aiosqlite").setLevel(logging.INFO)  # Too much logging on debug level


async def wait_rpc_state_condition(
    timeout: int,
    coroutine: Callable[[Dict[str, Any]], Awaitable[Dict]],
    params: List[Dict],
    condition_func: Callable[[Dict[str, Any]], bool],
) -> Dict:
    start = time.monotonic()
    resp = None
    while time.monotonic() - start < timeout:
        resp = await coroutine(*params)
        assert isinstance(resp, dict)
        if condition_func(resp):
            return resp
        await asyncio.sleep(0.5)
    # timed out
    assert time.monotonic() - start < timeout, resp
    return {}


async def tx_in_pool(mempool: MempoolManager, tx_id: bytes32) -> bool:
    tx = mempool.get_spendbundle(tx_id)
    if tx is None:
        return False
    return True


async def create_nft_sample(fake: Faker, royalty_did: str, royalty_basis_pts: uint16) -> List[Any]:
    sample: List[Any] = [
        bytes32(token_bytes(32)).hex(),  # data_hash
        fake.image_url(),  # data_url
        bytes32(token_bytes(32)).hex(),  # metadata_hash
        fake.url(),  # metadata_url
        bytes32(token_bytes(32)).hex(),  # license_hash
        fake.url(),  # license_url
        1,  # series_number
        1,  # series_total
        royalty_did,  # royalty_ph
        royalty_basis_pts,  # royalty_percentage
        encode_puzzle_hash(bytes32(token_bytes(32)), DID_HRP),  # target address
    ]
    return sample


@pytest.fixture(scope="function")
async def csv_file(tmpdir_factory: Any) -> str:
    count = 10000
    fake = Faker()
    royalty_did = encode_puzzle_hash(bytes32(token_bytes(32)), DID_HRP)
    royalty_basis_pts = uint16(200)
    coros = [create_nft_sample(fake, royalty_did, royalty_basis_pts) for _ in range(count)]
    data = await asyncio.gather(*coros)
    filename = str(tmpdir_factory.mktemp("data").join("sample.csv"))
    with open(filename, "w") as f:
        writer = csv.writer(f)
        writer.writerows(data)
    return filename


@pytest.mark.parametrize(
    "trusted",
    [True],
)
@pytest.mark.asyncio
# @pytest.mark.skip
async def test_nft_bulk_mint(two_wallet_nodes: Any, trusted: Any, csv_file: Any) -> None:
    csv_filename = await csv_file
    num_blocks = 10
    full_nodes, wallets = two_wallet_nodes
    full_node_api: FullNodeSimulator = full_nodes[0]
    full_node_server = full_node_api.server
    wallet_node_maker, server_0 = wallets[0]
    wallet_node_taker, server_1 = wallets[1]
    wallet_maker = wallet_node_maker.wallet_state_manager.main_wallet
    wallet_taker = wallet_node_taker.wallet_state_manager.main_wallet

    ph_maker = await wallet_maker.get_new_puzzlehash()
    ph_taker = await wallet_taker.get_new_puzzlehash()
    ph_token = bytes32(token_bytes())

    if trusted:
        wallet_node_maker.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
        wallet_node_taker.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
    else:
        wallet_node_maker.config["trusted_peers"] = {}
        wallet_node_taker.config["trusted_peers"] = {}

    await server_0.start_client(PeerInfo("localhost", uint16(full_node_server._port)), None)
    await server_1.start_client(PeerInfo("localhost", uint16(full_node_server._port)), None)

    for _ in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_maker))
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_taker))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_token))

    funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks)]
    )

    await time_out_assert(10, wallet_maker.get_unconfirmed_balance, funds)
    await time_out_assert(10, wallet_maker.get_confirmed_balance, funds)
    await time_out_assert(10, wallet_taker.get_unconfirmed_balance, funds)
    await time_out_assert(10, wallet_taker.get_confirmed_balance, funds)

    did_wallet_maker: DIDWallet = await DIDWallet.create_new_did_wallet(
        wallet_node_maker.wallet_state_manager, wallet_maker, uint64(1)
    )
    spend_bundle_list = await wallet_node_maker.wallet_state_manager.tx_store.get_unconfirmed_for_wallet(
        wallet_maker.id()
    )

    spend_bundle = spend_bundle_list[0].spend_bundle
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, spend_bundle.name())

    for _ in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_token))

    await time_out_assert(15, wallet_maker.get_pending_change_balance, 0)
    await time_out_assert(10, wallet_maker.get_unconfirmed_balance, funds - 1)
    await time_out_assert(10, wallet_maker.get_confirmed_balance, funds - 1)

    hex_did_id = did_wallet_maker.get_my_DID()
    hmr_did_id = encode_puzzle_hash(bytes32.from_hexstr(hex_did_id), DID_HRP)
    did_id = bytes32.fromhex(hex_did_id)
    royalty_did = hmr_did_id
    royalty_basis_pts = uint16(200)

    nft_wallet_maker = await NFTWallet.create_new_nft_wallet(
        wallet_node_maker.wallet_state_manager, wallet_maker, name="NFT WALLET DID 1", did_id=did_id
    )

    for _ in range(1, num_blocks * 3):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_maker))

    with open(csv_filename, "r") as f:
        csv_reader = csv.reader(f)
        bulk_data = list(csv_reader)

    chunk = 5

    metadata_list_rpc = []
    for row in bulk_data[:chunk]:
        metadata = {
            "hash": row[0],
            "uris": [row[1]],
            "meta_hash": row[2],
            "meta_urls": [row[3]],
            "license_hash": row[4],
            "license_urls": [row[5]],
            "series_number": row[6],
            "series_total": row[7],
        }
        metadata_list_rpc.append(metadata)

    metadata_list = []
    for meta in metadata_list_rpc:
        metadata = [  # type: ignore
            ("u", meta["uris"]),
            ("h", hexstr_to_bytes(meta["hash"])),  # type: ignore
            ("mu", meta.get("meta_uris", [])),
            ("lu", meta.get("license_uris", [])),
            ("sn", uint64(meta.get("series_number", 1))),  # type: ignore
            ("st", uint64(meta.get("series_total", 1))),  # type: ignore
        ]
        if "meta_hash" in meta and len(meta["meta_hash"]) > 0:
            metadata.append(("mh", hexstr_to_bytes(meta["meta_hash"])))  # type: ignore
        if "license_hash" in meta and len(meta["license_hash"]) > 0:
            metadata.append(("lh", hexstr_to_bytes(meta["license_hash"])))  # type: ignore
        metadata_list.append(Program.to(metadata))

    fee = uint64(5)
    tx = await nft_wallet_maker.bulk_generate_nfts(metadata_list, did_id, royalty_did, royalty_basis_pts, fee)

    await time_out_assert(
        15, tx_in_pool, True, full_node_api.full_node.mempool_manager, tx.spend_bundle.name()  # type: ignore
    )

    for _ in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_token))

    await time_out_assert(15, len, chunk, nft_wallet_maker.my_nft_coins)

    # set DID for the bulk NFTs
    nfts_to_set = nft_wallet_maker.my_nft_coins
    set_tx = await nft_wallet_maker.bulk_set_nft_did(nfts_to_set, did_id, fee=fee)
    await asyncio.sleep(5)
    await time_out_assert(
        15, tx_in_pool, True, full_node_api.full_node.mempool_manager, set_tx.spend_bundle.name()  # type: ignore
    )

    for _ in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_maker))

    await time_out_assert(15, len, chunk, nft_wallet_maker.my_nft_coins)

    # send NFTs to recipients
    targets = [decode_puzzle_hash(row[10]) for row in bulk_data]
    nfts_to_send = nft_wallet_maker.my_nft_coins
    send_tx = await nft_wallet_maker.bulk_transfer(list(zip(nfts_to_send, targets[:chunk])), fee=fee)

    await time_out_assert(
        15, tx_in_pool, True, full_node_api.full_node.mempool_manager, send_tx.spend_bundle.name()  # type: ignore
    )

    for _ in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_token))
    await asyncio.sleep(5)
    await time_out_assert(15, len, 0, nft_wallet_maker.my_nft_coins)


@pytest.mark.parametrize(
    "trusted",
    [True],
)
@pytest.mark.asyncio
async def test_nft_rpc_bulk_mint(two_wallet_nodes: Any, trusted: Any, csv_file: Any) -> None:
    csv_filename = await csv_file
    num_blocks = 3
    full_nodes, wallets = two_wallet_nodes
    full_node_api = full_nodes[0]
    full_node_server = full_node_api.server
    wallet_node_0, server_0 = wallets[0]
    wallet_node_1, server_1 = wallets[1]
    wallet_0 = wallet_node_0.wallet_state_manager.main_wallet
    wallet_1 = wallet_node_1.wallet_state_manager.main_wallet

    ph = await wallet_0.get_new_puzzlehash()
    ph1 = await wallet_1.get_new_puzzlehash()

    if trusted:
        wallet_node_0.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
        wallet_node_1.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
    else:
        wallet_node_0.config["trusted_peers"] = {}
        wallet_node_1.config["trusted_peers"] = {}

    for i in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))

    await server_0.start_client(PeerInfo("localhost", uint16(full_node_server._port)), None)
    await server_1.start_client(PeerInfo("localhost", uint16(full_node_server._port)), None)

    funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks - 1)]
    )

    await time_out_assert(10, wallet_0.get_unconfirmed_balance, funds)
    await time_out_assert(10, wallet_0.get_confirmed_balance, funds)

    api_0 = WalletRpcApi(wallet_node_0)
    api_1 = WalletRpcApi(wallet_node_1)
    await time_out_assert(10, wallet_node_0.wallet_state_manager.synced, True)
    await time_out_assert(10, wallet_node_1.wallet_state_manager.synced, True)

    did_wallet: DIDWallet = await DIDWallet.create_new_did_wallet(
        wallet_node_0.wallet_state_manager, wallet_0, uint64(1)
    )
    spend_bundle_list = await wallet_node_0.wallet_state_manager.tx_store.get_unconfirmed_for_wallet(wallet_0.id())
    spend_bundle = spend_bundle_list[0].spend_bundle
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, spend_bundle.name())

    for _ in range(1, num_blocks * 5):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))

    await time_out_assert(15, wallet_0.get_pending_change_balance, 0)
    hex_did_id = did_wallet.get_my_DID()
    hmr_did_id = encode_puzzle_hash(bytes32.from_hexstr(hex_did_id), DID_HRP)
    # did_id = bytes32.fromhex(hex_did_id)
    royalty_address = hmr_did_id
    royalty_percentage = uint16(200)

    nft_wallet_0 = await api_0.create_new_wallet(dict(wallet_type="nft_wallet", name="NFT WALLET 1", did_id=hmr_did_id))
    assert isinstance(nft_wallet_0, dict)
    assert nft_wallet_0.get("success")
    nft_wallet_0_id = nft_wallet_0["wallet_id"]

    with open(csv_filename, "r") as f:
        csv_reader = csv.reader(f)
        bulk_data = list(csv_reader)

    chunk = 15

    metadata_list_rpc = []
    for row in bulk_data[:chunk]:
        metadata = {
            "hash": row[0],
            "uris": [row[1]],
            "meta_hash": row[2],
            "meta_urls": [row[3]],
            "license_hash": row[4],
            "license_urls": [row[5]],
            "series_number": row[6],
            "series_total": row[7],
        }
        metadata_list_rpc.append(metadata)

    metadata_list = []
    for meta in metadata_list_rpc:
        metadata = [  # type: ignore
            ("u", meta["uris"]),
            ("h", hexstr_to_bytes(meta["hash"])),  # type: ignore
            ("mu", meta.get("meta_uris", [])),
            ("lu", meta.get("license_uris", [])),
            ("sn", uint64(meta.get("series_number", 1))),  # type: ignore
            ("st", uint64(meta.get("series_total", 1))),  # type: ignore
        ]
        if "meta_hash" in meta and len(meta["meta_hash"]) > 0:
            metadata.append(("mh", hexstr_to_bytes(meta["meta_hash"])))  # type: ignore
        if "license_hash" in meta and len(meta["license_hash"]) > 0:
            metadata.append(("lh", hexstr_to_bytes(meta["license_hash"])))  # type: ignore
        metadata_list.append(Program.to(metadata))

    resp = await api_0.nft_bulk_mint_nft(
        {
            "wallet_id": nft_wallet_0_id,
            "metadata_list": metadata_list_rpc,
            "royalty_address": royalty_address,
            "royalty_percentage": royalty_percentage,
            "did_id": hmr_did_id,
            "fee": 100,
        }
    )
    assert resp["success"]
    # Confirm the transaction is in mempool, farm block, and confirm nfts are created in wallet
    for _ in range(1, num_blocks * 5):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph1))
    ntf_num = len(metadata_list_rpc)
    res = await wait_rpc_state_condition(
        15,
        api_0.nft_get_nfts,
        [dict(wallet_id=nft_wallet_0_id)],
        lambda x: x["nft_list"] and len(x["nft_list"]) == ntf_num,
    )
    transfer_list = []
    addr = encode_puzzle_hash(ph1, "txch")
    for nft in res["nft_list"]:
        nft_info = nft.to_json_dict()
        assert nft_info["owner_did"][2:] == hex_did_id
        transfer_list.append([nft_info["nft_coin_id"], addr])

    # Create another DID
    did_wallet_1: DIDWallet = await DIDWallet.create_new_did_wallet(
        wallet_node_1.wallet_state_manager, wallet_1, uint64(1)
    )
    spend_bundle_list = await wallet_node_1.wallet_state_manager.tx_store.get_unconfirmed_for_wallet(wallet_1.id())
    spend_bundle = spend_bundle_list[0].spend_bundle
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, spend_bundle.name())

    # Test bulk transfer
    resp = await api_0.nft_bulk_transfer(dict(wallet_id=nft_wallet_0_id, transfer_list=transfer_list, fee=100))
    assert resp["success"]

    for _ in range(1, num_blocks * 5):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))

    await time_out_assert(15, wallet_1.get_pending_change_balance, 0)
    hex_did_id_1 = did_wallet_1.get_my_DID()
    hmr_did_id_1 = encode_puzzle_hash(bytes32.from_hexstr(hex_did_id_1), DID_HRP)

    res = await wait_rpc_state_condition(15, api_1.nft_get_by_did, [dict()], lambda x: x["wallet_id"])
    nft_wallet_1_id = res["wallet_id"]
    res = await wait_rpc_state_condition(
        15,
        api_1.nft_get_nfts,
        [dict(wallet_id=nft_wallet_1_id)],
        lambda x: x["nft_list"] and len(x["nft_list"]) == ntf_num,
    )
    nft_list = []
    for nft in res["nft_list"]:
        nft_info = nft.to_json_dict()
        assert not nft_info["owner_did"]
        nft_list.append(nft_info["nft_coin_id"])

    # Test bulk set DID
    resp = await api_1.nft_bulk_set_did(
        dict(wallet_id=nft_wallet_1_id, nft_coin_id_list=nft_list, did_id=hmr_did_id_1, fee=100)
    )
    assert resp["success"]

    for _ in range(1, num_blocks * 5):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph1))

    res = await wait_rpc_state_condition(
        15, api_1.nft_get_by_did, [dict(did_id=hmr_did_id_1)], lambda x: x["wallet_id"]
    )
    nft_wallet_2_id = res["wallet_id"]
    res = await wait_rpc_state_condition(
        15,
        api_1.nft_get_nfts,
        [dict(wallet_id=nft_wallet_2_id)],
        lambda x: x["nft_list"] and len(x["nft_list"]) == ntf_num,
    )
    for nft in res["nft_list"]:
        nft_info = nft.to_json_dict()
        assert nft_info["owner_did"][2:] == hex_did_id_1
