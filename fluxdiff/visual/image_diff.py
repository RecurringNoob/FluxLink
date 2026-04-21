# fluxdiff/visual/image_diff.py
import cv2
import numpy as np


def generate_visual_diff(before_image, after_image, output_path):
    """
    Generate a visual diff overlay between two PCB images.

    Removed areas (present in before, gone in after) → red
    Added areas   (absent in before, present in after) → green

    FIX (NumPy >=1.24 compatibility): np.full_like(arr, [0,0,255]) with a list
    fill value raises ValueError in NumPy >=1.24 when the fill shape does not
    exactly match the array shape. Replaced with explicit zero-allocation +
    slice assignment, which is safe across all NumPy versions.
    """
    before = cv2.imread(before_image)
    after = cv2.imread(after_image)

    if before is None:
        raise FileNotFoundError(f"Could not load before image: {before_image}")
    if after is None:
        raise FileNotFoundError(f"Could not load after image: {after_image}")

    # Normalise sizes
    if before.shape != after.shape:
        h = min(before.shape[0], after.shape[0])
        w = min(before.shape[1], after.shape[1])
        before = cv2.resize(before, (w, h))
        after = cv2.resize(after, (w, h))

    before_gray = cv2.cvtColor(before, cv2.COLOR_BGR2GRAY)
    after_gray = cv2.cvtColor(after, cv2.COLOR_BGR2GRAY)

    diff = cv2.absdiff(before_gray, after_gray)
    _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)

    removed_mask = cv2.bitwise_and(thresh, cv2.compare(before_gray, after_gray, cv2.CMP_GT))
    added_mask = cv2.bitwise_and(thresh, cv2.compare(after_gray, before_gray, cv2.CMP_GT))

    context = cv2.addWeighted(before, 0.5, after, 0.5, 0)
    overlay = context.copy()

    overlay[removed_mask > 0] = [0, 0, 255]   # red  = removed
    overlay[added_mask > 0] = [0, 255, 0]     # green = added

    blended = cv2.addWeighted(context, 0.4, overlay, 0.6, 0)

    kernel = np.ones((3, 3), np.uint8)
    removed_dilated = cv2.dilate(removed_mask, kernel, iterations=1)
    added_dilated = cv2.dilate(added_mask, kernel, iterations=1)

    # FIX: build solid-color planes explicitly instead of np.full_like with a
    # list fill value, which is non-portable across NumPy versions.
    red_plane = np.zeros_like(blended)
    red_plane[:, :] = [0, 0, 255]

    green_plane = np.zeros_like(blended)
    green_plane[:, :] = [0, 255, 0]

    blended[removed_dilated > 0] = cv2.addWeighted(
        blended, 0.3, red_plane, 0.7, 0
    )[removed_dilated > 0]

    blended[added_dilated > 0] = cv2.addWeighted(
        blended, 0.3, green_plane, 0.7, 0
    )[added_dilated > 0]

    result = cv2.imwrite(output_path, blended)
    if not result:
        raise RuntimeError(f"cv2.imwrite failed — could not write to {output_path}")