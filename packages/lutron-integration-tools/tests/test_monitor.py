"""Tests for the monitor CLI tool."""

import asyncio
from argparse import Namespace
from unittest.mock import AsyncMock, MagicMock, patch

from lutron_integration import devices, types, qse
from lutron_integration_tools import monitor
from lutron_integration_tools.monitor import format_device_update


def test_format_device_update_basic():
    """Test basic device update formatting."""
    # Create a test device update
    sn = types.SerialNumber(b"1234567A")
    update = devices.DeviceUpdate(
        serial_number=sn,
        component=8,
        action=types.DeviceAction.LIGHT_LEVEL,
        value=(b"100.00",),
    )

    # Create a minimal universe with the device
    device_details = qse.DeviceDetails(
        sn=sn,
        integration_id=b"PH Grafik Eye",
        family=b"GRAFIK_EYE(2)",
        product=b"QSG-ECO(2)",
        raw_attrs={},
    )
    universe = qse.LutronUniverse(
        devices_by_sn={sn: device_details}, iidmap=types.IntegrationIDMap()
    )

    # Format the update
    result = format_device_update(update, universe)

    # Check the output
    assert "SN: 1234567A" in result
    assert "IID: PH Grafik Eye" in result
    assert "Component: 8 (ZONE/8)" in result  # Should resolve to ZONE component group
    assert "Action: LIGHT_LEVEL(14)" in result
    assert "Value: 100.00" in result
    assert "DeviceUpdate" not in result


def test_format_device_update_no_iid():
    """Test formatting when device has no integration ID."""
    sn = types.SerialNumber(b"00F535EB")
    update = devices.DeviceUpdate(
        serial_number=sn,
        component=2,
        action=types.DeviceAction.LIGHT_LEVEL,
        value=(b"50.00",),
    )

    # Device with no integration ID
    device_details = qse.DeviceDetails(
        sn=sn,
        integration_id=b"(Not Set)",
        family=b"KEYPAD(1)",
        product=b"QSWS2-GB(1)",
        raw_attrs={},
    )
    universe = qse.LutronUniverse(
        devices_by_sn={sn: device_details}, iidmap=types.IntegrationIDMap()
    )

    result = format_device_update(update, universe)

    assert "SN: 00F535EB" in result
    assert "IID:" not in result  # Should not appear if Not Set
    assert "Component: 2" in result
    assert "Value: 50.00" in result


def test_format_device_update_no_value():
    """Test formatting when there's no value."""
    sn = types.SerialNumber(b"00F535EB")
    update = devices.DeviceUpdate(
        serial_number=sn, component=1, action=types.DeviceAction.LIGHT_LEVEL, value=()
    )

    device_details = qse.DeviceDetails(
        sn=sn,
        integration_id=b"(Not Set)",
        family=b"KEYPAD(1)",
        product=b"QSWS2-GB(1)",
        raw_attrs={},
    )
    universe = qse.LutronUniverse(
        devices_by_sn={sn: device_details}, iidmap=types.IntegrationIDMap()
    )

    result = format_device_update(update, universe)

    assert "SN: 00F535EB" in result
    assert "Component: 1" in result
    assert "Action: LIGHT_LEVEL(14)" in result
    assert "Value:" not in result  # Should not appear if no value


def test_format_device_update_multiple_values():
    """Test formatting with multiple parameter values."""
    sn = types.SerialNumber(b"02A6DF67")
    update = devices.DeviceUpdate(
        serial_number=sn,
        component=1,
        action=types.DeviceAction.PRESS_CLOSE_UNOCC,
        value=(b"1", b"2", b"3"),
    )

    device_details = qse.DeviceDetails(
        sn=sn,
        integration_id=b"PH Skylight W",
        family=b"SHADES(3)",
        product=b"ROLLER(1)",
        raw_attrs={},
    )
    universe = qse.LutronUniverse(
        devices_by_sn={sn: device_details}, iidmap=types.IntegrationIDMap()
    )

    result = format_device_update(update, universe)

    assert "SN: 02A6DF67" in result
    assert "IID: PH Skylight W" in result
    assert "Value: 1, 2, 3" in result
    # Should not have byte literals
    assert "b'" not in result


def test_format_device_update_unknown_device():
    """Test formatting for an unknown device."""
    sn = types.SerialNumber(b"FFFFFFFF")
    update = devices.DeviceUpdate(
        serial_number=sn,
        component=5,
        action=types.DeviceAction.LIGHT_LEVEL,
        value=(b"75.00",),
    )

    # Empty universe - device not found
    universe = qse.LutronUniverse(devices_by_sn={}, iidmap=types.IntegrationIDMap())

    result = format_device_update(update, universe)

    assert "SN: FFFFFFFF" in result
    assert "Component: 5" in result
    assert "Action: LIGHT_LEVEL(14)" in result
    assert "Value: 75.00" in result


def test_main_passes_record_option() -> None:
    with (
        patch.object(
            monitor.argparse.ArgumentParser,
            "parse_args",
            return_value=Namespace(
                host="192.0.2.10", username="nwk2", record="session.json"
            ),
        ),
        patch.object(monitor.getpass, "getpass", return_value="secret"),
        patch.object(monitor.asyncio, "run") as mock_run,
    ):
        monitor.main()

    mock_run.assert_called_once()
    coro = mock_run.call_args.args[0]
    assert coro.cr_code.co_name == "run"
    coro.close()


def test_monitor_device_updates_writes_recording(tmp_path) -> None:
    async def run_test() -> None:
        fake_reader = AsyncMock()
        fake_writer = MagicMock()
        fake_writer.close = MagicMock()
        fake_writer.wait_closed = AsyncMock(return_value=None)
        fake_writer.write_session_file = MagicMock()

        fake_conn = AsyncMock()
        fake_conn.read_unsolicited.side_effect = asyncio.CancelledError()

        universe = qse.LutronUniverse(
            devices_by_sn={}, iidmap=types.IntegrationIDMap()
        )

        with (
            patch.object(
                monitor.recorded_session,
                "open_recorded_connection",
                AsyncMock(return_value=(fake_reader, fake_writer)),
            ),
            patch.object(monitor.connection, "login", AsyncMock(return_value=fake_conn)),
            patch.object(
                monitor.qse, "enumerate_universe", AsyncMock(return_value=universe)
            ),
        ):
            async def run() -> None:
                reader, writer = await monitor.recorded_session.open_recorded_connection(
                    "192.0.2.10", 23
                )
                try:
                    await monitor.monitor_device_updates(
                        "192.0.2.10",
                        reader,
                        writer,
                        "nwk2",
                        "secret",
                    )
                finally:
                    writer.close()
                    await writer.wait_closed()
                writer.write_session_file(tmp_path / "recording.json")

            await run()

        fake_writer.close.assert_called_once()
        fake_writer.wait_closed.assert_awaited_once()
        fake_writer.write_session_file.assert_called_once()

    asyncio.run(run_test())
