"""
Pure Python Protobuf wire-format encoder/decoder for Google Drive Desktop IPC.
Implements apps/drive/fs/proto/mcp_ipc.proto wire framing and serialization.
No external dependencies required.
"""

import struct

def encode_varint(value: int) -> bytes:
    """Encode an integer as a protobuf varint."""
    if value < 0:
        value += (1 << 64)
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)

def decode_varint(buf: bytes, offset: int = 0):
    """Decode a varint from buffer at offset. Returns (value, new_offset)."""
    res = 0
    shift = 0
    while offset < len(buf):
        b = buf[offset]
        offset += 1
        res |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return res, offset

def encode_tag(field_num: int, wire_type: int) -> bytes:
    return encode_varint((field_num << 3) | wire_type)

def encode_string(field_num: int, s: str) -> bytes:
    if not s:
        return b""
    s_bytes = s.encode('utf-8')
    return encode_tag(field_num, 2) + encode_varint(len(s_bytes)) + s_bytes

def encode_int64(field_num: int, val: int) -> bytes:
    return encode_tag(field_num, 0) + encode_varint(val)

def encode_bool(field_num: int, val: bool) -> bytes:
    return encode_tag(field_num, 0) + encode_varint(1 if val else 0)

def encode_submessage(field_num: int, body: bytes) -> bytes:
    return encode_tag(field_num, 2) + encode_varint(len(body)) + body

def parse_protobuf_fields(buf: bytes):
    """Parses raw protobuf fields into a list of (field_num, wire_type, value)."""
    offset = 0
    fields = []
    while offset < len(buf):
        tag_varint, offset = decode_varint(buf, offset)
        field_num = tag_varint >> 3
        wire_type = tag_varint & 7
        if wire_type == 0: # varint
            val, offset = decode_varint(buf, offset)
            fields.append((field_num, wire_type, val))
        elif wire_type == 2: # length-delimited
            length, offset = decode_varint(buf, offset)
            val = buf[offset:offset+length]
            offset += length
            fields.append((field_num, wire_type, val))
        elif wire_type == 1: # 64-bit
            val = buf[offset:offset+8]
            offset += 8
            fields.append((field_num, wire_type, val))
        elif wire_type == 5: # 32-bit
            val = buf[offset:offset+4]
            offset += 4
            fields.append((field_num, wire_type, val))
        else:
            break
    return fields


# --- McpIpcRequest Encoding & Decoding ---

