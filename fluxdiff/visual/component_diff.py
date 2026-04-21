# fluxdiff/visual/component_diff.py
import cv2
import numpy as np
import re

# F4 FIX: import PIXELS_PER_MM from the shared constants module.
# Previously this was a hardcoded inline constant with a comment referencing
# scale=4.0 in kicad_export.py — if one changed without the other, component
# markers would silently appear at wrong board positions with no error.
from fluxdiff.visual.constants import PIXELS_PER_MM


def generate_component_visual_diff(before_png, after_png, before_components, after_components, output_path):
    before_img = cv2.imread(before_png)
    after_img = cv2.imread(after_png)

    if before_img is None or after_img is None:
        raise FileNotFoundError("Could not load PCB images.")

    # Ensure sizes match
    h, w = after_img.shape[:2]
    if before_img.shape[:2] != (h, w):
        before_img = cv2.resize(before_img, (w, h))

    # 1. Create the faded background
    gray = cv2.cvtColor(after_img, cv2.COLOR_BGR2GRAY)
    gray_bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    white_canvas = np.full((h, w, 3), 255, dtype=np.uint8)
    canvas = cv2.addWeighted(gray_bgr, 0.25, white_canvas, 0.75, 0)

    # 2. Extract EXACT component shapes using pixel differences
    before_gray = cv2.cvtColor(before_img, cv2.COLOR_BGR2GRAY)
    diff = cv2.absdiff(before_gray, gray)
    _, diff_mask = cv2.threshold(diff, 15, 255, cv2.THRESH_BINARY)

    kernel = np.ones((3, 3), np.uint8)
    diff_mask = cv2.dilate(diff_mask, kernel, iterations=1)

    removed_mask = cv2.bitwise_and(diff_mask, cv2.compare(before_gray, gray, cv2.CMP_GT))
    added_mask = cv2.bitwise_and(diff_mask, cv2.compare(gray, before_gray, cv2.CMP_GT))

    canvas[removed_mask > 0] = [0, 0, 255]
    canvas[added_mask > 0] = [0, 255, 0]

    # 3. Component Matching
    ref_pattern = re.compile(r"REF\*\*")
    def is_valid_ref(ref): return bool(ref) and not ref_pattern.fullmatch(ref)
    def get_key(c): return getattr(c, "uuid", "") or c.ref

    before_dict = {get_key(c): c for c in before_components if hasattr(c, "ref") and is_valid_ref(c.ref)}
    after_dict = {get_key(c): c for c in after_components if hasattr(c, "ref") and is_valid_ref(c.ref)}

    # 4. Coordinate labels
    font_scale = max(0.4, w / 3000)
    thickness = max(1, int(w / 2000))

    def get_px(comp):
        # F4 FIX: PIXELS_PER_MM is now imported from constants, not hardcoded
        return int(round(comp.x * PIXELS_PER_MM)), int(round(comp.y * PIXELS_PER_MM))

    def draw_marker(px, py, label, color):
        cv2.drawMarker(canvas, (px, py), color, markerType=cv2.MARKER_CROSS, markerSize=16, thickness=2)
        text_size = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)[0]
        tx, ty = px + 10, py - 10
        cv2.rectangle(canvas, (tx - 2, ty - text_size[1] - 4), (tx + text_size[0] + 2, ty + 4), (255,255,255), -1)
        cv2.rectangle(canvas, (tx - 2, ty - text_size[1] - 4), (tx + text_size[0] + 2, ty + 4), color, 1)
        cv2.putText(canvas, label, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, font_scale, color, thickness, cv2.LINE_AA)

    # A. ADDED
    for key, ac in after_dict.items():
        if key not in before_dict:
            px, py = get_px(ac)
            draw_marker(px, py, f"ADDED: {ac.ref}", (0, 180, 0))

    # B. REMOVED
    for key, bc in before_dict.items():
        if key not in after_dict:
            px, py = get_px(bc)
            draw_marker(px, py, f"REMOVED: {bc.ref}", (0, 0, 255))

    # C. SHIFTED
    for key in set(before_dict) & set(after_dict):
        bc, ac = before_dict[key], after_dict[key]
        if ((ac.x - bc.x)**2 + (ac.y - bc.y)**2)**0.5 > 0.1:
            px_old, py_old = get_px(bc)
            px_new, py_new = get_px(ac)

            cv2.arrowedLine(canvas, (px_old, py_old), (px_new, py_new),
                            (0, 180, 255), thickness=3, tipLength=0.1, line_type=cv2.LINE_AA)

            draw_marker(px_old, py_old, f"OLD: {bc.ref}", (0, 150, 255))
            draw_marker(px_new, py_new, f"NEW: {ac.ref}", (0, 150, 255))

    cv2.imwrite(output_path, canvas)