# start the chain
# register a wallet
# register neuron
# start validator.py with wallet/neuron
# validate (metagraph, logs)

# start the chain
# register a wallet
# register neuron
# start miner.py with wallet/neuron
# validate (metagraph, logs)
import asyncio
import atexit
import os
import sys

import bittensor
import pytest
from bittensor import Subtensor, Metagraph, Balance
from bittensor.utils import networking

from tests.e2e_tests.tools.chain_interactions import register_subnet, add_stake, wait_epoch
from tests.e2e_tests.tools.e2e_test_utils import setup_wallet


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
async def test_validator(local_chain):
    """
    Test the Dendrite mechanism and successful registration on the network.

    Steps:
        1. Register a subnet and register Alice
        2. Check if metagraph.axon is updated and check axon attributes
        3. Run Alice as a miner on the subnet
        4. Check the metagraph again after running the miner and verify all attributes
    Raises:
        AssertionError: If any of the checks or verifications fail
    """

    print("Testing test_axon")

    netuid = 1
    # Register root as Alice - the subnet owner
    alice_keypair, wallet = setup_wallet("//Alice")

    subtensor = Subtensor(network="ws://localhost:9945")

    # Register a subnet, netuid 1
    assert register_subnet(local_chain, wallet), "Subnet wasn't created"

    # Verify subnet <netuid 1> created successfully
    assert local_chain.query(
        "SubtensorModule", "NetworksAdded", [netuid]
    ).serialize(), "Subnet wasn't created successfully"

    # Register Bob
    bob_keypair, bob_wallet = setup_wallet("//Bob")

    subtensor = Subtensor(network="ws://localhost:9945")

    # Register Bob to the network
    assert subtensor.burned_register(
        bob_wallet, netuid
    ), "Unable to register Bob as a neuron"

    metagraph = Metagraph(netuid=netuid, network="ws://localhost:9945")

    # Assert one neuron is Bob
    assert len(subtensor.neurons(netuid=netuid)) == 1
    neuron = metagraph.neurons[0]
    assert neuron.hotkey == bob_keypair.ss58_address
    assert neuron.coldkey == bob_keypair.ss58_address

    # Assert stake is 0
    assert neuron.stake.tao == 0

    # Stake to become to top neuron after the first epoch
    assert add_stake(local_chain, bob_wallet, Balance.from_tao(10_000))

    # Refresh metagraph
    metagraph = Metagraph(netuid=netuid, network="ws://localhost:9945")
    old_neuron = metagraph.neurons[0]

    # Assert stake is 10000
    assert (
        old_neuron.stake.tao == 10_000.0
    ), f"Expected 10_000.0 staked TAO, but got {neuron.stake.tao}"

    # Assert neuron is not a validator yet
    assert old_neuron.active is True
    assert old_neuron.validator_permit is False
    assert old_neuron.validator_trust == 0.0
    assert old_neuron.pruning_score == 0

    log_file_path = "validator.logs"
    # Open the log file in append mode

    log_file = open(log_file_path, "w")

    # Start the validator process in the background
    process = await start_validator(bob_wallet, netuid)

    # Function to read and log output
    async def log_output(stream, log_prefix):
        while True:
            line = await stream.readline()
            if not line:
                break
            decoded_line = line.decode().strip()
            print(f"{log_prefix}{decoded_line}")  # Print to console
            log_file.write(f"{log_prefix}{decoded_line}\n")  # Write to file
            log_file.flush()

    # Start logging both stdout and stderr in the background
    asyncio.create_task(log_output(process.stdout, "[STDOUT] "))
    asyncio.create_task(log_output(process.stderr, "[STDERR] "))

    # Don't wait for process to finish; allow test to proceed
    print("Validator process started in the background")

    # Continue with other test steps
    await asyncio.sleep(15)

    print("Neuron Alice is now validating")

    await asyncio.sleep(
        5
    )  # wait for 5 seconds for the metagraph and subtensor to refresh with latest data

    await wait_epoch(subtensor, netuid=netuid)

    # Refresh the metagraph
    metagraph = bittensor.Metagraph(netuid=netuid, network="ws://localhost:9945")

    # Refresh validator neuron
    updated_neuron = metagraph.neurons[0]

    assert len(metagraph.neurons) == 1
    assert updated_neuron.active is True
    assert updated_neuron.validator_permit is True
    assert updated_neuron.hotkey == bob_keypair.ss58_address
    assert updated_neuron.coldkey == bob_keypair.ss58_address
    assert updated_neuron.pruning_score != 0

    print("✅ Passed test_validator")

    #Ensure process is terminated
    cleanup_validator()