class McpIpcRequest:
    def __init__(self, request_id: int = 0, **kwargs):
        self.request_id = request_id
        self.get_sync_status = kwargs.get('get_sync_status')       # dict(path=...)
        self.add_folder_to_sync = kwargs.get('add_folder_to_sync')    # dict(path=..., account_id=...)
        self.get_drive_url = kwargs.get('get_drive_url')         # dict(path=...)
        self.list_tracked_folders = kwargs.get('list_tracked_folders')  # True if present
        self.fix_acl = kwargs.get('fix_acl')               # dict(email=..., item_id=..., account_id=...)
        self.get_streaming_mount_points = kwargs.get('get_streaming_mount_points') # dict(account_id=...)
        self.remove_folder_from_sync = kwargs.get('remove_folder_from_sync') # dict(path=..., account_id=...)
        self.set_sync_mode = kwargs.get('set_sync_mode') # dict(sync_mode=..., account_id=...)
        self.trigger_sync = kwargs.get('trigger_sync') # dict(account_id=...)

    def serialize(self) -> bytes:
        payload = bytearray()
        if self.request_id:
            payload.extend(encode_int64(1, self.request_id))
        
        if self.get_sync_status:
            sub = encode_string(1, self.get_sync_status.get('path', ''))
            payload.extend(encode_submessage(2, sub))
        elif self.add_folder_to_sync:
            sub = (encode_string(1, self.add_folder_to_sync.get('path', '')) +
                   encode_string(2, self.add_folder_to_sync.get('account_id', '')))
            payload.extend(encode_submessage(3, sub))
        elif self.get_drive_url:
            sub = encode_string(1, self.get_drive_url.get('path', ''))
            payload.extend(encode_submessage(4, sub))
        elif self.list_tracked_folders is not None:
            payload.extend(encode_submessage(5, b""))
        elif self.fix_acl:
            sub = (encode_string(1, self.fix_acl.get('email', '')) +
                   encode_string(2, self.fix_acl.get('item_id', '')) +
                   encode_string(3, self.fix_acl.get('account_id', '')))
            payload.extend(encode_submessage(6, sub))
        elif self.get_streaming_mount_points is not None:
            sub = encode_string(1, self.get_streaming_mount_points.get('account_id', ''))
            payload.extend(encode_submessage(7, sub))
        elif self.remove_folder_from_sync:
            sub = (encode_string(1, self.remove_folder_from_sync.get('path', '')) +
                   encode_string(2, self.remove_folder_from_sync.get('account_id', '')))
            payload.extend(encode_submessage(8, sub))
        elif self.trigger_sync is not None:
            path = self.trigger_sync.get('path', '') if isinstance(self.trigger_sync, dict) else ''
            account_id = self.trigger_sync.get('account_id', '') if isinstance(self.trigger_sync, dict) else ''
            sub = (encode_string(1, path) +
                   encode_string(2, account_id))
            payload.extend(encode_submessage(9, sub))
        elif self.set_sync_mode:
            sub = (encode_string(1, self.set_sync_mode.get('sync_mode', '')) +
                   encode_string(2, self.set_sync_mode.get('account_id', '')))
            payload.extend(encode_submessage(10, sub))
            
        return bytes(payload)

    @classmethod
    def deserialize(cls, buf: bytes) -> 'McpIpcRequest':
        req = cls()
        fields = parse_protobuf_fields(buf)
        for num, wire, val in fields:
            if num == 1 and wire == 0:
                req.request_id = val
            elif num == 2 and wire == 2:
                subfields = parse_protobuf_fields(val)
                req.get_sync_status = {'path': ''}
                for snum, swire, sval in subfields:
                    if snum == 1: req.get_sync_status['path'] = sval.decode('utf-8', errors='replace')
            elif num == 3 and wire == 2:
                subfields = parse_protobuf_fields(val)
                req.add_folder_to_sync = {'path': '', 'account_id': ''}
                for snum, swire, sval in subfields:
                    if snum == 1: req.add_folder_to_sync['path'] = sval.decode('utf-8', errors='replace')
                    elif snum == 2: req.add_folder_to_sync['account_id'] = sval.decode('utf-8', errors='replace')
            elif num == 4 and wire == 2:
                subfields = parse_protobuf_fields(val)
                req.get_drive_url = {'path': ''}
                for snum, swire, sval in subfields:
                    if snum == 1: req.get_drive_url['path'] = sval.decode('utf-8', errors='replace')
            elif num == 5 and wire == 2:
                req.list_tracked_folders = True
            elif num == 6 and wire == 2:
                subfields = parse_protobuf_fields(val)
                req.fix_acl = {'email': '', 'item_id': '', 'account_id': ''}
                for snum, swire, sval in subfields:
                    if snum == 1: req.fix_acl['email'] = sval.decode('utf-8', errors='replace')
                    elif snum == 2: req.fix_acl['item_id'] = sval.decode('utf-8', errors='replace')
                    elif snum == 3: req.fix_acl['account_id'] = sval.decode('utf-8', errors='replace')
            elif num == 7 and wire == 2:
                subfields = parse_protobuf_fields(val)
                req.get_streaming_mount_points = {'account_id': ''}
                for snum, swire, sval in subfields:
                    if snum == 1: req.get_streaming_mount_points['account_id'] = sval.decode('utf-8', errors='replace')
            elif num == 8 and wire == 2:
                subfields = parse_protobuf_fields(val)
                req.remove_folder_from_sync = {'path': '', 'account_id': ''}
                for snum, swire, sval in subfields:
                    if snum == 1: req.remove_folder_from_sync['path'] = sval.decode('utf-8', errors='replace')
                    elif snum == 2: req.remove_folder_from_sync['account_id'] = sval.decode('utf-8', errors='replace')
            elif num == 9 and wire == 2:
                subfields = parse_protobuf_fields(val)
                req.trigger_sync = {'path': '', 'account_id': ''}
                for snum, swire, sval in subfields:
                    if snum == 1: req.trigger_sync['path'] = sval.decode('utf-8', errors='replace')
                    elif snum == 2: req.trigger_sync['account_id'] = sval.decode('utf-8', errors='replace')
            elif num == 10 and wire == 2:
                subfields = parse_protobuf_fields(val)
                req.set_sync_mode = {'sync_mode': '', 'account_id': ''}
                for snum, swire, sval in subfields:
                    if snum == 1: req.set_sync_mode['sync_mode'] = sval.decode('utf-8', errors='replace')
                    elif snum == 2: req.set_sync_mode['account_id'] = sval.decode('utf-8', errors='replace')
        return req


# --- McpIpcResponse Encoding & Decoding ---

