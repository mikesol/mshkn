def transform(req):
    b = req["body_json"]
    if type(b) != "dict" or "b64" not in b:
        return None
    payload = b["b64"]
    if type(payload) != "string" or len(payload) == 0 or len(payload) > 65536:
        return None
    for ch in payload.elems():
        if not (ch.isalnum() or ch in "+/="):
            return None
    return {
        "action": "fork",
        "label": "brain",
        "self_destruct": True,
        "exclusive": "error_on_conflict",
        "exec": "membrane say " + payload,
    }
