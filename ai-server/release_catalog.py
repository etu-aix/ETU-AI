"""Discover host-compatible llama-server archives from MAIA GitHub releases."""

import json
import re
import threading
import time
import urllib.request
from urllib.error import HTTPError, URLError

from configs import safe_urlopen
from llama_setup import get_platform_info

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
_TTL_SECONDS = 300


def backend_for_variant(variant: str) -> str:
    if variant == "openvino":
        return "npu"
    if variant.startswith("cuda"):
        return "cuda"
    if variant.startswith("vulkan"):
        return "vulkan"
    return variant


def parse_release_assets(releases: list[dict], repo: str, os_name: str, arch: str, ext: str) -> list[dict]:
    """List release archives and mark which ones can be installed on this host."""
    variants = []
    seen = set()
    for release in releases:
        if release.get("draft"):
            continue
        tag = release.get("tag_name", "")
        if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", tag):
            continue
        prefix = f"llama-server-{tag}-"
        for asset in release.get("assets", []):
            name = asset.get("name", "")
            if not isinstance(name, str) or not name.startswith(prefix):
                continue
            match = re.fullmatch(r"(windows|linux|macos)-(x64|arm64)-([A-Za-z0-9][A-Za-z0-9._-]*)\.(zip|tar\.gz)", name[len(prefix):])
            if not match:
                continue
            platform_name, asset_arch, variant, archive_ext = match.groups()
            if archive_ext != ("zip" if platform_name == "windows" else "tar.gz"):
                continue
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", variant):
                continue
            url = asset.get("browser_download_url", "")
            expected_url = f"https://github.com/{repo}/releases/download/{tag}/{name}"
            if url != expected_url or name in seen:
                continue
            digest = asset.get("digest") or ""
            if digest and not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
                continue
            seen.add(name)
            variants.append({"id": name, "label": name, "name": name, "tag": tag,
                             "variant": variant, "platform": platform_name, "arch": asset_arch,
                             "compatible": platform_name == os_name and asset_arch == arch and archive_ext == ext,
                             "url": url, "sha256": digest.removeprefix("sha256:")})
    return variants


def release_variants(config: dict, refresh: bool = False) -> list[dict]:
    repo = config["llama_release_repo"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Dépôt GitHub de llama-server invalide")
    os_name, arch, ext, _ = get_platform_info("cpu")
    key = f"{repo}:{os_name}:{arch}"
    with _cache_lock:
        cached = _cache.get(key)
        if cached and not refresh and time.monotonic() - cached[0] < _TTL_SECONDS:
            return cached[1]
    url = f"https://api.github.com/repos/{repo}/releases?per_page=100"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "ETU-AI-Setup/2"})
    try:
        with safe_urlopen(request, timeout=10) as response:
            releases = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        if cached:
            return cached[1]
        raise RuntimeError("Impossible de lire les versions du serveur sur GitHub") from exc
    if not isinstance(releases, list):
        raise RuntimeError("Réponse GitHub inattendue")
    variants = parse_release_assets(releases, repo, os_name, arch, ext)
    with _cache_lock:
        _cache[key] = (time.monotonic(), variants)
    return variants
