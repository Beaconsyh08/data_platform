"""Authenticated Agent transport for transient dataset-local caption reads."""

from flask import jsonify, request

from lerobot.data_platform.caption_reads import MAX_REPLY_BYTES, CaptionReadRelay
from lerobot.data_platform.routes.control_plane import _bearer_token


def register_caption_read_routes(app, ctx) -> None:
    ctx.caption_read_relay = CaptionReadRelay()
    app.config["DATA_PLATFORM_CAPTION_READ_PROTOCOL"] = 1

    def node():
        return ctx.control_plane_store.authenticate_node(_bearer_token())

    @app.get("/api/agents/caption-reads")
    def claim_caption_read():
        actor = node()
        if actor is None:
            return jsonify(error="Invalid node token"), 401
        return jsonify(read=ctx.caption_read_relay.claim(actor["node_id"]))

    @app.post("/api/agents/caption-reads/<request_id>")
    def complete_caption_read(request_id):
        actor = node()
        if actor is None:
            return jsonify(error="Invalid node token"), 401
        try:
            status = int(request.args.get("status", "200"))
            binary = request.mimetype == "application/octet-stream"
            if request.mimetype not in {"application/json", "application/octet-stream"}:
                raise ValueError("Invalid reply type")
            if request.content_length is None or request.content_length > MAX_REPLY_BYTES:
                raise ValueError("Caption read reply is too large")
            ctx.caption_read_relay.complete(
                request_id, actor["node_id"], status, request.stream.read(MAX_REPLY_BYTES + 1), binary=binary
            )
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except KeyError:
            return jsonify(error="Caption read is no longer pending for this node"), 404
        return jsonify(ok=True)
