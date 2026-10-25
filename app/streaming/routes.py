from flask import Blueprint, Response, abort, current_app

streaming_bp = Blueprint("streaming", __name__)


def _camera_or_404(hive_id):
    camera = current_app.extensions.get("cameras", {}).get(hive_id)
    if camera is None:
        abort(404, "No camera for this hive")
    return camera


def _mjpeg(camera):
    """Sends each new annotated frame from the shared camera thread."""
    last_id = -1
    while True:
        frame_id, jpeg = camera.wait_for_frame(last_id, timeout=5.0)
        if jpeg is None or frame_id == last_id:
            continue  # no new frame yet (camera reconnecting)
        last_id = frame_id
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
        )


@streaming_bp.route("/video_feed/<int:hive_id>")
def video_feed(hive_id):
    camera = _camera_or_404(hive_id)
    return Response(_mjpeg(camera), mimetype="multipart/x-mixed-replace; boundary=frame")


@streaming_bp.route("/snapshot/<int:hive_id>.jpg")
def snapshot(hive_id):
    camera = _camera_or_404(hive_id)
    _, jpeg = camera.wait_for_frame(-1, timeout=5.0)
    if jpeg is None:
        abort(503, "No frame available yet")
    return Response(jpeg, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})
