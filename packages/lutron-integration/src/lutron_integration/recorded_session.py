import asyncio
import contextlib
import json
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict


_CHUNK_SIZE = 65536


Direction = Literal["incoming", "outgoing"]


class JsonSessionEvent(TypedDict):
    dir: Direction
    contents: str


@dataclass(frozen=True)
class SessionEvent:
    direction: Direction
    contents: bytes


def _encode_contents(data: bytes) -> str:
    return data.decode("latin-1")


def _decode_contents(data: str) -> bytes:
    return data.encode("latin-1")


def session_to_jsonable(events: Sequence[SessionEvent]) -> list[JsonSessionEvent]:
    return [
        {"dir": event.direction, "contents": _encode_contents(event.contents)}
        for event in events
    ]


def session_from_jsonable(events: Sequence[JsonSessionEvent]) -> list[SessionEvent]:
    out: list[SessionEvent] = []
    for event in events:
        direction = event["dir"]
        if direction not in ("incoming", "outgoing"):
            raise ValueError(f"Unsupported session direction: {direction!r}")
        out.append(
            SessionEvent(
                direction=direction, contents=_decode_contents(event["contents"])
            )
        )
    return out


def write_session_file(path: str | Path, events: Sequence[SessionEvent]) -> None:
    Path(path).write_text(json.dumps(session_to_jsonable(events), indent=2) + "\n")


def read_session_file(path: str | Path) -> list[SessionEvent]:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list):
        raise ValueError("Session file must contain a JSON array")
    return session_from_jsonable(data)


async def create_stream_pair() -> tuple[
    tuple[asyncio.StreamReader, asyncio.StreamWriter],
    tuple[asyncio.StreamReader, asyncio.StreamWriter],
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
        tuple[asyncio.StreamReader, asyncio.StreamWriter]
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
        proxy_writer: asyncio.StreamWriter,
        remote_writer: asyncio.StreamWriter,
        tasks: list[asyncio.Task[None]],
        events: list[SessionEvent],
    ) -> None:
        self._proxy_writer = proxy_writer
        self._remote_writer = remote_writer
        self._tasks = tasks
        self._events = events

    @property
    def events(self) -> list[SessionEvent]:
        return list(self._events)

    def write(self, data: bytes) -> None:
        self._proxy_writer.write(data)

    def writelines(self, data: Sequence[bytes]) -> None:
        self._proxy_writer.writelines(data)

    def write_eof(self) -> None:
        self._proxy_writer.write_eof()

    def can_write_eof(self) -> bool:
        return self._proxy_writer.can_write_eof()

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

    def get_extra_info(self, name: str, default=None):
        return self._proxy_writer.get_extra_info(name, default)

    def write_session_file(self, path: str | Path) -> None:
        write_session_file(path, self._events)


async def open_recorded_connection(
    host: str, port: int
) -> tuple[asyncio.StreamReader, RecordedStreamWriter]:
    remote_reader, remote_writer = await asyncio.open_connection(host, port)
    (reader, proxy_writer), (proxy_reader, proxy_peer_writer) = await create_stream_pair()
    events: list[SessionEvent] = []

    async def pump(
        src: asyncio.StreamReader,
        dst: asyncio.StreamWriter,
        direction: Direction,
    ) -> None:
        try:
            while True:
                data = await src.read(_CHUNK_SIZE)
                if not data:
                    return
                events.append(SessionEvent(direction=direction, contents=data))
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
    return reader, RecordedStreamWriter(proxy_writer, remote_writer, tasks, events)


class ReplayedSession:
    def __init__(self, events: Sequence[SessionEvent | JsonSessionEvent]) -> None:
        if events and isinstance(events[0], dict):
            self._events = session_from_jsonable(events)  # type: ignore[arg-type]
        else:
            self._events = list(events)  # type: ignore[arg-type]
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._peer_reader: asyncio.StreamReader | None = None
        self._peer_writer: asyncio.StreamWriter | None = None
        self._incoming_task: asyncio.Task[None] | None = None
        self._outgoing_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "ReplayedSession":
        (self.reader, self.writer), (self._peer_reader, self._peer_writer) = (
            await create_stream_pair()
        )
        self._incoming_task = asyncio.create_task(self._play_incoming())
        self._outgoing_task = asyncio.create_task(self._verify_outgoing())
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
            actual.extend(data)

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
            await self._outgoing_task
