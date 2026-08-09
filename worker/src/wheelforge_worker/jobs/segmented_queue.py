from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4


class QueueCapacityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class QueueLimits:
    segment_size: int
    max_segments: int
    max_records: int
    max_record_bytes: int
    record_max_bytes: int
    state_max_bytes: int
    reconcile_max_mutations: int

    @property
    def capacity(self) -> int:
        return self.segment_size * self.max_segments


@dataclass(frozen=True, slots=True)
class QueueIO:
    get_xattr: Callable[[int, bytes, int], bytes | None]
    set_xattr: Callable[[int, bytes, bytes], None]
    put_xattr: Callable[[int, bytes, bytes], None]
    fsync: Callable[[int], None]


@dataclass(frozen=True, slots=True)
class QueueItem:
    lane: str
    segment: int
    slot: int
    record: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _ObservedItem:
    lane: str
    segment: int
    slot: int
    record: dict[str, Any]
    size: int


def initial_state() -> dict[str, Any]:
    return {
        "version": 3,
        "nextSequence": 0,
        "capacityEvents": 0,
        "lanes": {
            lane: {
                "headCursor": 0,
                "writeCursor": 0,
                "count": 0,
                "bytes": 0,
                "segments": 0,
            }
            for lane in ("ready", "deferred", "held")
        },
    }


def ensure(
    root_descriptor: int,
    name: str,
    *,
    io: QueueIO,
    limits: QueueLimits,
    state_xattr: bytes,
    write_state: Callable[[int, dict[str, Any]], None],
) -> None:
    try:
        os.mkdir(name, 0o700, dir_fd=root_descriptor)
    except FileExistsError:
        pass
    queue = open_queue(root_descriptor, name)
    try:
        for lane in ("ready", "deferred", "held"):
            try:
                os.mkdir(lane, 0o700, dir_fd=queue)
            except FileExistsError:
                pass
        payload = io.get_xattr(queue, state_xattr, limits.state_max_bytes)
        if payload is None:
            io.set_xattr(
                queue,
                state_xattr,
                encode_state(initial_state(), limits.state_max_bytes),
            )
        reconcile(
            queue,
            io=io,
            limits=limits,
            state_xattr=state_xattr,
            write_state=write_state,
        )
        io.fsync(queue)
        io.fsync(root_descriptor)
    finally:
        os.close(queue)


def encode_state(state: dict[str, Any], max_bytes: int) -> bytes:
    payload = json.dumps(
        state, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if len(payload) > max_bytes:
        raise RuntimeError("maintenance queue state exceeds its byte limit")
    return payload


def read_state(
    queue_descriptor: int,
    *,
    io: QueueIO,
    limits: QueueLimits,
    state_xattr: bytes,
) -> dict[str, Any]:
    payload = io.get_xattr(
        queue_descriptor, state_xattr, limits.state_max_bytes
    )
    if payload is None:
        raise RuntimeError("maintenance queue state is unavailable")
    try:
        state = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("maintenance queue state is invalid") from error
    if (
        not isinstance(state, dict)
        or state.get("version") != 3
        or not isinstance(state.get("nextSequence"), int)
        or state["nextSequence"] < 0
        or not isinstance(state.get("capacityEvents"), int)
        or state["capacityEvents"] < 0
    ):
        raise RuntimeError("maintenance queue state is invalid")
    lanes = state.get("lanes")
    if not isinstance(lanes, dict) or set(lanes) != {"ready", "deferred", "held"}:
        raise RuntimeError("maintenance queue state is invalid")
    for lane in lanes.values():
        if not isinstance(lane, dict) or set(lane) != {
            "headCursor",
            "writeCursor",
            "count",
            "bytes",
            "segments",
        }:
            raise RuntimeError("maintenance queue state is invalid")
        if not all(isinstance(value, int) and value >= 0 for value in lane.values()):
            raise RuntimeError("maintenance queue state is invalid")
        if (
            lane["headCursor"] >= limits.capacity
            or lane["writeCursor"] >= limits.capacity
            or lane["count"] > limits.max_records
            or lane["bytes"] > limits.max_record_bytes
            or lane["segments"] > limits.max_segments
        ):
            raise RuntimeError("maintenance queue state is invalid")
    return state


def write_state(
    queue_descriptor: int,
    state: dict[str, Any],
    *,
    io: QueueIO,
    state_xattr: bytes,
    state_max_bytes: int,
) -> None:
    io.put_xattr(
        queue_descriptor,
        state_xattr,
        encode_state(state, state_max_bytes),
    )
    io.fsync(queue_descriptor)


def open_queue(root_descriptor: int, name: str) -> int:
    return os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=root_descriptor,
    )


