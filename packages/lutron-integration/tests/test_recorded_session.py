import asyncio
import json
from pathlib import Path

import pytest

from lutron_integration.recorded_session import (
    ReplayedSession,
    SessionEvent,
    create_stream_pair,
    open_recorded_stream,
    read_session_file,
    session_from_jsonable,
    session_to_jsonable,
    write_session_event_jsonl_line,
)


def test_session_json_round_trip_handles_all_bytes() -> None:
    raw = bytes(range(256))
    events = [SessionEvent(direction="incoming", contents=raw)]

    encoded = json.dumps(session_to_jsonable(events))
    decoded = json.loads(encoded)

    assert session_from_jsonable(decoded) == events


def test_session_event_serialization_round_trip() -> None:
    event = SessionEvent(direction="outgoing", contents=b"\x00abc\xff\r\n")

    assert SessionEvent.deserialize_from(event.serialize()) == event


def test_create_stream_pair_transfers_in_both_directions() -> None:
    async def run_test() -> None:
        (left_reader, left_writer), (right_reader, right_writer) = (
            await create_stream_pair()
        )

        left_writer.write(b"left-to-right")
        await left_writer.drain()
        assert await right_reader.readexactly(13) == b"left-to-right"

        right_writer.write(b"right-to-left")
        await right_writer.drain()
        assert await left_reader.readexactly(13) == b"right-to-left"

        left_writer.close()
        right_writer.close()
        await left_writer.wait_closed()
        await right_writer.wait_closed()

    asyncio.run(run_test())


def test_replayed_session_raises_on_outgoing_mismatch() -> None:
    async def run_test() -> None:
        with pytest.raises(AssertionError, match="Outgoing stream mismatch"):
            async with ReplayedSession(
                [
                    SessionEvent(direction="incoming", contents=b"abc"),
                    SessionEvent(direction="outgoing", contents=b"expected"),
                ]
            ) as replayed:
                assert replayed.reader is not None
                assert replayed.writer is not None

                assert await replayed.reader.readexactly(3) == b"abc"
                replayed.writer.write(b"actual")
                await replayed.writer.drain()

    asyncio.run(run_test())


def test_replayed_session_raises_on_first_inconsistent_write() -> None:
    async def run_test() -> None:
        async with ReplayedSession(
            [
                SessionEvent(direction="outgoing", contents=b"hello"),
                SessionEvent(direction="outgoing", contents=b"world"),
            ]
        ) as replayed:
            assert replayed.writer is not None

            replayed.writer.write(b"hello")
            await replayed.writer.drain()

            replayed.writer.write(b"there")
            with pytest.raises(AssertionError, match="Outgoing stream mismatch"):
                await replayed.writer.drain()

    asyncio.run(run_test())


def test_replayed_session_handles_multiple_reads_and_writes() -> None:
    async def run_test() -> None:
        async with ReplayedSession(
            [
                SessionEvent(direction="incoming", contents=b"abc"),
                SessionEvent(direction="incoming", contents=b"defg"),
                SessionEvent(direction="outgoing", contents=b"hello"),
                SessionEvent(direction="outgoing", contents=b"world"),
            ]
        ) as replayed:
            assert replayed.reader is not None
            assert replayed.writer is not None

            assert await replayed.reader.readexactly(2) == b"ab"
            assert await replayed.reader.readexactly(2) == b"cd"
            assert await replayed.reader.readexactly(3) == b"efg"

            replayed.writer.write(b"he")
            await replayed.writer.drain()
            replayed.writer.write(b"llo")
            await replayed.writer.drain()
            replayed.writer.write(b"wor")
            await replayed.writer.drain()
            replayed.writer.write(b"ld")
            await replayed.writer.drain()

    asyncio.run(run_test())


def test_write_session_event_jsonl_line_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    events = [
        SessionEvent(direction="incoming", contents=b"abc\r\n"),
        SessionEvent(direction="outgoing", contents=b"\x00\xff"),
    ]

    with path.open("w", encoding="utf-8") as file:
        for event in events:
            write_session_event_jsonl_line(file, event)

    assert read_session_file(path) == events


def test_open_recorded_stream_captures_both_directions(tmp_path: Path) -> None:
    async def run_test() -> None:
        async def handle_client(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            writer.write(b"server->")
            await writer.drain()
            writer.write(b"client")
            await writer.drain()

            assert await reader.readexactly(7) == b"client-"
            assert await reader.readexactly(7) == b">server"
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
        try:
            sock = server.sockets[0]
            host, port = sock.getsockname()[0:2]

            events: list[SessionEvent] = []

            reader, writer = await open_recorded_stream(host, port, events.append)

            assert await reader.readexactly(7) == b"server-"
            assert await reader.readexactly(7) == b">client"

            writer.write(b"client-")
            await writer.drain()
            writer.write(b">server")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

            record_path = tmp_path / "session.jsonl"
            with record_path.open("w", encoding="utf-8") as file:
                for event in events:
                    write_session_event_jsonl_line(file, event)

            assert read_session_file(record_path) == [
                SessionEvent(direction="incoming", contents=b"server->client"),
                SessionEvent(direction="outgoing", contents=b"client->server"),
            ]
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run_test())
