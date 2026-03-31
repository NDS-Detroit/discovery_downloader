import logging
import time, random
from typing import Iterable, Dict, Set, Optional
from boxsdk.exception import BoxAPIException

log = logging.getLogger(__name__)

# ---------- retry helpers ----------
RETRY_STATUSES = {429, 500, 502, 503, 504}

def _is_retryable(exc: Exception) -> bool:
    return isinstance(exc, BoxAPIException) and (exc.status in RETRY_STATUSES)

def _with_retries(fn, *args, retries: int = 5, base: float = 0.8, **kwargs):
    """Call fn with retries/backoff for Box 429/5xx."""
    attempt = 0
    while True:
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if not _is_retryable(e) or attempt >= retries:
                raise
            delay = base * (2 ** attempt) + random.uniform(0, 0.25)
            log.warning(
                "Box transient error %s; retry %s/%s in %.1fs",
                getattr(e, "status", None),
                attempt + 1,
                retries,
                delay,
            )
            time.sleep(delay)
            attempt += 1

# ---------- list items with retries ----------
def _iter_items_with_retries(folder, limit=1000) -> Iterable:
    offset = 0
    while True:
        page = _with_retries(folder.get_items, limit=limit, offset=offset)
        items = list(page)
        if not items:
            return
        for it in items:
            yield it
        offset += len(items)

# ---------- cache folder children to reduce API calls ----------
_children_cache: Dict[str, Dict[str, str]] = {}  # folder_id -> {name: item_id}

def _ensure_children_cache(folder) -> Dict[str, str]:
    fid = folder.object_id
    if fid not in _children_cache:
        names: Dict[str, str] = {}
        for it in _iter_items_with_retries(folder):
            names[it.name] = getattr(it, "id", None)
        _children_cache[fid] = names
    return _children_cache[fid]

def _find_child_by_name(folder, name: str):
    """Exact-name lookup within a Box folder (cached + retried)."""
    names = _ensure_children_cache(folder)
    iid = names.get(name)
    if iid:
        # return a lightweight object without re-listing
        return folder.client.folder(iid) if getattr(folder, "get", None) and False else folder.client.file(iid) if False else None
    # Fall back to a single fresh pass (in case cache is stale)
    for it in _iter_items_with_retries(folder):
        if it.name == name:
            _children_cache[folder.object_id][name] = it.id
            return it
    return None

def _ensure_subfolder(client: Client, parent_folder_id: str, name: str) -> str:
    clean = _sanitize(name)
    parent = client.folder(parent_folder_id)

    # cached lookup
    names = _ensure_children_cache(parent)
    existing_id = names.get(clean)
    if existing_id:
        return existing_id

    # create with retries
    try:
        sub = _with_retries(parent.create_subfolder, clean)
        _children_cache[parent_folder_id][clean] = sub.id
        return sub.id
    except BoxAPIException as e:
        if e.status == 409:
            # someone else created it—refresh cache once
            _children_cache.pop(parent_folder_id, None)
            parent = client.folder(parent_folder_id)
            names = _ensure_children_cache(parent)
            if clean in names:
                return names[clean]
        raise

def _upload_or_rename(client: Client, local_file: pathlib.Path, dest_folder_id: str):
    if is_ignored(local_file):
        return

    folder = client.folder(dest_folder_id)
    names = _ensure_children_cache(folder)

    base, ext = os.path.splitext(local_file.name)
    candidate = local_file.name
    i = 1
    while candidate in names:
        candidate = f"{base} ({i}){ext}"
        i += 1

    size = local_file.stat().st_size
    if size < 32 * 1024 * 1024 or not hasattr(folder, "get_chunked_uploader"):
        with open(local_file, "rb") as f:
            _with_retries(folder.upload_stream, f, candidate)
    else:
        uploader = folder.get_chunked_uploader(str(local_file))
        uploader.file_name = candidate
        # single-thread to avoid duplicate_part_id races we saw earlier
        try:
            if hasattr(uploader, "_thread_pool_size"):
                uploader._thread_pool_size = 1
            elif hasattr(uploader, "_thread_pool") and hasattr(uploader._thread_pool, "_max_workers"):
                uploader._thread_pool._max_workers = 1
        except Exception:
            pass
        _with_retries(uploader.start)

    # update cache so subsequent files see this name
    _children_cache[dest_folder_id][candidate] = "<new>"

def _ensure_folder_path(client, box_path: str, create_missing: bool = True) -> str:
    if not box_path or box_path == "/":
        return "0"
    parts = [p for p in box_path.split("/") if p]
    current_id = "0"
    for seg in parts:
        name = _sanitize(seg)
        parent = client.folder(current_id)
        names = _ensure_children_cache(parent)
        child_id = names.get(name)
        if child_id:
            current_id = child_id
            continue
        if not create_missing:
            raise FileNotFoundError(f"Missing segment '{name}' under folder {current_id}")
        try:
            sub = _with_retries(parent.create_subfolder, name)
            _children_cache[current_id][name] = sub.id
            current_id = sub.id
        except BoxAPIException as e:
            if e.status == 409:
                _children_cache.pop(current_id, None)  # refresh and read again
                parent = client.folder(current_id)
                names = _ensure_children_cache(parent)
                child_id = names.get(name)
                if child_id:
                    current_id = child_id
                    continue
            raise
    return current_id