def open_lane(queue_descriptor: int, lane: str) -> int:
    if lane not in {"ready", "deferred", "held"}:
        raise ValueError("invalid maintenance queue lane")
    return os.open(
        lane,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=queue_descriptor,
    )


def enqueue(
    root_descriptor: int,
    queue_name: str,
    lane_name: str,
    record: dict[str, Any],
    *,
    io: QueueIO,
    limits: QueueLimits,
    state_xattr: bytes,
    write_state_callback: Callable[[int, dict[str, Any]], None],
    deduplicate: bool = True,
    deduplicate_generation: bool = False,
) -> QueueItem:
    queue = open_queue(root_descriptor, queue_name)
    lane = -1
    segment = -1
    try:
        state = read_state(
            queue, io=io, limits=limits, state_xattr=state_xattr
        )
        prepared = _prepare_record(record, state["nextSequence"])
        if deduplicate and "recordId" in record:
            existing = _find_record_id(queue, prepared["recordId"], limits)
            if existing is not None:
                return existing
        elif deduplicate_generation:
            existing = _find_record_id(
                queue,
                prepared["recordId"],
                limits,
                generation=prepared["generation"],
                lane_name=lane_name,
            )
            if existing is not None:
                return existing
        payload = _encode_record(prepared, limits.record_max_bytes)
        total_count = sum(state["lanes"][name]["count"] for name in state["lanes"])
        total_bytes = sum(state["lanes"][name]["bytes"] for name in state["lanes"])
        if total_count >= limits.max_records or total_bytes + len(payload) > limits.max_record_bytes:
            state["capacityEvents"] += 1
            write_state_callback(queue, state)
            raise QueueCapacityError("maintenance queue capacity is exhausted")
        lane_state = state["lanes"][lane_name]
        lane = open_lane(queue, lane_name)
        cursor = lane_state["writeCursor"]
        for _ in range(limits.capacity):
            segment_number, slot_number = divmod(cursor, limits.segment_size)
            segment_name = f"{segment_number:016x}"
            segment_created = False
            try:
                os.mkdir(segment_name, 0o700, dir_fd=lane)
                segment_created = True
            except FileExistsError:
                pass
            if segment_created:
                io.fsync(lane)
            segment = os.open(
                segment_name,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=lane,
            )
            try:
                descriptor = os.open(
                    f"{slot_number:03d}.json",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=segment,
                )
            except FileExistsError:
                os.close(segment)
                segment = -1
                cursor = (cursor + 1) % limits.capacity
                continue
            try:
                _write_all(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            io.fsync(segment)
            lane_state["count"] += 1
            lane_state["bytes"] += len(payload)
            if lane_state["count"] == 1:
                lane_state["headCursor"] = cursor
            lane_state["writeCursor"] = (cursor + 1) % limits.capacity
            lane_state["segments"] = _count_segments(lane, limits)
            state["nextSequence"] += 1
            write_state_callback(queue, state)
            return QueueItem(lane_name, segment_number, slot_number, prepared)
        state["capacityEvents"] += 1
        write_state_callback(queue, state)
        raise QueueCapacityError("maintenance queue has no free slot")
    finally:
        if segment != -1:
            os.close(segment)
        if lane != -1:
            os.close(lane)
        os.close(queue)


def peek(
    root_descriptor: int,
    queue_name: str,
    lane_name: str,
    *,
    io: QueueIO,
    limits: QueueLimits,
    state_xattr: bytes,
    reconcile_callback: Callable[[int], None],
) -> QueueItem | None:
    queue = open_queue(root_descriptor, queue_name)
    lane = -1
    try:
        state = read_state(queue, io=io, limits=limits, state_xattr=state_xattr)
        lane_state = state["lanes"][lane_name]
        if lane_state["count"] == 0:
            return None
        lane = open_lane(queue, lane_name)
        item = _find_item(lane, lane_name, lane_state["headCursor"], limits)
        if item is not None:
            return item
        reconcile_callback(queue)
        state = read_state(queue, io=io, limits=limits, state_xattr=state_xattr)
        if state["lanes"][lane_name]["count"] == 0:
            return None
        item = _find_item(
            lane, lane_name, state["lanes"][lane_name]["headCursor"], limits
        )
        if item is None:
            raise RuntimeError("maintenance queue reconciliation made no progress")
        return item
    finally:
        if lane != -1:
            os.close(lane)
        os.close(queue)


def peek_due(
    root_descriptor: int,
    queue_name: str,
    lane_name: str,
    now: datetime,
    max_records: int,
    *,
    io: QueueIO,
    limits: QueueLimits,
    state_xattr: bytes,
    is_due: Callable[[dict[str, Any], datetime], bool],
    reconcile_callback: Callable[[int], None],
) -> tuple[QueueItem | None, int]:
    if max_records <= 0:
        return None, 0
    queue = open_queue(root_descriptor, queue_name)
    lane = -1
    try:
        state = read_state(queue, io=io, limits=limits, state_xattr=state_xattr)
        lane_state = state["lanes"][lane_name]
        if lane_state["count"] == 0:
            return None, 0
        lane = open_lane(queue, lane_name)
        inspected = 0
        record_limit = min(max_records, lane_state["count"])
        for offset in range(limits.capacity):
            cursor = (lane_state["headCursor"] + offset) % limits.capacity
            segment_number, slot_number = divmod(cursor, limits.segment_size)
            segment = _try_open_segment(lane, segment_number)
            if segment is None:
                continue
            try:
                current = _read_slot(segment, slot_number, limits.record_max_bytes)
            finally:
                os.close(segment)
            if current is None:
                continue
            inspected += 1
            if is_due(current[0], now):
                return QueueItem(lane_name, segment_number, slot_number, current[0]), inspected
            if inspected >= record_limit:
                return None, inspected
        reconcile_callback(queue)
        return None, inspected
    finally:
        if lane != -1:
            os.close(lane)
        os.close(queue)


def complete(
    root_descriptor: int,
    queue_name: str,
    item: QueueItem,
    *,
    io: QueueIO,
    limits: QueueLimits,
    state_xattr: bytes,
    write_state_callback: Callable[[int, dict[str, Any]], None],
    reconcile_callback: Callable[[int], None],
) -> None:
    queue = open_queue(root_descriptor, queue_name)
    lane = open_lane(queue, item.lane)
    segment = -1
    try:
        state = read_state(queue, io=io, limits=limits, state_xattr=state_xattr)
        lane_state = state["lanes"][item.lane]
        try:
            segment = os.open(
                f"{item.segment:016x}",
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=lane,
            )
        except FileNotFoundError:
            reconcile_callback(queue)
            return
        current = _read_slot(segment, item.slot, limits.record_max_bytes)
        if current is None:
            reconcile_callback(queue)
            return
        if (
            current[0].get("recordId") != item.record.get("recordId")
            or current[0].get("generation") != item.record.get("generation")
        ):
            raise RuntimeError("maintenance queue ownership changed")
        os.unlink(f"{item.slot:03d}.json", dir_fd=segment)
        io.fsync(segment)
        lane_state["count"] = max(0, lane_state["count"] - 1)
        lane_state["bytes"] = max(0, lane_state["bytes"] - current[1])
        lane_state["headCursor"] = (
            item.segment * limits.segment_size + item.slot + 1
        ) % limits.capacity
        write_state_callback(queue, state)
    finally:
        if segment != -1:
            os.close(segment)
        os.close(lane)
        os.close(queue)


def counts(
    root_descriptor: int,
    queue_name: str,
    *,
    io: QueueIO,
    limits: QueueLimits,
    state_xattr: bytes,
) -> dict[str, int]:
    queue = open_queue(root_descriptor, queue_name)
    try:
        state = read_state(queue, io=io, limits=limits, state_xattr=state_xattr)
        return {
            **{lane: state["lanes"][lane]["count"] for lane in state["lanes"]},
            "recordBytes": sum(state["lanes"][lane]["bytes"] for lane in state["lanes"]),
            "segments": sum(state["lanes"][lane]["segments"] for lane in state["lanes"]),
            "capacityEvents": state["capacityEvents"],
        }
    finally:
        os.close(queue)


def reconcile(
    queue_descriptor: int,
    *,
    io: QueueIO,
    limits: QueueLimits,
    state_xattr: bytes,
    write_state: Callable[[int, dict[str, Any]], None],
) -> None:
    previous = read_state(
        queue_descriptor, io=io, limits=limits, state_xattr=state_xattr
    )
    observed: list[_ObservedItem] = []
    lane_descriptors: dict[str, int] = {}
    mutations = 0
    try:
        for lane_name in ("ready", "deferred", "held"):
            lane = open_lane(queue_descriptor, lane_name)
            lane_descriptors[lane_name] = lane
            segments = _segment_names(lane, limits)
            for segment_number, segment_name in segments:
                segment = os.open(
                    segment_name,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=lane,
                )
                try:
                    for slot_number in range(limits.segment_size):
                        slot_value = _read_slot(
                            segment, slot_number, limits.record_max_bytes
                        )
                        if slot_value is None:
                            continue
                        observed.append(
                            _ObservedItem(
                                lane_name,
                                segment_number,
                                slot_number,
                                slot_value[0],
                                slot_value[1],
                            )
                        )
                        if len(observed) > limits.max_records:
                            raise QueueCapacityError("maintenance queue record limit is exhausted")
                finally:
                    os.close(segment)

        winners: dict[str, _ObservedItem] = {}
        lane_priority = {"ready": 0, "deferred": 1, "held": 2}
        for item in observed:
            winner = winners.get(item.record["recordId"])
            rank = (
                item.record["generation"],
                item.record["sequence"],
                lane_priority[item.lane],
            )
            if winner is None or rank > (
                winner.record["generation"],
                winner.record["sequence"],
                lane_priority[winner.lane],
            ):
                winners[item.record["recordId"]] = item

        winner_locations = {
            (item.lane, item.segment, item.slot) for item in winners.values()
        }
        for item in observed:
            if (item.lane, item.segment, item.slot) in winner_locations:
                continue
            segment = os.open(
                f"{item.segment:016x}",
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=lane_descriptors[item.lane],
            )
            try:
                current = _read_slot(segment, item.slot, limits.record_max_bytes)
                if current is None:
                    continue
                if (
                    current[0]["recordId"] != item.record["recordId"]
                    or current[0]["generation"] != item.record["generation"]
                ):
                    raise RuntimeError("maintenance queue slot changed during reconcile")
                os.unlink(f"{item.slot:03d}.json", dir_fd=segment)
                io.fsync(segment)
                mutations += 2
            finally:
                os.close(segment)
            _check_reconcile_budget(mutations, limits)

        rebuilt = initial_state()
        rebuilt["capacityEvents"] = previous["capacityEvents"]
        maximum_sequence = -1
        for lane_name in ("ready", "deferred", "held"):
            lane_items = sorted(
                (item for item in winners.values() if item.lane == lane_name),
                key=lambda item: item.record["sequence"],
            )
            lane_state = rebuilt["lanes"][lane_name]
            lane_state["count"] = len(lane_items)
            lane_state["bytes"] = sum(item.size for item in lane_items)
            if lane_items:
                first, last = lane_items[0], lane_items[-1]
                lane_state["headCursor"] = first.segment * limits.segment_size + first.slot
                lane_state["writeCursor"] = (
                    last.segment * limits.segment_size + last.slot + 1
                ) % limits.capacity
                maximum_sequence = max(
                    maximum_sequence,
                    max(item.record["sequence"] for item in lane_items),
                )
            lane = lane_descriptors[lane_name]
            for _, segment_name in _segment_names(lane, limits):
                segment = os.open(
                    segment_name,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=lane,
                )
                try:
                    has_record = any(
                        _read_slot(segment, slot, limits.record_max_bytes) is not None
                        for slot in range(limits.segment_size)
                    )
                finally:
                    os.close(segment)
                if not has_record:
                    os.rmdir(segment_name, dir_fd=lane)
                    io.fsync(lane)
                    mutations += 2
                    _check_reconcile_budget(mutations, limits)
            lane_state["segments"] = _count_segments(lane, limits)
        rebuilt["nextSequence"] = maximum_sequence + 1
        if sum(rebuilt["lanes"][lane]["bytes"] for lane in rebuilt["lanes"]) > limits.max_record_bytes:
            raise QueueCapacityError("maintenance queue byte limit is exhausted")
        write_state(queue_descriptor, rebuilt)
    finally:
        for descriptor in lane_descriptors.values():
            os.close(descriptor)


def _prepare_record(record: dict[str, Any], sequence: int) -> dict[str, Any]:
    prepared = dict(record)
    try:
        record_id = str(UUID(prepared.get("recordId", str(uuid4()))))
    except (AttributeError, TypeError, ValueError) as error:
        raise RuntimeError("maintenance queue record id is invalid") from error
    generation = prepared.get("generation", 0)
    if not isinstance(generation, int) or generation < 0:
        raise RuntimeError("maintenance queue generation is invalid")
    prepared.update(version=2, recordId=record_id, generation=generation, sequence=sequence)
    return prepared


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("maintenance queue record write made no progress")
        remaining = remaining[written:]


def _encode_record(record: dict[str, Any], max_bytes: int) -> bytes:
    try:
        payload = json.dumps(
            record, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise RuntimeError("maintenance queue record is invalid") from error
    if len(payload) > max_bytes:
        raise RuntimeError("maintenance queue record exceeds its byte limit")
    return payload


def _decode_record(payload: bytes, max_bytes: int) -> dict[str, Any]:
    if len(payload) > max_bytes:
        raise RuntimeError("maintenance queue record exceeds its byte limit")
    try:
        record = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("maintenance queue record is invalid") from error
    if not isinstance(record, dict) or record.get("version") != 2:
        raise RuntimeError("maintenance queue record is invalid")
    try:
        record["recordId"] = str(UUID(record.get("recordId")))
    except (AttributeError, TypeError, ValueError) as error:
        raise RuntimeError("maintenance queue record id is invalid") from error
    if not isinstance(record.get("generation"), int) or record["generation"] < 0:
        raise RuntimeError("maintenance queue record ordering is invalid")
    if not isinstance(record.get("sequence"), int) or record["sequence"] < 0:
        raise RuntimeError("maintenance queue record ordering is invalid")
    return record


def _read_slot(
    segment_descriptor: int, slot: int, max_bytes: int
) -> tuple[dict[str, Any], int] | None:
    try:
        descriptor = os.open(
            f"{slot:03d}.json",
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=segment_descriptor,
        )
    except FileNotFoundError:
        return None
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > max_bytes:
            raise RuntimeError("maintenance queue slot is invalid")
        payload = os.read(descriptor, max_bytes + 1)
        if len(payload) != details.st_size:
            raise RuntimeError("maintenance queue slot changed while reading")
        return _decode_record(payload, max_bytes), len(payload)
    finally:
        os.close(descriptor)


def _segment_names(lane_descriptor: int, limits: QueueLimits) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    with os.scandir(lane_descriptor) as entries:
        for entry in entries:
            if len(result) >= limits.max_segments:
                raise QueueCapacityError("maintenance queue segment limit is exhausted")
            try:
                number = int(entry.name, 16)
            except ValueError as error:
                raise RuntimeError("maintenance queue segment is invalid") from error
            if (
                len(entry.name) != 16
                or number < 0
                or number >= limits.max_segments
                or not entry.is_dir(follow_symlinks=False)
            ):
                raise RuntimeError("maintenance queue segment is invalid")
            result.append((number, entry.name))
    return result


def _count_segments(lane_descriptor: int, limits: QueueLimits) -> int:
    return len(_segment_names(lane_descriptor, limits))


def _try_open_segment(lane_descriptor: int, segment: int) -> int | None:
    try:
        return os.open(
            f"{segment:016x}",
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=lane_descriptor,
        )
    except FileNotFoundError:
        return None


def _find_item(
    lane_descriptor: int,
    lane_name: str,
    start_cursor: int,
    limits: QueueLimits,
) -> QueueItem | None:
    for offset in range(limits.capacity):
        cursor = (start_cursor + offset) % limits.capacity
        segment_number, slot_number = divmod(cursor, limits.segment_size)
        segment = _try_open_segment(lane_descriptor, segment_number)
        if segment is None:
            continue
        try:
            current = _read_slot(segment, slot_number, limits.record_max_bytes)
            if current is not None:
                return QueueItem(lane_name, segment_number, slot_number, current[0])
        finally:
            os.close(segment)
    return None


def _find_record_id(
    queue_descriptor: int,
    record_id: str,
    limits: QueueLimits,
    *,
    generation: int | None = None,
    lane_name: str | None = None,
) -> QueueItem | None:
    lanes = (lane_name,) if lane_name is not None else ("ready", "deferred", "held")
    for current_lane in lanes:
        lane = open_lane(queue_descriptor, current_lane)
        try:
            for cursor in range(limits.capacity):
                segment_number, slot_number = divmod(cursor, limits.segment_size)
                segment = _try_open_segment(lane, segment_number)
                if segment is None:
                    continue
                try:
                    current = _read_slot(segment, slot_number, limits.record_max_bytes)
                finally:
                    os.close(segment)
                if (
                    current is not None
                    and current[0]["recordId"] == record_id
                    and (
                        generation is None
                        or current[0]["generation"] == generation
                    )
                ):
                    return QueueItem(
                        current_lane,
                        segment_number,
                        slot_number,
                        current[0],
                    )
        finally:
            os.close(lane)
    return None


def _check_reconcile_budget(mutations: int, limits: QueueLimits) -> None:
    if mutations > limits.reconcile_max_mutations:
        raise RuntimeError("maintenance queue reconcile budget is exhausted")
