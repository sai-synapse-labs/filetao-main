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

import json
import time
from redis import asyncio as aioredis
import asyncio
import bittensor as bt
from typing import Dict, List, Any, Union, Optional, Tuple


async def set_ttl_for_hash_and_hotkey(
    data_hash: str,
    ss58_address: str,
    database: aioredis.Redis,
    ttl: int = 60 * 60 * 24 * 30,
):
    """
    Sets the TTL for a hash in Redis.

    Parameters:
        data_hash (str): The key representing the hash.
        database (aioredis.Redis): The Redis client instance.
        ttl (int): The TTL in seconds. (Default 30 days)
    """
    key = f"hotkey:{ss58_address}"
    ttl_metadata = {
        "generated": time.time(),  # This is required to compare against later
        "ttl": ttl,
    }
    ttl_metadata_json = json.dumps(ttl_metadata)
    await database.hset(key, f"ttl:{data_hash}", ttl_metadata_json)
    bt.logging.trace(f"Set TTL for {data_hash} to {ttl} seconds.")


async def get_ttl_for_hash_and_hotkey(
    data_hash: str, ss58_address: str, database: aioredis.Redis
) -> int:
    """
    Retrieves the TTL for a hash in Redis.

    Parameters:
        data_hash (str): The key representing the hash.
        database (aioredis.Redis): The Redis client instance.

    Returns:
        The TTL in seconds, or None if not found.
    """
    key = f"hotkey:{ss58_address}"
    ttl_metadata = await database.hget(key, f"ttl:{data_hash}")
    if ttl_metadata:
        ttl_metadata = json.loads(ttl_metadata)
        ttl = int(ttl_metadata["ttl"])
        return ttl
    else:
        return None


async def get_time_since_generation_for_hash_and_hotkey(
    data_hash: str, ss58_address: str, database: aioredis.Redis
) -> Optional[int]:
    """
    Retrieves the TTL for a hash in Redis.

    Parameters:
        data_hash (str): The key representing the hash.
        database (aioredis.Redis): The Redis client instance.

    Returns:
        The TTL in seconds, or None if not found.
    """
    key = f"hotkey:{ss58_address}"
    ttl_metadata = await database.hget(key, f"ttl:{data_hash}")
    if ttl_metadata:
        ttl_metadata = json.loads(ttl_metadata)
        generated = float(ttl_metadata["generated"])
        return time.time() - generated
    else:
        return None


async def is_ttl_expired_for_hash_and_hotkey(
    data_hash: str, ss58_address: str, database: aioredis.Redis
) -> bool:
    """
    Checks if the TTL for a hash has expired.

    Parameters:
        data_hash (str): The key representing the hash.
        database (aioredis.Redis): The Redis client instance.

    Returns:
        True if the TTL has expired, False otherwise.
    """
    key = f"hotkey:{ss58_address}"
    elapsed = await get_time_since_generation_for_hash_and_hotkey(
        data_hash, ss58_address, database
    )
    ttl = await get_ttl_for_hash_and_hotkey(data_hash, ss58_address, database)
    if elapsed > ttl:
        return True
    else:
        return False


async def purge_expired_ttl_keys(database: aioredis.Redis):
    """
    Purges all expired TTL keys from the database.

    Parameters:
        database (aioredis.Redis): The Redis client instance.
    """
    # Iterate over all hotkeys
    async for hotkey in database.scan_iter("*"):
        if not hotkey.startswith(b"hotkey:"):
            continue
        data_hashes = await database.hgetall(hotkey)
        for data_hash in data_hashes:
            data_hash = data_hash.decode("utf-8")
            if data_hash.startswith("ttl:"):
                hk = hotkey.decode("utf-8")[7:]
                hs = data_hash[4:]
                if await is_ttl_expired_for_hash_and_hotkey(hs, hk, database):
                    await remove_metadata_from_hotkey(hk, hs, database)


async def add_metadata_to_hotkey(
    ss58_address: str,
    data_hash: str,
    metadata: Dict,
    database: aioredis.Redis,
    ttl: Optional[int] = None,
):
    """
    Associates a data hash and its metadata with a hotkey in Redis.

    Parameters:
        ss58_address (str): The primary key representing the hotkey.
        data_hash (str): The subkey representing the data hash.
        metadata (dict): The metadata to associate with the data hash. Includes the size of the data, the seed,
            and the encryption payload. E.g. {'size': 123, 'seed': 456, 'encryption_payload': 'abc'}.
        database (aioredis.Redis): The Redis client instance.
    """
    # Serialize the metadata as a JSON string
    metadata_json = json.dumps(metadata)
    # Use HSET to associate the data hash with the hotkey
    key = f"hotkey:{ss58_address}"
    await database.hset(key, data_hash, metadata_json)
    bt.logging.trace(f"Associated data hash {data_hash} with hotkey {ss58_address}.")

    if ttl:
        await set_ttl_for_hash_and_hotkey(data_hash, ss58_address, database, ttl)


