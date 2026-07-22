import os
import pathlib
import re
import hashlib
import logging
from pathlib import Path

from boxsdk import JWTAuth, Client # type: ignore
from boxsdk.exception import BoxAPIException
from src.config import BOX_AUTH_PATH, BOX_USER
from src.discovery.discovery_utils import (
    compose_discovery_email, package_summary, identify_source, html_to_text,
    human_readable_size,
)

log = logging.getLogger(__name__)
logging.getLogger("boxsdk").setLevel(logging.WARNING)

# ---------------- config for ignore ----------------
JUNK_DIRS  = {"__MACOSX", ".git", ".venv", ".idea", ".pytest_cache", ".mypy_cache"}
JUNK_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}

def is_ignored(path: pathlib.Path) -> bool:
    name = path.name
    if name in JUNK_FILES or name.startswith("."):
        return True
    for part in path.parts:
        if part in JUNK_DIRS or part.startswith("."):
            return True
    return False

# ---------------- Box folder utilities ----------------
def _sanitize(name: str) -> str:
    return re.sub(r"[^\w\-. ]+", "_", name).strip() or "unnamed"

def _find_child_by_name(folder, name: str, fields=None):
    """Exact-name lookup within a Box folder.

    `fields` is passed through to Box's get_items so callers that need extra
    attributes (e.g. `sha1` for content dedupe) get them populated on the
    returned item. Box always returns `id`/`type` regardless of `fields`."""
    offset = 0
    while True:
        items = list(folder.get_items(limit=1000, offset=offset, fields=fields))
        if not items:
            return None
        for it in items:
            if it.name == name:
                return it
        offset += len(items)


def _sha1_of_file(path: pathlib.Path) -> str:
    """SHA1 of a file's contents — the same digest Box stores per file, so a
    match means Box already has this exact content."""
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def _ensure_folder_path(client, box_path: str, create_missing: bool = True) -> str:
    """Resolve '/A/B/C' to a folder id, optionally creating segments."""
    if not box_path or box_path == "/":
        return "0"
    parts = [p for p in box_path.split("/") if p]
    current_id = "0"
    for seg in parts:
        name = _sanitize(seg)
        parent = client.folder(current_id)
        child = _find_child_by_name(parent, name)
        if child and child.type == "folder":
            current_id = child.id
            continue
        if not create_missing:
            raise FileNotFoundError(f"Missing segment '{name}' under folder {current_id}")
        # create, handling concurrent create/conflict
        try:
            current_id = parent.create_subfolder(name).id
        except BoxAPIException as e:
            if e.status == 409:  # someone else created it
                child = _find_child_by_name(parent, name)
                if child and child.type == "folder":
                    current_id = child.id
                else:
                    raise
            else:
                raise
    return current_id

def _is_same_content_conflict(e: BoxAPIException) -> bool:
    try:
        info = e.context_info or {}
        c = (info.get("conflicts") or {})
        return c.get("sha1") and c.get("file_version", {}).get("sha1") == c.get("sha1")
    except Exception:
        return False


# ---------------- uploads with auto-rename ----------------
def _upload_or_rename(client: Client, local_file: pathlib.Path, dest_folder_id: str):
    """Upload file; auto-rename on conflict."""
    if is_ignored(local_file):
        return
    folder = client.folder(dest_folder_id)
    base, ext = os.path.splitext(local_file.name)

    # Resolve the destination name, content-aware:
    #   same name + same SHA1 already on Box -> already uploaded, skip (idempotent)
    #   same name + different content        -> auto-rename "(1)", "(2)", ...
    #   name free                            -> upload as-is
    # This makes a retry after a partially-failed package upload a no-op for the
    # files that already landed, instead of re-uploading them as "name (1).ext"
    # duplicates. SHA1 is computed only when a name actually collides, so a clean
    # first upload (nothing pre-existing) reads no file bytes for the hash.
    local_sha1 = None
    candidate = local_file.name
    i = 1
    while True:
        existing = _find_child_by_name(
            folder, candidate, fields=["id", "type", "name", "sha1"])
        if existing is None:
            break
        if getattr(existing, "type", None) == "file":
            if local_sha1 is None:
                local_sha1 = _sha1_of_file(local_file)
            if getattr(existing, "sha1", None) == local_sha1:
                log.info("Skipping %s -> Box folder %s (already uploaded, same SHA1)",
                         local_file, dest_folder_id)
                return
        candidate = f"{base} ({i}){ext}"
        i += 1

    log.info("Uploading %s to Box folder %s", local_file, dest_folder_id)
    with open(local_file, "rb") as f:
        # use chunked uploader for large files
        size = local_file.stat().st_size
        if size >= 500 * 1024 * 1024 and hasattr(folder, "get_chunked_uploader"):
            # Large files: create a fresh upload session and upload once.
            while True:
                session = None
                try:
                    # Preflight to catch name conflict or duplicate SHA1
                    try:
                        folder.preflight_check(size=size, name=candidate)
                    except BoxAPIException as pe:
                        if pe.status == 409:
                            if _is_same_content_conflict(pe):
                                log.info("Skipping duplicate (same SHA1): %s", local_file)
                                return
                            candidate = f"{base} ({i}){ext}"; i += 1
                            continue
                        raise

                    # Create a NEW session every attempt
                    session = folder.create_upload_session(size, candidate)
                    uploader = session.get_chunked_uploader(str(local_file))
                    uploader.start()  # uploads all parts + commit
                    break  # success

                except BoxAPIException as e:
                    # If a part already exists in this session, abort and try a fresh session
                    if e.status == 409 and ("Part id" in getattr(e, "message", "") or
                                            "part" in (e.context_info or {}).get("code", "").lower()):
                        try:
                            if session:
                                session.abort()
                        except Exception:
                            pass
                        # try again with a fresh session (same candidate name)
                        continue
                    # Name conflict or same-content duplicate already handled above; otherwise re-raise
                    raise

        else:
            folder.upload_stream(f, candidate)

