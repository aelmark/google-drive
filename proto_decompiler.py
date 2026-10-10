import sys
import os

TYPE_NAMES = {
    1: 'double', 2: 'float', 3: 'int64', 4: 'uint64', 5: 'int32', 6: 'fixed64',
    7: 'fixed32', 8: 'bool', 9: 'string', 10: 'group', 11: 'message', 12: 'bytes',
    13: 'uint32', 14: 'enum', 15: 'sfixed32', 16: 'sfixed64', 17: 'sint32', 18: 'sint64'
}
LABEL_NAMES = {1: 'optional', 2: 'required', 3: 'repeated'}

def decode_varint(buf, offset):
    res = 0
    shift = 0
    while offset < len(buf):
        b = buf[offset]
        offset += 1
        res |= (b & 0x7f) << shift
        if not (b & 0x80):
            break
        shift += 7
    return res, offset

def parse_msg(buf, offset=0, end=None):
    if end is None:
        end = len(buf)
    items = []
    while offset < end:
        if offset >= len(buf):
            break
        tag_varint, offset = decode_varint(buf, offset)
        tag = tag_varint >> 3
        wire = tag_varint & 7
        if wire == 0:
            val, offset = decode_varint(buf, offset)
            items.append((tag, wire, val))
        elif wire == 2:
            length, offset = decode_varint(buf, offset)
            if offset + length > len(buf):
                # reached end or invalid boundary
                break
            val = buf[offset:offset+length]
            offset += length
            items.append((tag, wire, val))
        elif wire == 1:
            val = buf[offset:offset+8]
            offset += 8
            items.append((tag, wire, val))
        elif wire == 5:
            val = buf[offset:offset+4]
            offset += 4
            items.append((tag, wire, val))
        else:
            # unsupported wire type or corrupted stream
            break
    return items

def decode_enum(ebuf):
    eitems = parse_msg(ebuf)
    name = ''
    values = []
    for t, w, v in eitems:
        if t == 1:
            name = v.decode('utf-8', errors='replace')
        elif t == 2: # EnumValueDescriptorProto
            vitems = parse_msg(v)
            vname, vnum = '', 0
            for vt, vw, vv in vitems:
                if vt == 1: vname = vv.decode('utf-8', errors='replace')
                elif vt == 2: vnum = vv
            values.append((vname, vnum))
    return name, values

def decode_field(fbuf):
    fitems = parse_msg(fbuf)
    name, num, ftype, label, type_name, default_val, oneof_idx = '', 0, 0, 1, '', '', None
    for t, w, v in fitems:
        if t == 1: name = v.decode('utf-8', errors='replace')
        elif t == 3: num = v
        elif t == 4: label = v
        elif t == 5: ftype = v
        elif t == 6: type_name = v.decode('utf-8', errors='replace')
        elif t == 7: default_val = v.decode('utf-8', errors='replace')
        elif t == 9: oneof_idx = v
    tname = TYPE_NAMES.get(ftype, str(ftype))
    if ftype in (11, 14):
        tname = type_name.split('.')[-1]
    lbl = LABEL_NAMES.get(label, '')
    return num, name, tname, lbl, default_val, oneof_idx

def decode_desc(dbuf):
    ditems = parse_msg(dbuf)
    msg_name = ''
    fields = []
    nested_types = []
    nested_enums = []
    oneofs = []
    for t, w, v in ditems:
        if t == 1 and isinstance(v, bytes):
            msg_name = v.decode('utf-8', errors='replace')
        elif t == 2 and w == 2 and isinstance(v, bytes):
            fields.append(decode_field(v))
        elif t == 3 and w == 2 and isinstance(v, bytes):
            nested_types.append(decode_desc(v))
        elif t == 4 and w == 2 and isinstance(v, bytes):
            nested_enums.append(decode_enum(v))
        elif t == 8 and w == 2 and isinstance(v, bytes): # oneof_decl
            oitems = parse_msg(v)
            for ot, ow, ov in oitems:
                if ot == 1 and isinstance(ov, bytes):
                    oneofs.append(ov.decode('utf-8', errors='replace'))
    return msg_name, fields, nested_types, nested_enums, oneofs