async def remove_metadata_from_hotkey(
    ss58_address: str, data_hash: str, database: aioredis.Redis
):
    """
    Removes a data hash and its metadata from a hotkey in Redis.

    Parameters:
        ss58_address (str): The primary key representing the hotkey.
        data_hash (str): The subkey representing the data hash.
        database (aioredis.Redis): The Redis client instance.
    """
    # Use HDEL to remove the data hash from the hotkey
    key = f"hotkey:{ss58_address}"
    await database.hdel(key, data_hash)
    await database.hdel(key, f"ttl:{data_hash}")  # delete the TTL as well
    bt.logging.trace(f"Removed data hash {data_hash} from hotkey {ss58_address}.")


async def get_metadata_for_hotkey(
    ss58_address: str, database: aioredis.Redis
) -> Dict[str, dict]:
    """
    Retrieves all data hashes and their metadata for a given hotkey.

    Parameters:
        ss58_address (str): The key representing the hotkey.
        database (aioredis.Redis): The Redis client instance.

    Returns:
        A dictionary where keys are data hashes and values are the associated metadata.
    """
    # Fetch all fields (data hashes) and values (metadata) for the hotkey
    all_data_hashes = await database.hgetall(f"hotkey:{ss58_address}")

    # Deserialize the metadata for each data hash
    data_hash_metadata = {
        data_hash.decode("utf-8"): json.loads(metadata.decode("utf-8"))
        for data_hash, metadata in all_data_hashes.items()
        if not data_hash.startswith(b"ttl:")
    }

    # TODO: include the TTL in this metadata as a unified dict?
    bt.logging.trace(
        f"get_metadata_for_hotkey() # hashes found for hotkey {ss58_address}: {len(data_hash_metadata)}"
    )
    return data_hash_metadata


async def get_hashes_for_hotkey(
    ss58_address: str, database: aioredis.Redis
) -> List[str]:
    """
    Retrieves all data hashes and their metadata for a given hotkey.

    Parameters:
        ss58_address (str): The key representing the hotkey.
        database (aioredis.Redis): The Redis client instance.

    Returns:
        A dictionary where keys are data hashes and values are the associated metadata.
    """
    # Fetch all fields (data hashes) and values (metadata) for the hotkey
    all_data_hashes = await database.hgetall(f"hotkey:{ss58_address}")

    # Deserialize the metadata for each data hash
    return [
        data_hash.decode("utf-8") for data_hash, metadata in all_data_hashes.items()
    ]


async def remove_hashes_for_hotkey(
    ss58_address: str, hashes: list, database: aioredis.Redis
) -> List[str]:
    """
    Retrieves all data hashes and their metadata for a given hotkey.

    Parameters:
        ss58_address (str): The key representing the hotkey.
        database (aioredis.Redis): The Redis client instance.

    Returns:
        A dictionary where keys are data hashes and values are the associated metadata.
    """
    bt.logging.trace(
        f"remove_hashes_for_hotkey() removing {len(hashes)} hashes from hotkey {ss58_address}"
    )
    for _hash in hashes:
        await remove_metadata_from_hotkey(ss58_address, _hash, database)


async def update_metadata_for_data_hash(
    ss58_address: str, data_hash: str, new_metadata: dict, database: aioredis.Redis
):
    """
    Updates the metadata for a specific data hash associated with a hotkey.

    Parameters:
        ss58_address (str): The key representing the hotkey.
        data_hash (str): The subkey representing the data hash to update.
        new_metadata (dict): The new metadata to associate with the data hash.
        database (aioredis.Redis): The Redis client instance.
    """
    # Serialize the new metadata as a JSON string
    new_metadata_json = json.dumps(new_metadata)
    # Update the field in the hash with the new metadata
    await database.hset(f"hotkey:{ss58_address}", data_hash, new_metadata_json)
    bt.logging.trace(
        f"Updated metadata for data hash {data_hash} under hotkey {ss58_address}."
    )


async def get_metadata_for_hotkey_and_hash(
    ss58_address: str, data_hash: str, database: aioredis.Redis, verbose: bool = False
) -> Optional[Dict[str, Any]]:
    """
    Retrieves specific metadata from a hash in Redis for the given field_key.

    Parameters:
        ss58_address (str): The hotkey associated.
        data_hash (str): The data hash associated.
        databse (aioredis.Redis): The Redis client instance.

    Returns:
        The deserialized metadata as a dictionary, or None if not found.
    """
    # Get the JSON string from Redis
    metadata_json = await database.hget(f"hotkey:{ss58_address}", data_hash)
    if verbose:
        bt.logging.trace(
            f"hotkey {ss58_address[:16]} | data_hash {data_hash[:16]} | metadata_json {metadata_json}"
        )
    if metadata_json:
        # Deserialize the JSON string to a Python dictionary
        metadata = json.loads(metadata_json)
        return metadata
    else:
        bt.logging.trace(f"No metadata found for {data_hash} in hash {ss58_address}.")
        return None


