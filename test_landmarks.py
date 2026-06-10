#!/usr/bin/env python3
import time, requests, cv2
import numpy as np
import onnxruntime as ort

CAMERA_URL     = "http://localhost:8081"
HAND_MODEL     = "models/hand_yolov8n.onnx"
LANDMARK_MODEL = "models/handpose_estimation.onnx"
HAND_CONF      = 0.5
IMG_SIZE       = 320
LANDMARK_SIZE  = 224
NMS_IOU        = 0.4
BOX_PAD        = 0.2

hand_session     = ort.InferenceSession(HAND_MODEL,     providers=["CPUExecutionProvider"])
landmark_session = ort.InferenceSession(LANDMARK_MODEL, providers=["CPUExecutionProvider"])
hand_input       = hand_session.get_inputs()[0].name
landmark_input   = landmark_session.get_inputs()[0].name
print("✅ hand model loaded")
print("✅ landmark model loaded")

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
            print(f"[camera] {e}")
        time.sleep(0.25)
    return None

def detect_best_hand(frame):
    img = cv2.resize(frame, (IMG_SIZE, IMG_SIZE))
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img = img.astype(np.float32) / 255.0
    img = np.transpose(img, (2, 0, 1))[np.newaxis]

    outputs = hand_session.run(None, {hand_input: img})[0][0].T
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
        return None, None, None, None

    img = cv2.resize(crop, (LANDMARK_SIZE, LANDMARK_SIZE))
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img = img.astype(np.float32) / 255.0
    img = img[np.newaxis]  # NHWC

    results = landmark_session.run(None, {landmark_input: img})

    identity   = results[0]   # [1, 63]
    identity_1 = results[1]   # [1, 1]
    identity_2 = results[2]   # [1, 1]

    lm = identity[0].reshape(21, 3)

    # Map x,y back to original frame coords
    crop_w = cx2 - cx1
    crop_h = cy2 - cy1
    lm_mapped = lm.copy()
    lm_mapped[:, 0] = lm[:, 0] / LANDMARK_SIZE * crop_w + cx1
    lm_mapped[:, 1] = lm[:, 1] / LANDMARK_SIZE * crop_h + cy1

    return lm_mapped, float(identity_1[0][0]), float(identity_2[0][0]), identity.shape

print(f"[test_landmarks] watching {CAMERA_URL}")
print("[test_landmarks] show your hand...")

while True:
    frame = get_frame()
    if frame is None:
        print(f"[{time.strftime('%H:%M:%S')}] no frame")
        time.sleep(1)
        continue

    box, hand_conf = detect_best_hand(frame)

    if box is None:
        print(f"[{time.strftime('%H:%M:%S')}] no hand")
        time.sleep(0.5)
        continue

    landmarks, id1, id2, id_shape = run_landmarks(frame, box)

    print(f"[{time.strftime('%H:%M:%S')}] hand conf={hand_conf:.2f} | "
          f"Identity shape={id_shape} | "
          f"Identity_1={id1:.4f} | "
          f"Identity_2={id2:.4f}")

    # Draw 21 points on debug image
    debug = frame.copy()

    # Hand box
    x1, y1, x2, y2 = box
    cv2.rectangle(debug, (x1, y1), (x2, y2), (0, 255, 255), 2)

    # 21 landmark dots
    if landmarks is not None:
        for i, (x, y, z) in enumerate(landmarks):
            cv2.circle(debug, (int(x), int(y)), 4, (0, 200, 255), -1)
            cv2.putText(debug, str(i), (int(x)+5, int(y)-5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.3, (255, 255, 255), 1)

    cv2.imwrite("debug_landmarks.jpg", debug)
    time.sleep(0.5)