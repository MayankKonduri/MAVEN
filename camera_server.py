from flask import Flask, Response, jsonify
from picamera2 import Picamera2
import cv2
import time
import threading

app = Flask(__name__)

FRAME_LOCK = threading.Lock()
VIEWER_LOCK = threading.Lock()
latest_jpeg = None
camera_ok = False
camera_error = None
viewer_count = 0

WIDTH, HEIGHT = 640, 480
FPS_LIMIT = 12


def change_viewers(delta):
    global viewer_count
    with VIEWER_LOCK:
        viewer_count += delta


def camera_loop():
    global latest_jpeg, camera_ok, camera_error

    try:
        picam2 = Picamera2()
        config = picam2.create_video_configuration(
            main={"size": (WIDTH, HEIGHT), "format": "RGB888"}
        )
        picam2.configure(config)
        picam2.start()
        time.sleep(1)
        camera_ok = True
        camera_error = None
    except Exception as e:
        camera_ok = False
        camera_error = f"Camera Not Found: {e}"
        return

    delay = 1.0 / FPS_LIMIT

    while True:
        try:
            with VIEWER_LOCK:
                watching = viewer_count > 0

            if not watching:
                time.sleep(0.25)
                continue

            frame = picam2.capture_array()
            ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ok:
                with FRAME_LOCK:
                    latest_jpeg = buffer.tobytes()
            time.sleep(delay)

        except Exception as e:
            camera_ok = False
            camera_error = str(e)
            time.sleep(1)


@app.route("/")
def index():
    if not camera_ok:
        return f"""
        <html>
          <body style="background:#111;color:white;text-align:center;font-family:sans-serif;">
            <h1>MAVEN Camera</h1>
            <p>{camera_error or "Camera Not Found"}</p>
          </body>
        </html>
        """
    return """
    <html>
      <body style="background:#111;color:white;text-align:center;font-family:sans-serif;">
        <h1>MAVEN Live Camera</h1>
        <img src="/video" width="640">
      </body>
    </html>
    """


@app.route("/status")
def status():
    with VIEWER_LOCK:
        viewers = viewer_count
    return jsonify({
        "ok": camera_ok,
        "error": camera_error,
        "width": WIDTH,
        "height": HEIGHT,
        "fps_limit": FPS_LIMIT,
        "viewers": viewers
    })


@app.route("/frame.jpg")
def frame_jpg():
    if not camera_ok:
        return "Camera Not Found", 503

    change_viewers(+1)
    try:
        time.sleep(0.15)
        with FRAME_LOCK:
            frame = latest_jpeg
    finally:
        change_viewers(-1)

    if frame is None:
        return "Frame not ready", 503
    return Response(frame, mimetype="image/jpeg")


@app.route("/video")
def video():
    def generate():
        change_viewers(+1)
        fail_count = 0
        try:
            while True:
                if not camera_ok:
                    fail_count += 1
                    if fail_count > 20:  # ~20 seconds of failures, then drop stream
                        break
                    time.sleep(1)
                    continue

                fail_count = 0

                with FRAME_LOCK:
                    frame = latest_jpeg

                if frame is None:
                    time.sleep(0.05)
                    continue

                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" +
                    frame +
                    b"\r\n"
                )
                time.sleep(1.0 / FPS_LIMIT)
        finally:
            change_viewers(-1)

    if not camera_ok:
        return "Camera Not Found", 503

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )


if __name__ == "__main__":
    t = threading.Thread(target=camera_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=8081, threaded=True)