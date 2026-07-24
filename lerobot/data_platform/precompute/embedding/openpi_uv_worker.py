from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

import numpy as np


def _write(payload: dict) -> None:
    print(json.dumps(payload, separators=(",", ":")), flush=True)


def _pool_prefix_batch(
    prefix_out: np.ndarray,
    prefix_mask: np.ndarray,
    layer_hook: str,
    prompt_len: int = 0,
    stage_tokens: int = 0,
) -> np.ndarray:
    prefix_out = np.asarray(prefix_out, dtype=np.float32)
    prefix_mask = np.asarray(prefix_mask).astype(bool)
    if prefix_out.ndim == 2:
        prefix_out = prefix_out[None, ...]
    if prefix_mask.ndim == 1:
        prefix_mask = prefix_mask[None, ...]
    if layer_hook == "vision_encoder" and prompt_len:
        end = max(prefix_out.shape[1] - prompt_len - stage_tokens, 1)
        prefix_out = prefix_out[:, :end]
        prefix_mask = prefix_mask[:, :end]
    elif layer_hook == "pi_prefix_prompt" and prompt_len:
        start = max(prefix_out.shape[1] - prompt_len - stage_tokens, 0)
        end = max(prefix_out.shape[1] - stage_tokens, start + 1)
        prefix_out = prefix_out[:, start:end]
        prefix_mask = prefix_mask[:, start:end]
    weights = prefix_mask.astype(np.float32)
    denom = np.maximum(weights.sum(axis=1, keepdims=True), 1.0)
    return ((prefix_out * weights[..., None]).sum(axis=1) / denom).astype(np.float32)


def _load_raw_payload(payload_path: Path, manifest_path: Path) -> list[dict]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    arrays = np.load(payload_path, allow_pickle=False)
    frame_manifests = manifest.get("frames") or [manifest]
    raws = []
    for frame_manifest in frame_manifests:
        raw = {}
        for key, spec in frame_manifest.items():
            if spec.get("type") == "array":
                raw[key] = arrays[spec["name"]]
            elif spec.get("type") == "scalar":
                raw[key] = spec.get("value")
        raws.append(raw)
    return raws