async def get_all_chunk_hashes(database: aioredis.Redis) -> Dict[str, List[str]]:
    """
    Retrieves all chunk hashes and associated metadata from the Redis instance.

    Parameters:
        database (aioredis.Redis): The Redis client instance.

    Returns:
        A dictionary where keys are chunk hashes and values are lists of hotkeys associated with each chunk hash.
    """
    # Initialize an empty dictionary to store the inverse map
    chunk_hash_hotkeys = {}

    # Retrieve all hotkeys (assuming keys are named with a 'hotkey:' prefix)
    async for hotkey in database.scan_iter("*"):
        if not hotkey.startswith(b"hotkey:"):
            continue
        # Fetch all fields (data hashes) for the current hotkey
        data_hashes = await database.hkeys(hotkey)
        # Iterate over each data hash and append the hotkey to the corresponding list
        for data_hash in data_hashes:
            data_hash = data_hash.decode("utf-8")
            if data_hash not in chunk_hash_hotkeys:
                chunk_hash_hotkeys[data_hash] = []
            chunk_hash_hotkeys[data_hash].append(hotkey.decode("utf-8").split(":")[1])

    return chunk_hash_hotkeys


async def get_all_full_hashes(database: aioredis.Redis) -> List[str]:
    """
    Retrieves all data hashes and their corresponding hotkeys from the Redis instance.

    Parameters:
        database (aioredis.Redis): The Redis client instance.

    Returns:
        A dictionary where keys are data hashes and values are lists of hotkeys associated with each data hash.
    """
    data_hashes = []
    async for key in database.scan_iter("*"):
        if not key.startswith(b"file:"):
            continue
        data_hashes.append(key.decode("utf-8").split(":")[1])

    return data_hashes


async def get_all_hotkeys_for_data_hash(
    data_hash: str, database: aioredis.Redis, is_full_hash: bool = False
) -> List[str]:
    """
    Fetch all hotkeys associated with a given hash, which can be a full file hash or a chunk hash.

    Parameters:
    - data_hash (str): The hash value of the file or chunk.
    - database (aioredis.Redis): An instance of the Redis database.
    - is_full_hash (bool): A flag indicating if the hash_value is a full file hash.

    Returns:
    - List[str]: A list of hotkeys associated with the hash.
      Returns None if no hotkeys are found.
    """
    all_hotkeys = set()

    if is_full_hash:
        # Get all chunks for the full hash
        chunks_info = await get_all_chunks_for_file(data_hash, database)
        if chunks_info is None:
            return None
        # Aggregate hotkeys from all chunks
        for chunk_info in chunks_info.values():
            all_hotkeys.update(chunk_info["hotkeys"])
    else:
        # Fetch hotkeys for a single chunk hash
        chunk_metadata = await database.hgetall(f"chunk:{data_hash}")
        if chunk_metadata:
            hotkeys = chunk_metadata.get(b"hotkeys")
            if hotkeys:
                all_hotkeys.update(hotkeys.decode().split(","))

    return list(all_hotkeys)


async def total_hotkey_storage(
    hotkey: str, database: aioredis.Redis, verbose: bool = False
) -> int:
    """
    Calculates the total storage used by a hotkey in the database.

    Parameters:
        database (aioredis.Redis): The Redis client instance.
        hotkey (str): The key representing the hotkey.

    Returns:
        The total storage used by the hotkey in bytes.
    """
    total_storage = 0
    keys = await database.hkeys(f"hotkey:{hotkey}")
    for data_hash in keys:
        if data_hash.startswith(b"ttl:"):
            continue
        # Get the metadata for the current data hash
        metadata = await get_metadata_for_hotkey_and_hash(
            hotkey, data_hash, database, verbose
        )
        if metadata:
            # Add the size of the data to the total storage
            total_storage += metadata["size"]
    return total_storage


async def hotkey_at_capacity(
    hotkey: str, database: aioredis.Redis, verbose: bool = False
) -> bool:
    """
    Checks if the hotkey is at capacity.

    Parameters:
        database (aioredis.Redis): The Redis client instance.
        hotkey (str): The key representing the hotkey.

    Returns:
        True if the hotkey is at capacity, False otherwise.
    """
    # Get the total storage used by the hotkey
    total_storage = await total_hotkey_storage(hotkey, database, verbose)
    # Check if the hotkey is at capacity
    byte_limit = await database.hget(f"stats:{hotkey}", "storage_limit")
    if byte_limit is None:
        if verbose:
            bt.logging.trace(f"Could not find storage limit for {hotkey}.")
        return False
    try:
        limit = int(byte_limit)
    except Exception as e:
        if verbose:
            bt.logging.trace(f"Could not parse storage limit for {hotkey} | {e}.")
        return False
    if total_storage >= limit:
        if verbose:
            bt.logging.trace(
                f"Hotkey {hotkey} is at max capacity {limit // 1024**3} GB."
            )
        return True
    else:
        if verbose:
            bt.logging.trace(
                f"Hotkey {hotkey} has {(limit - total_storage) // 1024**3} GB free."
            )
        return False


