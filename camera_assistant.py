#!/usr/bin/env python3
import time, requests, cv2
import numpy as np
import onnxruntime as ort
from collections import deque
from datetime import datetime
from enum import Enum

CAMERA_URL     = "http://localhost:8081"
PERSON_MODEL   = "models/yolov8n.onnx"
HAND_MODEL     = "models/hand_yolov8n.onnx"
LANDMARK_MODEL = "models/handpose_estimation.onnx"
IR_SERVER_URL  = "http://localhost:5000"

INTERVAL       = 0.5
CONF_THRESH    = 0.4
HAND_CONF      = 0.5
LANDMARK_CONF  = 0.5
IMG_SIZE       = 320
LANDMARK_SIZE  = 224
NMS_IOU        = 0.4
BOX_PAD        = 0.2

SMOOTH_WINDOW   = 5
SMOOTH_REQUIRED = 3

# STABILITY RULES
HAND_CONF_THRESHOLD    = 0.7
LANDMARK_CONF_THRESHOLD = 0.7
GESTURE_HOLD_TIME      = 2.3
COOLDOWN_TIME          = 2.5
CENTER_FRAME_THRESHOLD = 0.8

HAND_CONNECTIONS = [
    (0,1),(1,2),(2,3),(3,4),
    (0,5),(5,6),(6,7),(7,8),
    (0,9),(9,10),(10,11),(11,12),
    (0,13),(13,14),(14,15),(15,16),
    (0,17),(17,18),(18,19),(19,20),
    (5,9),(9,13),(13,17),
]

FINGERTIPS = [8, 12, 16, 20]  # index, middle, ring, pinky tips
MIDS       = [6, 10, 14, 18]  # their middle knuckles
THUMB_TIP  = 4
THUMB_MID  = 3
WRIST      = 0

# GESTURE TO IR COMMAND MAPPING
GESTURE_IR_MAP = {
    "OPEN_PALM": "power_toggle",
    "THUMBS_UP": "vol_up",
    "THUMBS_DOWN": "vol_down",
}

person_session   = ort.InferenceSession(PERSON_MODEL,   providers=["CPUExecutionProvider"])
hand_session     = ort.InferenceSession(HAND_MODEL,     providers=["CPUExecutionProvider"])
landmark_session = ort.InferenceSession(LANDMARK_MODEL, providers=["CPUExecutionProvider"])

person_input   = person_session.get_inputs()[0].name
hand_input     = hand_session.get_inputs()[0].name
landmark_input = landmark_session.get_inputs()[0].name

print("✅ person model loaded")
print("✅ hand model loaded")
print("✅ landmark model loaded")

hand_history    = deque(maxlen=SMOOTH_WINDOW)
gesture_history = deque(maxlen=SMOOTH_WINDOW)
prev_state      = None

# Gesture hold tracking
gesture_hold_start_time = None
last_gesture_held       = None
gesture_triggered       = False
cooldown_until          = 0.0

# ── Logging ────────────────────────────────────────────────────────────────────

class LogLevel(Enum):
    DEBUG = 0
    INFO = 1
    WARN = 2
    ERROR = 3

LOG_LEVEL = LogLevel.INFO

def log(level: LogLevel, msg: str):
    """Structured logging"""
    if level.value < LOG_LEVEL.value:
        return
    timestamp = datetime.now().strftime("%H:%M:%S")
    prefix = {
        LogLevel.DEBUG: "🔍",
        LogLevel.INFO: "📡",
        LogLevel.WARN: "⚠️ ",
        LogLevel.ERROR: "❌",
    }[level]
    print(f"[{timestamp}] {prefix} {msg}", flush=True)

# ── IR Token ──────────────────────────────────────────────────────────────────

def load_ir_token() -> str:
    """Fetch session token from pi_server"""
    try:
        r = requests.get(f"{IR_SERVER_URL}/api/local/token", timeout=3)
        if r.status_code == 200:
            return r.json().get("token", "")
    except requests.exceptions.RequestException:
        pass
    return ""

IR_TOKEN = load_ir_token()

