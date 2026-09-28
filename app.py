"""
app.py - Modell-Downloader fuer das SeriesAI ComfyUI-Video-Backend (LTX-2.3 GGUF).

Laedt alle fuer das LTX-2.3-Template benoetigten Modelldateien direkt von
HuggingFace in die richtigen ComfyUI-Modellordner (sibling ComfyUI_windows_portable).

Eigenschaften:
- Resumable: bereits vollstaendig heruntergeladene Dateien werden uebersprungen
  (Groessenvergleich gegen die von HuggingFace gemeldete Content-Length).
- Fortsetzbare Teil-Downloads ueber HTTP Range-Requests (bricht der Download ab,
  wird beim naechsten Lauf an der Abbruchstelle weitergemacht).
- Keine Zerstoerung vorhandener Daten: schreibt ausschliesslich in
  ComfyUI/models/<kategorie>/, ruehrt Series/ oder die SQLite-DB nicht an.
- Nutzt nur die Standardbibliothek (kein huggingface_hub noetig), damit es auch
  in der frischen venv von start.py sofort laeuft.

Aufruf:
    python app.py                # alles herunterladen
    python app.py --only unet    # nur eine Kategorie
    python app.py --list         # nur anzeigen, was fehlt/vorhanden ist
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
COMFYUI_MODELS = (PROJECT_ROOT / ".." / "ComfyUI_windows_portable" / "ComfyUI" / "models").resolve()

# (huggingface_repo, datei_im_repo, ziel_unterordner, ziel_dateiname)
MODELS = [
    ("unsloth/LTX-2.3-GGUF", "ltx-2.3-22b-dev-Q4_K_M.gguf", "unet", "ltx-2.3-22b-dev-Q4_K_M.gguf"),
    ("unsloth/LTX-2.3-GGUF", "vae/ltx-2.3-22b-dev_video_vae.safetensors", "vae", "ltx-2.3-22b-dev_video_vae.safetensors"),
    ("unsloth/LTX-2.3-GGUF", "vae/ltx-2.3-22b-dev_audio_vae.safetensors", "vae", "ltx-2.3-22b-dev_audio_vae.safetensors"),
    ("unsloth/LTX-2.3-GGUF", "text_encoders/ltx-2.3-22b-dev_embeddings_connectors.safetensors", "text_encoders", "ltx-2.3-22b-dev_embeddings_connectors.safetensors"),
    ("unsloth/gemma-3-12b-it-qat-GGUF", "gemma-3-12b-it-qat-UD-Q4_K_XL.gguf", "text_encoders", "gemma-3-12b-it-qat-UD-Q4_K_XL.gguf"),
    ("unsloth/gemma-3-12b-it-qat-GGUF", "mmproj-BF16.gguf", "text_encoders", "mmproj-BF16.gguf"),
    ("Lightricks/LTX-2.3", "ltx-2.3-22b-distilled-lora-384.safetensors", "loras", "ltx-2.3-22b-distilled-lora-384.safetensors"),
    ("Lightricks/LTX-2.3", "ltx-2.3-spatial-upscaler-x2-1.1.safetensors", "latent_upscale_models", "ltx-2.3-spatial-upscaler-x2-1.1.safetensors"),
]


def hf_url(repo: str, path: str) -> str:
    return f"https://huggingface.co/{repo}/resolve/main/{path}"


def remote_size(url: str) -> int | None:
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            length = resp.headers.get("Content-Length")
            return int(length) if length else None
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
            return remote_size(e.headers["Location"])
        print(f"  ! HEAD-Request fehlgeschlagen ({e.code}): {url}")
        return None
    except Exception as e:
        print(f"  ! HEAD-Request fehlgeschlagen ({e}): {url}")
        return None


def download(url: str, dest: Path, expected_size: int | None) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    existing = tmp.stat().st_size if tmp.exists() else 0
    if dest.exists() and expected_size and dest.stat().st_size == expected_size:
        print(f"  = bereits vollstaendig vorhanden: {dest.name}")
        return True

    headers = {}
    mode = "wb"
    if existing:
        headers["Range"] = f"bytes={existing}-"
        mode = "ab"
        print(f"  > setze fort bei {existing/1e9:.2f} GB: {dest.name}")
    else:
        print(f"  > starte Download: {dest.name}")

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = expected_size or (existing + int(resp.headers.get("Content-Length", 0)))
            written = existing
            last_print = time.time()
            with open(tmp, mode) as f:
                while True:
                    chunk = resp.read(1024 * 1024 * 8)
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                    if time.time() - last_print > 5:
                        pct = (written / total * 100) if total else 0
                        print(f"    {dest.name}: {written/1e9:.2f} / {total/1e9:.2f} GB ({pct:.1f}%)")
                        last_print = time.time()
    except Exception as e:
        print(f"  ! Download unterbrochen ({e}) - beim naechsten Lauf wird fortgesetzt: {dest.name}")
        return False

    tmp.rename(dest)
    print(f"  OK {dest.name} fertig ({dest.stat().st_size/1e9:.2f} GB)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="Nur Modelle in diesen Zielordner laden (z.B. unet, vae, text_encoders, loras, latent_upscale_models)")
    parser.add_argument("--list", action="store_true", help="Nur Status anzeigen, nichts herunterladen")
    args = parser.parse_args()

    if not COMFYUI_MODELS.exists():
        print(f"FEHLER: ComfyUI-Modellordner nicht gefunden unter {COMFYUI_MODELS}")
        print("Erwartet wird ComfyUI_windows_portable als Sibling-Ordner von SeriesAI (siehe config.yaml).")
        return 1

    print(f"ComfyUI-Modellordner: {COMFYUI_MODELS}\n")

    ok_all = True
    for repo, path_in_repo, subdir, filename in MODELS:
        if args.only and args.only != subdir:
            continue
        url = hf_url(repo, path_in_repo) + "?download=true"
        dest = COMFYUI_MODELS / subdir / filename
        if args.list:
            status = "vorhanden" if dest.exists() else "fehlt"
            print(f"[{status:9}] {subdir}/{filename}")
            continue
        size = remote_size(hf_url(repo, path_in_repo))
        if not download(url, dest, size):
            ok_all = False

    if not args.list:
        print("\nAlle Modelle bearbeitet." if ok_all else "\nEinige Downloads sind unvollstaendig - Skript erneut ausfuehren zum Fortsetzen.")
    return 0 if ok_all else 2


if __name__ == "__main__":
    raise SystemExit(main())
