# Third-party components

YardGuard itself is licensed under the GNU AGPL-3.0 (see [LICENSE](LICENSE)). It does not ship
these components in this repository: `setup\bootstrap.ps1` downloads them from their official
sources on first run (exact versions in `requirements.txt`, `setup\torch-*.txt` and
`setup\versions.json`), and the AI models download when they are first used. Each keeps its own
licence; the main ones are listed below. Licence texts are installed alongside each package
(for Python packages in `.venv\Lib\site-packages\<package>-<version>.dist-info\`).

## Installed by the first-run setup

| Component | Used for | Licence |
|---|---|---|
| [Python](https://www.python.org/) 3.12 (python.org NuGet build) | Runs YardGuard | PSF License 2.0 |
| [Ultralytics YOLO](https://github.com/ultralytics/ultralytics) | Person and vehicle detection, tracking | AGPL-3.0 |
| [PyTorch](https://pytorch.org/) and torchvision | Runs the AI models | BSD-3-Clause |
| [OpenCV](https://opencv.org/) (opencv-python) | Image processing, live view | Apache-2.0 (the wheel bundles FFmpeg under LGPL-2.1) |
| [NumPy](https://numpy.org/) | Numbers and images | BSD-3-Clause |
| [Qt](https://www.qt.io/) through [PySide6](https://doc.qt.io/qtforpython/) | The desktop window | LGPL-3.0 |
| [Pillow](https://python-pillow.org/) | Pictures | MIT-CMU (HPND) |
| [lap](https://github.com/gatagat/lap) | Object tracking (assignment) | BSD-2-Clause |
| [Eclipse Paho MQTT](https://github.com/eclipse/paho.mqtt.python) | Home Assistant integration | EPL-2.0 / EDL-1.0 |
| [Requests](https://requests.readthedocs.io/) | Telegram, ntfy, Discord, webhooks | Apache-2.0 |
| [segno](https://github.com/heuer/segno) | QR codes for phone pairing | BSD-3-Clause |
| [psutil](https://github.com/giampaolo/psutil) | Process and memory checks | BSD-3-Clause |
| [Matplotlib](https://matplotlib.org/), [Polars](https://pola.rs/) and other Ultralytics dependencies | Required by Ultralytics | Matplotlib licence (PSF-based), MIT and others (see each package) |
| [FFmpeg](https://ffmpeg.org/) 9.0.2, [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) full build | Camera video decoding, incident clips | GPL-3.0 (this build includes GPL libraries); installed as separate programs in `tools\ffmpeg` with its licence file |

## AI models (downloaded when first used)

| Model | Used for | Licence |
|---|---|---|
| [YOLO11](https://docs.ultralytics.com/models/yolo11/) n / s / m / l / x by Ultralytics | Main detector | AGPL-3.0 |
| [License plate detector](https://huggingface.co/morsetechlab/yolov11-license-plate-detection) (YOLO11 fine-tune by morsetechlab) | Licence plates add-on: finding plates | AGPL-3.0 |
| [PARSeq](https://github.com/baudm/parseq) by Darwin Bautista and Rowel Atienza | Licence plates add-on: reading the text | Apache-2.0 |
| [LocateAnything-3B](https://huggingface.co/nvidia/LocateAnything-3B) by NVIDIA | AI brain add-on | **NVIDIA licence: non-commercial use only.** Read it before installing; do not use the AI brain commercially. |

The AI brain add-on runs in its own environment (`.venv-brain`) with
[Transformers](https://github.com/huggingface/transformers), Tokenizers, PEFT, Accelerate,
Safetensors and huggingface_hub (all Apache-2.0).

## Optional programs you install yourself

| Program | Used for | Licence |
|---|---|---|
| [Tailscale](https://tailscale.com/) | Phone web app away from home | Tailscale's terms (client partly BSD-3-Clause) |

## Services

Telegram, ntfy.sh, Discord, your email provider and Hugging Face (model downloads) are
third-party services with their own terms. YardGuard only contacts the ones you set up.
