import asyncio
from dataclasses import dataclass
import re
import collections
from collections.abc import Callable, Iterable
import logging
from . import types

_LOGGER = logging.getLogger(__name__)

# A message we received that, when converted to uppercase,
# starts with one of these prefixes, is considered to be a synchronous
# reply to a query.  Additionally, the empty string is a synchronous
# reply.  We consider a query to be answered when we receive a synchronous
# reply followed by a prompt.
#
# This appears to be completely reliably on QSE-CI-NWK-E with one
# exception: #OUTPUT and ?OUTPUT commands, at least on verrsion
# 8.60, produce no output per se.  The NWK sends a ~OUTPUT, but
# that's entirely indistinguishable from an *unsolicited* message.
#
# The best solution found so far is to avoid ever sending #OUTPUT
# or ?OUTPUT, which, conveniently, is never necessary on QS
# standalone, as #DEVICE and ?DEVICE handle all cases and more.
_REPLY_PREFIXES = [
    b"~DETAILS",
    b"~ERROR",
    b"~INTEGRATIONID",
    b"~PROGRAMMING",
    b"~ETHERNET",
    b"~MONITORING",
]


class LoginError(Exception):
    """Exception raised when login fails."""

    message: bytes

    def __init__(self, message: bytes) -> None:
        self.message = message
        super().__init__(message.decode("utf-8", errors="replace"))


class ProtocolError(Exception):
    """Exception raised when the protocol doesn't parse correctly."""

    def __init__(self, message: str) -> None:
        super().__init__(str)


class DisconnectedError(Exception):
    """Exception raised when we aren't connected."""

    def __init__(self) -> None:
        super().__init__("Disconnected")


@dataclass
class _Conn:
    r: asyncio.StreamReader
    w: asyncio.StreamWriter


@dataclass
class _CurrentQuery:
    reply: None | bytes
    unsolicited_messages: None | list[bytes]


# Monitoring messages may be arbitrarily interspersed with actual replies, and
# there is no mechanism in the protocol to tell which messages are part of a reply vs.
# which are asynchronously received monitoring messages.
#
# On the bright side, once we enable prompts, we at least know that all direct
# replies to queries (critically, ?DETAILS) will be received before the QSE>
# prompt.  However, we do not know *which* QSE> prompt they preceed because
# unsolicited messages end with '\r\nQSE>'.  Thanks, Lutron.
#
# We manage to parse the protocol by observing that the incoming stream is a
# stream of messages where each message either ends with b'\r\nQSE>' or
# is just b'QSE>' (no newline).  We further observe that no actual logical
# line of the protocol can start with a Q (everything starts with ~), so
# we can't get confused by a stray Q at the start of a line.
#
# This does not handle the #PASSWD flow.


