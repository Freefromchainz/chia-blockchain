import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Awaitable
import aiosqlite
import traceback
import asyncio
from chia.data_layer.data_layer_types import InternalNode, TerminalNode, DownloadMode, Subscription, Root, Status
from chia.data_layer.data_store import DataStore
from chia.rpc.wallet_rpc_client import WalletRpcClient
from chia.server.server import ChiaServer
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.config import load_config
from chia.util.db_wrapper import DBWrapper
from chia.util.ints import uint32, uint64, uint16
from chia.util.path import mkdir, path_from_root
from chia.wallet.transaction_record import TransactionRecord
from chia.data_layer.data_layer_wallet import SingletonRecord
from chia.data_layer.download_data import download_data


class DataLayer:
    data_store: DataStore
    db_wrapper: DBWrapper
    db_path: Path
    connection: Optional[aiosqlite.Connection]
    config: Dict[str, Any]
    log: logging.Logger
    wallet_rpc_init: Awaitable[WalletRpcClient]
    state_changed_callback: Optional[Callable[..., object]]
    wallet_id: uint64
    initialized: bool

    def __init__(
        self,
        root_path: Path,
        wallet_rpc_init: Awaitable[WalletRpcClient],
        name: Optional[str] = None,
    ):
        if name == "":
            # TODO: If no code depends on "" counting as 'unspecified' then we do not
            #       need this.
            name = None
        config = load_config(root_path, "config.yaml", "data_layer")
        self.initialized = False
        self.config = config
        self.connection = None
        self.wallet_rpc_init = wallet_rpc_init
        self.log = logging.getLogger(name if name is None else __name__)
        self._shut_down: bool = False
        db_path_replaced: str = config["database_path"].replace("CHALLENGE", config["selected_network"])
        self.db_path = path_from_root(root_path, db_path_replaced)
        mkdir(self.db_path.parent)

    def _set_state_changed_callback(self, callback: Callable[..., object]) -> None:
        self.state_changed_callback = callback

    def set_server(self, server: ChiaServer) -> None:
        self.server = server

    async def _start(self) -> bool:
        self.connection = await aiosqlite.connect(self.db_path)
        self.db_wrapper = DBWrapper(self.connection)
        self.data_store = await DataStore.create(self.db_wrapper)
        self.wallet_rpc = await self.wallet_rpc_init
        self.periodically_fetch_data_task: asyncio.Task[Any] = asyncio.create_task(self.periodically_fetch_data())
        self.subscription_lock: asyncio.Lock = asyncio.Lock()
        return True

    def _close(self) -> None:
        # TODO: review for anything else we need to do here
        self._shut_down = True
        self.periodically_fetch_data_task.cancel()

    async def _await_closed(self) -> None:
        if self.connection is not None:
            await self.connection.close()

    async def create_store(
        self, fee: uint64, root: bytes32 = bytes32([0] * 32)
    ) -> Tuple[List[TransactionRecord], bytes32]:
        txs, tree_id = await self.wallet_rpc.create_new_dl(root, fee)
        res = await self.data_store.create_tree(tree_id=tree_id)
        if res is None:
            self.log.fatal("failed creating store")
        self.initialized = True
        return txs, tree_id

    async def batch_update(
        self,
        tree_id: bytes32,
        changelist: List[Dict[str, Any]],
        fee: uint64,
    ) -> TransactionRecord:
        for change in changelist:
            if change["action"] == "insert":
                key = change["key"]
                value = change["value"]
                reference_node_hash = change.get("reference_node_hash")
                side = change.get("side")
                if reference_node_hash or side:
                    await self.data_store.insert(key, value, tree_id, reference_node_hash, side)
                await self.data_store.autoinsert(key, value, tree_id)
            else:
                assert change["action"] == "delete"
                key = change["key"]
                await self.data_store.delete(key, tree_id)

        await self.data_store.get_tree_root(tree_id)
        root = await self.data_store.get_tree_root(tree_id)
        # todo return empty node hash from get_tree_root
        if root.node_hash is not None:
            node_hash = root.node_hash
        else:
            node_hash = bytes32([0] * 32)  # todo change
        transaction_record = await self.wallet_rpc.dl_update_root(tree_id, node_hash, fee)
        assert transaction_record
        # todo register callback to change status in data store
        # await self.data_store.change_root_status(root, Status.COMMITTED)
        return transaction_record

    async def get_value(self, store_id: bytes32, key: bytes) -> Optional[bytes]:
        res = await self.data_store.get_node_by_key(tree_id=store_id, key=key)
        if res is None:
            self.log.error("Failed to fetch key")
            return None
        return res.value

    async def get_keys_values(self, store_id: bytes32, root_hash: Optional[bytes32]) -> List[TerminalNode]:
        res = await self.data_store.get_keys_values(store_id, root_hash)
        if res is None:
            self.log.error("Failed to fetch keys values")
        return res

    async def get_ancestors(self, node_hash: bytes32, store_id: bytes32) -> List[InternalNode]:
        res = await self.data_store.get_ancestors(node_hash=node_hash, tree_id=store_id)
        if res is None:
            self.log.error("Failed to get ancestors")
        return res

    async def get_root(self, store_id: bytes32) -> Tuple[Optional[bytes32], Status]:
        latest = await self.wallet_rpc.dl_latest_singleton(store_id, True)
        if latest is None:
            self.log.error(f"Failed to get root for {store_id.hex()}")
            return None, Status.PENDING
        return latest.root, Status.COMMITTED

    async def _validate_batch(
        self,
        tree_id: bytes32,
        to_check: List[SingletonRecord],
        min_generation: int,
        max_generation: int,
    ) -> bool:
        last_checked_hash: Optional[bytes32] = None
        for record in to_check:
            # Ignore two consecutive identical root hashes, as we've already validated it.
            if last_checked_hash is not None and record.root == last_checked_hash:
                continue
            # Pick the latest root in our data store with the desired hash, before our already validated data.
            root: Optional[Root] = await self.data_store.get_last_tree_root_by_hash(
                tree_id, record.root, max_generation
            )
            if root is None or root.generation < min_generation:
                return False

            self.log.info(
                f"Validated chain hash {record.root} in downloaded datastore. "
                f"Wallet generation: {record.generation}"
            )
            max_generation = root.generation
            last_checked_hash = record.root

        return True

    async def fetch_and_validate(self, subscription: Subscription) -> None:
        tree_id = subscription.tree_id
        singleton_record: Optional[SingletonRecord] = await self.wallet_rpc.dl_latest_singleton(tree_id, True)
        if singleton_record is None:
            self.log.info(f"Fetch data: No singleton record for {tree_id}.")
            return
        if singleton_record.generation == uint32(0):
            self.log.info(f"Fetch data: No data on chain for {tree_id}.")
            return
        old_root: Optional[Root] = None
        try:
            old_root = await self.data_store.get_tree_root(tree_id=tree_id)
        except Exception:
            pass
        wallet_current_generation = await self.data_store.get_validated_wallet_generation(tree_id)
        assert int(wallet_current_generation) <= singleton_record.generation
        # Wallet generation didn't change, so no new data committed on chain.
        if wallet_current_generation is not None and uint32(wallet_current_generation) == singleton_record.generation:
            self.log.info(f"Fetch data: wallet generation matching on-chain generation: {tree_id}.")
            return
        to_check: List[SingletonRecord] = []
        if subscription.mode is DownloadMode.LATEST:
            to_check = [singleton_record]
        if subscription.mode is DownloadMode.HISTORY:
            to_check = await self.wallet_rpc.dl_history(
                launcher_id=tree_id, min_generation=uint32(wallet_current_generation + 1)
            )
        # No root hash changes in the new wallet records, so ignore.
        # TODO: wallet should handle identical hashes part?
        if (
            old_root is not None
            and old_root.node_hash is not None
            and to_check[0].root == old_root.node_hash
            and len(set(record.root for record in to_check)) == 1
        ):
            await self.data_store.set_validated_wallet_generation(tree_id, int(singleton_record.generation))
            self.log.info(
                f"Fetch data: fast-forwarded for {tree_id} as all on-chain hashes are identical to our root hash. "
                f"Current wallet generation saved: {int(singleton_record.generation)}"
            )
            return
        # Delete all identical root hashes to our old root hash, until we detect a change.
        if old_root is not None and old_root.node_hash is not None:
            while to_check[-1].root == old_root.node_hash:
                to_check.pop()

        self.log.info(
            f"Downloading and validating {subscription.tree_id}. "
            f"Current wallet generation: {int(wallet_current_generation)}. "
            f"Target wallet generation: {singleton_record.generation}."
        )

        downloaded = await download_data(self.data_store, subscription, singleton_record.root)
        if not downloaded:
            raise RuntimeError("Could not download the data.")
        self.log.info(f"Successfully downloaded data for {tree_id}.")

        root = await self.data_store.get_tree_root(tree_id=tree_id)
        # Wallet root hash must match to our data store root hash.
        if root.node_hash is not None and root.node_hash == to_check[0].root:
            self.log.info(
                f"Validated chain hash {root.node_hash} in downloaded datastore. "
                f"Wallet generation: {to_check[0].generation}"
            )
        else:
            raise RuntimeError("Can't find data on chain in our datastore.")
        to_check.pop(0)
        min_generation = (0 if old_root is None else old_root.generation) + 1
        max_generation = root.generation

        # Light validation: check the new set of operations against the new set of wallet records.
        # If this matches, we know all data will match, as we've previously checked that data matches
        # for `min_generation` data store root and `wallet_current_generation` wallet record.
        is_valid: bool = await self._validate_batch(tree_id, to_check, min_generation, max_generation)

        # If for some reason we have mismatched data using the light checks, recheck all history as a fallback.
        if not is_valid:
            self.log.warning(f"Light validation failed for {tree_id}. Validating all history.")
            to_check = await self.wallet_rpc.dl_history(launcher_id=tree_id, min_generation=uint32(1))
            # Already checked above.
            self.log.info(
                f"Validated chain hash {root.node_hash} in downloaded datastore. "
                f"Wallet generation: {to_check[0].generation}"
            )
            to_check.pop(0)
            is_valid = await self._validate_batch(tree_id, to_check, 0, max_generation)
            if not is_valid:
                raise RuntimeError("Could not validate on-chain data.")

        self.log.info(
            f"Finished downloading and validating {subscription.tree_id}. "
            f"Wallet generation saved: {singleton_record.generation}. "
            f"Root hash saved: {singleton_record.root}."
        )
        await self.data_store.set_validated_wallet_generation(tree_id, int(singleton_record.generation))

    async def subscribe(self, store_id: bytes32, mode: DownloadMode, ip: str, port: uint16) -> None:
        subscription = Subscription(store_id, mode, ip, port)
        subscriptions = await self.get_subscriptions()
        if subscription.tree_id in [subscription.tree_id for subscription in subscriptions]:
            return
        await self.wallet_rpc.dl_track_new(subscription.tree_id)
        async with self.subscription_lock:
            await self.data_store.subscribe(subscription)
        self.log.info(f"Subscribed to {subscription.tree_id}")

    async def unsubscribe(self, tree_id: bytes32) -> None:
        subscriptions = await self.get_subscriptions()
        if tree_id not in [subscription.tree_id for subscription in subscriptions]:
            return
        async with self.subscription_lock:
            await self.data_store.unsubscribe(tree_id)
        await self.wallet_rpc.dl_stop_tracking(tree_id)
        self.log.info(f"Unsubscribed to {tree_id}")

    async def get_subscriptions(self) -> List[Subscription]:
        async with self.subscription_lock:
            return await self.data_store.get_subscriptions()

    async def periodically_fetch_data(self) -> None:
        fetch_data_interval = self.config.get("fetch_data_interval", 60)
        while not self._shut_down:
            async with self.subscription_lock:
                subscriptions = await self.data_store.get_subscriptions()
                for subscription in subscriptions:
                    try:
                        await self.fetch_and_validate(subscription)
                    except Exception as e:
                        self.log.error(f"Exception while fetching data: {type(e)} {e} {traceback.format_exc()}.")
            try:
                await asyncio.sleep(fetch_data_interval)
            except asyncio.CancelledError:
                pass