async def cache_hotkeys_capacity(
    hotkeys: List[str], database: aioredis.Redis, verbose: bool = False
) -> Dict[str, Tuple[int, Optional[int]]]:
    """
    Caches the capacity information for a list of hotkeys.

    Parameters:
        hotkeys (list): List of hotkey strings to check.
        database (aioredis.Redis): The Redis client instance.
        verbose (bool): A flag indicating if verbose logging is enabled.

    Returns:
        dict: A dictionary with hotkeys as keys and a tuple of (total_storage, limit) as values.
    """
    hotkeys_capacity = {}

    for hotkey in hotkeys:
        # Get the total storage used by the hotkey
        total_storage = await total_hotkey_storage(hotkey, database, verbose)
        # Get the byte limit for the hotkey
        byte_limit = await database.hget(f"stats:{hotkey}", "storage_limit")

        if byte_limit is None:
            bt.logging.warning(f"Could not find storage limit for {hotkey}.")
            limit = None
        else:
            try:
                limit = int(byte_limit)
            except Exception as e:
                bt.logging.warning(f"Could not parse storage limit for {hotkey} | {e}.")
                limit = None

        hotkeys_capacity[hotkey] = (total_storage, limit)

    return hotkeys_capacity


async def check_hotkeys_capacity(hotkeys_capacity, hotkey: str, verbose: bool = False) -> bool:
    """
    Checks if a hotkey is at capacity using the cached information.

    Parameters:
        hotkeys_capacity (dict): Dictionary with cached capacity information.
        hotkey (str): The key representing the hotkey.

    Returns:
        True if the hotkey is at capacity, False otherwise.
    """
    total_storage, limit = hotkeys_capacity.get(hotkey, (0, None))

    if limit is None:
        # Limit information not available or couldn't be parsed
        return False

    if total_storage >= limit:
        if verbose:
            bt.logging.trace(
                f"Hotkey {hotkey} is at max capacity {limit // 1024**3} GB."
            )
        return True
    else:
        if verbose:
            bt.logging.trace(
                f"Hotkey {hotkey} has {(limit - total_storage) // 1024**3} GB free."
            )
        return False


async def total_validator_storage(database: aioredis.Redis) -> int:
    """
    Calculates the total storage used by all hotkeys in the database.

    Parameters:
        database (aioredis.Redis): The Redis client instance.

    Returns:
        The total storage used by all hotkeys in the database in bytes.
    """
    total_storage = 0
    # Iterate over all hotkeys
    async for hotkey in database.scan_iter("*"):
        if not hotkey.startswith(b"hotkey:"):
            continue
        # Grab storage for that hotkey
        total_storage += await total_hotkey_storage(
            hotkey.decode().split(":")[1], database
        )
    return total_storage


async def get_miner_statistics(database: aioredis.Redis) -> Dict[str, Dict[str, str]]:
    """
    Retrieves statistics for all miners in the database.
    Parameters:
        database (aioredis.Redis): The Redis client instance.
    Returns:
        A dictionary where keys are hotkeys and values are dictionaries containing the statistics for each hotkey.
    """
    stats = {}
    async for key in database.scan_iter(b"stats:*"):
        # Await the hgetall call and then process its result
        key_stats = await database.hgetall(key)
        # Process the key_stats as required
        processed_stats = {
            k.decode("utf-8"): v.decode("utf-8") for k, v in key_stats.items()
        }
        stats[key.decode("utf-8").split(":")[-1]] = processed_stats

    return stats


async def get_single_miner_statistics(
    ss58_address: str, database: aioredis.Redis
) -> Dict[str, Dict[str, str]]:
    """
    Retrieves statistics for all miners in the database.
    Parameters:
        database (aioredis.Redis): The Redis client instance.
    Returns:
        A dictionary where keys are hotkeys and values are dictionaries containing the statistics for each hotkey.
    """
    stats = await database.hgetall(f"stats:{ss58_address}")
    return {k.decode("utf-8"): v.decode("utf-8") for k, v in stats.items()}


async def get_redis_db_size(database: aioredis.Redis) -> int:
    """
    Calculates the total approximate size of all keys in a Redis database.
    Parameters:
        database (int): Redis database
    Returns:
        int: Total size of all keys in bytes
    """
    total_size = 0
    async for key in database.scan_iter("*"):
        size = await database.execute_command("MEMORY USAGE", key)
        if size:
            total_size += size
    return total_size


async def store_file_chunk_mapping_ordered(
    full_hash: str,
    chunk_hashes: List[str],
    chunk_indices: List[str],
    database: aioredis.Redis,
    encryption_payload: Optional[Union[bytes, dict]] = None,
):
    """
    Store an ordered mapping of file chunks in the database.

    This function takes a file's full hash and the hashes of its individual chunks, along with their
    respective indices, and stores them in a sorted set in the Redis database. The order is preserved
    based on the chunk index.

    Parameters:
    - full_hash (str): The full hash of the file.
    - chunk_hashes (List[str]): A list of hashes for the individual chunks of the file.
    - chunk_indices (List[int]): A list of indices corresponding to each chunk hash.
    - database (aioredis.Redis): An instance of the Redis database.
    - encryption_payload (Optional[Union[bytes, dict]]): The encryption payload to store with the file.
    """
    key = f"file:{full_hash}"
    for chunk_index, chunk_hash in zip(chunk_indices, chunk_hashes):
        await database.zadd(key, {chunk_hash: chunk_index})

    # Store the encryption payload if provided
    if encryption_payload:
        if isinstance(encryption_payload, dict):
            encryption_payload = json.dumps(encryption_payload)
        await database.set(f"payload:{full_hash}", encryption_payload)


