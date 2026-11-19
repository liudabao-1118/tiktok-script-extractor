import os, sys, json, subprocess, time, random, glob
import cv2
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed

links_file, part_s, nparts_s, out_csv = sys.argv[1:5]
part, nparts = int(part_s), int(nparts_s)
all_links = json.load(open(links_file, encoding='utf-8'))
chunk = [(gi, it) for gi, it in enumerate(all_links) if gi % nparts == part]
WORK = f'w2_p{part}'
os.makedirs(WORK, exist_ok=True)

DET = None
MODE = 'none'
try:
    DET = cv2.FaceDetectorYN.create('face_detection_yunet_2023mar.onnx', '', (320, 320),
                                    score_threshold=0.6, nms_threshold=0.3, top_k=5000)
    MODE = 'yunet'
except Exception as e:
    print('YUNETFAIL::' + str(e)[:180], flush=True)
    DET = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
    MODE = 'haar'
print(f'DETECTOR3::{MODE}', flush=True)
print(f'CHUNK3::part={part} size={len(chunk)}', flush=True)


def download(gi, it):
    p = os.path.join(WORK, f'v{gi}.mp4')
    if os.path.exists(p) and os.path.getsize(p) > 10000:
        return gi, p, 'ok'
    cmd = ['yt-dlp', '--no-warnings', '--no-playlist', '--socket-timeout', '15',
           '--retries', '2', '-f', 'best[ext=mp4]/best', '-o', p, it['u']]
    for attempt in range(3):
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=50)
            if os.path.exists(p) and os.path.getsize(p) > 10000:
                return gi, p, 'ok'
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.5 + random.random() * 1.2)
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
    fps = 8.0 / d
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', p, '-vf',
           f'fps={fps},scale=256:-1', '-q:v', '5', '-y', os.path.join(fd, 'x%02d.jpg')]
    try:
        subprocess.run(cmd, capture_output=True, timeout=45)
    except Exception:
        pass
    return sorted(glob.glob(os.path.join(fd, '*.jpg')))[:8], d


def frame_face(path):
    img = cv2.imread(path)
    if img is None:
        return 0, 0.0, 0.0
    h, w = img.shape[:2]
    hit, ba, bs = 0, 0.0, 0.0
    if MODE == 'yunet':
        try:
            DET.setInputSize((w, h))
            _, faces = DET.detect(img)
            if faces is not None:
                for f in faces:
                    s = float(f[-1])
                    a = float(f[2]) * float(f[3]) / float(w * h)
                    if s >= 0.6 and a >= 0.002:
                        hit += 1
                        ba, bs = max(ba, a), max(bs, s)
        except Exception:
            pass
    else:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        try:
            for (x, y, fw, fh) in DET.detectMultiScale(gray, 1.1, 4, minSize=(24, 24)):
                a = (fw * fh) / float(w * h)
                if a >= 0.006:
                    hit += 1
                    ba, bs = max(ba, a), 1.0
        except Exception:
            pass
    return hit, ba, bs


dl = {}
with ThreadPoolExecutor(max_workers=8) as ex:
    futs = [ex.submit(download, gi, it) for gi, it in chunk]
    for i, f in enumerate(as_completed(futs)):
        gi, p, st = f.result()
        dl[gi] = (p, st)
        if (i + 1) % 40 == 0:
            print(f'DL3::part={part} {i+1}/{len(chunk)}', flush=True)

results = []
for gi, it in chunk:
    p, st = dl.get(gi, (None, 'missing'))
    ff, ba, bs, dur, verdict = 0, 0.0, 0.0, 0.0, 'unknown'
    if st == 'ok':
        frames, dur = extract(gi, p)
        for fp in frames:
            hit, a, s = frame_face(fp)
            if hit:
                ff += 1
                ba, bs = max(ba, a), max(bs, s)
        if not frames:
            st = 'no_frames'
        else:
            verdict = 'face' if ff >= 1 else 'noface'
    try:
        if p and os.path.exists(p):
            os.remove(p)
    except Exception:
        pass
    results.append((part, gi, it['k'], it.get('c', ''), st, f'{dur:.1f}',
                    ff, f'{ba:.4f}', f'{bs:.2f}', verdict))

with open(out_csv, 'w', encoding='utf-8') as f:
    for r in results:
        f.write('RES3::' + '|'.join(str(x) for x in r) + '\n')
print(f'DONE3::part={part} results={len(results)}', flush=True)
