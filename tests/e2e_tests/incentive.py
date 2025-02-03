# start the chain
# register 2 wallets
# register neurons
# start validator.py with wallet/neuron
# start miner.py with wallet/neuron
# wait (miner ingest hash/data, miner set weights, miner does work)
# validate (metagraph, logs, miner metrics)


import asyncio
import atexit
import os
import sys

import pytest

from bittensor.core.subtensor import Subtensor
from tests.e2e_tests.tools.chain_interactions import register_subnet, add_stake, wait_epoch
from tests.e2e_tests.tools.e2e_test_utils import setup_wallet
from bittensor.utils.balance import Balance
from bittensor.core.extrinsics import utils
from bittensor.core.extrinsics.set_weights import do_set_weights
from bittensor.core.metagraph import Metagraph


FAST_BLOCKS_SPEEDUP_FACTOR = 5

# Track the miner process globally for cleanup
miner_process = None

async def start_miner(wallet, netuid):
    """Starts the miner process and tracks it globally for cleanup."""
    global miner_process

    cmd = [
        sys.executable,
        "/Users/danielivanov/Repos/filetao-main/neurons/miner.py",
        "--netuid", str(netuid),
        "--subtensor.network", "local",
        "--subtensor.chain_endpoint", "ws://localhost:9945",
        "--wallet.path", wallet.path,
        "--wallet.name", wallet.name,
        "--wallet.hotkey", "default",
        "--database.redis_conf_path", "/opt/homebrew/etc/redis.conf",
        "--database.redis_password", "trxdpPgIHfuKUPonLJbZBHnQroc="
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ,
    )

    miner_process = process
    return process

def cleanup_miner():
    """Ensures the miner is terminated properly even on test interruption."""
    global miner_process
    if miner_process and miner_process.returncode is None:  # Check if still running
        print("Cleaning up miner process...")
        miner_process.terminate()
        try:
            miner_process.wait(timeout=5)  # Give it time to exit
        except Exception:
            pass  # Ignore timeout errors
        print("Miner process terminated.")

# Ensure cleanup on exit (even on CTRL+C)
atexit.register(cleanup_miner)


# Track the validator process globally for cleanup
validator_process = None

async def start_validator(wallet, netuid):
    """Starts the validator process and tracks it globally for cleanup."""
    global validator_process

    cmd = [
        sys.executable,
        "/Users/danielivanov/Repos/filetao-main/neurons/validator.py",
        "--netuid", str(netuid),
        "--subtensor.network", "local",
        "--subtensor.chain_endpoint", "ws://localhost:9945",
        "--wallet.path", wallet.path,
        "--wallet.name", wallet.name,
        "--wallet.hotkey", "default",
        "--database.redis_conf_path", "/opt/homebrew/etc/redis.conf",  # Inject Redis config path directly
        "--database.redis_password", "trxdpPgIHfuKUPonLJbZBHnQroc="  # Inject password directly
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ,
    )

    validator_process = process
    return process

def cleanup_validator():
    """Ensures the validator is terminated properly even on test interruption."""
    global validator_process
    if validator_process and validator_process.returncode is None:  # Check if still running
        print("Cleaning up validator process...")
        validator_process.terminate()
        try:
            validator_process.wait(timeout=5)  # Give it time to exit
        except Exception:
            pass  # Ignore timeout errors
        print("Validator process terminated.")

# Ensure cleanup on exit (even on CTRL+C)
atexit.register(cleanup_validator)


