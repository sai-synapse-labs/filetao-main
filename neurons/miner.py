# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2024 Synapse Labs Corp.

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import os
import sys
import json
import time
import torch
import typing
import base64
import asyncio
import threading
import traceback
import bittensor as bt
from typing import Dict
from redis import asyncio as aioredis

from pprint import pformat

# import this repo
import storage
from storage.shared.ecc import (
    hash_data,
    ECCommitment,
    ecc_point_to_hex,
    hex_to_ecc_point,
)

from storage.shared.utils import (
    b64_encode,
    chunk_data,
    safe_key_search,
    get_redis_password,
)

from storage.shared.checks import check_environment, check_registration

from storage.miner import (
    run,
)

from storage.miner.utils import (
    compute_subsequent_commitment,
    save_data_to_filesystem,
    load_from_filesystem,
    commit_data_with_seed,
    init_wandb,
    update_storage_stats,
    load_request_log,
    log_request,
    RateLimiter,
    get_purge_ttl_script_path,
)

from storage.miner.config import (
    config,
    check_config,
    add_args,
)

from storage.miner.database import (
    store_chunk_metadata,
    update_seed_info,
    get_chunk_metadata,
    get_filepath,
    store_or_update_chunk_metadata,
)


class miner:
    @classmethod
    def check_config(cls, config: "bt.Config"):
        """
        Adds neuron-specific arguments to the argument parser.

        Args:
            parser (argparse.ArgumentParser): Parser to add arguments to.

        This class method enriches the argument parser with options specific to the neuron's configuration.
        """
        check_config(cls, config)

    @classmethod
    def add_args(cls, parser):
        """
        Adds neuron-specific arguments to the argument parser.

        Args:
            parser (argparse.ArgumentParser): Parser to add arguments to.

        This class method enriches the argument parser with options specific to the neuron's configuration.
        """
        add_args(cls, parser)

    @classmethod
    def config(cls):
        """
        Retrieves the configuration for the neuron.

        Returns:
            bt.Config: The configuration object for the neuron.

        This class method returns the neuron's configuration, which is used throughout the neuron's lifecycle
        for various functionalities and operations.
        """
        return config(cls)

    subtensor: "bt.subtensor"
    wallet: "bt.wallet"
    metagraph: "bt.metagraph"

    def __init__(self):
        self.config = miner.config()
        self.check_config(self.config)
        bt.logging(config=self.config, logging_dir=self.config.miner.full_path)
        bt.logging.info(f"{self.config}")

        redis_password = get_redis_password(self.config.database.redis_password)
        if self.config.database.native_environment_check:
            try:
                asyncio.run(check_environment(
                    self.config.database.redis_conf_path,
                    self.config.database.host,
                    self.config.database.port,
                    redis_password
                ))
            except AssertionError as e:
                bt.logging.warning(
                    f"Something is missing in your environment: {e}. Please check your configuration, use the README for help, and try again."
                )
        else:
            bt.logging.info("Skipping environment checks.")

        bt.logging.info("miner.__init__()")

        # Init device.
        bt.logging.debug("loading device")
        self.device = torch.device(self.config.miner.device)
        bt.logging.debug(str(self.device))

        # Init subtensor
        bt.logging.debug("loading subtensor")
        self.subtensor = bt.subtensor(config=self.config)
        bt.logging.debug(str(self.subtensor))
        self.current_block = self.subtensor.get_current_block()

        # Init wallet.
        bt.logging.debug("loading wallet")
        self.wallet = bt.wallet(config=self.config)
        self.wallet.create_if_non_existent()
        if not self.config.wallet._mock:
            check_registration(self.subtensor, self.wallet, self.config.netuid)

        bt.logging.debug(f"wallet: {str(self.wallet)}")

        # Init metagraph.
        bt.logging.debug("loading metagraph")
        self.metagraph = bt.metagraph(
            netuid=self.config.netuid, network=self.subtensor.network, sync=False
        )  # Make sure not to sync without passing subtensor
        self.metagraph.sync(subtensor=self.subtensor)  # Sync metagraph with subtensor.
        bt.logging.debug(str(self.metagraph))

        # Setup database
        bt.logging.info("loading database")
        self.database = aioredis.StrictRedis(
            host=self.config.database.host,
            port=self.config.database.port,
            db=self.config.database.index,
            socket_keepalive=True,
            socket_connect_timeout=300,
            password=redis_password,
        )
        self.purge_ttl_path = get_purge_ttl_script_path(
            os.path.dirname(os.path.abspath(__file__))
        )

        self.my_subnet_uid = self.metagraph.hotkeys.index(
            self.wallet.hotkey.ss58_address
        )
        bt.logging.info(f"Running miner on uid: {self.my_subnet_uid}")

        # Init wandb.
        if not self.config.wandb.off:
            bt.logging.debug("loading wandb")
            init_wandb(self)

        # The axon handles request processing, allowing validators to send this process requests.
        self.axon = bt.axon(wallet=self.wallet, config=self.config)
        bt.logging.info(f"Axon {self.axon}")

        # Attach determiners which functions are called when servicing a request.
        bt.logging.info("Attaching forward functions to axon.")
        self.axon.attach(
            forward_fn=self.store,
            blacklist_fn=self.store_blacklist_fn,
            priority_fn=self.store_priority_fn,
        ).attach(
            forward_fn=self.challenge,
            blacklist_fn=self.challenge_blacklist_fn,
            priority_fn=self.challenge_priority_fn,
        ).attach(
            forward_fn=self.retrieve,
            blacklist_fn=self.retrieve_blacklist_fn,
            priority_fn=self.retrieve_priority_fn,
        )

        # Serve passes the axon information to the network + netuid we are hosting on.
        # This will auto-update if the axon port of external ip have changed.
        bt.logging.info(
            f"Serving axon {self.axon} on network: {self.subtensor.chain_endpoint} with netuid: {self.config.netuid}"
        )
        self.axon.serve(netuid=self.config.netuid, subtensor=self.subtensor)

        # Start  starts the miner's axon, making it active on the network.
        bt.logging.info(f"Starting axon server on port: {self.config.axon.port}")
        self.axon.start()

        # Init the event loop.
        self.loop = asyncio.get_event_loop()

        # Instantiate runners
        self.should_exit: bool = False
        self.is_running: bool = False
        self.thread: threading.Thread = None
        self.lock = asyncio.Lock()
        self.request_timestamps: Dict = {}

        self.step = 0

        # Init the miner's storage request tracker
        self.request_count = 0
        self.start_request_count_timer()
        self.requests_per_hour = []
        self.average_requests_per_hour = 0

        # Init the miner's storage usage tracker
        update_storage_stats(self)

        self.rate_limiters = {}
        self.request_log = load_request_log(self.config.miner.request_log_path)

    def start_request_count_timer(self):
        """
        Initializes and starts a timer for tracking the number of requests received by the miner in an hour.

        This method sets up a one-hour timer that, upon expiration, calls the `reset_request_count` method to log
        the number of requests received and reset the count for the next hour. The timer is set to run in a separate
        thread to avoid blocking the main execution.

        Usage:
            Should be called during the initialization of the miner to start tracking requests per hour.
        """
        self.request_count_timer = threading.Timer(3600, self.reset_request_count)
        self.request_count_timer.start()

    def reset_request_count(self):
        """
        Logs the number of requests received in the last hour and resets the count.

        This method is automatically called when the one-hour timer set by `start_request_count_timer` expires.
        It logs the count of requests received in the last hour and then resets the count. Additionally, it
        restarts the timer for the next hour.

        Usage:
            This method is intended to be called automatically by a timer and typically should not be called directly.
        """
        bt.logging.info(
            f"Number of requests received in the last hour: {self.request_count}"
        )
        self.requests_per_hour.append(self.request_count)
        bt.logging.info(f"Requests per hour: {self.requests_per_hour}")
        self.average_requests_per_hour = sum(self.requests_per_hour) / len(
            self.requests_per_hour
        )
        bt.logging.info(f"Average requests per hour: {self.average_requests_per_hour}")
        self.request_count = 0
        self.start_request_count_timer()

    @property
    async def total_storage(self):
        """
        Calculates the total size of data stored by the miner.

        This method fetches all data keys from the Redis database and sums up the size of each data object.
        It provides an estimate of the total amount of data currently held by the miner.

        Returns:
            int: Total size of data (in bytes) stored by the miner.

        Example:
            >>> miner.total_storage()
            102400  # Example output indicating 102,400 bytes of data stored
        """
        # Fetch all keys from Redis
        all_keys = await safe_key_search(self.database, "*")

        # Filter out keys that contain a period (temporary, remove later)
        filtered_keys = [key for key in all_keys if b"." not in key]
        total_size = 0
        for key in filtered_keys:
            try:
                key_dict = await self.database.hgetall(key)
                first_hotkey = list(key_dict)[0]
                size = int(json.loads(key_dict[first_hotkey]).get("size", 0))
            except Exception as e:
                size = 0
            total_size += size
        return total_size

    def store_blacklist_fn(
        self, synapse: storage.protocol.Store
    ) -> typing.Tuple[bool, str]:
        """
        Determines whether a given synapse should be blacklisted based on the recognition
        of the hotkey in the metagraph. This function is used to filter out requests from
        entities that are not part of the network's current state.

        Parameters:
        - synapse (bt.Synapse): The synapse object which contains the dendrite information
        including the hotkey.

        Returns:
        - (bool, str): A tuple where the first element is a boolean indicating whether the
        synapse's hotkey is blacklisted, and the second element is a string message explaining
        the reason.

        If the hotkey is not recognized in the metagraph, the synapse is blacklisted, and
        the function returns (True, "Unrecognized hotkey"). Otherwise, it returns (False,
        "Hotkey recognized!"), allowing the synapse to interact with the network.

        Usage:
        This method is internally used by the network to ensure that only recognized
        entities can participate in communication or transactions.
        """
        try:
            self.request_log = log_request(synapse, self.request_log)
        except Exception as e:
            bt.logging.error(f"Error logging request: {e}")

        caller = synapse.dendrite.hotkey
        if caller in self.config.blacklist.blacklist_hotkeys:
            return True, f"Hotkey {caller} in blacklist."
        elif caller in self.config.blacklist.whitelist_hotkeys:
            return False, f"Hotkey {caller} in whitelist."

        if caller not in self.rate_limiters:
            self.rate_limiters[caller] = RateLimiter(
                self.config.miner.max_requests_per_window,
                self.config.miner.rate_limit_window,
            )

        if not self.rate_limiters[caller].is_allowed(caller):
            window = self.config.miner.max_requests_per_window
            blocks = self.config.miner.rate_limit_window
            reason = f"Caller {caller} rate limited. Exceeded {window} requests in {blocks} blocks."
            return True, reason

        if caller not in self.metagraph.hotkeys:
            bt.logging.trace(f"Blacklisting unrecognized hotkey {caller}")
            return True, "Unrecognized hotkey"

        bt.logging.trace(f"Not Blacklisting recognized hotkey {caller}")
        return False, "Hotkey recognized!"

    def store_priority_fn(self, synapse: storage.protocol.Store) -> float:
        """
        Assigns a priority to a given synapse based on the stake of the calling entity
        in the metagraph. This function is crucial for prioritizing network requests
        and ensuring that higher-stake entities are given precedence in processing.

        Parameters:
        - synapse (bt.Synapse): The synapse object which contains the dendrite information
        including the hotkey of the caller.

        Returns:
        - float: The priority value assigned to the synapse, derived from the stake of
        the calling hotkey in the metagraph.

        The priority is determined by the stake associated with the caller's UID in the
        metagraph. A higher stake results in a higher priority.

        Usage:
        This method is used within the network's request handling mechanism to allocate
        resources and processing time based on the stake-based priority of each request.
        """
        caller_uid = self.metagraph.hotkeys.index(
            synapse.dendrite.hotkey
        )  # Get the caller index.
        prirority = float(
            self.metagraph.S[caller_uid]
        )  # Return the stake as the priority.
        bt.logging.trace(
            f"Prioritizing {synapse.dendrite.hotkey} with value: ", prirority
        )
        return prirority

    def challenge_blacklist_fn(
        self, synapse: storage.protocol.Challenge
    ) -> typing.Tuple[bool, str]:
        """
        Determines whether a given synapse should be blacklisted based on the recognition
        of the hotkey in the metagraph. This function is used to filter out requests from
        entities that are not part of the network's current state.

        Parameters:
        - synapse (bt.Synapse): The synapse object which contains the dendrite information
        including the hotkey.

        Returns:
        - (bool, str): A tuple where the first element is a boolean indicating whether the
        synapse's hotkey is blacklisted, and the second element is a string message explaining
        the reason.

        If the hotkey is not recognized in the metagraph, the synapse is blacklisted, and
        the function returns (True, "Unrecognized hotkey"). Otherwise, it returns (False,
        "Hotkey recognized!"), allowing the synapse to interact with the network.

        Usage:
        This method is internally used by the network to ensure that only recognized
        entities can participate in communication or transactions.
        """
        try:
            self.request_log = log_request(synapse, self.request_log)
        except Exception as e:
            bt.logging.error(f"Error logging request: {e}")

        caller = synapse.dendrite.hotkey
        if caller in self.config.blacklist.blacklist_hotkeys:
            return True, f"Hotkey {caller} in blacklist."
        elif caller in self.config.blacklist.whitelist_hotkeys:
            return False, f"Hotkey {caller} in whitelist."

        if caller not in self.rate_limiters:
            self.rate_limiters[caller] = RateLimiter(
                self.config.miner.max_requests_per_window,
                self.config.miner.rate_limit_window,
            )

        if not self.rate_limiters[caller].is_allowed(caller):
            window = self.config.miner.max_requests_per_window
            blocks = self.config.miner.rate_limit_window
            reason = f"Caller {caller} rate limited. Exceeded {window} requests in {blocks} blocks."
            return True, reason

        if caller not in self.metagraph.hotkeys:
            bt.logging.trace(f"Blacklisting unrecognized hotkey {caller}")
            return True, "Unrecognized hotkey"

        bt.logging.trace(f"Not Blacklisting recognized hotkey {caller}")
        return False, "Hotkey recognized!"

    def challenge_priority_fn(self, synapse: storage.protocol.Challenge) -> float:
        """
        Assigns a priority to a given synapse based on the stake of the calling entity
        in the metagraph. This function is crucial for prioritizing network requests
        and ensuring that higher-stake entities are given precedence in processing.

        Parameters:
        - synapse (bt.Synapse): The synapse object which contains the dendrite information
        including the hotkey of the caller.

        Returns:
        - float: The priority value assigned to the synapse, derived from the stake of
        the calling hotkey in the metagraph.

        The priority is determined by the stake associated with the caller's UID in the
        metagraph. A higher stake results in a higher priority.

        Usage:
        This method is used within the network's request handling mechanism to allocate
        resources and processing time based on the stake-based priority of each request.
        """
        caller_uid = self.metagraph.hotkeys.index(
            synapse.dendrite.hotkey
        )  # Get the caller index.
        prirority = float(
            self.metagraph.S[caller_uid]
        )  # Return the stake as the priority.
        bt.logging.trace(
            f"Prioritizing {synapse.dendrite.hotkey} with value: ", prirority
        )
        return prirority

    def retrieve_blacklist_fn(
        self, synapse: storage.protocol.Retrieve
    ) -> typing.Tuple[bool, str]:
        """
        Determines whether a given synapse should be blacklisted based on the recognition
        of the hotkey in the metagraph. This function is used to filter out requests from
        entities that are not part of the network's current state.

        Parameters:
        - synapse (bt.Synapse): The synapse object which contains the dendrite information
        including the hotkey.

        Returns:
        - (bool, str): A tuple where the first element is a boolean indicating whether the
        synapse's hotkey is blacklisted, and the second element is a string message explaining
        the reason.

        If the hotkey is not recognized in the metagraph, the synapse is blacklisted, and
        the function returns (True, "Unrecognized hotkey"). Otherwise, it returns (False,
        "Hotkey recognized!"), allowing the synapse to interact with the network.

        Usage:
        This method is internally used by the network to ensure that only recognized
        entities can participate in communication or transactions.
        """
        try:
            self.request_log = log_request(synapse, self.request_log)
        except Exception as e:
            bt.logging.error(f"Error logging request: {e}")

        caller = synapse.dendrite.hotkey
        if caller in self.config.blacklist.blacklist_hotkeys:
            return True, f"Hotkey {caller} in blacklist."
        elif caller in self.config.blacklist.whitelist_hotkeys:
            return False, f"Hotkey {caller} in whitelist."

        if caller not in self.rate_limiters:
            self.rate_limiters[caller] = RateLimiter(
                self.config.miner.max_requests_per_window,
                self.config.miner.rate_limit_window,
            )

        if not self.rate_limiters[caller].is_allowed(caller):
            window = self.config.miner.max_requests_per_window
            blocks = self.config.miner.rate_limit_window
            reason = f"Caller {caller} rate limited. Exceeded {window} requests in {blocks} blocks."
            return True, reason

        if caller not in self.metagraph.hotkeys:
            bt.logging.trace(f"Blacklisting unrecognized hotkey {caller}")
            return True, "Unrecognized hotkey"

        bt.logging.trace(f"Not Blacklisting recognized hotkey {caller}")
        return False, "Hotkey recognized!"

    def retrieve_priority_fn(self, synapse: storage.protocol.Retrieve) -> float:
        """
        Assigns a priority to a given synapse based on the stake of the calling entity
        in the metagraph. This function is crucial for prioritizing network requests
        and ensuring that higher-stake entities are given precedence in processing.

        Parameters:
        - synapse (bt.Synapse): The synapse object which contains the dendrite information
        including the hotkey of the caller.

        Returns:
        - float: The priority value assigned to the synapse, derived from the stake of
        the calling hotkey in the metagraph.

        The priority is determined by the stake associated with the caller's UID in the
        metagraph. A higher stake results in a higher priority.

        Usage:
        This method is used within the network's request handling mechanism to allocate
        resources and processing time based on the stake-based priority of each request.
        """
        caller_uid = self.metagraph.hotkeys.index(
            synapse.dendrite.hotkey
        )  # Get the caller index.
        prirority = float(
            self.metagraph.S[caller_uid]
        )  # Return the stake as the priority.
        bt.logging.trace(
            f"Prioritizing {synapse.dendrite.hotkey} with value: ", prirority
        )
        return prirority

    async def store(self, synapse: storage.protocol.Store) -> storage.protocol.Store:
        """
        Processes the storage request from a synapse by securely storing the provided data and returning
        a proof of storage. The data is committed using elliptic curve cryptography, stored on the filesystem,
        and the metadata is recorded in a Redis database. A cryptographic proof of the commitment, along with
        a digital signature from the server's hotkey, is returned in the synapse for verification by the requester.

        Args:
            synapse (storage.protocol.Store): An object containing the data to be stored,
            encoded in base64 format, along with associated metadata like the cryptographic
            curve parameters, a seed for the commitment, and the expected commitment group elements.

        Returns:
            storage.protocol.Store: The synapse is returned with additional fields populated,
            including the randomness used in the commitment, the commitment point itself, a signature
            from this storage server's hotkey, and a commitment hash that can be used for chained proofs.

        The method performs the following operations:
        1. Decodes the base64-encoded data into raw bytes.
        2. Commits to the data using the provided elliptic curve parameters and the seed to generate a commitment point.
        3. Stores the raw byte data in the filesystem using a hash of the data as the filename.
        4. Records metadata about the stored data in the Redis database, including the file path, previous seed, and data size.
        5. Updates the synapse object with the commitment details and a digital signature.

        This process ensures the integrity and non-repudiation of the data storage, allowing clients to verify
        that their data has been stored correctly without the need to retrieve the full data set.

        Example usage:
            Assuming an initialized 'committer' object and 'synapse' with necessary data:
            >>> updated_synapse = self.store(synapse)
        """
        bt.logging.info(f"received store request: {synapse.encrypted_data[:24]}")
        self.request_count += 1

        # Decode the data from base64 to raw bytes
        encrypted_byte_data = base64.b64decode(synapse.encrypted_data)
        bt.logging.trace(f"store b64decrypted data: {encrypted_byte_data[:24]}")

        # Store the data with the hash as the key in the filesystem
        bt.logging.trace("entering hash_data()")
        data_hash = hash_data(encrypted_byte_data)

        # If already storing this hash, simply update the validator seeds and return challenge
        bt.logging.trace("checking if data already exists...")
        
        if not await self.database.hexists(data_hash, synapse.dendrite.hotkey):
            # Store the data in the filesystem
            filepath = save_data_to_filesystem(
                encrypted_byte_data, self.config.database.directory, synapse.dendrite.hotkey, str(data_hash)
            )
            bt.logging.trace(f"stored data {data_hash} in filepath: {filepath}")
        else:
            filepath = await get_filepath(self.database, data_hash, synapse.dendrite.hotkey)

        # Add the initial chunk, size, and validator seed information
        # If data exists and is the same hotkey caller, overwrite prev seed, otherwise add a new entry
        await store_or_update_chunk_metadata(
            self.database,
            data_hash,
            filepath,
            synapse.dendrite.hotkey,
            sys.getsizeof(encrypted_byte_data),
            synapse.seed,
            synapse.ttl,
        )

        # Commit to the entire data block
        bt.logging.trace("entering ECCommitment()")
        committer = ECCommitment(
            hex_to_ecc_point(synapse.g, synapse.curve),
            hex_to_ecc_point(synapse.h, synapse.curve),
        )
        bt.logging.trace("entering commit()")
        c, m_val, r = committer.commit(encrypted_byte_data + str(synapse.seed).encode())
        if self.config.miner.verbose:
            bt.logging.debug(f"committer: {committer}")
            bt.logging.debug(f"encrypted_byte_data: {encrypted_byte_data}")
            bt.logging.debug(f"c: {c}")
            bt.logging.debug(f"m_val: {m_val}")
            bt.logging.debug(f"r: {r}")

        # Send back some proof that we stored the data
        synapse.randomness = r
        synapse.commitment = ecc_point_to_hex(c)
        bt.logging.trace(f"signed commitment: {synapse.commitment}")

        # Initialize the commitment hash with the initial commitment for chained proofs
        synapse.commitment_hash = str(m_val)
        bt.logging.trace(f"initial commitment_hash: {synapse.commitment_hash}")
        if self.config.miner.verbose:
            bt.logging.debug(f"signed m_val: {synapse.signature.hex()}")
            bt.logging.debug(f"type(seed): {type(synapse.seed)}")
            bt.logging.debug(f"initial commitment_hash: {synapse.commitment_hash}")

        bt.logging.info(
            f"stored data hash {data_hash} with commitment: {synapse.commitment}"
        )

        # Don't send data back, no need.
        synapse.encrypted_data = base64.b64encode(b"").decode()  # Empty b64 response
        return synapse

    async def challenge(
        self, synapse: storage.protocol.Challenge
    ) -> storage.protocol.Challenge:
        """
        Handles a data challenge by providing cryptographic proof of data possession. This method retrieves
        the specified data from storage, calculates its commitment using elliptic curve cryptography, and
        constructs a Merkle proof. The response includes the requested data chunk, Merkle proof, root, and
        the commitment, which collectively serve as verifiable evidence of data possession.

        Args:
            synapse (storage.protocol.Challenge): An object representing the challenge request, which includes
            parameters such as the hash of the data to retrieve, chunk size, challenge index, and elliptic
            curve parameters for commitment calculation.

        Returns:
            storage.protocol.Challenge: The synapse object is updated with the response to the challenge,
            including the encrypted data chunk, commitment point, Merkle proof, and root hash.

        The method performs the following steps:
        1. Fetches the encrypted data from storage using the hash provided in the challenge.
        2. Splits the data into chunks based on the specified chunk size.
        3. Computes a new commitment hash to provide a time-bound proof of possession.
        4. Generates a Merkle tree from the committed data chunks and extracts a proof for the requested chunk.
        5. Encodes the requested chunk and Merkle proof in base64 for transmission.
        6. Updates the challenge synapse with the commitment, data chunk, randomness, and Merkle proof.
        7. Records the updated commitment hash in storage for future challenges.

        This method ensures data integrity and allows the verification of data possession without disclosing the
        entire data set. It is designed to fulfill data verification requests in a secure and verifiable manner.

        Example usage:
            Assuming an initialized 'synapse' object with the challenge parameters:
            >>> updated_synapse = self.challenge(synapse)
        """
        # Retrieve the data itself from miner storage
        bt.logging.info(f"received challenge hash: {synapse.challenge_hash}")
        self.request_count += 1

        bt.logging.trace("entering get_chunk_metadata()")
        data = await get_chunk_metadata(
            r=self.database,
            chunk_hash=synapse.challenge_hash,
            hotkey=synapse.dendrite.hotkey,
        )
        if data is None:
            bt.logging.error(f"No data found for {synapse.challenge_hash}")
            synapse.axon.status_code = 404
            synapse.axon.status_message = "File metadata not found"
            return synapse

        bt.logging.trace(f"retrieved data: {pformat(data)}")

        # Chunk the data according to the specified (random) chunk size
        filepath = data.get("filepath", None)
        if filepath is None:
            # fallback to load the data from the filesystem via database path construction
            # use new path construction by hotkey and challenge hash
            file_not_found = True
            new_filepath = os.path.expanduser(
                f"{self.config.database.directory}/{synapse.dendrite.hotkey}/{synapse.challenge_hash}"
            )
            if os.path.isfile(new_filepath):
                file_not_found = False
                filepath = new_filepath

            if file_not_found:
                # Use old path construction by just challenge hash
                fallback_filepath = os.path.expanduser(
                    f"{self.config.database.directory}/{synapse.challenge_hash}"
                )
                if not os.path.isfile(fallback_filepath):
                    bt.logging.error(
                        f"challenge() No file found for {synapse.challenge_hash} in {fallback_filepath}."
                    )
                    synapse.axon.status_code = 404
                    synapse.axon.status_message = "File not found"
                    return synapse
                else:
                    filepath = fallback_filepath
                    bt.logging.debug(
                        f"challenge() File found for {synapse.challenge_hash} in {filepath}."
                    )

        bt.logging.trace("entering load_from_filesystem()")
        try:
            encrypted_data_bytes = load_from_filesystem(filepath)
        except Exception as e:
            bt.logging.error(f"Error loading file {filepath}: {e}")
            synapse.axon.status_code = 404
            synapse.axon.status_message = "File not found"
            return synapse

        # Construct the next commitment hash using previous commitment and hash
        # of the data to prove storage over time
        prev_seed = data.get("seed", "").encode()
        bt.logging.debug(f"challenge() prev_seed: {prev_seed}")
        if prev_seed is None:
            bt.logging.error(f"No seed found for {synapse.challenge_hash}")
            synapse.axon.status_code = 404
            synapse.axon.status_message = "Previous seed not found"
            return synapse

        bt.logging.trace("entering comput_subsequent_commitment()...")
        new_seed = synapse.seed.encode()
        next_commitment, proof = compute_subsequent_commitment(
            encrypted_data_bytes, prev_seed, new_seed, verbose=self.config.miner.verbose
        )
        if self.config.miner.verbose:
            bt.logging.debug(f"prev seed : {prev_seed}")
            bt.logging.debug(f"new seed  : {new_seed}")
            bt.logging.debug(f"proof     : {proof}")
            bt.logging.debug(f"commitment: {next_commitment}\n")
        synapse.commitment_hash = next_commitment
        synapse.commitment_proof = proof

        # update the commitment seed challenge hash in storage
        bt.logging.trace(f"updating challenge miner storage: {pformat(data)}")
        await update_seed_info(
            self.database,
            chunk_hash=synapse.challenge_hash,
            hotkey=synapse.dendrite.hotkey,
            seed=new_seed.decode("utf-8"),
        )

        # Chunk the data according to the provided chunk_size
        bt.logging.trace("entering chunk_data()")
        data_chunks = chunk_data(encrypted_data_bytes, synapse.chunk_size)

        # Extract setup params
        g = hex_to_ecc_point(synapse.g, synapse.curve)
        h = hex_to_ecc_point(synapse.h, synapse.curve)

        # Commit the data chunks based on the provided curve points
        bt.logging.trace("entering ECCcommitment()")
        committer = ECCommitment(g, h)
        bt.logging.trace("entering commit_data_with_seed()")
        randomness, chunks, commitments, merkle_tree = commit_data_with_seed(
            committer,
            data_chunks=data_chunks,
            n_chunks=sys.getsizeof(encrypted_data_bytes) // synapse.chunk_size + 1,
            seed=synapse.seed,
        )

        # Prepare return values to validator
        bt.logging.trace("entering b64_encode()")
        synapse.commitment = commitments[synapse.challenge_index]
        synapse.data_chunk = base64.b64encode(chunks[synapse.challenge_index])
        synapse.randomness = randomness[synapse.challenge_index]
        synapse.merkle_proof = b64_encode(
            merkle_tree.get_proof(synapse.challenge_index)
        )

        bt.logging.trace("getting merkle root...")
        synapse.merkle_root = merkle_tree.get_merkle_root()

        if self.config.miner.verbose:
            bt.logging.debug(f"commitment: {str(synapse.commitment)[:24]}")
            bt.logging.debug(f"randomness: {str(synapse.randomness)[:24]}")
            bt.logging.debug(f"merkle_proof: {str(synapse.merkle_proof[:24])}")
            bt.logging.debug(f"merkle_root: {str(synapse.merkle_root)[:24]}")

        bt.logging.info(f"returning challenge data {synapse.data_chunk[:24]}...")
        return synapse

    async def retrieve(
        self, synapse: storage.protocol.Retrieve
    ) -> storage.protocol.Retrieve:
        """
        Retrieves the encrypted data associated with a specific hash from the storage system and
        validates the miner's continuous possession of the data. The method fetches the data's
        metadata from a Redis database, loads the encrypted data from the filesystem, and
        performs a cryptographic challenge-response to ensure the data's integrity and availability.

        Args:
            synapse (storage.protocol.Retrieve): A Retrieve synapse object that includes the hash of
            the data to be retrieved and a seed for the cryptographic challenge.

        Returns:
            storage.protocol.Retrieve: The synapse object is updated with the retrieved encrypted data
            encoded in base64 and a commitment hash that serves as a proof of retrieval.

        The method executes the following operations:
        1. Retrieves the metadata associated with the data hash from the Redis database.
        2. Loads the encrypted data from the filesystem based on the filepath specified in the metadata.
        3. Computes a new commitment using the previous seed from the metadata and the new seed from
        the synapse, which serves as a proof for the continuous possession of the data.
        4. Updates the metadata with the new seed and re-stores it in the database to prepare for future retrievals.
        5. Encodes the encrypted data in base64 and attaches it to the synapse for return.

        This retrieval process is vital for data validation in decentralized storage systems, as it
        demonstrates not only possession but also the ability to return the data upon request, which
        is crucial for maintaining integrity and trust in the system.

        Example usage:
            Assuming an initialized 'synapse' with a data hash and seed:
            >>> updated_synapse = self.retrieve(synapse)
        """
        bt.logging.info(f"received retrieve hash: {synapse.data_hash}")
        self.request_count += 1

        # Fetch the data from the miner database
        bt.logging.trace("entering get_chunk_metadata()")
        data = await get_chunk_metadata(
            r=self.database, chunk_hash=synapse.data_hash, hotkey=synapse.dendrite.hotkey
        )

        # Decode the data + metadata from bytes to json
        bt.logging.debug(f"retrieved data: {pformat(data)}")

        # Get the data from filesystem to retrieve
        filepath = data.get("filepath", None)
        if filepath is None:
            # fallback to load the data from the filesystem via database path construction
            # use new path construction by hotkey and retrieve hash
            file_not_found = True
            new_filepath = os.path.expanduser(
                f"{self.config.database.directory}/{synapse.dendrite.hotkey}/{synapse.data_hash}"
            )
            if os.path.isfile(new_filepath):
                file_not_found = False
                filepath = new_filepath

            if file_not_found:
                # Use old path construction by just retrieve hash
                fallback_filepath = os.path.expanduser(
                    f"{self.config.database.directory}/{synapse.data_hash}"
                )
                if not os.path.isfile(fallback_filepath):
                    bt.logging.error(
                        f"retrieve() No file found for {synapse.data_hash} in {fallback_filepath}."
                    )
                    synapse.axon.status_code = 404
                    synapse.axon.status_message = "File not found"
                    return synapse
                else:
                    filepath = fallback_filepath
                    bt.logging.debug(
                        f"retrieve() File found for {synapse.data_hash} in {filepath}."
                    )

        bt.logging.trace("entering load_from_filesystem()")
        try:
            encrypted_data_bytes = load_from_filesystem(filepath)
        except Exception as e:
            bt.logging.error(f"Error loading file {filepath}: {e}")
            synapse.axon.status_code = 404
            synapse.axon.status_message = "File not found"
            return synapse

        # incorporate a final seed challenge to verify they still have the data at retrieval time
        bt.logging.trace("entering compute_subsequent_commitment()")
        commitment, proof = compute_subsequent_commitment(
            encrypted_data_bytes,
            previous_seed=data.get("seed", "").encode(),
            new_seed=synapse.seed.encode(),
            verbose=self.config.miner.verbose,
        )
        synapse.commitment_hash = commitment
        synapse.commitment_proof = proof

        # store new seed
        bt.logging.trace("entering update_seed_info()")
        await update_seed_info(
            self.database,
            chunk_hash=synapse.data_hash,
            hotkey=synapse.dendrite.hotkey,
            seed=synapse.seed,
        )
        bt.logging.debug(f"updated retrieve miner storage: {pformat(data)}")

        # Return base64 data
        bt.logging.trace("entering b64_encode()")
        synapse.data = base64.b64encode(encrypted_data_bytes)
        bt.logging.info(f"returning retrieved data {synapse.data[:24]}...")
        return synapse

    def run(self):
        run(self)

    def run_in_background_thread(self):
        """
        Starts the miner's operations in a separate background thread.
        This is useful for non-blocking operations.
        """
        if not self.is_running:
            bt.logging.debug("Starting miner in background thread.")
            self.should_exit = False
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
            self.is_running = True
            bt.logging.debug("Started")

    def stop_run_thread(self):
        """
        Stops the miner's operations that are running in the background thread.
        """
        if self.is_running:
            bt.logging.debug("Stopping miner in background thread.")
            self.should_exit = True
            self.thread.join(5)
            self.is_running = False
            bt.logging.debug("Stopped")

    def __enter__(self):
        """
        Starts the miner's operations in a background thread upon entering the context.
        This method facilitates the use of the miner in a 'with' statement.
        """
        self.run_in_background_thread()

    def __exit__(self, exc_type, exc_value, traceback):
        """
        Stops the miner's background operations upon exiting the context.
        This method facilitates the use of the miner in a 'with' statement.

        Args:
            exc_type: The type of the exception that caused the context to be exited.
                      None if the context was exited without an exception.
            exc_value: The instance of the exception that caused the context to be exited.
                       None if the context was exited without an exception.
            traceback: A traceback object encoding the stack trace.
                       None if the context was exited without an exception.
        """
        self.stop_run_thread()


def run_miner():
    """
    Main function to run the neuron.

    This function initializes and runs the neuron. It handles the main loop, state management, and interaction
    with the Bittensor network.
    """

    miner().run_in_background_thread()

    try:
        while 1:
            time.sleep(1)
    except KeyboardInterrupt:
        bt.logging.info("Keyboard interrupt detected, exiting.")
        sys.exit(0)
    except Exception as e:
        bt.logging.error(traceback.format_exc())
        bt.logging.error(f"Unhandled exception: {e}")
        sys.exit(1)


if __name__ == "__main__":
    run_miner()
