"""Model catalog, Hugging Face browsing and resumable GGUF downloads."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import requests

from . import config, db

HF_API = "https://huggingface.co/api"
HF_UA = {"User-Agent": "LocalCode/0.1"}
GB = 1024 ** 3

# Curated, verified GGUF repos. `quant` is the recommended quantization regex.
CATALOG: list[dict] = [
    # ------------------------------------------------------------- starter
    {
        "id": "qwen25-coder-1.5b", "name": "Qwen2.5 Coder 1.5B",
        "repo": "Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF", "quant": "Q4_K_M",
        "category": "starter", "params": "1.5B", "context": 32768,
        "license": "Apache-2.0", "size_gb": 1.04, "min_ram_gb": 3,
        "blurb": "Tiny and quick. Great for a first run on a low-RAM laptop, simple edits and explanations.",
        "tags": ["fast", "low-ram"],
    },
    {
        "id": "qwen25-coder-3b", "name": "Qwen2.5 Coder 3B",
        "repo": "Qwen/Qwen2.5-Coder-3B-Instruct-GGUF", "quant": "Q4_K_M",
        "category": "starter", "params": "3B", "context": 32768,
        "license": "Apache-2.0", "size_gb": 1.96, "min_ram_gb": 5,
        "blurb": "A solid small coder. Faster than 7B and still understands most everyday tasks.",
        "tags": ["fast", "coding"],
    },
    {
        "id": "qwen3-4b", "name": "Qwen3 4B Instruct",
        "repo": "unsloth/Qwen3-4B-Instruct-2507-GGUF", "quant": "Q4_K_M",
        "category": "starter", "params": "4B", "context": 262144,
        "license": "Apache-2.0", "size_gb": 2.33, "min_ram_gb": 6,
        "blurb": "Best all-round small agent. Recommended starting point for an 8 GB machine.",
        "tags": ["recommended", "agentic", "long-context"],
    },
    {
        "id": "gemma3-4b", "name": "Gemma 3 4B IT (vision)",
        "repo": "unsloth/gemma-3-4b-it-GGUF", "quant": "Q4_K_M",
        "category": "starter", "params": "4B", "context": 131072,
        "license": "Gemma Terms", "size_gb": 3.10, "min_ram_gb": 8,
        "vision": True, "mmproj": True,
        "blurb": "Small multimodal model: reads text, images and PDF page images. Download the vision projector too.",
        "tags": ["vision", "images", "small"],
    },
    # -------------------------------------------------------------- coding
    {
        "id": "qwen25-coder-7b", "name": "Qwen2.5 Coder 7B",
        "repo": "bartowski/Qwen2.5-Coder-7B-Instruct-GGUF", "quant": "Q4_K_M",
        "category": "coding", "params": "7B", "context": 32768,
        "license": "Apache-2.0", "size_gb": 4.36, "min_ram_gb": 8,
        "blurb": "The dependable 7B coding workhorse. Excellent for edits, refactors and small projects.",
        "tags": ["coding", "proven"],
    },
    {
        "id": "qwen3-8b", "name": "Qwen3 8B",
        "repo": "unsloth/Qwen3-8B-GGUF", "quant": "Q4_K_M",
        "category": "coding", "params": "8B", "context": 131072,
        "license": "Apache-2.0", "size_gb": 4.68, "min_ram_gb": 8,
        "blurb": "Strong general reasoning with optional thinking mode. Good on 8–12 GB systems.",
        "tags": ["reasoning", "agentic"],
    },
    {
        "id": "qwen35-coder-9b", "name": "Qwen3.5 Coder 9B",
        "repo": "mradermacher/Qwen3.5-9B-Coder-GGUF", "quant": "Q4_K_M",
        "category": "coding", "params": "9B", "context": 131072,
        "license": "Apache-2.0", "size_gb": 5.38, "min_ram_gb": 10,
        "blurb": "A newer 9B coding model with a good quality-to-speed balance for 12 GB machines.",
        "tags": ["coding", "new"],
    },
    {
        "id": "deepseek-coder-v2-lite", "name": "DeepSeek Coder V2 Lite 16B",
        "repo": "bartowski/DeepSeek-Coder-V2-Lite-Instruct-GGUF", "quant": "Q4_K_M",
        "category": "coding", "params": "16B MoE", "context": 163840,
        "license": "DeepSeek License", "size_gb": 9.65, "min_ram_gb": 14,
        "blurb": "MoE coder with only ~2.4B active parameters: faster than its size suggests.",
        "tags": ["moe", "coding"],
    },
    {
        "id": "qwen25-coder-14b", "name": "Qwen2.5 Coder 14B",
        "repo": "bartowski/Qwen2.5-Coder-14B-Instruct-GGUF", "quant": "Q4_K_M",
        "category": "coding", "params": "14B", "context": 32768,
        "license": "Apache-2.0", "size_gb": 8.37, "min_ram_gb": 12,
        "blurb": "Noticeably stronger than 7B for multi-file work. Needs a 12–16 GB machine.",
        "tags": ["coding"],
    },
    {
        "id": "qwen25-coder-32b", "name": "Qwen2.5 Coder 32B",
        "repo": "bartowski/Qwen2.5-Coder-32B-Instruct-GGUF", "quant": "Q4_K_M",
        "category": "coding", "params": "32B", "context": 32768,
        "license": "Apache-2.0", "size_gb": 18.49, "min_ram_gb": 24,
        "blurb": "One of the best classic local coding models. A serious step up if you have 24 GB+ RAM.",
        "tags": ["coding", "quality"],
    },
    {
        "id": "codestral-22b", "name": "Codestral 22B",
        "repo": "bartowski/Codestral-22B-v0.1-GGUF", "quant": "Q4_K_M",
        "category": "coding", "params": "22B", "context": 32768,
        "license": "MNPL", "size_gb": 12.42, "min_ram_gb": 16,
        "blurb": "Mistral's code specialist. Strong fill-in-the-middle and completion behaviour.",
        "tags": ["coding", "mistral"],
    },
    {
        "id": "gemma3-27b", "name": "Gemma 3 27B",
        "repo": "unsloth/gemma-3-27b-it-GGUF", "quant": "Q4_K_M",
        "category": "coding", "params": "27B", "context": 131072,
        "license": "Gemma Terms", "size_gb": 15.40, "min_ram_gb": 20,
        "blurb": "Google's strong open model. Great explanations, planning and general coding help.",
        "tags": ["general", "reasoning"],
    },
    {
        "id": "qwen25-vl-7b", "name": "Qwen2.5 VL 7B (vision)",
        "repo": "unsloth/Qwen2.5-VL-7B-Instruct-GGUF", "quant": "Q4_K_M",
        "category": "coding", "params": "7B", "context": 131072,
        "license": "Apache-2.0", "size_gb": 6.05, "min_ram_gb": 12,
        "vision": True, "mmproj": True,
        "blurb": "Strong vision-language model for screenshots, diagrams, PDFs and UI work. Needs the projector.",
        "tags": ["vision", "images", "coding"],
    },
    {
        "id": "qwen38-27b", "name": "Qwen3.8 27B",
        "repo": "unsloth/Qwen3.8-27B-GGUF", "quant": "Q4_K_M",
        "category": "coding", "params": "27B", "context": 262144,
        "license": "Apache-2.0", "size_gb": 15.33, "min_ram_gb": 20,
        "featured": True,
        "blurb": "Current flagship dense model for coding and reasoning. Excellent agent behaviour at 20 GB+ RAM.",
        "tags": ["featured", "agentic", "reasoning"],
    },
    {
        "id": "qwen3-coder-30b", "name": "Qwen3 Coder 30B A3B",
        "repo": "unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF", "quant": "Q4_K_M",
        "category": "coding", "params": "30B MoE", "context": 262144,
        "license": "Apache-2.0", "size_gb": 17.28, "min_ram_gb": 20,
        "featured": True,
        "blurb": "Fast MoE coder with strong tool use and long context. Around 3B active parameters.",
        "tags": ["featured", "moe", "agentic"],
    },
    {
        "id": "tiel-coder-35b", "name": "Tiel Coder 35B A3B",
        "repo": "peculiar-ragdoll/Tiel-Coder-35B-A3B-GGUF", "quant": "Q4_K_XL",
        "category": "coding", "params": "35B MoE", "context": 131072,
        "license": "Apache-2.0", "size_gb": 20.83, "min_ram_gb": 24,
        "featured": True,
        "blurb": "A top open coding model of late 2026. Strong agentic edits with MoE inference speed.",
        "tags": ["featured", "moe", "coding"],
    },
    {
        "id": "kat-coder-v25", "name": "KAT Coder V2.5 Dev",
        "repo": "bartowski/Kwaipilot_KAT-Coder-V2.5-Dev-GGUF", "quant": "Q4_K_M",
        "category": "coding", "params": "32B", "context": 131072,
        "license": "Apache-2.0", "size_gb": 19.92, "min_ram_gb": 24,
        "blurb": "Kwaipilot's agentic coding specialist. Excellent at repository-scale changes.",
        "tags": ["coding", "agentic"],
    },
    {
        "id": "qwen3-coder-next", "name": "Qwen3 Coder Next",
        "repo": "unsloth/Qwen3-Coder-Next-GGUF", "quant": "Q4_K_M",
        "category": "coding", "params": "80B MoE", "context": 262144,
        "license": "Apache-2.0", "size_gb": 45.20, "min_ram_gb": 48,
        "blurb": "Large next-gen coder. Needs a workstation or a GPU with plenty of VRAM.",
        "tags": ["large", "moe"],
    },
    {
        "id": "llama33-70b", "name": "Llama 3.3 70B",
        "repo": "bartowski/Llama-3.3-70B-Instruct-GGUF", "quant": "Q4_K_M",
        "category": "coding", "params": "70B", "context": 131072,
        "license": "Llama 3.3 License", "size_gb": 39.60, "min_ram_gb": 48,
        "blurb": "Meta's 70B generalist. Great writing and reasoning if you have the hardware.",
        "tags": ["general", "large"],
    },
    # ---------------------------------------------------------- uncensored
    {
        "id": "qwen25-coder-7b-abliterated", "name": "Qwen2.5 Coder 7B Abliterated",
        "repo": "bartowski/Qwen2.5-Coder-7B-Instruct-abliterated-GGUF", "quant": "Q4_K_M",
        "category": "uncensored", "params": "7B", "context": 32768,
        "license": "Apache-2.0", "size_gb": 4.36, "min_ram_gb": 8,
        "featured": True,
        "blurb": "The proven 7B coder with refusals removed. Small enough for an 8 GB laptop.",
        "tags": ["uncensored", "coding", "small"],
    },
    {
        "id": "dolphin3-8b", "name": "Dolphin 3.0 Llama 3.1 8B",
        "repo": "cognitivecomputations/Dolphin3.0-Llama3.1-8B-GGUF", "quant": "Q4_K_M",
        "category": "uncensored", "params": "8B", "context": 131072,
        "license": "Llama 3.1 License", "size_gb": 4.58, "min_ram_gb": 8,
        "blurb": "Friendly, uncensored general model from Cognitive Computations. Good writing and code.",
        "tags": ["uncensored", "general"],
    },
    {
        "id": "lexi-uncensored-8b", "name": "Lexi Uncensored V2 8B",
        "repo": "bartowski/Llama-3.1-8B-Lexi-Uncensored-V2-GGUF", "quant": "Q4_K_M",
        "category": "uncensored", "params": "8B", "context": 131072,
        "license": "Llama 3.1 License", "size_gb": 4.58, "min_ram_gb": 8,
        "blurb": "Popular uncensored 8B that runs on 8 GB machines. Strong writing and open-ended tasks.",
        "tags": ["uncensored", "writing", "recommended"],
    },
    {
        "id": "qwen38-27b-abliterated", "name": "Qwen3.8 27B Abliterated",
        "repo": "huihui-ai/Huihui-Qwen3.8-27B-abliterated-GGUF", "quant": "Q4_K_M",
        "category": "uncensored", "params": "27B", "context": 262144,
        "license": "Apache-2.0", "size_gb": 15.41, "min_ram_gb": 20,
        "featured": True,
        "blurb": "The current 27B flagship with abliteration. Strong coding without refusals.",
        "tags": ["featured", "uncensored", "coding"],
    },
    {
        "id": "qwen38-27b-uncensored", "name": "Qwen3.8 27B Uncensored",
        "repo": "JonathanColetti/Qwen3.8-27B-Uncensored-GGUF", "quant": "Q4_K_M",
        "category": "uncensored", "params": "27B", "context": 262144,
        "license": "Apache-2.0", "size_gb": 15.66, "min_ram_gb": 20,
        "blurb": "An independently uncensored Qwen3.8 27B tune with strong general ability.",
        "tags": ["uncensored", "reasoning"],
    },
    {
        "id": "heretic-neo-coder-27b", "name": "Heretic NEO Coder 27B",
        "repo": "DavidAU/Qwen3.8-27B-TURBO-Fable-Cold-Fusion-735-882-Heretic-Uncensored-NEO-CODER-MAX-MTP-GGUF",
        "quant": "Q4_K_M", "category": "uncensored", "params": "27B",
        "context": 131072, "license": "Apache-2.0", "size_gb": 17.23, "min_ram_gb": 22,
        "featured": True,
        "blurb": "A maximal, uncensored 27B coding fusion tune. Heavy on creativity and code.",
        "tags": ["featured", "uncensored", "coding"],
    },
    {
        "id": "huihui-qwen3-coder-30b-abl", "name": "Huihui Qwen3 Coder 30B Abliterated",
        "repo": "mradermacher/Huihui-Qwen3-Coder-30B-A3B-Instruct-abliterated-i1-GGUF",
        "quant": "Q4_K_M", "category": "uncensored", "params": "30B MoE",
        "context": 262144, "license": "Apache-2.0", "size_gb": 17.28, "min_ram_gb": 20,
        "blurb": "Uncensored MoE coder with excellent speed for its size on 20 GB+ machines.",
        "tags": ["uncensored", "moe", "coding"],
    },
    {
        "id": "deepseek-v41-abliterated", "name": "DeepSeek V4.1 Flash Abliterated",
        "repo": "audreyt/DeepSeek-V4.1-Flash-Abliterated-GGUF", "quant": "Q2",
        "category": "uncensored", "params": "Huge MoE", "context": 262144,
        "license": "MIT", "size_gb": 340.6, "min_ram_gb": 384,
        "blurb": "A 340 GB abliterated frontier model. Only for multi-GPU rigs or 384 GB+ servers.",
        "tags": ["uncensored", "frontier", "huge"],
    },
    # ------------------------------------------------------------ frontier
    {
        "id": "deepseek-v41-flash", "name": "DeepSeek V4.1 Flash",
        "repo": "unsloth/DeepSeek-V4.1-Flash-1017-GGUF", "quant": "IQ2_XXS",
        "category": "frontier", "params": "Huge MoE", "context": 262144,
        "license": "MIT", "size_gb": 279.84, "min_ram_gb": 320,
        "featured": True,
        "blurb": "DeepSeek's 2026 frontier model. Q4 is ~549 GB; pick a smaller quant for big servers.",
        "tags": ["frontier", "coding", "huge"],
    },
    {
        "id": "glm-53-flash", "name": "GLM-5.3 Flash",
        "repo": "unsloth/GLM-5.3-Flash-GGUF", "quant": "IQ2_XXS",
        "category": "frontier", "params": "Huge MoE", "context": 262144,
        "license": "MIT", "size_gb": 94.85, "min_ram_gb": 128,
        "featured": True,
        "blurb": "Zhipu's agentic coding model. Around 200 GB at Q4; use IQ2 on a 256 GB server.",
        "tags": ["frontier", "agentic", "huge"],
    },
    {
        "id": "minimax-m25", "name": "MiniMax M2.5",
        "repo": "unsloth/MiniMax-M2.5-GGUF", "quant": "IQ2_XXS",
        "category": "frontier", "params": "Huge MoE", "context": 262144,
        "license": "MIT", "size_gb": 69.03, "min_ram_gb": 96,
        "blurb": "A large agentic model from MiniMax. Strong planning and tool use at frontier scale.",
        "tags": ["frontier", "agentic", "huge"],
    },
    {
        "id": "kimi-k25", "name": "Kimi K2.5",
        "repo": "unsloth/Kimi-K2.5-GGUF", "quant": "IQ2_XXS",
        "category": "frontier", "params": "Huge MoE", "context": 262144,
        "license": "Modified MIT", "size_gb": 304.3, "min_ram_gb": 384,
        "blurb": "Moonshot's frontier model. Massive even at low quantization; a multi-GPU server target.",
        "tags": ["frontier", "agentic", "huge"],
    },
    {
        "id": "deepseek-v31", "name": "DeepSeek V3.1",
        "repo": "unsloth/DeepSeek-V3.1-GGUF", "quant": "IQ2_XXS",
        "category": "frontier", "params": "671B MoE", "context": 131072,
        "license": "MIT", "size_gb": 210.37, "min_ram_gb": 256,
        "blurb": "The 671B V3.1 MoE. Q4 is ~404 GB; lower quants fit on 256–384 GB servers.",
        "tags": ["frontier", "coding", "huge"],
    },
    {
        "id": "qwen3-235b", "name": "Qwen3 235B A22B",
        "repo": "unsloth/Qwen3-235B-A22B-Instruct-2507-GGUF", "quant": "Q4_K_M",
        "category": "frontier", "params": "235B MoE", "context": 262144,
        "license": "Apache-2.0", "size_gb": 132.39, "min_ram_gb": 192,
        "blurb": "A 235B MoE with ~22B active parameters. Strong reasoning if you have the RAM.",
        "tags": ["frontier", "reasoning", "huge"],
    },
    {
        "id": "deepseek-r1-0528-8b", "name": "DeepSeek R1 0528 Qwen3 8B",
        "repo": "unsloth/DeepSeek-R1-0528-Qwen3-8B-GGUF", "quant": "Q4_K_M",
        "category": "frontier", "params": "8B", "context": 131072,
        "license": "MIT", "size_gb": 4.68, "min_ram_gb": 8,
        "featured": True,
        "blurb": "Distilled R1 reasoning in an 8B package. Runs on a normal laptop and thinks before answering.",
        "tags": ["reasoning", "small"],
    },
]

CATEGORY_LABELS = {
    "starter": "Starter",
    "coding": "Coding",
    "uncensored": "Uncensored",
    "frontier": "Frontier",
}

_QUANT_RE = re.compile(
    r"(?<![A-Za-z0-9])(IQ\d+_[A-Z0-9]+|Q\d+_K_[A-Z0-9]+|Q\d+_K|Q\d+_\d+|Q\d+)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_SPLIT_RE = re.compile(r"^(?P<base>.+?)-(?P<part>\d{5})-(?:of|OF)-(?P<total>\d{5})\.gguf$", re.IGNORECASE)
_QUANT_RANK = {
    "Q8_0": 100, "Q6_K": 95, "Q6_K_L": 96, "Q5_K_M": 90, "Q5_K_L": 91, "Q5_K_S": 85,
    "Q4_K_M": 82, "Q4_K_XL": 84, "Q4_K_L": 83, "Q4_K_S": 76, "Q4_K": 80,
    "IQ4_XS": 72, "IQ4_NL": 70, "Q4_0": 60, "Q4_1": 58,
    "Q3_K_M": 55, "Q3_K_L": 56, "Q3_K_XL": 57, "Q3_K_S": 50, "Q3_K": 52,
    "Q2_K": 40, "Q2_K_XL": 42, "Q2_K_L": 41,
    "IQ2_M": 35, "IQ2_S": 33, "IQ2_XS": 31, "IQ2_XXS": 30,
    "IQ1_M": 20, "IQ1_S": 18, "Q1": 10,
}


def normalize_repo(repo: str) -> str:
    repo = (repo or "").strip()
    if "huggingface.co/" in repo:
        repo = repo.split("huggingface.co/", 1)[1]
        repo = repo.split("?", 1)[0].split("#", 1)[0].strip("/")
    if repo.startswith("https://") or repo.startswith("http://"):
        repo = urlparse(repo).path.strip("/")
    parts = [p for p in repo.split("/") if p]
    if len(parts) < 2:
        raise ValueError("Use a Hugging Face repo id like 'unsloth/Qwen3-8B-GGUF'")
    return "/".join(parts[:2])


def hf_token() -> str:
    return str(config.get("hf_token") or "").strip()


def _headers() -> dict:
    h = dict(HF_UA)
    token = hf_token()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _hf_get(url: str, params: dict | None = None, timeout: int = 30):
    r = requests.get(url, params=params, headers=_headers(), timeout=timeout)
    if r.status_code in (401, 403):
        raise ValueError("This Hugging Face model is gated. Add your HF token in Settings → Downloads.")
    if r.status_code == 404:
        raise ValueError("Model not found on Hugging Face.")
    r.raise_for_status()
    return r


def _cache_path(repo: str) -> Path:
    h = hashlib.sha1(repo.encode("utf-8")).hexdigest()
    return config.HF_CACHE_DIR / f"{h}.json"


def repo_quants(repo: str, force: bool = False) -> dict:
    repo = normalize_repo(repo)
    cache = _cache_path(repo)
    if not force and cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if time.time() - data.get("ts", 0) < 6 * 3600:
                return data["data"]
        except Exception:
            pass
    r = _hf_get(f"{HF_API}/models/{repo}/tree/main", {"recursive": "true"}, timeout=45)
    files = r.json()
    if not isinstance(files, list):
        raise ValueError("Unexpected response from Hugging Face.")
    entries = []
    mmproj_files = []
    for f in files:
        if f.get("type") != "file":
            continue
        path = str(f.get("path") or "")
        if not path.lower().endswith(".gguf"):
            continue
        if "mmproj" in path.lower():
            mmproj_files.append({"path": path, "size": int(f.get("size") or 0)})
            continue
        entries.append({"path": path, "size": int(f.get("size") or 0)})
    groups: dict[str, dict] = {}
    for entry in entries:
        name = entry["path"].rsplit("/", 1)[-1]
        m = _SPLIT_RE.match(name)
        if m:
            total = int(m.group("total"))
            key = entry["path"][: -len(name)] + m.group("base") + f"::of{total}"
            grp = groups.setdefault(key, {
                "key": key, "base": m.group("base"), "total_parts": total,
                "parts": [], "files": [], "size": 0, "main_file": None,
            })
            grp["parts"].append((int(m.group("part")), entry))
        else:
            key = entry["path"]
            groups[key] = {
                "key": key, "base": name[:-5], "total_parts": 1,
                "parts": [(1, entry)], "files": [entry], "size": entry["size"],
                "main_file": entry["path"],
            }
    quants = []
    for grp in groups.values():
        if grp["total_parts"] > 1:
            if len(grp["parts"]) != grp["total_parts"]:
                continue
            grp["parts"].sort(key=lambda x: x[0])
            grp["files"] = [p[1] for p in grp["parts"]]
            grp["size"] = sum(f["size"] for f in grp["files"])
            grp["main_file"] = grp["files"][0]["path"]
        if not grp["files"]:
            continue
        label_src = grp["base"]
        matches = _QUANT_RE.findall(label_src)
        quant = (matches[-1] if matches else "GGUF").upper()
        grp["quant"] = quant
        grp["name"] = grp["base"]
        grp["rank"] = _QUANT_RANK.get(quant, 50)
        quants.append(grp)
    quants.sort(key=lambda q: (-q["rank"], q["base"].lower()))
    data = {
        "repo": repo,
        "quants": [
            {
                "key": q["key"], "quant": q["quant"], "name": q["name"],
                "size": q["size"], "parts": len(q["files"]),
                "files": q["files"], "main_file": q["main_file"],
                "rank": q["rank"],
            }
            for q in quants
        ],
        "mmproj": mmproj_files,
    }
    cache.write_text(json.dumps({"ts": time.time(), "data": data}), encoding="utf-8")
    return data


def pick_quant(quants: list[dict], wanted: str) -> dict | None:
    wanted = (wanted or "").upper()
    if not quants:
        return None
    if wanted:
        for q in quants:
            if q["quant"] == wanted:
                return q
        for q in quants:
            if wanted in q["quant"] or q["quant"] in wanted:
                return q
    return quants[0]


def catalog_for_ui(installed: list[dict], runtime_status: dict) -> list[dict]:
    info = config.system_info()
    ram = info.get("ram") or 0
    gpus = info.get("gpus") or []
    vram_gb = max(((g.get("vram") or 0) for g in gpus), default=0) / 1e9 if gpus else 0
    loaded_id = runtime_status.get("model_id")
    installed_by_repo: dict[str, list[dict]] = {}
    for m in installed:
        installed_by_repo.setdefault(m.get("repo") or "", []).append(m)
    out = []
    for entry in CATALOG:
        item = dict(entry)
        item["size"] = int(float(entry.get("size_gb") or 0) * GB)
        item["min_ram"] = int(float(entry.get("min_ram_gb") or 0) * GB)
        if ram and item["min_ram"]:
            ratio = item["min_ram"] / ram
            item["fit"] = "great" if ratio <= 0.7 else ("tight" if ratio <= 1.0 else "no")
        else:
            item["fit"] = "unknown"
        # GPU fit is separate: RAM fit says nothing about whether the whole model
        # fits in VRAM. On cards smaller than the model, it runs partially on CPU.
        size_gb = float(entry.get("size_gb") or 0)
        if vram_gb <= 0:
            item["gpu_fit"] = "none"
        elif size_gb <= vram_gb * 0.85:
            item["gpu_fit"] = "full"
        else:
            item["gpu_fit"] = "partial"
        item["vram_gb"] = round(vram_gb, 1)
        item["installed"] = [
            {**m, "loaded": m["id"] == loaded_id} for m in installed_by_repo.get(entry["repo"], [])
        ]
        out.append(item)
    return out


# ------------------------------------------------------------------ installed
def _sidecar_paths() -> list[Path]:
    if not config.MODELS_DIR.exists():
        return []
    return list(config.MODELS_DIR.rglob("model.localcode.json"))


def _load_sidecar(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("kind") != "localcode.model":
            return None
        base = path.parent
        files = [base / f for f in data.get("files", [])]
        main = base / data.get("main_file", "")
        if not main.exists():
            return None
        data["_dir"] = str(base)
        data["_files"] = [str(f) for f in files if f.exists()]
        data["_main"] = str(main)
        data["size"] = int(data.get("size") or sum(f.stat().st_size for f in files if f.exists()))
        return data
    except Exception:
        return None


def scan_installed() -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for sc in _sidecar_paths():
        data = _load_sidecar(sc)
        if not data:
            continue
        seen.add(str(Path(data["_dir"]).resolve()))
        mmproj = next(
            (f for f in data["_files"] if "mmproj" in Path(f).name.lower()), None
        )
        items.append({
            "id": data.get("id") or config.safe_slug(data.get("name", "model")),
            "name": data.get("name") or Path(data["_main"]).stem,
            "repo": data.get("repo"),
            "category": data.get("category", "local"),
            "quant": data.get("quant", "GGUF"),
            "params": data.get("params", ""),
            "context": data.get("context"),
            "size": data.get("size"),
            "path": data["_dir"],
            "main_file": data["_main"],
            "mmproj_file": mmproj,
            "vision_ready": bool(mmproj),
            "files": data["_files"],
            "installed_at": data.get("installed_at"),
            "source": "download",
        })
    # Models copied in manually (no sidecar).
    for gguf in sorted(config.MODELS_DIR.rglob("*.gguf")):
        parent = str(gguf.parent.resolve())
        if parent in seen or "mmproj" in gguf.name.lower():
            continue
        name = gguf.name
        if _SPLIT_RE.match(name) and "-00001-" not in name.lower():
            continue
        siblings = sorted(gguf.parent.glob("*.gguf"))
        size = sum(f.stat().st_size for f in siblings)
        main = next((f for f in siblings if "-00001-" in f.name.lower()), gguf)
        rel_id = config.safe_slug(str(gguf.parent.relative_to(config.MODELS_DIR)).replace("\\", "/"))
        items.append({
            "id": f"local/{rel_id}",
            "name": main.stem,
            "repo": "local",
            "category": "local",
            "quant": "GGUF",
            "params": "",
            "context": None,
            "size": size,
            "path": str(gguf.parent),
            "main_file": str(main),
            "files": [str(f) for f in siblings],
            "mmproj_file": next(
                (str(f) for f in siblings if "mmproj" in f.name.lower()), None
            ),
            "vision_ready": any("mmproj" in f.name.lower() for f in siblings),
            "installed_at": None,
            "source": "local",
        })
        seen.add(parent)
    items.sort(key=lambda x: (x.get("name") or "").lower())
    return items


def get_installed(model_id: str) -> dict | None:
    for item in scan_installed():
        if item["id"] == model_id:
            return item
    return None


def delete_installed(model_id: str) -> None:
    item = get_installed(model_id)
    if not item:
        raise ValueError("Model not found")
    base = config.MODELS_DIR.resolve()
    for f in item["files"]:
        p = Path(f).resolve()
        if base not in p.parents and p != base:
            raise ValueError("Refusing to delete a file outside the models directory")
    for f in item["files"]:
        try:
            Path(f).unlink()
        except OSError:
            pass
    sidecar = Path(item["path"]) / "model.localcode.json"
    try:
        sidecar.unlink()
    except OSError:
        pass
    try:
        Path(item["path"]).rmdir()
    except OSError:
        pass


# ------------------------------------------------------------------- search
def search_hf(query: str = "", limit: int = 24) -> list[dict]:
    params = {
        "filter": "gguf",
        "sort": "downloads",
        "direction": "-1",
        "limit": max(1, min(limit, 50)),
    }
    if query.strip():
        params["search"] = query.strip()
    r = _hf_get(f"{HF_API}/models", params, timeout=30)
    data = r.json()
    out = []
    for m in data:
        tags = [t for t in (m.get("tags") or []) if not t.startswith("region:")]
        out.append({
            "id": m.get("id"),
            "downloads": m.get("downloads") or 0,
            "likes": m.get("likes") or 0,
            "tags": tags[:12],
            "pipeline_tag": m.get("pipeline_tag"),
            "gated": bool(m.get("gated")),
            "updated": m.get("lastModified"),
        })
    return out


# ----------------------------------------------------------------- downloads
class DownloadManager:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gguf-dl")

    def public(self, job: dict) -> dict:
        out = {k: v for k, v in job.items() if k not in {"cancel", "thread", "_lock"}}
        files = []
        for f in job.get("files", []):
            files.append({
                "path": f.get("path"),
                "size": f.get("size", 0),
                "downloaded": f.get("downloaded", 0),
                "status": f.get("status", "pending"),
            })
        out["files"] = files
        return out

    def list(self) -> list[dict]:
        with self.lock:
            jobs = list(self.jobs.values())
        return [self.public(j) for j in sorted(jobs, key=lambda x: x.get("created_at", ""), reverse=True)]

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
        return self.public(job) if job else None

    def start(self, repo: str, files: list[dict], meta: dict,
              mmproj: list[dict] | None = None) -> dict:
        repo = normalize_repo(repo)
        if not files:
            raise ValueError("No model files selected")
        clean_files = []
        for f in files:
            path = str(f.get("path") or "").replace("\\", "/")
            if not path.lower().endswith(".gguf") or ".." in path.split("/"):
                raise ValueError("Invalid GGUF file path")
            clean_files.append({"path": path, "size": int(f.get("size") or 0)})
        for f in mmproj or []:
            path = str(f.get("path") or "").replace("\\", "/")
            if not path.lower().endswith(".gguf") or ".." in path.split("/"):
                raise ValueError("Invalid vision projector path")
            clean_files.append({
                "path": path, "size": int(f.get("size") or 0), "role": "mmproj"
            })
        total = sum(f["size"] for f in clean_files)
        free = config.disk_info()["free"]
        if total and free < total * 1.02:
            raise ValueError(
                f"Not enough disk space: need {config.human_bytes(total)}, only {config.human_bytes(free)} free."
            )
        main_files = [f for f in clean_files if f.get("role") != "mmproj"]
        name = str(meta.get("name") or (main_files or clean_files)[0]["path"].rsplit("/", 1)[-1][:-5])
        quant = str(meta.get("quant") or "GGUF")
        slug = config.safe_slug(f"{quant}-{name}", 70)
        target_dir = config.MODELS_DIR / config.safe_slug(repo.replace("/", "__"), 90) / slug
        if (target_dir / "model.localcode.json").exists():
            raise ValueError("That model is already downloaded")
        target_dir.mkdir(parents=True, exist_ok=True)
        model_id = f"{config.safe_slug(repo.replace('/', '__'), 90)}/{slug}"
        with self.lock:
            for other in self.jobs.values():
                if other["status"] in {"queued", "downloading"} and other["model_id"] == model_id:
                    raise ValueError("That model is already downloading. Check the Downloads tab.")
        job_id = db.new_id("dl")
        job = {
            "id": job_id,
            "name": name,
            "repo": repo,
            "category": str(meta.get("category") or "local"),
            "quant": quant,
            "params": str(meta.get("params") or ""),
            "context": meta.get("context"),
            "status": "queued",
            "total": total,
            "downloaded": 0,
            "percent": 0,
            "speed": 0,
            "eta": None,
            "error": None,
            "created_at": config.now(),
            "started_at": None,
            "finished_at": None,
            "files": clean_files,
            "target_dir": str(target_dir),
            "model_id": model_id,
            "main_file": None,
            "mmproj": bool(mmproj),
            "cancel": threading.Event(),
        }
        with self.lock:
            self.jobs[job_id] = job
        self.pool.submit(self._run, job)
        return self.public(job)

    def start_url(self, url: str, name: str = "", category: str = "local") -> dict:
        url = url.strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("https", "http"):
            raise ValueError("URL must start with http:// or https://")
        filename = unquote(parsed.path.rsplit("/", 1)[-1]) or "model.gguf"
        if not filename.lower().endswith(".gguf"):
            raise ValueError("The URL must point to a .gguf file")
        size = 0
        try:
            head = requests.head(url, headers=HF_UA, allow_redirects=True, timeout=30)
            size = int(head.headers.get("Content-Length") or 0)
        except Exception:
            pass
        display = name.strip() or filename[:-5]
        slug = config.safe_slug(display, 70)
        target_dir = config.MODELS_DIR / "custom" / slug
        if (target_dir / "model.localcode.json").exists():
            raise ValueError("A model with that name is already downloaded")
        model_id = f"custom/{slug}"
        with self.lock:
            for other in self.jobs.values():
                if other["status"] in {"queued", "downloading"} and other["model_id"] == model_id:
                    raise ValueError("That model is already downloading. Check the Downloads tab.")
        target_dir.mkdir(parents=True, exist_ok=True)
        job_id = db.new_id("dl")
        job = {
            "id": job_id, "name": display, "repo": "custom", "category": category,
            "quant": "GGUF", "params": "", "context": None, "status": "queued",
            "total": size, "downloaded": 0, "percent": 0, "speed": 0, "eta": None,
            "error": None, "created_at": config.now(), "started_at": None,
            "finished_at": None,
            "files": [{"path": filename, "size": size, "url": url}],
            "target_dir": str(target_dir), "model_id": model_id,
            "main_file": None, "mmproj": False, "cancel": threading.Event(),
        }
        with self.lock:
            self.jobs[job_id] = job
        self.pool.submit(self._run, job)
        return self.public(job)

    def cancel(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
        if not job:
            return False
        job["cancel"].set()
        return True

    def remove(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return False
            if job["status"] in {"queued", "downloading"}:
                raise ValueError("Cancel the download first")
            target_dir = Path(job["target_dir"])
            del self.jobs[job_id]
        # A removed job leaves no trace: drop partial files unless a finished
        # model already lives in that folder.
        try:
            if target_dir.exists() and not (target_dir / "model.localcode.json").exists():
                for part in target_dir.glob("*.part"):
                    part.unlink(missing_ok=True)
        except Exception:
            pass
        return True

    def _run(self, job: dict) -> None:
        target_dir = Path(job["target_dir"])
        job["status"] = "downloading"
        job["started_at"] = config.now()
        started = time.time()
        completed = 0
        try:
            for file in job["files"]:
                if job["cancel"].is_set():
                    raise _Cancelled()
                path = str(file["path"])
                filename = path.rsplit("/", 1)[-1]
                dest = target_dir / filename
                url = file.get("url") or (
                    f"https://huggingface.co/{job['repo']}/resolve/main/{quote(path)}?download=true"
                )
                self._download_one(job, file, url, dest, completed)
                completed += int(file.get("size") or dest.stat().st_size)
                file["downloaded"] = int(file.get("size") or dest.stat().st_size)
                file["status"] = "complete"
            main_files = [f for f in job["files"] if f.get("role") != "mmproj"]
            main_name = Path((main_files or job["files"])[0]["path"]).name
            sidecar = {
                "kind": "localcode.model",
                "id": job["model_id"],
                "name": job["name"],
                "repo": job["repo"],
                "category": job["category"],
                "quant": job["quant"],
                "params": job["params"],
                "context": job["context"],
                "size": completed,
                "files": [Path(f["path"]).name for f in job["files"]],
                "main_file": main_name,
                "installed_at": config.now(),
            }
            (target_dir / "model.localcode.json").write_text(
                json.dumps(sidecar, indent=2), encoding="utf-8"
            )
            job["main_file"] = str(target_dir / main_name)
            job["downloaded"] = completed
            job["percent"] = 100
            job["speed"] = 0
            job["eta"] = 0
            job["status"] = "complete"
            job["finished_at"] = config.now()
        except _Cancelled:
            job["status"] = "cancelled"
            job["finished_at"] = config.now()
            job["speed"] = 0
        except Exception as exc:
            job["status"] = "error"
            job["error"] = str(exc)
            job["finished_at"] = config.now()
            job["speed"] = 0
        finally:
            job["cancel"].set()

    def _download_one(self, job: dict, file: dict, url: str, dest: Path, completed_before: int) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        existing = part.stat().st_size if part.exists() else 0
        expected = int(file.get("size") or 0)
        if expected and existing > expected:
            part.unlink(missing_ok=True)
            existing = 0
        headers = dict(HF_UA)
        token = hf_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if existing:
            headers["Range"] = f"bytes={existing}-"
        file["status"] = "downloading"
        with requests.get(url, headers=headers, stream=True, timeout=(20, 120), allow_redirects=True) as r:
            if r.status_code in (401, 403):
                raise RuntimeError("Access denied. This model may be gated — add a Hugging Face token in Settings.")
            if r.status_code == 416 and existing and expected and existing >= expected:
                part.replace(dest)
                file["downloaded"] = expected
                return
            if existing and r.status_code == 200:
                existing = 0
                part.unlink(missing_ok=True)
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0) + existing
            if expected and not total:
                total = expected
            mode = "ab" if existing and r.status_code == 206 else "wb"
            done = existing
            last_time = time.time()
            last_done = done
            with open(part, mode) as fh:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if job["cancel"].is_set():
                        raise _Cancelled()
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done += len(chunk)
                    file["downloaded"] = done
                    now = time.time()
                    if now - last_time >= 0.4:
                        speed = (done - last_done) / max(0.001, now - last_time)
                        overall_done = completed_before + done
                        job["downloaded"] = overall_done
                        job["percent"] = round(overall_done / job["total"] * 100, 1) if job["total"] else 0
                        job["speed"] = speed
                        remaining = max(0, (job["total"] or 0) - overall_done)
                        job["eta"] = int(remaining / speed) if speed > 0 and remaining else None
                        last_time = now
                        last_done = done
            if expected and part.stat().st_size < expected:
                raise RuntimeError(f"Download incomplete for {dest.name}")
            part.replace(dest)
            file["downloaded"] = part.stat().st_size if part.exists() else done
            file["status"] = "complete"


class _Cancelled(Exception):
    pass


downloads = DownloadManager()


def disk_summary() -> dict:
    info = config.disk_info()
    return {
        "models": config.dir_size(config.MODELS_DIR),
        "free": info["free"],
        "total": info["total"],
    }
