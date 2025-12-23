#!/usr/bin/env python3
"""
Command-line tool to monitor unsolicited device updates from a Lutron QSE-CI-NWK-E hub.
"""

import asyncio
import argparse
import getpass
import logging
import sys

from lutron_integration import connection, devices, qse


logging.basicConfig(level=logging.WARNING)


def print_device_table(universe: "qse.LutronUniverse") -> None:
    """Print a nicely formatted table of devices."""
    if not universe.devices_by_sn:
        print("No devices found.", file=sys.stderr)
        return

    # Prepare data for table
    rows = []
    for sn in sorted(universe.devices_by_sn.keys(), key=lambda x: x.sn):
        device = universe.devices_by_sn[sn]
        integration_id = device.integration_id.decode("utf-8", errors="replace")
        if integration_id == "(Not Set)":
            integration_id = ""
        family = device.family.decode("utf-8", errors="replace")
        product = device.product.decode("utf-8", errors="replace")
        rows.append((sn.sn.decode("utf-8"), integration_id, family, product))

    # Calculate column widths
    col_widths = [
        max(len("Serial Number"), max(len(row[0]) for row in rows)),
        max(
            len("Integration ID"),
            max(len(row[1]) for row in rows) if any(row[1] for row in rows) else 0,
        ),
        max(len("Family"), max(len(row[2]) for row in rows)),
        max(len("Product"), max(len(row[3]) for row in rows)),
    ]

    # Print header
    header = f"{'Serial Number':<{col_widths[0]}}  {'Integration ID':<{col_widths[1]}}  {'Family':<{col_widths[2]}}  {'Product':<{col_widths[3]}}"
    print(header, file=sys.stderr)
    print("-" * len(header), file=sys.stderr)

    # Print rows
    for row in rows:
        line = f"{row[0]:<{col_widths[0]}}  {row[1]:<{col_widths[1]}}  {row[2]:<{col_widths[2]}}  {row[3]:<{col_widths[3]}}"
        print(line, file=sys.stderr)

    print("", file=sys.stderr)


def format_device_update(
    update: devices.DeviceUpdate, universe: "qse.LutronUniverse"
) -> str:
    """Format a DeviceUpdate nicely for display."""
    sn = update.serial_number
    device = universe.devices_by_sn.get(sn)

    # Build the basic info
    parts = []

    # Serial number
    sn_str = sn.sn.decode("utf-8", errors="replace")
    parts.append(f"SN: {sn_str}")

    # Integration ID if present
    if device and device.integration_id != b"(Not Set)":
        iid = device.integration_id.decode("utf-8", errors="replace")
        parts.append(f"IID: {iid}")

    # Component number, group name, and index
    component_str = f"Component: {update.component}"
    if device:
        if group_info := _lookup_component_group(device, update.component):
            group_name, index = group_info
            component_str += f" ({group_name}/{index})"
    parts.append(component_str)

    # Action name and number
    action_name = update.action.name
    action_num = update.action.value
    parts.append(f"Action: {action_name}({action_num})")

    # Parameters
    if update.value:
        params_str = ", ".join(
            v.decode("utf-8", errors="replace") for v in update.value
        )
        parts.append(f"Value: {params_str}")

    return " | ".join(parts)


def _lookup_component_group(
    device_details: qse.DeviceDetails, component_num: int
) -> tuple[str, int] | None:
    """Look up a component group and index for a device.

    Args:
        device_details: The device details containing family information
        component_num: The component number to look up

    Returns:
        Tuple of (group_name, index) if found, None otherwise
    """
    # Find the device class for this family
    device_class = devices.FAMILY_TO_CLASS.get(device_details.family)
    if device_class is None:
        return None

    # Look up the component in the device class
    result = device_class.lookup_component(component_num)
    if result is None:
        return None

    group, index = result
    return (group.name, index)


async def monitor_device_updates(host: str, username: str, password: str) -> None:
    """
    Connect to a Lutron hub and monitor device updates.

    Args:
        host: IP address or hostname of the Lutron hub
        username: Username for authentication
        password: Password for authentication
    """
    reader, writer = await asyncio.open_connection(host, 23)

    try:
        # Log in to the device
        conn = await connection.login(
            reader,
            writer,
            username.encode("utf-8"),
            password.encode("utf-8") if password else None,
        )

        # Enumerate the universe to get the iidmap
        print(f"Connected to {host}. Enumerating devices...", file=sys.stderr)
        universe = await qse.enumerate_universe(conn)
        print(f"Found {len(universe.devices_by_sn)} devices.\n", file=sys.stderr)

        # Print device table
        print_device_table(universe)

        print("Monitoring for device updates. Press Ctrl-C to exit.\n", file=sys.stderr)

        # Monitor unsolicited messages
        while True:
            try:
                message = await conn.read_unsolicited()

                # Try to decode the message
                decoded = devices.decode_device_update(message, universe.iidmap)
                if decoded:
                    print(format_device_update(decoded, universe))
                else:
                    # Fall back to raw repr if decode fails
                    print(repr(message))
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error processing message: {e}", file=sys.stderr)

    except connection.LoginError as e:
        print(f"Login failed: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Connection error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        writer.close()
        await writer.wait_closed()


def main() -> None:
    """Main entry point for the lutron_monitor CLI."""
    parser = argparse.ArgumentParser(
        description="Monitor unsolicited device updates from a Lutron QSE-CI-NWK-E hub"
    )
    parser.add_argument("host", help="IP address or hostname of the Lutron hub")
    parser.add_argument(
        "-u",
        "--username",
        default="nwk2",
        help="Username for authentication (default: nwk2)",
    )

    args = parser.parse_args()

    # Prompt for password
    password = getpass.getpass("Password: ")

    # Run the monitoring loop
    try:
        asyncio.run(monitor_device_updates(args.host, args.username, password))
    except KeyboardInterrupt:
        print("\nDisconnected.", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
