"""Resolve WeChat's member-derived display title for unnamed group chats."""
import sqlite3


def fields(data):
    def varint(pos):
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(data):
                raise ValueError('Truncated room metadata')
            byte = data[pos]
            pos += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value, pos
        raise ValueError('Invalid room metadata')
    pos = 0
    while pos < len(data):
        tag, pos = varint(pos)
        kind = tag & 7
        if kind == 0:
            value, pos = varint(pos)
        elif kind in (1, 2, 5):
            if kind == 2:
                size, pos = varint(pos)
            else:
                size = 8 if kind == 1 else 4
            if pos + size > len(data):
                raise ValueError('Truncated room metadata field')
            value = data[pos:pos + size]
            pos += size
        else:
            raise ValueError('Unsupported room metadata field')
        yield tag >> 3, value


def display_name(connection, group_id, bot_id, nickname):
    if nickname and nickname.strip():
        return nickname
    try:
        row = connection.execute('SELECT ext_buffer FROM chat_room WHERE username=?', (group_id,)).fetchone()
        if not row or not row[0] or len(row[0]) > 1024 * 1024:
            return ''
        members = []
        for number, value in fields(row[0]):
            if number != 1 or not isinstance(value, bytes):
                continue
            member = dict(fields(value))
            user = member.get(1, b'').decode('utf-8')
            if not user or user == bot_id:
                continue
            contact = connection.execute('SELECT remark,nick_name FROM contact WHERE username=?', (user,)).fetchone()
            alias = member.get(2, b'').decode('utf-8')
            name = (contact[0] if contact else '') or alias or (contact[1] if contact else '')
            if not name:
                return ''
            members.append(name)
            if len(members) == 3:
                break
        return '、'.join(members)
    except (ValueError, UnicodeError, AttributeError, sqlite3.Error):
        return ''


def member_names(connection, group_id):
    """Full room roster for unambiguous name references; never infer from a partial tail."""
    try:
        row = connection.execute('SELECT ext_buffer FROM chat_room WHERE username=?', (group_id,)).fetchone()
        if not row or not row[0] or len(row[0]) > 1024 * 1024:
            return {}
        result = {}
        for number, value in fields(row[0]):
            if number != 1 or not isinstance(value, bytes):
                continue
            member = dict(fields(value))
            user = member.get(1, b'').decode('utf-8')
            if not user:
                continue
            contact = connection.execute('SELECT remark,nick_name FROM contact WHERE username=?', (user,)).fetchone()
            names = [member.get(2, b'').decode('utf-8')] + (list(contact) if contact else [])
            result[user] = list(dict.fromkeys(n for n in names if isinstance(n, str) and n.strip()))
            if len(result) > 2000:
                return {}
        return result
    except (ValueError, UnicodeError, AttributeError, sqlite3.Error):
        return {}
