#!/bin/bash

generate_password() {
    openssl rand -base64 20
}

# Try to find the correct path to the Redis config file
REDIS_CONF="/usr/local/etc/redis.conf"
if [ ! -f "$REDIS_CONF" ]; then
    REDIS_CONF="/opt/homebrew/etc/redis.conf"  # Apple Silicon (M1, M2) Macs
fi

# Check if the Redis config file exists
if [ ! -f "$REDIS_CONF" ]; then
    echo "Redis configuration file not found at $REDIS_CONF"
    exit 1
fi

# Create the directory if it doesn't exist
if [ ! -d "/etc/redis" ]; then
    sudo mkdir -p /etc/redis
    echo "Created /etc/redis directory"
fi

# Create the symlink if it doesn't already exist
if [ ! -L "/etc/redis/redis.conf" ]; then
    sudo ln -s $REDIS_CONF /etc/redis/redis.conf
    echo "Created symlink for redis.conf"
fi

# Generate a new Redis password
REDIS_PASSWORD=$(generate_password)

# Backup the config file
sudo cp $REDIS_CONF "${REDIS_CONF}.bak"

# Update Redis configuration with the password
if sudo grep -q "^requirepass " $REDIS_CONF; then
    # Update the existing requirepass line
    sudo sed -i '' "s/^requirepass .*/requirepass $REDIS_PASSWORD/" $REDIS_CONF
elif sudo grep -q "^# *requirepass " $REDIS_CONF; then
    # Uncomment and update the requirepass line
    sudo sed -i '' "s/^# *requirepass .*/requirepass $REDIS_PASSWORD/" $REDIS_CONF
else
    # Add a new requirepass line at the end of the file
    echo "requirepass $REDIS_PASSWORD" | sudo tee -a $REDIS_CONF > /dev/null
fi

# Restart Redis server using brew services
brew services restart redis

# Export password for the current session
export REDIS_PASSWORD

# Check the status of Redis (using brew services status)
brew services list | grep redis

# Verify that the password was set in the configuration file
echo "Password set in Redis config:"

# Extract and set the REDIS_PASSWORD from the config
export REDIS_PASSWORD=$(sudo awk -F ' ' '/^requirepass/ {print $2}' $REDIS_CONF)

echo $REDIS_PASSWORD