def decode_service(sbuf):
    sitems = parse_msg(sbuf)
    sname = ''
    methods = []
    for t, w, v in sitems:
        if t == 1:
            sname = v.decode('utf-8', errors='replace')
        elif t == 2: # MethodDescriptorProto
            mitems = parse_msg(v)
            mname, in_type, out_type = '', '', ''
            client_stream, server_stream = False, False
            for mt, mw, mv in mitems:
                if mt == 1: mname = mv.decode('utf-8', errors='replace')
                elif mt == 2: in_type = mv.decode('utf-8', errors='replace')
                elif mt == 3: out_type = mv.decode('utf-8', errors='replace')
                elif mt == 5: client_stream = bool(mv)
                elif mt == 6: server_stream = bool(mv)
            methods.append((mname, in_type.split('.')[-1], out_type.split('.')[-1], client_stream, server_stream))
    return sname, methods

def format_enum(name, values, indent='  '):
    lines = [f'{indent}enum {name} {{']
    for vname, vnum in values:
        lines.append(f'{indent}  {vname} = {vnum};')
    lines.append(f'{indent}}}')
    return '\n'.join(lines)

def format_message(msg_tuple, indent='  '):
    name, fields, nested_types, nested_enums, oneofs = msg_tuple
    lines = [f'{indent}message {name} {{']
    for n_enum in nested_enums:
        lines.append(format_enum(n_enum[0], n_enum[1], indent + '  '))
    for n_type in nested_types:
        lines.append(format_message(n_type, indent + '  '))
    
    # Group fields by oneof if applicable
    for num, fname, tname, lbl, defval, oidx in fields:
        prefix = f'{lbl} ' if lbl and lbl != 'optional' else ''
        extra = ''
        if oidx is not None and oidx < len(oneofs):
            extra = f' // oneof {oneofs[oidx]}'
        if defval:
            extra += f' [default = {defval}]'
        lines.append(f'{indent}  {prefix}{tname} {fname} = {num};{extra}')
    lines.append(f'{indent}}}')
    return '\n'.join(lines)

def decompile_file(buf):
    fitems = parse_msg(buf)
    filename, package, syntax = '', '', 'proto2'
    deps = []
    messages = []
    enums = []
    services = []
    for t, w, v in fitems:
        if t == 1 and not filename and isinstance(v, bytes):
            filename = v.decode('utf-8', errors='replace')
        elif t == 2 and not package and isinstance(v, bytes):
            package = v.decode('utf-8', errors='replace')
        elif t == 3 and isinstance(v, bytes):
            deps.append(v.decode('utf-8', errors='replace'))
        elif t == 4 and w == 2 and isinstance(v, bytes):
            m = decode_desc(v)
            if m[0]: # only if message has a name
                messages.append(m)
        elif t == 5 and w == 2 and isinstance(v, bytes):
            e = decode_enum(v)
            if e[0]:
                enums.append(e)
        elif t == 6 and w == 2 and isinstance(v, bytes):
            s = decode_service(v)
            if s[0]:
                services.append(s)
        elif t == 12 and syntax == 'proto2' and isinstance(v, bytes):
            syntax = v.decode('utf-8', errors='replace')
        elif t > 14:
            break
    
    out = [f'// Extracted protobuf descriptor: {filename}']
    out.append(f'syntax = "{syntax}";\n')
    if package:
        out.append(f'package {package};\n')
    for d in deps:
        out.append(f'import "{d}";')
    if deps:
        out.append('')
    for e in enums:
        out.append(format_enum(e[0], e[1], ''))
        out.append('')
    for m in messages:
        out.append(format_message(m, ''))
        out.append('')
    for sname, methods in services:
        out.append(f'service {sname} {{')
        for mname, in_t, out_t, c_str, s_str in methods:
            c_p = 'stream ' if c_str else ''
            s_p = 'stream ' if s_str else ''
            out.append(f'  rpc {mname} ({c_p}{in_t}) returns ({s_p}{out_t});')
        out.append('}\n')
    return filename, '\n'.join(out)

if __name__ == '__main__':
    binary_path = sys.argv[1]
    proto_name = sys.argv[2]
    with open(binary_path, 'rb') as f:
        data = f.read()
    
    target = proto_name.encode('utf-8')
    # Varint encoded length
    l = len(target)
    if l < 128:
        needle = bytes([10, l]) + target
    else:
        needle = bytes([10, (l & 0x7f) | 0x80, l >> 7]) + target
    
    pos = data.find(needle)
    if pos == -1:
        print(f"Could not find descriptor for {proto_name}", file=sys.stderr)
        sys.exit(1)
    
    # Attempt to read descriptor
    # The descriptor starts at pos. In protobuf, file descriptor might be enclosed or bare.
    # We inspect up to 500000 bytes
    chunk = data[pos:pos+500000]
    fname, proto_txt = decompile_file(chunk)
    print(proto_txt)
