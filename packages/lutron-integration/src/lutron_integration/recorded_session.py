"""
This is a little shim for recording and replaying a stream connection.
It's a nearly 100% vibe-coded implementation, and it's extremely sloppy.
The actual underlying plumbing is absurd (seriously, it uses sockets to
communicate with itself!).

To be slightly fair, Python's StreamReader and StreamWriter are pretty
awful -- there is no documented way to create a StreamReader or StreamWriter
that isn't backed by a socket.  OTOH, this little module uses StreamReaderLike,
etc., and Python's streams.py strongly suggests that one could attach it
to a different protocol and transport.

Oh well.
"""

import asyncio
import contextlib
import json
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self, TypedDict
from . import connection


_CHUNK_SIZE = 65536


Direction = Literal["incoming", "outgoing"]


class JsonSessionEvent(TypedDict):
    dir: Direction
    contents: str


@dataclass(frozen=True)
class SessionEvent:
    direction: Direction
    contents: bytes

    def serialize(self) -> JsonSessionEvent:
        return {
            "dir": self.direction,
            "contents": self.contents.decode("latin-1"),
        }

    @classmethod
    def deserialize_from(cls, event: JsonSessionEvent) -> Self:
        direction = event["dir"]
        if direction not in ("incoming", "outgoing"):
            raise ValueError(f"Unsupported session direction: {direction!r}")
        return cls(direction=direction, contents=event["contents"].encode("latin-1"))

def session_to_jsonable(events: Sequence[SessionEvent]) -> list[JsonSessionEvent]:
    return [event.serialize() for event in events]


def session_from_jsonable(events: Sequence[JsonSessionEvent]) -> list[SessionEvent]:
    return [SessionEvent.deserialize_from(event) for event in events]


def write_session_event_jsonl_line(file, event: SessionEvent) -> None:
    file.write(json.dumps(event.serialize()) + "\n")


