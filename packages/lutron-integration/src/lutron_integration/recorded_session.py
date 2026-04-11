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


class _ReplayedStreamWriter:
    def __init__(self, expected: bytes) -> None:
        self._expected = expected
        self._actual = bytearray()
        self._is_closing = False
        self._failure: AssertionError | None = None
        self._failure_observed = False

    def _raise_mismatch(self) -> None:
        if self._failure is None:
            self._failure = AssertionError(
                f"Outgoing stream mismatch:\nexpected={self._expected!r}\nactual={bytes(self._actual)!r}"
            )
        self._failure_observed = True
        raise self._failure

    def write(self, data: bytes) -> None:
        if self._is_closing:
            raise RuntimeError("Cannot write to closing stream")

        next_expected = self._expected[len(self._actual) : len(self._actual) + len(data)]
        self._actual.extend(data)

        if len(next_expected) != len(data) or data != next_expected:
            self._raise_mismatch()

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self._is_closing = True

    def is_closing(self) -> bool:
        return self._is_closing

    async def wait_closed(self) -> None:
        self._is_closing = True
        if bytes(self._actual) != self._expected:
            if not self._failure_observed:
                self._raise_mismatch()


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
        self._incoming_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "ReplayedSession":
        reader = asyncio.StreamReader()
        self.reader = reader
        self.writer = _ReplayedStreamWriter(
            b"".join(
                event.contents
                for event in self._events
                if event.direction == "outgoing"
            )
        )
        self._incoming_task = asyncio.create_task(self._play_incoming())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def _play_incoming(self) -> None:
        assert isinstance(self.reader, asyncio.StreamReader)
        for event in self._events:
            if event.direction != "incoming":
                continue
            self.reader.feed_data(event.contents)
            await asyncio.sleep(0)
        self.reader.feed_eof()

    async def aclose(self) -> None:
        if self.writer is not None and not self.writer.is_closing():
            self.writer.close()
            await self.writer.wait_closed()

        if self._incoming_task is not None:
            await self._incoming_task
