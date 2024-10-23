# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2023 philanthrope
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
import json
import base64
import asyncio
import argparse

import storage
from storage.validator.encryption import encrypt_data
from storage.validator.cid import generate_cid_string
from storage.shared.ecc import hash_data
from storage.api.store_api import store
from storage.shared.utils import get_coldkey_wallets_for_path, get_hash_mapping, save_hash_mapping

import bittensor

from typing import List
from rich.prompt import Prompt
from storage.validator.utils import get_all_validators

from .default_values import defaults


bittensor.trace()


# Create a console instance for CLI display.
console = bittensor.__console__



class StoreData:
    """
    Executes the 'put' command to store data from the local disk on the Bittensor network.
    This command is essential for users who wish to upload and store data securely on the network.

    Usage:
    The command encrypts and sends the data located at the specified file path to the network.
    The data is encrypted using the wallet's private key, ensuring secure storage.
    After successful storage, a unique hash corresponding to the data is generated and saved,
    allowing for easy retrieval of the data in the future.

    This command is particularly useful for users looking to leverage the decentralized nature of the
    Bittensor network for secure data storage.

    Optional arguments:
    - --filepath (str): The path to the data file to be stored on the network.
    - --hash_basepath (str): The base path where hash files are stored. Defaults to '~/.bittensor/hashes'.
    - --stake_limit (float): The stake limit for excluding validator axons from the query.

    The resulting output includes:
    - Success or failure message regarding data storage.
    - The unique data hash generated upon successful storage.

    Example usage:
    >>> ftcli store put --filepath "/path/to/data.txt"

    Note:
    This command is vital for users who need to store data on the Bittensor network securely.
    It provides a streamlined process for encrypting and uploading data, with an emphasis on security and data integrity.
    """

    @staticmethod
    async def run(cli):
        r"""Store data from local disk on the Bittensor network."""

        wallet = bittensor.wallet(
            name=cli.config.wallet.name, hotkey=cli.config.wallet.hotkey
        )
        bittensor.logging.debug("wallet:", wallet)

        # Unlock the wallet
        if cli.config.encrypt:
            wallet.hotkey
            wallet.coldkey

        cli.config.filepath = os.path.expanduser(cli.config.filepath)
        if not os.path.exists(cli.config.filepath):
            bittensor.logging.error(
                "File does not exist: {}".format(cli.config.filepath)
            )
            return

        with open(cli.config.filepath, "rb") as f:
            raw_data = f.read()

        hash_basepath = os.path.expanduser(cli.config.hash_basepath)
        hash_filepath = os.path.join(hash_basepath, wallet.name + ".json")
        bittensor.logging.debug("store hashes path:", hash_filepath)

        try:
            sub = bittensor.subtensor(network=cli.config.subtensor.network)
            bittensor.logging.debug("subtensor:", sub)
            await StoreData._run(cli, raw_data, sub, wallet, hash_filepath)
        finally:
            if "sub" in locals():
                sub.close()
                bittensor.logging.debug("closing subtensor connection")

    @staticmethod
    async def _run(cli, raw_data: bytes, subtensor: "bittensor.subtensor", wallet: "bittensor.wallet", hash_filepath: str):
        r"""Store data from local disk on the Bittensor network."""

        success = False
        with bittensor.__console__.status(":satellite: Storing data..."):

            data_hash, stored_hotkeys = await store(
                    data=raw_data,
                    wallet=wallet,
                    subtensor=subtensor,
                    netuid=cli.config.netuid,
                    ttl=cli.config.ttl,
                    encrypt=cli.config.encrypt,
                    encoding=cli.config.encoding,
                    timeout=cli.config.timeout,
                    uid=cli.config.uid,
                )

            if len(stored_hotkeys) > 0:
                bittensor.logging.info(
                    f"Stored data with hotkeys: {stored_hotkeys}."
                )
                success = True

        if success:
            # Save hash mapping after successful storage
            filename = os.path.basename(cli.config.filepath)
            save_hash_mapping(hash_filepath, filename=filename, data_hash=data_hash, hotkeys=stored_hotkeys)
            bittensor.logging.info(
                f"Stored {filename} on the Bittensor network with CID {data_hash}"
            )
        else:
            bittensor.logging.error(f"Failed to store data at {cli.config.filepath}.")

    @staticmethod
    def check_config(config: "bittensor.config"):
        if not config.is_set("subtensor.network") and not config.no_prompt:
            network = Prompt.ask(
                "Enter subtensor network",
                default=defaults.subtensor.network,
                choices=["finney", "local", "test"],
            )
            config.subtensor.network = str(network)

        if not config.is_set("netuid") and not config.no_prompt:
            netuid = Prompt.ask(
                "Enter netuid",
                default=defaults.netuid
                if config.subtensor.network == "finney" or config.subtensor.network == "local"
                else "22",
            )
            config.netuid = str(netuid)

        if not config.is_set("wallet.name") and not config.no_prompt:
            wallet_name = Prompt.ask("Enter wallet name", default=defaults.wallet.name)
            config.wallet.name = str(wallet_name)

        if not config.is_set("wallet.hotkey") and not config.no_prompt:
            wallet_hotkey = Prompt.ask(
                "Enter wallet hotkey", default=defaults.wallet.hotkey
            )
            config.wallet.hotkey = str(wallet_hotkey)

        if not config.is_set("filepath") and not config.no_prompt:
            config.filepath = Prompt.ask(
                "Enter path to data you with to store on the Bittensor network",
            )

    @staticmethod
    def add_args(parser: argparse.ArgumentParser):
        store_parser = parser.add_parser(
            "put", help="""Store data on the Bittensor network."""
        )
        store_parser.add_argument(
            "--filepath",
            type=str,
            help="Path to data to store on the Bittensor network.",
        )
        store_parser.add_argument(
            "--hash_basepath",
            type=str,
            default=defaults.hash_basepath,
            help="Path to store hashes",
        )
        store_parser.add_argument(
            "--stake_limit",
            type=float,
            default=500,
            help="Stake limit to exclude validator axons to query.",
        )
        store_parser.add_argument(
            "--netuid",
            type=str,
            default=defaults.netuid,
            help="Network identifier for the Bittensor network.",
        )
        store_parser.add_argument(
            "--neuron.vpermit_tao_limit",
            type=int,
            default=500,
            help="Tao limit for the validator permit.",
        )
        store_parser.add_argument(
            "--encrypt",
            action="store_true",
            help="Encrypt the data before storing it on the Bittensor network with bittensor wallet coldkey.",
        )
        store_parser.add_argument(
            "--ttl",
            type=int,
            default=60 * 60 * 24 * 30,
            help="Time to live for the data on the Bittensor network. (Default 30 days)",
        )
        store_parser.add_argument(
            "--timeout",
            type=int,
            default=180,
            help="Timeout for the complete storage request on the Bittensor network.",
        )
        store_parser.add_argument(
            "--uid",
            type=int,
            help="UID of validator API to ping directly",
        )

        bittensor.wallet.add_args(store_parser)
        bittensor.subtensor.add_args(store_parser)
        bittensor.logging.add_args(store_parser)
