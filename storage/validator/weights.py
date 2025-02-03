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

# Utils for weights setting on chain.

import numpy as np
import torch
import bittensor as bt
from bittensor.utils.weight_utils import convert_weights_and_uids_for_emit, process_weights_for_netuid
from typing import Optional
from storage import __spec_version__ as spec_version
from storage.shared.weights import set_weights
from storage.validator.event import EventSchema

def set_weights_for_validator(
    subtensor: "bt.subtensor",
    wallet: "bt.wallet",
    netuid: int,
    metagraph: "bt.metagraph",
    moving_averaged_scores: "torch.Tensor",
    wandb_on: bool = False,
    wait_for_inclusion: bool = False,
    wait_for_finalization: bool = False,
) -> Optional[EventSchema]:
    """
    Sets miners' weights on the Bittensor network.

    This function assigns a weight of 1 to the current miner (identified by its UID) and
    a weight of 0 to all other peers in the network. The weights determine the trust level
    the miner assigns to other nodes on the network.

    Args:
        subtensor (bt.subtensor): The Bittensor object managing the blockchain connection.
        wallet (bt.wallet): The miner's wallet holding cryptographic information.
        netuid (int): The unique identifier for the chain subnet.
        metagraph (bt.metagraph): Bittensor metagraph.
        moving_averaged_scores (torch.Tensor): Moving averaged scores.
        wandb_on (bool, optional): Flag to determine if logging to Weights & Biases is enabled. Defaults to False.
        wait_for_inclusion (bool, optional): Whether to wait for the extrinsic to enter a block.
        wait_for_finalization (bool, optional): Whether to wait for the extrinsic to be finalized on the chain.

    Returns:
        EventSchema: Event schema containing the results of setting weights.
    """
    # Replace NaN values in moving_averaged_scores with 0
    moving_averaged_scores_no_nan = np.nan_to_num(moving_averaged_scores, nan=0.0)

    # Ensure all scores are non-negative
    minimum = np.min(moving_averaged_scores_no_nan)
    if minimum < 0:
        positive_moving_averaged_scores = moving_averaged_scores_no_nan - minimum
    else:
        positive_moving_averaged_scores = moving_averaged_scores_no_nan

    # Set negative scores to zero
    positive_moving_averaged_scores[positive_moving_averaged_scores < 0] = 0

    # Normalize scores
    sum_scores = np.sum(positive_moving_averaged_scores)
    if sum_scores > 0:
        raw_weights = positive_moving_averaged_scores / sum_scores
    else:
        raw_weights = np.zeros_like(positive_moving_averaged_scores)

    # Process weights for the specific subnet
    processed_weight_uids, processed_weights = process_weights_for_netuid(
        uids=metagraph.uids,
        weights=raw_weights,
        netuid=netuid,
        subtensor=subtensor,
        metagraph=metagraph,
    )

    # Convert to uint16 weights and uids
    uint_uids, uint_weights = convert_weights_and_uids_for_emit(
        uids=processed_weight_uids, weights=processed_weights
    )

    # Set the weights on chain via our subtensor connection
    success, message = set_weights(
        subtensor=subtensor,
        wallet=wallet,
        netuid=netuid,
        uids=uint_uids,
        weights=uint_weights,
        wandb_on=wandb_on,
        version_key=spec_version,
        wait_for_finalization=wait_for_finalization,
        wait_for_inclusion=wait_for_inclusion,
    )

    if success:
        bt.logging.info("Set weights on chain successfully!")
        best_idx = np.argmax(uint_weights)
        best_uid = uint_uids[best_idx]
        event = EventSchema(
            task_name="SetWeights",
            successful=[],
            completion_times=[],
            task_status_messages=[],
            task_status_codes=[],
            block=subtensor.get_current_block(),
            uids=uint_uids,
            step_length=0.0,
            best_uid=best_uid,
            best_hotkey=metagraph.hotkeys[best_uid],
            rewards=[],
            set_weights=uint_weights,
        )
        return event
    else:
        bt.logging.error(f"Set weights failed: {message}")
        return None