class LutronConnection:
    """Represents an established Lutron connection."""

    __lock: asyncio.Lock
    __cond: asyncio.Condition

    __conn: _Conn | None  # TODO: There is no value to ever nulling this out
    __prompt_prefix: bytes

    # Unsolicited messages that we have read are enqueued at the
    # end of __unsolicited_queue and are popped from the front.
    #
    # Protected by __lock
    __unsolicited_queue: collections.deque[bytes]

    # While running a query, this is not None and it contains the
    # collected results
    #
    # Protected by __lock
    __current_query: _CurrentQuery | None

    # Are we currently reading?  The login code does not count.
    #
    # Protected by __lock
    __currently_reading: bool

    # Only used by __read_one_message(), which is never called
    # concurrently with itself.
    __buffered_byte: bytes

    # This lock serves (solely) to prevent raw_query() from being called
    # concurrently with itself.
    __query_lock: asyncio.Lock

    # TODO: We should possibly track when we are in a bad state and quickly fail future operations

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.__conn = _Conn(reader, writer)
        self.__lock = asyncio.Lock()
        self.__cond = asyncio.Condition(self.__lock)
        self.__unsolicited_queue = collections.deque()
        self.__buffered_byte = b""
        self.__query_lock = asyncio.Lock()

    @classmethod
    async def create_from_connnection(
        cls, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> "LutronConnection":
        self = LutronConnection(reader, writer)
        assert self.__conn is not None

        # When we first connect, the MONITORING state is uknown, which is rather annoying.
        # To function sensibly, we need:
        #
        # Diagnostic Monitoring (1): otherwise errors will be ignored and we won't find out about them
        # Reply State (11): Queries will never be answered if this is off
        # Prompt State (12): The prompt is how we tell that the system has finished processing a request
        #
        # And, awkwardly, until we set these, we might be in a state where the system
        # is entirely silent, and we don't really know what replies to expect.

        # Send the commands to enable the above monitoring modes
        self.__conn.w.write(
            b"".join(b"#MONITORING,%d,1\r\n" % mode for mode in (1, 11, 12))
        )

        # In response, we really have no idea what to expect, except that there really ought
        # be at least one prompt.  So we'll do an outrageous cheat and send a command with
        # a known reply that we wouldn't otherwise see so we can wait for it.
        self.__conn.w.write(b"?MONITORING,2\r\n")

        await self.__conn.r.readuntil(b"~MONITORING,2,")
        data = await self.__conn.r.readuntil(b">")

        m = re.fullmatch(b"\\d\r\n([A-Za-z0-9]+)>", data)
        if not m:
            raise ProtocolError(
                f"Could not parse {(b'~MONITORING,2,' + data + b'>')!r} as a monitoring ping reply"
            )
        self.__prompt_prefix = m[1]

        self.__currently_reading = False
        self.__current_query = None

        return self

    # This returns the protocol name as inferred from the prompt.
    # For example, QS Standalong is b'QSE'.
    @property
    def protocol_name(self) -> bytes:
        return self.__prompt_prefix

    # This is the meat of the reader.  This function is the only thing that reads from
    # the underlying StreamReader, and it is never called concurrently.
    #
    # We don't use cancellation ourselves, but we want to recover cleanly from
    # a client cancelling a call, which means that we can never await something
    # that might result in a cancellation while we are storing data that
    # we've read in a local variable.
    async def __read_one_message(self) -> bytes:
        assert self.__currently_reading
        # This needs to be cancelable and then runnable again without losing data
        assert self.__conn is not None
        if not self.__buffered_byte:
            self.__buffered_byte = await self.__conn.r.read(1)

            if not self.__buffered_byte:
                # We got EOF.
                raise DisconnectedError()

        if self.__buffered_byte == self.__prompt_prefix[0:1]:
            # We got Q and expect SE>
            expected = self.__prompt_prefix[1:] + b">"
            data = await self.__conn.r.readexactly(len(expected))
            if data != expected:
                raise ProtocolError(f"Expected {expected!r} but received {data!r}")
            self.__buffered_byte = b""
            return b""
        else:
            # We got the first byte of a message and expect the rest of it
            # followed by b'\r\nQSE>'
            data = await self.__conn.r.readuntil(b"\r\n" + self.__prompt_prefix + b">")
            result = (
                self.__buffered_byte + data[: -(len(self.__prompt_prefix) + 1)]
            )  # strip the QSE>
            self.__buffered_byte = b""
            return result

    def __is_message_a_reply(self, message: bytes) -> bool:
        # If it's blank (i.e. they send b'QSE>'), then it's a reply.
        if not message:
            return True

        # Is it in our list of reply prefixes?
        upper = message.upper()
        for prefix in _REPLY_PREFIXES:
            if upper.startswith(prefix):
                return True

        # Otherwise it's not a reply.  (Note that messages like ~DEVICE
        # may well be sent as a result of a query, but they are not sent
        # as a reply to the query -- they're sent as though they're
        # unsolicited.)

        # Sanity check: we expect exactly one b'\r\n', and it will be at the
        # end.
        assert message.endswith(b"\r\n")
        assert b"\r\n" not in message[:-2], (
            f"Unsolicited message {message!r} has too many lines"
        )
        return False

    # Reads one message and stores the result in the appropriate member variables(s)
    #
    # Caller must hold self.__cond
    async def __read_and_dispatch(self):
        # TODO: I'm not thrilled with this unlock-and-relock sequence.
        # Getting rid of it would require refactoring the callers.
        self.__cond.release()
        try:
            data = await self.__read_one_message()
        finally:
            await self.__cond.acquire()

        if not self.__is_message_a_reply(data):
            self.__unsolicited_queue.append(data)

            if (
                self.__current_query
                and self.__current_query.unsolicited_messages is not None
            ):
                self.__current_query.unsolicited_messages.append(data)
                _LOGGER.debug("Received semi-solicited message: %s", repr(data))
            else:
                _LOGGER.debug("Received unsolicited message: %s", repr(data))

            self.__cond.notify_all()
        else:
            if self.__current_query is None:
                _LOGGER.error("Received unexpected syncronous message %s", repr(data))
                return  # No need to notify_all()

            if self.__current_query.reply is not None:
                _LOGGER.error(
                    "Received syncronous message %s before handling prior sync message %s",
                    (repr(data), repr(self.__current_query.reply)),
                )
                return  # No need to notify_all()

            _LOGGER.debug("Received synchronous message: %s", repr(data))
            self.__current_query.reply = data
            self.__cond.notify_all()

    # Reads until predicate returns true.  May be called concurrently.
    # Needs to tolerate cancellation.
    #
    # Caller must hold self.__cond
    async def __wait_for_data(self, predicate: Callable[[], bool]):
        assert self.__cond.locked()

        while True:
            if predicate():
                return

            if self.__currently_reading:
                await self.__cond.wait()
                continue

            try:
                self.__currently_reading = True
                await self.__read_and_dispatch()
            finally:
                self.__currently_reading = False

    async def __raw_query(
        self, command: bytes, unsolicited_out: None | list[bytes] = None
    ) -> bytes:
        assert self.__conn is not None

        async with self.__query_lock:
            async with self.__cond:
                if self.__current_query:
                    raise ProtocolError(
                        "raw_query called while a query in in progress (did you cancel and try again)"
                    )
                self.__current_query = _CurrentQuery(
                    reply=None, unsolicited_messages=unsolicited_out
                )

            assert b"\r\n" not in command

            self.__conn.w.write(command + b"\r\n")
            await self.__conn.w.drain()

            async with self.__cond:
                await self.__wait_for_data(
                    lambda: self.__current_query is not None
                    and self.__current_query.reply is not None
                )

                assert self.__current_query is not None
                assert self.__current_query.reply is not None
                reply = self.__current_query.reply
                self.__current_query = None
                return reply

    # Issues a command and returns the synchronous reply.
    async def raw_query(self, command: bytes) -> bytes:
        return await self.__raw_query(command, None)

    # Issues a command and returns the synchronous reply and
    # a copy of all unsolicited messages received starting just before
    # sending the command and receiving the synchronous reply.
    # This is inherently racy and may return earlier unsolicited messages
    # as well.  It will not prevent read_unsolicited() from receiving the
    # same messages.
    #
    # This is useful for probing devices or outputs without interfering
    # with whatever other code might be using read_unsolicited()
    async def raw_query_collect(self, command: bytes) -> tuple[bytes, list[bytes]]:
        unsolicited_out: list[bytes] = []
        reply = await self.__raw_query(command, unsolicited_out)
        return (reply, unsolicited_out)

    # Helper to ping the other end
    async def ping(self) -> None:
        await self.raw_query(b"")

    async def send_device_command(
        self,
        dev: types.SerialNumber | bytes,
        component: int,
        action: types.DeviceAction,
        params: Iterable[bytes],
    ) -> None:
        if isinstance(dev, types.SerialNumber):
            sn = dev.sn
        else:
            sn = dev
        command = b"#DEVICE,%s,%d,%d,%s" % (
            sn,
            component,
            action.value,
            b",".join(params),
        )
        await self.raw_query(command)

    # Reads one single unsolicited message
    async def read_unsolicited(self) -> bytes:
        async with self.__cond:
            await self.__wait_for_data(lambda: len(self.__unsolicited_queue) >= 1)

            result = self.__unsolicited_queue.popleft()
            return result

    # Higher-level queries

    # Gets the most recent for all components that respond to probes.
    async def probe_device(
        self, dev_id: types.SerialNumber | bytes
    ) -> tuple[bytes, list[bytes]]:
        if isinstance(dev_id, bytes):
            target = dev_id
        else:
            assert isinstance(dev_id, types.SerialNumber)
            target = dev_id.sn
        return await self.raw_query_collect(b"?DEVICE,%s,0,0" % target)

    async def disconnect(self) -> None:
        if self.__conn is None:
            return None
        self.__conn.w.close()
        await self.__conn.w.wait_closed()
        self._conn = None
        return None


async def login(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    username: bytes,
    password: None | bytes,
) -> LutronConnection:
    """
    Authenticate with a Lutron device over an asyncio stream.

    Waits for the login prompt, sends the username, and validates the response.

    Args:
        reader: The asyncio StreamReader for receiving data
        writer: The asyncio StreamWriter for sending data
        username: The username as bytes
        password: The password as bytes, or None if no password is required

    Returns:
        LutronConnection object representing the established connection

    Raises:
        LoginError: If the login fails, containing the error message from the server
    """
    # Wait for the login prompt
    await reader.readuntil(b"login: ")

    # Send the username (line-oriented protocol requires newline)
    writer.write(username + b"\r\n")
    await writer.drain()

    # Read the server's response
    response = await reader.readline()
    response = response.strip()

    # Check if login was successful
    if response == b"connection established":
        return await LutronConnection.create_from_connnection(reader, writer)
    else:
        raise LoginError(response)


async def dump_replies(conn: LutronConnection):
    try:
        async with asyncio.timeout(0.25):
            while True:
                # It would be nice to just do this until it would block, but StreamReader doesn't
                # have a nonblocking read operation
                print(repr(await conn.read_unsolicited()))
    except TimeoutError:
        pass
