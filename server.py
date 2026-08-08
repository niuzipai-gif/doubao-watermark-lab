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
    default_x = round(width * 0.54)
    default_y = round(height * 0.64)
    default_w = round(width * 0.46)
    default_h = round(height * 0.32)
    x = max(0, min(width, number("x", default_x)))
    y = max(0, min(height, number("y", default_y)))
    w = max(0, min(width - x, number("w", default_w)))
    h = max(0, min(height - y, number("h", default_h)))
    return x, y, w, h


def low_sat_bright(frame, value_threshold=125, saturation_threshold=135):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    return cv2.inRange(
        hsv,
        np.array([0, 0, value_threshold], dtype=np.uint8),
        np.array([180, saturation_threshold, 255], dtype=np.uint8),
    )


def clamp_box(box, width, height):
    x, y, w, h = [int(value) for value in box]
    x = max(0, min(width - 1, x))
    y = max(0, min(height - 1, y))
    w = max(1, min(width - x, w))
    h = max(1, min(height - y, h))
    return x, y, w, h


def box_mask(shape, box, padding=5):
    height, width = shape[:2]
    if isinstance(padding, (tuple, list)):
        left_padding, top_padding, right_padding, bottom_padding = [int(value) for value in padding]
    else:
        left_padding = top_padding = right_padding = bottom_padding = int(padding)
    x, y, w, h = clamp_box(
        (
            box[0] - left_padding,
            box[1] - top_padding,
            box[2] + left_padding + right_padding,
            box[3] + top_padding + bottom_padding,
        ),
        width,
        height,
    )
    mask = np.zeros((height, width), dtype=np.uint8)
    mask[y : y + h, x : x + w] = 255
    return mask