@pytest.mark.asyncio
async def test_incentive(local_chain):
    """
    Test the incentive mechanism and interaction of miners/validators

    Steps:
        1. Register a subnet and register Alice & Bob
        2. Add Stake by Alice
        3. Run Alice as validator & Bob as miner. Wait Epoch
        4. Verify miner has correct: trust, rank, consensus, incentive
        5. Verify validator has correct: validator_permit, validator_trust, dividends, stake
    Raises:
        AssertionError: If any of the checks or verifications fail
    """

    print("Testing test_incentive")
    netuid = 1

    utils.EXTRINSIC_SUBMISSION_TIMEOUT = 12  # handle fast blocks

    # Register root as Alice - the subnet owner and validator
    alice_keypair, alice_wallet = setup_wallet("//Alice")
    register_subnet(local_chain, alice_wallet)

    # Verify subnet <netuid> created successfully
    assert local_chain.query(
        "SubtensorModule", "NetworksAdded", [netuid]
    ).serialize(), "Subnet wasn't created successfully"

    # Register Bob as miner
    bob_keypair, bob_wallet = setup_wallet("//Bob")

    subtensor = Subtensor(network="ws://localhost:9945")

    # Register Alice as a neuron on the subnet
    assert subtensor.burned_register(
        alice_wallet, netuid
    ), "Unable to register Alice as a neuron"

    # Register Bob as a neuron on the subnet
    assert subtensor.burned_register(
        bob_wallet, netuid
    ), "Unable to register Bob as a neuron"

    # Assert two neurons are in network
    assert (
        len(subtensor.neurons(netuid=netuid)) == 2
    ), "Alice & Bob not registered in the subnet"

    # Alice to stake to become to top neuron after the first epoch
    add_stake(local_chain, alice_wallet, Balance.from_tao(10_000))

    miner_log_file_path = "miner_incentive.log"
    # Open the log file in append mode
    miner_log_file = open(miner_log_file_path, "w")
    # Prepare to run Bob as miner
    # Start the miner process in the background
    process = await start_miner(bob_wallet, netuid)
    # Function to read and log output
    async def log_output(stream, log_prefix, log_file):
        while True:
            line = await stream.readline()
            if not line:
                break
            decoded_line = line.decode().strip()
            print(f"{log_prefix}{decoded_line}")  # Print to console
            log_file.write(f"{log_prefix}{decoded_line}\n")  # Write to file
            log_file.flush()

    # Start logging both stdout and stderr in the background
    asyncio.create_task(log_output(process.stdout, "[STDOUT] ", miner_log_file))
    asyncio.create_task(log_output(process.stderr, "[STDERR] ", miner_log_file))

    print("Neuron Bob is now mining")
    await asyncio.sleep(
        5
    )  # wait for 5 seconds for the metagraph to refresh with latest data

    validator_log_file_path = "validator_incentive.log"
    # Open the log file in append mode
    validator_log_file = open(validator_log_file_path, "w")
    # Prepare to run Alice as validator
    # Start the validator process in the background
    process = await start_validator(alice_wallet, netuid)

    # Start logging both stdout and stderr in the background
    asyncio.create_task(log_output(process.stdout, "[STDOUT] ", validator_log_file))
    asyncio.create_task(log_output(process.stderr, "[STDERR] ", validator_log_file))

    print("Neuron Alice is now validating")
    await asyncio.sleep(
        5
    )  # wait for 5 seconds for the metagraph and subtensor to refresh with latest data

    # Get latest metagraph
    metagraph = Metagraph(netuid=netuid, network="ws://localhost:9945")

    # Get current miner/validator stats
    bob_neuron = metagraph.neurons[1]
    assert bob_neuron.incentive == 0
    assert bob_neuron.consensus == 0
    assert bob_neuron.rank == 0
    assert bob_neuron.trust == 0

    alice_neuron = metagraph.neurons[0]
    assert alice_neuron.validator_permit is False
    assert alice_neuron.dividends == 0
    assert alice_neuron.stake.tao == 10_000.0
    assert alice_neuron.validator_trust == 0

    # Wait until next epoch
    await wait_epoch(subtensor)

    # Set weights by Alice on the subnet
    do_set_weights(
        self=subtensor,
        wallet=alice_wallet,
        uids=[1],
        vals=[65535],
        netuid=netuid,
        version_key=0,
        wait_for_inclusion=True,
        wait_for_finalization=True,
        period=5 * FAST_BLOCKS_SPEEDUP_FACTOR,
    )
    print("Alice neuron set weights successfully")

    await wait_epoch(subtensor)

    # Refresh metagraph
    metagraph = Metagraph(netuid=netuid, network="ws://localhost:9945")

    # Get current emissions and validate that Alice has gotten tao
    bob_neuron = metagraph.neurons[1]
    assert bob_neuron.incentive == 1
    assert bob_neuron.consensus == 1
    assert bob_neuron.rank == 1
    assert bob_neuron.trust == 1

    alice_neuron = metagraph.neurons[0]
    assert alice_neuron.validator_permit is True
    assert alice_neuron.dividends == 1
    assert alice_neuron.stake.tao == 10_000.0
    assert alice_neuron.validator_trust == 1

    print("✅ Passed test_incentive")
    cleanup_miner()
    cleanup_validator()
