
#!/usr/bin/env bash

set -Eeuo pipefail

# XPDevs NetworkOS - Administrator Recovery Tool

NETWORKOSCTL="/usr/local/sbin/networkosctl"

echo
echo "======================================"
echo "       XPDevs NetworkOS"
echo "       Administrator Recovery"
echo "======================================"
echo

# Require root privileges
if [[ "$EUID" -ne 0 ]]; then
    echo "Please run this script with sudo:"
    echo "sudo bash admin-recovery.sh"
    exit 1
fi

# Check NetworkOS installation
if [[ ! -x "$NETWORKOSCTL" ]]; then
    echo "ERROR: NetworkOS control utility not found."
    echo "Expected: $NETWORKOSCTL"
    exit 1
fi

# Get username
while true; do
    read -r -p "Enter administrator username: " username

    if [[ "$username" =~ ^[a-zA-Z0-9._-]{3,32}$ ]]; then
        break
    fi

    echo "Invalid username. Use 3-32 letters, numbers, dots, underscores or hyphens."
done

# Get password
while true; do
    echo
    read -r -s -p "Enter new password: " password
    echo

    read -r -s -p "Confirm new password: " confirm_password
    echo

    if [[ -z "$password" ]]; then
        echo "Password cannot be empty."
        continue
    fi

    if [[ "$password" != "$confirm_password" ]]; then
        echo "Passwords do not match. Try again."
        continue
    fi

    break
done

echo
echo "Updating administrator account..."

# Create account or update existing account
if printf '%s\n' "$password" | "$NETWORKOSCTL" admin-create \
    --username "$username" \
    --password-stdin; then

    echo
    echo "SUCCESS: Administrator account updated."
    echo "Username: $username"
    echo "You can now sign in to NetworkOS."

else
    echo
    echo "ERROR: Administrator recovery failed."
    echo "Check the password requirements and try again."
    unset password confirm_password
    exit 1
fi

# Clear password variables
unset password confirm_password

echo
