"""Read the bounded chat_room.ext_buffer member list; never infer identity from names."""
from __future__ import annotations


def _fields(data):
    position = 0

    def varint():
        nonlocal position
        value = 0
        for shift in range(0, 70, 7):
            if position >= len(data):
                raise ValueError
            byte = data[position]
            position += 1
            value |= (byte & 127) << shift
            if not byte & 128:
                return value
        raise ValueError

    result = []
    while position < len(data):
        if len(result) >= 12000:
            raise ValueError
        tag = varint()
        number, wire = tag >> 3, tag & 7
        if not 1 <= number < 2**29:
            raise ValueError
        value = None
        if wire == 0:
            varint()
        elif wire in (1, 5):
            position += 8 if wire == 1 else 4
        elif wire == 2:
            size = varint()
            value = bytes(data[position:position + size])
            position += size
        else:
            raise ValueError
        if position > len(data):
            raise ValueError
        result.append((number, wire, value))
    return result


def parse_member_aliases(data):
    """Linux 4.1 member field 1 -> username 1 / group display name 2.

    Missing display names stay empty. Contact remarks and account aliases are
    different fields and are deliberately not substituted for group aliases.
    Unknown or inconsistent encoding produces no updates to existing aliases.
    """
    if not isinstance(data, (bytes, memoryview)) or len(data) > 2 * 1024 * 1024:
        return {}
    result = {}
    try:
        for number, wire, member in _fields(data):
            if number != 1 or wire != 2:
                continue
            values = _fields(member)
            identifiers = [v.decode('utf-8') for n, w, v in values if n == 1 and w == 2]
            aliases = [v.decode('utf-8') for n, w, v in values if n == 2 and w == 2]
            if len(identifiers) != 1 or len(aliases) > 1:
                return {}
            uid, alias = identifiers[0], aliases[0] if aliases else ''
            if (not uid or len(uid) > 256 or uid.endswith('@chatroom') or len(alias) > 256
                    or any(ord(c) < 32 for c in uid + alias) or uid in result):
                return {}
            result[uid] = alias
            if len(result) > 2000:
                return {}
    except (ValueError, UnicodeError):
        return {}
    return result