class OpenPIFrameEmbedder:
    def __init__(self, ckpt: Path, config: str, layer_hook: str):
        from openpi.policies import policy_config as _policy_config
        from openpi.training import config as _config

        self.layer_hook = layer_hook
        self.last_timing: dict[str, float] = {}
        train_config = _config.get_config(config)
        self.policy = _policy_config.create_trained_policy(train_config, ckpt)

    def embed(self, raw: dict) -> np.ndarray:
        return self.embed_many([raw])[0]

    def embed_many(self, raws: list[dict]) -> np.ndarray:
        start = time.perf_counter()
        raws = [dict(raw) for raw in raws]
        for raw in raws:
            raw.pop("actions", None)
        transform_start = time.perf_counter()
        batch = self._transformed_batch(raws)
        transform_s = time.perf_counter() - transform_start
        model_start = time.perf_counter()
        if getattr(self.policy, "_is_pytorch_model", False):
            vecs = self._embed_torch_batch(batch)
        else:
            vecs = self._embed_jax_batch(batch)
        model_s = time.perf_counter() - model_start
        self.last_timing = {
            "worker_transform_s": transform_s,
            "worker_model_s": model_s,
            "worker_total_s": time.perf_counter() - start,
            "worker_frames": float(len(raws)),
        }
        return vecs

    def _transformed_batch(self, raws: list[dict]):
        import jax

        transformed = []
        for raw in raws:
            inputs = self.policy._input_transform(dict(raw))
            inputs.pop("prompt_text", None)
            transformed.append(inputs)
        return jax.tree.map(lambda *xs: np.stack([np.asarray(x) for x in xs], axis=0), *transformed)

    def _embed_jax_batch(self, batch) -> np.ndarray:
        import jax
        import jax.numpy as jnp
        from openpi.models import model as _model
        from openpi.models.pi0 import make_attn_mask

        inputs = jax.tree.map(lambda x: jnp.asarray(x), batch)
        observation = _model.Observation.from_dict(inputs)
        observation = _model.preprocess_observation(None, observation, train=False)
        prefix_tokens, prefix_mask, prefix_ar_mask = self.policy._model.embed_prefix(observation)
        prefix_attn_mask = make_attn_mask(prefix_mask, prefix_ar_mask)
        positions = jnp.cumsum(prefix_mask, axis=1) - 1
        (prefix_out, _), _ = self.policy._model.PaliGemma.llm(
            [prefix_tokens, None],
            mask=prefix_attn_mask,
            positions=positions,
        )
        prompt_len = int(observation.tokenized_prompt.shape[1]) if observation.tokenized_prompt is not None else 0
        stage_tokens = 4 if getattr(self.policy._model, "use_stage_fusion", False) and observation.subtask_state is not None else 0
        return _pool_prefix_batch(np.asarray(prefix_out), np.asarray(prefix_mask), self.layer_hook, prompt_len, stage_tokens)

    def _embed_torch_batch(self, batch) -> np.ndarray:
        import jax
        import torch
        from openpi.models import model as _model
        from openpi.models_pytorch.pi0_pytorch import make_att_2d_masks

        device = self.policy._pytorch_device
        torch_inputs = jax.tree.map(lambda x: torch.from_numpy(np.asarray(x)).to(device), batch)
        observation = _model.Observation.from_dict(torch_inputs)
        model = self.policy._model
        with torch.no_grad():
            images, img_masks, lang_tokens, lang_masks, _state = model._preprocess_observation(observation, train=False)
            prefix_embs, prefix_pad_masks, prefix_att_masks = model.embed_prefix(images, img_masks, lang_tokens, lang_masks)
            att_2d = make_att_2d_masks(prefix_pad_masks, prefix_att_masks)
            att_4d = model._prepare_attention_masks_4d(att_2d)
            position_ids = torch.cumsum(prefix_pad_masks, dim=1) - 1
            model.paligemma_with_expert.paligemma.language_model.config._attn_implementation = "eager"
            outputs_embeds, _ = model.paligemma_with_expert.forward(
                attention_mask=att_4d,
                position_ids=position_ids,
                past_key_values=None,
                inputs_embeds=[prefix_embs, None],
                use_cache=True,
            )
            prefix_out = outputs_embeds[0].detach().float().cpu().numpy()
            prefix_mask = prefix_pad_masks.detach().cpu().numpy()
        prompt_len = int(lang_tokens.shape[1]) if lang_tokens is not None else 0
        return _pool_prefix_batch(prefix_out, prefix_mask, self.layer_hook, prompt_len, 0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ckpt", type=Path, required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--layer-hook", default="pi_prefix")
    args = parser.parse_args()

    try:
        embedder = OpenPIFrameEmbedder(args.ckpt, args.config, args.layer_hook)
    except Exception:
        _write({"status": "error", "error": traceback.format_exc()})
        return

    _write({"status": "ready"})
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get("stop"):
                return
            request_start = time.perf_counter()
            load_start = time.perf_counter()
            raws = _load_raw_payload(Path(request["path"]), Path(request["manifest"]))
            payload_load_s = time.perf_counter() - load_start
            vecs = embedder.embed_many(raws)
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            vecs = np.divide(vecs, np.maximum(norms, 1e-8), out=np.zeros_like(vecs), where=norms > 1e-8)
            timing = {
                "worker_payload_load_s": payload_load_s,
                **embedder.last_timing,
                "worker_request_s": time.perf_counter() - request_start,
            }
            _write({"status": "ok", "vectors": vecs.astype(float).tolist(), "timing": timing})
        except Exception:
            _write({"status": "error", "error": traceback.format_exc()})


if __name__ == "__main__":
    main()