def read_session_file(path: str | Path) -> list[SessionEvent]:
    events: list[SessionEvent] = []
    with Path(path).open(encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            events.append(SessionEvent.deserialize_from(data))
    return events


async def create_stream_pair() -> tuple[
    tuple[connection.StreamReaderLike, connection.StreamWriterLike],
    tuple[connection.StreamReaderLike, connection.StreamWriterLike],
]:
    try:
        sockets = socket.socketpair()
    except (AttributeError, OSError):
        sockets = None

    if sockets is not None:
        try:
            for sock in sockets:
                sock.setblocking(False)
            left = await asyncio.open_connection(sock=sockets[0])
            right = await asyncio.open_connection(sock=sockets[1])
            return left, right
        except Exception:
            for sock in sockets:
                with contextlib.suppress(OSError):
                    sock.close()

    accepted: asyncio.Future[
        tuple[connection.StreamReaderLike, connection.StreamWriterLike]
    ] = asyncio.get_running_loop().create_future()

    async def handle_client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if not accepted.done():
            accepted.set_result((reader, writer))

    server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
    try:
        sock = server.sockets[0]
        host, port = sock.getsockname()[0:2]
        left = await asyncio.open_connection(host, port)
        right = await accepted
        return left, right
    finally:
        server.close()
        await server.wait_closed()


class RecordedStreamWriter:
    def __init__(
        self,
        proxy_writer: connection.StreamWriterLike,
        remote_writer: connection.StreamWriterLike,
        tasks: list[asyncio.Task[None]],
    ) -> None:
        self._proxy_writer = proxy_writer
        self._remote_writer = remote_writer
        self._tasks = tasks

    def write(self, data: bytes) -> None:
        self._proxy_writer.write(data)

    async def drain(self) -> None:
        await self._proxy_writer.drain()

    def close(self) -> None:
        self._proxy_writer.close()

    def is_closing(self) -> bool:
        return self._proxy_writer.is_closing()

    async def wait_closed(self) -> None:
        with contextlib.suppress(Exception):
            await self._proxy_writer.wait_closed()

        for task in self._tasks:
            with contextlib.suppress(Exception):
                await task

        if not self._remote_writer.is_closing():
            self._remote_writer.close()
            with contextlib.suppress(Exception):
                await self._remote_writer.wait_closed()

        self._tasks.clear()


class _OutgoingVerificationState:
    def __init__(self) -> None:
        self.written_bytes = 0
        self.verified_bytes = 0
        self.progress_event = asyncio.Event()
        self.observed_failure: BaseException | None = None


class _VerifiedStreamWriter:
    def __init__(
        self,
        writer: connection.StreamWriterLike,
        verify_task: asyncio.Task[None],
        verify_state: _OutgoingVerificationState,
    ) -> None:
        self._writer = writer
        self._verify_task = verify_task
        self._verify_state = verify_state

    def _raise_if_verify_failed(self) -> None:
        if not self._verify_task.done():
            return

        exception = self._verify_task.exception()
        if exception is not None:
            self._verify_state.observed_failure = exception
            raise exception

    def write(self, data: bytes) -> None:
        self._raise_if_verify_failed()
        self._writer.write(data)
        self._verify_state.written_bytes += len(data)

    async def drain(self) -> None:
        await self._writer.drain()
        while (
            self._verify_state.verified_bytes < self._verify_state.written_bytes
            and not self._verify_task.done()
        ):
            self._verify_state.progress_event.clear()
            await self._verify_state.progress_event.wait()
        self._raise_if_verify_failed()

    def close(self) -> None:
        self._writer.close()

    def is_closing(self) -> bool:
        return self._writer.is_closing()

    async def wait_closed(self) -> None:
        await self._writer.wait_closed()
        await asyncio.sleep(0)
        self._raise_if_verify_failed()


async def open_recorded_stream(
    host: str,
    port: int,
    event_callback: Callable[[SessionEvent], None],
) -> tuple[connection.StreamReaderLike, RecordedStreamWriter]:
    remote_reader, remote_writer = await asyncio.open_connection(host, port)
    (reader, proxy_writer), (proxy_reader, proxy_peer_writer) = await create_stream_pair()

    async def pump(
        src: connection.StreamReaderLike,
        dst: connection.StreamWriterLike,
        direction: Direction,
    ) -> None:
        try:
            while True:
                data = await src.read(_CHUNK_SIZE)
                if not data:
                    return
                event_callback(SessionEvent(direction=direction, contents=data))
                dst.write(data)
                await dst.drain()
        finally:
            if not dst.is_closing():
                dst.close()
                with contextlib.suppress(Exception):
                    await dst.wait_closed()

    tasks = [
        asyncio.create_task(pump(proxy_reader, remote_writer, "outgoing")),
        asyncio.create_task(pump(remote_reader, proxy_peer_writer, "incoming")),
    ]
    return reader, RecordedStreamWriter(proxy_writer, remote_writer, tasks)


class ReplayedSession:
    def __init__(self, events: Sequence[SessionEvent]) -> None:
        self._events = list(events)
        self.reader: connection.StreamReaderLike | None = None
        self.writer: connection.StreamWriterLike | None = None
        self._peer_reader: connection.StreamReaderLike | None = None
        self._peer_writer: connection.StreamWriterLike | None = None
        self._incoming_task: asyncio.Task[None] | None = None
        self._outgoing_task: asyncio.Task[None] | None = None
        self._outgoing_verify_state = _OutgoingVerificationState()

    async def __aenter__(self) -> "ReplayedSession":
        (self.reader, writer), (self._peer_reader, self._peer_writer) = (
            await create_stream_pair()
        )
        self._incoming_task = asyncio.create_task(self._play_incoming())
        self._outgoing_task = asyncio.create_task(self._verify_outgoing())
        self.writer = _VerifiedStreamWriter(
            writer, self._outgoing_task, self._outgoing_verify_state
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def _play_incoming(self) -> None:
        assert self._peer_writer is not None
        for event in self._events:
            if event.direction != "incoming":
                continue
            self._peer_writer.write(event.contents)
            await self._peer_writer.drain()

    async def _verify_outgoing(self) -> None:
        assert self._peer_reader is not None
        expected = b"".join(
            event.contents for event in self._events if event.direction == "outgoing"
        )
        actual = bytearray()
        while True:
            data = await self._peer_reader.read(_CHUNK_SIZE)
            if not data:
                break
            next_expected = expected[len(actual) : len(actual) + len(data)]
            if len(next_expected) != len(data) or data != next_expected:
                actual.extend(data)
                self._outgoing_verify_state.verified_bytes = len(actual)
                self._outgoing_verify_state.progress_event.set()
                raise AssertionError(
                    f"Outgoing stream mismatch:\nexpected={expected!r}\nactual={bytes(actual)!r}"
                )
            actual.extend(data)
            self._outgoing_verify_state.verified_bytes = len(actual)
            self._outgoing_verify_state.progress_event.set()

        if bytes(actual) != expected:
            raise AssertionError(
                f"Outgoing stream mismatch:\nexpected={expected!r}\nactual={bytes(actual)!r}"
            )

    async def aclose(self) -> None:
        if self.writer is not None and not self.writer.is_closing():
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()

        if self._incoming_task is not None:
            await self._incoming_task

        if self._peer_writer is not None and not self._peer_writer.is_closing():
            self._peer_writer.close()
            with contextlib.suppress(Exception):
                await self._peer_writer.wait_closed()

        if self._outgoing_task is not None:
            try:
                await self._outgoing_task
            except AssertionError as err:
                if self._outgoing_verify_state.observed_failure is not err:
                    raise
