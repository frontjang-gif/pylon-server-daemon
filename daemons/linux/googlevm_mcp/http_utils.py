"""Small bounded HTTP parsing helpers shared by the two transports."""
import json
from urllib.parse import parse_qsl


async def read_body(request, limit):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise ValueError("Request body exceeds size limit")
    return bytes(body)


def unique_parameters(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate parameter: {key}")
        result[key] = value
    return result


async def read_form(request):
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/x-www-form-urlencoded":
        raise ValueError("Expected application/x-www-form-urlencoded")
    body = (await read_body(request, 16384)).decode("utf-8")
    return unique_parameters(parse_qsl(body, keep_blank_values=True, max_num_fields=30))


async def read_json(request, limit):
    return json.loads(await read_body(request, limit))
