import os, sys, json, subprocess, time, random, glob
import cv2
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed

links_file, part_s, nparts_s, out_csv, sheet_prefix = sys.argv[1:6]
part, nparts = int(part_s), int(nparts_s)
all_links = json.load(open(links_file, encoding='utf-8'))
chunk = [(gi, it) for gi, it in enumerate(all_links) if gi % nparts == part]
WORK = f'work_p{part}'
os.makedirs(WORK, exist_ok=True)

# ---- face detector: mediapipe preferred, Haar cascade fallback ----
HAVE_MP = False
detector = None
haar = None
try:
    import mediapipe as mp
    detector = mp.solutions.face_detection.FaceDetection(
        model_selection=1, min_detection_confidence=0.45)
    HAVE_MP = True
    print('DETECTOR::mediapipe', flush=True)
except Exception as e:
    print('DETECTOR::haar', str(e)[:120], flush=True)
    haar = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
    haar2 = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_profileface.xml')
print(f'CHUNK::part={part} size={len(chunk)}', flush=True)


def download(gi, it):
    url = it['u']
    p = os.path.join(WORK, f'v{gi}.mp4')
    if os.path.exists(p) and os.path.getsize(p) > 10000:
        return gi, p, 'ok'
    cmd = ['yt-dlp', '--no-warnings', '--no-playlist', '--socket-timeout', '15',
           '--retries', '1', '-f', 'best[ext=mp4]/best', '-o', p, url]
    for attempt in range(2):
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=45)
            if os.path.exists(p) and os.path.getsize(p) > 10000:
                return gi, p, 'ok'
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.6 + random.random() * 1.4)
    return gi, None, 'download_failed'


def duration(p):
    try:
        return float(subprocess.check_output(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'csv=p=0', p], text=True, timeout=10).strip())
    except Exception:
        return 0.0


def extract(gi, p):
    fd = os.path.join(WORK, f'f{gi}')
    os.makedirs(fd, exist_ok=True)
    for f in glob.glob(os.path.join(fd, '*.jpg')):
        os.remove(f)
    d = duration(p)
    if d <= 0:
        return [], 0.0
    fps = 6.0 / d
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', p, '-vf',
           f'fps={fps},scale=256:-1', '-q:v', '5', '-y', os.path.join(fd, 'x%02d.jpg')]
    try:
        subprocess.run(cmd, capture_output=True, timeout=45)
    except Exception:
        pass
    return sorted(glob.glob(os.path.join(fd, '*.jpg')))[:6], d


def frame_face(path):
    img = cv2.imread(path)
    if img is None:
        return 0, 0.0, 0.0
    h, w = img.shape[:2]
    best_area, best_score = 0.0, 0.0
    hit = 0
    if HAVE_MP:
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        res = detector.process(rgb)
        if res and res.detections:
            for d in res.detections:
                s = float(d.score[0])
                bb = d.location_data.relative_bounding_box
                a = float(bb.width) * float(bb.height)
                if s >= 0.5 and a >= 0.004:
                    hit += 1
                    best_area = max(best_area, a)
                    best_score = max(best_score, s)
    else:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        for c in (haar, haar2):
            try:
                faces = c.detectMultiScale(gray, 1.1, 4, minSize=(24, 24))
                for (x, y, fw, fh) in faces:
                    a = (fw * fh) / float(w * h)
                    if a >= 0.006:
                        hit += 1
                        best_area = max(best_area, a)
                        best_score = 1.0
            except Exception:
                pass
    return hit, best_area, best_score


CELL_W, CELL_H = 120, 176
COLS, ROWS = 10, 8
PER_SHEET = COLS * ROWS


def make_cell(img, gi, verdict, status):
    canvas = np.full((CELL_H, CELL_W, 3), 232, np.uint8)
    if img is not None:
        h, w = img.shape[:2]
        scale = min(CELL_W / w, (CELL_H - 20) / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        small = cv2.resize(img, (nw, nh))
        x = (CELL_W - nw) // 2
        y = 20 + (CELL_H - 20 - nh) // 2
        canvas[y:y + nh, x:x + nw] = small
    cv2.rectangle(canvas, (0, 0), (CELL_W, 18), (28, 28, 28), -1)
    if verdict == 'face':
        color, tag = (80, 230, 80), 'F'
    elif status != 'ok':
        color, tag = (80, 80, 230), 'X'
    else:
        color, tag = (200, 200, 200), '-'
    cv2.putText(canvas, f'#{gi}{tag}', (3, 13), cv2.FONT_HERSHEY_SIMPLEX,
                0.36, color, 1, cv2.LINE_AA)
    return canvas


def flush_sheet(cells, idxs, k):
    while len(cells) < PER_SHEET:
        cells.append(np.full((CELL_H, CELL_W, 3), 245, np.uint8))
    rows = [np.hstack(cells[r * COLS:(r + 1) * COLS]) for r in range(ROWS)]
    sheet = np.vstack(rows)
    fn = f'{sheet_prefix}_p{part}_{k:02d}.jpg'
    cv2.imwrite(fn, sheet, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    print(f'SHEETMAP::part={part}|k={k}|{"|".join(str(x) for x in idxs)}', flush=True)
    return fn


# ---- download concurrently ----
dl = {}
with ThreadPoolExecutor(max_workers=6) as ex:
    futs = [ex.submit(download, gi, it) for gi, it in chunk]
    for i, f in enumerate(as_completed(futs)):
        gi, p, st = f.result()
        dl[gi] = (p, st)
        if (i + 1) % 25 == 0:
            print(f'DL::part={part} {i+1}/{len(chunk)}', flush=True)

# ---- detect + build sheets ----
results = []
cells, idxs = [], []
k = 0
for gi, it in chunk:
    p, st = dl.get(gi, (None, 'missing'))
    rep, verdict, ff, ba, bs, dur = None, 'unknown', 0, 0.0, 0.0, 0.0
    if st == 'ok':
        frames, dur = extract(gi, p)
        best_metric, rep_path = -1.0, None
        for fp in frames:
            hit, a, s = frame_face(fp)
            if hit:
                ff += 1
                ba, bs = max(ba, a), max(bs, s)
            m = (s * a * 1000) if hit else 0.0
            if m > best_metric:
                best_metric, rep_path = m, fp
        if not frames:
            st = 'no_frames'
        else:
            verdict = 'face' if (ff >= 1 and ba >= 0.006) else 'noface'
            pick = rep_path if (rep_path and best_metric > 0) else frames[len(frames) // 2]
            rep = cv2.imread(pick)
    try:
        if p and os.path.exists(p):
            os.remove(p)
    except Exception:
        pass
    results.append((part, gi, it['k'], it.get('c', ''), st, f'{dur:.1f}', ff,
                    f'{ba:.4f}', f'{bs:.2f}', verdict))
    cells.append(make_cell(rep, gi, verdict, st))
    idxs.append(gi)
    if len(cells) == PER_SHEET:
        flush_sheet(cells, idxs, k)
        cells, idxs = [], []
        k += 1
if cells:
    flush_sheet(cells, idxs, k)

with open(out_csv, 'w', encoding='utf-8') as f:
    for r in results:
        f.write('RES::' + '|'.join(str(x) for x in r) + '\n')
print(f'DONE::part={part} results={len(results)} sheets={k+1 if cells or k else k}', flush=True)
