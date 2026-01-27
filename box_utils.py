import os
import pathlib
import re
import logging
from pathlib import Path

from boxsdk import JWTAuth, Client # type: ignore
from boxsdk.exception import BoxAPIException
from src.config import BOX_AUTH_PATH, BOX_USER
from src.discovery.discovery_utils import prev_compose_email
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

def _find_child_by_name(folder, name: str):
    """Exact-name lookup within a Box folder."""
    offset = 0
    while True:
        items = list(folder.get_items(limit=1000, offset=offset))
        if not items:
            return None
        for it in items:
            if it.name == name:
                return it
        offset += len(items)

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
    print(f"Uploading {local_file} to Box folder {dest_folder_id}")
    folder = client.folder(dest_folder_id)
    base, ext = os.path.splitext(local_file.name)
    candidate = local_file.name
    i = 1
    # find first free name
    while _find_child_by_name(folder, candidate):
        candidate = f"{base} ({i}){ext}"
        i += 1
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
                                print(f"Skipping duplicate (same SHA1): {local_file}")
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
                            sender, body_text, casenum, date_str) -> str:
    """Uploads a directory to box and composes email message"""
    filelist_path = os.path.join(dirname, 'filelist.txt')
    with open(filelist_path, 'w+', encoding='utf-8') as filelist:
        email_message = prev_compose_email(
            attorney, client, str(dirname), subject, sender, body_text, casenum)
        filelist.write(email_message)
    outdir_on_box = Path(dirname).name.split('-package')[0] + f"-{date_str}"
    print("Uploading to Box:", outdir_on_box)

    upload_folder_to_path(BOX_AUTH_PATH, dirname,
        "/Evidence Files/" + outdir_on_box, BOX_USER)
    return email_message