def _ensure_subfolder(client: Client, parent_folder_id: str, name: str) -> str:
    clean = _sanitize(name)
    parent = client.folder(parent_folder_id)
    existing = _find_child_by_name(parent, clean)
    if existing and existing.type == "folder":
        return existing.id
    try:
        return parent.create_subfolder(clean).id
    except BoxAPIException as e:
        if e.status == 409:  # created elsewhere between find/create
            existing = _find_child_by_name(parent, clean)
            if existing and existing.type == "folder":
                return existing.id
        raise

def upload_folder_recursive(client, local_dir: pathlib.Path, dest_folder_id: str):
    rel_to_box = {pathlib.Path("."): dest_folder_id}
    for root, dirs, files in os.walk(local_dir):
        root_path = pathlib.Path(root)
        # prune junk dirs in-place
        dirs[:] = [d for d in dirs if not is_ignored(root_path / d)]

        rel = root_path.relative_to(local_dir)
        parent_box_id = rel_to_box[rel]

        # ensure subfolders (find-or-create)
        for d in dirs:
            sub_id = _ensure_subfolder(client, parent_box_id, d)
            rel_to_box[rel / d] = sub_id

        # upload files (auto-rename)
        for fname in files:
            fpath = root_path / fname
            if is_ignored(fpath):
                continue
            _upload_or_rename(client, fpath, parent_box_id)

def get_login_as_user(auth_path, username):
    auth = JWTAuth.from_settings_file(str(auth_path))
    admin_client = Client(auth)  # token minted for the app's service account
    # Look up your managed user by email (requires Manage Users scope)
    users = admin_client.users(limit=1000, filter_term=username)
    target = next((u for u in users), None)
    assert target, "User not found or app lacks 'Manage users' scope"

    # Impersonate
    user_client = admin_client.as_user(target)
    return user_client

def upload_folder_to_path(auth_path, local_dir: str | pathlib.Path, box_path: str, as_user: str):
    user_client = get_login_as_user(auth_path, as_user)
    local_dir = pathlib.Path(local_dir).resolve()
    if not local_dir.is_dir():
        raise NotADirectoryError(local_dir)
    dest_id = _ensure_folder_path(user_client, box_path, create_missing=True)
    upload_folder_recursive(user_client, local_dir, dest_id)


def upload_directory_to_box(dirname, attorney, client, subject,
                            sender, body_text, casenum, date_str, team='',
                            extra=None, match_note='') -> str:
    """Upload a discovery package to Box and return the HTML notification body.

    The Box folder is resolved/created first so the email can link straight to
    it (app.box.com/folder/<id> — attorneys already collaborate on Evidence
    Files, so no shared link is needed). Then we summarize the package, compose
    the notification, save a plain-text copy as filelist.txt, and upload."""
    dirname = str(dirname)
    if not os.path.isdir(dirname):
        raise NotADirectoryError(dirname)

    outdir_on_box = Path(dirname).name.split('-package')[0] + f"-{date_str}"
    box_path = "/Evidence Files/" + outdir_on_box

    user_client = get_login_as_user(BOX_AUTH_PATH, BOX_USER)
    dest_id = _ensure_folder_path(user_client, box_path, create_missing=True)
    box_url = f"https://app.box.com/folder/{dest_id}"

    n_files, total_bytes, tree_text = package_summary(dirname)
    source = identify_source(sender, subject, body_text)

    email_kwargs = dict(
        client=client, attorney=attorney, team=team, casenum=casenum,
        sender=sender, body_text=body_text, box_url=box_url,
        box_path="Evidence Files/" + outdir_on_box, date_str=date_str,
        source=source, n_files=n_files, total_bytes=total_bytes,
        extra=extra, match_note=match_note,
    )

    # The emailed notification caps the file tree at 250 lines
    # (package_summary's default) so the reply stays readable.
    email_message = compose_discovery_email(**email_kwargs, tree_text=tree_text)

    # filelist.txt is written into the package and uploaded to Box so an
    # attorney can confirm the WHOLE download arrived — it must list every
    # extracted file, not the 250-line-capped tree. Re-summarize with the cap
    # lifted. (Restores the complete listing that regressed when the
    # notification switched to the capped package_summary tree.)
    _, _, full_tree = package_summary(dirname, max_lines=float('inf'))
    filelist_path = os.path.join(dirname, 'filelist.txt')
    with open(filelist_path, 'w+', encoding='utf-8') as filelist:
        filelist.write(html_to_text(
            compose_discovery_email(**email_kwargs, tree_text=full_tree)))

    log.info("Uploading to Box: %s (%s files, %s)", outdir_on_box, n_files,
             human_readable_size(total_bytes))
    upload_folder_recursive(user_client, Path(dirname).resolve(), dest_id)
    return email_message