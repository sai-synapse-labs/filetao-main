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
import re
import json
import base64
import subprocess
import bittensor as bt
from typing import List, Union
from redis import asyncio as aioredis


def is_running_in_docker():
    if os.path.exists('/.dockerenv'):
        return True
    try:
        with open('/proc/self/cgroup', 'rt') as f:
            return any('docker' in line or 'kubepods' in line for line in f)
    except Exception:
        return False


async def safe_key_search(database: aioredis.Redis, pattern: str) -> List[str]:
    """
    Safely search for keys in the database that doesn't block.
    `scan_iter` uses cursor under the hood.
    """
    return [key async for key in database.scan_iter(pattern)]


def b64_encode(data: Union[bytes, str, List[str], List[bytes], dict]) -> str:
    """
    Encodes the given data into a base64 string. If the data is a list or dictionary of bytes, it converts
    the bytes into hexadecimal strings before encoding.

    Args:
        data (list or dict): The data to be base64 encoded. Can be a list of bytes or a dictionary with bytes values.

    Returns:
        str: The base64 encoded string of the input data.

    Raises:
        TypeError: If the input is not a list, dict, or bytes.
    """
    if isinstance(data, bytes):
        data = data.hex()
    if isinstance(data, list) and len(data) and isinstance(data[0], bytes):
        data = [d.hex() for d in data]
    if isinstance(data, dict) and isinstance(data[list(data.keys())[0]], bytes):
        data = {k: v.hex() for k, v in data.items()}
    return base64.b64encode(json.dumps(data).encode()).decode("utf-8")


def b64_decode(data: bytes, decode_hex: bool = False, encrypted: bool = False):
    """
    Decodes a base64 string into a list or dictionary. If decode_hex is True, it converts any hexadecimal strings
    within the data back into bytes.

    Args:
        data (bytes or str): The base64 encoded data to be decoded.
        decode_hex (bool): A flag to indicate whether to decode hex strings into bytes. Defaults to False.

    Returns:
        list or dict: The decoded data. Returns a list if the original encoded data was a list, and a dict if it was a dict.

    Raises:
        ValueError: If the input is not properly base64 encoded or if hex decoding fails.
    """
    data = data.decode("utf-8") if isinstance(data, bytes) else data
    decoded_data = json.loads(
        base64.b64decode(data) if encrypted else base64.b64decode(data).decode("utf-8")
    )
    if decode_hex:
        try:
            decoded_data = (
                [bytes.fromhex(d) for d in decoded_data]
                if isinstance(decoded_data, list)
                else {k: bytes.fromhex(v) for k, v in decoded_data.items()}
            )
        except:  # TODO: do not use bare except
            pass
    return decoded_data


def chunk_data(data: bytes, chunksize: int) -> List[bytes]:
    """
    Generator function that chunks the given data into pieces of a specified size.

    Args:
        data (bytes): The binary data to be chunked.
        chunksize (int): The size of each chunk in bytes.

    Yields:
        bytes: A chunk of the data with the size equal to 'chunksize' or the remaining size of data.

    Raises:
        ValueError: If 'chunksize' is less than or equal to 0.
    """
    for i in range(0, len(data), chunksize):
        yield data[i : i + chunksize]


def get_redis_port():
    """
    Gets the port number of the Redis server.

    Returns:
        str: The port number of the Redis server.

    Raises:
        CalledProcessError: If the command to get the Redis service status fails.
    """

    try:
        result = subprocess.check_output(
            ["sudo", "systemctl", "status", "redis-server.service"], text=True
        )
        match = re.search(r"(\d{1,3}\.){3}\d{1,3}:(\d+)", result)
        if match:
            return match.group(2)
        else:
            return "Redis server port not found in the service status."
    except subprocess.CalledProcessError as e:
        return "Failed to get Redis service status: " + str(e)


def get_redis_password(
    redis_password: str = None, redis_conf: str = "/etc/redis/redis.conf"
) -> str:
    redis_password = os.getenv("REDIS_PASSWORD") or redis_password
    if redis_password is None:
        try:
            cmd = ["grep", "-Po", "^requirepass \K.*", redis_conf] if is_running_in_docker() else ["sudo", "grep", "-Po", "^requirepass \K.*", redis_conf]
            redis_password = subprocess.check_output(
                cmd,
                text=True,
            ).strip()
        except Exception as e:
            bt.logging.warning(
                f"No Redis password set in Redis config file: {redis_conf}"
            )
    if redis_password == "" or redis_password is None:
        bt.logging.warning(
            "Redis password not found! This must be set as either an env var `REDIS_PASSWORD`, passed via CLI in `--database.redis_pasword`, or parsed from /etc/redis/redis.conf."
            "Please ensure it is set by running `. ./scripts/redis/set_redis_password.sh` and try again."
            f"You may also run: `sudo grep -Po '^requirepass \K.*' {redis_conf}` to discover this manually and pass to the cli."
        )
        return None

    return redis_password


def list_all_hashes(hash_file):
    try:
        with open(os.path.expanduser(hash_file), "r") as file:
            hashes = json.load(file)
            return hashes
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_coldkey_wallets_for_path(path: str) -> List["bt.wallet"]:
    try:
        wallet_names = next(os.walk(os.path.expanduser(path)))[1]
        return [bt.wallet(path=path, name=name) for name in wallet_names]
    except StopIteration:
        # No wallet files found.
        wallets = []
    return wallets


def get_hash_mapping(hash_file, filename):
    try:
        with open(os.path.expanduser(hash_file), "r") as file:
            hashes = json.load(file)
            return hashes.get(filename)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

    try:
        with open(os.path.expanduser(hash_file), "r") as file:
            hashes = json.load(file)
            return hashes.get(filename)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_hash_mapping(hash_file: str, filename: str, data_hash: str, hotkeys: List[str]):
    base_dir = os.path.dirname(hash_file)
    if not os.path.exists(base_dir):
        os.makedirs(base_dir)

    try:
        with open(hash_file, "r") as file:
            hashes = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        hashes = {}

    hashes[filename] = data_hash
    hashes[filename + "_hotkeys"] = hotkeys

    with open(hash_file, "w") as file:
        json.dump(hashes, file)
