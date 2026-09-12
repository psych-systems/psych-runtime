"""What the remote adapter accepts from a service that is not on its side.

The response stream is hostile input. It arrives as bytes over a socket the
service controls, so every property the adapter needs -- one frame at a time,
a bounded number of them, bounded in size, decodable -- has to be established
here rather than assumed. These cases are the ways a stream can be expensive
without being large, and the ways a legitimate one is shaped.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable

import pytest

from psych_runtime.sandbox import remote
from psych_runtime.sandbox.protocol import SandboxProtocolError
from psych_runtime.sandbox.remote import (
    _LINES_PER_YIELD,
    _MAX_BLANK_LINES,
    _MAX_FRAME_BYTES,
    _MAX_FRAMES,
    _MAX_STREAM_BYTES,
    _iter_frame_lines,
)


class _Response:
    """A service's answer, delivered in exactly the chunks it chose."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = list(chunks)

    @property
    def status_code(self) -> int:
        return 200

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


async def _collect(*chunks: bytes) -> list[str]:
    return [line async for line in _iter_frame_lines(_Response(chunks))]


class TestOrdinaryStreams:
    async def test_frames_split_across_chunks_are_rejoined(self) -> None:
        """A chunk boundary is the network's choice, not the protocol's."""
        lines = await _collect(b'{"ty', b'pe": "ready"}\n{"type"', b': "done"}\n')
        assert lines == ['{"type": "ready"}', '{"type": "done"}']

    async def test_many_frames_in_one_chunk_are_read_in_order(self) -> None:
        lines = await _collect(b'{"n": 1}\n{"n": 2}\n{"n": 3}\n')
        assert lines == ['{"n": 1}', '{"n": 2}', '{"n": 3}']

    async def test_carriage_returns_are_not_part_of_the_frame(self) -> None:
        """A service writing CRLF is writing NDJSON, not a different protocol."""
        lines = await _collect(b'{"n": 1}\r\n{"n": 2}\r\n')
        assert lines == ['{"n": 1}', '{"n": 2}']

    async def test_a_last_frame_without_a_newline_is_still_read(self) -> None:
        lines = await _collect(b'{"n": 1}\n{"n": 2}')
        assert lines == ['{"n": 1}', '{"n": 2}']

    async def test_a_handful_of_blank_lines_is_allowed(self) -> None:
        lines = await _collect(b'\n\n{"n": 1}\n\n')
        assert lines == ['{"n": 1}']

    async def test_a_frame_that_is_not_utf8_ends_the_stream(self) -> None:
        with pytest.raises(SandboxProtocolError, match="UTF-8"):
            await _collect(b"\xff\xfe\n")


class TestHostileStreams:
    """Four ways to be expensive, each refused by name."""

    async def test_a_flood_of_empty_lines_is_refused(self) -> None:
        """The cheapest padding there is: no frame, barely any bytes, all work."""
        flood = b"\n" * (_MAX_BLANK_LINES + 10)
        with pytest.raises(SandboxProtocolError, match="blank line ceiling"):
            await _collect(flood)

    async def test_a_flood_of_tiny_valid_frames_is_refused(self) -> None:
        """Well inside the byte ceiling, and still unbounded work."""
        frame = b"{}\n"
        chunk = frame * 10_000
        chunks = [chunk] * ((_MAX_FRAMES // 10_000) + 2)
        with pytest.raises(SandboxProtocolError, match="line ceiling"):
            await _collect(*chunks)

    async def test_a_frame_with_no_newline_in_it_is_refused_by_size(self) -> None:
        """The endless line: bounded while it is still bytes, never assembled."""
        chunk = b"a" * (1024 * 1024)
        chunks = [chunk] * ((_MAX_FRAME_BYTES // len(chunk)) + 2)
        with pytest.raises(SandboxProtocolError, match="byte ceiling"):
            await _collect(*chunks)

    async def test_the_whole_stream_is_bounded_as_well_as_each_frame(self) -> None:
        """Many legal frames are still a budget, and it is spent in bytes."""
        line = b"x" * (1024 * 1024 - 1) + b"\n"
        chunks = [line] * ((_MAX_STREAM_BYTES // len(line)) + 2)
        with pytest.raises(SandboxProtocolError, match="byte ceiling"):
            await _collect(*chunks)

    async def test_the_event_loop_keeps_running_while_a_flood_is_parsed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Parsing is synchronous, so without a yield it blocks the process.

        That is worse than slow: the wall-clock timeout that is supposed to
        end a hostile stream is itself a task on this loop, and a reader that
        never yields has disabled it. The assertion is on progress rather than
        on elapsed time -- another task ran -- because a clock reading on a
        loaded machine measures the machine.
        """
        flood = b"{}\n" * 60_000

        async def ticks_while_reading() -> int:
            ticks = 0

            async def tick() -> None:
                nonlocal ticks
                while True:
                    ticks += 1
                    await asyncio.sleep(0)

            ticker = asyncio.create_task(tick())
            await asyncio.sleep(0)
            try:
                assert await _collect(flood) != []
            finally:
                ticker.cancel()
            return ticks

        assert await ticks_while_reading() > 50, "the reader held the loop for the whole flood"

        # The control: with the yield pushed out of reach, the same flood runs
        # to the end without letting anything else move. Without this the
        # assertion above could pass on a reader that never yielded at all.
        monkeypatch.setattr(remote, "_LINES_PER_YIELD", 10**9)
        assert await ticks_while_reading() <= 2

    async def test_small_chunks_do_not_buy_an_unyielding_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The work budget is the response's, not the chunk's.

        A service picks its own chunk boundaries. If the count towards the
        next yield restarts at each one, sending a couple of hundred chunks of a few
        hundred frames each buys exactly what one enormous chunk was refused:
        the whole response parsed without letting anything else on the loop
        move. Every chunk here is deliberately under the yield interval.
        """
        per_chunk = _LINES_PER_YIELD - 12
        chunks = [b'{"n": 1}\n' * per_chunk] * 190
        assert per_chunk < _LINES_PER_YIELD
        assert per_chunk * len(chunks) < _MAX_FRAMES
        assert per_chunk * len(chunks) > _MAX_FRAMES * 0.9

        async def ticks_while_reading() -> int:
            ticks = 0

            async def tick() -> None:
                nonlocal ticks
                while True:
                    ticks += 1
                    await asyncio.sleep(0)

            ticker = asyncio.create_task(tick())
            await asyncio.sleep(0)
            try:
                assert len(await _collect(*chunks)) == per_chunk * len(chunks)
            finally:
                ticker.cancel()
            return ticks

        assert await ticks_while_reading() > 100, (
            "chunk boundaries reset the yield counter, so the read never gave the loop back"
        )

        # The control, as above: with yielding disabled the same chunking runs
        # straight through, which is what this test exists to catch.
        monkeypatch.setattr(remote, "_LINES_PER_YIELD", 10**9)
        assert await ticks_while_reading() <= 2