async def retrieve_encryption_payload(
    full_hash: str,
    database: aioredis.Redis,
    return_dict: bool = False,
) -> Optional[Union[bytes, dict]]:
    """
    Retrieve the encryption payload for a file.

    This function fetches the encryption payload for a file from the Redis database.

    Parameters:
    - full_hash (str): The full hash of the file.
    - database (aioredis.Redis): An instance of the Redis database.

    Returns:
    - Optional[Union[bytes, dict]]: The encryption payload for the file.
    """
    encryption_payload = await database.get(f"payload:{full_hash}")
    if encryption_payload:
        if return_dict:
            return encryption_payload
        try:
            return json.loads(encryption_payload)
        except json.JSONDecodeError:
            return encryption_payload
    else:
        return None


async def get_all_chunks_for_file(
    file_hash: str, database: aioredis.Redis
) -> Optional[Dict[int, Dict[str, Union[str, List[str], int]]]]:
    """
    Retrieve all chunk hashes and their metadata for a given file hash.

    This function fetches the hashes and metadata of all chunks associated with a particular file hash.
    The data is retrieved from a sorted set and returned in a dictionary with the chunk index as the key.

    Parameters:
    - file_hash (str): The full hash of the file whose chunks are to be retrieved.
    - database (aioredis.Redis): An instance of the Redis database.

    Returns:
    - dict: A dictionary where keys are chunk indices, and values are dictionaries with chunk metadata.
      Returns None if no chunks are found.
    """
    file_chunks_key = f"file:{file_hash}"
    chunk_hashes_with_index = await database.zrange(
        file_chunks_key, 0, -1, withscores=True
    )
    if not chunk_hashes_with_index:
        return None

    chunks_info = {}
    for chunk_hash_bytes, index in chunk_hashes_with_index:
        chunk_hash = chunk_hash_bytes.decode()
        chunk_metadata = await database.hgetall(f"chunk:{chunk_hash}")
        if chunk_metadata:
            chunks_info[int(index)] = {
                "chunk_hash": chunk_hash,
                "hotkeys": chunk_metadata[b"hotkeys"].decode().split(","),
                "size": int(chunk_metadata[b"size"]),
            }
    return chunks_info


async def get_hotkeys_for_hash(
    hash_value: str, database: aioredis.Redis, is_full_hash: bool = False
):
    """
    Fetch all hotkeys associated with a given hash, which can be a full file hash or a chunk hash.

    Parameters:
    - hash_value (str): The hash value of the file or chunk.
    - database (aioredis.Redis): An instance of the Redis database.
    - is_full_hash (bool): A flag indicating if the hash_value is a full file hash.

    Returns:
    - List[str]: A list of hotkeys associated with the hash.
      Returns None if no hotkeys are found.
    """
    all_hotkeys = set()

    if is_full_hash:
        # Get UIDs for all chunks under the full hash
        chunks_info = get_all_chunks_for_file(hash_value, database)
        if chunks_info is None:
            return None
        for chunk_info in chunks_info.values():
            all_hotkeys.update(chunk_info["hotkeys"])
    else:
        # Get UIDs for a single chunk hash
        chunk_metadata = await database.hgetall(f"chunk:{hash_value}")
        if chunk_metadata:
            hotkeys = chunk_metadata.get(b"hotkeys")
            if hotkeys:
                all_hotkeys.update(hotkeys.decode().split(","))

    return list(all_hotkeys)


async def add_hotkey_to_chunk(chunk_hash: str, hotkey: str, database: aioredis.Redis):
    """
    Add a hotkey to the metadata of a specific chunk.

    This function updates the chunk's metadata to include the given hotkey. If the hotkey is already
    associated with the chunk, no changes are made.

    Parameters:
    - chunk_hash (str): The hash of the chunk to which the hotkey is to be added.
    - hotkey (str): The hotkey to add to the chunk's metadata.
    - database (aioredis.Redis): An instance of the Redis database.
    """
    chunk_metadata_key = f"chunk:{chunk_hash}"

    # Fetch existing UIDs for the chunk
    existing_metadata = await database.hget(chunk_metadata_key, "hotkeys")
    if existing_metadata:
        existing_hotkeys = existing_metadata.decode().split(",")

        # Add new UID if it's not already in the list
        if hotkey not in existing_hotkeys:
            updated_hotkeys = existing_hotkeys + [hotkey]
            await database.hset(
                chunk_metadata_key, "hotkeys", ",".join(updated_hotkeys)
            )
            print(f"UID {hotkey} added to chunk {chunk_hash}.")
        else:
            print(f"UID {hotkey} already exists for chunk {chunk_hash}.")
    else:
        # If no UIDs are associated with this chunk, create a new entry
        await database.hmset(chunk_metadata_key, {"hotkeys": hotkey})
        print(f"UID {hotkey} set for new chunk {chunk_hash}.")


