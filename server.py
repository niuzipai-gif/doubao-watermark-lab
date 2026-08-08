import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, jsonify, request, send_file


ROOT = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=str(ROOT), static_url_path="")


def number(name, default=0):
    try:
        return int(float(request.form.get(name, default)))
    except (TypeError, ValueError):
        return default


def rect_from_request(width, height):
    x = max(0, min(width, number("x", round(width * 0.80))))
    y = max(0, min(height, number("y", round(height * 0.90))))
    w = max(0, min(width - x, number("w", round(width * 0.20))))
    h = max(0, min(height - y, number("h", round(height * 0.10))))
    return x, y, w, h


def low_sat_bright(frame, value_threshold=142, saturation_threshold=70):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, np.array([0, 0, value_threshold], dtype=np.uint8), np.array([180, saturation_threshold, 255], dtype=np.uint8))


def keep_small_components(candidate, zones, frame_area):
    kept = np.zeros_like(candidate)
    min_area = max(4, int(frame_area * 0.000006))
    max_area = max(240, int(frame_area * 0.0009))
    for left, top, right, bottom in zones:
        roi = candidate[top:bottom, left:right]
        count, labels, stats, _ = cv2.connectedComponentsWithStats((roi > 0).astype(np.uint8), 8)
        for label in range(1, count):
            area = int(stats[label, cv2.CC_STAT_AREA])
            w = int(stats[label, cv2.CC_STAT_WIDTH])
            h = int(stats[label, cv2.CC_STAT_HEIGHT])
            if min_area <= area <= max_area and w >= 2 and h >= 2:
                kept[top:bottom, left:right][labels == label] = 255
    return kept


def build_mask(frame, rect, video_mode=False, full_rect=False):
    height, width = frame.shape[:2]
    if full_rect:
        x, y, w, h = rect
        mask = np.zeros((height, width), dtype=np.uint8)
        mask[y:y + h, x:x + w] = 255
        return mask

    if video_mode:
        zones = [
            (int(width * 0.03), int(height * 0.015), int(width * 0.27), int(height * 0.09)),
            (int(width * 0.73), int(height * 0.015), int(width * 0.97), int(height * 0.09)),
            (int(width * 0.03), int(height * 0.89), int(width * 0.27), int(height * 0.985)),
            (int(width * 0.73), int(height * 0.89), int(width * 0.99), int(height * 0.985)),
        ]
        mask = np.zeros((height, width), dtype=np.uint8)
        scale = width / 720.0
        min_area = max(20, int(80 * scale * scale))
        max_area = max(450, int(450 * scale * scale))
        min_width = max(3, int(4 * scale))
        max_width = max(18, int(26 * scale))
        min_height = max(8, int(15 * scale))
        max_height = max(25, int(35 * scale))
        for index, (left, top, right, bottom) in enumerate(zones):
            threshold = 185 if index < 2 else 110
            roi = low_sat_bright(frame, threshold, 100)[top:bottom, left:right]
            count, labels, stats, _ = cv2.connectedComponentsWithStats((roi > 0).astype(np.uint8), 8)
            glyphs = []
            for label in range(1, count):
                area = int(stats[label, cv2.CC_STAT_AREA])
                box_width = int(stats[label, cv2.CC_STAT_WIDTH])
                box_height = int(stats[label, cv2.CC_STAT_HEIGHT])
                if min_area <= area <= max_area and min_width <= box_width <= max_width and min_height <= box_height <= max_height:
                    glyphs.append(label)
            if len(glyphs) < 6:
                continue
            x0 = min(int(stats[label, cv2.CC_STAT_LEFT]) for label in glyphs) + left
            y0 = min(int(stats[label, cv2.CC_STAT_TOP]) for label in glyphs) + top
            x1 = max(int(stats[label, cv2.CC_STAT_LEFT] + stats[label, cv2.CC_STAT_WIDTH]) for label in glyphs) + left
            y1 = max(int(stats[label, cv2.CC_STAT_TOP] + stats[label, cv2.CC_STAT_HEIGHT]) for label in glyphs) + top
            padding_x = max(16, int(33 * scale))
            padding_y = max(3, int(4 * scale))
            for label in glyphs:
                mask[top:bottom, left:right][labels == label] = 255
            mask[max(0, y0 - padding_y):min(height, y1 + padding_y), max(0, x0 - padding_x):min(width, x0 + 2)] = 255
        kernel = np.ones((3, 3), np.uint8)
        return cv2.dilate(mask, kernel, iterations=1)

    x, y, w, h = rect
    candidate = low_sat_bright(frame)
    mask = np.zeros((height, width), dtype=np.uint8)
    roi = candidate[y:y + h, x:x + w]
    mask[y:y + h, x:x + w] = keep_small_components(roi, [(0, 0, w, h)], width * height)
    return cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1)