# ── IR Command Sending ─────────────────────────────────────────────────────────

def ir_send_command(button_name):
    """
    Send IR command to pi_server.
    POST /api/send/<button_name>
    Header: X-Maven-Token: <token>
    """
    if not IR_TOKEN:
        log(LogLevel.ERROR, "No IR token — cannot send command")
        return False

    try:
        headers = {"X-Maven-Token": IR_TOKEN}
        r = requests.post(
            f"{IR_SERVER_URL}/api/send/{button_name}",
            headers=headers,
            timeout=3,
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("ok"):
                log(LogLevel.INFO, f"✅ IR command sent: {button_name}")
                return True
            log(LogLevel.WARN, f"IR send rejected: {data}")
        elif r.status_code == 403:
            log(LogLevel.ERROR, "IR token rejected (403) — restart camera_assistant")
        elif r.status_code == 404:
            log(LogLevel.WARN, f"Button '{button_name}' not learned in pi_server DB")
        else:
            log(LogLevel.WARN, f"IR send HTTP {r.status_code}")
    except requests.exceptions.RequestException as e:
        log(LogLevel.ERROR, f"IR send error: {e}")

    return False

# ── Camera Frame Capture ───────────────────────────────────────────────────────

def get_frame():
    for _ in range(10):
        try:
            r = requests.get(f"{CAMERA_URL}/frame.jpg", timeout=3)
            if r.status_code == 200:
                arr = np.frombuffer(r.content, np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is not None:
                    return frame
        except Exception as e:
            log(LogLevel.ERROR, f"[camera] {e}")
        time.sleep(0.25)
    return None

def preprocess_yolo(frame):
    img = cv2.resize(frame, (IMG_SIZE, IMG_SIZE))
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img = img.astype(np.float32) / 255.0
    return np.transpose(img, (2, 0, 1))[np.newaxis]

def count_people(frame):
    outputs = person_session.run(None, {person_input: preprocess_yolo(frame)})[0][0].T
    boxes, scores = [], []
    sx = frame.shape[1] / IMG_SIZE
    sy = frame.shape[0] / IMG_SIZE
    for row in outputs:
        class_scores = row[4:]
        class_id = int(np.argmax(class_scores))
        conf = float(class_scores[class_id])
        if class_id != 0 or conf < CONF_THRESH:
            continue
        cx, cy, w, h = row[:4]
        x1 = int((cx - w/2) * sx)
        y1 = int((cy - h/2) * sy)
        boxes.append([x1, y1, int(w*sx), int(h*sy)])
        scores.append(conf)
    if not boxes:
        return 0, []
    indices = cv2.dnn.NMSBoxes(boxes, scores, CONF_THRESH, NMS_IOU).flatten()
    boxes  = [[b[0], b[1], b[0]+b[2], b[1]+b[3]] for i, b in enumerate(boxes) if i in indices]
    scores = [s for i, s in enumerate(scores) if i in indices]
    return len(boxes), list(zip(boxes, scores))

def detect_best_hand(frame):
    outputs = hand_session.run(None, {hand_input: preprocess_yolo(frame)})[0][0].T
    boxes, scores = [], []
    sx = frame.shape[1] / IMG_SIZE
    sy = frame.shape[0] / IMG_SIZE
    for row in outputs:
        conf = float(row[4:].max())
        if conf < HAND_CONF:
            continue
        cx, cy, w, h = row[:4]
        x1 = int((cx - w/2) * sx)
        y1 = int((cy - h/2) * sy)
        boxes.append([x1, y1, int(w*sx), int(h*sy)])
        scores.append(conf)
    if not boxes:
        return None, None
    indices = cv2.dnn.NMSBoxes(boxes, scores, HAND_CONF, NMS_IOU)
    if len(indices) == 0:
        return None, None
    kept   = indices.flatten()
    boxes  = [boxes[i]  for i in kept]
    scores = [scores[i] for i in kept]
    best   = int(np.argmax(scores))
    bx, by, bw, bh = boxes[best]
    return [bx, by, bx+bw, by+bh], scores[best]

def is_hand_in_center(frame, hand_box):
    """Hand must be within center 80% of frame"""
    if hand_box is None:
        return False
    x1, y1, x2, y2 = hand_box
    fw = frame.shape[1]
    fh = frame.shape[0]
    
    margin_x = fw * (1 - CENTER_FRAME_THRESHOLD) / 2
    margin_y = fh * (1 - CENTER_FRAME_THRESHOLD) / 2
    
    left   = margin_x
    right  = fw - margin_x
    top    = margin_y
    bottom = fh - margin_y
    
    hand_cx = (x1 + x2) / 2
    hand_cy = (y1 + y2) / 2
    
    return left <= hand_cx <= right and top <= hand_cy <= bottom

def run_landmarks(frame, box):
    fh, fw = frame.shape[:2]
    x1, y1, x2, y2 = box
    bw = x2 - x1
    bh = y2 - y1
    pad_x = int(bw * BOX_PAD)
    pad_y = int(bh * BOX_PAD)
    cx1 = max(0, x1 - pad_x)
    cy1 = max(0, y1 - pad_y)
    cx2 = min(fw, x2 + pad_x)
    cy2 = min(fh, y2 + pad_y)
    crop = frame[cy1:cy2, cx1:cx2]
    if crop.size == 0:
        return None, 0.0

    img = cv2.resize(crop, (LANDMARK_SIZE, LANDMARK_SIZE))
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img = img.astype(np.float32) / 255.0
    img = img[np.newaxis]

    results  = landmark_session.run(None, {landmark_input: img})
    lm_raw   = results[0][0].reshape(21, 3)
    presence = float(results[1][0][0])

    if presence < LANDMARK_CONF:
        return None, presence

    crop_w = cx2 - cx1
    crop_h = cy2 - cy1
    lm = lm_raw.copy()
    lm[:, 0] = lm_raw[:, 0] / LANDMARK_SIZE * crop_w + cx1
    lm[:, 1] = lm_raw[:, 1] / LANDMARK_SIZE * crop_h + cy1

    return lm, presence

def classify_gesture(landmarks):
    """
    Classify hand gesture:
    - OPEN_PALM: all 4 fingers extended
    - FIST: all 4 fingers curled
    - THUMBS_UP: only thumb extended upward, fingers curled
    - THUMBS_DOWN: only thumb extended downward, fingers curled
    - UNKNOWN: anything else
    """
    # Check if fingers are extended (tip above middle knuckle)
    extended = []
    for tip, mid in zip(FINGERTIPS, MIDS):
        extended.append(landmarks[tip][1] < landmarks[mid][1])
    
    # Check thumb position relative to wrist
    thumb_up = landmarks[THUMB_TIP][1] < landmarks[WRIST][1]
    thumb_down = landmarks[THUMB_TIP][1] > landmarks[WRIST][1]
    
    # All fingers extended
    if all(extended):
        return "OPEN_PALM"
    
    # All fingers curled
    if not any(extended):
        # Check if it's a thumbs gesture
        if thumb_up:
            return "THUMBS_UP"
        elif thumb_down:
            return "THUMBS_DOWN"
        else:
            return "FIST"
    
    # Mixed state
    return "UNKNOWN"

def smooth_gesture(raw_gesture):
    gesture_history.append(raw_gesture)
    if len(gesture_history) < SMOOTH_REQUIRED:
        return "UNKNOWN"
    counts = {}
    for g in gesture_history:
        counts[g] = counts.get(g, 0) + 1
    best = max(counts, key=counts.get)
    if counts[best] >= SMOOTH_REQUIRED:
        return best
    return "UNKNOWN"

def update_gesture_hold(gesture):
    """
    Track gesture hold time and trigger on stable hold.
    Returns: (gesture_stable, time_held, should_trigger)
    """
    global gesture_hold_start_time, last_gesture_held, gesture_triggered, cooldown_until
    
    now = time.time()
    
    # Rule: Reset if gesture becomes UNKNOWN
    if gesture == "UNKNOWN":
        gesture_hold_start_time = None
        last_gesture_held = None
        gesture_triggered = False
        return False, 0.0, False
    
    # Start timing new gesture
    if gesture != last_gesture_held:
        gesture_hold_start_time = now
        last_gesture_held = gesture
        gesture_triggered = False
        return False, 0.0, False
    
    if gesture_hold_start_time is None:
        return False, 0.0, False
    
    time_held = now - gesture_hold_start_time
    gesture_stable = time_held >= GESTURE_HOLD_TIME
    in_cooldown = now < cooldown_until
    should_trigger = gesture_stable and not in_cooldown and not gesture_triggered
    
    if should_trigger:
        gesture_triggered = True
        cooldown_until = now + COOLDOWN_TIME
    
    return gesture_stable, time_held, should_trigger

def save_debug(frame, person_detections, hand_box, hand_conf, hand_present, 
               landmarks, lm_presence, gesture, gesture_stable, time_held, in_center):
    debug = frame.copy()
    
    # Draw center-of-frame boundary (80%)
    fw = frame.shape[1]
    fh = frame.shape[0]
    margin_x = fw * (1 - CENTER_FRAME_THRESHOLD) / 2
    margin_y = fh * (1 - CENTER_FRAME_THRESHOLD) / 2
    left   = int(margin_x)
    right  = int(fw - margin_x)
    top    = int(margin_y)
    bottom = int(fh - margin_y)
    cv2.rectangle(debug, (left, top), (right, bottom), (100, 100, 100), 1)
    
    # Person boxes — green
    for (x1, y1, x2, y2), conf in person_detections:
        cv2.rectangle(debug, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(debug, f"person {conf:.2f}", (x1, max(20, y1-8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

    # Hand box — yellow if confirmed and in center, red otherwise
    if hand_box is not None:
        x1, y1, x2, y2 = hand_box
        hand_color = (0, 255, 255) if (hand_present and in_center) else (0, 0, 255)
        cv2.rectangle(debug, (x1, y1), (x2, y2), hand_color, 2)
        cv2.putText(debug, f"hand {hand_conf:.2f}", (x1, max(20, y1-8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, hand_color, 1)

    # Landmarks — skeleton lines + dots
    if landmarks is not None:
        for a, b in HAND_CONNECTIONS:
            xa, ya = int(landmarks[a][0]), int(landmarks[a][1])
            xb, yb = int(landmarks[b][0]), int(landmarks[b][1])
            cv2.line(debug, (xa, ya), (xb, yb), (255, 200, 0), 1)
        for i, (x, y, z) in enumerate(landmarks):
            cv2.circle(debug, (int(x), int(y)), 4, (0, 200, 255), -1)

    # Status line
    if hand_present and in_center and landmarks is not None:
        hold_str = f"{time_held:.1f}s" if gesture_stable else f"{time_held:.1f}s (need {GESTURE_HOLD_TIME}s)"
        label = f"{gesture} {hold_str} (lm {lm_presence:.2f})"
        
        # Color based on gesture
        if gesture == "OPEN_PALM":
            color = (0, 255, 100)
        elif gesture == "THUMBS_UP":
            color = (100, 255, 0)
        elif gesture == "THUMBS_DOWN":
            color = (100, 0, 255)
        else:
            color = (180, 180, 180)
    elif hand_present and not in_center:
        label = f"HAND — OUT OF CENTER"
        color = (0, 165, 255)
    elif hand_present:
        label = f"HAND — low landmark conf ({lm_presence:.2f})"
        color = (0, 165, 255)
    else:
        label = "NO HAND"
        color = (0, 0, 255)

    cv2.putText(debug, label, (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

    cv2.imwrite("debug_camera_assistant.jpg", debug)

# ── Startup ────────────────────────────────────────────────────────────────────

print(f"[camera_assistant_pi_ir] watching {CAMERA_URL}")
print(f"Stability rules enabled:")
print(f"  • Hand conf threshold: {HAND_CONF_THRESHOLD}")
print(f"  • Landmark conf threshold: {LANDMARK_CONF_THRESHOLD}")
print(f"  • Gesture hold time: {GESTURE_HOLD_TIME}s")
print(f"  • Cooldown time: {COOLDOWN_TIME}s")
print(f"  • Hand center frame threshold: {CENTER_FRAME_THRESHOLD*100:.0f}%")
print()
print("Gesture mappings:")
for gesture, ir_cmd in GESTURE_IR_MAP.items():
    print(f"  • {gesture} → {ir_cmd}")
print()

if IR_TOKEN:
    log(LogLevel.INFO, f"✅ IR token loaded ({IR_TOKEN[:6]}…)")
else:
    log(LogLevel.WARN, "IR token not found — IR commands will not fire")

print()

# ── Main Loop ──────────────────────────────────────────────────────────────────

while True:
    frame = get_frame()
    if frame is None:
        log(LogLevel.DEBUG, "no frame")
        time.sleep(INTERVAL)
        continue

    person_count, person_detections = count_people(frame)
    
    # Rule 1: If person_count == 0 → ignore hand and gesture completely
    if person_count == 0:
        hand_history.append(False)
        hand_present = False
        hand_box, hand_conf = None, None
        landmarks, lm_presence = None, 0.0
        gesture = "UNKNOWN"
        gesture_stable = False
        time_held = 0.0
        in_center = False
    else:
        hand_box, hand_conf = detect_best_hand(frame)
        in_center = is_hand_in_center(frame, hand_box)
        
        # Rule 2: Only accept hand if hand_conf >= 0.7 AND in center
        if hand_conf is not None and hand_conf >= HAND_CONF_THRESHOLD and in_center:
            hand_history.append(True)
        else:
            hand_history.append(False)
        
        hand_present = sum(hand_history) >= SMOOTH_REQUIRED
        
        landmarks, lm_presence = None, 0.0
        gesture = "UNKNOWN"
        gesture_stable = False
        time_held = 0.0
        
        if hand_present and hand_box is not None:
            landmarks, lm_presence = run_landmarks(frame, hand_box)
            
            # Rule 3: Only accept landmarks if lm_presence >= 0.7
            if landmarks is not None and lm_presence >= LANDMARK_CONF_THRESHOLD:
                raw_gesture = classify_gesture(landmarks)
                gesture = smooth_gesture(raw_gesture)
            else:
                gesture_history.append("UNKNOWN")
                gesture = "UNKNOWN"
        
        # Rules 4, 6, 7: Update gesture hold tracking
        gesture_stable, time_held, should_trigger = update_gesture_hold(gesture)

    state = f"{gesture}/{person_count}p" if hand_present else f"PERSON/{person_count}p" if person_count > 0 else "EMPTY"

    if state != prev_state:
        if hand_present and gesture != "UNKNOWN":
            conf_str = f"{hand_conf:.2f}" if hand_conf else "n/a"
            lm_str   = f"landmarks ✅ {lm_presence:.2f}" if landmarks is not None else f"landmarks ❌ {lm_presence:.2f}"
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✋ {gesture} (conf {conf_str}) | {lm_str} | people: {person_count}")
        elif person_count > 0:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 🧍 People: {person_count}")
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] empty")
        prev_state = state

    # ── TRIGGER EVENT: Send IR command ─────────────────────────────────────────
    if 'should_trigger' in locals() and should_trigger:
        log(LogLevel.INFO, f"🎯 TRIGGER: {gesture} held for {time_held:.1f}s")
        
        # Get the IR command for this gesture
        ir_command = GESTURE_IR_MAP.get(gesture)
        
        if ir_command:
            log(LogLevel.INFO, f"Sending IR command: {ir_command}")
            ir_send_command(ir_command)
        else:
            log(LogLevel.WARN, f"No IR mapping for gesture: {gesture}")

    save_debug(frame, person_detections, hand_box,
               hand_conf if hand_conf else 0.0,
               hand_present, landmarks, lm_presence, gesture, gesture_stable, time_held, in_center)
    
    time.sleep(INTERVAL)