async def remove_hotkey_from_chunk(
    chunk_hash: str, hotkey: str, database: aioredis.Redis, verbose: bool = False
):
    """
    Remove a hotkey from the metadata of a specific chunk.

    This function updates the chunk's metadata to remove the given hotkey. If the hotkey is not
    associated with the chunk, no changes are made.

    Parameters:
    - chunk_hash (str): The hash of the chunk to which the hotkey is to be added.
    - hotkey (str): The hotkey to add to the chunk's metadata.
    - database (aioredis.Redis): An instance of the Redis database.
    """
    chunk_metadata_key = f"chunk:{chunk_hash}"

    # Fetch existing UIDs for the chunk
    existing_metadata = await database.hget(chunk_metadata_key, "hotkeys")
    if existing_metadata:
        existing_hotkeys = existing_metadata.decode().split(",")

        # Remove UID if it's in the list
        if hotkey in existing_hotkeys:
            existing_hotkeys.remove(hotkey)
            await database.hset(
                chunk_metadata_key, "hotkeys", ",".join(existing_hotkeys)
            )
            if verbose:
                bt.logging.trace(f"UID {hotkey} removed from chunk {chunk_hash}.")
        else:
            if verbose:
                bt.logging.trace(f"UID {hotkey} does not exist for chunk {chunk_hash}.")
    else:
        if verbose:
            bt.logging.trace(f"No UIDs associated with chunk {chunk_hash}.")


async def store_chunk_metadata(
    full_hash: str,
    chunk_hash: str,
    hotkeys: List[str],
    chunk_size: int,
    database: aioredis.Redis,
):
    """
    Store metadata for a specific file chunk.

    This function creates or updates the metadata for a chunk, including the associated hotkeys and chunk size.

    Parameters:
    - full_hash (str): The full hash of the file that the chunk belongs to.
    - chunk_hash (str): The hash of the chunk whose metadata is to be stored.
    - hotkeys (List[str]): A list of hotkeys associated with the chunk.
    - chunk_size (int): The size of the chunk in bytes.
    - database (aioredis.Redis): An instance of the Redis database.
    """
    chunk_metadata_key = f"chunk:{chunk_hash}"
    existing_metadata = await database.hget(chunk_metadata_key, "hotkeys")
    if existing_metadata:
        existing_hotkeys = existing_metadata.decode().split(",")
        hotkeys = set(existing_hotkeys + hotkeys)
    metadata = {"hotkeys": ",".join(hotkeys), "size": chunk_size}

    await database.hmset(chunk_metadata_key, metadata)


async def get_ordered_metadata(
    file_hash: str, database: aioredis.Redis
) -> List[Dict[str, Union[str, List[str], int]]]:
    """
    Retrieve the metadata for all chunks of a file in the order of their indices.

    This function calls `get_all_chunks_for_file` to fetch all chunks' metadata and then sorts
    them based on their indices to maintain the original file order.

    Parameters:
    - file_hash (str): The full hash of the file whose ordered metadata is to be retrieved.
    - database (aioredis.Redis): An instance of the Redis database.

    Returns:
    - List[dict]: A list of metadata dictionaries for each chunk, ordered by their chunk index.
      Returns None if no chunks are found.
    """
    chunks_info = await get_all_chunks_for_file(file_hash, database)
    if chunks_info is None:
        return None

    ordered_chunks = sorted(chunks_info.items(), key=lambda x: x[0])
    return [chunk_info for _, chunk_info in ordered_chunks]


# Function to grab mutually exclusive UIDs for a specific full_hash (get chunks of non-overlapping UIDs)
async def retrieve_mutually_exclusive_hotkeys_full_hash(
    full_hash: str, database: aioredis.Redis
) -> Dict[str, List[str]]:
    """
    Retrieve a list of mutually exclusive hotkeys for a specific full hash.

    This function retrieves the metadata for all chunks of a file and then sorts them based on their
    indices to maintain the original file order. It then iterates over the chunks and adds the hotkeys
    of each chunk to the dict of chunk hash <> mutually exclusive hotkey mappings if not already present.

    Parameters:
    - full_hash (str): The full hash of the file whose ordered metadata is to be retrieved.
    - database (aioredis.Redis): An instance of the Redis database.

    Returns:
    - Dict[str, List[str]]: A dict of mutually exclusive hotkeys for each corresponding hash.
      Returns None if no chunks are found.
    """
    chunks_info = await get_all_chunks_for_file(full_hash, database)
    if chunks_info is None:
        return None

    ordered_chunks = sorted(chunks_info.items(), key=lambda x: x[0])
    mutually_exclusive_hotkeys = {}
    for _, chunk_info in ordered_chunks:
        if chunk_info["chunk_hash"] not in mutually_exclusive_hotkeys:
            mutually_exclusive_hotkeys[chunk_info["chunk_hash"]] = []
        for hotkey in chunk_info["hotkeys"]:
            if hotkey not in mutually_exclusive_hotkeys[chunk_info["chunk_hash"]]:
                mutually_exclusive_hotkeys[chunk_info["chunk_hash"]].append(hotkey)

    return mutually_exclusive_hotkeys


async def check_hash_type(data_hash: str, database: aioredis.Redis) -> str:
    """
    Determine if the data_hash is a full file hash, a chunk hash, or a standalone challenge hash.

    Parameters:
    - data_hash (str): The data hash to check.
    - database (aioredis.Redis): The Redis database client.

    Returns:
    - str: A string indicating the type of hash ('full_file', 'chunk', or 'challenge').
    """
    is_full_file = await database.exists(f"file:{data_hash}")
    if is_full_file:
        return "full_file"

    is_chunk = await database.exists(f"chunk:{data_hash}")
    if is_chunk:
        return "chunk"

    return "challenge"