def candidate_mask(frame):
    height, width = frame.shape[:2]
    candidate = low_sat_bright(frame, value_threshold=120, saturation_threshold=170)
    corners = np.zeros_like(candidate)
    corners[: int(height * 0.18), int(width * 0.03) : int(width * 0.40)] = 255
    corners[int(height * 0.80) :, int(width * 0.62) :] = 255
    candidate = cv2.bitwise_and(candidate, corners)
    candidate = cv2.morphologyEx(candidate, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    return candidate


def find_watermark_box(frame, previous_box=None):
    height, width = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    regions = {
        "top-left": (0.04, 0.36, 0.015, 0.15),
        "bottom-right": (0.70, 0.995, 0.84, 0.995),
    }
    options = []
    for value_threshold in (90, 110, 130, 150, 170, 190):
        candidate = cv2.inRange(
            hsv,
            np.array([0, 0, value_threshold], dtype=np.uint8),
            np.array([180, 170, 255], dtype=np.uint8),
        )
        candidate = cv2.morphologyEx(candidate, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        count, _, stats, _ = cv2.connectedComponentsWithStats(candidate, 8)
        for corner, (x0, x1, y0, y1) in regions.items():
            glyphs = []
            for label in range(1, count):
                x = int(stats[label, cv2.CC_STAT_LEFT])
                y = int(stats[label, cv2.CC_STAT_TOP])
                w = int(stats[label, cv2.CC_STAT_WIDTH])
                h = int(stats[label, cv2.CC_STAT_HEIGHT])
                area = int(stats[label, cv2.CC_STAT_AREA])
                if x < width * x0 or x + w > width * x1 or y < height * y0 or y + h > height * y1:
                    continue
                if w < 2 or w > max(12, int(width * 0.08)):
                    continue
                if h < max(5, int(height * 0.008)) or h > max(90, int(height * 0.08)):
                    continue
                if area < max(5, int(width * height * 0.000006)):
                    continue
                glyphs.append((x, y, w, h, area))

            for anchor in glyphs:
                center_y = anchor[1] + anchor[3] / 2
                row = [glyph for glyph in glyphs if abs(glyph[1] + glyph[3] / 2 - center_y) <= max(4, height * 0.012)]
                row.sort(key=lambda glyph: glyph[0])
                if len(row) < 4:
                    continue
                clusters = []
                cluster = []
                gap_limit = max(7, int(width * 0.03))
                for glyph in row:
                    if cluster and glyph[0] - (cluster[-1][0] + cluster[-1][2]) > gap_limit:
                        clusters.append(cluster)
                        cluster = []
                    cluster.append(glyph)
                if cluster:
                    clusters.append(cluster)
                for cluster in clusters:
                    if len(cluster) < 5:
                        continue
                    left = min(glyph[0] for glyph in cluster)
                    top = min(glyph[1] for glyph in cluster)
                    right = max(glyph[0] + glyph[2] for glyph in cluster)
                    bottom = max(glyph[1] + glyph[3] for glyph in cluster)
                    box_width = right - left
                    box_height = bottom - top
                    if box_width < width * 0.08 or box_width > width * 0.28:
                        continue
                    if box_height < height * 0.015 or box_height > height * 0.075:
                        continue
                    if box_width / max(box_height, 1) < 2.2:
                        continue
                    if corner == "top-left":
                        # The left-side scene banner is a common false positive; the logo is brighter and sits below it.
                        if value_threshold < 175 or right > width * 0.36:
                            continue
                    else:
                        # The logo hugs the right edge; cup rims and chair highlights do not.
                        if right < width * 0.90:
                            continue
                    score = len(cluster) * 4 + box_width / width * 18 + value_threshold / 400
                    if corner == "bottom-right":
                        score += 1.5
                    options.append((score, (left, top, box_width, box_height)))
    if options:
        options.sort(key=lambda item: item[0], reverse=True)
        return options[0][1], candidate_mask(frame)
    return None, candidate_mask(frame)


def track_box(previous_gray, current_gray, previous_box):
    if previous_gray is None or previous_box is None:
        return None
    height, width = current_gray.shape[:2]
    x, y, w, h = clamp_box(previous_box, width, height)
    padding = max(4, int(max(w, h) * 0.12))
    template_box = clamp_box((x - padding, y - padding, w + padding * 2, h + padding * 2), width, height)
    tx, ty, tw, th = template_box
    template = previous_gray[ty : ty + th, tx : tx + tw]
    if template.shape[0] < 8 or template.shape[1] < 12:
        return None
    search_padding = max(32, int(max(w, h) * 1.2))
    sx = max(0, tx - search_padding)
    sy = max(0, ty - search_padding)
    ex = min(width, tx + tw + search_padding)
    ey = min(height, ty + th + search_padding)
    search = current_gray[sy:ey, sx:ex]
    if search.shape[0] < template.shape[0] or search.shape[1] < template.shape[1]:
        return None
    result = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
    _, confidence, _, location = cv2.minMaxLoc(result)
    if confidence < 0.36:
        return None
    return clamp_box((sx + location[0] + padding, sy + location[1] + padding, w, h), width, height)


def stabilize_box(previous_box, current_box, width, height):
    if not previous_box:
        return current_box
    previous_center = np.array([previous_box[0] + previous_box[2] / 2, previous_box[1] + previous_box[3] / 2])
    current_center = np.array([current_box[0] + current_box[2] / 2, current_box[1] + current_box[3] / 2])
    if np.linalg.norm(current_center - previous_center) > width * 0.12:
        return current_box
    blended = previous_center * 0.35 + current_center * 0.65
    size = np.array([current_box[2], current_box[3]]) * 0.65 + np.array([previous_box[2], previous_box[3]]) * 0.35
    return clamp_box((blended[0] - size[0] / 2, blended[1] - size[1] / 2, size[0], size[1]), width, height)


def image_mask(frame, rect, full_rect=False):
    if full_rect:
        return box_mask(frame.shape, rect, padding=0)
    detected, candidate = find_watermark_box(frame)
    if detected and detected[0] >= frame.shape[1] * 0.65 and detected[1] >= frame.shape[0] * 0.75:
        return box_mask(frame.shape, detected, padding=max(4, int(frame.shape[1] / 160)))
    # The still-image mark is anchored to the lower-right corner. Keep the
    # fallback proportional, but mask only the detected glyph pixels instead of
    # painting a large opaque rectangle over the scene.
    height, width = frame.shape[:2]
    glyphs = low_sat_bright(frame, value_threshold=100, saturation_threshold=170)
    x0 = int(width * 0.80)
    y0 = int(height * 0.92)
    mask = np.zeros(frame.shape[:2], dtype=np.uint8)
    mask[y0:, x0:] = glyphs[y0:, x0:]
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1)
    if np.count_nonzero(mask) >= max(120, int(width * height * 0.00002)):
        return mask
    fallback = (int(width * 0.80), int(height * 0.90), int(width * 0.20), int(height * 0.10))
    return box_mask(frame.shape, fallback, padding=0)


def clean_image_frame(frame, rect, full_rect=False):
    mask = image_mask(frame, rect, full_rect=full_rect)
    pixels = int(np.count_nonzero(mask))
    if pixels == 0:
        return frame, 0
    radius = max(3, min(9, int(frame.shape[1] / 180)))
    return cv2.inpaint(frame, mask, radius, cv2.INPAINT_TELEA), pixels


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
    cleaned, pixels = clean_image_frame(
        frame,
        rect_from_request(width, height),
        full_rect=request.form.get("fullRect") == "1",
    )
    ok, encoded = cv2.imencode(".png", cleaned)
    if not ok:
        return jsonify(error="cannot encode image"), 500
    response = send_file(
        io.BytesIO(encoded.tobytes()),
        mimetype="image/png",
        as_attachment=True,
        download_name=f"{Path(upload.filename).stem}-doubao-cleaned-v3.png",
    )
    response.headers["X-Doubao-Mask-Pixels"] = str(pixels)
    response.headers["X-Doubao-Mode"] = "box-inpaint"
    return response


@app.post("/api/clean-video")
def clean_video():
    upload = request.files.get("media")
    if not upload:
        return jsonify(error="missing media"), 400
    full_rect = request.form.get("fullRect") == "1"
    with tempfile.TemporaryDirectory(prefix="doubao-v3-") as temp_dir:
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
        detected_frames = 0
        tracked_frames = 0
        mask_pixels = 0
        previous_gray = None
        previous_box = None
        missing_frames = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            frames += 1
            if full_rect:
                cleaned = cv2.inpaint(frame, box_mask(frame.shape, rect, padding=0), 5, cv2.INPAINT_TELEA)
                mask_pixels += rect[2] * rect[3]
            else:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                box, _ = find_watermark_box(frame, previous_box)
                mode = "detected"
                if box is not None:
                    box = stabilize_box(previous_box, box, width, height)
                    detected_frames += 1
                    missing_frames = 0
                elif previous_box is not None and missing_frames < 2:
                    box = track_box(previous_gray, gray, previous_box)
                    mode = "tracked"
                    if box is not None:
                        tracked_frames += 1
                    missing_frames += 1
                else:
                    box = None
                    previous_box = None
                    missing_frames += 1
                if box is not None:
                    previous_box = box
                    mask = box_mask(
                        frame.shape,
                        box,
                        padding=(max(12, int(width / 32)), max(5, int(height / 150)), max(6, int(width / 120)), max(5, int(height / 150))),
                    )
                    cleaned = cv2.inpaint(frame, mask, max(3, min(9, int(width / 180))), cv2.INPAINT_TELEA)
                    mask_pixels += int(np.count_nonzero(mask))
                else:
                    cleaned = frame
                previous_gray = gray
            writer.write(cleaned)
        capture.release()
        writer.release()
        if frames == 0:
            return jsonify(error="video has no frames"), 400

        output_path = temp / "cleaned.webm"
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            command = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(processed_path),
                "-i",
                str(source_path),
                "-map",
                "0:v:0",
                "-map",
                "1:a?",
                "-c:v",
                "libvpx-vp9",
                "-deadline",
                "realtime",
                "-cpu-used",
                "4",
                "-crf",
                "31",
                "-b:v",
                "0",
                "-c:a",
                "libopus",
                str(output_path),
            ]
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode != 0:
                output_path = processed_path
        else:
            output_path = processed_path
        mime = "video/webm" if output_path.suffix == ".webm" else "video/mp4"
        response = send_file(
            io.BytesIO(output_path.read_bytes()),
            mimetype=mime,
            as_attachment=True,
            download_name=f"{Path(upload.filename).stem}-doubao-cleaned-v3{output_path.suffix}",
        )
        response.headers["X-Doubao-Frames"] = str(frames)
        response.headers["X-Doubao-Detected-Frames"] = str(detected_frames)
        response.headers["X-Doubao-Tracked-Frames"] = str(tracked_frames)
        response.headers["X-Doubao-Mask-Pixels"] = str(mask_pixels)
        response.headers["X-Doubao-Mode"] = "per-frame-box-tracking"
        return response


@app.get("/")
def index():
    return app.send_static_file("index.html")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "4173")), threaded=True)
