"""Offline, non-Django experiment for visible dark-region highlighting on organizer samples.

This does not use image labels to choose regions and does not claim ground-truth localization.
"""

import csv
import json
import time
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageOps


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / 'Samples'
OUT = Path(__file__).resolve().parent / 'localization_poc'


def locate(source):
    """Find large, unusually dark connected regions inside the known product field of view."""
    image = ImageOps.exif_transpose(source).convert('RGB')
    gray = np.asarray(image.convert('L'), dtype=np.float32)
    h, w = gray.shape
    # Exclude the dark background/frame.  The organiser samples use fixed central plate framing.
    y0, y1, x0, x1 = round(h * .09), round(h * .91), round(w * .09), round(w * .91)
    plate = gray[y0:y1, x0:x1]
    # Row-median normalisation suppresses horizontal illumination/banding before thresholding.
    normalized = plate - np.median(plate, axis=1, keepdims=True)
    mask = normalized < -22
    visited = np.zeros(mask.shape, dtype=bool)
    regions = []
    for yy, xx in np.argwhere(mask):
        if visited[yy, xx]:
            continue
        queue, points = deque([(int(yy), int(xx))]), []
        visited[yy, xx] = True
        while queue:
            cy, cx = queue.popleft()
            points.append((cy, cx))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < mask.shape[0] and 0 <= nx < mask.shape[1] and mask[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        queue.append((ny, nx))
        if len(points) < max(8, round(w * h * .00008)):
            continue
        ys, xs = zip(*points)
        min_y, max_y, min_x, max_x = min(ys), max(ys), min(xs), max(xs)
        width, height = max_x - min_x + 1, max_y - min_y + 1
        aspect = min(width, height) / max(width, height)
        # Defect candidates are compact; reject long illumination artefacts.
        if aspect < .35:
            continue
        cx, cy = x0 + (min_x + max_x) / 2, y0 + (min_y + max_y) / 2
        radius = max(width, height) / 2 + max(2, round(min(w, h) * .01))
        regions.append((round(cx, 1), round(cy, 1), round(radius, 1)))
    return image, regions


def annotate(image, regions):
    copy = image.copy()
    draw = ImageDraw.Draw(copy)
    line = max(2, round(min(copy.size) * .012))
    for x, y, radius in regions:
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline='#d92d20', width=line)
    return copy


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with (SAMPLES / 'labels.csv').open(newline='', encoding='utf-8-sig') as handle:
        rows = list(csv.DictReader(handle))
    measurements, output_rows = [], []
    for row in rows:
        path = SAMPLES / row['relative_path']
        started = time.perf_counter()
        with Image.open(path) as opened:
            image, regions = locate(opened)
        elapsed = (time.perf_counter() - started) * 1000
        measurements.append(elapsed)
        annotated = annotate(image, regions)
        annotated.save(OUT / f"{path.stem}_annotated.png", format='PNG')
        # Brightness perturbations are a stability check only, not an accuracy evaluation.
        stability = []
        for factor in (.8, 1.2):
            with Image.open(path) as opened:
                _image, altered = locate(ImageEnhance.Brightness(opened).enhance(factor))
            stability.append(len(altered) == len(regions))
        output_rows.append({
            'image_id': row['image_id'], 'filename': path.name, 'label': row['label'],
            'regions': len(regions), 'processing_ms': round(elapsed, 3), 'brightness_count_stable': all(stability),
        })
    normal = [r for r in output_rows if r['label'] == 'Normal']
    defective = [r for r in output_rows if r['label'] == 'Defective']
    report = {
        'method': 'row-median illumination normalization, dark-region thresholding, connected components, compact-shape filtering',
        'coordinate_annotations_available': False,
        'defective_images_with_regions': sum(r['regions'] > 0 for r in defective),
        'defective_images_without_regions': sum(r['regions'] == 0 for r in defective),
        'normal_images_with_regions': sum(r['regions'] > 0 for r in normal),
        'normal_images_without_regions': sum(r['regions'] == 0 for r in normal),
        'defective_images_with_multiple_regions': sum(r['regions'] > 1 for r in defective),
        'brightness_count_stable': sum(r['brightness_count_stable'] for r in output_rows),
        'mean_processing_ms': round(float(np.mean(measurements)), 3),
        'max_processing_ms': round(float(np.max(measurements)), 3),
        'rows': output_rows,
        'limitations': 'No bounding boxes or masks are supplied. Region counts are not localization ground truth; no IoU, pixel accuracy, or localization recall is reported.',
    }
    (OUT / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key not in ('rows',)}, indent=2))


if __name__ == '__main__':
    main()