async def is_file_chunk(chunk_hash: str, database: aioredis.Redis) -> str:
    """
    Determines if the given chunk_hash is part of a full file.

    Parameters:
    - chunk_hash (str): The hash of the chunk to check.
    - database (aioredis.Redis): The Redis database client.

    Returns:
    - bool: True if the hash belongs to a full file, false otherwise (challenge data)
    """
    async for key in database.scan_iter(match="chunk:*"):
        if chunk_hash in key.decode():
            return True
    return False


async def get_all_hashes_in_database(database: aioredis.Redis) -> List[str]:
    """
    Retrieves all hashes from the Redis instance.

    Parameters:
        database (aioredis.Redis): The Redis client instance.

    Returns:
        A list of hashes.
    """
    all_hashes = set()

    async for hotkey_key in database.scan_iter(match="hotkey:*"):
        all_hashes.update(list(await database.hgetall(hotkey_key)))

    return list(all_hashes)


async def get_all_challenge_hashes(database: aioredis.Redis) -> List[str]:
    """
    Retrieves all challenge hashes from the Redis instance.

    Parameters:
        database (aioredis.Redis): The Redis client instance.

    Returns:
        A list of challenge hashes.
    """
    all_hashes = await get_all_hashes_in_database(database)

    challenge_hashes = []
    for h in all_hashes:
        if await check_hash_type(h, database) == "challenge":
            challenge_hashes.append(h)

    return challenge_hashes


async def get_challenges_for_hotkey(ss58_address: str, database: aioredis.Redis):
    """
    Retrieves a list of challenge hashes associated with a specific hotkey.

    This function scans through all the hashes related to a given hotkey and filters out
    those which are identified as challenge data. It's useful for identifying which
    challenges a particular miner (identified by hotkey) is involved with.

    Parameters:
    - ss58_address (str): The hotkey (miner identifier) whose challenge hashes are to be retrieved.
    - database (aioredis.Redis): An instance of the Redis database used for data storage.

    Returns:
    - List[str]: A list of challenge hashes associated with the given hotkey.
      Returns an empty list if no challenge data is associated with the hotkey.
    """
    hashes = list(await database.hgetall(f"hotkey:{ss58_address}"))
    challenges = []
    for j, h in enumerate(hashes):
        if await check_hash_type(h, database) == "challenge":
            challenges.append(h)

    return challenges


async def purge_challenges_for_hotkey(ss58_address: str, database: aioredis.Redis):
    """
    Purges (deletes) all challenge hashes associated with a specific hotkey.

    This function is used for housekeeping purposes in the database, allowing for the
    removal of all challenge data related to a particular miner (hotkey). This can be
    useful for clearing outdated or irrelevant challenge data from the database.

    Parameters:
    - ss58_address (str): The hotkey (miner identifier) whose challenge hashes are to be purged.
    - database (aioredis.Redis): An instance of the Redis database used for data storage.
    """
    challenge_hashes = await get_challenges_for_hotkey(ss58_address, database)
    bt.logging.trace(f"purging challenges for {ss58_address}...")
    for ch in challenge_hashes:
        await database.hdel(f"hotkey:{ss58_address}", ch)


async def purge_challenges_for_all_hotkeys(database: aioredis.Redis):
    """
    Purges (deletes) all challenge hashes for every hotkey in the database.

    This function performs a comprehensive cleanup of the database by removing all
    challenge-related data. It iterates over each hotkey in the database and
    individually purges the challenge hashes associated with them. This is particularly
    useful for global maintenance tasks where outdated or irrelevant challenge data
    needs to be cleared from the entire database. For example, when a UID is replaced.

    Parameters:
    - database (aioredis.Redis): An instance of the Redis database used for data storage.
    """
    bt.logging.debug("purging challenges for ALL hotkeys...")
    async for hotkey in database.scan_iter(match="hotkey:*"):
        hotkey = hotkey.decode().split(":")[1]
        await purge_challenges_for_hotkey(hotkey, database)


async def delete_file_from_database(file_hash: str, database: aioredis.Redis):
    """
    Deletes all data related to a file from the database.

    This function is used for housekeeping purposes in the database, allowing for the
    removal of all data related to a particular file. This can be useful for clearing
    outdated or irrelevant data from the database.

    Parameters:
    - file_hash (str): The hash of the file to be deleted.
    - database (aioredis.Redis): An instance of the Redis database used for data storage.
    """
    bt.logging.debug(f"deleting file {file_hash} from database...")

    chunk_data = await get_all_chunks_for_file(file_hash, database)
    if chunk_data is None:
        bt.logging.debug(f"file {file_hash} not found in database.")
        return

    # Delete all chunk hashes
    for idx, chunk_dict in chunk_data.items():
        chunk_hash = chunk_dict["chunk_hash"]
        await database.delete(f"chunk:{chunk_hash}")

    # Test getting the chunk hash back
    chunk_data = await get_all_chunks_for_file(file_hash, database)
    if chunk_data == {}:
        bt.logging.debug(f"all chunks deleted for file {file_hash}.")
        await database.delete(f"file:{file_hash}")

    bt.logging.trace(f"File {file_hash} deleted!")