class McpIpcResponse:
    def __init__(self, request_id: int = 0, **kwargs):
        self.request_id = request_id
        self.get_sync_status = kwargs.get('get_sync_status')       # dict(status=..., description=...)
        self.add_folder_to_sync = kwargs.get('add_folder_to_sync')    # dict(success=..., error_message=...)
        self.get_drive_url = kwargs.get('get_drive_url')         # dict(drive_url=..., error_message=...)
        self.list_tracked_folders = kwargs.get('list_tracked_folders')  # dict(paths=[...], error_message=...)
        self.fix_acl = kwargs.get('fix_acl')               # dict(success=..., error_message=...)
        self.get_streaming_mount_points = kwargs.get('get_streaming_mount_points') # dict(mount_points=[...], error_message=...)
        self.remove_folder_from_sync = kwargs.get('remove_folder_from_sync') # dict(success=..., error_message=...)
        self.set_sync_mode = kwargs.get('set_sync_mode') # dict(success=..., error_message=...)
        self.trigger_sync = kwargs.get('trigger_sync') # dict(success=..., error_message=...)

    def serialize(self) -> bytes:
        payload = bytearray()
        if self.request_id:
            payload.extend(encode_int64(1, self.request_id))
            
        if self.get_sync_status:
            sub = (encode_string(1, self.get_sync_status.get('status', '')) +
                   encode_string(2, self.get_sync_status.get('description', '')))
            payload.extend(encode_submessage(2, sub))
        elif self.add_folder_to_sync:
            sub = (encode_bool(1, self.add_folder_to_sync.get('success', False)) +
                   encode_string(2, self.add_folder_to_sync.get('error_message', '')))
            payload.extend(encode_submessage(3, sub))
        elif self.get_drive_url:
            sub = (encode_string(1, self.get_drive_url.get('drive_url', '')) +
                   encode_string(2, self.get_drive_url.get('error_message', '')))
            payload.extend(encode_submessage(4, sub))
        elif self.list_tracked_folders:
            sub = bytearray()
            for p in self.list_tracked_folders.get('paths', []):
                sub.extend(encode_string(1, p))
            if self.list_tracked_folders.get('error_message'):
                sub.extend(encode_string(2, self.list_tracked_folders['error_message']))
            payload.extend(encode_submessage(5, bytes(sub)))
        elif self.fix_acl:
            sub = (encode_bool(1, self.fix_acl.get('success', False)) +
                   encode_string(2, self.fix_acl.get('error_message', '')))
            payload.extend(encode_submessage(6, sub))
        elif self.get_streaming_mount_points:
            sub = bytearray()
            for mp in self.get_streaming_mount_points.get('mount_points', []):
                mpsub = (encode_string(1, mp.get('account_id', '')) +
                         encode_string(2, mp.get('mount_point', '')) +
                         encode_string(3, mp.get('my_drive_path', '')))
                sub.extend(encode_submessage(1, mpsub))
            if self.get_streaming_mount_points.get('error_message'):
                sub.extend(encode_string(2, self.get_streaming_mount_points['error_message']))
            payload.extend(encode_submessage(7, bytes(sub)))
        elif self.remove_folder_from_sync:
            sub = (encode_bool(1, self.remove_folder_from_sync.get('success', False)) +
                   encode_string(2, self.remove_folder_from_sync.get('error_message', '')))
            payload.extend(encode_submessage(8, sub))
        elif self.trigger_sync:
            sub = (encode_bool(1, self.trigger_sync.get('success', False)) +
                   encode_string(2, self.trigger_sync.get('status', 'synced')) +
                   encode_string(3, self.trigger_sync.get('error_message', '')) +
                   encode_int64(4, self.trigger_sync.get('items_synced', 0)))
            payload.extend(encode_submessage(9, sub))
        elif self.set_sync_mode:
            sub = (encode_bool(1, self.set_sync_mode.get('success', False)) +
                   encode_string(2, self.set_sync_mode.get('error_message', '')))
            payload.extend(encode_submessage(10, sub))
            
        return bytes(payload)

    @classmethod
    def deserialize(cls, buf: bytes) -> 'McpIpcResponse':
        resp = cls()
        fields = parse_protobuf_fields(buf)
        for num, wire, val in fields:
            if num == 1 and wire == 0:
                resp.request_id = val
            elif num == 2 and wire == 2:
                sub = parse_protobuf_fields(val)
                resp.get_sync_status = {'status': '', 'description': ''}
                for snum, swire, sval in sub:
                    if snum == 1: resp.get_sync_status['status'] = sval.decode('utf-8', errors='replace')
                    elif snum == 2: resp.get_sync_status['description'] = sval.decode('utf-8', errors='replace')
            elif num == 3 and wire == 2:
                sub = parse_protobuf_fields(val)
                resp.add_folder_to_sync = {'success': False, 'error_message': ''}
                for snum, swire, sval in sub:
                    if snum == 1: resp.add_folder_to_sync['success'] = bool(sval)
                    elif snum == 2: resp.add_folder_to_sync['error_message'] = sval.decode('utf-8', errors='replace')
            elif num == 4 and wire == 2:
                sub = parse_protobuf_fields(val)
                resp.get_drive_url = {'drive_url': '', 'error_message': ''}
                for snum, swire, sval in sub:
                    if snum == 1: resp.get_drive_url['drive_url'] = sval.decode('utf-8', errors='replace')
                    elif snum == 2: resp.get_drive_url['error_message'] = sval.decode('utf-8', errors='replace')
            elif num == 5 and wire == 2:
                sub = parse_protobuf_fields(val)
                resp.list_tracked_folders = {'paths': [], 'error_message': ''}
                for snum, swire, sval in sub:
                    if snum == 1: resp.list_tracked_folders['paths'].append(sval.decode('utf-8', errors='replace'))
                    elif snum == 2: resp.list_tracked_folders['error_message'] = sval.decode('utf-8', errors='replace')
            elif num == 6 and wire == 2:
                sub = parse_protobuf_fields(val)
                resp.fix_acl = {'success': False, 'error_message': ''}
                for snum, swire, sval in sub:
                    if snum == 1: resp.fix_acl['success'] = bool(sval)
                    elif snum == 2: resp.fix_acl['error_message'] = sval.decode('utf-8', errors='replace')
            elif num == 7 and wire == 2:
                sub = parse_protobuf_fields(val)
                resp.get_streaming_mount_points = {'mount_points': [], 'error_message': ''}
                for snum, swire, sval in sub:
                    if snum == 1:
                        mpsub = parse_protobuf_fields(sval)
                        mp = {'account_id': '', 'mount_point': '', 'my_drive_path': ''}
                        for msnum, mswire, msval in mpsub:
                            if msnum == 1: mp['account_id'] = msval.decode('utf-8', errors='replace')
                            elif msnum == 2: mp['mount_point'] = msval.decode('utf-8', errors='replace')
                            elif msnum == 3: mp['my_drive_path'] = msval.decode('utf-8', errors='replace')
                        resp.get_streaming_mount_points['mount_points'].append(mp)
                    elif snum == 2:
                        resp.get_streaming_mount_points['error_message'] = sval.decode('utf-8', errors='replace')
            elif num == 8 and wire == 2:
                sub = parse_protobuf_fields(val)
                resp.remove_folder_from_sync = {'success': False, 'error_message': ''}
                for snum, swire, sval in sub:
                    if snum == 1: resp.remove_folder_from_sync['success'] = bool(sval)
                    elif snum == 2: resp.remove_folder_from_sync['error_message'] = sval.decode('utf-8', errors='replace')
            elif num == 9 and wire == 2:
                sub = parse_protobuf_fields(val)
                resp.trigger_sync = {'success': False, 'status': '', 'error_message': '', 'items_synced': 0}
                for snum, swire, sval in sub:
                    if snum == 1: resp.trigger_sync['success'] = bool(sval)
                    elif snum == 2: resp.trigger_sync['status'] = sval.decode('utf-8', errors='replace')
                    elif snum == 3: resp.trigger_sync['error_message'] = sval.decode('utf-8', errors='replace')
                    elif snum == 4: resp.trigger_sync['items_synced'] = int(sval)
            elif num == 10 and wire == 2:
                sub = parse_protobuf_fields(val)
                resp.set_sync_mode = {'success': False, 'error_message': ''}
                for snum, swire, sval in sub:
                    if snum == 1: resp.set_sync_mode['success'] = bool(sval)
                    elif snum == 2: resp.set_sync_mode['error_message'] = sval.decode('utf-8', errors='replace')
        return resp



def frame_message(payload: bytes) -> bytes:
    """Frame payload with a 4-byte little-endian length prefix."""
    return struct.pack('<I', len(payload)) + payload

def read_framed_message(sock) -> bytes:
    """Read a 4-byte length-prefixed message from a socket."""
    header = sock.recv(4)
    if not header or len(header) < 4:
        return b""
    length = struct.unpack('<I', header)[0]
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)
