"""Build the self-sufficient GLiNER model pack for the offline bundle.

Run ONCE on a CONNECTED build machine, with the `ml` extra installed:

    uv sync --extra ml
    uv run python scripts/fetch_gliner_model.py vendor/gliner-model

The output folder is what `build_offline_bundle.ps1 -WithML` copies into the
bundle, and what `launch.bat` points `ANONYMIZER_GLINER_MODEL` at.

WHY THIS SCRIPT EXISTS -- three things the pack needs that a plain
`snapshot_download` of the model repo does NOT give you. Each one fails only on a
machine with no network, i.e. never on the build box and always on the target:

1. NO TOKENIZER. `urchade/gliner_multi-v2.1` ships exactly three files --
   gliner_config.json, model.safetensors, pytorch_model.bin. The tokenizer comes
   from the base encoder named in gliner_config.json (`microsoft/mdeberta-v3-base`),
   so `from_pretrained(load_tokenizer=True)` silently resolves it from the Hub.
2. NO `tokenizer_class`. mdeberta's tokenizer_config.json has two keys and names
   no tokenizer class, and the pack has no config.json for AutoTokenizer to infer
   the model type from -- so transformers 5.x refuses to build it. We write the
   class in, then re-serialize a FAST tokenizer.json so the target does no
   SentencePiece conversion at runtime at all.
3. NO ENCODER CONFIG. gliner's Encoder calls
   `AutoConfig.from_pretrained("microsoft/mdeberta-v3-base")` -- a hub id, not a
   path. It forwards `cache_dir`, so we pre-populate a tiny pack-local HF cache
   (~4 MB, config + tokenizer only, never the base encoder's weights) that
   `load_gliner_backend` passes back in. This keeps the pack RELOCATABLE: the
   bundle is copied to an arbitrary folder off a network share, so nothing may
   depend on an absolute path baked in at build time.

Verify the result the only way that means anything -- with the network cut:

    set HF_HUB_OFFLINE=1
    uv run python -c "from anonymizer.gliner_recognizer import load_gliner_backend as L; \
        print(L({'model_path': r'vendor\\gliner-model'}).predict('Ada Lovelace arbeitet bei DeepL Pro.', ['person','tool']))"
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

MODEL_REPO = "urchade/gliner_multi-v2.1"
ENCODER_REPO = "microsoft/mdeberta-v3-base"
# The base encoder's tokenizer class. mdeberta-v3 is SentencePiece-based; its own
# tokenizer_config.json omits this, which is what breaks AutoTokenizer.
TOKENIZER_CLASS = "DebertaV2Tokenizer"
PACK_CACHE_DIRNAME = "hf-cache"  # must match gliner_recognizer.PACK_CACHE_DIRNAME


def main(dest: Path) -> int:
    from huggingface_hub import hf_hub_download, snapshot_download

    dest.mkdir(parents=True, exist_ok=True)

    print(f"[1/4] weights + config: {MODEL_REPO}")
    snapshot_download(
        repo_id=MODEL_REPO,
        local_dir=str(dest),
        # pytorch_model.bin is a duplicate of model.safetensors -- 1.1 GB of nothing.
        allow_patterns=["gliner_config.json", "model.safetensors"],
    )

    print(f"[2/4] tokenizer source files: {ENCODER_REPO}")
    for name in ("tokenizer_config.json", "spm.model"):
        shutil.copy2(hf_hub_download(repo_id=ENCODER_REPO, filename=name), dest / name)
        print(f"      {name}")

    print(f"[3/4] writing tokenizer_class + serializing a fast tokenizer.json")
    cfg_path = dest / "tokenizer_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg.setdefault("tokenizer_class", TOKENIZER_CLASS)
    cfg_path.write_text(json.dumps(cfg, indent=1), encoding="utf-8")

    from transformers import AutoTokenizer

    AutoTokenizer.from_pretrained(str(dest), local_files_only=True).save_pretrained(str(dest))
    if not (dest / "tokenizer.json").exists():
        print("      ERROR: no tokenizer.json was produced -- the pack would need "
              "sentencepiece at runtime and may fail on the target.")
        return 1

    print(f"[4/4] pack-local HF cache for the base encoder config")
    snapshot_download(
        repo_id=ENCODER_REPO,
        cache_dir=str(dest / PACK_CACHE_DIRNAME),
        allow_patterns=["config.json", "tokenizer_config.json", "spm.model"],
    )

    # snapshot_download(local_dir=...) leaves a download-metadata folder behind.
    # It is pure build residue and must not be copied into the bundle.
    shutil.rmtree(dest / ".cache", ignore_errors=True)

    total = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    print(f"\npack ready: {dest}  ({total / 1e6:.0f} MB)")
    for f in sorted(dest.rglob("*")):
        if f.is_file() and f.stat().st_size > 100_000:
            print(f"    {f.stat().st_size / 1e6:9.1f} MB  {f.relative_to(dest)}")
    print("\nNow verify it OFFLINE (HF_HUB_OFFLINE=1) before building the bundle -- "
          "a pack that only loads on a connected machine is not a pack.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(Path(sys.argv[1]).resolve()))