async def get_hash_keys(ss58_address: str, database: aioredis.Redis) -> List[str]:
    """
    Filter out the ttl: hashes from the hotkey hashes and return the list of keys.
    """
    return [
        key for key in await database.hkeys(f"hotkey:{ss58_address}") if not key.startswith(b"ttl:")
    ]


async def active_hotkeys(database: aioredis.Redis) -> List[str]:
    """
    Returns a list of all active hotkeys in the database.
    """
    return [
        x.decode().split(":")[-1] async for x in database.scan_iter("hotkey:*")
    ]


async def get_network_capacity(database) -> int:
    """
    Get the total storage capacity of the network in bytes.
    """
    cap = 0
    for k,v in (await get_miner_statistics(database)).items():
        cap += int(v.get('storage_limit', 0))

    return cap


async def current_validator_storage(database) -> int:
    """
    Get the current storage capacity of the network in bytes.
    """
    hotkeys = await active_hotkeys(database)

    current_storage = 0
    for k, (cur, _) in (await cache_hotkeys_capacity(hotkeys, database)).items():
        current_storage += cur

    return current_storage


async def tier_statistics(database: aioredis.Redis, by_tier: bool = False) -> Dict[str, Dict[str, int]]:
    tier_counts = {
        "Super Saiyan": 0,
        "Diamond": 0,
        "Gold": 0,
        "Silver": 0,
        "Bronze": 0,
    } 
    tier_capacity = {
        "Super Saiyan": 0,
        "Diamond": 0,
        "Gold": 0,
        "Silver": 0,
        "Bronze": 0,
    }
    tier_usage = {
        "Super Saiyan": 0,
        "Diamond": 0,
        "Gold": 0,
        "Silver": 0,
        "Bronze": 0,
    }

    for k,v in (await get_miner_statistics(database)).items():
        tier = v.get('tier', None)
        if tier:
            hotkey = k.split(":")[-1]
            tier_counts[tier] += 1
            tier_capacity[tier] += int(v.get('storage_limit', 0))
            tier_usage[tier] += await total_hotkey_storage(hotkey, database, False)

    tier_percent_usage = {
        k: 100 * (v / tier_capacity[k]) if tier_capacity[k] > 0 else 0
        for k,v in tier_usage.items()
    }

    type_dict = {
        "counts": tier_counts, 
        "capacity": tier_capacity, 
        "usage": tier_usage,
        "percent_usage": tier_percent_usage
    }

    if by_tier:
        inverted_dict = {}

        for category, tier_dict in type_dict.items():
            for tier, value in tier_dict.items():
                if tier not in inverted_dict:
                    inverted_dict[tier] = {}
                inverted_dict[tier][category] = value

        return inverted_dict

    return type_dict

async def total_successful_requests(database: aioredis.Redis) -> int:
    return sum(
        [
            int(v.get('total_successes', 0))
            for k,v in (await get_miner_statistics(database)).items()
        ]
    )


async def compute_by_tier_stats(database):

    stats = await get_miner_statistics(database)

    tier_stats = {}

    for _, d in stats.items():
        tier = d.get('tier', "Bronze")

        if tier not in tier_stats:
            tier_stats[tier] = {
                'store_attempts': 0,
                'store_successes': 0,
                'challenge_attempts': 0,
                'challenge_successes': 0,
                'retrieve_attempts': 0,
                'retrieve_successes': 0,
                'total_current_attempts': 0,
                'total_current_successes': 0,
                'total_global_successes': 0,
            }

        tier_stats[tier]['store_attempts'] += int(d.get('store_attempts', 0))
        tier_stats[tier]['store_successes'] += int(d.get('store_successes', 0))
        tier_stats[tier]['challenge_attempts'] += int(d.get('challenge_attempts', 0))
        tier_stats[tier]['challenge_successes'] += int(d.get('challenge_successes', 0))
        tier_stats[tier]['retrieve_attempts'] += int(d.get('retrieve_attempts', 0))
        tier_stats[tier]['retrieve_successes'] += int(d.get('retrieve_successes', 0))

        tier_stats[tier]['total_current_attempts'] = sum([
            tier_stats[tier]['store_attempts'], 
            tier_stats[tier]['challenge_attempts'], 
            tier_stats[tier]['retrieve_attempts']
        ])
        tier_stats[tier]['total_current_successes'] = sum([
            tier_stats[tier]['store_successes'], 
            tier_stats[tier]['challenge_successes'], 
            tier_stats[tier]['retrieve_successes']
        ])
        tier_stats[tier]['total_global_successes'] += int(d.get('total_successes', 0))

        total_attempts = tier_stats[tier]['total_current_attempts']
        total_successes = tier_stats[tier]['total_current_successes']
        success_rate = (total_successes / total_attempts * 100) if total_attempts else 0
        tier_stats[tier]['success_rate'] = success_rate

    return tier_stats