def clean_frame(frame, rect, video_mode=False, full_rect=False):
    mask = build_mask(frame, rect, video_mode=video_mode, full_rect=full_rect)
    pixels = int(np.count_nonzero(mask))
    if pixels == 0:
        return frame, 0
    return cv2.inpaint(frame, mask, 3, cv2.INPAINT_TELEA), pixels


@app.post("/api/clean-image")
def clean_image():
    upload = request.files.get("media")
    if not upload:
        return jsonify(error="missing media"), 400
    data = np.frombuffer(upload.read(), dtype=np.uint8)
    frame = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if frame is None:
        return jsonify(error="cannot decode image"), 400
    height, width = frame.shape[:2]
    cleaned, pixels = clean_frame(frame, rect_from_request(width, height), full_rect=request.form.get("fullRect") == "1")
    ok, encoded = cv2.imencode(".png", cleaned)
    if not ok:
        return jsonify(error="cannot encode image"), 500
    response = send_file(io.BytesIO(encoded.tobytes()), mimetype="image/png", as_attachment=True, download_name=f"{Path(upload.filename).stem}-doubao-cleaned-v2.png")
    response.headers["X-Doubao-Mask-Pixels"] = str(pixels)
    return response


@app.post("/api/clean-video")
def clean_video():
    upload = request.files.get("media")
    if not upload:
        return jsonify(error="missing media"), 400
    full_rect = request.form.get("fullRect") == "1"
    with tempfile.TemporaryDirectory(prefix="doubao-v2-") as temp_dir:
        temp = Path(temp_dir)
        source_path = temp / (Path(upload.filename).name or "source.mp4")
        upload.save(source_path)
        capture = cv2.VideoCapture(str(source_path))
        if not capture.isOpened():
            return jsonify(error="cannot open video"), 400
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = capture.get(cv2.CAP_PROP_FPS) or 24.0
        rect = rect_from_request(width, height)
        processed_path = temp / "processed.mp4"
        writer = cv2.VideoWriter(str(processed_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
        if not writer.isOpened():
            capture.release()
            return jsonify(error="cannot create video writer"), 500
        frames = 0
        mask_pixels = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            cleaned, pixels = clean_frame(frame, rect, video_mode=True, full_rect=full_rect)
            writer.write(cleaned)
            frames += 1
            mask_pixels += pixels
        capture.release()
        writer.release()
        if frames == 0:
            return jsonify(error="video has no frames"), 400

        output_path = temp / "cleaned.webm"
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(processed_path), "-i", str(source_path), "-map", "0:v:0", "-map", "1:a?", "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "4", "-crf", "32", "-b:v", "0", "-c:a", "libopus", str(output_path)]
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode != 0:
                output_path = processed_path
        else:
            output_path = processed_path
        mime = "video/webm" if output_path.suffix == ".webm" else "video/mp4"
        output_bytes = output_path.read_bytes()
        response = send_file(io.BytesIO(output_bytes), mimetype=mime, as_attachment=True, download_name=f"{Path(upload.filename).stem}-doubao-cleaned-v2{output_path.suffix}")
        response.headers["X-Doubao-Frames"] = str(frames)
        response.headers["X-Doubao-Mask-Pixels"] = str(mask_pixels)
        return response


@app.get("/")
def index():
    return app.send_static_file("index.html")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "4173")), threaded=True)
