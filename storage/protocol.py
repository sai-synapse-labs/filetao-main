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

from typing import List, Optional, Union, Dict
from pydantic import BaseModel, Field
import bittensor as bt


class Store(bt.Synapse):
    """
    Stores encrypted data along with cryptographic parameters for verification.
    """
    encrypted_data: str  # base64 encoded string of encrypted data (bytes)
    curve: str  # e.g., P-256
    g: str  # base point (hex string representation)
    h: str  # random point (hex string representation)
    seed: Union[str, int, bytes]  # Random seed (bytes stored as hex) for commitment

    # Optional fields that are computed later
    randomness: Optional[int] = None
    commitment: Optional[str] = None
    signature: Optional[bytes] = None
    commitment_hash: Optional[str] = None
    ttl: Optional[int] = None  # Time to live (in seconds)

    required_hash_fields: List[str] = Field(
        default=[
            "curve", "g", "h", "seed", "randomness", "commitment",
            "signature", "commitment_hash"
        ],
        title="Required Hash Fields",
        description="A list of required fields for the hash.",
    )


class StoreUser(bt.Synapse):
    """
    Represents a user storing encrypted data with an encryption payload.
    """
    encrypted_data: str  # base64 encoded string of encrypted data (bytes)
    encryption_payload: str  # encrypted JSON serialized bytestring of encryption params

    data_hash: Optional[str] = None  # Miner storage lookup key
    ttl: Optional[int] = None  # Time to live (in seconds)

    required_hash_fields: List[str] = Field(
        default=["encrypted_data", "encryption_payload"],
        title="Required Hash Fields",
        description="A list of required fields for the hash."
    )


class Challenge(bt.Synapse):
    """
    Represents a challenge used for verification of stored data.
    """
    challenge_hash: str  # Hash of the data to challenge
    challenge_index: int  # Block indices to challenge
    chunk_size: int  # Bytes (e.g., 1024) for chunk sizes

    g: str  # Base point (hex string representation)
    h: str  # Random point (hex string representation)
    curve: str
    seed: Union[str, int]  # Random seed for the commitment

    # Results from challenge verification
    commitment_hash: Optional[str] = None
    commitment_proof: Optional[str] = None
    commitment: Optional[str] = None
    data_chunk: Optional[bytes] = None
    randomness: Optional[int] = None
    merkle_proof: Optional[Union[List[Dict[str, str]], str]] = None
    merkle_root: Optional[str] = None

    required_hash_fields: List[str] = Field(
        default=[
            "commitment_hash", "commitment_proof", "commitment",
            "data_chunk", "randomness", "merkle_proof", "merkle_root"
        ],
        title="Required Hash Fields",
        description="A list of required fields for the hash."
    )


class Retrieve(bt.Synapse):
    """
    Handles retrieving stored data along with cryptographic proofs.
    """
    data_hash: str  # Miner storage lookup key
    seed: str  # New random seed to hash the data with

    data: Optional[str] = None  # The retrieved data
    commitment_hash: Optional[str] = None  # Commitment hash for verification
    commitment_proof: Optional[str] = None  # Proof of commitment

    required_hash_fields: List[str] = Field(
        default=["data", "data_hash", "seed", "commitment_proof", "commitment_hash"],
        title="Required Hash Fields",
        description="A list of required fields for the hash."
    )


class RetrieveUser(bt.Synapse):
    """
    Represents a user retrieving encrypted data with an encryption payload.
    """
    data_hash: str  # Miner storage lookup key
    encrypted_data: Optional[str] = None  # Encrypted retrieved data
    encryption_payload: Optional[str] = None  # Encryption payload metadata

    required_hash_fields: List[str] = Field(
        default=["data_hash"],
        title="Required Hash Fields",
        description="A list of required fields for the hash."
    )


class DeleteUser(bt.Synapse):
    """
    Handles deletion requests for stored data.
    """
    data_hash: str  # Miner storage lookup key
    deleted: bool = False  # Flag indicating whether deletion was successful

    required_hash_fields: List[str] = Field(
        default=["data_hash"],
        title="Required Hash Fields",
        description="A list of required fields for the hash."
    )
